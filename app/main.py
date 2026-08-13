from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes.db import router as db_router
from app.api.routes.drive import router as drive_router
from app.api.routes.pages import router as pages_router
from app.api.routes.processing import router as processing_router
from app.core.config import get_settings
from app.db import get_db
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from app.services.ad_auth import ActiveDirectoryAuthError, authenticate_ad_user
from app.services.local_auth import (
    authenticate_local_user,
    find_user,
    normalize_username,
    session_user,
)


settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title=settings.app_name)
# Le dice a FastAPI que confíe en los encabezados enviadas por Nginx (X-Forwarded-Proto, etc.)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

PUBLIC_PATHS = {"/login"}


def safe_next_url(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/") or path in PUBLIC_PATHS:
        return await call_next(request)
    if request.session.get("user") is not None:
        return await call_next(request)
    return RedirectResponse(f"/login?next={quote(path, safe='/')}", status_code=303)


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: str | None = None):
    if request.session.get("user"):
        return RedirectResponse(safe_next_url(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next_url": safe_next_url(next), "error": None},
    )


@app.post("/login", include_in_schema=False)
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next_url: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    redirect_to = safe_next_url(next_url)
    normalized_username = normalize_username(username)
    database_user = find_user(db, normalized_username)

    if database_user is None or not database_user.status:
        user = None
        source = ""
        error_message = "El usuario no está habilitado en la aplicación."
    elif database_user.origen.strip().upper() == "LOCAL":
        user = authenticate_local_user(db, normalized_username, password)
        source = "LOCAL"
        error_message = "Usuario o contraseña incorrectos."
    else:
        try:
            authenticate_ad_user(normalized_username, password)
            user = database_user
            source = "AD"
            error_message = ""
        except ActiveDirectoryAuthError as error:
            user = None
            source = "AD"
            error_message = str(error)

    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next_url": redirect_to,
                "error": error_message,
                "username": normalized_username,
            },
            status_code=401,
        )

    request.session["user"] = session_user(user, source)
    return RedirectResponse(redirect_to, status_code=303)


@app.post("/logout", include_in_schema=False)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


app.include_router(pages_router)
app.include_router(db_router)
app.include_router(drive_router)
app.include_router(processing_router)
