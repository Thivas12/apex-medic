"""Build allowed explanations before the model selects one.

The selected action is always included verbatim. Supporting text is copied
only from the guidance already attached to that action by the knowledge layer.
"""

from dataclasses import dataclass

from backend.app.knowledge import GroundedDecision


WORD_LIMIT = 80
CHARACTER_LIMIT = 1000


@dataclass(frozen=True)
class ExplanationOption:
    text: str
    card_id: str | None = None


def explanation_options(decision: GroundedDecision) -> dict[str, ExplanationOption]:
    def fits(text: str) -> bool:
        return (
            bool(text.strip())
            and len(text) <= CHARACTER_LIMIT
            and len(text.split()) <= WORD_LIMIT
        )

    if not fits(decision.message):
        raise ValueError("The full recommendation exceeds the explanation size limit.")

    options = {"step": ExplanationOption(decision.message)}
    for index, card in enumerate(decision.guidance, start=1):
        text = f"{decision.message}\n\n{card.text}"
        if fits(text):
            options[f"guidance_{index}"] = ExplanationOption(text, card.card_id)
    return options
