"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the login page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/api/auth")
async def authenticate(request: Request) -> RedirectResponse:
    """Validate the auth code and create a session."""
    form = await request.form()
    code = str(form.get("code", "")).strip()

    auth: object = request.app.state.auth
    if not auth.validate(code):  # type: ignore[union-attr]
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid authentication code. Check the terminal for the correct code."},
            status_code=401,
        )

    session_id = request.app.state.session.create()
    response = RedirectResponse(url="/loading", status_code=303)
    response.set_cookie(
        key="spark_session",
        value=session_id,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/auto-login")
async def auto_login(request: Request) -> RedirectResponse:
    """Auto-login via URL query parameter (used by browser auto-open on startup)."""
    code = request.query_params.get("code", "")
    auth: object = request.app.state.auth
    if not auth.validate(code):  # type: ignore[union-attr]
        return RedirectResponse(url="/login", status_code=303)

    session_id = request.app.state.session.create()
    response = RedirectResponse(url="/loading", status_code=303)
    response.set_cookie(
        key="spark_session",
        value=session_id,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Destroy the session and redirect to login."""
    request.app.state.session.destroy()
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("spark_session")
    return response
