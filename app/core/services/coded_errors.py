"""Error bodies that always carry a machine-readable code.

The consent and account-deletion routes answer every error as
{"detail": str, "code": str}, so the frontend can branch on the code and show the
detail. HTTP errors are raised as BodyHTTPException (rpc_errors.py), whose handler
sends the body as it is. A request body that fails validation would otherwise get
FastAPI's own 422, a list of errors with no code; CodedErrorRoute turns it into
the same shape. 401s come from the shared login dependency and stay as they are.
"""

from typing import Callable

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.core.services.rpc_errors import BodyHTTPException


def coded(status_code: int, detail: str, code: str, **extra) -> BodyHTTPException:
    return BodyHTTPException(status_code, {"detail": detail, "code": code, **extra})


def _validation_detail(exc: RequestValidationError) -> str:
    problems = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ())[1:])
        problems.append(f"{where}: {error.get('msg')}" if where else str(error.get("msg")))
    return "The request is not valid. " + "; ".join(problems) if problems else "The request is not valid."


class CodedErrorRoute(APIRoute):
    """A route whose body-validation failure answers 422 {"detail": str,
    "code": "validation_error"} instead of FastAPI's list of errors."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                return JSONResponse(status_code=422,
                                    content={"detail": _validation_detail(exc), "code": "validation_error"})

        return handler
