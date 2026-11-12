"""Validated HTTP payloads, separate from application wiring and domain state."""

from typing import Literal

from pydantic import Field

from backend.app.models.case import (
    CompletedTest,
    Observation,
    SymptomAnswer,
    ValidatedModel,
)


class CaseCreate(ValidatedModel):
    player_description: str = Field(min_length=1, max_length=4000)
    origin: Literal["synthetic", "user_report"] = "user_report"

    symptom: Literal[
        "unknown",
        "high_latency",
        "packet_loss",
        "rubber_banding",
    ] = "unknown"

    connection_type: Literal[
        "unknown",
        "ethernet",
        "wifi",
    ] = "unknown"

    data_center: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    onset: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )


class EventRequest(ValidatedModel):
    event_id: str = Field(min_length=1, max_length=100)


class ObservationRequest(EventRequest):
    entry: Observation


class TestRequest(EventRequest):
    entry: CompletedTest


class AnswerRequest(EventRequest):
    entry: SymptomAnswer


class EventReceipt(ValidatedModel):
    case_id: str
    event_id: str
    added: bool


class ExplanationRequest(ValidatedModel):
    decision_token: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class DeleteCaseRequest(ValidatedModel):
    confirm_case_id: str = Field(min_length=1, max_length=100)
    deletion_token: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$",
    )


class DeleteCaseReceipt(ValidatedModel):
    case_id: str
    deleted: Literal[True] = True
