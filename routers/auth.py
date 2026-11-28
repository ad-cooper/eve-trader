import threading
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse
from services.esi import fetch_character_orders
from services.sso import get_auth_url, exchange_auth_code
from services.accounting import calculate_performance
from workflows.scheduler import refresh_import_opportunities_job

router = APIRouter(prefix="/sso", tags=["auth"])


@router.get("/login")
async def sso_login(role: str = "primary"):
    return RedirectResponse(get_auth_url(state=role or "primary"))


@router.get("/callback")
async def sso_callback(code: str = None, state: str = None, error: str = None):
    if error:
        return HTMLResponse(f"<h3>CCP SSO Error: {error}</h3><p><a href='/'>Back to Dashboard</a></p>", status_code=400)
    if not code:
        return HTMLResponse("<h3>Error: No authorization code received</h3><p><a href='/'>Back to Dashboard</a></p>", status_code=400)

    role = state or "primary"
    bundle = exchange_auth_code(code, role=role)
    if role == "cj6mt":
        if not bundle:
            return HTMLResponse(
                "<h3>C-J6MT Seller SSO Exchange Failed</h3>"
                "<p>Ensure <code>esi-markets.structure_markets.v1</code>, <code>esi-assets.read_assets.v1</code>, and <code>esi-markets.read_character_orders.v1</code> are enabled.</p>"
                "<p><a href='/'>Back to Dashboard</a></p>",
                status_code=400,
            )
        threading.Thread(target=refresh_import_opportunities_job, daemon=True).start()
        return RedirectResponse("/?tab=imports", status_code=303)

    if not bundle:
        return HTMLResponse(
            "<h3>SSO Token Exchange Failed</h3>"
            "<p>Check <code>docker compose logs</code> to see the exact rejection message from CCP.</p>"
            "<p><a href='/'>Back to Dashboard</a></p>",
            status_code=400,
        )

    fetch_character_orders()
    calculate_performance()
    return RedirectResponse("/", status_code=303)
