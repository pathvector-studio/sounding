"""The read-only web UI, JSON API and Atom feed (spec phase 3, R6-R9).

The web process reads the database and never writes to it. It is built by ``create_app``; ``python -m
sounding.web`` runs it under uvicorn. Pages are server-rendered Jinja2 (autoescape on) without JavaScript.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import jinja2
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from sounding.config import Settings
from sounding.profile import ProfileError, load_profile
from sounding.web import auth, queries
from sounding.web.feed import FEED_LIMIT, build_feed
from sounding.web.i18n import labels

log = logging.getLogger("sounding.web")

HERE = Path(__file__).resolve().parent
MAX_FORM_BYTES = 4096
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
OPEN_PATHS = ("/login", "/healthz")
OPEN_PREFIXES = ("/static/", "/feed/")  # the feed checks its own token


def safe_next(value: str | None) -> str:
    """A same-site path (one leading slash) to go to after login, else ``/``. Closes open redirects."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return "/"
    parts = urlsplit(value)
    return value if not parts.scheme and not parts.netloc else "/"


class _Env:
    """Jinja environment plus the profile language, re-read per request (the profile may be edited)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(HERE / "templates")), autoescape=True, undefined=jinja2.StrictUndefined,
        )
        self.env.filters["safe_url"] = queries.clean_url
        self.env.filters["dt"] = lambda v: "" if not v else str(v).replace("T", " ")[:16]
        self._last_profile_error: str | None = None

    def language(self) -> str:
        try:
            lang = load_profile(self.settings.profile_path).profile.language or "en"
            self._last_profile_error = None
            return lang
        except ProfileError as exc:
            if str(exc) != self._last_profile_error:  # log each distinct error once, not on every request
                log.error("profile unreadable, UI labels fall back to en: %s", exc)
                self._last_profile_error = str(exc)
            return "en"


def create_app(
    settings: Settings, session_factory: Callable[[], Session] | None = None, *, throttle: auth.LoginThrottle | None = None
) -> FastAPI:
    secret = settings.session_secret.get_secret_value()
    pw_hash = settings.web_password_hash.get_secret_value().strip()
    auth.validate_web_config(secret, pw_hash)
    if session_factory is None:
        from sounding.db import get_session_factory

        session_factory = get_session_factory()
    if not pw_hash:
        log.warning("WEB_PASSWORD_HASH is empty: the web UI has NO login. Use it only behind Tailscale or an access proxy.")

    base_url = settings.public_base_url
    public_origin = "{0.scheme}://{0.netloc}".format(urlsplit(base_url))
    cookie_secure = base_url.lower().startswith("https")
    env = _Env(settings)
    throttle = throttle or auth.LoginThrottle()
    atom_path = f"/feed/{auth.feed_token(secret)}.atom"

    app = FastAPI(title="Sounding", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.throttle = throttle
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # --- helpers --------------------------------------------------------------------------------

    def render(name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        t = labels(env.language())
        html = env.env.get_template(name).render(t=t, no_password=not pw_hash, has_login=bool(pw_hash), **ctx)
        return HTMLResponse(html, status_code=status)

    def authed(request: Request) -> bool:
        return not pw_hash or auth.verify_session(secret, request.cookies.get(auth.SESSION_COOKIE))

    def origin_ok(request: Request) -> bool:
        origin = request.headers.get("origin")
        if origin is None:
            return True  # SameSite=Lax already keeps the cookie off cross-site POSTs
        if origin == public_origin:
            return True
        return urlsplit(origin).netloc == request.headers.get("host")  # same-origin even when reached by another name

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # --- guard: CSRF origin check, login requirement, security headers ---------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS") and not origin_ok(request):
            response: Response = PlainTextResponse("cross-origin request refused", status_code=403)
        elif not authed(request) and path not in OPEN_PATHS and not path.startswith(OPEN_PREFIXES):
            if path.startswith("/api/"):
                response = JSONResponse({"detail": "login required"}, status_code=401)
            else:
                target = path + (f"?{request.url.query}" if request.url.query else "")
                response = RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)
        else:
            response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        if not path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 404:
            return render("not_found.html", 404)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_params(request: Request, exc: RequestValidationError) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "invalid request"}, status_code=422)
        return render("not_found.html", 404)

    # --- no auth ---------------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/feed/{token}.atom")
    def atom(token: str) -> Response:
        if not auth.feed_token_ok(secret, token):
            return PlainTextResponse("not found", status_code=404)
        with session_factory() as s:
            digests = queries.recent_digests(s, FEED_LIMIT)
        xml = build_feed(digests, base_url=base_url, feed_url=base_url + atom_path)
        return Response(xml, media_type="application/atom+xml")

    # --- login -----------------------------------------------------------------------------------

    @app.get("/login")
    def login_form(request: Request, next: str = "/") -> Response:
        if not pw_hash or authed(request):
            return RedirectResponse(safe_next(next), status_code=303)
        return render("login.html", next=safe_next(next), error=None)

    @app.post("/login")
    async def login(request: Request) -> Response:
        if not pw_hash:
            return RedirectResponse("/", status_code=303)
        raw = await request.body()
        if len(raw) > MAX_FORM_BYTES:
            return PlainTextResponse("too large", status_code=413)
        form = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        password = (form.get("password") or [""])[0]
        nxt = safe_next((form.get("next") or ["/"])[0])
        key = client_key(request)
        wait = throttle.retry_after(key)
        t = labels(env.language())
        if wait:
            r = render("login.html", 429, next=nxt, error=t["login_throttled"].format(n=wait))
            r.headers["Retry-After"] = str(wait)
            return r
        if await run_in_threadpool(auth.verify_password, password, pw_hash):
            throttle.success(key)
            resp = RedirectResponse(nxt, status_code=303)
            resp.set_cookie(
                auth.SESSION_COOKIE, auth.make_session(secret), max_age=auth.SESSION_LIFETIME,
                httponly=True, samesite="lax", secure=cookie_secure, path="/",
            )
            return resp
        throttle.failure(key)
        log.warning("failed login from %s", key)  # never the submitted password
        return render("login.html", 401, next=nxt, error=t["login_failed"])

    @app.post("/logout")
    def logout() -> Response:
        resp = RedirectResponse("/login" if pw_hash else "/", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=cookie_secure)
        return resp

    # --- API ---------------------------------------------------------------------------------------

    def not_found(what: str) -> StarletteHTTPException:
        return StarletteHTTPException(404, f"{what} not found")

    @app.get("/api/digests")
    def api_digests(page: int = 1) -> dict[str, Any]:
        with session_factory() as s:
            return queries.digest_list(s, page)

    @app.get("/api/digests/latest")
    def api_latest() -> dict[str, Any]:
        with session_factory() as s:
            d = queries.latest_digest(s)
        if d is None:
            raise not_found("digest")
        return d

    @app.get("/api/digests/{digest_id}")
    def api_digest(digest_id: int) -> dict[str, Any]:
        with session_factory() as s:
            d = queries.digest_by_id(s, digest_id)
        if d is None:
            raise not_found("digest")
        return d

    @app.get("/api/conversations/{conversation_id}")
    def api_conversation(conversation_id: int) -> dict[str, Any]:
        with session_factory() as s:
            c = queries.conversation_detail(s, conversation_id, env.language())
        if c is None:
            raise not_found("conversation")
        return c

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        with session_factory() as s:
            return queries.status_info(s, settings, base_url + atom_path)

    # --- pages -------------------------------------------------------------------------------------

    @app.get("/")
    def page_latest() -> Response:
        with session_factory() as s:
            d = queries.latest_digest(s)
        return render("digest.html", digest=d)

    @app.get("/digests")
    def page_digests(page: int = 1) -> Response:
        with session_factory() as s:
            data = queries.digest_list(s, page)
        return render("digests.html", **data)

    @app.get("/digests/{digest_id}")
    def page_digest(digest_id: int) -> Response:
        with session_factory() as s:
            d = queries.digest_by_id(s, digest_id)
        if d is None:
            raise not_found("digest")
        return render("digest.html", digest=d)

    @app.get("/conversations/{conversation_id}")
    def page_conversation(conversation_id: int) -> Response:
        with session_factory() as s:
            c = queries.conversation_detail(s, conversation_id, env.language())
        if c is None:
            raise not_found("conversation")
        return render("conversation.html", c=c, sources_by_ref={x["ref"]: x for x in (c["state"] or {}).get("sources", [])})

    @app.get("/status")
    def page_status() -> Response:
        with session_factory() as s:
            info = queries.status_info(s, settings, base_url + atom_path)
        return render("status.html", info=info)

    return app
