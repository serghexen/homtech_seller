"""Точка входа независимого API HomTech Seller."""

from __future__ import annotations

import os

import psycopg
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from domains.local_auth import AuthenticatedUser, create_access_token, decode_access_token, hash_password, normalize_email, verify_password
from domains.marketplace_connections_api import mount_marketplace_connection_routes
from domains.marketplace_read_api import mount_marketplace_read_routes
from domains.marketplace_sync_jobs_api import mount_marketplace_sync_job_routes


app = FastAPI(title="HomTech Seller API", version="0.0.21")


def cors_origins() -> list[str]:
    # Разрешает локальную разработку интерфейса без открытия API любым внешним сайтам.
    configured = str(os.getenv("CORS_ORIGINS", "")).strip()
    if configured:
        return [item.strip() for item in configured.split(",") if item.strip()]
    return ["http://localhost:5173", "http://127.0.0.1:5173"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(default="", max_length=120)
    workspace_name: str = Field(default="", max_length=160)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class SellerUserOut(BaseModel):
    id: int
    email: str
    display_name: str
    workspace_id: int
    workspace_name: str
    role_code: str


class AuthOut(BaseModel):
    user: SellerUserOut


def check_database() -> None:
    # Проверяет доступность отдельной БД без изменения схемы или данных.
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    with psycopg.connect(database_url, connect_timeout=3) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")


def database_url() -> str:
    # Возвращает обязательный URL собственной БД, не используя конфигурацию CRM.
    value = str(os.getenv("DATABASE_URL", "")).strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required")
    return value


def auth_secret() -> str:
    # Берёт отдельный секрет сессий Seller, который никогда не совпадает с JWT-секретом CRM.
    value = str(os.getenv("SELLER_AUTH_JWT_SECRET", "")).strip()
    if len(value) < 32:
        raise HTTPException(status_code=503, detail="Seller authentication is not configured")
    return value


def auth_ttl_minutes() -> int:
    # Ограничивает срок локальной сессии, чтобы скомпрометированный cookie не был бессрочным.
    return max(15, min(int(os.getenv("SELLER_AUTH_JWT_TTL_MIN", "720")), 43_200))


def cookie_is_secure() -> bool:
    # Не включает Secure локально, но требует его на HTTPS-стенде и production.
    return str(os.getenv("SELLER_COOKIE_SECURE", "true")).strip().lower() in {"1", "true", "yes"}


def user_with_workspace(connection, user_id: int) -> SellerUserOut | None:
    # Находит владельца или участника организации и не позволяет выбрать workspace из параметров запроса.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_row.id, user_row.email, user_row.display_name,
                   workspace.id, workspace.name, member.role_code
            FROM seller.users AS user_row
            JOIN seller.workspace_members AS member ON member.user_id=user_row.id
            JOIN seller.workspaces AS workspace ON workspace.id=member.workspace_id
            WHERE user_row.id=%s AND user_row.is_active=true
            ORDER BY CASE member.role_code WHEN 'owner' THEN 0 ELSE 1 END, workspace.id
            LIMIT 1
            """,
            (user_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    return SellerUserOut(
        id=int(row[0]),
        email=str(row[1]),
        display_name=str(row[2] or ""),
        workspace_id=int(row[3]),
        workspace_name=str(row[4]),
        role_code=str(row[5]),
    )


def issue_session(response: Response, user: SellerUserOut) -> AuthOut:
    # Создаёт short-lived сессию в HttpOnly cookie, чтобы JavaScript не хранил токен в localStorage.
    token = create_access_token(
        user_id=user.id,
        email=user.email,
        secret=auth_secret(),
        ttl_minutes=auth_ttl_minutes(),
    )
    response.set_cookie(
        key="seller_session",
        value=token,
        max_age=auth_ttl_minutes() * 60,
        httponly=True,
        secure=cookie_is_secure(),
        samesite="lax",
        path="/",
    )
    return AuthOut(user=user)


def current_user(
    authorization: str | None = Header(default=None),
    seller_session: str | None = Cookie(default=None),
) -> AuthenticatedUser:
    # Проверяет bearer-токен для API-клиентов либо защищённый cookie для веб-интерфейса.
    bearer = authorization.removeprefix("Bearer ").strip() if authorization else ""
    token = bearer or str(seller_session or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        return decode_access_token(token, secret=auth_secret())
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid session") from exc


@app.get("/health")
def health() -> dict[str, str]:
    # Отдаёт готовность API только после проверки соединения с его собственной БД.
    try:
        check_database()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is unavailable") from exc
    return {"status": "ok", "service": "homtech-seller-api"}


@app.post("/auth/register", response_model=AuthOut, status_code=201)
def register(payload: RegisterIn, response: Response) -> AuthOut:
    # Создаёт локальный аккаунт и внутреннюю рабочую область одной транзакцией для изоляции данных.
    try:
        email = normalize_email(payload.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid email") from exc
    display_name = str(payload.display_name or "").strip()
    workspace_name = str(payload.workspace_name or "").strip() or display_name or "Мой кабинет"
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO seller.users(email, display_name, password_hash)
                VALUES (%s, %s, %s)
                ON CONFLICT (email) DO NOTHING
                RETURNING id
                """,
                (email, display_name, hash_password(payload.password)),
            )
            user_row = cursor.fetchone()
            if not user_row:
                raise HTTPException(status_code=409, detail="Account already exists")
            user_id = int(user_row[0])
            cursor.execute(
                "INSERT INTO seller.workspaces(name, owner_user_id) VALUES (%s, %s) RETURNING id",
                (workspace_name, user_id),
            )
            workspace_id = int(cursor.fetchone()[0])
            cursor.execute(
                "INSERT INTO seller.workspace_members(workspace_id, user_id, role_code) VALUES (%s, %s, 'owner')",
                (workspace_id, user_id),
            )
        user = user_with_workspace(connection, user_id)
    if not user:
        raise HTTPException(status_code=500, detail="Workspace was not created")
    return issue_session(response, user)


@app.post("/auth/login", response_model=AuthOut)
def login(payload: LoginIn, response: Response) -> AuthOut:
    # Проверяет пароль только в самостоятельной базе Seller и возвращает локальную защищённую сессию.
    try:
        email = normalize_email(payload.email)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Неверный email или пароль") from exc
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, password_hash FROM seller.users WHERE email=%s AND is_active=true", (email,))
            row = cursor.fetchone()
        if not row or not verify_password(payload.password, str(row[1] or "")):
            raise HTTPException(status_code=401, detail="Неверный email или пароль")
        user = user_with_workspace(connection, int(row[0]))
    if not user:
        raise HTTPException(status_code=403, detail="Workspace access is unavailable")
    return issue_session(response, user)


@app.get("/auth/me", response_model=AuthOut)
def me(user: AuthenticatedUser = Depends(current_user)) -> AuthOut:
    # Возвращает актуальную организацию, чтобы после будущего SSO права всегда проверялись в Seller.
    with psycopg.connect(database_url()) as connection:
        seller_user = user_with_workspace(connection, user.user_id)
    if not seller_user:
        raise HTTPException(status_code=401, detail="Account is inactive")
    return AuthOut(user=seller_user)


@app.post("/auth/logout", status_code=204)
def logout() -> Response:
    # Удаляет только локальную сессию Seller, не затрагивая будущую общую SSO-сессию.
    response = Response(status_code=204)
    response.delete_cookie(
        key="seller_session",
        path="/",
        secure=cookie_is_secure(),
        httponly=True,
        samesite="lax",
    )
    return response


# Подключает изолированный модуль магазинов после определения локальной авторизации и workspace-прав.
mount_marketplace_connection_routes(
    app,
    database_url=database_url,
    psycopg=psycopg,
    current_user=current_user,
    user_with_workspace=user_with_workspace,
)

# Подключает снимки каталога и заказов после авторизации, сохраняя их внутри отдельного Seller workspace.
mount_marketplace_read_routes(
    app,
    database_url=database_url,
    psycopg=psycopg,
    current_user=current_user,
    user_with_workspace=user_with_workspace,
)

# HTTP только ставит синхронизацию в PostgreSQL-очередь; внешние API вызывает отдельный worker.
mount_marketplace_sync_job_routes(
    app,
    database_url=database_url,
    psycopg=psycopg,
    current_user=current_user,
    user_with_workspace=user_with_workspace,
)
