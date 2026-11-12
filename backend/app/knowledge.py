"""Validate source attribution and attach only action-specific reviewed cards."""

import json
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    HttpUrl,
    TypeAdapter,
    computed_field,
    field_validator,
    model_validator,
)

from backend.app.agent.rules import Decision
from backend.app.models.case import ValidatedModel


Text = Annotated[str, Field(min_length=1)]


class Source(ValidatedModel):
    source_id: Text
    title: Text
    url: HttpUrl
    publisher: Text
    source_type: Literal["official_guidance"]
    reviewed_on: date
    topics: list[Text]

    @field_validator("url")
    @classmethod
    def approved_url(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.scheme != "https"
            or value.host != "help.ea.com"
            or value.port != 443
            or value.username is not None
            or value.password is not None
        ):
            raise ValueError(
                "Sources must use HTTPS on help.ea.com without credentials."
            )
        return value


class KnowledgeCard(ValidatedModel):
    card_id: Text
    action_ids: list[Text] = Field(min_length=1)
    title: Text
    basis: Literal["official_guidance", "project_policy"]
    text: Text
    source_ids: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_attribution(self) -> Self:
        if self.basis == "official_guidance" and not self.source_ids:
            raise ValueError("Official guidance needs a source ID.")

        if self.basis == "project_policy" and self.source_ids:
            raise ValueError(
                "Project policy must not be attributed to an external source."
            )

        return self


class Guidance(ValidatedModel):
    card_id: str
    title: str
    basis: Literal["official_guidance", "project_policy"]
    text: str
    citations: list[Source]


class GroundedDecision(Decision):
    knowledge_revision: str
    guidance: list[Guidance]

    @computed_field
    @property
    def decision_token(self) -> str:
        snapshot = self.model_dump(
            mode="json",
            exclude={"decision_token"},
        )

        encoded = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
        )

        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KnowledgeBase:
    revision: str
    sources: dict[str, Source]
    cards: tuple[KnowledgeCard, ...]


def load_knowledge(directory: Path) -> KnowledgeBase:
    source_text = (directory / "sources.json").read_text(
        encoding="utf-8-sig"
    )
    card_text = (directory / "guidance.json").read_text(
        encoding="utf-8-sig"
    )

    sources = TypeAdapter(list[Source]).validate_json(source_text)
    cards = TypeAdapter(list[KnowledgeCard]).validate_json(card_text)

    source_ids = [source.source_id for source in sources]
    card_ids = [card.card_id for card in cards]

    if len(source_ids) != len(set(source_ids)):
        raise ValueError("Duplicate source IDs in the knowledge catalogue.")

    if len(card_ids) != len(set(card_ids)):
        raise ValueError("Duplicate card IDs in the knowledge catalogue.")

    known_sources = set(source_ids)

    for card in cards:
        missing = set(card.source_ids) - known_sources
        if missing:
            raise ValueError(
                f"Card '{card.card_id}' references missing sources: "
                f"{sorted(missing)}"
            )

    revision = sha256(
        (source_text + "\n" + card_text).encode("utf-8")
    ).hexdigest()

    return KnowledgeBase(
        revision=revision,
        sources={source.source_id: source for source in sources},
        cards=tuple(cards),
    )


def ground_decision(
    decision: Decision,
    knowledge: KnowledgeBase,
) -> GroundedDecision:
    selected = [
        card
        for card in knowledge.cards
        if decision.action_id in card.action_ids
    ]

    cited = {
        source_id
        for card in selected
        for source_id in card.source_ids
    }

    if cited != set(decision.source_ids):
        raise ValueError(
            "Rule citations and action-specific knowledge cards disagree."
        )

    guidance = [
        Guidance(
            card_id=card.card_id,
            title=card.title,
            basis=card.basis,
            text=card.text,
            citations=[
                knowledge.sources[source_id]
                for source_id in card.source_ids
            ],
        )
        for card in selected
    ]

    return GroundedDecision(
        **decision.model_dump(),
        knowledge_revision=knowledge.revision,
        guidance=guidance,
    )
