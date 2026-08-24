"""Read-only подключение кабинетов маркетплейсов в изолированном Seller workspace."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from domains.local_auth import AuthenticatedUser
from domains.marketplace_connection_verification import discover_yandex_market_stores, verify_ozon_connection


ProviderCode = Literal["ozon", "yandex_market"]


class MarketplaceConnectionDiscoverIn(BaseModel):
    provider_code: ProviderCode
    token: str = Field(min_length=8, max_length=4096)
    client_id: str = Field(default="", max_length=128)


class MarketplaceConnectionCreateIn(MarketplaceConnectionDiscoverIn):
    display_name: str = Field(min_length=1, max_length=120)
    business_id: int | None = Field(default=None, gt=0)
    campaign_id: int | None = Field(default=None, gt=0)


class MarketplaceStoreCandidateOut(BaseModel):
    business_id: int
    campaign_id: int
    display_name: str


class MarketplaceConnectionDiscoverOut(BaseModel):
    provider_code: ProviderCode
    items: list[MarketplaceStoreCandidateOut]


class MarketplaceConnectionOut(BaseModel):
    id: int
    provider_code: ProviderCode
    display_name: str
    client_id: str = ""
    business_id: str = ""
    campaign_id: str = ""
    token_masked: str
    status: str
    last_checked_at: datetime | None = None
    last_successful_sync_at: datetime | None = None
    last_error: str = ""
    created_at: datetime


class MarketplaceConnectionListOut(BaseModel):
    workspace_name: str
    items: list[MarketplaceConnectionOut]


def mount_marketplace_connection_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Подключает маршруты кабинетов без смешивания их с автономной авторизацией."""

    def credentials_secret() -> str:
        # Проверяет отдельный ключ шифрования до записи токена, чтобы не создать нечитаемые подключения.
        value = str(os.getenv("MARKETPLACE_CREDENTIALS_SECRET", "")).strip()
        if len(value) < 32:
            raise HTTPException(status_code=503, detail="Не настроено защищённое хранение токенов маркетплейсов")
        return value

    def token_mask(token_suffix: str) -> str:
        # Возвращает только короткий хвост ключа, чтобы оператор узнал магазин без раскрытия реквизита.
        suffix = str(token_suffix or "").strip()[-4:]
        return f"••••{suffix}" if suffix else "••••"

    def connection_out(row) -> MarketplaceConnectionOut:
        # Собирает контракт карточки магазина из безопасных полей, исключая ciphertext из ответа API.
        return MarketplaceConnectionOut(
            id=int(row[0]),
            provider_code=str(row[1]),
            display_name=str(row[2]),
            client_id=str(row[3] or ""),
            business_id=str(row[4] or ""),
            campaign_id=str(row[5] or ""),
            token_masked=token_mask(str(row[6] or "")),
            status=str(row[7]),
            last_checked_at=row[8],
            last_error=str(row[9] or ""),
            last_successful_sync_at=row[10],
            created_at=row[11],
        )

    def workspace_for_user(connection, user: AuthenticatedUser):
        # Получает единственное активное пространство текущей сессии и не принимает workspace от клиента.
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        return seller_user

    @app.get("/marketplaces/connections", response_model=MarketplaceConnectionListOut)
    def list_connections(user: AuthenticatedUser = Depends(current_user)) -> MarketplaceConnectionListOut:
        # Отдаёт только магазины организации текущего пользователя, сохраняя изоляцию будущих тарифов.
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, provider_code, display_name, client_id, business_id, campaign_id,
                           token_suffix, status, last_checked_at, last_error,
                           last_successful_sync_at, created_at
                    FROM seller.marketplace_connections
                    WHERE workspace_id=%s
                    ORDER BY created_at DESC, id DESC
                    """,
                    (seller_user.workspace_id,),
                )
                rows = cursor.fetchall()
        return MarketplaceConnectionListOut(
            workspace_name=seller_user.workspace_name,
            items=[connection_out(row) for row in rows],
        )

    @app.post("/marketplaces/connections/discover", response_model=MarketplaceConnectionDiscoverOut)
    def discover_connection(
        payload: MarketplaceConnectionDiscoverIn,
        _user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceConnectionDiscoverOut:
        # Проверяет ключ до записи: Ozon подтверждает пару ключей, Маркет показывает доступные кабинеты.
        token = str(payload.token).strip()
        if payload.provider_code == "ozon":
            client_id = str(payload.client_id).strip()
            if not client_id:
                raise HTTPException(status_code=400, detail="Для Ozon укажите Client ID кабинета")
            verify_ozon_connection(client_id=client_id, token=token)
            return MarketplaceConnectionDiscoverOut(provider_code="ozon", items=[])
        stores = discover_yandex_market_stores(token=token)
        return MarketplaceConnectionDiscoverOut(
            provider_code="yandex_market",
            items=[MarketplaceStoreCandidateOut(**store) for store in stores],
        )

    @app.post("/marketplaces/connections", response_model=MarketplaceConnectionOut, status_code=201)
    def create_connection(
        payload: MarketplaceConnectionCreateIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceConnectionOut:
        # Сохраняет только успешно проверенный read-only доступ и шифрует токен до появления будущих синхронизаций.
        token = str(payload.token).strip()
        display_name = str(payload.display_name).strip()
        client_id = str(payload.client_id).strip()
        business_id = str(payload.business_id or "")
        campaign_id = str(payload.campaign_id or "")
        if payload.provider_code == "ozon":
            if not client_id:
                raise HTTPException(status_code=400, detail="Для Ozon укажите Client ID кабинета")
            verify_ozon_connection(client_id=client_id, token=token)
        else:
            if not business_id or not campaign_id:
                raise HTTPException(status_code=400, detail="Сначала выберите магазин Яндекс Маркета")
            available_stores = discover_yandex_market_stores(token=token)
            selected = {(str(item["business_id"]), str(item["campaign_id"])) for item in available_stores}
            if (business_id, campaign_id) not in selected:
                raise HTTPException(status_code=400, detail="Выбранный магазин больше не доступен этому API-Key")
        secret = credentials_secret()
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO seller.marketplace_connections(
                        workspace_id, provider_code, display_name, client_id, business_id, campaign_id,
                        token_ciphertext, token_suffix, status, last_checked_at, last_error, created_by_user_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        pgp_sym_encrypt(%s, %s, 'cipher-algo=aes256, compress-algo=0'),
                        %s, 'active', now(), '', %s
                    )
                    ON CONFLICT (workspace_id, provider_code, client_id, campaign_id)
                    DO UPDATE SET
                        display_name=EXCLUDED.display_name,
                        business_id=EXCLUDED.business_id,
                        token_ciphertext=EXCLUDED.token_ciphertext,
                        token_suffix=EXCLUDED.token_suffix,
                        status='active',
                        last_checked_at=now(),
                        last_error='',
                        updated_at=now()
                    RETURNING id, provider_code, display_name, client_id, business_id, campaign_id,
                              token_suffix, status, last_checked_at, last_error,
                              last_successful_sync_at, created_at
                    """,
                    (
                        seller_user.workspace_id,
                        payload.provider_code,
                        display_name,
                        client_id,
                        business_id,
                        campaign_id,
                        token,
                        secret,
                        token[-4:],
                        seller_user.id,
                    ),
                )
                row = cursor.fetchone()
        return connection_out(row)

    @app.post("/marketplaces/connections/{connection_id}/disable", response_model=MarketplaceConnectionOut)
    def disable_connection(
        connection_id: int,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceConnectionOut:
        # Отключает кабинет обратимо и только в текущем workspace, не удаляя будущую историю синхронизаций.
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.marketplace_connections
                    SET status='disabled', updated_at=now()
                    WHERE id=%s AND workspace_id=%s
                    RETURNING id, provider_code, display_name, client_id, business_id, campaign_id,
                              token_suffix, status, last_checked_at, last_error,
                              last_successful_sync_at, created_at
                    """,
                    (connection_id, seller_user.workspace_id),
                )
                row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Подключенный магазин не найден")
        return connection_out(row)

    @app.post("/marketplaces/connections/{connection_id}/enable", response_model=MarketplaceConnectionOut)
    def enable_connection(
        connection_id: int,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceConnectionOut:
        # Повторно проверяет сохранённый ключ перед включением, не заставляя пользователя вводить его заново.
        secret = credentials_secret()
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            workspace_id = seller_user.workspace_id
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT provider_code, client_id, business_id, campaign_id,
                           pgp_sym_decrypt(token_ciphertext, %s)
                    FROM seller.marketplace_connections
                    WHERE id=%s AND workspace_id=%s
                    """,
                    (secret, connection_id, workspace_id),
                )
                saved_connection = cursor.fetchone()
        if not saved_connection:
            raise HTTPException(status_code=404, detail="Подключенный магазин не найден")

        provider_code, client_id, business_id, campaign_id, token = saved_connection
        token = str(token or "")
        if provider_code == "ozon":
            verify_ozon_connection(client_id=str(client_id or ""), token=token)
        else:
            available_stores = discover_yandex_market_stores(token=token)
            available_store_ids = {
                (str(item["business_id"]), str(item["campaign_id"])) for item in available_stores
            }
            if (str(business_id or ""), str(campaign_id or "")) not in available_store_ids:
                raise HTTPException(
                    status_code=400,
                    detail="Сохранённый API-ключ больше не даёт доступ к этому магазину",
                )

        with psycopg.connect(database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.marketplace_connections
                    SET status='active', last_checked_at=now(), last_error='', updated_at=now()
                    WHERE id=%s AND workspace_id=%s
                    RETURNING id, provider_code, display_name, client_id, business_id, campaign_id,
                              token_suffix, status, last_checked_at, last_error,
                              last_successful_sync_at, created_at
                    """,
                    (connection_id, workspace_id),
                )
                row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Подключенный магазин не найден")
        return connection_out(row)
