"""Pure, ordered troubleshooting policy. Reading a case never mutates it."""

from typing import Literal

from pydantic import Field

from backend.app.models.case import Case, ValidatedModel


DecisionKind = Literal["question", "test", "report"]


class Decision(ValidatedModel):
    case_id: str
    case_origin: Literal["synthetic", "user_report"]
    policy_version: Literal["rules-v1"] = "rules-v1"
    kind: DecisionKind
    action_id: str
    message: str
    reason: str
    source_ids: list[str] = Field(default_factory=list)


COMPARISONS = {
    "compare_data_centers": (
        "Compare readings with a nearby available Apex data centre. "
        "Keep the game mode and connection unchanged. "
        "Record both data-centre names and readings."
    ),
    "compare_background_traffic": (
        "Pause your own nonessential downloads or streams, then compare "
        "readings under otherwise similar conditions."
    ),
}

SUPPORTED_TESTS = set(COMPARISONS) | {"verify_improvement"}


def recommend_next_step(case: Case) -> Decision:
    """Choose one action from saved evidence; order encodes workflow precedence."""

    def decide(
        kind: DecisionKind,
        action_id: str,
        message: str,
        reason: str,
        source_ids: list[str] | None = None,
    ) -> Decision:
        return Decision(
            case_id=case.case_id,
            case_origin=case.origin,
            kind=kind,
            action_id=action_id,
            message=message,
            reason=reason,
            source_ids=source_ids or [],
        )

    if case.status != "investigating":
        return decide(
            "report",
            "review_closed_case",
            "Review the stored case before starting another investigation.",
            f"The case is marked '{case.status}'.",
        )

    if case.symptom == "unknown":
        return decide(
            "question",
            "clarify_symptom",
            "Are you seeing high latency, packet-loss readings, or rubber-banding?",
            "The symptom has not been classified.",
        )

    latest = case.completed_tests[-1] if case.completed_tests else None

    if latest is not None:
        if latest.test_id not in SUPPORTED_TESTS:
            return decide(
                "report",
                "review_unsupported_test",
                "Review this procedure before recommending further changes.",
                f"'{latest.test_id}' is outside this prototype's test catalogue.",
            )

        if latest.test_id == "verify_improvement":
            return decide(
                "report",
                "review_verification",
                "Summarise the verification readings and any remaining symptoms. "
                "This policy does not establish a root cause.",
                f"The most recently recorded verification outcome is "
                f"'{latest.outcome}'.",
            )

        if latest.outcome in {"worsened", "inconclusive"}:
            return decide(
                "question",
                "clarify_test_result",
                "What changed during this test, and what readings were recorded "
                "before and after it?",
                f"'{latest.test_id}' was recorded as '{latest.outcome}'. "
                "Clarify that result before recommending another change.",
            )

        if latest.outcome == "improved":
            return decide(
                "test",
                "verify_improvement",
                "Keep the settings associated with the reported improvement. "
                "Collect another comparable session's readings. "
                "Compare them with the original affected baseline and record "
                "the result under test_id 'verify_improvement'.",
                f"'{latest.test_id}' reports improvement, but repeatability "
                "has not been checked.",
            )

    required = {
        "packet_loss": ("packet_loss_pct",),
        "high_latency": ("latency_ms",),
        "rubber_banding": ("latency_ms", "packet_loss_pct"),
    }[case.symptom]

    observation = case.observations[-1] if case.observations else None

    missing = [
        name
        for name in required
        if observation is None or getattr(observation, name) is None
    ]

    if missing:
        return decide(
            "question",
            "collect_performance_readings",
            "Enable Performance Display and record readings during an affected match.",
            "The most recently recorded observation is missing: "
            + ", ".join(missing),
            ["ea_connection_guide"],
        )

    if case.symptom == "packet_loss" and observation.packet_loss_pct == 0:
        return decide(
            "question",
            "capture_affected_period",
            "Does the symptom still occur? If it does, capture readings while it happens.",
            "The latest recorded sample shows zero packet loss. "
            "That sample alone does not rule out an intermittent problem.",
            source_ids=["ea_connection_guide"],
        )

    completed = {test.test_id for test in case.completed_tests}

    for test_id, instructions in COMPARISONS.items():
        if test_id not in completed:
            return decide(
                "test",
                test_id,
                instructions,
                "Relevant readings are available and this comparison "
                "has not been recorded.",
                ["ea_connection_guide"],
            )

    return decide(
        "report",
        "summarise_unresolved",
        "Summarise the observations, completed comparisons, and remaining uncertainty.",
        "The current comparison catalogue is exhausted "
        "without a verified improvement.",
    )
