"""One synthetic provider check. Never reads cases or prints credentials."""

import argparse
import asyncio
from pathlib import Path

from backend.app.agent.rules import recommend_next_step
from backend.app.knowledge import ground_decision, load_knowledge
from backend.app.llm import ExplanationFailure, generate_explanation
from backend.app.model_config import ModelConfigurationError, load_model_settings
from backend.app.models.case import Case, Observation


async def check(*, config_only=False) -> int:
    try:
        settings = load_model_settings()
        print(f"Provider: {settings.provider}")
        print(f"Model: {settings.model}")
        print("Configuration: OK")
        if config_only:
            return 0
        case = Case(
            case_id="synthetic_provider_check", origin="synthetic",
            platform="windows_pc", symptom="packet_loss",
            player_description="Synthetic provider check; not a real player's report.",
            connection_type="ethernet",
            observations=[Observation(
                source="player_report", context="Synthetic affected-match reading",
                latency_ms=55, packet_loss_pct=8, fps=144,
            )],
        )
        decision = ground_decision(
            recommend_next_step(case),
            load_knowledge(Path(__file__).resolve().parents[2] / "knowledge"),
        )
        result = await generate_explanation(decision, settings=settings)
        print("Provider request: OK")
        print(f"Selected supporting card: {result._selected_card_id or 'none (action only)'}")
        print(result.explanation)
        print("The model selected reviewed wording. The recommended action is preserved verbatim.")
        return 0
    except (ModelConfigurationError, ExplanationFailure) as error:
        print(f"Error: {error}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true", help="Check settings without making a model request.")
    args = parser.parse_args()
    return asyncio.run(check(config_only=args.config_only))


if __name__ == "__main__":
    raise SystemExit(main())
