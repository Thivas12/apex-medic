import sqlite3

from fastapi import APIRouter, HTTPException, Request

from backend.app.api.schemas import DeleteCaseReceipt, DeleteCaseRequest
from backend.app.db.case_deletion import (
    CaseChanged,
    CaseNotAvailable,
    DeletionPreview,
    delete_owned_case,
    preview_deletion,
)
from backend.app.db.config import Database


def build_deletion_router(database: Database, cookie_name: str) -> APIRouter:
    # Include this inside the existing owner-protected case router.
    router = APIRouter(tags=["Privacy"])

    @router.get("/cases/{case_id}/deletion-preview", response_model=DeletionPreview)
    def deletion_preview(case_id: str, request: Request) -> DeletionPreview:
        """Preview the owned case and obtain a token for its current contents."""
        try:
            return preview_deletion(database, case_id, request.cookies.get(cookie_name))
        except CaseNotAvailable as error:
            raise HTTPException(404, "Case not found.") from error
        except sqlite3.OperationalError as error:
            raise HTTPException(503, "Case storage is unavailable. Try again shortly.") from error

    @router.delete("/cases/{case_id}", response_model=DeleteCaseReceipt)
    def delete_case(
        case_id: str,
        payload: DeleteCaseRequest,
        request: Request,
    ) -> DeleteCaseReceipt:
        """Delete an owned case only if its preview token is still current."""
        if payload.confirm_case_id != case_id:
            raise HTTPException(422, "Enter the full case ID to confirm deletion.")
        try:
            delete_owned_case(
                database, case_id, request.cookies.get(cookie_name), payload.deletion_token,
            )
        except CaseNotAvailable as error:
            raise HTTPException(404, "Case not found.") from error
        except CaseChanged as error:
            raise HTTPException(409, str(error)) from error
        except sqlite3.OperationalError as error:
            raise HTTPException(503, "Case storage is unavailable. Review the case and try again.") from error

        # Do not clear the cookie here: another tab may just have created a new session.
        return DeleteCaseReceipt(case_id=case_id)

    return router
