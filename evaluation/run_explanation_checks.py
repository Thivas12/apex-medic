import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from backend.app.agent.rules import recommend_next_step
from backend.app.knowledge import ground_decision, load_knowledge
from backend.app.llm import PROMPT_VERSION, ExplanationFailure
from backend.app.model_config import load_model_settings
from backend.app.explanation_service import explain_decision
from backend.app.models.case import Case


ROOT = Path(__file__).resolve().parents[1]


def reading(loss):
    return {
        "source": "player_report",
        "context": "Synthetic affected-match reading",
        "latency_ms": 55.0,
        "packet_loss_pct": float(loss),
        "fps": 144.0,
    }


def result(test_id, outcome):
    return {
        "test_id": test_id,
        "conditions": "Synthetic comparable session; other conditions unchanged",
        "outcome": outcome,
        "notes": "Synthetic reported result; the underlying cause is unknown.",
    }


SCENARIOS = [
    ("unknown_symptom", "clarify_symptom", {
        "symptom": "unknown",
        "observations": [],
    }),
    ("missing_reading", "collect_performance_readings", {
        "observations": [],
    }),
    ("zero_loss", "capture_affected_period", {
        "observations": [reading(0)],
    }),
    ("recorded_loss", "compare_data_centers", {}),
    ("unchanged_comparison", "compare_background_traffic", {
        "completed_tests": [
            result("compare_data_centers", "unchanged"),
        ],
    }),
    ("reported_improvement", "verify_improvement", {
        "completed_tests": [
            result("compare_data_centers", "improved"),
        ],
    }),
    ("repeated_improvement", "review_verification", {
        "completed_tests": [
            result("compare_data_centers", "improved"),
            result("verify_improvement", "improved"),
        ],
    }),
    ("worsened_comparison", "clarify_test_result", {
        "completed_tests": [
            result("compare_data_centers", "worsened"),
        ],
    }),
]


async def main(scenarios=None, suite="development"):
    selected_scenarios = SCENARIOS if scenarios is None else scenarios

    if not selected_scenarios:
        raise ValueError("At least one scenario is required.")

    settings = load_model_settings()

    knowledge = load_knowledge(ROOT / "knowledge")
    prepared = []

    # Check every scenario before making any model requests.
    for name, expected_action, changes in selected_scenarios:
        case = Case.model_validate({
            "case_id": f"eval_{name}",
            "origin": "synthetic",
            "platform": "windows_pc",
            "symptom": "packet_loss",
            "player_description": "Synthetic demo: players teleport in Apex.",
            "connection_type": "ethernet",
            "data_center": None,
            "onset": None,
            "observations": [reading(8)],
            "completed_tests": [],
            "confirmed_cause": None,
            "status": "investigating",
            **changes,
        })

        rule_decision = recommend_next_step(case)

        try:
            decision = ground_decision(rule_decision, knowledge)
        except ValueError:
            print(
                json.dumps(
                    {
                        "scenario": name,
                        "action_id": rule_decision.action_id,
                        "rule_source_ids": rule_decision.source_ids,
                        "matching_guidance": [
                            {
                                "card_id": card.card_id,
                                "source_ids": card.source_ids,
                            }
                            for card in knowledge.cards
                            if rule_decision.action_id in card.action_ids
                        ],
                    },
                    indent=2,
                ),
                flush=True,
            )
            raise

        if decision.action_id != expected_action:
            raise RuntimeError(
                f"{name}: expected {expected_action}, "
                f"got {decision.action_id}"
            )

        prepared.append((name, case, decision))

    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%S_%fZ")

    folder = ROOT / "evaluation" / "runs" / run_id
    folder.mkdir(parents=True, exist_ok=False)

    report_path = folder / "explanations.json"
    report = {
        "started_at": started.isoformat(),
        "configured_provider": settings.provider,
        "configured_model": settings.model,
        "suite": suite,
        "configured_prompt_version": PROMPT_VERSION,
        "knowledge_revision": prepared[0][2].knowledge_revision,
        "latency_note": (
            "Elapsed client time; model residency/cache not controlled."
        ),
        "results": [],
    }

    def save():
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    save()
    print(f"Report: {report_path}", flush=True)

    for index, (name, case, decision) in enumerate(prepared, start=1):
        # Pace this manual evaluation on the cloud plan; failures are never retried.
        if settings.provider == "groq" and index > 1 and decision.kind != "report":
            print("Waiting 30 seconds between cloud evaluation requests.", flush=True)
            await asyncio.sleep(30)
        row = {
            "scenario": name,
            "case": case.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "review": {
                "action_preserved": None,
                "no_unsupported_claims": None,
                "uncertainty_preserved": None,
                "notes": "",
            },
        }

        print(f"\n[{index}/{len(prepared)}] {name}", flush=True)
        print(f"Recommended: {decision.message}", flush=True)

        before = perf_counter()

        try:
            explanation = await explain_decision(decision)

            row.update(
                status="valid_output",
                explanation=explanation.explanation,
                source=explanation.source,
                model=explanation.model,
                provider=explanation.provider,
                selected_card_id=explanation.selected_card_id,
                prompt_version=explanation.prompt_version,
                word_count=len(explanation.explanation.split()),
            )

        except ExplanationFailure as error:
            row.update(
                status="error",
                error=str(error),
                status_code=error.status_code,
            )

        row["elapsed_s"] = round(perf_counter() - before, 3)
        report["results"].append(row)
        save()

        print(
            f"{row['status']} | {row.get('source', 'unavailable')} | "
            f"{row['elapsed_s']} s",
            flush=True,
        )

        print(
            row.get("explanation", row.get("error", "")),
            flush=True,
        )

    valid = sum(
        row["status"] == "valid_output"
        for row in report["results"]
    )

    print(f"\nValid outputs: {valid}/{len(prepared)}")
    for source in ("model_selected_guidance", "reviewed_guidance"):
        count = sum(
            row.get("source") == source
            for row in report["results"]
        )
        print(f"{source}: {count}")
    print(
        "Review the selected supporting text for relevance. "
        "The current action is preserved; source guidance still requires review."
    )
    print(f"Saved: {report_path}")

    return 0 if valid == len(prepared) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
