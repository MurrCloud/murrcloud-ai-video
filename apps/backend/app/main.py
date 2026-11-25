import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.dependencies import audit, current_user, editor, get_db
from app.limits import LimiterUnavailable, RateLimitExceeded, RedisLimiter
from app.middleware import BoundedBodyMiddleware
from app.models import AuthSession, Project, ProjectSettings, User
from app.schemas import LoginIn, ProjectIn, ProjectOut, UserOut
from app.security import (
    DUMMY_PASSWORD_HASH,
    PASSWORD_SLOTS,
    SESSION_COOKIE,
    new_session,
    verify_password,
)

logger = logging.getLogger("video.api")
API = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    engine=None,
    limiter=None,
    provider_factory=None,
    generation_dispatcher=None,
    director_factory=None,
    director_dispatcher=None,
    production_dispatcher=None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine if engine is not None else make_engine(settings.database_url)
    limiter = limiter if limiter is not None else RedisLimiter(settings.redis_url)

    @asynccontextmanager
    async def lifespan(application):
        yield
        engine.dispose()
        limiter.close()

    application = FastAPI(
        title="MurrCloud AI Video Studio",
        version="0.5.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.environment != "production" else None,
    )
    application.state.settings = settings
    application.state.engine = engine
    application.state.sessions = make_session_factory(engine)
    application.state.limiter = limiter
    from app.runtime import (
        dispatch_director,
        dispatch_generation,
        dispatch_production,
        make_director,
        make_provider,
    )

    application.state.provider_factory = provider_factory or make_provider
    application.state.generation_dispatcher = generation_dispatcher or dispatch_generation
    application.state.director_factory = director_factory or make_director
    application.state.director_dispatcher = director_dispatcher or dispatch_director
    application.state.production_dispatcher = production_dispatcher or dispatch_production
    application.state.image_slots = asyncio.Semaphore(1)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    application.add_middleware(BoundedBodyMiddleware)

    @application.middleware("http")
    async def request_guards(request: Request, call_next):
        request_id = secrets.token_hex(12)
        # Never trust arbitrary X-Forwarded-For. Configure the server's trusted
        # proxy list only after the actual reverse proxy path has been reviewed.
        client = request.client.host if request.client else "unknown"
        try:
            if request.url.path.startswith(API):
                await run_in_threadpool(
                    limiter.check, "api:" + client, settings.api_requests_per_minute
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if request.cookies.get(SESSION_COOKIE) and origin not in settings.allowed_origins:
                    return JSONResponse({"detail": "Allowed Origin header required"}, 403)
                if origin is not None and origin not in settings.allowed_origins:
                    return JSONResponse({"detail": "Origin not allowed"}, 403)
            response = await call_next(request)
        except RateLimitExceeded:
            response = JSONResponse(
                {"detail": "Rate limit exceeded"}, 429, headers={"Retry-After": "60"}
            )
        except LimiterUnavailable:
            response = JSONResponse({"detail": "Service temporarily unavailable"}, 503)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'"
            )
        return response

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        if request.url.path.startswith(API + "/settings/provider-keys"):
            # Even an unknown JSON field name or URL component may itself be a
            # pasted secret. Return no attacker-controlled validation locations.
            return JSONResponse({"detail": "Invalid provider key request"}, 422)
        # Pydantic's default error includes the rejected input, potentially a password.
        return JSONResponse(
            {
                "detail": [
                    {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ]
            },
            422,
        )

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        # Do not log raw SQL exceptions: they can contain prompts/credential URLs.
        logger.error("Database operation failed (%s)", type(exc).__name__)
        return JSONResponse({"detail": "Database temporarily unavailable"}, 503)

    @application.get("/health/live", include_in_schema=False)
    def live():
        return {"status": "ok"}

    @application.get("/health/ready", include_in_schema=False)
    def ready(db: Session = Depends(get_db)):
        try:
            revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            # Keep this aligned with the migration head; a DB merely reachable
            # but not migrated must never be advertised as ready.
            from app.migration_head import SCHEMA_HEAD

            if revision != SCHEMA_HEAD or not limiter.ping():
                raise ValueError("Dependency not ready")
        except Exception:
            return JSONResponse({"status": "unavailable"}, 503)
        return {"status": "ready"}

    @application.post(API + "/auth/login", response_model=UserOut)
    def login(
        payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)
    ):
        email = str(payload.email).lower()
        client = request.client.host if request.client else "unknown"
        try:
            limiter.check("login-ip:" + client, settings.login_attempts_per_minute)
            limiter.check("login-email:" + email, settings.login_attempts_per_minute)
        except RateLimitExceeded:
            raise HTTPException(
                429, "Too many login attempts", headers={"Retry-After": "60"}
            ) from None
        except LimiterUnavailable:
            raise HTTPException(503, "Service temporarily unavailable") from None
        if not PASSWORD_SLOTS.acquire(blocking=False):
            raise HTTPException(429, "Please retry shortly", headers={"Retry-After": "5"})
        try:
            user = db.scalar(select(User).where(User.email == email).with_for_update())
            valid = verify_password(
                payload.password, user.password_hash if user else DUMMY_PASSWORD_HASH
            )
        finally:
            PASSWORD_SLOTS.release()
        if not user or not valid or not user.active:
            audit(db, "auth.login_failed")
            db.commit()
            raise HTTPException(401, "Invalid email or password")
        token, digest, expiry = new_session(
            settings.session_secret.get_secret_value(), settings.session_hours
        )
        db.add(AuthSession(user_id=user.id, token_hash=digest, expires_at=expiry))
        audit(db, "auth.login", user)
        db.commit()
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path=API,
        )
        return user

    @application.post(API + "/auth/logout", status_code=204)
    def logout(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
        request.state.auth_session.revoked_at = datetime.now(UTC)
        audit(db, "auth.logout", user)
        db.commit()
        response = Response(status_code=204)
        response.delete_cookie(
            SESSION_COOKIE,
            path=API,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @application.get(API + "/auth/me", response_model=UserOut)
    def me(user: User = Depends(current_user)):
        return user

    @application.post(API + "/projects", response_model=ProjectOut, status_code=201)
    def create_project(
        payload: ProjectIn, user: User = Depends(editor), db: Session = Depends(get_db)
    ):
        project = Project(
            owner_id=user.id,
            name=payload.name,
            prompt=payload.prompt,
            target_duration_seconds=payload.target_duration_seconds,
            budget_usd=payload.budget_usd,
        )
        db.add(project)
        db.flush()
        db.add(
            ProjectSettings(
                project_id=project.id, aspect_ratio=payload.aspect_ratio, language=payload.language
            )
        )
        audit(db, "project.created", user, project)
        db.commit()
        return project

    @application.get(API + "/projects", response_model=list[ProjectOut])
    def projects(
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
    ):
        query = select(Project).where(Project.archived.is_(False))
        if user.role != "admin":
            query = query.where(Project.owner_id == user.id)
        return db.scalars(
            query.order_by(Project.created_at.desc(), Project.id).offset(offset).limit(limit)
        ).all()

    @application.get(API + "/projects/{project_id}", response_model=ProjectOut)
    def project_detail(
        project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)
    ):
        query = select(Project).where(Project.id == project_id)
        if user.role != "admin":
            query = query.where(Project.owner_id == user.id)
        project = db.scalar(query)
        if not project:
            raise HTTPException(404, "Project not found")
        return project

    from app.asset_api import router as asset_router
    from app.catalog_api import router as catalog_router
    from app.generation_api import router as generation_router
    from app.ingestion_api import router as ingestion_router
    from app.library_api import router as library_router
    from app.production_api import router as production_router
    from app.provider_keys_api import router as provider_keys_router
    from app.reconciliation_api import router as reconciliation_router
    from app.reports_api import router as reports_router
    from app.storyboard_api import router as storyboard_router
    from app.workspace_api import router as workspace_router

    application.include_router(catalog_router)
    application.include_router(generation_router)
    application.include_router(asset_router)
    application.include_router(ingestion_router)
    application.include_router(storyboard_router)
    application.include_router(production_router)
    application.include_router(library_router)
    application.include_router(reports_router)
    application.include_router(workspace_router)
    application.include_router(provider_keys_router)
    application.include_router(reconciliation_router)
    return application


app = create_app()
