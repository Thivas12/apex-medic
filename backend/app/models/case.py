from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ValidatedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        str_strip_whitespace=True,
    )


class Observation(ValidatedModel):
    source: Literal["player_report", "manual_measurement"]
    context: str = Field(min_length=1)

    latency_ms: float | None = Field(default=None, ge=0)
    packet_loss_pct: float | None = Field(default=None, ge=0, le=100)
    fps: float | None = Field(default=None, ge=0)


class CompletedTest(ValidatedModel):
    test_id: str = Field(min_length=1)
    conditions: str = Field(min_length=1)
    outcome: Literal[
        "improved",
        "unchanged",
        "worsened",
        "inconclusive",
    ]
    notes: str = Field(min_length=1)


class Case(ValidatedModel):
    case_id: str = Field(min_length=1, max_length=100)
    origin: Literal["synthetic", "user_report"]
    platform: Literal["windows_pc"]

    symptom: Literal[
        "high_latency",
        "packet_loss",
        "rubber_banding",
        "unknown",
    ]

    player_description: str = Field(min_length=1)
    connection_type: Literal["ethernet", "wifi", "unknown"]

    data_center: str | None = None
    onset: str | None = None

    observations: list[Observation] = Field(default_factory=list)
    completed_tests: list[CompletedTest] = Field(default_factory=list)

    confirmed_cause: str | None = None
    status: Literal[
        "investigating",
        "resolved",
        "unresolved",
    ] = "investigating"


class SymptomAnswer(ValidatedModel):
    question_id: Literal["clarify_symptom"]
    symptom: Literal[
        "high_latency",
        "packet_loss",
        "rubber_banding",
    ]
