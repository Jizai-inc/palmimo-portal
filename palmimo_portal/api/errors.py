"""The error envelope every endpoint answers with, and the exception that carries it.

The technical design fixes the shape for every error response, machine-readable
and translation-ready::

    {"error": {"code": "<snake_case_i18n_key>", "params": {...}}}

The backend never puts a human sentence in an error body — ``code`` is an
i18n key the frontend's locale resource resolves, and ``params`` carries
whatever the message needs to interpolate (e.g. an SSID). This module is
shared by ``api/`` (which raises :class:`PortalError`) and ``app.py`` (which
registers the exception handlers that translate it, and every other error
FastAPI can raise, into this shape).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class PortalError(HTTPException):
    """An HTTP error whose body is the Portal's error envelope.

    Raise this instead of a bare :class:`~fastapi.HTTPException` from any
    ``api/`` module so the response body always carries a machine-readable
    ``code`` rather than a free-text ``detail``.
    """

    def __init__(self, status_code: int, code: str, **params: Any) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "params": params})
