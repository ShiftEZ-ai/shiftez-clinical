from .services import catalogue_proof_identity  # pins source before application modules are loaded
from contextlib import asynccontextmanager
from datetime import date, timedelta, timezone
from zoneinfo import ZoneInfo
import copy
import base64
import hashlib
import json
import logging
import re
import signal
from pathlib import Path
from typing import Literal
import secrets
import time
import traceback
from collections import OrderedDict
from threading import Event, Lock
from urllib.parse import urlencode, urlparse
from dateutil.relativedelta import relativedelta
from anyio.to_thread import current_default_thread_limiter
from fastapi.exceptions import RequestValidationError
from fastapi import BackgroundTasks, Depends, FastAPI, Request, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import defer, load_only, undefer
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError, TimeoutError as PoolTimeout
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.datastructures import UploadFile
from starlette.routing import Match
from packages.contracts import schemas as s
from packages.domain.generation import MANIFEST
from packages.domain.recurrence import stored_series_definition
from packages.domain.reports import workload, report_csv, assignments_csv, calendar
from packages.domain.validation import evaluate
from packages.domain.conditions import active_rules, scope_matches_throughout
from . import models as m
from . import oidc
from .auth import (authenticate, password_login, password_login_enabled, login_event, login_digest, digest, keep_alive, require_permission, require_role, current_roles, effective_roles, revoke_session, revoke_session_installations, require_catalogue_read,
                   session_event, session_cookie_max_age, identifier_method, rotate_session)
from .commands import execute, authorize_receipt, logged_command_kind, command_roles, receipt_result
from .db import DatabaseWriteTimeout, allow_long_idle, make_engine, transaction, retry_write, liveness_write, request_db_time_ms
from .version import __version__, build
from .rate_limit import enforce, session_identity
from .errors import Problem, integrity_problem, problem_payload, require, safe_message, validation_issues
from .logging_setup import configure_logging
from .serialization import record, job_record, json_value, projection_json_value
from .services.leave_disclosure import disclose_leave
from .settings import Settings
from .read_io import IndependentFileResponse, IndependentStaticFiles, background_io
from packages.web_assets import WEB, asset_url, available_bundle_version
from .schema import supported_revision
from .snapshots import (domain_snapshot,
                        publication_snapshot, fingerprint, snapshot_hash)
from .services.common import CATALOGUES, scoped, expected
from .services.access import SWAP_AUTOMATION_ISSUER
from .services.schedule import in_force_snapshot
from .services.planning import health, edit_availability, allowed_actions
from .services import schedule
from .services.policies import policy_impact, check_policy, duplicate_groups, policy_reach, rules_for_person
from packages.domain.solver_rules import capability_catalogue, generation_manifest
from .services import reviews, swaps, planner_tools, availability, operations as service_operations
from .services import table_views, availability_patterns, independence, monitors
from .services import collection_filters
from .services import auth_configuration
from .services import home as home_service, read_projection_warming, workspace_detail
from .services.catalogue_version import department_version
from . import live as live_updates

ROOT = Path(__file__).resolve().parents[2]
AUDIT_EXPORT_MAX = 50_000  # rows per decision-history CSV
log = logging.getLogger("rota.api")
INTERNAL_ERROR_MESSAGE = "Something went wrong. Quote the reference if you contact support."


def _deferred_logout_cleanup(engine, browser, session_id, correlation_id):
    """Finish logout side effects after the session revocation is committed."""
    if browser:
        try:
            liveness_write(engine, lambda db: oidc.cancel_flow(db, browser, correlation_id),
                           context={"route": "auth.logout", "phase": "oidc_cleanup"})
        except Exception:
            log.exception("logout_oidc_cleanup_failed", extra={"correlation_id": correlation_id})
    try:
        liveness_write(engine, lambda db: revoke_session_installations(db, session_id),
                       context={"route": "auth.logout", "phase": "push_cleanup"})
    except Exception:
        log.exception("logout_push_cleanup_failed", extra={"correlation_id": correlation_id})


def parse_collection_filter(table_key):
    def parse(filter: str | None = Query(None, max_length=65536,
            description='JSON CollectionFilter: conditions and as_of, applied before count and paging')):
        return collection_filters.parse(filter, table_key=table_key)
    return parse


def parse_search_options(search_options: str | None = Query(None, max_length=4096)):
    try:
        return s.SearchOptions.model_validate_json(search_options) if search_options is not None else None
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error


class BodyLimitMiddleware:
    def __init__(self, app, maximum):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        maximum = self.maximum
        if scope.get("method") == "POST":
            path = scope.get("path", "").rstrip("/")
            if path == "/api/feedback":
                maximum = s.FEEDBACK_LIMITS["body_bytes"]
            elif path == "/api/score-imports" or (
                    path.startswith("/api/score-imports/") and path.endswith("/sources")):
                maximum = s.SCORE_CHECK_LIMITS["upload_body_bytes"]
            elif path == "/api/system/monitor-events":
                maximum = s.MONITOR_LIMITS["body_bytes"]
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = maximum + 1
        if declared < 0 or declared > maximum:
            return await JSONResponse({"error": {"code": "BODY_TOO_LARGE", "message": "Request body exceeds the supported size"}}, status_code=413)(scope, receive, send)
        chunks, length = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise ClientDisconnect()
            chunk = message.get("body", b"")
            length += len(chunk)
            if length > maximum:
                return await JSONResponse({"error": {"code": "BODY_TOO_LARGE", "message": "Request body exceeds the supported size"}}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        used = False

        async def replay():
            nonlocal used
            if not used:
                used = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()
        return await self.app(scope, replay, send)


def _chain_shutdown_signal(sig, frame, previous, loop):
    try:
        loop.call_soon_threadsafe(live_updates.begin_draining)
    except RuntimeError:  # The loop can close after the signal during final teardown.
        pass
    if callable(previous):
        previous(sig, frame)
    elif previous == signal.SIG_IGN:
        return
    else:
        # Uvicorn normally supplies a callable handler. Outside uvicorn, preserve
        # Python's default termination semantics instead of swallowing the signal.
        signal.signal(sig, signal.SIG_DFL)
        signal.raise_signal(sig)


def create_app(settings: Settings | None = None, engine=None):
    settings = settings or Settings.from_env()
    settings.check(require_proxy=True)
    engine = engine or make_engine(settings.database_url, settings=settings)
    schema_revision = supported_revision()
    cookie_name = "__Host-rota" if settings.environment == "production" else "rota_session"
    secure = settings.environment == "production"
    oidc_browser_cookie = "__Host-rota_oidc_browser" if secure else "rota_oidc_browser"
    oidc_reauth_cookie = "__Host-rota_oidc_reauthenticate" if secure else "rota_oidc_reauthenticate"
    oidc_logout_cookie = "__Host-rota_oidc_logout" if secure else "rota_oidc_logout"

    def schema_ready(db):
        db.execute(text("SELECT 1"))
        return list(db.scalars(text("SELECT version_num FROM alembic_version"))) == [schema_revision]

    def warm_worker():
        with transaction(engine) as db:
            if not schema_ready(db):
                log.error("api_process_warmup_failed", extra={"error_code": "SCHEMA_UNAVAILABLE"})
                return "failed"
        read_projection_warming.warm_process(engine)
        return "done"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio
        import threading
        from . import live
        live.configure(settings)
        live.attach(asyncio.get_running_loop())
        live.listen(settings.database_url)
        loop = asyncio.get_running_loop()
        previous_handlers = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous = signal.getsignal(sig)

                # uvicorn 0.54.0 installs capture_signals around _serve before lifespan startup; the real-uvicorn Linux test pins that ordering.
                def on_shutdown(received, frame, previous=previous):
                    _chain_shutdown_signal(received, frame, previous, loop)

                signal.signal(sig, on_shutdown)
                previous_handlers[sig] = previous
        try:
            # Availability beats warmth: a failed warm-up is logged loudly and the process serves cold.
            try:
                app.state.process_warmed = await background_io(warm_worker)
            except Exception as exc:
                app.state.process_warmed = "failed"
                log.error("api_process_warmup_failed", exc_info=exc, extra={"error_code": type(exc).__name__})
            yield
        finally:
            app.state.process_warmed = None
            for sig, previous in previous_handlers.items():
                signal.signal(sig, previous)
            live.detach(asyncio.get_running_loop())

    app = FastAPI(title="Rota", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.engine, app.state.settings = engine, settings
    app.state.process_warmed = None
    from .memory import MemoryReclamationMiddleware
    app.add_middleware(MemoryReclamationMiddleware, threshold_mb=settings.memory_reclaim_threshold_mb)
    app.add_middleware(BodyLimitMiddleware, maximum=settings.max_body_bytes)
    # Large rotas are megabytes of repetitive JSON; compressed they are a small fraction. CSRF tokens travel in cookies, not bodies.
    # Calendar validators describe the ICS bytes, so do not transform that representation.
    app.add_middleware(GZipMiddleware, minimum_size=1024, exclude_content_types=(*DEFAULT_EXCLUDED_CONTENT_TYPES, "text/calendar"))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlparse(settings.public_origin).hostname, "localhost", "127.0.0.1"] if not secure else [urlparse(settings.public_origin).hostname])

    def actor_for(request, db, write=False):
        actor = authenticate(db, request.cookies.get(cookie_name), request.headers.get("x-csrf-token"), write, settings=settings)
        db.info['workspace_shared_only'] = request.url.path == '/api/home' or request.url.path.startswith('/api/home/')
        request.state.identity_id, request.state.department_id = actor.identity_id, actor.department_id
        request.state.session_authenticated = True
        return actor

    def install_cookies(response, values, max_age=None):
        if max_age is None:
            max_age = session_cookie_max_age(values.get("cookie_expires_at", values["expires_at"]))
        options = {"secure": secure, "samesite": "lax", "path": "/", "max_age": max_age}
        response.set_cookie(cookie_name, values["token"], httponly=True, **options)
        response.set_cookie("rota_csrf", values["csrf"], httponly=False, **options)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        correlation = secrets.token_hex(12)
        request.state.correlation_id = correlation
        refresh_correlation = request.headers.get("x-refresh-correlation-id", "")[:128]
        if refresh_correlation and not re.fullmatch(r"[A-Za-z0-9._:-]+", refresh_correlation):
            refresh_correlation = ""
        request.state.refresh_correlation_id = refresh_correlation or None
        db_metrics = {"ms": 0.0}
        db_time_token = request_db_time_ms.set(db_metrics)
        started = time.monotonic()
        limiter = current_default_thread_limiter()
        # Route templates redact path tokens and arbitrary unmatched input; never log query strings.
        path = next((route.path for route in app.routes if route.matches(request.scope)[0] == Match.FULL), "unmatched")
        log.info("http_request_started", extra={"method": request.method, "path": path, "correlation_id": correlation,
                 **({"refresh_correlation_id": refresh_correlation} if refresh_correlation else {}),
                 "threadpool_borrowed": limiter.borrowed_tokens, "threadpool_total": limiter.total_tokens})
        client_aborted = False
        token = request.cookies.get(cookie_name)
        foreground = request.url.path.startswith("/api/") and request.url.path not in {"/api/events", "/api/pulse"}
        # Debounced recurrence previews must not write session activity or renew cookies.
        foreground = foreground and path not in {'/api/series/preview', '/api/series/{series_id}/preview'}
        monitor_webhook = request.method == "POST" and request.url.path.rstrip("/") == "/api/system/monitor-events"
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not monitor_webhook and request.headers.get("origin") != settings.public_origin:
            request.state.error_code = "ORIGIN_REJECTED"
            # D15/F-D1: the configured address goes with the refusal, so the sign-in page can send the person there.
            response = JSONResponse({"error": {"code": "ORIGIN_REJECTED", "message": "A same-origin request is required",
                                               "details": {"canonical_origin": settings.public_origin}}, "correlation_id": correlation}, status_code=403)
        else:
            try:
                # Idle timeout: API requests count as activity; background checks (live stream, pulse) do not.
                if token and foreground:
                    try:
                        await run_in_threadpool(keep_alive, engine, token, settings)
                    except Exception as exc:
                        log.warning('session_liveness_write_skipped', extra={'error_code': type(exc).__name__})
                response = await call_next(request)
            except ClientDisconnect:
                client_aborted = True
                request.state.error_code = "CLIENT_ABORTED"
                response = Response(status_code=499)
            except Exception:
                log.exception("unhandled_error", extra={"correlation_id": correlation, "method": request.method, "path": path})
                response = JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": INTERNAL_ERROR_MESSAGE},
                                         "correlation_id": correlation}, status_code=500)
        if request.method == "POST" and request.url.path == "/auth/login" and not getattr(request.state, "password_login_audited", False):
            # Rejected origins and malformed bodies have no trusted username; never parse their credentials for audit.
            try:
                await run_in_threadpool(liveness_write, engine, lambda db: login_event(
                    db, [login_digest("account:", settings),
                         login_digest("peer:" + (request.client.host if request.client else "unknown"), settings)],
                    getattr(request.state, "error_code", "LOGIN_REJECTED"), correlation,
                    window=settings.password_login_window_seconds),
                    context={"route": "auth.login", "phase": "fallback_audit"})
            except (SQLAlchemyError, DatabaseWriteTimeout):
                log.error("login_audit_unavailable", extra={"correlation_id": correlation})
        # Only sign-in/refresh writes auth cookies: an older API response must never overwrite a rotated token.
        # Static app files (public code and styles, no data) may be kept but are revalidated on every load, so a
        # reload costs 304s instead of the whole bundle and a new release is still picked up at once.
        static = request.url.path.startswith("/assets/") and response.status_code < 400
        route = request.scope.get("route")
        cache_control = response.headers.get("Cache-Control", "no-cache" if static else "no-store")
        # Server time (application and database) only on replies to a signed-in session, so a slow call can be split from
        # its network path. Never on identity routes or any unauthenticated reply: separate timings there would sharpen
        # account probing (OWASP information exposure).
        if (getattr(request.state, "session_authenticated", False) and request.url.path.startswith("/api/")
                and not request.url.path.startswith(("/api/account-links/", "/api/account/password"))):
            response.headers["Server-Timing"] = f"app;dur={(time.monotonic() - started) * 1000:.1f}, db;dur={db_metrics['ms']:.1f}"
        response.headers.update({"X-Correlation-ID": correlation, "Cache-Control": cache_control, "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Resource-Policy": "same-origin",
                                 "X-Permitted-Cross-Domain-Policies": "none",
                                 "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"})
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        status = response.status_code
        # Ids and codes only: no query strings, headers, cookies or bodies.
        response_body = getattr(response, "body", None)
        response_bytes = len(response_body) if isinstance(response_body, (bytes, bytearray)) else None
        fields = {"method": request.method, "route": route.path if route else "unmatched", "status": status,
                  "duration_ms": round((time.monotonic() - started) * 1000, 2), "correlation_id": correlation,
                  "response_bytes": response_bytes if response_bytes is not None else int(response.headers.get("content-length", 0) or 0),
                  "db_time_ms": round(db_metrics["ms"], 2)}
        if refresh_correlation:
            fields["refresh_correlation_id"] = refresh_correlation
        fields.update({k: v for k in ("error_code", "identity_id", "department_id") if (v := getattr(request.state, k, None))})
        level = logging.INFO if client_aborted else logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        log.log(level, "client_aborted" if client_aborted else "http_request", extra=fields)
        request_db_time_ms.reset(db_time_token)
        return response

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        request.state.error_code = exc.code
        if exc.status >= 500:
            log.error("problem", exc_info=exc, extra={"correlation_id": request.state.correlation_id, "error_code": exc.code})
        retry = (exc.details.get("retry_after")
                 if isinstance(exc.details, dict)
                 and (exc.status == 429 or exc.code in {"RULE_CATALOGUE_BUSY", "SIGN_IN_BUSY"}) else None)
        if exc.code == "RULE_CATALOGUE_BUSY" and retry is None:
            retry = getattr(getattr(request.app.state, "settings", None), "catalogue_build_wait_seconds", 1)
        return JSONResponse({"error": problem_payload(exc),
                             "correlation_id": request.state.correlation_id}, status_code=exc.status,
                            headers={"Retry-After": str(retry)} if retry else None)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_handler(request, exc):
        offered = next((e for e in exc.errors() if e['type'] == s.BALANCE_SHARE_OFFERED_METRIC), None)
        if offered:
            request.state.error_code = s.BALANCE_SHARE_OFFERED_METRIC
            return JSONResponse({'error': {'code': s.BALANCE_SHARE_OFFERED_METRIC, 'message': offered['msg']},
                                 'correlation_id': request.state.correlation_id}, status_code=400)
        request.state.error_code = "SCHEMA_INVALID"
        # Field path, message and type only; never the input value or context (they can carry private or secret text).
        errors = validation_issues(exc.errors())
        return JSONResponse({"error": {"code": "SCHEMA_INVALID", "message": "Review the highlighted fields", "details": errors}, "correlation_id": request.state.correlation_id}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        request.state.error_code = "VALUE_INVALID"
        return JSONResponse({"error": {"code": "VALUE_INVALID", "message": safe_message(str(exc))}, "correlation_id": request.state.correlation_id}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        refusal = integrity_problem(exc)
        request.state.error_code = refusal.code
        return JSONResponse({"error": {"code": refusal.code, "message": refusal.message},
                             "correlation_id": request.state.correlation_id}, status_code=refusal.status)

    @app.exception_handler(PoolTimeout)
    @app.exception_handler(OperationalError)
    async def database_error(request, exc):
        original = getattr(exc, "orig", None)
        code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        detail = str(original or exc).lower()
        locked = engine.dialect.name == "sqlite" and any(
            reason in detail for reason in ("database is locked", "interrupted"))
        if isinstance(exc, PoolTimeout) or code in {"55P03", "57014"} or locked:
            log.warning("database_wait_timeout", extra={"correlation_id": request.state.correlation_id,
                        "error_code": code or type(exc).__name__, "phase": "pool" if isinstance(exc, PoolTimeout) else "statement"})
            return await problem_handler(request, DatabaseWriteTimeout())
        connection_unavailable = (isinstance(code, str) and (code.startswith("08") or code in {"57P01", "57P02", "57P03"})) or any(
            marker in detail for marker in ("connection lost", "connection failed", "connection failure", "connection refused",
                                            "connection reset", "connection timed out", "could not connect", "cannot connect",
                                            "server closed the connection", "connection is closed",
                                            "connection aborted", "network is unreachable", "unable to open database file", "disk i/o error"))
        if connection_unavailable:
            request.state.error_code = "DATABASE_UNAVAILABLE"
            log.error("database_unavailable", exc_info=exc, extra={"correlation_id": request.state.correlation_id})
            return JSONResponse({"error": {"code": "DATABASE_UNAVAILABLE", "message": "The system is unavailable, so we could not confirm whether this change was saved. Try again."}, "correlation_id": request.state.correlation_id}, status_code=503)
        request.state.error_code = "INTERNAL_ERROR"
        log.error("database_error", exc_info=exc, extra={"correlation_id": request.state.correlation_id})
        return JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": INTERNAL_ERROR_MESSAGE}, "correlation_id": request.state.correlation_id}, status_code=500)

    @app.get("/health/live")
    async def live():
        return {"status": "alive", "version": __version__}

    def readiness(service=False):
        if app.state.process_warmed is None:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        try:
            with transaction(engine) as db:
                if not schema_ready(db):
                    return JSONResponse({"status": "unavailable"}, status_code=503)
                if service:
                    from .services.operations import worker_state
                    if worker_state(db, m.now())["worker_liveness"] != "running":
                        return JSONResponse({"status": "unavailable"}, status_code=503)
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready", "warm": "failed"} if app.state.process_warmed == "failed" else {"status": "ready"}

    @app.get("/health/ready")
    async def ready():
        return await background_io(readiness)

    @app.get("/health/service")
    async def service_ready():
        return await background_io(readiness, True)

    @app.get("/auth/config", response_model=s.AuthConfig)
    def auth_config():
        with transaction(engine) as db:
            config = auth_configuration.current(db, settings)
        password_enabled = password_login_enabled(settings)
        return {"environment": settings.environment, "deployment": settings.deployment, "password_login": password_enabled,
                "development_login": password_enabled, "mode": config.mode,
                "institutional_login": config.institutional_enabled and config.mode != "password_only"}

    @app.get('/api/branding', response_model=s.BrandingPublic)
    def public_branding():
        from .services import branding
        with transaction(engine) as db:
            return branding.public(db)

    @app.get('/api/branding/images/{image_id}')
    def branding_image(image_id: s.ID):
        from .services import branding
        with transaction(engine) as db:
            row = branding.published_image(db, image_id)
            return Response(row.content, media_type='image/png', headers={
                'ETag': '"' + row.sha256 + '"', 'X-Content-Type-Options': 'nosniff'})

    def current_pwa_response(respond):
        from .services import pwa
        with transaction(engine) as db:
            current = pwa.current_icons(db)
            if current is not None:
                return respond(*current)
        return retry_write(engine, lambda db: respond(*pwa.current_icons(db, render_missing=True)))

    @app.get('/assets/manifest.webmanifest')
    @app.get('/api/pwa/manifest.webmanifest')
    def pwa_manifest(request: Request):
        from .services import pwa
        def respond(value, artifacts):
            body = json.dumps(pwa.manifest(value, artifacts), separators=(',', ':'), ensure_ascii=False).encode()
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            headers = {'ETag': etag, 'Cache-Control': 'no-cache'}
            if request.headers.get('if-none-match') == etag:
                return Response(status_code=304, headers=headers)
            return Response(body, media_type='application/manifest+json', headers=headers)
        return current_pwa_response(respond)

    def pwa_icon_response(request, row, cache_control):
        from .services.conditional_reads import conditional_response
        headers = conditional_response(request, '"' + row.sha256 + '"', cache_control=cache_control)
        if isinstance(headers, Response):
            return headers
        return Response(row.content, media_type='image/png', headers=headers)

    @app.get('/api/pwa/icons/{artifact_id}.png')
    def pwa_icon(request: Request, artifact_id: s.ID):
        from .services import pwa
        with transaction(engine) as db:
            row = pwa.icon(db, artifact_id, include_content='if-none-match' not in request.headers)
            return pwa_icon_response(request, row,
                                     f'public, max-age={settings.static_cache_seconds}, immutable')

    @app.get('/api/pwa/apple-touch-icon.png')
    def pwa_apple_icon(request: Request):
        def respond(value, artifacts):
            row = next(a for a in artifacts if a.purpose == 'apple')
            return pwa_icon_response(request, row, 'no-cache')
        return current_pwa_response(respond)

    @app.get('/api/me/push', response_model=s.PwaStatus)
    def me_push(request: Request, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        from .services import pwa
        with transaction(engine) as db:
            return pwa.status(db, actor_for(request, db), settings, limit, offset)

    @app.get('/api/admin/notifications/policy')
    def admin_notification_policy(request: Request):
        from .services import pwa
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'admin')
            value, row = pwa.policy(db, actor.department_id)
            return {'policy': value, 'revision': row.revision if row else 1,
                    'email_configured': pwa.email_configured(db, actor.department_id)}

    @app.get('/api/me/notification-preferences')
    def notification_preferences(request: Request):
        from .services import pwa
        with transaction(engine) as db:
            return pwa.effective_events(db, actor_for(request, db))

    @app.get('/api/me/notification-preferences/history')
    @app.get('/api/admin/notifications/policy/history')
    @app.get('/api/operator/pwa/history')
    def pwa_versions(request: Request, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        from .services import pwa
        cls = (m.PwaSystemVersion if request.url.path.startswith('/api/operator/') else
               m.NotificationPolicyVersion if request.url.path.startswith('/api/admin/') else m.NotificationPreferenceVersion)
        with transaction(engine) as db:
            return pwa.versions(db, actor_for(request, db), cls, limit, offset)

    @app.get('/api/operator/pwa')
    def pwa_operator(request: Request):
        from .services import pwa
        with transaction(engine) as db:
            require_role(actor_for(request, db), 'operator')
            value, row = pwa.system(db)
            providers = db.execute(select(m.PushProviderState.origin, m.PushProviderState.key_id,
                m.PushProviderState.paused, m.PushProviderState.cooldown_until).order_by(m.PushProviderState.origin).limit(100))
            return {'settings': value.model_dump(mode='json'), 'revision': row.revision if row else 1,
                    'providers': [dict(item._mapping) for item in providers]}

    @app.get('/api/notifications/{notification_id}/destination')
    def notification_destination(request: Request, notification_id: s.ID):
        from .services import pwa
        with transaction(engine) as db:
            return pwa.destination(db, actor_for(request, db), notification_id)

    @app.get('/api/admin/branding', response_model=s.BrandingStatus)
    def admin_branding(request: Request):
        from .services import branding
        with transaction(engine) as db:
            return branding.status(db, actor_for(request, db))

    @app.get('/api/admin/branding/history')
    def branding_history(request: Request, limit: int = Query(100, ge=1, le=100),
                         offset: int = Query(0, ge=0, le=1_000_000)):
        from .services import branding
        with transaction(engine) as db:
            return branding.versions(db, actor_for(request, db), limit, offset)

    @app.post('/api/admin/branding/images', response_model=s.BrandingImage)
    async def upload_branding_image(request: Request):
        from .services import branding

        def authorize():
            enforce(engine, 'commands', [session_identity(engine, request.cookies.get(cookie_name))],
                    settings.rate_commands_per_minute, 60, 'Too many changes; wait a minute and try again')
            with transaction(engine) as db:
                branding.require_editor(actor_for(request, db, write=True))

        await run_in_threadpool(authorize)
        content = await request.body()
        prepared = await run_in_threadpool(branding.prepare_image, content,
                                          request.headers.get('content-type', '').split(';', 1)[0].lower())
        return await run_in_threadpool(lambda: retry_write(engine, lambda db: branding.store_image(
            db, actor_for(request, db, write=True), prepared, request.state.correlation_id)))

    @app.post("/auth/identify", response_model=s.IdentifierResponse)
    def identify(data: s.IdentifierInput, request: Request):
        values = identifier_method(engine, data.identifier, peer(request), settings)
        if isinstance(values, Problem):
            raise values
        return values

    @app.get("/api/admin/auth-config", response_model=s.AuthConfigStatus)
    def admin_auth_config(request: Request):
        with transaction(engine) as db:
            return auth_configuration.status(db, actor_for(request, db), settings)

    @app.put("/api/admin/auth-config", response_model=s.AuthConfigStatus)
    def save_auth_config(data: s.AuthConfigInput, request: Request):
        return retry_write(engine, lambda db: auth_configuration.change(
            db, actor_for(request, db, write=True), data, settings))

    @app.delete("/api/admin/auth-config", response_model=s.AuthConfigStatus)
    def reset_auth_config(data: s.AuthConfigChange, request: Request):
        return retry_write(engine, lambda db: auth_configuration.change(
            db, actor_for(request, db, write=True), data, settings, action="reset"))

    @app.post("/api/admin/auth-config/undo", response_model=s.AuthConfigStatus)
    def undo_auth_config(data: s.AuthConfigChange, request: Request):
        return retry_write(engine, lambda db: auth_configuration.change(
            db, actor_for(request, db, write=True), data, settings, action="undo"))

    @app.post("/api/admin/auth-config/test-connection", response_model=s.AuthConnectionResult)
    def test_auth_connection(data: s.AuthConnectionTest, request: Request):
        with transaction(engine) as db:
            tested = auth_configuration.prepare_test(db, actor_for(request, db, write=True), data, settings)
        result = auth_configuration.test_connection(tested)
        with transaction(engine) as db:
            auth_configuration.prepare_test(db, actor_for(request, db, write=True), data, settings)
        return result

    @app.post("/auth/refresh", response_model=s.LoginResponse)
    def refresh_auth(request: Request):
        values = liveness_write(engine, lambda db: rotate_session(db, actor_for(request, db, write=True), settings),
                                context={"route": "auth.refresh"})
        response = JSONResponse(s.LoginResponse(expires_at=values["expires_at"], display_name=values["display_name"]).model_dump(mode="json"))
        install_cookies(response, values)
        return response

    @app.post("/auth/login")
    def login(data: s.LoginInput, request: Request):
        values = password_login(engine, data, request.client.host if request.client else "unknown", settings,
                                user_agent=request.headers.get('user-agent'),
                                previous_token=request.cookies.get(cookie_name),
                                correlation_id=request.state.correlation_id)
        request.state.password_login_audited = True
        if isinstance(values, Problem):
            raise values
        response = JSONResponse(s.LoginResponse(expires_at=values["expires_at"], display_name=values["display_name"]).model_dump(mode="json"))
        install_cookies(response, values)
        return response

    @app.post("/auth/logout")
    def logout(request: Request, background_tasks: BackgroundTasks):
        def revoke(db):
            actor = actor_for(request, db, write=True)
            session = db.scalar(select(m.Session).where(m.Session.id == actor.session_id).with_for_update())
            revoke_session(session, cleanup=False)
            session_event(db, session, actor.person_id, "auth.sign_out",
                          correlation_id=request.state.correlation_id)
            configured = auth_configuration.effective(db, settings)
            identity = db.get(m.Identity, actor.identity_id)
            institutional = bool(session.auth_method == "oidc" or
                                 session.auth_method is None and configured.oidc_issuer
                                 and identity.issuer == configured.oidc_issuer)
            return configured, institutional, session.id
        configured, institutional, session_id = liveness_write(engine, revoke, context={"route": "auth.logout"})
        background_tasks.add_task(_deferred_logout_cleanup, engine, request.cookies.get(oidc_browser_cookie),
                                  session_id, request.state.correlation_id)
        state = secrets.token_urlsafe(32)
        end_session_url = oidc.logout_url(configured, "oidc", state) if institutional else None
        response = JSONResponse({"signed_out": True, **({"end_session_url": end_session_url} if institutional else {})})
        response.delete_cookie(cookie_name, path="/", secure=secure, httponly=True, samesite="lax")
        response.delete_cookie("rota_csrf", path="/", secure=secure, samesite="lax")
        response.delete_cookie(oidc_browser_cookie, path="/", secure=secure, httponly=True, samesite="lax")
        response.delete_cookie("rota_oidc_return", path="/auth", secure=secure, httponly=True, samesite="lax")
        if institutional:
            # A late sign-in response must never undo explicit sign-out: subsequent sign-ins keep requiring fresh auth.
            response.set_cookie(oidc_reauth_cookie, "yes", path="/", secure=secure,
                                httponly=True, samesite="lax", max_age=365 * 24 * 3600)
        if end_session_url:
            response.set_cookie(oidc_logout_cookie, digest(state), path="/", secure=secure,
                                httponly=True, samesite="lax", max_age=300)
        return response

    def peer(request):
        return request.client.host if request.client else "unknown"

    def peer_key(request):
        # Client addresses are low in entropy: rate-limit rows store only their keyed hash (#930).
        return login_digest("peer:" + peer(request), settings)

    # One-time invitation and password-reset links: per-link limits and a shared-address abuse backstop,
    # same-origin POST only (the guard above), audited. Tokens are never logged: the request log
    # records the route template, not the path.
    @app.get("/api/account-links/{token}")
    def account_link(token: str, request: Request):
        enforce(engine, "account-link-address", [peer_key(request)], 1000, 900, "Too many attempts; wait a few minutes and try again")
        enforce(engine, "account-link", [digest(token)], 60, 900, "Too many attempts; wait a few minutes and try again")
        from .services import accounts
        with transaction(engine) as db:
            return accounts.link_info(db, token)

    @app.post("/api/account-links/{token}")
    def use_account_link(token: str, data: s.PasswordSet, request: Request):
        enforce(engine, "account-link-address", [peer_key(request)], 1000, 900, "Too many attempts; wait a few minutes and try again")
        enforce(engine, "account-link-use", [digest(token)], 10, 900, "Too many attempts; wait a few minutes and try again")
        from .services import accounts
        return liveness_write(engine, lambda db: accounts.accept_link(db, token, data.password, settings, request.state.correlation_id),
                              context={"route": "auth.account_link"})

    @app.post("/api/account/password")
    def change_password(data: s.PasswordChange, request: Request):
        identity_id = session_identity(engine, request.cookies.get(cookie_name))
        enforce(engine, "password-change", [identity_id or peer_key(request)], 5, 900,
                "Too many password attempts; wait a few minutes and try again")
        from .services import accounts

        outcome = {}
        def operation(db):
            # A concurrent disable or reset can make this session unusable while the account lock is contended.
            account = accounts.lock_link_account(db, identity_id) if identity_id else None
            actor = actor_for(request, db, write=True)
            # The scanner treats this response as a password and follows the shared return value to worker logs (issue 1642).
            outcome.update(accounts.change_password(db, actor, data, settings, request.state.correlation_id, account=account))
        liveness_write(engine, operation, context={"route": "auth.password_change"})
        return outcome

    @app.get("/auth/start")
    def oidc_start(request: Request, next: str = "", identifier: str = Query(default="", max_length=255), remember_me: bool = False):
        try:
            with transaction(engine) as db:
                configured = auth_configuration.effective(db, settings)
                auth_configuration.require_institutional(configured)
            # Starting a flow is not proof that any identifier exists. Authorization still requires an explicit binding.
            # Do not send the entered identifier to the provider or disclose it in logs.
            config = oidc.document(configured)
            def start(db):
                latest = auth_configuration.effective(db, settings)
                auth_configuration.require_institutional(latest)
                require(latest.auth_configuration_revision == configured.auth_configuration_revision,
                        "AUTH_CONFIG_CHANGED", "Authentication settings changed; restart sign-in", 409)
                return oidc.start_flow(db, latest, config,
                                      force_login=request.cookies.get(oidc_reauth_cookie) == "yes",
                                      browser=request.cookies.get(oidc_browser_cookie), remember_me=remember_me)
            location, browser = liveness_write(engine, start, context={"route": "auth.oidc_start"})
        except Problem:
            raise
        except Exception:
            raise Problem(503, "IDENTITY_PROVIDER_UNAVAILABLE", "Unable to start institutional sign-in")
        response = RedirectResponse(location, status_code=303)
        # One host-bound browser binding across concurrent flows; states/nonces/verifiers remain one-use.
        response.set_cookie(oidc_browser_cookie, browser, httponly=True, secure=secure, samesite="lax", path="/", max_age=300)
        response.delete_cookie("rota_oidc_browser", path="/auth", secure=secure, httponly=True, samesite="lax")
        # The page the person was on (an in-app address, "#schedule/week"), so sign-in returns there like the built-in one.
        if oidc.return_route(next):
            response.set_cookie("rota_oidc_return", next, httponly=True, secure=secure, samesite="lax", path="/auth", max_age=300)
        return response

    @app.get("/auth/callback")
    def oidc_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        phase = "callback"
        try:
            require(0 < len(state) <= 500, "OIDC_STATE", "Invalid identity callback state", 401)
            phase = "configuration"
            def consume(db):
                configured = auth_configuration.effective(db, settings, secret=True)
                auth_configuration.require_institutional(configured)
                flow = oidc.consume_flow(db, state, request.cookies.get(oidc_browser_cookie))
                require(flow["config_revision"] == configured.auth_configuration_revision,
                        "AUTH_CONFIG_CHANGED", "Authentication settings changed; restart sign-in", 409)
                return flow, configured
            flow, configured = liveness_write(engine, consume, context={"route": "auth.oidc_callback", "phase": "consume"})
            phase = "provider_response"
            require(not error, "OIDC_PROVIDER_DENIED" if error == "access_denied" else "OIDC_PROVIDER_ERROR",
                    "Institutional sign-in was not completed", 401)
            require(0 < len(code) <= 4096, "OIDC_CALLBACK", "Invalid identity callback", 401)
            require(request.cookies.get(oidc_reauth_cookie) != "yes" or flow["reauthenticate_after"] is not None,
                    "OIDC_REAUTHENTICATION", "Fresh institutional authentication is required; restart sign-in", 401)
            phase = "exchange"
            claims, id_token = oidc.exchange(configured, flow, code)
            phase = "identity"
            def finish(db):
                latest = auth_configuration.effective(db, settings)
                auth_configuration.require_institutional(latest)
                require(flow["config_revision"] == latest.auth_configuration_revision,
                        "AUTH_CONFIG_CHANGED", "Authentication settings changed; restart sign-in", 409)
                return oidc.finish_flow(db, latest, claims, user_agent=request.headers.get('user-agent'), peer=peer(request),
                                        correlation_id=request.state.correlation_id, id_token=id_token, flow_id=flow["id"])
            values = liveness_write(engine, finish, context={"route": "auth.oidc_callback", "phase": "finish"})
        except Exception as exc:
            outcome = exc.code if isinstance(exc, Problem) else "OIDC_CALLBACK_FAILED"
            if outcome == "OIDC_PROVIDER_DENIED":
                public_error = "provider_denied"
            elif outcome in {"OIDC_STATE", "OIDC_REAUTHENTICATION"}:
                public_error = "expired"
            elif outcome in {"AUTH_CONFIG_CHANGED", "AUTH_KEY_UNAVAILABLE", "OIDC_UNCONFIGURED",
                             "OIDC_CONFIGURATION_CHANGED", "OIDC_ISSUER_MISMATCH", "OIDC_ENDPOINT_REJECTED",
                             "OIDC_DISCOVERY_INVALID", "OIDC_PKCE_REQUIRED", "OIDC_CODE_REQUIRED",
                             "OIDC_CLIENT_AUTH_METHOD"}:
                public_error = "config"
            else:
                public_error = "failed"
            request.state.error_code = outcome
            # Keep diagnostic frames and internal codes, never exception text/locals or provider query values.
            cause = exc.__cause__ or exc.__context__
            log.warning("oidc_callback_failed", extra={"correlation_id": request.state.correlation_id,
                        "error_code": outcome, "signin_error": public_error, "phase": phase,
                        "exception_type": type(exc).__name__, "traceback": traceback.format_tb(exc.__traceback__),
                        "cause_type": type(cause).__name__ if cause else None,
                        "cause_traceback": traceback.format_tb(cause.__traceback__) if cause else []})
            try:
                liveness_write(engine, lambda db: login_event(
                    db, [digest("oidc-callback"), login_digest("peer:" + peer(request), settings)], outcome,
                    request.state.correlation_id, window=settings.password_login_window_seconds),
                    context={"route": "auth.oidc_callback", "phase": "fallback_audit"})
            except (SQLAlchemyError, DatabaseWriteTimeout):
                log.error("login_audit_unavailable", extra={"correlation_id": request.state.correlation_id,
                          "error_code": outcome})
            response = RedirectResponse("/?signin_error=" + public_error, status_code=303)
            response.delete_cookie("rota_oidc_return", path="/auth", secure=secure, httponly=True, samesite="lax")
            return response
        if values.get("department_required"):
            return RedirectResponse("/?" + urlencode({"signin_choice": state}), status_code=303)
        response = RedirectResponse("/" + oidc.return_route(request.cookies.get("rota_oidc_return", "")), status_code=303)
        install_cookies(response, values)
        response.delete_cookie("rota_oidc_return", path="/auth", secure=secure, httponly=True, samesite="lax")
        return response

    @app.get("/auth/oidc/departments", response_model=s.OIDCDepartmentChoices)
    def oidc_departments(request: Request, continuation: str = Query(min_length=1, max_length=500)):
        with transaction(engine) as db:
            configured = auth_configuration.effective(db, settings)
            auth_configuration.require_institutional(configured)
            return oidc.department_choices(db, configured, continuation, request.cookies.get(oidc_browser_cookie))

    @app.post("/auth/oidc/department", response_model=s.OIDCDepartmentSession)
    def oidc_department(data: s.OIDCDepartmentChoice, request: Request):
        def redeem(db):
            configured = auth_configuration.effective(db, settings)
            auth_configuration.require_institutional(configured)
            return oidc.choose_department(db, configured, data.continuation, request.cookies.get(oidc_browser_cookie),
                data.department_id, reauthenticate=request.cookies.get(oidc_reauth_cookie) == "yes",
                user_agent=request.headers.get('user-agent'), peer=peer(request),
                correlation_id=request.state.correlation_id)
        values = liveness_write(engine, redeem, context={"route": "auth.oidc_department"})
        response = JSONResponse(s.OIDCDepartmentSession(expires_at=values['expires_at'],
            display_name=values['display_name'],
            return_to="/" + oidc.return_route(request.cookies.get("rota_oidc_return", ""))).model_dump(mode="json"))
        install_cookies(response, values)
        response.delete_cookie("rota_oidc_return", path="/auth", secure=secure, httponly=True, samesite="lax")
        return response

    @app.post("/auth/oidc/cancel")
    def oidc_cancel(data: s.OIDCContinuation, request: Request):
        liveness_write(engine, lambda db: oidc.cancel_flow(db, request.cookies.get(oidc_browser_cookie),
            request.state.correlation_id, continuation=data.continuation), context={"route": "auth.oidc_cancel"})
        response = JSONResponse({"cancelled": True})
        response.delete_cookie(oidc_browser_cookie, path="/", secure=secure, httponly=True, samesite="lax")
        response.delete_cookie("rota_oidc_return", path="/auth", secure=secure, httponly=True, samesite="lax")
        return response

    @app.get("/api/me")
    def me(request: Request):
        from .services import bulk_staged, training
        with transaction(engine) as db:
            actor = actor_for(request, db)
            org = db.get(m.Organisation, actor.organisation_id)
            department = db.get(m.Department, actor.department_id)
            return {"identity_id": actor.identity_id, "display_name": actor.display_name,
                    "department": record(department), "organisation": record(org),
                    "catalogue_version": department_version(db, actor.department_id),
                    "roles": sorted(actor.roles), "person_id": actor.person_id,
                    "permissions": sorted(name for name in s.PERMISSION_NAMES if actor.permits(*s.PERMISSIONS[name])),
                    "bulk_staged_count": db.scalar(select(func.count()).select_from(m.BulkStaged).where(
                        m.BulkStaged.department_id == actor.department_id, m.BulkStaged.owner_id == actor.identity_id,
                        m.BulkStaged.status == "active", m.BulkStaged.expires_at > m.now())),
                    "bulk_review_count": bulk_staged.review_count(db, actor),
                    "staff_swap_access": bool(actor.person_id and "staff" in actor.roles and swaps.staff_identity(db, actor.identity_id, actor.person_id, actor.department_id)),
                    "self_approval": independence.my_actions(db, actor),
                    # In a training programme here: their rotations and requests are on Programmes › My training.
                    "trainee": bool(actor.person_id and training.is_trainee(db, actor.department_id, actor.person_id)),
                    "account": {"username": account.username} if (account := db.scalar(select(m.Account).where(m.Account.identity_id == actor.identity_id))) else None,
                    # Every role (staff read no catalogues) colours duty types the same way: dom.setTypeColours.
                    "duty_types": [{"id": i, "created_at": c.isoformat(), "colour": (d or {}).get("colour") or ""} for i, c, d in db.execute(
                        select(m.AssignmentType.id, m.AssignmentType.created_at, m.AssignmentType.definition)
                        .where(m.AssignmentType.department_id == actor.department_id))],
                    "environment": settings.environment, "deployment": settings.deployment, "production_qualified": False,
                    "version": __version__, "release": build()["release"]}  # the web client compares its own build at start

    @app.get("/api/about")
    def about(request: Request):
        """What is running, for every signed-in user: release, build, engines, schema and the worker's last report.
        No hostnames, URLs or secrets. The API serves only at the supported head (/health/ready), so that is the DB's."""
        from packages.domain import fairness, score
        from packages.domain.generation import ALGORITHM_VERSION
        from packages.domain.solver_rules import COMPILER_VERSION
        from packages.domain.validation import ENGINE_VERSION
        with transaction(engine) as db:
            actor_for(request, db)
            from .services.operations import latest_worker_heartbeat
            worker = latest_worker_heartbeat(db, m.now())
        return {**build(), "deployment": settings.deployment, "migration": schema_revision,
                "engines": {"compiler": COMPILER_VERSION, "validator": ENGINE_VERSION, "greedy": ALGORITHM_VERSION,
                            "score": score.VERSION, "fairness": fairness.VERSION},
                "worker": {"release": worker.release, "last_seen_at": worker.last_seen_at.isoformat()} if worker else None}

    @app.get("/api/time/resolve")
    def resolve_time(request: Request, local: str):
        from .services.time_resolution import resolve
        with transaction(engine) as db:
            actor = actor_for(request, db)
            zone = db.get(m.Organisation, actor.organisation_id).timezone
            return {"instant": resolve(local, zone), "timezone": zone}

    @app.post("/api/time/resolve", response_model=s.TimeResolveBatchResult)
    def resolve_times(value: s.TimeResolveBatchInput, request: Request):
        from .services.time_resolution import resolve
        with transaction(engine) as db:
            actor = actor_for(request, db, write=True)
            zone = db.get(m.Organisation, actor.organisation_id).timezone
            return {"instants": [resolve(local, zone) for local in value.locals], "timezone": zone}

    def pulse_state(request, workspace_id=None, changes=None, job_ids=()):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            if workspace_id:
                require_role(actor, 'coordinator')
                scoped(db, m.Workspace, workspace_id, actor)
            if job_ids:
                watched = db.execute(select(m.Job.id, m.Job.kind).join(m.Workspace, m.Workspace.id == m.Job.workspace_id)
                    .where(m.Job.department_id == actor.department_id, m.Job.id.in_(job_ids))).all()
                require({row.id for row in watched} == set(job_ids), 'NOT_FOUND', 'Job not found', 404)
                for row in watched:
                    require_role(actor, 'coordinator', *(['approver'] if row.kind in {'feasibility', 'catalogue_proof'} else []))
            org = db.get(m.Organisation, actor.organisation_id)
            # Changes when a new frontend is deployed, so open tabs can offer a reload.
            build = available_bundle_version(WEB)
            # Rotas, swaps and requests changed by someone else: open pages refresh when this moves.
            activity = live_updates.activity(db, actor.department_id)
            if changes is not None and not actor.permits(*s.TEAM_READER_ROLES):
                # Workspace edit payloads are planner data.  Staff retain the
                # existing compact invalidation hint and its full-read fallback.
                changes = [{key: value for key, value in change.items()
                            if key not in {'delta', 'workspace', 'validation', 'health', 'fix_list_delta'}}
                           for change in changes]
            progress = {}
            unread = db.scalar(select(func.count()).select_from(m.Notification).where(
                m.Notification.identity_id == actor.identity_id, m.Notification.read_at.is_(None),
                m.Notification.outbox_id.in_(select(m.Outbox.id).where(m.Outbox.department_id == actor.department_id)))) or 0
            return {"input_revision": org.input_revision, "availability_revision": org.availability_revision, "activity": int(activity),
                    "roles": sorted(actor.roles), "unread": int(unread), "as_of": m.now().isoformat(), "build": build,
                    "department_id": actor.department_id, "changes": changes, **progress}

    @app.get("/api/pulse", response_model=s.LivePulse, response_model_exclude_unset=True)
    def pulse(request: Request, workspace_id: s.ID | None = None):
        return pulse_state(request, workspace_id)

    open_streams = {}  # identity id -> live event streams open in this process
    STREAMS_PER_IDENTITY = 12

    @app.get("/api/events")
    async def events(request: Request, once: bool = False, workspace_id: s.ID | None = None,
                     job_id: list[s.ID] = Query(default=[])):
        """Live updates: the pulse is pushed the moment something in the department changes (see apps/api/live.py),
        with a ping every 20 s so the page knows the stream is alive; the same 20 s also re-checks the session and picks
        up anything a NOTIFY could not announce. The stream ends after 30 minutes; the browser reconnects by itself."""
        import asyncio
        from starlette.concurrency import run_in_threadpool
        from .streaming import ClosingStreamingResponse as StreamingResponse
        job_ids = tuple(dict.fromkeys(job_id)) if isinstance(job_id, list) else ()
        initial_version = live_updates.sequence()
        # Authorise with bounded metadata off the event loop. The first byte must
        # not wait for the full pulse query; pulse_state repeats these checks
        # before publishing any state.
        def authorize_stream():
            with transaction(engine) as db:
                actor = actor_for(request, db)
                if workspace_id:
                    require_role(actor, 'coordinator')
                    scoped(db, m.Workspace, workspace_id, actor)
                if job_ids:
                    watched = db.execute(select(m.Job.id, m.Job.kind).join(m.Workspace, m.Workspace.id == m.Job.workspace_id)
                        .where(m.Job.department_id == actor.department_id, m.Job.id.in_(job_ids))).all()
                    require({row.id for row in watched} == set(job_ids), 'NOT_FOUND', 'Job not found', 404)
                    for row in watched:
                        require_role(actor, 'coordinator', *(['approver'] if row.kind in {'feasibility', 'catalogue_proof'} else []))
                return {'department_id': actor.department_id, 'roles': sorted(actor.roles)}
        actor_state = await run_in_threadpool(authorize_stream)
        NL = chr(10)
        identity = request.state.identity_id
        # shortcut: per-process cap; with several API processes one person can hold STREAMS_PER_IDENTITY in each.
        require(open_streams.get(identity, 0) < STREAMS_PER_IDENTITY, "TOO_MANY_STREAMS",
                "Too many open pages for this sign-in; close some tabs and reload", 429)
        once = once or live_updates.is_draining()

        topics = live_updates.subscription_topics(actor_state, workspace_id, job_ids)
        seen = tuple(min(value, initial_version) for value in live_updates.version(topics))

        async def stream():
            open_streams[identity] = open_streams.get(identity, 0) + 1
            try:
                async for chunk in updates():
                    yield chunk
            finally:
                open_streams[identity] -= 1
                if not open_streams[identity]:
                    del open_streams[identity]

        async def updates():
            nonlocal seen, topics
            # Flush a comment before the pulse transaction so proxies and the
            # browser receive a first byte even when a cold pulse read is slow.
            yield ": connected" + NL + NL
            state = await run_in_threadpool(pulse_state, request, workspace_id, None, job_ids)
            topics = live_updates.subscription_topics(state, workspace_id, job_ids)
            seen = tuple(min(value, initial_version) for value in live_updates.version(topics))
            last, sent, started = None, 0.0, time.monotonic()
            advanced = False
            while time.monotonic() - started < 1800 and not await request.is_disconnected():
                if live_updates.is_draining() and not once:
                    return
                key = {k: v for k, v in state.items() if k not in {"as_of", "changes"}}
                if key != last or advanced:
                    last, sent = key, time.monotonic()
                    yield "event: pulse" + NL + "data: " + json.dumps(state) + NL + NL
                    if once:  # probes and tests: the current state, then end
                        return
                elif time.monotonic() - sent > 20:
                    sent = time.monotonic()
                    yield "event: ping" + NL + "data: {}" + NL + NL
                await live_updates.wait(topics, seen, 20)
                if live_updates.is_draining() and not once:
                    return
                await asyncio.sleep(0.3)  # let a burst of changes land, then recompute once
                current_seen = live_updates.version(topics)
                advanced = current_seen != seen
                changes = live_updates.changes_since(topics, seen, current_seen) if advanced else None
                seen = current_seen  # Capture before reading so concurrent commits wake the next wait.
                try:
                    state = await run_in_threadpool(pulse_state, request, workspace_id, changes, job_ids)
                    eligible = live_updates.subscription_topics(state, workspace_id, job_ids)
                    if eligible != topics:
                        previous = dict(zip(topics, seen))
                        topics, seen = eligible, tuple(previous.get(topic, -1) for topic in eligible)
                        state['changes'] = None
                except Problem as exc:
                    if exc.status == 401:
                        yield "event: expired" + NL + "data: {}" + NL + NL
                    else:
                        yield "event: error" + NL + "data: {\"retry\": true}" + NL + NL
                    return
                except Exception:
                    # A transient database or server failure must reconnect, never sign the person out.
                    yield "event: error" + NL + "data: {\"retry\": true}" + NL + NL
                    return
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/calendar")
    def calendar_view(request: Request, start: s.date, end: s.date, workspace_id: str | None = None,
                      programme_id: str | None = None, category_id: str | None = None, person_id: str | None = None,
                      service_id: str | None = None, location_id: str | None = None, mode: str | None = None):
        from .services.calendar import calendar
        from .services.conditional_reads import conditional_json
        with transaction(engine) as db:
            actor = actor_for(request, db)
            value = calendar(db, actor, start, end, workspace_id, programme_id, category_id, person_id,
                             service_id, location_id, mode)
            return conditional_json(request, value, actor)

    @app.get("/api/schedule/overlaps")
    def schedule_overlaps(request: Request, starts_on: s.date | None = None, ends_on: s.date | None = None,
                          workspace_id: str | None = None, base_publication_id: str | None = None,
                          replaces_publication_id: str | None = None):
        """What a rota overlaps (published rotas in force, competing drafts) and the ways to place it: before creating
        one (its dates) or before publishing one (its id). The same answer publishing refuses with."""
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator", "approver", "programme_lead", "auditor")
            if workspace_id:
                target = scoped(db, m.Workspace, workspace_id, actor)
            else:
                require(starts_on is not None and ends_on is not None and starts_on < ends_on, "VALUE_INVALID",
                        "Give the rota's first day and the day after its last")
                require((ends_on - starts_on).days <= 93, "VALUE_INVALID", "A rota spans at most 93 days")
                for pid in (base_publication_id, replaces_publication_id):
                    if pid:
                        scoped(db, m.Publication, pid, actor)
                target = schedule.candidate(starts_on, ends_on, base_publication_id, replaces_publication_id)
            from packages.domain.rota_summary import suggested_rota_name
            return {**schedule.overlaps(db, actor.department_id, target),
                    'suggested_name': suggested_rota_name(target.starts_on, target.ends_on)}

    @app.get("/api/schedule/days")
    def schedule_days(request: Request, response: Response, start: s.date, end: s.date,
                      collection_filter: s.CollectionFilter = Depends(parse_collection_filter("schedule"))):
        """One summary per source per day (sessions, open places, errors) for the Year and Month views; staff: own duties."""
        from .services import schedule_view
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "staff", *s.PERMISSIONS['schedule.read_team'])
            require(start < end and (end - start).days <= 400, "VALUE_INVALID", "Ask for 1 to 400 days (end is the day after the last)")
            tag, body = schedule_view.days(db, actor, start, end, request.headers.get("if-none-match"),
                                           collection_filter=collection_filter)
        if body is None:
            return Response(status_code=304, headers={"ETag": tag})
        response.headers["ETag"] = tag
        return body

    @app.get("/api/schedule/sessions")
    def schedule_sessions(request: Request, response: Response, start: s.date, end: s.date, exclude: str | None = None,
                          collection_filter: s.CollectionFilter = Depends(parse_collection_filter("schedule"))):
        """Published sessions in force in a window (the layer under a draft, or the published schedule); staff: own duties.
        `exclude`: publication or rota ids to leave out (the rota a draft amends)."""
        from .services import schedule_view
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "staff", *s.PERMISSIONS['schedule.read_team'])
            require(start < end and (end - start).days <= 92, "VALUE_INVALID", "Ask for 1 to 92 days (end is the day after the last)")
            tag, body = schedule_view.sessions(db, actor, start, end, {i for i in (exclude or "").split(",") if i},
                                               request.headers.get("if-none-match"), collection_filter=collection_filter)
        if body is None:
            return Response(status_code=304, headers={"ETag": tag})
        response.headers["ETag"] = tag
        return body

    @app.get("/api/content")
    def department_content(request: Request, page: int = Query(default=1, ge=1, le=100000),
                           page_size: int = Query(default=4, ge=1, le=s.CONTENT_PAGE_SIZE)):
        from zoneinfo import ZoneInfo
        from .services import announcements, audiences
        with transaction(engine) as db:
            actor = actor_for(request, db)
            org = db.get(m.Organisation, actor.organisation_id)
            today = m.now().astimezone(ZoneInfo(org.timezone)).date()
            query = select(m.DepartmentContent).where(m.DepartmentContent.department_id == actor.department_id,
                m.DepartmentContent.active.is_(True), m.DepartmentContent.starts_on <= today,
                (m.DepartmentContent.ends_on.is_(None) | (m.DepartmentContent.ends_on > today)))
            query = audiences.visible_content(db, actor, today, query)
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            query = query.outerjoin(m.AnnouncementReceipt,
                (m.AnnouncementReceipt.content_id == m.DepartmentContent.id) &
                (m.AnnouncementReceipt.identity_id == actor.identity_id))
            priority = case(((m.DepartmentContent.kind == 'announcement') &
                             (m.DepartmentContent.importance == 'must_acknowledge') &
                             m.AnnouncementReceipt.acknowledged_at.is_(None), 0),
                            (m.DepartmentContent.importance.in_(['important', 'must_acknowledge']), 1), else_=2)
            items = list(db.scalars(query.order_by(priority, m.DepartmentContent.name, m.DepartmentContent.id)
                                    .offset((page - 1) * page_size).limit(page_size)))
            receipts = {r.content_id: record(r) for r in db.scalars(select(m.AnnouncementReceipt).where(
                m.AnnouncementReceipt.content_id.in_([r.id for r in items]),
                m.AnnouncementReceipt.identity_id == actor.identity_id))}
            counts = announcements.counts(db, actor, items) if actor.permits('admin', 'coordinator') else {}
            return {"items": [{**record(r), "receipt": receipts.get(r.id),
                               "acknowledgement_required": r.kind == 'announcement' and r.importance == 'must_acknowledge',
                               **({"receipts": counts[r.id]} if r.id in counts else {})} for r in items],
                    "page": page, "page_size": page_size, "total": total}

    @app.get('/api/email/settings')
    def email_configuration(request: Request):
        from .services import email_settings
        with transaction(engine) as db:
            return email_settings.status(db, actor_for(request, db), settings)

    @app.get('/api/email/microsoft')
    def microsoft_mailbox(request: Request):
        from .services import mailboxes
        with transaction(engine, write=True) as db:
            actor = actor_for(request, db)
            require_role(actor, 'admin', 'coordinator')
            mailboxes.prune(db)
            return mailboxes.status(db, actor, settings)

    @app.get('/api/email/microsoft/callback')
    def microsoft_mailbox_callback(request: Request, state: str = Query(max_length=256),
                                   code: str = Query(default='', max_length=16384),
                                   error: str | None = Query(default=None, max_length=500)):
        from .services import mailboxes
        # Consume state even on a rejected grant; provider bodies never enter the response or logs.
        with transaction(engine, write=True) as db:
            actor = actor_for(request, db)
            try:
                result = mailboxes.callback(db, actor, settings, state, '' if error else code)
            except Problem as exc:
                result = JSONResponse(status_code=exc.status, content={'error': {'code': exc.code,
                    'message': exc.message}, 'draft_available': True}, headers={'Cache-Control': 'no-store'})
        return result if isinstance(result, Response) else JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.get('/api/content/{content_id}/email-preview')
    def content_email_preview(content_id: s.ID, request: Request, page: int = Query(default=1, ge=1, le=100000),
                              page_size: int = Query(default=50, ge=1, le=s.CONTENT_PAGE_SIZE)):
        from .services import email_messages
        with transaction(engine) as db:
            return email_messages.preview(db, actor_for(request, db), 'content', content_id, settings, page, page_size)

    @app.get('/api/content/{content_id}/draft.eml')
    def content_email_draft(content_id: s.ID, request: Request):
        from .services import email_messages
        with transaction(engine) as db:
            message = email_messages.draft(db, actor_for(request, db), 'content', content_id, settings)
            return Response(message.as_bytes(), media_type='message/rfc822', headers={
                'Content-Disposition': 'attachment; filename="announcement.eml"', 'Cache-Control': 'no-store'})

    @app.get('/api/events/{event_id}/email-preview')
    def event_email_preview(event_id: s.ID, request: Request, page: int = Query(default=1, ge=1, le=100000),
                            page_size: int = Query(default=50, ge=1, le=s.CONTENT_PAGE_SIZE)):
        from .services import email_messages
        with transaction(engine) as db:
            return email_messages.preview(db, actor_for(request, db), 'event', event_id, settings, page, page_size)

    @app.get('/api/events/{event_id}/draft.eml')
    def event_email_draft(event_id: s.ID, request: Request,
                          group: str | None = Query(None, pattern='^(current|former)$')):
        from .services import email_messages
        with transaction(engine) as db:
            message = email_messages.draft(db, actor_for(request, db), 'event', event_id, settings, group=group)
            return Response(message.as_bytes(), media_type='message/rfc822', headers={
                'Content-Disposition': 'attachment; filename="event.eml"', 'Cache-Control': 'no-store'})

    @app.get("/api/content/{content_id}/receipts", response_model=s.AnnouncementReceiptList)
    def content_receipts(content_id: str, request: Request, page: int = Query(default=1, ge=1, le=100000),
                         page_size: int = Query(default=50, ge=1, le=200)):
        from .services import announcements, audiences
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'admin', 'coordinator')
            content = scoped(db, m.DepartmentContent, content_id, actor)
            day = independence.today(db, actor.department_id)
            members = audiences.recipients(db, actor.department_id, content.audience, day)
            R = m.AnnouncementReceipt
            total = db.scalar(select(func.count()).select_from(R).where(R.content_id == content.id))
            rows = db.execute(select(R, m.Person.display_name, m.Identity.display_name)
                .join(m.Identity, m.Identity.id == R.identity_id).outerjoin(m.Person, m.Person.id == R.person_id)
                .where(R.content_id == content.id).order_by(R.created_at, R.seq).offset((page - 1) * page_size).limit(page_size)).all()
            from .services.email_messages import receipt_deliveries
            email_deliveries = receipt_deliveries(db, [r.id for r, _, _ in rows])
            return {"receipts": [{**record(r, exclude=('event_occurrence_id', 'response', 'responded_at')),
                                  "person": {"id": r.person_id, "display_name": name} if r.person_id else None,
                                  "identity": {"id": r.identity_id, "display_name": identity_name},
                                  "in_audience": r.identity_id in members,
                                  "email_deliveries": email_deliveries.get(r.id, [])} for r, name, identity_name in rows],
                    "counts": announcements.counts(db, actor, [content])[content.id],
                    "page": page, "page_size": page_size, "total": total}

    @app.get('/api/event-types')
    def event_types(request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            rows = db.scalars(select(m.EventType).where(m.EventType.department_id == actor.department_id)
                              .order_by(m.EventType.name, m.EventType.id))
            visible = {'id', 'name', 'colour', 'starts_on', 'ends_on', 'active', 'attendance', 'calendar', 'rsvp'}
            return {'event_types': [record(r) if actor.permits('admin', 'coordinator') else
                                    {k: v for k, v in record(r).items() if k in visible} for r in rows]}

    @app.get('/api/events/occurrences', response_model=s.EventOccurrenceList, response_model_exclude_unset=True)
    def events_list(request: Request, starts_on: s.date, ends_on: s.date):
        from .services import events
        with transaction(engine) as db:
            return events.occurrences(db, actor_for(request, db), starts_on, ends_on)

    @app.get('/api/events/definitions/{event_id}')
    def event_definition(event_id: s.ID, request: Request):
        from .services import events
        with transaction(engine) as db:
            actor = actor_for(request, db)
            events.require_definition_access(actor)
            return {'record': events.event_value(db, scoped(db, m.Event, event_id, actor))}

    @app.get('/api/events/{occurrence_id}/receipts', response_model=s.EventReceiptList)
    def event_receipts(occurrence_id: s.ID, request: Request, page: int = Query(default=1, ge=1, le=100000),
                       page_size: int = Query(default=50, ge=1, le=200)):
        from .services import events
        with transaction(engine) as db:
            return events.receipt_list(db, actor_for(request, db), occurrence_id, page, page_size)

    @app.get("/api/home")
    def home_summary(request: Request):
        """Home: role-scoped counts and short lists of what needs this person (services/home.py)."""
        with transaction(engine) as db:
            actor = actor_for(request, db)
            db.info['_request_parents'] = {}
            return home_service.home(db, actor, settings=settings)

    @app.get('/api/home/rotas', response_model=s.HomeRotaList)
    def home_rotas(request: Request, start: s.date, end: s.date,
                   limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        with transaction(engine) as db:
            return home_service.rota_strip(db, actor_for(request, db), start, end, limit, offset)

    @app.get("/api/integrations/github", response_model=s.GithubIntegrationStatus)
    def github_integration_status(request: Request):
        from .services import github_integrations
        with transaction(engine) as db:
            return github_integrations.status(db, actor_for(request, db), settings)

    @app.post("/api/feedback", response_model=s.FeedbackCreated)
    def feedback_create(value: s.FeedbackCreate, request: Request):
        enforce(engine, "commands", [session_identity(engine, request.cookies.get(cookie_name))],
                settings.rate_commands_per_minute, 60, "Too many changes in a short time; wait a minute and try again")
        command = s.Command(kind="feedback.create", payload=value.model_dump(mode="json"))
        result = execute(engine, settings, request.cookies.get(cookie_name), request.headers.get("x-csrf-token"),
                       request.headers.get("idempotency-key"), command, request.state.correlation_id)
        request.state.session_authenticated = True
        return {"id": result["id"], "matched_report_id": result["matched_report_id"]}

    @app.get("/api/feedback", response_model=s.FeedbackList)
    def feedback_list(request: Request, mine: bool = False, status: s.FeedbackStatus | None = None,
                      kind: s.FeedbackKind | None = None, duplicate_of: s.ID | None = None,
                      q: str = Query(default="", max_length=300),
                      search_options: s.SearchOptions | None = Depends(parse_search_options),
                      page: int = Query(default=1, ge=1, le=100000), page_size: int = Query(default=s.FEEDBACK_LIMITS["page_size"], ge=1, le=100)):
        from .services import feedback
        with transaction(engine) as db:
            return feedback.list_reports(db, actor_for(request, db), mine=mine, status=status, kind=kind,
                                         duplicate_of=duplicate_of, q=q, page=page, page_size=page_size, search_options=search_options)

    @app.get("/api/feedback/{report_id}", response_model=s.FeedbackRow)
    def feedback_detail(report_id: str, request: Request):
        from .services import feedback
        with transaction(engine) as db:
            return feedback.detail(db, actor_for(request, db), report_id)

    @app.get("/api/feedback/{report_id}/screenshot")
    def feedback_screenshot(report_id: str, request: Request):
        from .services import feedback
        def read(db):
            actor = actor_for(request, db)
            db.info["correlation_id"] = request.state.correlation_id
            return feedback.screenshot(db, actor, report_id)
        data, media_type = retry_write(engine, read)
        return Response(data, media_type=media_type, headers={"Cache-Control": "private, no-store",
                        "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})

    # Each list the app keeps in memory: kind -> (its table, the tables whose changes it shows). People carry their aliases.
    DEPARTMENT_LISTS = {"memberships": m.Membership, "eligibilities": m.Eligibility, "grants": m.CapabilityGrant,
                        "policies": m.Policy, "exceptions": m.PolicyException, "closures": m.LocationClosure,
                        "tag_members": m.TagMember}
    LISTS = {**{kind: (cls, (cls,)) for kind, cls in CATALOGUES.items()}, "people": (m.Person, (m.Person, m.SourceMapping,
             m.Category, m.Membership, m.Programme, m.ProgrammeMembership, m.RotationType, m.Rotation, m.Cohort)),
             "rotations": (m.Rotation, (m.Rotation, m.Mentorship)),
             "tags": (m.Tag, (m.Tag, m.Policy)),  # each tag carries how many rule lines use it
             **{kind: (cls, (cls,)) for kind, cls in DEPARTMENT_LISTS.items()}}
    list_cache = OrderedDict()  # (department, roles, reader scope, kind) -> (built at, stamp, JSON fragment, version)
    list_cache_lock = Lock()
    list_building = {}  # key -> Event while one request builds it; concurrent readers wait instead of rebuilding

    def reader_list(db, actor, kind, fragment):
        """Overlay private workspace policies only after reading the shared fragment."""
        if kind != 'policies' or not actor.permits('coordinator'):
            return fragment
        from .services.workspace_access import shared_clause
        from .services.policies import policy_versions
        private = select(m.Workspace.id).where(m.Workspace.department_id == actor.department_id,
                                               ~shared_clause())
        rows = list(db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id,
            m.Policy.status != 'retired', m.Policy.definition['workspace_id'].as_string().in_(private))
            .order_by(m.Policy.id).limit(4001)))
        if not rows:
            return fragment
        shared = json.loads(fragment)
        require(len(shared) + len(rows) <= 4000, 'CATALOGUE_LIMIT',
                'This catalogue is too large to display completely; contact your admin', 409)
        return json.dumps(sorted(shared + policy_versions(db, actor, [record(row) for row in rows]),
                                 key=lambda row: row['id']), default=str, separators=(',', ':'))

    def build_list(db, actor, kind, manages, today):
        from .auth import can_read_catalogue, can_read_rules
        if not can_read_catalogue(actor, kind):
            return []
        cls = LISTS[kind][0]
        if kind in {"department_content", "events"} and not manages:
            return []
        if kind in DEPARTMENT_LISTS:
            rows = list(db.scalars(select(cls).where(cls.department_id == actor.department_id,
                *([cls.status != 'retired']
                  if cls is m.Policy else []),
                *([cls.kind != 'policies']
                  if cls is m.TagMember and not can_read_rules(actor) else [])).order_by(cls.id).limit(4001)))
            require(len(rows) <= 4000, "CATALOGUE_LIMIT", "This catalogue is too large to display completely; contact your admin", 409)
            # Retired versions load through the pageable history endpoint, outside the operational ceiling.
            if cls is m.Policy:
                from .services.policies import policy_versions
                return policy_versions(db, actor, [record(row) for row in rows])
            return [record(row) for row in rows]
        query = select(cls.id, cls.heading, cls.cancelled) if kind == 'events' else select(cls)
        query = query.where(cls.organisation_id == actor.organisation_id if cls is m.Person else cls.department_id == actor.department_id)
        from .services.training import catalogue_scope
        query = query.where(catalogue_scope(db, actor, kind, cls))
        limit = s.MAX_CATALOGUE_ROTATIONS if kind == "rotations" else 2000
        if kind == "rotations":
            query = query.where(cls.ends_on >= today - relativedelta(months=12))
        # Stable order (by name, then id): an edited record keeps its place instead of moving to the end.
        label = getattr(cls, "display_name", None) or getattr(cls, "name", None) or getattr(cls, "heading", None)
        order = [cls.sort_order] if kind in {'leave_types', 'categories'} else []
        query = query.order_by(*order, *([label] if label is not None else []), cls.id).limit(limit + 1)
        rows = list(db.execute(query)) if kind == 'events' else list(db.scalars(query))
        require(len(rows) <= limit, "CATALOGUE_LIMIT", "This catalogue exceeds the current bounded UI; pagination upgrade required", 409)
        if kind == 'events':
            return [{'id': row.id, 'name': row.heading, 'active': not row.cancelled} for row in rows]
        out = [record(r) for r in rows]
        if kind == "people":
            from .services.people import current_people, imported_aliases
            from packages.domain.people import person_aliases
            current = current_people(db, actor, today, people={row['id']: row for row in out})['people']
            aliases = imported_aliases(db, actor, current)
            employee_number_redacted = not actor.permits('admin', 'coordinator')
            for person in out:
                person['aliases'] = person_aliases(person, aliases.get(person['id'], ()))
                if not employee_number_redacted:
                    person['configured_active'] = person['active']
                person.update(current.get(person['id'], {}))
                person['employee_number_redacted'] = employee_number_redacted
            from .services.people import visible_people
            out = visible_people(db, actor, out)
        if kind == "tags":
            from .services.tags import usage
            used = (usage(db, actor.department_id, rows) if can_read_rules(actor)
                    else {tag.id: {'rules': 0, 'lines': 0} for tag in rows})
            for tag in out:
                tag['used_by'] = used[tag['id']]
        return out

    @app.get("/api/catalogues")
    def catalogues(request: Request, response: Response, known: str | None = Query(None, max_length=2000), boot: bool = False,
                   include: list[str] = Query([], max_length=40), active_only: bool = False, counts_only: bool = False):
        """Every list the app keeps in memory, with `_versions` (a short hash per list). With `known` (the versions the
        app holds, comma-separated), only the lists that changed are sent: a save reloads what it changed."""
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor)
            from .auth import can_read_catalogue, can_read_rules
            reads_rules = can_read_rules(actor)
            catalogue_access = tuple((kind, can_read_catalogue(actor, kind)) for kind in LISTS)

            def list_version(kind, fragment):
                return hashlib.sha256(f'{kind}:{tuple(sorted(actor.roles))}:{can_read_catalogue(actor, kind)}:{reads_rules}:{fragment}'.encode()).hexdigest()[:12]

            # Boot keeps people and picker references. Full records load through this same endpoint when needed.
            deferred = {'memberships', 'eligibilities', 'grants', 'programme_memberships', 'rotations', 'mentorships',
                        'programme_calendar_entries', 'policies', 'exceptions', 'tag_members', 'recurring_series',
                        'operating_calendars', 'resource_pools', 'department_content', 'events', 'closures', 'rotation_templates', 'date_sets'}
            lists = {kind: value for kind, value in LISTS.items() if not boot or kind not in deferred}
            require(not (set(include) - LISTS.keys()), 'UNKNOWN_CATALOGUE', 'Choose a known catalogue')
            if include:
                lists = {kind: value for kind, value in lists.items() if kind in include}
            requested_lists = set(lists)
            if counts_only:
                # The in-force projection depends on these fragments even when a setup
                # badge asked for one different kind; they remain response-internal.
                lists.update({kind: LISTS[kind] for kind in {'policies', 'tags', 'tag_members'} - lists.keys()})
            # Shared writes advance durable catalogue/input revisions; cache checks never scan catalogue history.
            catalogue_revision, input_revision, organisation_revision, availability_revision, timezone = db.execute(select(
                m.Department.catalogue_revision, m.Organisation.input_revision, m.Organisation.revision,
                m.Organisation.availability_revision, m.Organisation.timezone).join(m.Organisation, m.Organisation.id == m.Department.organisation_id)
                .where(m.Department.id == actor.department_id)).one()
            stamp = (catalogue_revision, input_revision, organisation_revision)
            from .services.catalogue import exception_identity_stamp
            need_exception_identities = reads_rules and 'exceptions' in lists and actor.permits('approver', 'coordinator')
            if need_exception_identities:
                from .services.catalogue import exception_identity_rows
                exception_identities = exception_identity_rows(db, actor)
            else:
                exception_identities = []
            # Notices can be for particular people: only those who manage them get them here (others read /api/content).
            manages = actor.permits("admin", "coordinator")
            fragments, versions = {}, {}
            from zoneinfo import ZoneInfo
            today = m.now().astimezone(ZoneInfo(timezone)).date()
            from .services.training import led_programmes
            restricted = led_programmes(db, actor) is not None
            receipt_stamp = None
            if manages and 'department_content' in lists:
                receipt_stamp = tuple(db.execute(select(func.max(m.AnnouncementReceipt.revision),
                    func.max(m.AnnouncementReceipt.created_at)).where(
                    m.AnnouncementReceipt.content_id.in_(select(m.DepartmentContent.id).where(
                        m.DepartmentContent.department_id == actor.department_id)))).one())
            from .services.conditional_reads import conditional_response, revision_etag
            cheap_tag = revision_etag("catalogues", actor.department_id, actor.identity_id, actor.person_id,
                                      sorted(actor.roles), settings.publish_test_people_only, actor.display_name,
                                      catalogue_access, reads_rules, boot, sorted(include), active_only, counts_only,
                                      (catalogue_revision, input_revision, organisation_revision, availability_revision,
                                       timezone, today, restricted, receipt_stamp,
                                       exception_identity_stamp(exception_identities)))
            if not counts_only:
                cached = conditional_response(request, cheap_tag)
                if isinstance(cached, Response):
                    return cached
                response.headers.update(cached)
            for kind, (_, sources) in lists.items():
                key = (actor.department_id, tuple(sorted(actor.roles)), can_read_catalogue(actor, kind), reads_rules,
                       (actor.identity_id, actor.person_id) if restricted or kind == 'people' else None, kind)
                current_stamp = (*stamp, today) if restricted or kind in {'people', 'rotations'} else stamp
                def cached():
                    with list_cache_lock:
                        hit = list_cache.get(key)
                        if hit is not None:
                            list_cache.move_to_end(key)
                        fresh = hit and hit[1] == current_stamp and time.monotonic() - hit[0] < 120
                        building = None if fresh else list_building.get(key)
                        owner = not fresh and building is None
                        if owner:
                            list_building[key] = Event()
                    return (hit if fresh else None), building, owner
                hit, building, owner = cached()
                if building is not None:
                    # After a write, every open page asks at once.  Do not pin a
                    # request worker to a cold build: a prior fragment is safe for
                    # this display-only picker (commands re-check current rows),
                    # while a cold miss gets a retryable response.
                    if building.wait(settings.catalogue_build_wait_seconds):
                        hit, _, owner = cached()
                    else:
                        with list_cache_lock:
                            hit = list_cache.get(key)
                            if hit is not None:
                                list_cache.move_to_end(key)
                        if hit is None:
                            raise Problem(503, "CATALOGUE_BUSY",
                                          "The catalogue is still updating; try again")
                        response.headers["X-FMUC-Stale"] = "1"
                if hit is None:
                    try:
                        from .services.workspace_access import shared_scope
                        with shared_scope(db, actor):
                            fragment = json.dumps(build_list(db, actor, kind, manages, today), default=str, separators=(",", ":"))
                        hit = (time.monotonic(), current_stamp, fragment, list_version(kind, fragment))
                        with list_cache_lock:
                            list_cache[key] = hit
                            list_cache.move_to_end(key)
                            while len(list_cache) > settings.catalogue_cache_entries:
                                list_cache.popitem(last=False)
                    finally:
                        if owner:
                            with list_cache_lock:
                                list_building.pop(key).set()
                fragment = reader_list(db, actor, kind, hit[2])
                fragments[kind] = fragment
                versions[kind] = (hit[3] if fragment == hit[2] else
                                  hashlib.sha256(f'{kind}:{fragment}'.encode()).hexdigest()[:12])
            # Approval eligibility belongs to this request's actor, never to a department-shared cached fragment.
            inactive_counts = {}
            if active_only:
                for kind in {'services', 'locations', 'assignment_types'} & fragments.keys():
                    rows = json.loads(fragments[kind])
                    active = [row for row in rows if row.get('active', True)]
                    inactive_counts[kind] = len(rows) - len(active)
                    fragments[kind] = json.dumps(active, default=str, separators=(',', ':'))
                    versions[kind] = list_version(kind, fragments[kind])
            if fragments.get('exceptions', '[]') != '[]':
                from .services.catalogue import exception_actions
                policies = json.loads(fragments['policies']) if 'policies' in fragments else build_list(db, actor, 'policies', manages, today)
                exceptions = exception_actions(db, actor, json.loads(fragments['exceptions']), policies,
                                               identities=exception_identities)
                fragment = json.dumps(exceptions, default=str, separators=(",", ":"))
                fragments['exceptions'] = fragment
                versions['exceptions'] = list_version('exceptions', fragment)
            from .services.notification_audience import source_visibility
            for kind in {'department_content', 'events'} & fragments.keys():
                cls = CATALOGUES[kind]
                visibility = source_visibility(db, actor, cls)
                if visibility is True:
                    continue
                entries = json.loads(fragments[kind])
                visible = set(db.scalars(select(cls.id).where(cls.department_id == actor.department_id,
                    cls.id.in_([row['id'] for row in entries]), visibility)))
                fragment = json.dumps([row for row in entries if row['id'] in visible], default=str, separators=(",", ":"))
                fragments[kind] = fragment
                versions[kind] = list_version(kind, fragment)
            if manages and 'department_content' in fragments:
                # Receipt activity is live evidence, outside the saved-definition cache.
                from .services import announcements
                entries = json.loads(fragments['department_content'])
                content = list(db.scalars(select(m.DepartmentContent).where(
                    m.DepartmentContent.id.in_([row['id'] for row in entries]))))
                counts = announcements.counts(db, actor, content)
                fragment = json.dumps([{**row, 'receipts': counts[row['id']]} for row in entries],
                                      default=str, separators=(",", ":"))
                fragments['department_content'] = fragment
                versions['department_content'] = list_version('department_content', fragment)
            if counts_only:
                # The checker count shares the catalogue projection.  The compact
                # response only reports the requested kinds.
                from .services import rule_catalogue
                dependency = rule_catalogue.in_force_dependency_stamp(today, timezone, input_revision, versions)
                from .services.conditional_reads import conditional_response, revision_etag
                tag = revision_etag("catalogue-counts", actor.department_id, actor.identity_id, actor.person_id,
                                    sorted(actor.roles), catalogue_access, reads_rules, sorted(requested_lists), active_only, dependency)
                cached = conditional_response(request, tag, cache_control="private, no-cache")
                if isinstance(cached, Response):
                    return cached
                counts = {kind: sum(row.get('active') is not False for row in json.loads(fragments[kind]))
                          for kind in requested_lists}
                projection = rule_catalogue.catalogue(db, actor, day=today, _stamp=dependency,
                                                      build_wait_seconds=settings.catalogue_build_wait_seconds,
                                                      in_force_only=True)
                if 'policies' in counts and not reads_rules:
                    counts['policies'] = projection['catalogue_counts']['in_force']
                return JSONResponse({'counts': counts,
                                     'rules_in_force': projection['catalogue_counts']['in_force']},
                                    headers=cached)
            have = set((known or "").split(",")) if known is not None else set()
            sent = [kind for kind in lists if versions[kind] not in have]
            body = ("{" + ",".join(f'"{kind}":{fragments[kind]}' for kind in sent)
                    + (',' if sent else '') + '"_versions":' + json.dumps(versions, separators=(",", ":"))
                    + (',"inactive_count":' + json.dumps(inactive_counts) if active_only else '') + "}").encode()
            if known is not None:
                return Response(body, media_type="application/json", headers={"ETag": cheap_tag,
                                 "Cache-Control": "private, no-cache"})
            # Unchanged catalogues answer 304 Not Modified, so a page load re-sends nothing.
            tag = cheap_tag
            headers = {"ETag": tag, "Cache-Control": "private, no-cache"}
            if request.headers.get("if-none-match") == tag:
                return Response(status_code=304, headers=headers)
            return Response(body, media_type="application/json", headers=headers)

    DIRECTORY_PUBLIC_FIELDS = frozenset({
        "id", "display_name", "given_name", "family_name", "title", "directory_section", "directory_order"})
    DIRECTORY_FIELDS = ("display_name", "given_name", "family_name", "title", "directory_section", "work_phone", "work_extension", "work_email", "pager", "directory_order")

    @app.get("/api/date-sets", response_model=dict[str, list[s.DateSetRecord]])
    def date_sets_list(request: Request, include_inactive: bool = False):
        from .services import date_sets
        with transaction(engine) as db:
            return date_sets.read(db, actor_for(request, db), include_inactive=include_inactive)

    @app.get("/api/date-sets/resolved", response_model=list[s.ResolvedDateSetEntry])
    def date_sets_resolved(request: Request, start: date, end: date,
                          set_id: list[s.ID] | None = Query(None, max_length=500),
                          entry_name: list[s.Name] | None = Query(None, max_length=500),
                          include_inactive: bool = False):
        from .services import date_sets
        with transaction(engine) as db:
            return date_sets.resolved(db, actor_for(request, db), start, end, set_id, entry_name,
                                      include_inactive=include_inactive)

    @app.get("/api/leave-types")
    def active_leave_types(request: Request):
        """Safe request choices for every signed-in role; Configuration reads archived types through catalogues."""
        from .services.leave_types import catalogue
        with transaction(engine) as db:
            actor = actor_for(request, db)
            return {"leave_types": catalogue(db, actor.department_id, active_only=True)}

    @app.get("/api/me/profile")
    def self_profile(request: Request):
        from .services.self_profile import profile
        with transaction(engine) as db:
            return profile(db, actor_for(request, db))

    @app.get("/api/directory")
    def directory(request: Request):
        """Active people, with the same field visibility as the people catalogue."""
        with transaction(engine) as db:
            actor = actor_for(request, db)
            from .services.independence import today
            from .services.people import active_people_on, current_people, visible_people
            day = today(db, actor.department_id)
            rows = list(db.execute(select(m.Person.id, *(getattr(m.Person, f) for f in DIRECTORY_FIELDS)).where(
                m.Person.organisation_id == actor.organisation_id, active_people_on(day)).order_by(
                m.Person.directory_order, m.Person.display_name, m.Person.id).limit(2001)))
            require(len(rows) <= 2000, "CATALOGUE_LIMIT", "This directory exceeds the current bounded UI; pagination upgrade required", 409)
            current = current_people(db, actor, day,
                people={r.id: {'active': True, 'title': r.title} for r in rows})['people']
            people = visible_people(db, actor, [{**dict(zip(("id", *DIRECTORY_FIELDS), r)),
                              'title': current.get(r.id, {}).get('display_title', r.title)} for r in rows], directory=True)
            allowed = DIRECTORY_PUBLIC_FIELDS | {row['legacy_field'] for row in s.DEFAULT_CONTACT_TYPES if row['legacy_field']} | {'contacts'}
            return {"people": [{key: value for key, value in person.items() if key in allowed} for person in people]}

    @app.get('/api/department/display/versions')
    def display_versions(request: Request, response: Response, limit: int = Query(50, ge=1, le=200),
                         cursor: int | None = Query(None, ge=1)):
        from .services.display_settings import versions
        response.headers['Cache-Control'] = 'no-store'
        with transaction(engine) as db:
            return versions(db, actor_for(request, db), limit, cursor)

    @app.post('/api/people/properties')
    def person_properties(body: s.PersonPropertiesInput, request: Request, response: Response):
        from .services.person_properties import read
        response.headers['Cache-Control'] = 'no-store'
        with transaction(engine) as db:
            return read(db, actor_for(request, db), body)

    @app.get('/api/people/{person_id}/card', response_model=s.PersonCard, response_model_exclude_none=True)
    def person_card(request: Request, person_id: str, response: Response, upcoming: int = Query(3, ge=0, le=50)):
        from .services.conditional_reads import conditional_response
        from .services.person_card import card_etag, read
        from .services.person_record import require_person
        with transaction(engine) as db:
            actor = actor_for(request, db)
            person = require_person(db, actor, person_id, s.ROLE_NAMES)
            headers = conditional_response(request, card_etag(db, actor, person, upcoming))
            if isinstance(headers, Response):
                return headers
            response.headers.update(headers)
            return read(db, actor, person_id, upcoming, person=person)

    @app.get('/api/contact-types')
    def contact_types(request: Request):
        from .services.contacts import types
        with transaction(engine) as db:
            actor = actor_for(request, db)
            return {'contact_types': [record(row) for row in types(db, actor)]}

    @app.get('/api/people/{person_id}/contacts/history')
    def contact_history(request: Request, person_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        from .services.person_record import require_person
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_person(db, actor, person_id, s.PERMISSIONS['catalogue.department'])
            H = m.DefinitionHistory
            rows = list(db.scalars(select(H).where(H.department_id == actor.department_id,
                H.kind == 'person_contacts', H.snapshot['person_id'].as_string() == person_id)
                .order_by(H.created_at.desc(), H.id).offset(offset).limit(limit + 1)))
            return {'versions': [{**record(row), 'snapshot': {key: value for key, value in row.snapshot.items()
                                      if key != 'value'}} for row in rows[:limit]], 'has_more': len(rows) > limit}

    @app.get('/api/people/summary', response_model=s.PeopleCounts)
    def people_summary(request: Request):
        from .services.people import current_people_counts
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'admin', 'coordinator', 'approver', 'credentialer', 'auditor', 'programme_lead')
            return current_people_counts(db, actor)

    @app.get('/api/catalogues/categories/duplicate-codes')
    def rank_duplicate_codes(request: Request):
        from .services.catalogue import duplicate_rank_codes
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'admin', 'coordinator', 'auditor')
            return duplicate_rank_codes(db, actor)

    @app.get("/api/policies")
    def policies_list(request: Request, status: str = Query("retired", pattern="^(retired|active|proposed)$"), effective: bool = False,
                      limit: int = Query(4000, ge=1, le=4000), offset: int = Query(0, ge=0)):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            rows = list(db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id, m.Policy.status == status).order_by(m.Policy.id).offset(offset).limit(limit + 1)))
            has_more, rows = len(rows) > limit, rows[:limit]
            from .services.scoring_defaults import seeded_rows
            from .services.policies import policy_versions
            result = {"policies": policy_versions(db, actor, seeded_rows(db, [record(r) for r in rows])),
                      'has_more': has_more, 'offset': offset, 'limit': limit}
            if effective:
                from .services.policies import effective_sources
                result['effective'] = effective_sources(db, actor, rows)
            return result

    @app.get('/api/exceptions/{exception_id}/approval', response_model=s.ExceptionApproval)
    def exception_approval(exception_id: s.ID, request: Request):
        from .services.policies import exception_approval as approval
        with transaction(engine) as db:
            return approval(db, actor_for(request, db), exception_id)

    @app.post('/api/eligibilities/{credential_id}/published-impact')
    def eligibility_published_impact(credential_id: s.ID, value: s.RelationshipUpdateDates, request: Request):
        from .services.credential_impact import preview
        with transaction(engine) as db:
            return preview(db, actor_for(request, db), credential_id, value)

    @app.post("/api/catalogue/preview")
    def catalogue_preview(command: s.Command, request: Request):
        from .services.catalogue_preview import StaleBeforeCapture, build_preview, capture_before, prepare_preview, evaluate_preview
        postgres = engine.dialect.name == 'postgresql'
        reader = engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True) if postgres else engine

        def capture_and_trial(*, skip_contact_snapshots):
            with transaction(reader) as db:
                if not postgres:
                    db.execute(text('BEGIN'))
                db.info['_request_parents'] = {}
                before = capture_before(db, actor_for(request, db, write=True), command,
                                        skip_contact_snapshots=skip_contact_snapshots)
            return retry_write(engine, lambda db: prepare_preview(db, actor_for(request, db, write=True), command, before))

        def prepare():
            prepared = capture_and_trial(skip_contact_snapshots=True)
            if prepared is None:
                # The contact trial changed a scheduling input. Capture its baseline after releasing the writer.
                prepared = capture_and_trial(skip_contact_snapshots=False)
            return prepared

        try:
            prepared = prepare()
        except StaleBeforeCapture:
            if command.expected_fingerprint:
                raise
            log.info("catalogue_preview_recapture", extra={"correlation_id": request.state.correlation_id})
            prepared = prepare()
        return evaluate_preview(build_preview(engine, prepared))

    @app.get("/api/catalogues/{kind}/{object_id}/history")
    def catalogue_history(kind: str, object_id: str, request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor)
            if kind in {'events', 'event_occurrence'}:
                from .services import events
                events.require_definition_access(actor)
            require(kind in CATALOGUES, "NOT_FOUND", "Catalogue not found", 404)
            if kind == 'department_content':
                require_role(actor, 'admin', 'coordinator')
            row = scoped(db, CATALOGUES[kind], object_id, actor, include_withdrawn=True)
            from .services.training import catalogue_scope, led_programmes
            cls = CATALOGUES[kind]
            columns = cls.__table__.c
            require(db.scalar(select(columns.id).where(columns.id == row.id,
                              catalogue_scope(db, actor, kind, columns))) is not None,
                    "NOT_FOUND", "Record not found", 404)
            history_kinds = [kind]
            if kind == 'programme_memberships':
                history_kinds += ['programme_membership.withdraw', 'programme_membership.restore']
            query = select(m.DefinitionHistory).where(
                m.DefinitionHistory.department_id == actor.department_id,
                m.DefinitionHistory.kind.in_(history_kinds),
                m.DefinitionHistory.object_id == object_id)
            led = led_programmes(db, actor)
            if led is not None:
                H = m.DefinitionHistory
                if kind == "rotations":
                    # Legacy snapshots without programme provenance cannot prove director access after cohort moves.
                    query = query.where(H.snapshot["programme_id"].as_string().in_(led))
                elif kind in {"cohorts", "programme_memberships", "mentorships", "programme_calendar_entries", "rotation_templates"}:
                    scope = H.snapshot["programme_id"].as_string().in_(led)
                    if kind == "rotation_templates":
                        scope = or_(scope, H.snapshot["programme_id"].as_string().is_(None))
                    query = query.where(scope)
            versions = [record(r) for r in db.scalars(query.order_by(m.DefinitionHistory.object_revision))]
            if kind == 'people':
                from .services.people import visible_person
                versions = [{**version, 'snapshot': visible_person(actor, version['snapshot'])} for version in versions]
            return {"versions": versions}

    @app.get("/api/review-routes")
    def review_routes(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, *s.TEAM_READER_ROLES)
            query = select(m.ReviewRoute).where(m.ReviewRoute.department_id == actor.department_id)
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            routes = list(db.scalars(query.order_by(m.ReviewRoute.starts_on.desc(), m.ReviewRoute.id).offset(offset).limit(limit)))
            return {"routes": [record(row) for row in routes], "total": total, "limit": limit, "offset": offset,
                    "has_more": offset + len(routes) < total}

    @app.get("/api/review-delegations")
    def review_delegations(request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "admin", "approver", "programme_lead", "auditor")
            query = select(m.ReviewDelegation).where(m.ReviewDelegation.department_id == actor.department_id)
            if not actor.permits("admin", "auditor"):
                query = query.where((m.ReviewDelegation.delegator_id == actor.identity_id) | (m.ReviewDelegation.delegate_id == actor.identity_id))
            delegates = list(db.scalars(query.order_by(m.ReviewDelegation.created_at.desc(), m.ReviewDelegation.seq.desc()).limit(201)))
            identities = list(db.scalars(select(m.Identity).join(m.RoleBinding, m.RoleBinding.identity_id == m.Identity.id).where(
                m.RoleBinding.department_id == actor.department_id, m.Identity.disabled.is_(False),
                m.Identity.issuer != SWAP_AUTOMATION_ISSUER,
                m.RoleBinding.role.in_(["approver", "programme_lead", "admin"])).distinct().order_by(m.Identity.display_name, m.Identity.id).limit(501)))
            return {"delegations": [record(row) for row in delegates[:200]], "delegations_partial": len(delegates) > 200,
                    "reviewers": [{"id": identity.id, "display_name": identity.display_name,
                                   "roles": sorted(current_roles(db, identity.id, actor.department_id) & reviews.REVIEW_ROLES)}
                                   for identity in identities[:500] if current_roles(db, identity.id, actor.department_id) & reviews.REVIEW_ROLES],
                    "reviewers_partial": len(identities) > 500}

    @app.get("/api/self-approval")
    def self_approval(request: Request):
        """The guarded actions (one list, from the service) and every permission that waives them."""
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "admin", "approver", "programme_lead", "auditor")
            rows = list(db.scalars(select(m.SelfApprovalPermission).where(m.SelfApprovalPermission.department_id == actor.department_id)
                                   .order_by(m.SelfApprovalPermission.created_at.desc(), m.SelfApprovalPermission.seq.desc()).limit(500)))
            # SW3-B-20: an ended permission says when, by whom and why it ended (its revoking history entry).
            H = m.DefinitionHistory
            # Each grant's latest entry that ended it (a later restore records its own entry on the grant it replaces).
            ends = {h.object_id: h for h in db.scalars(select(H).where(H.department_id == actor.department_id, H.kind == "self_approval",
                    H.object_id.in_([r.id for r in rows if r.revoked] or [""])).order_by(H.object_revision))
                    if h.snapshot.get("revoked") and not h.snapshot.get("restored_to_id")}
            names = {i.id: i.display_name for i in db.scalars(select(m.Identity).where(m.Identity.id.in_(
                {r.identity_id for r in rows if r.identity_id} | {h.actor_id for h in ends.values()} or {""})))}
            day = independence.today(db, actor.department_id)
            holders = independence.department_holders(db, actor.department_id)
            usable = {r.id: independence.usable_actions(r, holders) for r in rows}
            # SW3-B-18: what a new grant may cover, per person and per role, so the form offers only those actions;
            # each saved grant names any action nobody it covers can take (kept and shown, never rewritten).
            grantable = {"identities": {i: sorted(independence.performable(effective)) for i, (_, effective) in holders.items()},
                         "roles": {role: sorted(independence.performable(effective_roles({role}))) for role in s.ROLE_NAMES}}
            return {"actions": [{"key": k, "label": v} for k, v in independence.LABELS.items()],
                    "limits": {"max_days": s.SELF_APPROVAL_MAX_DAYS, 'default_days': s.SELF_APPROVAL_MAX_DAYS},
                    "grantable": grantable,
                    "permissions": [{**record(r), "subject": names.get(r.identity_id) if r.identity_id else None,
                                      'no_end_date': r.ends_on is None, 'end_label': str(r.ends_on) if r.ends_on else 'No end date',
                                      "state": independence.permission_state(r, day), "usable_actions": usable[r.id],
                                      "unusable_actions": sorted(set(r.actions) - set(usable[r.id])),
                                      "ended": {"at": ends[r.id].created_at, "by_name": names.get(ends[r.id].actor_id), "reason": ends[r.id].reason}
                                      if r.id in ends else None}
                                    for r in rows]}

    @app.get("/api/swaps/options")
    def swap_options(request: Request, assignment_id: s.ID | None = None, swap_id: s.ID | None = None,
                     recipient_id: s.ID | None = None, kind: s.SwapKind | None = None):
        with transaction(engine) as db:
            return swaps.options(db, actor_for(request, db), assignment_id, swap_id, recipient_id, kind)

    @app.get("/api/swaps")
    def swap_requests(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                      person_id: s.ID | None = None):
        with transaction(engine) as db:
            return swaps.list_requests(db, actor_for(request, db), limit, offset, person_id)

    @app.get("/api/swaps/{swap_id}")
    def swap_request(swap_id: str, request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            row = swaps.get_scoped(db, actor, swap_id)
            return {"swap": swaps.projection(db, row, actor=actor),
                    "events": swaps.events_for(db, actor, row)}

    @app.get("/api/planner/views")
    def planner_views(request: Request):
        with transaction(engine) as db:
            return planner_tools.list_views(db, actor_for(request, db))

    @app.post("/api/planner/staffing-suggestions")
    def staffing_suggestions(value: s.StaffingSuggestionsInput, request: Request):
        from .services.staffing import suggestions
        # Pure candidates need a coherent read, without reserving the department writer.
        reader = engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True) if engine.dialect.name == 'postgresql' else engine
        with transaction(reader) as db:
            actor = actor_for(request, db, write=True)
            return disclose_leave(db, actor, suggestions(db, actor, value))

    @app.post("/api/planner/fill-options", response_model=s.FillOptionsResult, response_model_exclude_unset=True)
    def fill_options(value: s.FillOptionsInput, request: Request, async_options: bool = Query(False)):
        if async_options or 'respond-async' in request.headers.get('prefer', '').lower():
            from .services import jobs
            with transaction(engine, write=True) as db:
                actor = actor_for(request, db, write=True)
                queued = jobs.queue_fill_options(db, actor, value)
            return JSONResponse(json_value(queued), status_code=202)
        from .services.staffing import fill_options as options
        # Candidate explanations are read-only.  Keep their bounded validator
        # work off the serialized application writer, just like staffing
        # suggestions, so a large rota cannot delay an unrelated save.
        reader = (engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True)
                  if engine.dialect.name == 'postgresql' else engine)
        with transaction(reader) as db:
            actor = actor_for(request, db, write=True)
            return disclose_leave(db, actor, {**options(db, actor, value), 'provisional_page': False})

    @app.post("/api/planner/issues/replace-preview")
    def issues_replace_preview(value: s.IssuePreviewInput, request: Request):
        from .services.issues import preview
        with transaction(engine, write=True) as db:
            actor = actor_for(request, db, write=True)
            return disclose_leave(db, actor, preview(db, actor, value))

    @app.post("/api/planner/why", response_model=s.WhyResult, response_model_exclude_unset=True)
    def planner_why(value: s.WhyInput, request: Request):
        from .services.staffing import prepare_why, why
        postgres = engine.dialect.name == 'postgresql'
        reader = engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True) if postgres else engine
        with transaction(reader) as db:
            if not postgres:
                db.execute(text('BEGIN'))
            # Authentication and input capture can share the department row from this read view.
            db.info['_request_parents'] = {}
            actor = actor_for(request, db)
            source, options = prepare_why(db, actor, value)
        result = why(source, options, value)
        with transaction(engine) as db:
            current_actor = actor_for(request, db)
            require((current_actor.identity_id, current_actor.department_id) == (actor.identity_id, actor.department_id),
                    'PLANNER_CONTEXT_CHANGED', 'The signed-in department changed; open the rota explanation again.', 409)
            require_role(current_actor, *planner_tools.PLANNER_ROLES)
            return disclose_leave(db, current_actor, result)

    @app.post("/api/planner/preview")
    def planner_preview(command: s.Command, request: Request, async_preview: bool = Query(False)):
        # Roll out the durable path explicitly.  Existing clients keep the old
        # response until they send Prefer: respond-async (or async_preview=1).
        if async_preview or 'respond-async' in request.headers.get('prefer', '').lower():
            from .services import jobs
            with transaction(engine, write=True) as db:
                actor = actor_for(request, db, write=True)
                queued = jobs.queue_planner_preview(db, actor, command)
            return JSONResponse(json_value(queued), status_code=202)
        def operation(db):
            actor = actor_for(request, db, write=True)
            return disclose_leave(db, actor, planner_tools.preview(db, actor, command))
        if engine.dialect.name == 'postgresql':
            # The trial rolls back its writes. One snapshot keeps admission and both
            # views consistent without acquiring the instance-wide writer.
            trial = engine.execution_options(isolation_level='REPEATABLE READ')
            try:
                return JSONResponse(retry_write(trial, operation, serialize=False))
            except OperationalError as exc:
                if getattr(exc.orig, 'sqlstate', None) in {'40001', '40P01'}:
                    raise DatabaseWriteTimeout() from None
                raise
        return JSONResponse(retry_write(engine, operation))

    @app.get("/api/workspaces/{workspace_id}/compare")
    def workspace_comparison(workspace_id: str, other_id: str, request: Request,
                             limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
        with transaction(engine) as db:
            return planner_tools.compare(db, actor_for(request, db), workspace_id, other_id, limit, offset)

    @app.get('/api/planning-groups')
    def planning_groups(request: Request, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0), group_id: s.ID | None = None):
        from .services import planning_groups as service
        with transaction(engine) as db:
            return service.list_groups(db, actor_for(request, db), offset=offset, limit=limit, group_id=group_id)

    @app.get('/api/planning-settings')
    def planning_settings(request: Request, history: bool = False, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
        from .services import planning_groups as service
        with transaction(engine) as db:
            return service.read_settings(db, actor_for(request, db), history=history, offset=offset, limit=limit)

    @app.get('/api/score-settings')
    def score_settings(request: Request, history: bool = False, limit: int = Query(100, ge=1, le=100),
                       offset: int = Query(0, ge=0)):
        from .services import score_settings as service
        with transaction(engine) as db:
            return service.read(db, actor_for(request, db), history_view=history, offset=offset, limit=limit)

    @app.get('/api/workspaces/{workspace_id}/score-review', response_model=s.ScoreReview)
    def workspace_score_review(workspace_id: str, request: Request):
        from .services import score_review
        with transaction(engine) as db:
            return score_review.read(db, actor_for(request, db), workspace_id)

    @app.get('/api/workspaces/{workspace_id}/publication-audience', response_model=s.PublicationAudiencePreview)
    def workspace_publication_audience(workspace_id: s.ID, request: Request):
        from types import SimpleNamespace
        from .services import notification_audience, published_changes
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_permission(actor, 'workspace.publish')
            workspace = scoped(db, m.Workspace, workspace_id, actor)
            require(workspace.status == 'approved', 'WORKFLOW_STATE', 'Only an approved candidate can be published', 409)
            snapshot = publication_snapshot(db, workspace, evaluation=workspace.validation)
            previous = db.get(m.Publication, workspace.base_publication_id) if workspace.base_publication_id else None
            context = SimpleNamespace(id=None, department_id=workspace.department_id, workspace_id=workspace.id,
                                      snapshot=snapshot, sequence=None, starts_on=workspace.starts_on,
                                      ends_on=workspace.ends_on, effective_until=previous.effective_until if previous else None)
            changes = published_changes.personal_changes(db, previous, context) if previous else None
            return notification_audience.preview(db, actor, context, person_changes=changes)

    @app.get("/api/workspaces")
    def workspaces(request: Request, response: Response, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
                   start: s.date | None = None, end: s.date | None = None, current_only: bool = False,
                   archived: bool = False, include_archived: bool = False, archived_only: bool = False,
                   q: str = Query("", max_length=160), copy_starts_on: s.date | None = None,
                   context_before: s.date | None = None,
                   search_options: s.SearchOptions | None = Depends(parse_search_options)):

        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator", "approver", "programme_lead", "auditor")
            from .services.conditional_reads import conditional_response
            from .services.workspace_reads import workspace_list_etag
            archived_only = archived_only or archived
            representation = [limit, offset, start, end, current_only, archived, include_archived, archived_only, q,
                              copy_starts_on, context_before, search_options.model_dump(mode="json") if search_options else None]
            db.info['_request_parents'] = {}
            tag = workspace_list_etag(db, actor, representation)
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            if context_before is not None:
                from .snapshots import context_draft_clauses
                W = m.Workspace
                choices = list(db.execute(select(W.id, W.name, W.starts_on, W.ends_on, W.status).where(
                    W.department_id == actor.department_id, W.ends_on <= context_before,
                    *context_draft_clauses()).order_by(W.ends_on.desc(), W.created_at.desc(), W.id).offset(offset)
                    .limit(limit + 1)).mappings())
                from .services.common import rota_names
                names = rota_names(db, actor.roles, actor.person_id, {row['id']: row['name'] for row in choices[:limit]})
                return {"workspaces": [{**row, "name": names[row['id']]} for row in choices[:limit]],
                        "has_more": len(choices) > limit}
            # Keep the first page useful when a department retains years of rotas: current and
            # upcoming periods sort ahead of historical/far-future rows, while explicit filters
            # still expose the complete bounded history through offset/limit.
            today = m.now().date()
            near_until = today + timedelta(days=settings.workspace_read_max_days)
            near_since = today - timedelta(days=settings.workspace_read_max_days)
            query = select(m.Workspace).options(defer(m.Workspace.validation), defer(m.Workspace.review_snapshot)).where(
                m.Workspace.department_id == actor.department_id)
            if archived_only:
                query = query.where(m.Workspace.archived_at.is_not(None))
            elif not include_archived:
                query = query.where(m.Workspace.archived_at.is_(None))
            if not include_archived and not archived_only:
                query = query.where(~m.Workspace.id.in_(select(m.PublicationChangeList.workspace_id)))
            if search_options is not None:
                from .services.search import predicate
                query = query.where(predicate(db, actor, 'workspaces', q, search_options))
            elif q.strip():
                term = q.strip()
                escape = lambda value: value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                fields = [m.Workspace.name, m.Workspace.status]
                matches = [field.ilike("%" + escape(term) + "%", escape="\\") for field in fields]
                # Joined names also find the label shown in the menu ("OctoberRota" → "October Rota").
                # Punctuation in the query remains literal, including SQL LIKE wildcards.
                if term.isalnum():
                    for field in fields:
                        joined = field
                        for separator in (" ", "-", "_", ".", "'", "\t", "\n"):
                            joined = func.replace(joined, separator, "")
                        matches.append(joined.ilike("%" + escape(term) + "%", escape="\\"))
                query = query.where(or_(*matches))
            if current_only:
                query = query.where(m.Workspace.status.in_(['draft', 'submitted', 'approved', 'published']))
            if start:
                query = query.where(m.Workspace.ends_on > start)
            if end:
                query = query.where(m.Workspace.starts_on < end)
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            rows = list(db.scalars(query.order_by(
                case((and_(m.Workspace.starts_on <= today, m.Workspace.ends_on > today), 0),
                     (and_(m.Workspace.starts_on >= today, m.Workspace.starts_on < near_until), 1),
                     (and_(m.Workspace.ends_on > near_since, m.Workspace.starts_on < today), 2),
                     (m.Workspace.starts_on >= near_until, 3), else_=4),
                case((m.Workspace.starts_on >= today, m.Workspace.starts_on), else_=None).asc().nulls_last(),
                m.Workspace.created_at.desc(), m.Workspace.seq.desc()).offset(offset).limit(limit)))
            historical_ids = set(db.scalars(select(m.AuditEvent.target_id).where(
                m.AuditEvent.department_id == actor.department_id, m.AuditEvent.target_id.in_([r.id for r in rows]),
                m.AuditEvent.action.in_({'workspace.submit', 'workspace.publish'})))) if rows else set()
            published_ids = set(db.scalars(select(m.Publication.workspace_id).where(
                m.Publication.department_id == actor.department_id, m.Publication.workspace_id.in_([r.id for r in rows])))) if rows else set()
            # Keep shared fingerprint rows alive across the list (the ORM identity map holds weak references).
            # Submitted or approved, but inputs changed since: it must be checked and resubmitted before review or publishing.
            from .snapshots import cheap_fingerprints
            from .services.workspace_reads import stored_checks
            checks = stored_checks(db, rows)
            # A sidebar label is never permission to rebuild a full scheduling context.
            visible = [r for r in rows if r.ends_on > near_since and r.starts_on < near_until]
            current_inputs = cheap_fingerprints(db, visible, checks=checks)
            outdated = lambda r: r.status in ("submitted", "approved") and (
                r.id not in current_inputs or r.submitted_fingerprint != current_inputs[r.id])
            # Competing rotas for the same days, and published rotas overlapped without a decision (schedule.flags).
            overlap = schedule.flags(db, actor.department_id, rows)
            publications = {wid: pid for wid, pid in db.execute(select(m.Publication.workspace_id, m.Publication.id).where(
                m.Publication.department_id == actor.department_id, m.Publication.workspace_id.in_([r.id for r in rows])))} if rows else {}
            draft_ids = [r.id for r in rows if r.status == 'draft']
            from .services.planning import delete_draft_refused_clause
            delete_refused_ids = set(db.scalars(select(m.Workspace.id).where(
                m.Workspace.id.in_(draft_ids),
                delete_draft_refused_clause(m.Workspace.id)))) if draft_ids else set()
            forks = {row.workspace_id: row for row in db.scalars(select(m.WorkspaceForkBase).options(load_only(
                m.WorkspaceForkBase.id, m.WorkspaceForkBase.workspace_id, m.WorkspaceForkBase.source_workspace_id,
                m.WorkspaceForkBase.source_revision, m.WorkspaceForkBase.source_content_revision)).where(
                m.WorkspaceForkBase.workspace_id.in_([r.id for r in rows])))} if rows else {}
            fork_sources = {wid: revision for wid, revision in db.execute(select(m.Workspace.id, m.Workspace.revision).where(
                m.Workspace.id.in_([f.source_workspace_id for f in forks.values()])))} if forks else {}
            from .services.rota_summary import coverage_gaps
            from packages.domain.rota_summary import suggested_rota_name
            gaps = coverage_gaps(db, actor.department_id)
            previous = db.scalar(select(m.Workspace.id).where(m.Workspace.department_id == actor.department_id,
                m.Workspace.archived_at.is_(None), m.Workspace.ends_on <= copy_starts_on).order_by(
                m.Workspace.ends_on.desc(), m.Workspace.starts_on.desc(), m.Workspace.created_at.desc(), m.Workspace.seq.desc()).limit(1)) if copy_starts_on else None
            from .services.common import rota_names
            names = rota_names(db, actor.roles, actor.person_id, {r.id: r.name for r in rows})
            return {"workspaces": [{**record(r, exclude=("validation", "review_snapshot")), "name": names[r.id],
                                    "allowed_actions": allowed_actions(db, actor, r, historical=r.id in historical_ids,
                                                                        published=r.id in published_ids,
                                                                        delete_refused=r.id in delete_refused_ids),
                                    **({'fork_base': {'id': forks[r.id].id, 'source_workspace_id': forks[r.id].source_workspace_id,
                                                       'source_revision': forks[r.id].source_revision,
                                                       'source_content_revision': forks[r.id].source_content_revision,
                                                       'parent_changed_since': max(0, fork_sources.get(forks[r.id].source_workspace_id,
                                                                                                      forks[r.id].source_revision)
                                                                                     - forks[r.id].source_revision)}}
                                       if r.id in forks else {}),
                                    "health": {**checks[r.id]['health'], 'fresh': bool(checks[r.id]['checked'] and checks[r.id]['fingerprint'] == current_inputs.get(r.id))}, "outdated": outdated(r),
                                    "publication_id": publications.get(r.id), "overlap": overlap.get(r.id),
                                    "suggested_name": suggested_rota_name(r.starts_on, r.ends_on),
                                    "coverage_gaps": [g for g in gaps if r.id in (g['before_workspace_id'], g['after_workspace_id'])]}
                                   for r in rows], "total": total, "coverage_gaps": gaps, "previous_workspace_id": previous,
                    "limit": limit, "offset": offset, "has_more": offset + len(rows) < total}

    @app.post("/api/workspaces/{workspace_id}/lead-context", response_model=s.LeadContext)
    def workspace_lead_context(workspace_id: s.ID, value: s.LeadContextInput, request: Request):
        from .services.leadership import lead_context
        with transaction(engine) as db:
            actor = actor_for(request, db)
            return lead_context(db, actor, workspace_id, value)

    @app.get('/api/workspaces/{workspace_id}/fix-list', response_model=s.FixList | s.FixListLean | s.FixListDetail)
    def get_workspace_fix_list(workspace_id: str, request: Request, response: Response,
                               group_version_id: s.ID | None = None,
                               include_evidence: bool = True, cause_id: str | None = Query(None, max_length=300),
                               evidence_hash: str | None = Query(None, max_length=64),
                               view: Literal['full', 'lean', 'detail'] = 'full',
                               revision: int | None = Query(None, ge=0),
                               fingerprint: str | None = Query(None, max_length=128)):
        from .services.rota_summary import workspace_summary
        from .services.conditional_reads import conditional_response, revision_etag
        from .services.workspace_reads import fast_etag
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'coordinator', 'approver', 'programme_lead', 'auditor')
            workspace = scoped(db, m.Workspace, workspace_id, actor, options=(
                defer(m.Workspace.validation), defer(m.Workspace.review_snapshot)))
            tag_values = (fast_etag(db, actor, workspace, include_check_state=True), group_version_id,
                          include_evidence, cause_id, evidence_hash)
            if view != 'full':
                tag_values += (view, revision, fingerprint)
            tag = revision_etag("workspace-fix-list", *tag_values)
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            db.refresh(workspace, attribute_names=("validation", "review_snapshot"))
            result = workspace_summary(db, actor, workspace, group_version_id=group_version_id)['fix_list']
            causes = result['causes']
            require(view != 'detail' or cause_id is not None, 'CAUSE_REQUIRED',
                    'A cause id is required for detail view', 422)
            require(revision is None or revision == result['revision'], 'STALE_REVISION',
                    'The Fix list changed; refresh it before opening details', 409)
            require(fingerprint is None or fingerprint == result['fingerprint'], 'STALE_INPUTS',
                    'The Fix list inputs changed; refresh it before opening details', 409)
            if cause_id is not None:
                causes = [cause for cause in causes if cause['id'] == cause_id]
                require(bool(causes), 'NOT_FOUND' if view == 'detail' else 'FIX_LIST_CHANGED',
                        'Cause not found' if view == 'detail' else 'The Fix list changed; refresh it before opening details',
                        404 if view == 'detail' else 409)
            if evidence_hash is not None:
                require(cause_id is not None and all(cause['evidence_hash'] == evidence_hash for cause in causes),
                        'FIX_LIST_CHANGED', 'The Fix list changed; refresh it before opening details', 409)
            if view == 'lean':
                from .services.rota_summary import lean_fix_list
                # Keep optional lean fields genuinely omitted while full/detail retain their existing defaults.
                payload = s.FixListLean.model_validate(lean_fix_list({**result, 'causes': causes}))
                return JSONResponse(content=payload.model_dump(mode='json', exclude_unset=True), headers=dict(cached))
            if view == 'detail':
                return {'workspace_id': result.get('workspace_id'), 'revision': result.get('revision'),
                        'fingerprint': result.get('fingerprint'), 'cause': causes[0]}
            return {**result, 'causes': causes if include_evidence else [{**cause, 'evidence': []} for cause in causes]}

    @app.get("/api/workspaces/{workspace_id}/summary", response_model=s.RotaSummary | s.RotaCauseSummary)
    def get_workspace_summary(workspace_id: str, request: Request, response: Response, group_version_id: s.ID | None = None,
                              view: Literal["full", "causes"] = "full"):
        from .services.rota_summary import workspace_cause_summary, workspace_summary
        from .services.conditional_reads import conditional_response
        from .services.workspace_reads import summary_etag
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator", "approver", "programme_lead", "auditor")
            workspace = scoped(db, m.Workspace, workspace_id, actor, options=(
                defer(m.Workspace.validation), defer(m.Workspace.review_snapshot)))
            tag = summary_etag(db, actor, workspace, view, group_version_id)
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            db.refresh(workspace, attribute_names=("validation",))
            result = (workspace_cause_summary if view == 'causes' else workspace_summary)(
                db, actor, workspace, group_version_id=group_version_id)
            return {key: result[key] for key in s.RotaCauseSummary.model_fields} if view == "causes" else result

    @app.post('/api/workspaces/{workspace_id}/readiness', response_model=s.PeriodReadiness)
    def workspace_readiness(workspace_id: s.ID, value: s.PlanningContextInput, request: Request):
        from .services.planning_context import readiness
        with transaction(engine) as db:
            return readiness(db, actor_for(request, db), workspace_id, value)

    @app.post('/api/workspaces/{workspace_id}/observations/{observation_id}/evidence', response_model=s.RequirementEvidence)
    def observation_evidence(workspace_id: s.ID, observation_id: str, value: s.RequirementEvidenceInput, request: Request):
        from .services.planning_context import evidence
        with transaction(engine) as db:
            return evidence(db, actor_for(request, db), workspace_id, observation_id, value)

    @app.get("/api/workspaces/{workspace_id}/shifts/{shift_id}/history")
    def shift_history(workspace_id: str, shift_id: str, request: Request):
        """What changed on one session, newest first, from the rota's own change sets (last 60 edits)."""
        from .snapshots import workspace_content
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator", "approver", "programme_lead", "auditor")
            workspace = scoped(db, m.Workspace, workspace_id, actor)
            sets = list(db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace.id)
                                   .order_by(m.ChangeSet.created_at.desc(), m.ChangeSet.seq.desc()).limit(60)))
            after = next((s for s in workspace_content(db, workspace)["shifts"] if s["id"] == shift_id), None)
            names = {}

            def who(identity_id):
                if identity_id not in names:
                    row = db.get(m.Identity, identity_id)
                    names[identity_id] = row.display_name if row else "Unknown"
                return names[identity_id]
            people = {p.id: p.display_name for p in db.scalars(select(m.Person).where(m.Person.organisation_id == actor.organisation_id))}

            def view(shift):
                if shift is None:
                    return None
                return {"start": shift["start"], "end": shift["end"], "cancelled": bool(shift.get("cancelled")), "locked": bool(shift.get("locked")),
                        "location_id": shift.get("location_id"), "people": {p["person_id"]: {
                            "start": p.get("start", shift["start"]), "end": p.get("end", shift["end"]),
                            "participation": p.get("participation", "staff"), "fixed": bool(p.get("fixed")),
                            "overtime": bool(p.get("overtime")), "team_lead": bool(p.get("team_lead")),
                            "supervisor_person_id": p.get("supervisor_person_id"), "segments": p.get("segments") or []}
                            for p in shift.get("participants", [])}}
            entries = []
            for cs in sets:
                before = next((s for s in cs.before_snapshot.get("shifts", []) if s["id"] == shift_id), None)
                a, b = view(before), view(after)
                if a != b:
                    changes = []
                    if a is None:
                        changes.append("Session created")
                    elif b is None:
                        changes.append("Session removed")
                    else:
                        added = [people.get(p, "Unknown") for p in sorted(b["people"]) if p not in a["people"]]
                        removed = [people.get(p, "Unknown") for p in sorted(a["people"]) if p not in b["people"]]
                        if added:
                            changes.append("Added " + ", ".join(added))
                        if removed:
                            changes.append("Removed " + ", ".join(removed))
                        for person_id in sorted(a["people"].keys() & b["people"].keys()):
                            for field, label in (("start", "Assignment start"), ("end", "Assignment end"),
                                    ("participation", "Participation"), ("fixed", "Fixed assignment"),
                                    ("overtime", "Overtime"), ("team_lead", "Team lead"),
                                    ("supervisor_person_id", "Supervision"), ("segments", "Assignment segments")):
                                if a["people"][person_id][field] != b["people"][person_id][field]:
                                    changes.append(f"{label} changed for {people.get(person_id, 'Unknown')}")
                        if (a["start"], a["end"]) != (b["start"], b["end"]):
                            changes.append("Time changed")
                        if a["location_id"] != b["location_id"]:
                            changes.append("Location changed")
                        if a["cancelled"] != b["cancelled"]:
                            changes.append("Closed" if b["cancelled"] else "Reopened")
                        if a["locked"] != b["locked"]:
                            changes.append("Locked" if b["locked"] else "Unlocked")
                    entries.append({"at": cs.created_at.isoformat(), "by": who(cs.actor_id), "reason": cs.reason,
                                    "undone": cs.undone, "changes": changes or ["Details changed"]})
                after = before
            return {"shift_id": shift_id, "entries": entries, "partial": len(sets) == 60}

    @app.get("/api/workspaces/{workspace_id}/shifts/{shift_id}/rules")
    def shift_rules(workspace_id: str, shift_id: str, request: Request):
        """Which active rules apply to each person on one session, using the validator's own scope test.
        Pass/fail comes from the stored rota check; it is reported as unchecked when that check is stale."""
        from .snapshots import domain_snapshot
        from packages.domain.validation import exception_for, policy_applies, within_dates
        from packages.domain.rule_map import NUMBERS, adjustable, rule_number
        from packages.domain.fairness import GOAL_FINDINGS
        from packages.domain.time import aware
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator", "approver", "programme_lead", "auditor")
            workspace = scoped(db, m.Workspace, workspace_id, actor)
            data = domain_snapshot(db, workspace)
            shift = next((s for s in data["shifts"] if s["id"] == shift_id), None)
            require(shift is not None, "NOT_FOUND", "Session not found in this rota", 404)
            validation = workspace.validation or {}
            fresh = bool(validation) and validation.get("fingerprint") == data["fingerprint"]
            # A Balance goal's findings are per person over the period (no session): they count on each of that person's sessions.
            findings = [f for f in validation.get("findings", []) if f.get("rule_id") and (
                f.get("shift_id") == shift_id or not f.get("shift_id") and f.get("code") in GOAL_FINDINGS)]
            policies = active_rules(data["policies"], context=data)
            plain = active_rules(data["policies"], overrides=False, context=data)
            winners = {}  # rule id -> the active rules that replace it for the people they match
            for p in data["policies"]:
                for target in (p["definition"].get("overrides") or []) if p["status"] == "active" and p.get("active", True) else []:
                    winners.setdefault(target, []).append(p)
            data = {**data, "policies": policies}
            names = {}

            def who(identity_id):
                if identity_id and identity_id not in names:
                    row = db.get(m.Identity, identity_id)
                    names[identity_id] = row.display_name if row else None
                return names.get(identity_id)

            def row(policy, person_id, start, end, status, hits=()):
                definition = policy["definition"]
                exc = exception_for(data, policy, person_id, start, end)
                return {"id": policy["id"], "name": policy.get("name") or "Rule", "kind": definition["rule"].get("kind"),
                        "severity": definition.get("severity"), "status": status, "messages": [f["message"] for f in hits][:3],
                        "number": rule_number(definition["rule"]), "own_line": policy.get("template_line"),
                        "adjusted": bool(policy.get("rota_adjusted")), "safety": s.safety_rule(definition),
                        "exception": {"starts_on": exc["starts_on"], "ends_on": exc["ends_on"], "by": who(exc.get("approved_by"))} if exc else None,
                        "exception_allowed": bool(definition.get("exception_allowed")) and not s.safety_rule(definition), "adjustable": adjustable(definition["rule"])}

            def applies(policy, person_id, start, end):
                try:
                    return policy_applies(data, policy, person_id, shift, start, end)
                except (KeyError, TypeError, ValueError):
                    return False

            def rules_for(person_id, start, end):
                rows = []
                for policy in policies:
                    # A template's copies read as one rule; a catalogue rule's lines each check their own thing.
                    line = (policy.get("template_line") or {}).get("id")
                    if any(r["id"] == policy["id"] and (r["own_line"] or {}).get("id") == line for r in rows) or not applies(policy, person_id, start, end):
                        continue
                    hits = [f for f in findings if f["rule_id"] == policy["id"] and f.get("person_id") in (person_id, None)]
                    status = ("broken" if any(f["severity"] == "error" for f in hits) else "warning" if hits
                              else "passes" if fresh else "unchecked")
                    rows.append(row(policy, person_id, start, end, status, hits))
                # Rules that would apply here but another rule replaces for this person.
                for policy in plain:
                    if policy["id"] in winners and not any(r["id"] == policy["id"] for r in rows) and applies(policy, person_id, start, end):
                        by = [{"id": o["id"], "name": o["name"]} for o in winners[policy["id"]]
                              if within_dates(o, start, end, data["organisation"]["timezone"]) and scope_matches_throughout(data, o['definition']['scope'], person_id, None, start, end)]
                        rows.append({**row(policy, person_id, start, end, "replaced"), "replaced_by": by})
                order = {"broken": 0, "warning": 1, "unchecked": 2, "passes": 3, "replaced": 4}
                return sorted(rows, key=lambda r: (order[r["status"]], r["name"]))

            people = [{"person_id": p["person_id"], "rules": rules_for(p["person_id"], aware(p["start"]), aware(p["end"]))}
                      for p in shift["participants"]]
            return {"fresh": fresh, "checked": bool(validation), "people": people, "active_rules": len({p["id"] for p in policies})}

    @app.get("/api/workspaces/{workspace_id}/rule-map", response_model=s.RuleMapResponse)
    def workspace_rule_map(workspace_id: str, request: Request):
        """Which rules touch which sessions, people and days of this rota, and the marks that explain them
        (own numbers, overrides, exceptions, this rota's changes). Unchanged rules, people and sessions: 304."""
        from packages.domain.rule_map import rule_map, short_days
        from packages.domain.rota_summary import normalized_check
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator", "approver", "programme_lead", "auditor")
            from .services.conditional_reads import conditional_response
            from .services.workspace_reads import rule_map_etag
            workspace = scoped(db, m.Workspace, workspace_id, actor, options=(
                defer(m.Workspace.validation), defer(m.Workspace.review_snapshot)))
            tag = rule_map_etag(db, actor, workspace)
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            db.refresh(workspace, attribute_names=("validation",))
            validation = normalized_check(workspace.validation) or {}
            # A stored check is compared with its dirty hint only.  Rebuilding a full
            # scheduling snapshot just to label a cached map as fresh made the first
            # rules read pay the whole rota cost before the map cache could help.
            from .snapshots import cheap_fingerprints
            from .services.workspace_reads import check_projection
            check = check_projection(workspace, workspace.validation)
            current = (cheap_fingerprints(db, [workspace], checks={workspace.id: check}).get(workspace.id)
                       if validation else None)
            began = time.perf_counter()
            adjusted = [r["policy_id"] for r in workspace.rule_adjustments or []]
            originals = {p.id: record(p) for p in db.scalars(select(m.Policy).where(m.Policy.id.in_(adjusted)))} if adjusted else {}
            from .services.read_projections import cached as cached_projection
            prepared = {}
            def build_map(_):
                data = domain_snapshot(db, workspace, include_fairness=False, mandatory_rules=False, rule_map_bindings=True)
                prepared['data'] = data
                return rule_map(data, originals)
            result = cached_projection(db, [workspace.id], 'rule_map', {workspace.id: tag},
                                       build_map)[workspace.id]
            # short_days only needs the rota's date window.  On a projection hit
            # this avoids loading the full snapshot altogether.
            data = prepared.get('data') or {'workspace': {'starts_on': workspace.starts_on},
                                            'organisation': {'timezone': db.get(m.Organisation, actor.organisation_id).timezone}}
            fresh = bool(validation) and validation.get("fingerprint") == current
            aliases = {source_id: rule['id'] for rule in result['rules']
                       for source_id in rule.pop('_display_source_ids', [])}
            findings = [{**finding, 'rule_id': aliases.get(finding.get('rule_id'), finding.get('rule_id'))}
                        for finding in validation.get('findings', [])]
            short = short_days(result, data, findings)
            counts = {}
            for f in findings:
                if f.get("rule_id"):
                    c = counts.setdefault(f["rule_id"], {"errors": 0, "warnings": 0})
                    c["errors" if f["severity"] == "error" else "warnings"] += 1
            from .services.people import visible_identity_names
            identity_ids = {rule["adjusted"].get("by") for rule in result["rules"] if rule.get("adjusted")}
            identity_ids.update(marker.get("by") for marker in result["markers"])
            from .services.policies import policies_with_status
            from .services.independence import active_permissions, covers, department_holders
            proposed_rows = policies_with_status(db, actor, 'proposed')
            proposed_by_replacement, proposed_by_catalogue = {}, {}
            for proposed in proposed_rows:
                definition = proposed.definition or {}
                replaces = definition.get('replaces_id')
                if replaces:
                    proposed_by_replacement.setdefault(replaces, []).append(proposed)
                rule = definition.get('rule') or {}
                if proposed.kind == 'catalogue' and rule.get('id'):
                    proposed_by_catalogue.setdefault(rule['id'], []).append(proposed)
            proposal_rows = {}
            for rule in result["rules"]:
                candidates = (proposed_by_catalogue.get(rule.get('catalogue_id'), ())
                              if rule.get('catalogue_id') else proposed_by_replacement.get(rule['id'], ()))
                if candidates:
                    proposal_rows[rule['id']] = max(candidates, key=lambda row: (row.created_at, row.id))
            identity_ids.update(row.created_by for row in proposal_rows.values())
            names = visible_identity_names(db, actor, identity_ids)
            holders = department_holders(db, actor.department_id) if proposal_rows else {}
            permissions = active_permissions(db, actor.department_id) if proposal_rows else []

            def self_approval_allowed(identity_id):
                held = holders.get(identity_id)
                return bool(held and any('rule.activate' in permission.actions and
                                         covers(permission, identity_id, held[0])
                                         for permission in permissions))

            for rule in result["rules"]:
                c = counts.get(rule["id"], {"errors": 0, "warnings": 0})
                state = "unchecked" if not fresh else "breach" if c["errors"] else "warning" if c["warnings"] else "met"
                rule["status"] = {"state": state, **c}
                rule["short_days"] = short.get(rule["id"], [])
                if rule["adjusted"]:
                    rule["adjusted"]["by_name"] = names.get(rule["adjusted"].get("by"))
                proposal = proposal_rows.get(rule["id"])
                rule["proposal"] = ({"policy_id": proposal.id, "by_name": names.get(proposal.created_by),
                                     "at": proposal.created_at.isoformat(),
                                     "self_approval_allowed": self_approval_allowed(proposal.created_by)}
                                    if proposal else None)
            for marker in result["markers"]:
                marker["by_name"] = names.get(marker.get("by"))
            return JSONResponse({**result, "checked": bool(validation), "fresh": fresh,
                                 "elapsed_ms": round((time.perf_counter() - began) * 1000)}, headers={"ETag": tag})

    @app.get("/api/workspaces/{workspace_id}/rules-explained", response_model=s.RulesExplainedResponse)
    def workspace_rules_explained(workspace_id: str, request: Request,
                                   who: str | None = Query(None, max_length=512),
                                   where: str | None = Query(None, max_length=512)):
        """Explain the effective rules for a rota using the checker's active-rule resolver.

        ``who`` is the picker value (``person:<id>`` or ``rank:<category id>``); the
        domain layer also accepts comma-separated values for report generation.
        """
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator", "approver", "programme_lead", "auditor")
            workspace = scoped(db, m.Workspace, workspace_id, actor, options=(
                defer(m.Workspace.validation), defer(m.Workspace.review_snapshot)))
            data = domain_snapshot(db, workspace, include_fairness=False, include_objectives=False)
            from packages.domain.rule_explainer import explain_rules
            from .services.rule_catalogue import explained_conflicts
            return {**explain_rules(data, who=who, where=where),
                    'conflicts': explained_conflicts(data)}

    @app.get("/api/rules-explained", response_model=s.DepartmentRulesExplainedResponse)
    def department_rules_explained(request: Request, group_by: Literal['question'] = 'question',
                                   at: s.date | None = Query(None), workspace_id: s.ID | None = Query(None),
                                   baseline_id: s.ID | None = Query(None)):
        """Explain department rules for a date without inventing a rota status."""
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_permission(actor, 'rules.read')
            require(baseline_id is None or workspace_id is not None, 'TARGET_REQUIRED', 'Choose a draft', 422)
            from .services import rule_catalogue
            day = at or rule_catalogue.independence.today(db, actor.department_id)
            return rule_catalogue.department_rules_explained(db, actor, day, workspace_id=workspace_id,
                                                           baseline_id=baseline_id)

    @app.get('/api/workspaces/{workspace_id}/parts', response_model=s.WorkspacePartPage)
    def get_workspace_part(workspace_id: str, request: Request, response: Response,
                          query: s.WorkspacePartQuery = Depends()):
        from .services.conditional_reads import conditional_json, conditional_response, revision_etag
        from .services.workspace_reads import fast_etag
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'coordinator', 'approver', 'programme_lead', 'auditor')
            workspace = scoped(db, m.Workspace, workspace_id, actor, options=(
                defer(m.Workspace.validation), defer(m.Workspace.review_snapshot)))
            tag = revision_etag("workspace-part", fast_etag(db, actor, workspace), query.model_dump(mode="json"))
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            return conditional_json(request, workspace_detail.part(db, actor, workspace, query, settings), actor, tag=tag)

    @app.get('/api/workspaces/{workspace_id}/baseline')
    def get_workspace_baseline(workspace_id: str, request: Request):
        from .workspace_baselines import latest as latest_baseline, projection as baseline_projection
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'coordinator', 'approver', 'programme_lead', 'auditor')
            workspace = scoped(db, m.Workspace, workspace_id, actor)
            baseline = latest_baseline(db, workspace.id)
            require(baseline is not None, 'BASELINE_MISSING', 'This draft has no saved baseline', 404)
            return {'workspace_id': workspace.id, 'revision': workspace.revision,
                    'baseline': baseline_projection(baseline), 'snapshot_hash': baseline.payload_hash}

    @app.get('/api/workspaces/{workspace_id}/baseline/refresh-preview')
    def get_workspace_baseline_refresh_preview(workspace_id: str, request: Request):
        from .workspace_baselines import refresh_preview
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'coordinator')
            return refresh_preview(db, actor, s.Command(kind='workspace.baseline.refresh.preview',
                                                        target_id=workspace_id, payload={}))

    @app.get("/api/workspaces/{workspace_id}/review")
    @app.get("/api/workspaces/{workspace_id}")
    def get_workspace(workspace_id: str, request: Request, response: Response, view: s.WorkspaceDetailView = 'full'):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator", "approver", "programme_lead", "auditor")
            if view == 'compact':
                from .services.conditional_reads import conditional_json, conditional_response, revision_etag
                from .services.workspace_reads import fast_etag
                workspace = scoped(db, m.Workspace, workspace_id, actor, options=(
                    defer(m.Workspace.validation), defer(m.Workspace.review_snapshot)))
                tag = revision_etag("workspace-compact", fast_etag(db, actor, workspace, include_job_state=True))
                cached = conditional_response(request, tag)
                if isinstance(cached, Response):
                    return cached
                response.headers.update(cached)
                return conditional_json(request, s.WorkspaceCompactDetail.model_validate(workspace_detail.compact(db, actor, workspace)), actor,
                                        tag=tag)
            from .services.conditional_reads import conditional_response, revision_etag
            from .services.workspace_reads import fast_etag
            workspace = scoped(db, m.Workspace, workspace_id, actor, options=(
                defer(m.Workspace.validation), defer(m.Workspace.review_snapshot)))
            fast_tag = fast_etag(db, actor, workspace, include_job_state=True)
            tag = revision_etag("workspace-schedule", fast_tag) if view == "schedule" else fast_tag
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            # The validator above reads only scalar metadata. Hydrate the large JSON
            # columns only for a 200 response that will actually project them.
            db.refresh(workspace, attribute_names=("validation", "review_snapshot"))
            # Keep the weak identity-map entry alive across fingerprint, wishes and privacy checks.
            department = db.get(m.Department, actor.department_id)
            org = db.get(m.Organisation, actor.organisation_id)
            publication_id = db.scalar(select(m.Publication.id).where(
                m.Publication.department_id == actor.department_id, m.Publication.workspace_id == workspace.id))
            historical = db.scalar(select(m.AuditEvent.id).where(
                m.AuditEvent.department_id == actor.department_id, m.AuditEvent.target_id == workspace.id,
                m.AuditEvent.action.in_({'workspace.submit', 'workspace.publish'})).limit(1)) is not None
            from .snapshots import fingerprints as read_fingerprints
            current_fingerprint = read_fingerprints(db, [workspace])[workspace.id]
            from .services.common import visible_rota_name
            from .services.planning_groups import unrestricted
            from .services.workspace_access import access_record
            from .services.rota_summary import workspace_validation, current_check
            from .workspace_baselines import attach as attach_baseline
            can_undo, can_redo = edit_availability(db, actor, workspace) if actor.permits("coordinator") else (False, False)
            data = {"workspace": {**attach_baseline(db, workspace, record(workspace)),
                                  "can_undo": can_undo,
                                  "name": visible_rota_name(db, actor, workspace.id, workspace.name),
                                  "publication_id": publication_id,
                                  "allowed_actions": allowed_actions(db, actor, workspace, historical=historical,
                                                                      published=publication_id is not None)},
                    "current_input_revision": org.input_revision, "current_availability_revision": org.availability_revision,
                    "current_fingerprint": current_fingerprint,
                    "review_progress": reviews.viewer_progress(db, actor, workspace, current_fingerprint=current_fingerprint),
                    # The viewer is a contributor (any edit, swap party or alias): approving would be self-approval.
                    "reviewer_involved": reviews.contributed(db, workspace, actor.identity_id),
                    # SW3-A-10: on the period's integrated rota only its Rota Coordinator adds or removes a fixed assignment.
                    "can_change_fixed": actor.permits("coordinator") and unrestricted(db, actor, access_record(db, workspace)),
                    # SW3-A-06: the viewer's own Undo can be redone (the planner then offers Redo beside Undo).
                    "can_redo": can_redo,
                    "swap_id": db.scalar(select(m.SwapRequest.id).where(m.SwapRequest.workspace_id == workspace.id)),
                    "approvals": [record(a) for a in db.scalars(select(m.Approval).where(m.Approval.workspace_id == workspace.id))]}
            from .services.people import with_adjustment_names
            data['workspace'] = with_adjustment_names(db, actor, data['workspace'])
            data['workspace']['validation'] = workspace_validation(db, workspace)
            data['workspace']['validation_current'] = current_check(db, workspace, current_fingerprint)
            fork = db.scalar(select(m.WorkspaceForkBase).where(m.WorkspaceForkBase.workspace_id == workspace.id))
            if fork is not None:
                parent = db.get(m.Workspace, fork.source_workspace_id)
                data['workspace']['fork_base'] = {'id': fork.id, 'source_workspace_id': fork.source_workspace_id,
                    'source_revision': fork.source_revision, 'source_content_revision': fork.source_content_revision,
                    'parent_changed_since': max(0, (parent.revision if parent else fork.source_revision) - fork.source_revision)}
            from .services.acknowledged_gaps import review_items
            data['review_items'] = review_items(workspace)
            if data['workspace'].get('purpose') == 'integrated':
                from .services.rota_summary import workspace_summary
                combined = workspace_summary(db, actor, workspace)['fix_list']
                data['planning_context'] = combined['planning_context']
                data['review_items'] = combined['review_items']
                data['linked_summary'] = combined['planning_context']['held']
            # Staff wishes on the rota's days (markers in the planner); they change without the rota changing.
            from .services.wishes import planner_effects, snapshot_wishes
            from .services.published_changes import review_payload
            canonical = db.info.get('read_snapshots', {}).get(workspace.id)
            use_canonical = canonical is not None and workspace.status == 'draft'
            wishes = (canonical['wishes'] if use_canonical else snapshot_wishes(db, [workspace.department_id], workspace))
            from .snapshots import project_series_shifts
            from .services.series_conflicts import session_actions, finding_actions
            external_context = []
            if use_canonical:
                focus = set(canonical.get("focus_ids") or ())
                own_shifts = [row for row in canonical.get("shifts", ()) if row.get("id") in focus]
            else:
                own_shifts = workspace_detail.full_shifts(db, workspace.id)
            projected = project_series_shifts(db, workspace, own_shifts, external_out=external_context if wishes else None)
            # A canonical check may use a narrower set of sessions than the review diff.
            data.update(review_payload(db, workspace, actor, projected_shifts=None if use_canonical else projected))
            data["wishes"] = ([{k: v for k, v in row.items() if k != 'outcome_reasons'} for row in wishes]
                              if workspace.status == 'draft' else wishes)
            data["shifts"] = projected
            from .services.people import duty_ranks
            data['shifts'] = duty_ranks(db, actor.department_id, data['shifts'], org.timezone)
            data['shifts'] = session_actions(db, actor, data['shifts'])
            data['workspace']['validation'] = finding_actions(db, actor, data['workspace'].get('validation'))
            external = []
            if data["wishes"]:
                if external_context:
                    external = external_context[0]
                else:
                    from .snapshots import external_schedule, shift_records
                    from packages.domain.time import local_midnight
                    lo, hi = local_midnight(workspace.starts_on, org.timezone), local_midnight(workspace.ends_on, org.timezone)
                    department_ids = list(db.scalars(select(m.Department.id).where(m.Department.organisation_id == org.id)))
                    publications, query = external_schedule(db, workspace, department_ids, lo, hi, org.timezone)
                    external = shift_records(db, db.scalars(query)) if publications else []
            planner_effects(data, org.timezone, external_shifts=external)
            disclose_leave(db, actor, data["workspace"].get("validation"))
        if view == "schedule":
            from .serialization import schedule_projection
            data = schedule_projection(data)
        result = JSONResponse(projection_json_value(data))
        result.headers.update(cached)
        return result

    @app.get("/api/publications")
    def publications(request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "staff", *s.PERMISSIONS['publications.read_team'])
            # Only the name and report availability are read from each snapshot (megabytes for big rotas); staff see the ones they are on,
            # found through the publication_people index rather than by reading every snapshot.
            query = select(m.Publication, m.Publication.snapshot["workspace"]["name"].as_string(),
                           m.Publication.snapshot["quota_report"].as_string().is_not(None)).where(
                m.Publication.department_id == actor.department_id).options(
                defer(m.Publication.snapshot), defer(m.Publication.validation)).order_by(m.Publication.sequence.desc()).limit(100)
            if not actor.permits(*s.PERMISSIONS['publications.read_team']):
                query = query.join(m.Workspace, m.Workspace.id == m.Publication.workspace_id).where(
                    m.Workspace.archived_at.is_(None), m.Publication.id.in_(select(m.PublicationPerson.publication_id).where(
                    m.PublicationPerson.person_id == (actor.person_id or ""))))
            rows = list(db.execute(query))
            from .services.common import rota_names
            names = rota_names(db, actor.roles, actor.person_id, {p.workspace_id: n for p, n, _ in rows})
            return {"publications": [{**record(p, exclude=("snapshot", "validation",)), "name": names[p.workspace_id],
                                      "has_quota_report": has_quotas} for p, n, has_quotas in rows]}

    def public_projection(actor, publication, db):
        owner_workspace = db.get(m.Workspace, publication.workspace_id)
        if owner_workspace is not None and owner_workspace.archived_at is not None:
            require(actor.permits(*s.PERMISSIONS['publications.read_team']), 'NOT_FOUND', 'Publication not found', 404)
        # The rota as in force: days a later rota replaced it from are left out (the stored snapshot is unchanged).
        data = copy.deepcopy({key: value for key, value in in_force_snapshot(db, publication).items()
                              if key != 'fairness_qualification'})
        from .auth import can_read_rules
        if not can_read_rules(actor) and 'leadership' in data:
            data['leadership']['policies'] = []
        from .services.people import visible_person
        data['people'] = {pid: visible_person(actor, person) for pid, person in data.get('people', {}).items()}
        if data.get("workspace", {}).get("name"):
            from .services.common import visible_rota_name
            data["workspace"]["name"] = visible_rota_name(db, actor, publication.workspace_id, data["workspace"]["name"])
        if not actor.permits(*s.PERMISSIONS['publications.read_team']):
            data.pop('wish_worked', None)
            if 'overtime_offered_intervals' in data:
                data['overtime_offered_intervals'] = {actor.person_id: data['overtime_offered_intervals'].get(actor.person_id, [])}
            if 'wishes' in data:
                from .services.wishes import outcome_reasons
                data['wishes'] = [{**wish, 'outcome_reasons': outcome_reasons(actor, wish.get('outcome_reasons'))}
                                  for wish in data['wishes'] if wish['person_id'] == actor.person_id]
            if "quota_report" in data:
                from packages.domain.quotas import project_report
                data["quota_report"] = project_report(data["quota_report"], actor.person_id)
            data.pop("resource_pools", None)
            data.pop("operating_calendars", None)
            data["workspace"].pop("urgency_reason", None)
            data["workspace"].pop("review_snapshot", None)
            data['workspace'].pop('target_overrides', None)
            data['workspace'].pop('accepted_issues', None)
            data["shifts"] = [sh for sh in data["shifts"] if any(p["person_id"] == actor.person_id for p in sh["participants"])]
            require(bool(data["shifts"]), "NOT_FOUND", "Publication not found", 404)
            from apps.api.services.schedule_view import staff_shift
            data['shifts'] = [staff_shift(shift, actor.person_id) for shift in data['shifts']]
            shown = {p["person_id"] for sh in data["shifts"] for p in sh["participants"]}
            data["people"] = {pid: row for pid, row in data["people"].items() if pid in shown}
            visible_locations = {sh["location_id"] for sh in data["shifts"]}
            visible_services = {sid for sh in data["shifts"] for sid in sh["service_ids"]}
            data["locations"] = {key: row for key, row in data["locations"].items() if key in visible_locations}
            data["services"] = {key: row for key, row in data["services"].items() if key in visible_services}
        from .services.people import duty_ranks
        data['shifts'] = duty_ranks(db, actor.department_id, data['shifts'], data['timezone'])
        return data

    @app.get("/api/publications/{publication_id}")
    def get_publication(publication_id: str, request: Request, response: Response):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "staff", *s.PERMISSIONS['publications.read_team'])
            pub = scoped(db, m.Publication, publication_id, actor)
            data = public_projection(actor, pub, db)
            from .services.schedule_view import publication_session_flags
            data['shifts'] = publication_session_flags(db, actor, pub, data['shifts'],
                data.get('timezone') or db.get(m.Organisation, actor.organisation_id).timezone)
            # Qualification evidence is retained in the immutable publication for audit,
            # but is not part of the read contract used by the schedule UI.  It can be
            # hundreds of times larger than the sessions themselves.
            data.pop('fairness_qualification', None)
            data.get('workspace', {}).pop('fairness_qualification', None)
            from .services.published_operational import project
            operational = project(db, actor, [pub], [{**row, 'publication_id': pub.id} for row in data['shifts']],
                data.get('timezone') or db.get(m.Organisation, actor.organisation_id).timezone,
                pub.starts_on, pub.effective_until or pub.ends_on, flagged=True)[pub.id]
            from packages.domain.rota_summary import rota_summary
            body = {"operational": operational, "publication": record(pub, exclude=("snapshot", "validation")), "snapshot": data,
                    **({'summary': rota_summary(pub.validation)['issued_counts']}
                       if actor.permits(*s.PERMISSIONS['publications.read_team']) else {}),
                    "original_validation": pub.validation if actor.permits(*s.PERMISSIONS['publications.read_team']) else {"valid_at_publication": pub.validation["valid"]}}
            # as_of is a read timestamp; all meaningful operational changes are fingerprinted.
            tag = 'W/"' + hashlib.sha256(json.dumps(
                {**body, 'operational': {key: value for key, value in operational.items() if key != 'as_of'}},
                default=str, sort_keys=True).encode()).hexdigest()[:32] + '"'
            if request.headers.get('if-none-match') == tag:
                return Response(status_code=304, headers={'ETag': tag})
            response.headers['ETag'] = tag
            return body

    @app.get('/api/publications/{publication_id}/pending-changes')
    def pending_publication_changes(publication_id: s.ID, request: Request):
        from .services.published_changes import pending
        with transaction(engine) as db:
            return pending(db, actor_for(request, db), publication_id)

    @app.get('/api/absence-report-settings')
    def absence_report_settings(request: Request):
        from .services.published_changes import report_settings
        with transaction(engine) as db:
            return report_settings(db, actor_for(request, db))

    @app.get("/api/publications/{publication_id}/changes")
    def publication_changes(publication_id: str, request: Request):
        """Days an amendment changed compared with the rota it replaced (for the rota calendar)."""
        from zoneinfo import ZoneInfo
        from packages.domain.time import aware
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_permission(actor, 'publications.read_team')
            pub = scoped(db, m.Publication, publication_id, actor)
            previous = db.get(m.Publication, pub.supersedes_id) if pub.supersedes_id else None
            if previous is None:
                return {"publication_id": pub.id, "supersedes_id": None, "days": []}
            zone = ZoneInfo(pub.snapshot["timezone"]) if pub.snapshot.get("timezone") else ZoneInfo(db.get(m.Organisation, db.get(m.Department, pub.department_id).organisation_id).timezone)

            def sessions(snapshot):
                rows = {}
                for sh in snapshot["shifts"]:
                    key = (sh["assignment_type_id"], sh["location_id"], aware(sh["start"]).isoformat(), aware(sh["end"]).isoformat())
                    rows.setdefault(key, []).append((bool(sh.get("cancelled") or sh.get('closed')), tuple(sorted(p["person_id"] for p in sh["participants"]))))
                return rows
            before, after = sessions(in_force_snapshot(db, previous)), sessions(in_force_snapshot(db, pub))
            days = {}
            for key in before.keys() | after.keys():
                if sorted(before.get(key, [])) == sorted(after.get(key, [])):
                    continue
                day = aware(key[2]).astimezone(zone).date().isoformat()
                kind = "added" if key not in before else "removed" if key not in after else "changed"
                days.setdefault(day, {"date": day, "added": 0, "removed": 0, "changed": 0})[kind] += 1
            return {"publication_id": pub.id, "supersedes_id": previous.id, "days": sorted(days.values(), key=lambda d: d["date"])}

    @app.get("/api/publications/{publication_id}/coverage")
    def coverage(publication_id: str, request: Request, response: Response):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "staff", *s.PERMISSIONS['publications.read_team'])
            pub = scoped(db, m.Publication, publication_id, actor, options=(
                defer(m.Publication.snapshot), defer(m.Publication.validation)))
            org = db.get(m.Organisation, actor.organisation_id)
            department = db.get(m.Department, actor.department_id)
            workspace = db.get(m.Workspace, pub.workspace_id, options=(
                defer(m.Workspace.validation), defer(m.Workspace.review_snapshot)))
            from .services.conditional_reads import conditional_response
            from .services.workspace_reads import publication_coverage_etag
            tag = publication_coverage_etag(db, actor, pub, workspace, org, department)
            cached_response = conditional_response(request, tag)
            if isinstance(cached_response, Response):
                return cached_response
            response.headers.update(cached_response)
            db.refresh(pub, attribute_names=("snapshot", "validation"))
            own_only = not actor.permits(*s.PERMISSIONS['publications.read_team'])
            if own_only:
                visible = {sh["id"] for sh in schedule.in_force_shifts(
                    pub, pub.snapshot.get("shifts", []), pub.snapshot.get("timezone") or org.timezone)
                           if any(p["person_id"] == actor.person_id for p in sh["participants"])}
                require(bool(visible), "NOT_FOUND", "Publication not found", 404)
            from .services.read_projections import cached, input_stamp
            from packages.domain.input_manifest import digest
            stamp = digest([input_stamp(db, workspace), pub.id, pub.revision, json_value(pub.effective_until),
                            org.revision, department.revision])
            result = dict(cached(db, [workspace.id], 'publication_coverage', {workspace.id: stamp},
                lambda _: evaluate(domain_snapshot(db, workspace, operational=True)))[workspace.id])
            if own_only:
                result["coverage"] = [r for r in result["coverage"] if r["shift_id"] in visible]
                # Rule findings are operator information, including on the staff member's own published duties.
                result["findings"] = []
                result["error_count"] = result["warning_count"] = 0
                if "quota_report" in result:
                    from packages.domain.quotas import project_report
                    result["quota_report"] = project_report(result["quota_report"], actor.person_id)
                from .services.fairness import personal_goals
                result['goals'] = personal_goals(result.get('goals', []), actor.person_id)
                result["valid"] = result["error_count"] == 0
            org = db.get(m.Organisation, actor.organisation_id)
            return {"evaluation": result, "publication_id": pub.id, "availability_revision": org.availability_revision,
                    "as_of": m.now().isoformat(),
                    "basis": f"As published, re-checked against {s.LIVE_RECHECK_BASIS}. Not attendance."}

    @app.get("/api/reports/{publication_id}")
    def report(publication_id: str, request: Request, format: str = "json", person_id: str | None = None, grouping: str | None = None,
               layout: str = 'week', on: s.date | None = None, service_id: str | None = None,
               service_ids: list[str] | None = Query(None, max_length=32)):
        enforce(engine, "exports", [session_identity(engine, request.cookies.get(cookie_name))], settings.rate_exports_per_minute, 60,
                "Too many reports or exports in a short time; wait a minute and try again")
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "staff", *s.PERMISSIONS['publications.read_team'])
            pub = scoped(db, m.Publication, publication_id, actor)
            projection = public_projection(actor, pub, db)
            from packages.domain.rota_summary import normalized_check
            issued_score = ((normalized_check(pub.validation) or {}).get('score') or {}).get('score')
            own_only = not actor.permits(*s.PERMISSIONS['publications.read_team'])
            if own_only:
                require(person_id in {None, actor.person_id}, "FORBIDDEN", "Personal exports are limited to your identity binding", 403)
                person_id = actor.person_id
            if person_id:
                require(person_id in projection["people"], "NOT_FOUND", "Person not found in this publication", 404)
            if format == 'print':
                from packages.domain.reports import schedule_print
                require(layout in {'day', 'week', 'month'}, 'PRINT_LAYOUT', 'Choose day, week or month')
                require(not service_id or service_id in projection['services'], 'NOT_FOUND', 'Service not found in this publication', 404)
                require(all(sid in projection['services'] for sid in service_ids or []), 'NOT_FOUND', 'Service not found in this publication', 404)
                # The department-level score is for planners; personal prints leave it out.
                score = issued_score if not own_only and not person_id else None
                return Response(schedule_print(projection, pub.sequence, pub.current, layout, on, person_id, service_id, service_ids, score,
                                               stylesheet_url=asset_url('print.css', WEB)),
                                media_type='text/html')
            require(grouping in {None, 'week', 'month'}, 'REPORT_GROUPING', 'Choose week or month grouping')
            if grouping:
                from packages.domain.reports import workload_breakdown
                result = workload_breakdown(projection, person_id, grouping)
            else:
                result = workload(projection, person_id)
            if actor.permits("coordinator", "approver", "programme_lead", "auditor") and not person_id:
                from packages.domain.resources import resource_report
                result["resources"] = resource_report(projection)
            result.update({"publication_id": pub.id, "publication_sequence": pub.sequence, "filters": {"person_id": person_id}, "current": pub.current})
            if not own_only and not person_id and issued_score is not None:
                result["schedule_score"] = issued_score
            if format == "json":
                return result
            headers = {"Content-Disposition": f'attachment; filename="rota-publication-{pub.sequence}.{ "ics" if format == "ics" else "csv"}"'}
            if format == "csv":
                return Response(report_csv(result, pub.id, pub.sequence), media_type="text/csv", headers=headers)
            if format == "quotas":
                from packages.domain.quotas import quota_csv
                require("quotas" in result, "QUOTA_REPORT_NOT_ISSUED", "This publication has no issued block-quota report", 404)
                return Response(quota_csv(result["quotas"], pub.id, pub.sequence), media_type="text/csv", headers=headers)
            if format == "assignments":
                return Response(assignments_csv(projection, pub.id, pub.sequence, person_id), media_type="text/csv", headers=headers)
            if format == "ics":
                require(bool(person_id), "PERSON_REQUIRED", "Choose a person for a calendar export")
                department = db.get(m.Department, pub.department_id)
                return Response(calendar(projection, pub.id, pub.sequence, person_id,
                                         calendar_name=f"{department.name} rota"),
                                media_type="text/calendar", headers=headers)
            raise Problem(422, "FORMAT_UNSUPPORTED", "Supported formats are json, csv, assignments, quotas, ics and print")

    @app.get("/api/calendar-feed")
    def calendar_feed_status(request: Request):
        from .services.calendar_feeds import active_feed
        with transaction(engine) as db:
            actor = actor_for(request, db)
            feed = active_feed(db, actor)
            return {"linked": bool(actor.person_id), "active": bool(feed), "created_at": feed.created_at.isoformat() if feed else None,
                    "revision": feed.revision if feed else None, "hidden_event_type_ids": feed.hidden_event_type_ids if feed else [],
                    "last_used_at": feed.last_used_at.isoformat() if feed and feed.last_used_at else None}

    @app.get("/calendar/{token}.ics")
    def calendar_subscription(token: str, request: Request):
        """Read by calendar apps, which cannot sign in: the private link itself is the permission."""
        from .services.calendar_feeds import feed_calendar
        message = "This calendar link is being refreshed too often; your calendar app will retry later"
        # Address first: guessed tokens from one address stop before each adds its own counter row.
        enforce(engine, "feed-address", [peer_key(request)], settings.rate_feed_address_per_hour, 3600, message)
        enforce(engine, "feed", [digest(token)], settings.rate_feed_per_hour, 3600, message)
        with transaction(engine, write=True) as db:
            body = feed_calendar(db, token).encode("utf-8")
        # Hash the actual representation, including the calendar name and stable publication timestamps.
        tag = '"' + hashlib.sha256(body).hexdigest() + '"'
        headers = {"ETag": tag, "Cache-Control": "private, no-cache"}
        known = ",".join(request.headers.getlist("if-none-match"))
        if known.strip() == "*" or any(value.strip().removeprefix("W/") == tag for value in known.split(",")):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="text/calendar", headers=headers)

    @app.get("/api/requests/overtime-eligibility")
    def overtime_eligibility(request: Request, starts_on: date, ends_on: date, person_id: s.ID | None = None):
        from .services import overtime_availability
        with transaction(engine) as db:
            return overtime_availability.eligibility(db, actor_for(request, db), starts_on, ends_on, person_id)

    @app.get("/api/requests")
    def requests_list(request: Request, status: str | None = Query(None, pattern="^(submitted|approved|rejected|withdrawn)$")):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            return availability.list_requests(db, actor, status)

    @app.get("/api/requests/all")
    def requests_all(request: Request, response: Response, type: str | None = Query(None, pattern="^(leave|weekly|wish|swap|other)(,(leave|weekly|wish|swap|other))*$"),
                     status: str | None = Query(None, pattern="^(waiting|reply|accepted|open|approved|declined|withdrawn|expired)(,(waiting|reply|accepted|open|approved|declined|withdrawn|expired))*$"),
                     person_id: str | None = None, id: str | None = None,
                     limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000),
                     q: str | None = Query(None, max_length=200), person_ids: str | None = Query(None, max_length=20000),
                     sort_by: s.RequestSortField | None = None,
                     sort_direction: str = Query("asc", pattern="^(asc|desc)$"),
                      decided: bool = False, for_me: bool = False,
                     collection_filter: s.CollectionFilter = Depends(parse_collection_filter("requests"))):
        """The Requests page: leave, unavailability, weekly patterns, preferences and swaps in one paged list (or one with its history)."""
        people = person_ids.split(",") if person_ids else ()
        require(len(people) <= 500 and all(re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", pid) for pid in people),
                "INVALID_FILTER", "Choose up to 500 valid people", 422)
        from .services.conditional_reads import conditional_response
        with transaction(engine) as db:
            actor = actor_for(request, db)
            representation = [type or "", status or "", person_id, id, limit, offset, q, sorted(people),
                              sort_by, sort_direction, decided, for_me,
                              collection_filter.model_dump(mode="json") if collection_filter else None]
            if id:
                # History permissions changed, and dated programme authority can expire without a write.
                representation.append("request-history-v2")
                if actor.permits("programme_lead"):
                    representation.append(independence.today(db, actor.department_id).isoformat())
            tag = availability.requests_etag(db, actor, representation)
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            db.info['_request_parents'] = {}
            from .services.read_projections import cached_department
            return cached_department(
                db, actor, 'requests', tag,
                lambda: availability.list_all(
                    db, actor, (type or "").split(",") if type else (),
                    (status or "").split(",") if status else (), person_id, limit, offset, id, q, people,
                    sort_by=sort_by, sort_direction=sort_direction, collection_filter=collection_filter,
                    decided=decided, for_me=for_me),
                audience_values=('requests', representation))

    @app.get("/api/availability")
    def availability_list(request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "staff", "coordinator", "approver")
            query = select(m.Availability).where(m.Availability.department_id == actor.department_id,
                                                 availability.visible_record_clause())
            if not actor.permits("coordinator", "approver"):
                query = query.where(m.Availability.person_id == actor.person_id)
            from .services.leave_types import names
            return {"availability": names(db, actor.department_id,
                [record(r, exclude=("private_reason",)) for r in db.scalars(query.order_by(m.Availability.start.desc()).limit(500))])}

    @app.get("/api/fairness/goals")
    def fairness_goals(request: Request, response: Response, workspace_id: str | None = None, who: str | None = Query(None, max_length=20000),
                       starts_on: s.date | None = None, ends_on: s.date | None = None):
        from .services import fairness
        with transaction(engine) as db:
            actor = actor_for(request, db)
            from .services.conditional_reads import conditional_response
            validator = fairness.goals_etag(db, actor, workspace_id, who, starts_on, ends_on)
            cached = conditional_response(request, validator[0])
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            return fairness.goals(db, actor, workspace_id, who, starts_on, ends_on, validator=validator)

    @app.get("/api/fairness/exposure", response_model=s.FairnessExposure)
    def fairness_exposure(request: Request, response: Response, rows: str | None = Query(None, max_length=20000), cols: str | None = Query(None, max_length=20000),
                          pairing: str = "present", assignment_type_ids: list[str] = Query([]), service_ids: list[str] = Query([]),
                          starts_on: s.date | None = None, ends_on: s.date | None = None, location_ids: list[str] = Query([]), within: bool = True,
                          limit: int | None = Query(None, ge=1, le=500)):
        from .services import fairness
        with transaction(engine) as db:
            actor = actor_for(request, db)
            from .services.conditional_reads import conditional_response
            tag = fairness.exposure_etag(db, actor, rows, cols, pairing, assignment_type_ids,
                                         service_ids, starts_on, ends_on, location_ids, within, limit)
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            return fairness.exposure(db, actor, rows, cols, pairing, assignment_type_ids, service_ids, starts_on, ends_on,
                                     location_ids, within, limit)

    @app.get("/api/fairness/distribution")
    def fairness_distribution(request: Request, response: Response, metric: str = "duties", who: str | None = Query(None, max_length=20000),
                              assignment_type_ids: list[str] = Query([]), service_ids: list[str] = Query([]),
                              starts_on: s.date | None = None, ends_on: s.date | None = None, group_by: str | None = None,
                              location_ids: list[str] = Query([]), within: bool = True,
                              workspace_id: s.ID | None = None, goal_id: str | None = Query(None, max_length=200)):
        from .services import fairness
        with transaction(engine) as db:
            actor = actor_for(request, db)
            from .services.conditional_reads import conditional_response
            tag = fairness.distribution_etag(db, actor, metric, who, assignment_type_ids, service_ids,
                                             starts_on, ends_on, group_by, location_ids, within, workspace_id, goal_id)
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            return fairness.distribution(db, actor, metric, who, assignment_type_ids, service_ids, starts_on, ends_on, group_by,
                                         location_ids, within, workspace_id, goal_id)

    @app.get("/api/fairness/sessions")
    def fairness_sessions(request: Request, person_id: s.ID, other_person_id: s.ID | None = None, metric: str = "duties",
                          pairing: str = "present", assignment_type_ids: list[str] = Query([]), service_ids: list[str] = Query([]),
                          starts_on: s.date | None = None, ends_on: s.date | None = None,
                          month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), location_ids: list[str] = Query([]),
                          within: bool = True, workspace_id: s.ID | None = None, goal_id: str | None = Query(None, max_length=200)):
        from .services import fairness
        with transaction(engine) as db:
            return fairness.sessions(db, actor_for(request, db), person_id, other_person_id, metric, pairing, assignment_type_ids,
                                     service_ids, starts_on, ends_on, month, location_ids, within, workspace_id, goal_id)

    @app.post("/api/fairness/preview")
    def fairness_preview(body: dict, request: Request):
        from .services import fairness
        with transaction(engine) as db:
            return fairness.preview(db, actor_for(request, db), body)

    @app.get("/api/policies/capabilities")
    def policy_capabilities(request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            return capability_catalogue()

    @app.post("/api/policies/check")
    def policy_check(body: dict, request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            return check_policy(db, actor, body.get("definition", {}))

    @app.get("/api/policies/groups")
    def policy_groups(request: Request):
        """Near-duplicate per-person rules that can become rule templates (a preview; nothing is saved)."""
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            return duplicate_groups(db, actor)

    @app.get("/api/rules/catalogue")
    def rule_catalogue_page(request: Request, response: Response, version: int = Query(1, ge=1, le=2), include_hidden: bool = False,
                            day: s.date | None = None, workspace_id: s.ID | None = Query(None)):
        """The rule catalogue: every entry, its rule in force, the version waiting for approval (with its line diff),
        each line's reach today and its overlaps."""
        from .services import rule_catalogue
        from .services.conditional_reads import conditional_response
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            actual_day = day or rule_catalogue.independence.today(db, actor.department_id)
            if workspace_id is not None:
                return rule_catalogue.catalogue(db, actor, version, include_hidden, actual_day, workspace_id=workspace_id)
            stamp = rule_catalogue.catalogue_stamp(db, actor)
            tag = rule_catalogue.catalogue_etag(actor, version, include_hidden, actual_day, stamp)
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            result = rule_catalogue.catalogue(db, actor, version, include_hidden, actual_day, _stamp=stamp,
                                              build_wait_seconds=settings.catalogue_build_wait_seconds)
            if isinstance(result, dict) and result.get('state') == 'updating' and result.get('projection') == 'rule_catalogue':
                result = s.CatalogueUpdating.model_validate(result)
            if isinstance(result, s.CatalogueUpdating):
                return JSONResponse(result.model_dump(mode='json'), status_code=202,
                                    headers={**cached, 'Retry-After': str(result.retry_after_seconds)})
            return result

    @app.get("/api/people/{person_id}/scheduling-facts")
    def person_scheduling_facts(person_id: s.ID, request: Request, day: s.date | None = None):
        from .services import scheduling_facts
        with transaction(engine) as db:
            return scheduling_facts.read_person_facts(db, actor_for(request, db), person_id, on=day)

    @app.get('/api/timeline', response_model=s.TimelineResponse, response_model_exclude_unset=True)
    def timeline(request: Request, start: s.date, end: s.date,
                 person_ids: list[s.ID] | None = Query(None, max_length=200),
                 bucket: Literal['day', 'week', 'month'] | None = None,
                 source: str | None = Query(None, max_length=100), hide: str | None = Query(None, max_length=100),
                 hide_leave_types: str | None = Query(None, max_length=7500), workspace_id: s.ID | None = None,
                 eligible_workspace_id: s.ID | None = None,
                 layers: str | None = Query(None, max_length=400),
                 q: str | None = Query(None, max_length=160),
                 trainees_only: bool = False,
                 group_by: Literal['none', 'programme', 'rank', 'cohort'] = 'none',
                 sort_by: Literal['name', 'code'] = 'name', sort_direction: Literal['asc', 'desc'] = 'asc',
                 offset: int = Query(0, ge=0), limit: int = Query(150, ge=1, le=200),
                 collection_filter: s.CollectionFilter = Depends(parse_collection_filter('people'))):
        from .services.timeline import list_data
        with transaction(engine) as db:
            return list_data(db, actor_for(request, db), start, end, person_ids=person_ids, q=q,
                group_by=group_by, sort_by=sort_by, sort_direction=sort_direction,
                offset=offset, limit=limit, collection_filter=collection_filter, trainees_only=trainees_only, layers=layers,
                bucket=bucket, source=source, hide=hide, hide_leave_types=hide_leave_types, workspace_id=workspace_id,
                eligible_workspace_id=eligible_workspace_id)

    @app.get('/api/people/{person_id}/timeline', response_model=s.TimelineResponse, response_model_exclude_unset=True)
    def person_timeline(person_id: s.ID, request: Request, start: s.date, end: s.date,
                        bucket: Literal['day', 'week', 'month'] | None = None,
                        source: str | None = Query(None, max_length=100), hide: str | None = Query(None, max_length=100),
                        hide_leave_types: str | None = Query(None, max_length=7500), workspace_id: s.ID | None = None,
                        layers: str | None = Query(None, max_length=400),
                        collection_filter: s.CollectionFilter = Depends(parse_collection_filter('people'))):
        from .services.timeline import list_data
        with transaction(engine) as db:
            return list_data(db, actor_for(request, db), start, end, person_ids=[person_id],
                             limit=1, collection_filter=collection_filter, layers=layers,
                             bucket=bucket, source=source, hide=hide, hide_leave_types=hide_leave_types, workspace_id=workspace_id)

    @app.post('/api/timeline/preview', response_model=s.TimelinePreview)
    def timeline_preview(value: s.TimelineRotationInput, request: Request):
        from .services.timeline_commands import preview
        return retry_write(engine, lambda db: preview(db, actor_for(request, db, write=True), value))

    @app.get('/api/leave-balance-settings', response_model=s.LeaveBalanceSettings)
    def leave_balance_settings(request: Request):
        from .services import leave_balances
        with transaction(engine) as db:
            return leave_balances.settings(db, actor_for(request, db))

    @app.get('/api/my-leave-balances', response_model=list[s.LeaveBalance])
    def my_leave_balances(request: Request):
        from .services import leave_balances
        with transaction(engine) as db:
            actor = actor_for(request, db)
            return leave_balances.balances(db, actor, actor.person_id) if actor.person_id else []

    @app.get('/api/requests/{request_id}/leave-balances', response_model=list[s.LeaveBalance])
    def request_leave_balances(request_id: s.ID, request: Request):
        from .services import leave_balances
        with transaction(engine) as db:
            actor = actor_for(request, db)
            row = scoped(db, m.RequestRecord, request_id, actor)
            require(row.id in availability.readable_request_balance_ids(db, actor, [row.id]),
                    'FORBIDDEN', 'Only the person and permitted decision makers can read this balance', 403)
            return leave_balances.balances(db, actor, row.person_id, request=row) if row.kind == 'leave' else []

    @app.post("/api/rotation-board/reference-preview", response_model=s.ReferenceRotationReview)
    def reference_rotation_preview(value: s.ReferenceRotationImport, request: Request):
        from .services.training import reference_rotation_import
        with transaction(engine) as db:
            return reference_rotation_import(db, actor_for(request, db, write=True), value, preview=True)

    @app.get("/api/rotation-board", response_model=s.RotationBoard)
    def rotation_board(request: Request, start: s.date, end: s.date, programme_id: s.ID | None = None):
        from .services import training
        with transaction(engine) as db:
            return training.board(db, actor_for(request, db), start, end, programme_id)

    @app.get("/api/people/{person_id}/rotations", response_model=s.PersonRotations)
    def person_rotations(person_id: s.ID, request: Request, start: s.date, end: s.date):
        from .services import training
        with transaction(engine) as db:
            return training.person_rotations(db, actor_for(request, db), person_id, start, end)

    @app.get("/api/people/{person_id}/trainee-notes")
    def trainee_notes(person_id: s.ID, request: Request):
        from .services import training
        with transaction(engine) as db:
            return training.notes(db, actor_for(request, db), person_id)

    @app.get("/api/programme-calendar-entries/{entry_id}/protections")
    def calendar_protections(entry_id: s.ID, request: Request):
        from .services import scheduling_facts
        with transaction(engine) as db:
            return scheduling_facts.read_calendar_protections(db, actor_for(request, db), entry_id)

    @app.get("/api/rules/catalogue/migration")
    def rule_catalogue_migration(request: Request, proof: bool = False, version: int = Query(1, ge=1, le=2), background: bool = False,
                                 details: bool = False):
        """What moving the rules in force into the catalogue proposes and what stays as it is; with proof, the proof."""
        from .services import rule_catalogue
        with transaction(engine) as db:
            if proof:
                allow_long_idle(db, 30)
            actor = actor_for(request, db)
            if version == 2:
                from .services import rule_catalogue_v2
                preview = rule_catalogue_v2.plan(db, actor, proof=proof, background=background)
                return JSONResponse(preview if details else rule_catalogue_v2._preview_plan(preview))
            plan = rule_catalogue.migration_plan(db, actor)
            return {**plan, "proof": rule_catalogue.migration_proof(db, actor, plan) if proof else None}

    @app.get('/api/rules/catalogue/conversions')
    def catalogue_conversions(request: Request, include_history: bool = False):
        from .services import rule_catalogue_v2
        with transaction(engine) as db:
            return rule_catalogue_v2.read(db, actor_for(request, db), include_history=include_history)

    @app.get('/api/rules/catalogue/conversions/{conversion_id}')
    def catalogue_conversion(conversion_id: s.ID, request: Request, details: bool = False):
        from .services import rule_catalogue_v2
        with transaction(engine) as db:
            return JSONResponse(rule_catalogue_v2.read(db, actor_for(request, db), conversion_id, details=details))

    @app.post("/api/rules/catalogue/{catalogue_id}/check")
    def rule_catalogue_check(catalogue_id: str, body: dict, request: Request):
        """Duplicates, overlaps, warnings and issues of these lines, as you type (nothing is saved)."""
        from .services import rule_catalogue
        with transaction(engine) as db:
            return JSONResponse(rule_catalogue.check(db, actor_for(request, db), catalogue_id, body))

    @app.get('/api/rules/catalogue/{catalogue_id}/history')
    def rule_catalogue_history(catalogue_id: str, request: Request, version: int = Query(1, ge=1, le=2),
                               page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100)):
        from .services import rule_catalogue
        with transaction(engine) as db:
            return rule_catalogue.entry_history(db, actor_for(request, db), catalogue_id, version, page, limit)

    @app.get('/api/rules/catalogue/{catalogue_id}/detail')
    def rule_catalogue_detail(catalogue_id: str, request: Request, version: int = Query(1, ge=1, le=2),
                              day: s.date | None = None, policy_id: str | None = None, line_id: str | None = None,
                              workspace_id: str | None = None, starts_on: s.date | None = None, ends_on: s.date | None = None):
        from .services import rule_catalogue
        with transaction(engine) as db:
            return JSONResponse(rule_catalogue.detail(db, actor_for(request, db), catalogue_id, version, day, policy_id, line_id,
                                                      workspace_id, starts_on, ends_on))

    @app.get("/api/policies/applies")
    def policy_applies_to(request: Request, person_id: str, day: s.date | None = None):
        """Which rule applies to a person on a day, the number they get, and why."""
        from .services import independence
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            return rules_for_person(db, actor, person_id, day or independence.today(db, actor.department_id))

    @app.get("/api/policies/reach")
    def policy_reach_count(request: Request, day: s.date | None = None, workspace_id: str | None = None,
                           starts_on: s.date | None = None, ends_on: s.date | None = None, details: bool = False,
                           policy_id: str | None = None, line_id: str | None = None):
        """How many people each active or proposed rule reaches on a day (today by default): {rule id: count}."""
        from .services import independence
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            day = day or independence.today(db, actor.department_id)
            return {"day": day.isoformat(), **policy_reach(db, actor, day, workspace_id, starts_on, ends_on, details, policy_id, line_id)}

    @app.post('/api/rules/catalogue/{catalogue_id}/effect')
    def rule_line_effect(catalogue_id: str, value: s.RuleLineEffectInput, request: Request):
        from .services.rule_effects import draft_line_effect
        with transaction(engine) as db:
            return draft_line_effect(db, actor_for(request, db, write=True), catalogue_id, value)

    @app.post('/api/workspaces/{workspace_id}/adjustment-effect')
    def rota_adjustment_effect(workspace_id: s.ID, value: s.AdjustmentEffectInput, request: Request):
        from .services.rule_effects import adjustment_effect
        with transaction(engine) as db:
            return adjustment_effect(db, actor_for(request, db, write=True), workspace_id, value)

    def _policy_review(policy_id, request, *, decision=None):
        from .services.rule_effects import review
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            return review(db, actor, policy_id, decision=decision)

    @app.get('/api/policies/{policy_id}/review')
    def policy_review(policy_id: s.ID, request: Request):
        return _policy_review(policy_id, request)

    @app.post('/api/policies/{policy_id}/review')
    def policy_replacement_review(policy_id: s.ID, value: s.PolicyActivationDecision, request: Request):
        return _policy_review(policy_id, request, decision=value)

    @app.post('/api/policies/{policy_id}/relax')
    def policy_relax(policy_id: s.ID, value: s.RuleRelaxInput, request: Request):
        from .services.rule_effects import relax_seed
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            return relax_seed(db, actor, policy_id, value)

    @app.post('/api/policies/{policy_id}/promote-adjustment', response_model=s.PromoteAdjustmentSeed)
    def policy_promote_adjustment(policy_id: s.ID, value: s.PromoteAdjustmentInput, request: Request):
        from .services.rule_effects import promote_adjustment_seed
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            return promote_adjustment_seed(db, actor, policy_id, value)

    @app.post("/api/tags/{tag_id}/preview")
    def tag_preview(tag_id: str, change: s.TagMemberPreviewInput, request: Request):
        """What adding/removing these members would change (nothing is saved): rules and their reach today,
        saved views, sessions in rotas still being planned."""
        from .services.tags import preview
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "admin", "coordinator")
            return preview(db, actor, tag_id, change)

    @app.get("/api/policies/{policy_id}/impact")
    def impact(policy_id: str, request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_catalogue_read(actor, 'policies')
            policy = scoped(db, m.Policy, policy_id, actor)
            return {"impact": policy_impact(db, actor, policy)}

    @app.post('/api/series/preview', response_model=s.SeriesOverlapPreview | s.SeriesOverlapBatchPreview)
    def recurrence_proposal_preview(value: s.SeriesPreviewInput, request: Request):
        from .services.series_conflicts import preview, preview_many, validate_pending
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'admin', 'coordinator', 'approver')
            items = validate_pending(db, actor, value.items if value.items is not None else [s.SeriesPreviewItem(
                value=value.value, series_id=value.series_id, expected_revision=value.expected_revision)])
            if value.items is not None:
                return preview_many(db, actor, items)
            item, = items
            return preview(db, actor, item.value, item.series_id)

    @app.get("/api/series/{series_id}/preview", response_model=s.SeriesOverlapPreview)
    def recurrence_preview(series_id: str, request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "admin", "coordinator", "approver")
            series = scoped(db, m.RecurringSeries, series_id, actor)
            from .services.series_conflicts import preview
            # Stored definitions predate the fixed-assignee default: read them as stored (maintenance).
            value = s.SeriesInput.model_validate({**{key: record(series)[key] for key in s.SeriesInput.model_fields},
                                                 "definition": stored_series_definition(series.definition)})
            return preview(db, actor, value, series.id)

    @app.get("/api/wishes")
    def wish_list(request: Request, start: date | None = None, end: date | None = None,
                  person_id: s.ID | None = None, include_history: bool = False, workspace_id: s.ID | None = None):
        from .services import wishes
        with transaction(engine) as db:
            return wishes.list_wishes(db, actor_for(request, db), start, end, person_id, include_history, workspace_id)

    @app.get('/api/people/{person_id}/fairness', response_model=s.PersonFairness, response_model_exclude_unset=True)
    def person_fairness(person_id: s.ID, request: Request, workspace_id: s.ID | None = None,
                        starts_on: date | None = None, ends_on: date | None = None):
        from .services import person_fairness
        with transaction(engine) as db:
            return person_fairness.standing(db, actor_for(request, db), person_id, workspace_id, starts_on, ends_on)

    @app.get('/api/people/{person_id}/score', response_model=s.PersonScore)
    def person_score(person_id: s.ID, request: Request, workspace_id: s.ID | None = None,
                     open_limit: int = Query(0, ge=0, le=50), open_offset: int = Query(0, ge=0)):
        from .services import person_score
        with transaction(engine) as db:
            return person_score.contributions(db, actor_for(request, db), person_id, workspace_id, open_limit, open_offset)

    @app.get('/api/people/{person_id}/workload', response_model=s.PersonWorkload)
    def person_workload(person_id: s.ID, request: Request, on: date | None = None, workspace_id: s.ID | None = None):
        from .services import person_workload
        with transaction(engine) as db:
            return person_workload.read(db, actor_for(request, db), person_id, on, workspace_id)

    @app.get('/api/people/{person_id}/availability', response_model=s.PersonAvailability)
    def person_availability(person_id: s.ID, request: Request, start: date, end: date):
        from .services import person_availability
        with transaction(engine) as db:
            return person_availability.list_availability(db, actor_for(request, db), person_id, start, end)

    @app.get('/api/people/{person_id}/accounts', response_model=s.PersonAccounts)
    def person_accounts(person_id: s.ID, request: Request):
        from .services import person_accounts
        with transaction(engine) as db:
            return person_accounts.accounts(db, actor_for(request, db), person_id)

    @app.get('/api/people/{person_id}/sessions', response_model=s.PersonSessions)
    def person_sessions(person_id: s.ID, request: Request, limit: int = Query(100, ge=1, le=200),
                        offset: int = Query(0, ge=0)):
        from .services import person_accounts
        with transaction(engine) as db:
            return person_accounts.sessions(db, actor_for(request, db), person_id, limit, offset, settings=settings)

    @app.get('/api/people/{person_id}/auth-activity', response_model=s.PersonAuthActivity)
    def person_auth_activity(person_id: s.ID, request: Request, limit: int = Query(100, ge=1, le=100),
                             offset: int = Query(0, ge=0, le=1_000_000)):
        from .services import person_accounts
        with transaction(engine) as db:
            return person_accounts.auth_activity(db, actor_for(request, db), person_id, limit, offset)

    @app.get("/api/availability-patterns")
    def availability_pattern_list(request: Request):
        with transaction(engine) as db:
            return availability_patterns.list_patterns(db, actor_for(request, db))

    @app.get("/api/table-views")
    def table_view_list(request: Request, table: str):
        require(bool(re.fullmatch(r"[a-z0-9_-]{1,60}", table)), "INVALID_TABLE", "Unknown list")
        with transaction(engine) as db:
            return table_views.list_views(db, actor_for(request, db), table)

    @app.get("/api/notifications", response_model=s.NotificationList)
    def notifications(request: Request, limit: int = Query(100, ge=1, le=100),
                      offset: int = Query(0, ge=0, le=1_000_000), unread: bool = False,
                      kind: str | None = Query(None, max_length=40), search: str = Query('', max_length=200),
                      search_options: s.SearchOptions | None = Depends(parse_search_options)):
        from .services import notifications as notification_service
        with transaction(engine) as db:
            return notification_service.list_all(db, actor_for(request, db), limit=limit, offset=offset,
                                                 unread=unread, kind=kind, search=search, search_options=search_options)

    @app.get("/api/jobs")
    def jobs_list(request: Request, response: Response, workspace_id: str | None = None, status: str | None = None,
                  limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator", "operator", "approver")
            from packages.domain.worker_contract import JOB_STATES
            require(status is None or status in JOB_STATES, "SCHEMA_INVALID", "Unknown job status", 422)
            workspace = scoped(db, m.Workspace, workspace_id, actor, options=(
                defer(m.Workspace.validation), defer(m.Workspace.review_snapshot))) if workspace_id else None
            from .services.conditional_reads import conditional_response, revision_etag
            department = db.get(m.Department, actor.department_id)
            organisation = db.get(m.Organisation, department.organisation_id)
            from .services.job_reads import current_source_digest, generation_currency, job_stamp, metadata, result_stale
            from .serialization import effective_input_fingerprint
            from .services.jobs import application_recheckable, superseded_flag
            history_fields = (m.Job.kind, m.Job.workspace_id, m.Job.requested_by, m.Job.status,
                              m.Job.search_control, m.Job.read_metadata, m.Job.input_fingerprint, m.Job.error_code, m.Job.started_at,
                              m.Job.completed_at, m.Job.attempts, m.Job.heartbeat_at, m.Job.progress,
                              m.Job.id, m.Job.revision, m.Job.created_at, m.Job.department_id)
            history_filters = [m.Job.department_id == actor.department_id,
                or_(m.Job.kind.not_in(['merge_preview', 'merge_apply']), m.Job.requested_by == actor.identity_id),
                *([m.Job.kind.not_in(['catalogue_proof', 'swap_audience'])] if workspace_id else []),
                *([m.Job.kind.in_(['feasibility', 'catalogue_proof'])] if not actor.permits("coordinator", "operator") else []),
                *([m.Job.workspace_id == workspace_id] if workspace_id else []),
                # SW3-D-24: Operations opens the jobs in one state (its failed or cancelled count), newest first.
                *([m.Job.status == status] if status else [])]
            tag = revision_etag("jobs-list", actor.department_id, actor.identity_id, actor.person_id,
                                sorted(actor.roles), workspace_id, status, limit, offset,
                                (workspace.revision, workspace.status) if workspace else None,
                                (department.revision, organisation.revision, organisation.input_revision,
                                 organisation.availability_revision, settings.generation_engine, build()["release"],
                                 job_stamp(db, department_id=actor.department_id)))
            cached = conditional_response(request, tag)
            if isinstance(cached, Response):
                return cached
            response.headers.update(cached)
            found = db.execute(select(m.Job, superseded_flag(db, m.Job))
                .options(load_only(*history_fields)).where(*history_filters)
                .order_by(m.Job.created_at.desc(), m.Job.seq.desc()).offset(offset).limit(limit + 1)).all()
            rows = [row for row, _ in found]
            superseded = {row.id for row, flag in found if flag and row.kind == 'generation' and row.status == 'succeeded'}
            deferred = {row.id: True if row.read_metadata is None and row.status in {'queued', 'running'} else None
                        for row in rows}
            workspaces = {w.id: w for w in db.scalars(select(m.Workspace).where(
                m.Workspace.id.in_({row.workspace_id for row in rows[:limit] if row.kind == 'generation'}),
                m.Workspace.department_id == actor.department_id).options(defer(m.Workspace.validation)))}
            from .snapshots import fingerprints as read_fingerprints, receipt_currencies
            candidates = {wid: [row for row in rows[:limit] if row.kind == 'generation' and row.workspace_id == wid]
                          for wid in workspaces}
            fingerprints = receipt_currencies(db, list(workspaces.values()), candidates, read_fingerprints)
            source = current_source_digest() if any((row.read_metadata or {}).get('input_snapshot', {}).get('generation_scope') is not None for row in rows[:limit]) else None
            list_input_keys = {'generation_engine', 'generation_options', 'compiler_version', 'diagnostic_proposal', 'bindings',
                               'generation_scope', 'generation_scope_source', 'run_context', 'generation_manifest',
                               'planning_group_version_id', 'planning_group_id', 'lookahead', 'planning_context',
                               'proof_scope', 'completion'}
            from .workspace_baselines import workspace_generation_defaults
            return {**({"generation_defaults": workspace_generation_defaults(db, workspace, department)}
                       if workspace else {}),
                    "jobs": [{**job_record(row, include_result=False, include_progress_solution=False,
                                             input_snapshot={key: value for key, value in projection['input_snapshot'].items()
                                                             if key in list_input_keys},
                                       result_metadata=projection['result'], input_fingerprint=effective),
                                      **({'result_stale': stale,
                                           'currency': generation_currency(row, projection, stale=stale,
                                                                            can_recheck=can_recheck,
                                                                            superseded=row.id in superseded,
                                                                            deferred=deferred.get(row.id))}
                                         if row.kind == 'generation' else {})}
                            for row in rows[:limit] for projection in [metadata(row, load_legacy=False)]
                            for projection in [({**projection, 'input_snapshot': {**projection['input_snapshot'],
                                                                                  '_deferred_generation': True}}
                                                if deferred.get(row.id) and
                                                '_deferred_generation' not in projection['input_snapshot'] else projection)]
                            for effective in [effective_input_fingerprint(row, projection=projection,
                                                                          deferred=deferred.get(row.id))]
                            for stale in [result_stale(row, projection, fingerprints.get(row.workspace_id), source,
                                                       input_fingerprint=effective)]
                            for can_recheck in [application_recheckable(projection['input_snapshot'], projection['result'], source)]],
                "limit": limit, "offset": offset, "has_more": len(rows) > limit,
                "generation_capability": generation_manifest() if settings.generation_engine == "cp_sat" else MANIFEST}

    @app.get('/api/jobs/{job_id}/input-changes')
    def job_input_changes(job_id: str, request: Request, limit: int = Query(100, ge=1, le=100),
                          offset: int = Query(0, ge=0, le=1_000_000)):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'coordinator')
            job = scoped(db, m.Job, job_id, actor, options=[load_only(m.Job.kind, m.Job.workspace_id)])
            require(job.kind == 'generation', 'JOB_KIND', 'Input changes belong to an auto-schedule proposal', 409)
            workspace = scoped(db, m.Workspace, job.workspace_id, actor)
            frozen, since = db.execute(select(m.Job.input_snapshot['input_manifest'],
                m.Job.input_snapshot['audit_watermark']).where(m.Job.id == job.id)).one()
            current = domain_snapshot(db, workspace)
            from .input_changes import changed_facts
            rows = changed_facts(db, {'input_manifest': frozen}, current, actor.department_id, since or 0)
            return {'changes': rows[offset:offset + limit], 'total': len(rows), 'offset': offset, 'limit': limit,
                    'has_more': offset + limit < len(rows), 'current_fingerprint': current['fingerprint'],
                    'attribution_complete': bool(frozen) and all(r['attribution_complete'] for r in rows)}

    @app.get('/api/jobs/{job_id}/reconciliation')
    def job_reconciliation(job_id: str, request: Request,
                           section: Literal['changed_facts', 'conflicting_placements'] = 'changed_facts',
                           limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, 'coordinator')
            scoped(db, m.Job, job_id, actor, options=[load_only(m.Job.id)])
            from .services.job_reads import reconciliation_page
            receipt_id, total, rows = reconciliation_page(db, job_id, section, offset, limit)
            return {'receipt_id': receipt_id, section: rows, 'total': total or 0, 'offset': offset, 'limit': limit,
                    'has_more': offset + limit < (total or 0)}

    @app.get('/api/jobs/{job_id}/status')
    def job_status(job_id: str, request: Request, events: bool = False):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            from .services.job_reads import (complete_validation_delta, current_source_digest,
                                              generation_currency, result_stale, status_metadata)
            status_options = (defer(m.Job.input_snapshot), defer(m.Job.result), defer(m.Job.read_metadata))
            from .services.jobs import superseded_flag
            found = db.execute(select(m.Job, superseded_flag(db, m.Job)).where(m.Job.id == job_id)
                               .options(*status_options)).one_or_none()
            require(found is not None, 'NOT_FOUND', 'Record not found', 404)
            job = scoped(db, m.Job, job_id, actor, options=status_options)
            require_role(actor, 'coordinator', *(['approver'] if job.kind in {'feasibility', 'catalogue_proof'} else []))
            workspace = scoped(db, m.Workspace, job.workspace_id, actor, options=(defer(m.Workspace.validation),))
            projection = status_metadata(db, job.id)
            snapshot = projection['input_snapshot']
            include_events = events and job.kind == 'generation' and job.status == 'running'
            if include_events:
                snapshot = {**snapshot, 'focus_ids': db.scalar(select(m.Job.input_snapshot['focus_ids']).where(
                    m.Job.id == job.id)) or []}
            value = job_record(job, include_result=(job.kind == 'fill_options'), input_snapshot=snapshot,
                               result_metadata=projection['result'], include_events=include_events,
                               input_fingerprint=projection.get('input_fingerprint'), include_error_detail=True)
            if job.kind == 'generation':
                from .services.jobs import application_recheckable, superseded_flag
                deferred = (job.status in {'queued', 'running'}
                            and projection.get('input_fingerprint') is None)
                source = current_source_digest() if snapshot.get('generation_scope') is not None else None
                if deferred:
                    stale = None
                else:
                    from .snapshots import fingerprints as read_fingerprints
                    current = read_fingerprints(db, [workspace])[workspace.id]
                    stale = result_stale(job, projection, current, source,
                                         input_fingerprint=projection.get('input_fingerprint'))
                value['result_stale'] = stale
                value['currency'] = generation_currency(
                    job, projection, stale=stale,
                    can_recheck=application_recheckable(projection['input_snapshot'], projection['result'], source),
                    superseded=bool(found[1]),
                    deferred=deferred)
            value['delta'] = complete_validation_delta(db, job, workspace)
            return {'job': disclose_leave(db, actor, json_value(value))}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, request: Request, full: bool = False):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            from .services.job_reads import current_source_digest, full_job_result, generation_currency, metadata, result_stale
            from .services.jobs import superseded_flag
            from .serialization import effective_input_fingerprint, job_detail_result, job_result_projection
            from .serialization import job_result_with_reconciliation
            if full:
                options = []
            else:
                detail_fields = (m.Job.kind, m.Job.workspace_id, m.Job.requested_by, m.Job.status,
                                  m.Job.search_control, m.Job.read_metadata, m.Job.input_fingerprint, m.Job.result_summary,
                                  m.Job.error_code, m.Job.started_at, m.Job.completed_at, m.Job.attempts,
                                  m.Job.heartbeat_at, m.Job.progress, m.Job.id, m.Job.revision,
                                  m.Job.created_at, m.Job.department_id)
                options = [load_only(*detail_fields)]
            found = db.execute(select(m.Job, superseded_flag(db, m.Job)).where(m.Job.id == job_id)
                               .options(*options)).one_or_none()
            require(found is not None, 'NOT_FOUND', 'Record not found', 404)
            # Keep the loaded row alive so scoped reuses it while enforcing its access checks.
            job = scoped(db, m.Job, job_id, actor, options=options)
            deferred = True if job.read_metadata is None and job.status in {'queued', 'running'} else None
            require_role(actor, "coordinator", *( ["approver"] if job.kind in {'feasibility', 'catalogue_proof'} else []))
            workspace = scoped(db, m.Workspace, job.workspace_id, actor)
            snapshot = metadata(job)['input_snapshot']
            projection = None if full else (job.result_summary if job.read_metadata is not None
                                             else job_result_projection(job.kind, job.result))
            # Apply the current read projection to summaries retained by older workers too.
            result = full_job_result(db, job) if full else job_detail_result(job.kind, (projection or {}).get('result'))
            result = job_result_with_reconciliation(job, result)
            include_events = job.kind == 'generation' and job.status == 'running'
            if include_events:
                snapshot = {**snapshot, 'focus_ids': db.scalar(select(m.Job.input_snapshot['focus_ids']).where(
                    m.Job.id == job.id)) or []}
            retained = metadata(job, load_legacy=False)
            value = job_record(job, input_snapshot=snapshot, result_summary=result, include_events=include_events,
                               input_fingerprint=effective_input_fingerprint(job, projection=retained),
                               include_error_detail=True)
            if job.kind == 'generation':
                from packages.domain.solver_rules import COMPILER_VERSION
                from .services.issues import issue_key
                from .services.jobs import application_recheckable
                from .snapshots import fingerprints as read_fingerprints
                current = read_fingerprints(db, [workspace])[workspace.id]
                source = current_source_digest() if snapshot.get('generation_scope') is not None else None
                value['result_stale'] = result_stale(job, retained, current, source,
                    input_fingerprint=effective_input_fingerprint(job, projection=retained))
                can_recheck = application_recheckable(retained['input_snapshot'], retained['result'], source)
                superseded = job.status == 'succeeded' and found[1]
                value['currency'] = generation_currency(job, retained, stale=value['result_stale'],
                                                        can_recheck=can_recheck, superseded=superseded,
                                                        deferred=deferred)
                value['reconciliation'] = (result or {}).get('reconciliation')
                # A metadata-only detail must not fall back to the frozen input blob
                # just to reconstruct an optional context (legacy rows may omit it).
                # Full detail explicitly opts into the retained input and can still
                # derive the context for those old rows.
                if full and 'run_context' not in snapshot:
                    from .serialization import job_run_context
                    value['run_context'] = job_run_context(job.input_snapshot)
                from .services.job_recovery import recovery
                remedy = recovery(actor, job, result, stale=value['result_stale'])
                if remedy is not None:
                    value['recovery'] = remedy
                check = workspace.validation or {}
                current_advisory = {issue_key(f) for f in check.get('findings', []) if f['severity'] == 'warning'}
                accepted = {a['key'] for a in workspace.accepted_issues or []}
                value['advisory_acceptance'] = {'workspace_revision': workspace.revision, 'issues': [
                    {'key': issue_key(f), 'accepted': issue_key(f) in accepted,
                     'can_accept': (workspace.status == 'draft' and workspace.archived_at is None
                                    and check.get('fingerprint') == current and issue_key(f) in current_advisory
                                    and issue_key(f) not in accepted)}
                    for f in (result or {}).get('validation', {}).get('findings', []) if f['severity'] == 'warning']}
            if job.kind == 'feasibility':
                from packages.domain.solver_rules import COMPILER_VERSION
                from .snapshots import feasibility_fingerprint
                workspace = db.get(m.Workspace, job.workspace_id)
                stale = metadata(job).get('input_fingerprint') != feasibility_fingerprint(db, workspace) or snapshot.get('compiler_version') != COMPILER_VERSION
                for binding in snapshot.get('bindings', []):
                    # A person/calendar fact binding names the fact (its policy_id is the fact id).
                    policy = db.get(m.SchedulingFact if binding.get('fact_id') else m.Policy, binding['policy_id'])
                    stale = stale or policy is None or snapshot_hash(record(policy)) != binding['source_hash']
                value['check_stale'] = stale
            if job.kind == 'catalogue_proof':
                from .services.jobs import catalogue_proof_stale
                value['check_stale'] = catalogue_proof_stale(db, actor, job.input_snapshot)
            return {"job": disclose_leave(db, actor, json_value(value))}

    @app.get("/api/jobs/{job_id}/evidence")
    def job_evidence(job_id: str, request: Request, offset: int = Query(0, ge=0),
                     limit: int = Query(50, ge=1, le=100), phase: str | None = None,
                     diagnostic_index: int | None = Query(None, ge=0), shift_id: str | None = None):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            from .services.job_reads import evidence_page
            job = scoped(db, m.Job, job_id, actor, options=(defer(m.Job.input_snapshot), defer(m.Job.result)))
            require_role(actor, 'coordinator', *(['approver'] if job.kind == 'feasibility' else []))
            scoped(db, m.Workspace, job.workspace_id, actor)
            require(phase in ('baseline', 'proposed') if job.kind == 'feasibility' else phase is None,
                    'EVIDENCE_PHASE', 'Choose the current or proposed rule check', 422)
            page = evidence_page(db, job, phase=phase, diagnostic_index=diagnostic_index, shift_id=shift_id or None,
                                 offset=offset, limit=limit)
            require(diagnostic_index is None or diagnostic_index < page.pop('diagnostic_count'),
                    'EVIDENCE_NOT_FOUND', 'This explanation is unavailable', 404)
            page.pop('diagnostic_count', None)
            from .serialization import effective_input_fingerprint
            from .services.job_reads import metadata
            return {**page, 'candidates': disclose_leave(db, actor, page['candidates']),
                    'offset': offset, 'limit': limit, 'has_more': offset + limit < page['total'],
                    'input_fingerprint': effective_input_fingerprint(
                        job, projection=metadata(job, load_legacy=False)), 'job_id': job.id}

    def score_actor_for(request, db, *, write=False):
        from .services.score_check import require_enabled
        actor = actor_for(request, db, write=write)
        require_enabled(db, actor.department_id)
        return actor

    def score_mutation(request, command, files):
        enforce(engine, "commands", [session_identity(engine, request.cookies.get(cookie_name))],
                settings.rate_commands_per_minute, 60, "Too many changes; wait a minute and try again")
        command = command.model_copy(update={'payload': {**command.payload, 'files': [
            {'file_name': name, 'content_base64': base64.b64encode(data).decode('ascii')} for name, data in files]}})
        return execute(engine, settings, request.cookies.get(cookie_name), request.headers.get("x-csrf-token"),
                       request.headers.get("idempotency-key"), command, request.state.correlation_id)

    def score_upload_auth(request):
        with transaction(engine) as db:
            require_role(score_actor_for(request, db, write=True), 'coordinator')

    async def score_files(request):
        # Authenticate before parsing file parts; the ASGI body limit bounds the complete multipart envelope.
        await run_in_threadpool(score_upload_auth, request)
        require(request.headers.get('content-type', '').partition(';')[0].strip().lower() == 'multipart/form-data',
                'UPLOAD_MULTIPART', 'Upload files as multipart form data', 415)
        async with request.form(max_files=s.SCORE_CHECK_LIMITS['files'], max_fields=12,
                                max_part_size=s.SCORE_CHECK_LIMITS['file_bytes']) as form:
            files, fields, total = [], {}, 0
            allowed = {'profile_id', 'profile_revision', 'privacy_acknowledged', 'revision'}
            for key, part in form.multi_items():
                if isinstance(part, UploadFile):
                    require(key == 'files', 'UPLOAD_FIELD', 'Upload files in the files field')
                    data = await part.read(s.SCORE_CHECK_LIMITS['file_bytes'] + 1)
                    total += len(data)
                    require(0 < len(data) <= s.SCORE_CHECK_LIMITS['file_bytes'] and total <= s.SCORE_CHECK_LIMITS['total_file_bytes'],
                            'UPLOAD_LIMIT', 'The uploaded files exceed the supported size', 413)
                    files.append((part.filename or 'upload', data))
                else:
                    require(key in allowed and key not in fields, 'UPLOAD_FIELD', 'Use each supported upload field once')
                    fields[key] = part
            require(files, 'EMPTY_IMPORT', 'Choose at least one file')
            for key in ('profile_revision', 'revision'):
                if key in fields:
                    require(fields[key].isascii() and fields[key].isdigit() and int(fields[key]) > 0,
                            'UPLOAD_REVISION', 'Use a valid current revision')
                    fields[key] = int(fields[key])
            if 'privacy_acknowledged' in fields:
                require(fields['privacy_acknowledged'] in {'true', 'false'}, 'UPLOAD_CONSENT', 'Acknowledge the provider explicitly')
                fields['privacy_acknowledged'] = fields['privacy_acknowledged'] == 'true'
            return files, fields

    @app.post("/api/score-imports", openapi_extra={'requestBody': {'required': True, 'content': {
        'multipart/form-data': {'schema': s.ScoreImportUpload.model_json_schema()}}}})
    async def score_import_start(request: Request):
        files, fields = await score_files(request)
        revision = fields.pop('revision', None)
        require(revision is None, 'UPLOAD_REVISION', 'A new import has no revision')
        return await run_in_threadpool(score_mutation, request, s.Command(kind='score_import.upload', payload=fields), files)

    @app.post("/api/score-imports/{import_id}/sources", openapi_extra={'requestBody': {'required': True, 'content': {
        'multipart/form-data': {'schema': s.ScoreImportAddFiles.model_json_schema()}}}})
    async def score_import_add(import_id: str, request: Request):
        files, fields = await score_files(request)
        revision = fields.pop('revision', None)
        require(set(fields) <= {'privacy_acknowledged', 'profile_revision'}, 'UPLOAD_FIELD', 'Change source metadata after uploading')
        return await run_in_threadpool(score_mutation, request, s.Command(kind='score_import.add_files', target_id=import_id,
            expected_revision=revision, payload={'import_id': import_id, **fields}), files)

    @app.get("/api/score-imports/providers")
    def score_import_providers(request: Request):
        from .services import score_check_ai
        with transaction(engine) as db:
            return score_check_ai.choices(db, score_actor_for(request, db))

    @app.get("/api/score-imports/{import_id}")
    def score_import_detail(import_id: str, request: Request):
        from .services import score_check_api
        with transaction(engine) as db:
            actor = score_actor_for(request, db)
            return score_check_api.view_import(db, actor, scoped(db, m.ScoreCheckImportBatch, import_id, actor))

    @app.get("/api/score-imports/{import_id}/check")
    def score_import_check(import_id: str, request: Request):
        from .services import score_check_api
        # Refreshing the check writes gaps and inferred dates. Queue behind other writers before reading the import.
        with transaction(engine, write=True) as db:
            actor = score_actor_for(request, db)
            return score_check_api.view_check(db, actor, scoped(db, m.ScoreCheckImportBatch, import_id, actor), settings)

    @app.get("/api/score-imports/{import_id}/matches", response_model=s.ImportMatches)
    def score_import_matches(import_id: str, request: Request, offset: int = Query(0, ge=0, le=1_000_000),
                             limit: int = Query(50, ge=1, le=100)):
        from .services import score_check_api
        with transaction(engine) as db:
            actor = score_actor_for(request, db)
            require_role(actor, 'coordinator')
            return score_check_api.view_matches(db, actor, scoped(db, m.ScoreCheckImportBatch, import_id, actor),
                                                offset=offset, limit=limit)

    @app.get("/api/benchmark")
    def score_benchmark_view(request: Request, workspace: str):
        from .services import score_check_benchmark
        with transaction(engine) as db:
            return score_check_benchmark.frontend_view(db, score_actor_for(request, db), workspace)

    @app.get("/api/ai-providers")
    def score_ai_providers(request: Request):
        from .services import score_check_ai
        with transaction(engine) as db:
            return score_check_ai.profiles(db, score_actor_for(request, db), settings)

    @app.get("/api/ai-providers/calls")
    def score_ai_calls(request: Request):
        from .services import score_check_ai
        with transaction(engine) as db:
            return score_check_ai.recent_calls(db, score_actor_for(request, db))

    @app.get("/api/score-extraction-jobs/{job_id}")
    def score_ai_job(job_id: str, request: Request):
        from .services import score_check_ai
        with transaction(engine) as db:
            actor = score_actor_for(request, db)
            require_role(actor, 'coordinator', *s.PERMISSIONS['ai.providers'])
            job = scoped(db, m.ScoreCheckExtractionJob, job_id, actor)
            require(job.kind != 'test' or actor.permits(*s.PERMISSIONS['ai.providers']), 'FORBIDDEN', 'Provider tests require Developer access', 403)
            return score_check_ai.job_record(job)

    @app.get("/api/score-checks")
    def score_check_list(request: Request):
        from .services import score_check
        with transaction(engine) as db:
            return score_check.list_batches(db, score_actor_for(request, db))

    @app.get("/api/score-checks/{batch_id}")
    def score_check_detail(batch_id: str, request: Request):
        from .services import score_check
        with transaction(engine) as db:
            actor = score_actor_for(request, db)
            require_role(actor, "coordinator")
            return score_check.view(db, actor, scoped(db, m.ScoreCheckImportBatch, batch_id, actor))

    @app.get("/api/benchmarks")
    def score_check_benchmarks(request: Request, starts_on: date | None = None, ends_on: date | None = None):
        from .services import score_check_benchmark
        with transaction(engine) as db:
            return score_check_benchmark.list_results(db, score_actor_for(request, db), starts_on, ends_on)

    @app.get("/api/benchmarks/{result_id}")
    def score_check_benchmark_detail(result_id: str, request: Request):
        from .services import score_check_benchmark
        with transaction(engine) as db:
            return score_check_benchmark.detail(db, score_actor_for(request, db), result_id)

    @app.get("/api/imports")
    def imports_list(request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator")
            return {"batches": [record(r, exclude=("rows",)) for r in db.scalars(select(m.ImportBatch).options(defer(m.ImportBatch.rows)).where(
                m.ImportBatch.department_id == actor.department_id).order_by(m.ImportBatch.created_at.desc(), m.ImportBatch.seq.desc()).limit(100))]}

    @app.get("/api/reference-imports")
    def reference_imports(request: Request):
        from .services.imports import reference_receipt
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator")
            return {"imports": [reference_receipt(db, source) for source in db.scalars(
                select(m.ReferenceImport).where(m.ReferenceImport.department_id == actor.department_id,
                    func.coalesce(m.ReferenceImport.manifest['format'].as_string(), '') != s.ROTATION_REFERENCE_FORMAT)
                .order_by(m.ReferenceImport.created_at.desc(), m.ReferenceImport.seq.desc()).limit(10))]}

    @app.get("/api/staff-imports")
    def staff_imports_list(request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "admin", "coordinator", "credentialer")
            return {"batches": [record(r, exclude=("rows", "applied")) | {"rows": count} for r, count in db.execute(
                select(m.StaffImportBatch, func.json_array_length(m.StaffImportBatch.rows)).options(
                    defer(m.StaffImportBatch.rows), defer(m.StaffImportBatch.applied)).where(
                m.StaffImportBatch.department_id == actor.department_id).order_by(m.StaffImportBatch.created_at.desc(), m.StaffImportBatch.seq.desc()).limit(100))]}

    @app.get("/api/staff-imports/template.csv")
    def staff_import_template(request: Request):
        from .services.staff_import import template_csv
        with transaction(engine) as db:
            require_role(actor_for(request, db), "admin", "coordinator", "credentialer")
        return Response(template_csv(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="staff-import-template.csv"'})

    @app.get("/api/staff-imports/{batch_id}", response_model=s.StaffImportReview)
    def staff_import_detail(batch_id: str, request: Request):
        from .services import staff_import
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "admin", "coordinator", "credentialer")
            return staff_import.view(db, actor, scoped(db, m.StaffImportBatch, batch_id, actor))

    @app.get("/api/imports/{batch_id}")
    def import_detail(batch_id: str, request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "coordinator")
            return {"batch": record(scoped(db, m.ImportBatch, batch_id, actor))}

    @app.get("/api/access")
    def access_list(request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_role(actor, "admin", *s.PERMISSIONS["access.system_roles"], *s.PERMISSIONS["access.installation_admin"])
            full_access = actor.permits("admin", *s.PERMISSIONS["access.system_roles"])
            query = select(m.RoleBinding).join(m.Identity).where(
                m.RoleBinding.department_id == actor.department_id, m.Identity.issuer != SWAP_AUTOMATION_ISSUER)
            if not full_access:
                query = query.where(m.RoleBinding.role == s.INSTALLATION_ADMIN_ROLE)
            bindings = list(db.scalars(query))
            # Include sign-ins without bindings so authorized role managers can select them.
            from .services.access import sign_in_identities
            identities = list(db.scalars(select(m.Identity).where(sign_in_identities(actor, settings))))
            if not full_access:
                return {"bindings": [record(r) for r in bindings], "identities": [
                    {"id": i.id, "display_name": i.display_name, "disabled": i.disabled} for i in identities]}
            from .services import accounts
            from .identity_store import store_for
            return {"bindings": [record(r) for r in bindings], "identities": [record(i, exclude=("password_hash",)) for i in identities],
                    "accounts": accounts.account_rows(db, [i.id for i in identities]),
                    "accounts_enabled": store_for(settings) is not None,
                    "sessions": accounts.session_view(db.get(m.Department, actor.department_id), settings)}

    @app.get("/api/operations")
    def operations(request: Request, offset: int = 0, state: str | None = Query(None, max_length=200),
                   kind: str | None = Query(None, max_length=2000), q: str | None = Query(None, max_length=200),
                   collection_filter: s.CollectionFilter = Depends(parse_collection_filter("operations"))):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            return service_operations.observe(db, actor, engine.dialect.name, settings.environment, offset, state, kind, q,
                                              collection_filter=collection_filter)

    @app.get("/api/system/monitor-integration", response_model=s.MonitorIntegrationStatus)
    def monitor_integration(request: Request):
        with transaction(engine) as db:
            return monitors.integration(db, actor_for(request, db))

    @app.put("/api/system/monitor-integration", response_model=s.MonitorTokenResult, response_model_exclude_none=True)
    def change_monitor_integration(data: s.MonitorTokenInput, request: Request):
        enforce(engine, "commands", [session_identity(engine, request.cookies.get(cookie_name))],
                settings.rate_commands_per_minute, 60, "Too many changes; wait a minute and try again")

        def change(db):
            return monitors.change_token(db, actor_for(request, db, write=True), data, request.state.correlation_id)
        return retry_write(engine, change)

    @app.post("/api/system/monitor-events", response_model=s.MonitorEventReceipt)
    async def receive_monitor_event(request: Request):
        raw = await request.body()

        def receive():
            enforce(engine, "monitor-address", [peer_key(request)], 600, 60, "Too many monitor requests; wait a minute and try again")
            with transaction(engine) as db:
                department_id = monitors.authenticate(db, request.headers.get("authorization"))
            enforce(engine, "monitor-events", [department_id], settings.monitor_events_per_minute, 60,
                    "Too many monitor events; wait a minute and try again")
            parsed = monitors.parse_payload(raw)

            def store(db):
                # Reauthenticate under the write lock so rotation/revocation cannot race incident storage.
                department = monitors.authenticate(db, request.headers.get("authorization"))
                return monitors.receive(db, department, parsed)
            return retry_write(engine, store)
        return await run_in_threadpool(receive)

    @app.get("/api/system/monitor-events", response_model=s.MonitorEventList)
    def monitor_events(request: Request, offset: int = Query(0, ge=0, le=1_000_000)):
        with transaction(engine) as db:
            return monitors.incidents(db, actor_for(request, db), offset)

    @app.get("/api/system/status-files", response_model=s.SystemStatusFiles)
    def system_status_files(request: Request, lines: int = Query(50, ge=1, le=s.MONITOR_LIMITS["alert_lines"])):
        with transaction(engine) as db:
            actor = actor_for(request, db)
        return monitors.status_files(actor, settings.status_directory, lines)

    def audit_filters(start: s.date | None = Query(None, alias="from"), end: s.date | None = Query(None, alias="to"),
                      actor_id: str | None = Query(None, max_length=36), action: list[str] = Query([], max_length=40),
                      self_approved: bool = False, q: str | None = Query(None, max_length=200), person_id: s.ID | None = None,
                      target_id: s.ID | None = None,
                      collection_filter: s.CollectionFilter = Depends(parse_collection_filter("audit"))):
        """Decision-history filters shared by the page and its CSV; dates are department-local and inclusive."""
        for day in (start, end):
            require(day is None or s.date(2000, 1, 1) <= day <= s.date(2100, 12, 31), "INVALID_DATE", "Choose dates between 2000 and 2100")
        require(not (start and end) or start <= end, "INVALID_RANGE", "The start date must be on or before the end date")
        require(all(re.fullmatch(r"[a-z_]{1,40}", a) for a in action), "INVALID_ACTION", "Unknown kind of change")
        return {"start": start, "end": end, "actor_id": actor_id, "action": action, "self_approved": self_approved,
                "q": (q or "").strip(), "person_id": person_id, "target_id": target_id, "collection_filter": collection_filter}

    def audit_query(db, actor, f):
        from datetime import datetime, time as clock, timedelta, timezone
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(db.get(m.Organisation, actor.organisation_id).timezone)
        e = m.AuditEvent
        query = (select(e, m.Identity.display_name).outerjoin(m.Identity, m.Identity.id == e.actor_id)
                 .where(e.department_id == actor.department_id).order_by(e.created_at.desc(), e.seq.desc()))
        from .services.audit_log import read_scope
        query = query.where(read_scope(actor))
        if f["person_id"]:
            from .services.person_history import for_person
            query = for_person(db, actor, query, f["person_id"])
        if f["start"]:
            query = query.where(e.created_at >= datetime.combine(f["start"], clock(), zone).astimezone(timezone.utc))
        if f["end"]:
            query = query.where(e.created_at < datetime.combine(f["end"] + timedelta(days=1), clock(), zone).astimezone(timezone.utc))
        if f["actor_id"]:
            query = query.where(e.actor_id == f["actor_id"])
        if f['target_id']:
            query = query.where(e.target_id == f['target_id'])
        if f["action"]:
            query = query.where(or_(*(e.action.startswith(a + ".", autoescape=True) for a in f["action"])))
        if f["self_approved"]:
            query = query.where(e.detail["self_approved"].as_string().is_not(None))
        from .services import audit_log
        from .services.audit_log import summary_match
        masked = audit_log.masked_reason(e)
        shown = summary_match(db, actor, f["q"]) if (f["q"] or "").strip() else []
        if f['collection_filter'].search_options is not None:
            from .services.search import predicate
            query = query.where(or_(predicate(db, actor, 'audit', f['q'], f['collection_filter'].search_options,
                as_of=f['collection_filter'].as_of, columns=[masked, m.Identity.display_name]), *shown))
        elif f["q"]:
            like = "%" + re.sub(r"([\\%_])", r"\\\1", f["q"]) + "%"
            query = query.where(or_(masked.ilike(like, escape="\\"), m.Identity.display_name.ilike(like, escape="\\"), *shown))
        from .services.condition_context import evaluation_context, required_tokens
        context = evaluation_context(db, actor, as_of=f['collection_filter'].as_of, table_key='audit',
            tokens=required_tokens(f['collection_filter'].conditions)) if f['collection_filter'].conditions else None
        columns = {'id': e.id, 'action': e.action, 'kind': collection_filters.change_kind(e.action),
                   'actor_id': e.actor_id, 'person': m.Identity.display_name, 'target_id': e.target_id,
                   'reason': masked, 'created_at': e.created_at, 'date': e.created_at}
        query = query.where(collection_filters.predicate(f['collection_filter'].conditions, columns, context, table_key='audit'))
        query = collection_filters.order_by(query, f['collection_filter'], columns, default=(e.created_at.desc(), e.seq.desc()))
        return query, zone

    @app.get("/api/audit")
    def audit(request: Request, offset: int = 0, limit: int = Query(100, ge=1, le=100), people: bool = True,
              before_at: s.datetime | None = None, before_id: s.ID | None = None,
              fold: bool = False, cursor: str | None = Query(None, max_length=1000),
              run_id: str | None = Query(None, max_length=1000), f: dict = Depends(audit_filters)):
        from .services import audit_log
        require(0 <= offset <= 1_000_000, "INVALID_OFFSET", "Invalid pagination offset")
        require((before_at is None) == (before_id is None), 'INVALID_CURSOR', 'Use both cursor fields')
        ordered = not f['collection_filter'].sort and not f['collection_filter'].then_sort
        require(before_at is None or not fold and ordered and offset == 0, 'INVALID_CURSOR',
                'Cursor paging requires raw newest-first order without an offset')
        require(not cursor or fold, "INVALID_CURSOR", "Run cursors require folded history")
        require(not run_id or not fold, "INVALID_RUN", "Expand a run using raw history")
        require(not fold or not offset, "INVALID_OFFSET", "Use a cursor for folded history")
        require(not fold or ordered, "INVALID_SORT", "Folded history uses newest changes first")
        with transaction(engine) as db:
            actor = actor_for(request, db)
            # Without audit access a person reads only their own self-approved decisions (Home's row), not who else acted.
            own = not actor.permits(*s.PERMISSIONS["audit.read"])
            if own:
                require(f["self_approved"], "FORBIDDEN", "Your current role does not permit this action", 403)
                f, people = {**f, "actor_id": actor.identity_id}, False
            query, zone = audit_query(db, actor, f)
            if run_id:
                query = audit_log.expand(query, run_id)
            if before_at is not None:
                from packages.domain.time import aware
                instant = aware(before_at).astimezone(timezone.utc)
                query = query.where(audit_log._older(instant, before_id))
            if fold:
                page = audit_log.folded(db, query, zone, limit, cursor)
            else:
                events = list(db.execute(query.offset(offset).limit(limit + 1)))
                last = events[limit - 1][0] if len(events) > limit else None
                page = {"events": [{**audit_log.absence_audit_mask(record(e)), "kind": e.action.split('.', 1)[0], "actor_display_name": display_name}
                                   for e, display_name in events[:limit]], "has_more": len(events) > limit,
                        "next_cursor": {"before_at": json_value(last.created_at), "before_id": last.id} if last and ordered else None}
            # Probe the actor index once per identity instead of grouping years of event history.
            actors = list(db.execute(select(m.Identity.id, m.Identity.display_name).where(
                m.Identity.issuer != SWAP_AUTOMATION_ISSUER, select(m.AuditEvent.id).where(
                    m.AuditEvent.department_id == actor.department_id,
                    m.AuditEvent.actor_id == m.Identity.id).exists())
                .order_by(m.Identity.display_name, m.Identity.id).limit(2001))) if people else []
            audit_log.mask_balance_details(db, actor, page['events'])
            return {**page, "events": audit_log.subjects(db, actor.department_id, page['events']),
                    "offset": offset, "limit": limit,
                    "people": [{"id": i, "display_name": n} for i, n in actors[:2000]],
                    "people_has_more": len(actors) > 2000, "person_id": f["person_id"], "target_id": f['target_id']}

    @app.get("/api/audit.csv")
    def audit_csv(request: Request, f: dict = Depends(audit_filters)):
        import csv, io
        from .services import audit_log
        from .streaming import ClosingStreamingResponse as StreamingResponse
        from packages.domain.reports import safe_cell
        with transaction(engine) as db:
            actor = actor_for(request, db)
            require_permission(actor, "audit.read")
            cap = AUDIT_EXPORT_MAX
            total = db.scalar(select(func.count()).select_from(audit_query(db, actor, f)[0].limit(cap + 1).subquery()))
            department = db.get(m.Department, actor.department_id).name
        span = f"{f['start'] or 'start'}-to-{f['end'] or 'now'}"
        filename = re.sub(r"[^A-Za-z0-9._-]+", "-", f"decision-history-{department}-{span}").strip("-") + ".csv"

        def rows():
            out = io.StringIO(newline="")
            writer = csv.writer(out, quoting=csv.QUOTE_ALL)
            def flush():
                chunk = out.getvalue(); out.seek(0); out.truncate(); return chunk
            if total > cap:
                writer.writerow([f"Note: only the newest {cap} matching changes are included. Narrow the dates or filters to export the rest."])
            writer.writerow(["time", "person", "action", "what_changed", "reason", "self_approved", "reference"])
            from .services.audit_log import subjects
            offset = 0
            while offset < cap:
                with transaction(engine) as db:
                    allow_long_idle(db, 30)
                    query, zone = audit_query(db, actor, f)
                    batch = list(db.execute(query.offset(offset).limit(min(1000, cap - offset))))
                    # what_changed is the on-screen sentence (packages/contracts/audit.py), subjects resolved per batch.
                    for (e, name), event in zip(batch, subjects(db, actor.department_id, [
                            {'kind': e.action.split('.', 1)[0], 'action': e.action, 'detail': e.detail, 'target_id': e.target_id}
                            for e, _ in batch])):
                        view = audit_log.absence_audit_mask(record(e))
                        writer.writerow([safe_cell(v) for v in ((e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=s.timezone.utc)).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"), name or e.actor_id,
                                         e.action, event['summary'], view['reason'], "yes" if (e.detail or {}).get("self_approved") else "no", e.correlation_id)])
                # Release the snapshot before yielding so a slow client cannot
                # leave an idle-in-transaction connection holding locks.
                yield flush()
                if len(batch) < min(1000, cap - offset):
                    break
                offset += len(batch)
        return StreamingResponse(rows(), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{filename}"', **({"X-Export-Truncated": str(cap)} if total > cap else {})})

    def limit_command_request(token, *, account=False):
        subject = session_identity(engine, token)
        enforce(engine, "commands", [subject], settings.rate_commands_per_minute, 60,
                "Too many changes in a short time; wait a minute and try again")
        if account:
            enforce(engine, "account-commands", [subject], 60, 3600,
                    "Too many account changes in a short time; try again later")

    @app.post("/api/commands")
    async def command_endpoint(command: s.Command, request: Request):
        require(command.kind not in {'score_import.upload', 'score_import.add_files', 'score_check.upload'},
                'UPLOAD_MULTIPART', 'Upload files through the multipart import endpoints')
        fields = {"correlation_id": request.state.correlation_id, "command_kind": logged_command_kind(command.kind)}
        queued = time.monotonic()
        log.info("command_dispatch_queued", extra=fields)

        def handle():
            log.info("command_handler_started", extra={**fields, "threadpool_wait_ms": round((time.monotonic() - queued) * 1000, 2)})
            limit_command_request(request.cookies.get(cookie_name), account=command.kind.startswith("account."))
            return execute(engine, settings, request.cookies.get(cookie_name), request.headers.get("x-csrf-token"),
                           request.headers.get("idempotency-key"), command, request.state.correlation_id)

        result = await run_in_threadpool(handle)
        request.state.session_authenticated = True
        # Validation receipts can contain a department-sized findings list.
        # Render that JSON off the event loop as well as computing the command.
        return await run_in_threadpool(JSONResponse, result)

    def bulk_read_limit(request, *, write=True, scope="bulk-preview"):
        from .services import bulk_settings
        with transaction(engine) as db:
            actor = actor_for(request, db, write=write)
            rate = bulk_settings.limits(db, actor, settings)["rate_bulk_preview_per_minute"]
            preview_subject = actor.session_id
        enforce(engine, scope, [preview_subject], rate, 60,
                "Too many bulk previews in a short time; wait a minute and try again" if scope == "bulk-preview"
                else "Too many bulk reads in a short time; wait a minute and try again")

    @app.post("/api/commands/preview")
    def command_preview(command: s.Command, request: Request):
        from .services import bulk
        require(command.kind in {"bulk.apply", "department.create"}, "PREVIEW_UNSUPPORTED", "This command has no command preview")
        bulk_read_limit(request)
        def preview(db):
            from .services.contacts import redact_reply
            actor = actor_for(request, db, write=True)
            if command.kind == 'department.create':
                from .services import departments
                return departments.preview(db, actor, command, settings)
            return redact_reply(db, actor, bulk.preview(db, actor, command, settings))
        return retry_write(engine, preview)

    @app.post("/api/account-onboarding/preview")
    def account_onboarding_preview(value: s.AccountOnboarding, request: Request):
        """Review a department's staff selection without issuing invitations."""
        from .services import account_onboarding
        bulk_read_limit(request)
        with transaction(engine) as db:
            return account_onboarding.preview(db, actor_for(request, db, write=True), value, settings)

    @app.post("/api/account-onboarding/apply")
    def account_onboarding_apply(value: s.AccountOnboardingApply, request: Request):
        """Apply a reviewed selection and download only the invitations issued by this request."""
        from .services import account_onboarding
        token, csrf = request.cookies.get(cookie_name), request.headers.get("x-csrf-token")
        limit_command_request(token, account=True)
        result = account_onboarding.apply(engine, settings, token, csrf, value, request.state.correlation_id)
        request.state.session_authenticated = True
        return JSONResponse(result, headers={"Cache-Control": "private, no-store",
                                             "Content-Disposition": 'attachment; filename="staff-invitations.json"'})

    @app.post("/api/bulk/resolve")
    def bulk_resolve(value: s.BulkSelection, request: Request):
        from .services import bulk_selection
        bulk_read_limit(request, scope="bulk-resolve")
        with transaction(engine) as db:
            return bulk_selection.resolve(db, actor_for(request, db, write=True), value)

    @app.get('/api/search/references')
    def search_references(request: Request, collection: str, kind: s.SearchEntityKind,
                          q: str = Query('', max_length=500), limit: int = Query(20, ge=1, le=100)):
        from .services.search import suggestions
        bulk_read_limit(request, write=False, scope='bulk-resolve')
        with transaction(engine) as db:
            return suggestions(db, actor_for(request, db), collection, kind, q, limit)

    @app.get("/api/conditions/catalogue")
    def condition_catalogue(request: Request, context: s.ConditionContext = "filter", as_of: date | None = None,
                            workspace_id: s.ID | None = None, duty_day: date | None = None,
                            collection: s.BulkCollection | None = None,
                            table_key: s.ViewTableKey | None = None,
                            field: s.CollectionConditionField | None = None,
                            target_id: s.ID | None = None):
        from .services import condition_context
        bulk_read_limit(request, write=False, scope="bulk-resolve")
        with transaction(engine) as db:
            return condition_context.catalogue(db, actor_for(request, db), context=context, as_of=as_of,
                workspace_id=workspace_id, duty_day=duty_day, collection=collection, table_key=table_key,
                field=field, target_id=target_id)

    @app.post("/api/conditions/resolve-operand", response_model=s.ConditionResolution)
    def condition_resolve(value: s.ConditionResolveInput, request: Request):
        from .services import condition_context
        bulk_read_limit(request, scope="bulk-resolve")
        with transaction(engine) as db:
            return condition_context.resolve(db, actor_for(request, db, write=True), value)

    @app.post("/api/conditions/resolve-operands", response_model=s.ConditionResolveBatchResult, response_model_exclude_none=True)
    def condition_resolve_batch(value: s.ConditionResolveBatchInput, request: Request):
        from .services import condition_context
        bulk_read_limit(request, scope="bulk-resolve")
        with transaction(engine) as db:
            return condition_context.resolve_batch(db, actor_for(request, db, write=True), value)

    @app.post("/api/conditions/parse-date", response_model=s.ConditionDateParseResult)
    def condition_parse_date(value: s.ConditionDateParseInput, request: Request):
        from .services import condition_context
        bulk_read_limit(request, scope="bulk-resolve")
        with transaction(engine) as db:
            return condition_context.parse_date(db, actor_for(request, db, write=True), value)

    @app.post("/api/bulk/rows")
    def bulk_rows(value: s.BulkRowsInput, request: Request):
        from .services import bulk_selection
        from .services.conditional_reads import conditional_json
        bulk_read_limit(request, scope="bulk-rows")
        with transaction(engine) as db:
            actor = actor_for(request, db, write=True)
            return conditional_json(request, bulk_selection.rows(db, actor, value), actor)

    @app.get("/api/bulk/rows")
    def bulk_rows_get(request: Request, query: str = Query(..., max_length=65536)):
        from .services import bulk_selection
        from .services.conditional_reads import conditional_json
        try:
            value = s.BulkRowsInput.model_validate_json(query)
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from error
        bulk_read_limit(request, write=False, scope="bulk-rows")
        with transaction(engine) as db:
            actor = actor_for(request, db)
            return conditional_json(request, bulk_selection.rows(db, actor, value), actor)

    @app.get("/api/bulk/collections", response_model=s.BulkCollections)
    def bulk_collections(request: Request):
        from .services import bulk_selection
        bulk_read_limit(request, write=False, scope="bulk-collections")
        with transaction(engine) as db:
            return bulk_selection.collections(db, actor_for(request, db))

    @app.get("/api/bulk/settings")
    def bulk_limits(request: Request):
        from .services import bulk_settings
        with transaction(engine) as db:
            actor = actor_for(request, db)
            return {"settings": bulk_settings.limits(db, actor, settings),
                    "department_revision": db.get(m.Department, actor.department_id).revision}

    @app.get("/api/bulk/staged")
    def bulk_staged_list(request: Request):
        from .services import bulk_staged
        return retry_write(engine, lambda db: bulk_staged.list_mine(db, actor_for(request, db), settings))

    @app.post("/api/bulk/staged/{staged_id}/reopen")
    def bulk_staged_reopen(staged_id: s.ID, request: Request):
        from .services import bulk_staged
        bulk_read_limit(request)
        return retry_write(engine, lambda db: bulk_staged.reopen(db, actor_for(request, db, write=True), staged_id, settings))

    @app.get("/api/commands/{operation_key}")
    def command_status(operation_key: str, request: Request):
        with transaction(engine) as db:
            actor = actor_for(request, db)
            receipt = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == operation_key,
                m.CommandReceipt.actor_id == actor.identity_id, m.CommandReceipt.department_id == actor.department_id))
            require(receipt is not None, "RECEIPT_NOT_FOUND", "No committed receipt was found for this operation key", 404)
            authorize_receipt(db, actor, receipt)
            return {"status": "committed", "result": receipt_result(receipt.command_kind, receipt.result,
                                                                  db=db, actor=actor, settings=settings)}

    @app.get("/api/contracts")
    def contracts(request: Request):
        from .services.bulk_selection import collection_readers
        with transaction(engine) as db:
            actor_for(request, db)
        types = [s.AssignmentTypeInput, s.PolicyInput, s.SeriesInput, s.ShiftInput, s.Command, s.MembershipInput, s.GrantInput, s.ReviewRouteInput, s.ReviewDelegationInput,
                 s.ReviewDecisionInput, s.SwapOfferInput, s.SwapCounterofferInput, s.SwapPrepareInput, s.PlannerViewInput,
                 s.FeedbackCreate, s.FeedbackUpdate, s.FeedbackUndo, s.FeedbackExport,
                 s.DateSetInput, s.DateSetDeleteInput, s.DateSetEntryWriteInput, s.DateSetEntryDeleteInput,
                 s.DateSetAdjustmentInput, s.DateSetImportInput, s.DateSetCopyInput, s.ResolvedDateSetEntry,
                 s.RotationBlock, s.RotationTemplateInput, s.RotationPlanInput, s.RotationCopyInput, s.RotationRemoveInput,
                 s.RotationChangeInput, s.TraineeNoteInput, s.TraineeNoteEdit,
                 s.GithubIntegrationStatus, s.GithubConnect, s.GithubDisconnect, s.GithubTest,
                 s.BulkApply, s.BulkApplyResult, s.BulkPreview, s.BulkSelection, s.BulkResolved,
                 s.BulkRowsInput, s.BulkRows, s.BulkStageInput, s.BulkStagedRow, s.BulkStagedList,
                 s.BulkSettings, s.BulkSettingsInput, s.AvailabilityUpdate, s.AvailabilityUndoUpdate,
                  s.AvailabilityUndoCreate, s.DraftMove, s.ShiftAction, s.PolicyVersionApproval, s.ExceptionApproval,
                 s.RelationshipUpdateDates, s.ConditionResolveInput, s.ConditionResolution,
                 s.ConditionResolveBatchInput, s.ConditionResolveBatchResult, s.ConditionDateParseInput, s.ConditionDateParseResult]
        return {"schema_version": 1, "schemas": {c.__name__: c.model_json_schema() for c in types},
                "condition_fields": s.CONDITION_FIELDS,
                "table_condition_definitions": s.table_condition_registry(),
                "condition_field_definitions": s.CONDITION_FIELD_DEFINITIONS, "generator": MANIFEST,
                "bulk_item_kinds": s.BULK_ITEM_KINDS, "bulk_collections": s.bulk_collection_registry(command_roles, collection_readers)}

    @app.get("/api/openapi.json")
    def openapi(request: Request):
        with transaction(engine) as db:
            actor_for(request, db)
        return app.openapi()

    web = WEB
    if (web / "assets").is_dir():
        app.mount("/assets", IndependentStaticFiles(directory=web / "assets",
            cache_seconds=settings.static_cache_seconds, precompressed=settings.static_precompressed), name="assets")

    @app.get("/sw.js")
    async def service_worker():
        """The installable app's worker (served from the root so it covers the whole app; it never stores API data)."""
        require(await background_io((web / "sw.js").is_file), "WEB_NOT_BUILT", "Run the frontend build script", 503)
        return IndependentFileResponse(web / "sw.js", media_type="text/javascript", headers={'Cache-Control': 'no-cache'})

    async def index():
        require(await background_io((web / "index.html").is_file), "WEB_NOT_BUILT", "Run the frontend build script before opening the application", 503)
        return IndependentFileResponse(web / "index.html")

    @app.get("/")
    async def sign_in_page(request: Request, state: str = ""):
        if state:
            expected = request.cookies.get(oidc_logout_cookie, "")
            require(len(state) <= 500 and expected and secrets.compare_digest(expected, digest(state)),
                    "OIDC_LOGOUT_STATE", "Sign-out return did not match this browser", 401)
            response = RedirectResponse("/", status_code=303)
            response.delete_cookie(oidc_logout_cookie, path="/", secure=secure, httponly=True, samesite="lax")
            return response
        return await index()

    @app.get("/n/{token}")
    @app.get("/invite/{token}")
    @app.get("/reset/{token}")
    async def account_link_page(token: str):
        return await index()  # the app reads the token from the address and asks the API about it

    return app


configure_logging()
app = create_app()
