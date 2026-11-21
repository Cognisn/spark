"""The Administration landing page and the redirects that keep old URLs working.

``/admin`` lists the Administration areas the signed-in user may open (see
:mod:`spark.web.menu`). A user holding no administrative permission is refused
(403), as every administration page refuses them; personal mode has no
Administration area and is sent to its Settings page.

The old URLs of pages that moved answer with a redirect that keeps the query
string, so bookmarks and links keep working. Each moved page keeps its own
guard; a redirect grants nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from spark.identity.access import current_identity
from spark.web.menu import ADMIN_PATH, admin_menu

router = APIRouter()

#: Old GET paths and the paths they moved to.
MOVED: dict[str, str] = {
    "/audit": "/admin/audit",
    "/audit/export": "/admin/audit/export",
}


def _server() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        return False


def moved_to(request: Request, path: str) -> RedirectResponse:
    """A redirect to ``path`` carrying the request's query string (307 keeps the method)."""
    query = request.url.query
    return RedirectResponse(url=f"{path}?{query}" if query else path, status_code=307)


@router.get(ADMIN_PATH, response_class=HTMLResponse, response_model=None)
async def administration(request: Request) -> Response:
    """The Administration areas this user may open; 403 for a user who may open none."""
    if not _server():
        return RedirectResponse(url="/settings", status_code=303)
    if current_identity(request) is None:
        raise HTTPException(status_code=401, detail="not signed in")
    groups = admin_menu(request)
    if not groups:
        raise HTTPException(status_code=403, detail="forbidden")
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, "admin_index.html", {"groups": groups}
    )
    return response


def _redirect(target: str):  # type: ignore[no-untyped-def]
    async def redirect(request: Request) -> RedirectResponse:
        return moved_to(request, target)

    return redirect


for _old, _new in MOVED.items():
    router.add_api_route(_old, _redirect(_new), methods=["GET"], include_in_schema=False)
