"""Limits for new writes; saved Case models remain backward compatible."""

from backend.app.models.case import Case, CompletedTest, Observation, SymptomAnswer


MAX_REQUEST_BYTES = 64 * 1024
BODY_DEADLINE_SECONDS = 15
MAX_CASE_BYTES = 1024 * 1024
MAX_OBSERVATIONS = 100
MAX_COMPLETED_TESTS = 100


class WriteLimitExceeded(ValueError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _text_limit(value: str | None, field: str, maximum: int) -> None:
    if value is not None and len(value) > maximum:
        raise WriteLimitExceeded(
            f"{field} must contain at most {maximum} characters.", 422,
        )


def check_new_entry(entry: Observation | CompletedTest | SymptomAnswer) -> None:
    if isinstance(entry, Observation):
        _text_limit(entry.context, "Observation context", 2000)
    elif isinstance(entry, CompletedTest):
        _text_limit(entry.test_id, "Test ID", 100)
        _text_limit(entry.conditions, "Test conditions", 2000)
        _text_limit(entry.notes, "Test notes", 4000)


def check_case_capacity(case: Case, payload: str) -> None:
    if len(case.observations) > MAX_OBSERVATIONS:
        raise WriteLimitExceeded(
            f"This case has reached its limit of {MAX_OBSERVATIONS} observations.",
            409,
        )
    if len(case.completed_tests) > MAX_COMPLETED_TESTS:
        raise WriteLimitExceeded(
            f"This case has reached its limit of {MAX_COMPLETED_TESTS} test results.",
            409,
        )
    if len(payload.encode("utf-8")) > MAX_CASE_BYTES:
        raise WriteLimitExceeded(
            "This case would exceed its 1 MiB storage limit. "
            "The new evidence has not been saved.",
            409,
        )


def check_new_case(case: Case, payload: str) -> None:
    _text_limit(case.player_description, "Player description", 4000)
    _text_limit(case.data_center, "Data centre", 200)
    _text_limit(case.onset, "Onset", 500)
    _text_limit(case.confirmed_cause, "Confirmed cause", 4000)
    check_case_capacity(case, payload)
    for entry in (*case.observations, *case.completed_tests):
        check_new_entry(entry)
