"""Явные действия пользователя над карточками подключённого маркетплейса."""

from __future__ import annotations

import os
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from domains.local_auth import AuthenticatedUser
from domains.marketplace_catalog_service import update_yandex_catalog_archive


class MarketplaceCatalogArchiveIn(BaseModel):
    connection_id: int = Field(gt=0)
    external_product_id: str = Field(min_length=1, max_length=256)
    archived: bool


class MarketplaceCatalogArchiveOut(BaseModel):
    connection_id: int
    external_product_id: str
    archived: bool


def mount_marketplace_catalog_action_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Подключает только подтверждённые пользователем изменения карточек."""

    def credentials_secret() -> str:
        value = str(os.getenv("MARKETPLACE_CREDENTIALS_SECRET", "")).strip()
        if len(value) < 32:
            raise HTTPException(status_code=503, detail="Не настроено защищённое чтение токена маркетплейса")
        return value

    @app.post("/marketplaces/catalog/archive", response_model=MarketplaceCatalogArchiveOut)
    def set_catalog_archive(
        payload: MarketplaceCatalogArchiveIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceCatalogArchiveOut:
        # Сначала проверяет workspace и роль, затем вызывает Яндекс без удержания транзакции БД.
        product_id = str(payload.external_product_id or "").strip()
        with psycopg.connect(database_url()) as connection:
            seller_user = user_with_workspace(connection, user.user_id)
            if not seller_user:
                raise HTTPException(status_code=401, detail="Рабочая область недоступна")
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для изменения карточки")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT marketplace_connection.provider_code,
                           marketplace_connection.business_id,
                           pgp_sym_decrypt(marketplace_connection.token_ciphertext, %s),
                           item.offer_id, item.is_archived
                    FROM seller.catalog_items AS item
                    JOIN seller.marketplace_connections AS marketplace_connection
                      ON marketplace_connection.id=item.connection_id
                    WHERE item.connection_id=%s AND item.external_product_id=%s
                      AND item.is_present=true
                      AND marketplace_connection.workspace_id=%s
                      AND marketplace_connection.status='active'
                    LIMIT 1
                    """,
                    (credentials_secret(), payload.connection_id, product_id, seller_user.workspace_id),
                )
                row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Карточка или активный магазин не найдены")
        provider_code, business_id, token, offer_id, current_archived = row
        if str(provider_code) != "yandex_market":
            raise HTTPException(status_code=400, detail="Архивирование пока доступно только для Яндекс Маркета")
        if not str(business_id or "").isdigit():
            raise HTTPException(status_code=409, detail="У подключения не указан кабинет Яндекс Маркета")
        if bool(current_archived) == payload.archived:
            return MarketplaceCatalogArchiveOut(
                connection_id=payload.connection_id,
                external_product_id=product_id,
                archived=payload.archived,
            )

        update_yandex_catalog_archive(
            business_id=int(business_id),
            token=str(token),
            offer_id=str(offer_id),
            archived=payload.archived,
        )

        # Локальный снимок меняется только после успешного ответа Яндекса; повторная синхронизация подтвердит состояние.
        with psycopg.connect(database_url()) as connection:
            seller_user = user_with_workspace(connection, user.user_id)
            if not seller_user:
                raise HTTPException(status_code=401, detail="Рабочая область недоступна")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.catalog_items AS item
                    SET is_archived=%s,
                        archived_at=CASE WHEN %s THEN now() ELSE NULL END,
                        raw_payload=jsonb_set(
                          item.raw_payload,
                          '{offer}',
                          COALESCE(item.raw_payload->'offer', '{}'::jsonb)
                            || jsonb_build_object('archived', %s::boolean),
                          true
                        ),
                        synced_at=now()
                    FROM seller.marketplace_connections AS marketplace_connection
                    WHERE item.connection_id=marketplace_connection.id
                      AND item.connection_id=%s AND item.external_product_id=%s
                      AND item.is_present=true AND marketplace_connection.workspace_id=%s
                    """,
                    (
                        payload.archived, payload.archived, payload.archived,
                        payload.connection_id, product_id, seller_user.workspace_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise HTTPException(
                        status_code=409,
                        detail="Яндекс изменил карточку, но локальный снимок не обновился. Запустите синхронизацию каталога.",
                    )
        return MarketplaceCatalogArchiveOut(
            connection_id=payload.connection_id,
            external_product_id=product_id,
            archived=payload.archived,
        )
