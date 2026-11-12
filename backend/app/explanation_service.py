from collections.abc import Awaitable, Callable

from backend.app.knowledge import GroundedDecision
from backend.app.llm import (
    MODEL,
    PROMPT_VERSION,
    ExplanationResponse,
    ModelExplanation,
    generate_explanation,
)


async def explain_decision(
    decision: GroundedDecision,
    *,
    generator: Callable[
        [GroundedDecision], Awaitable[ModelExplanation]
    ] | None = None,
) -> ExplanationResponse:
    if decision.kind == "report":
        text = f"{decision.message}\n\n{decision.reason}"
        source = "reviewed_guidance"
        model = None
        prompt_version = None
        provider_name = None
        selected_card_id = None
    else:
        provider = generator if generator is not None else generate_explanation
        result = await provider(decision)
        text = result.explanation
        source = "model_selected_guidance"
        model = result._model_name or MODEL
        provider_name = result._provider_name
        selected_card_id = result._selected_card_id
        prompt_version = PROMPT_VERSION

    return ExplanationResponse(
        explanation=text,
        case_id=decision.case_id,
        action_id=decision.action_id,
        knowledge_revision=decision.knowledge_revision,
        source=source,
        model=model,
        prompt_version=prompt_version,
        provider=provider_name,
        selected_card_id=selected_card_id,
    )
