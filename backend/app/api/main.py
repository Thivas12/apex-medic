"""FastAPI composition root: dependencies, middleware, routes and UI mounting."""

import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from backend.app.agent.rules import recommend_next_step
from backend.app.api.access import (
    COOKIE_NAME,
    DEFAULT_ORIGIN,
    mutation_guard,
    validate_public_origin,
)
from backend.app.api.body_limit import RequestBodyLimit
from backend.app.api.deletion import build_deletion_router
from backend.app.api.explanations import build_explanation_router
from backend.app.api.rate_limit import RequestRateLimit
from backend.app.api.schemas import (
    AnswerRequest,
    CaseCreate,
    EventReceipt,
    ObservationRequest,
    TestRequest,
)
from backend.app.db.config import Database, StorageUnavailable, resolve_database
from backend.app.db.ownership import (
    create_owned_case,
    initialise_ownership,
    owns_case,
)
from backend.app.db.rate_limits import initialise_rate_limits, rate_database
from backend.app.db.repository import (
    append_evidence,
    get_case,
    get_history,
    initialise_database,
)
from backend.app.knowledge import GroundedDecision, ground_decision, load_knowledge
from backend.app.limits import WriteLimitExceeded
from backend.app.models.case import Case, ValidatedModel


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def create_app(
    database_path: Database | None = None,
    *,
    public_origin: str = DEFAULT_ORIGIN,
) -> FastAPI:
    """Build an app with explicit storage and origin settings for tests or hosting."""
    origin = validate_public_origin(public_origin)
    check_mutation = mutation_guard(origin)
    cookie_name = (
        "__Host-" + COOKIE_NAME if origin.startswith("https://") else COOKIE_NAME
    )
    database = resolve_database(database_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.knowledge = load_knowledge(
            PROJECT_ROOT / "knowledge"
        )
        initialise_database(database)
        initialise_ownership(database)
        initialise_rate_limits(rate_database(database))
        yield

    application = FastAPI(
        title="ApexMedic API",
        version="0.1.0",
        description=(
            "Evidence-driven Apex Legends connection troubleshooting. "
            "Rules select the next action; the optional model selects reviewed "
            "supporting text. Neither a reported improvement nor a model "
            "selection establishes a diagnosis. Create a case in this browser "
            "first: subsequent case operations require its session cookie."
        ),
        openapi_tags=[
            {"name": "Health", "description": "Process liveness; not a dependency probe."},
            {"name": "Cases", "description": "Create and reopen browser-owned cases."},
            {"name": "Evidence", "description": "Record evidence with idempotent event IDs."},
            {"name": "Guidance", "description": "Rule decisions and constrained guidance selection."},
            {"name": "Privacy", "description": "Preview and delete an owned case."},
        ],
        lifespan=lifespan,
        dependencies=[Depends(check_mutation)],
    )

    @application.middleware("http")
    async def private_response_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if request.url.path == "/cases" or request.url.path.startswith("/cases/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    application.add_middleware(RequestBodyLimit)
    # Added last: count attempts before receiving or parsing their bodies.
    application.add_middleware(RequestRateLimit, database=rate_database(database))

    @application.exception_handler(StorageUnavailable)
    async def storage_error(
        request: Request, error: StorageUnavailable,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(error)},
            headers={"Cache-Control": "no-store", "Retry-After": "5"},
        )

    @application.exception_handler(WriteLimitExceeded)
    async def write_limit_error(
        request: Request, error: WriteLimitExceeded,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": str(error)},
            headers={"Cache-Control": "no-store"},
        )

    def require_owner(case_id: str, request: Request) -> None:
        if not owns_case(database, case_id, request.cookies.get(cookie_name)):
            raise HTTPException(404, "Case not found.")

    case_router = APIRouter(dependencies=[Depends(require_owner)])

    def require_case(case_id: str) -> Case:
        case = get_case(database, case_id)

        if case is None:
            raise HTTPException(
                status_code=404,
                detail="Case not found.",
            )

        return case

    def record_event(
        case_id: str,
        event_type: str,
        event_id: str,
        entry: ValidatedModel,
    ) -> EventReceipt:
        require_case(case_id)

        try:
            added = append_evidence(
                database,
                case_id,
                event_type,
                entry.model_dump_json(),
                event_id,
            )
        except (ValidationError, WriteLimitExceeded):
            raise
        except ValueError as error:
            raise HTTPException(
                status_code=409,
                detail=str(error),
            ) from error

        return EventReceipt(
            case_id=case_id,
            event_id=event_id,
            added=added,
        )

    @application.get("/health", tags=["Health"])
    def health() -> dict[str, str]:
        """Report process liveness without making database or provider requests."""
        return {"status": "ok"}

    @application.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/ui/", status_code=307)

    @application.post(
        "/cases",
        response_model=Case,
        status_code=201,
        tags=["Cases"],
    )
    def new_case(
        payload: CaseCreate, request: Request, response: Response,
    ) -> Case:
        case = Case(
            case_id=uuid4().hex,
            platform="windows_pc",
            **payload.model_dump(),
        )

        session = create_owned_case(
            database, case, request.cookies.get(cookie_name),
        )
        response.set_cookie(
            key=cookie_name,
            value=session.token,
            max_age=max(0, session.expires_at - int(time.time())),
            httponly=True,
            secure=origin.startswith("https://"),
            samesite="strict",
            path="/",
        )
        return case

    @case_router.get(
        "/cases/{case_id}",
        response_model=Case,
        tags=["Cases"],
    )
    def read_case(case_id: str) -> Case:
        return require_case(case_id)

    @case_router.get(
        "/cases/{case_id}/next-step",
        response_model=GroundedDecision,
        tags=["Guidance"],
    )
    def next_step(case_id: str) -> GroundedDecision:
        """Return a deterministic action, reviewed sources and a freshness token."""
        decision = recommend_next_step(require_case(case_id))

        return ground_decision(
            decision,
            application.state.knowledge,
        )

    @case_router.get("/cases/{case_id}/history", tags=["Evidence"])
    def history(case_id: str) -> list[dict]:
        require_case(case_id)
        return get_history(database, case_id)

    @case_router.post(
        "/cases/{case_id}/observations",
        response_model=EventReceipt,
        tags=["Evidence"],
    )
    def add_observation(
        case_id: str,
        payload: ObservationRequest,
    ) -> EventReceipt:
        return record_event(
            case_id,
            "observation",
            payload.event_id,
            payload.entry,
        )

    @case_router.post(
        "/cases/{case_id}/tests",
        response_model=EventReceipt,
        tags=["Evidence"],
    )
    def add_test(
        case_id: str,
        payload: TestRequest,
    ) -> EventReceipt:
        return record_event(
            case_id,
            "test",
            payload.event_id,
            payload.entry,
        )

    @case_router.post(
        "/cases/{case_id}/answers",
        response_model=EventReceipt,
        tags=["Evidence"],
    )
    def answer(
        case_id: str,
        payload: AnswerRequest,
    ) -> EventReceipt:
        return record_event(
            case_id,
            "answer",
            payload.event_id,
            payload.entry,
        )
    case_router.include_router(build_deletion_router(database, cookie_name))
    case_router.include_router(build_explanation_router(next_step))
    application.include_router(case_router)

    application.mount(
        "/ui",
        StaticFiles(
            directory=PROJECT_ROOT / "frontend",
            html=True,
        ),
        name="ui",
    )

    return application


app = create_app(
    public_origin=os.environ.get("APEX_PUBLIC_ORIGIN", DEFAULT_ORIGIN),
)
