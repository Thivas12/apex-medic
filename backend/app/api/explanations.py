from collections.abc import Callable
from threading import Lock

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from backend.app.api.schemas import ExplanationRequest
from backend.app.explanation_service import explain_decision
from backend.app.knowledge import GroundedDecision
from backend.app.llm import (
    ExplanationFailure,
    ExplanationResponse,
    generate_explanation,
)


def build_explanation_router(
    get_decision: Callable[[str], GroundedDecision],
) -> APIRouter:
    router = APIRouter()
    generation_lock = Lock()

    @router.post(
        "/cases/{case_id}/explanation",
        response_model=ExplanationResponse,
        tags=["Guidance"],
    )
    async def explain_case(
        case_id: str,
        payload: ExplanationRequest,
    ) -> ExplanationResponse:
        """Select reviewed text only while the supplied decision is current."""
        decision = await run_in_threadpool(get_decision, case_id)

        if payload.decision_token != decision.decision_token:
            raise HTTPException(
                409,
                "The recommendation changed. Refresh the case and try again.",
            )

        locked = False

        if decision.kind != "report":
            locked = generation_lock.acquire(blocking=False)

            if not locked:
                raise HTTPException(
                    429,
                    "Another explanation is running. Try again shortly.",
                )

        try:
            result = await explain_decision(
                decision,
                generator=generate_explanation,
            )

            current = await run_in_threadpool(get_decision, case_id)

            if current != decision:
                raise HTTPException(
                    409,
                    "The recommendation changed while preparing the explanation. "
                    "Refresh the case and try again.",
                )

            return result

        except ExplanationFailure as error:
            raise HTTPException(
                error.status_code,
                str(error),
                headers={"Retry-After": str(error.retry_after)} if error.retry_after is not None else None,
            ) from error

        finally:
            if locked:
                generation_lock.release()

    return router
