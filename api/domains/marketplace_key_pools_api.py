"""Безопасное хранение ручных пулов ключей без выдачи в маркетплейс."""

from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
import os
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from domains.connection_entitlements import KEY_POOL_MANAGE, connection_allows
from domains.local_auth import AuthenticatedUser


class MarketplaceKeyOut(BaseModel):
    id: int
    masked_code: str
    status: str
    expires_at: date | None = None
    issued_order_ref: str = ""
    issued_order_id: str = ""
    issued_item_id: str = ""
    issued_at: datetime | None = None
    created_at: datetime


class MarketplaceKeyPoolOut(BaseModel):
    connection_id: int
    external_product_id: str
    free_count: int = 0
    reserved_count: int = 0
    delivered_count: int = 0
    expired_count: int = 0
    disabled_count: int = 0
    total: int = 0
    page: int = 1
    page_size: int = 20
    items: list[MarketplaceKeyOut] = Field(default_factory=list)


class MarketplaceKeysIn(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=1000)
    expires_at: date | None = None


class MarketplaceKeysCreateOut(BaseModel):
    added: int
    duplicates: int


def key_hash(value: str) -> str:
    # Создаёт стабильный отпечаток, чтобы один код нельзя было добавить в несколько пулов.
    return sha256(f"seller-marketplace-key:v1:{value}".encode("utf-8")).hexdigest()


def masked_code(value: str) -> str:
    # Показывает оператору только последние символы и не расшифровывает ключ при обычном просмотре.
    suffix = str(value or "").strip()[-4:]
    return f"••••{suffix}" if suffix else "••••"


def mount_marketplace_key_pool_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Подключает просмотр и пополнение пула, но намеренно не добавляет операции выдачи."""

    def workspace_for_user(connection, user: AuthenticatedUser):
        # Получает workspace и роль только из серверной сессии, не доверяя данным браузера.
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        return seller_user

    def key_pool_secret() -> str:
        # Использует отдельный от API-токенов секрет, чтобы разделить последствия возможной утечки.
        value = str(os.getenv("SELLER_KEY_POOL_SECRET", "")).strip()
        if len(value) < 32:
            raise HTTPException(status_code=503, detail="Не настроено защищённое хранение ключей Seller")
        return value

    def normalized_product_id(value: str) -> str:
        product_id = str(value or "").strip()
        if not product_id or len(product_id) > 256:
            raise HTTPException(status_code=400, detail="Не удалось определить карточку товара")
        return product_id

    def require_product(cursor, connection_id: int, product_id: str, workspace_id: int) -> None:
        # Не позволяет читать или создавать пул чужой либо отсутствующей карточки.
        cursor.execute(
            """
            SELECT 1
            FROM seller.catalog_items AS item
            JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id
            WHERE item.connection_id=%s AND item.external_product_id=%s
              AND item.is_present=true AND connection.workspace_id=%s
            LIMIT 1
            """,
            (connection_id, product_id, workspace_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Карточка товара не найдена")

    def ensure_pool(cursor, connection_id: int, product_id: str, workspace_id: int) -> int:
        # Создаёт пул только при первой записи, сохраняя GET-запрос без побочных изменений.
        require_product(cursor, connection_id, product_id, workspace_id)
        cursor.execute(
            """
            INSERT INTO seller.marketplace_key_pools(connection_id, external_product_id)
            VALUES (%s, %s)
            ON CONFLICT (connection_id, external_product_id)
            DO UPDATE SET updated_at=now()
            RETURNING id
            """,
            (connection_id, product_id),
        )
        return int(cursor.fetchone()[0])

    def pool_out(cursor, connection_id: int, product_id: str, workspace_id: int, page: int, page_size: int):
        # Возвращает счётчики и маскированные строки, не загружая открытые ключи в API.
        require_product(cursor, connection_id, product_id, workspace_id)
        cursor.execute(
            "SELECT id FROM seller.marketplace_key_pools WHERE connection_id=%s AND external_product_id=%s",
            (connection_id, product_id),
        )
        pool = cursor.fetchone()
        if not pool:
            return MarketplaceKeyPoolOut(
                connection_id=connection_id,
                external_product_id=product_id,
                page=page,
                page_size=page_size,
            )
        pool_id = int(pool[0])
        cursor.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status='free'),
              COUNT(*) FILTER (WHERE status IN ('reserved', 'sending')),
              COUNT(*) FILTER (WHERE status='delivered'),
              COUNT(*) FILTER (WHERE status='expired'),
              COUNT(*) FILTER (WHERE status='disabled'),
              COUNT(*)
            FROM seller.marketplace_keys AS key
            WHERE key.pool_id=%s AND key.key_origin='pool'
            """,
            (pool_id,),
        )
        stats = cursor.fetchone() or (0, 0, 0, 0, 0, 0)
        cursor.execute(
            """
            SELECT key.id, key.code_suffix, key.status, key.expires_at, key.issued_order_ref,
                   key.issued_at, key.created_at,
                   COALESCE(fulfillment.external_order_id, ''),
                   COALESCE(fulfillment.external_item_id, '')
            FROM seller.marketplace_keys AS key
            LEFT JOIN LATERAL (
              SELECT order_fulfillment.external_order_id, order_fulfillment.external_item_id
              FROM seller.fulfillment_key_reservations AS reservation
              JOIN seller.order_fulfillments AS order_fulfillment
                ON order_fulfillment.id=reservation.fulfillment_id
              WHERE reservation.key_id=key.id
              ORDER BY reservation.id DESC
              LIMIT 1
            ) AS fulfillment ON true
            WHERE key.pool_id=%s AND key.key_origin='pool'
            ORDER BY CASE key.status WHEN 'free' THEN 0 WHEN 'reserved' THEN 1 WHEN 'sending' THEN 2 ELSE 3 END,
                     key.created_at DESC, key.id DESC
            LIMIT %s OFFSET %s
            """,
            (pool_id, page_size, (page - 1) * page_size),
        )
        rows = cursor.fetchall()
        return MarketplaceKeyPoolOut(
            connection_id=connection_id,
            external_product_id=product_id,
            free_count=int(stats[0] or 0),
            reserved_count=int(stats[1] or 0),
            delivered_count=int(stats[2] or 0),
            expired_count=int(stats[3] or 0),
            disabled_count=int(stats[4] or 0),
            total=int(stats[5] or 0),
            page=page,
            page_size=page_size,
            items=[
                MarketplaceKeyOut(
                    id=int(row[0]), masked_code=masked_code(row[1]), status=str(row[2]),
                    expires_at=row[3], issued_order_ref=str(row[4] or ""),
                    issued_at=row[5], created_at=row[6],
                    issued_order_id=str(row[7] or ""), issued_item_id=str(row[8] or ""),
                )
                for row in rows
            ],
        )

    @app.get("/marketplaces/catalog/key-pool", response_model=MarketplaceKeyPoolOut)
    def get_key_pool(
        connection_id: int = Query(gt=0),
        external_product_id: str = Query(min_length=1, max_length=256),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceKeyPoolOut:
        # Любая роль workspace может видеть количество и маски, но не открытые значения ключей.
        product_id = normalized_product_id(external_product_id)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                result = pool_out(cursor, connection_id, product_id, seller_user.workspace_id, page, page_size)
        return result

    @app.post("/marketplaces/catalog/key-pool/keys", response_model=MarketplaceKeysCreateOut)
    def add_key_pool_keys(
        payload: MarketplaceKeysIn,
        connection_id: int = Query(gt=0),
        external_product_id: str = Query(min_length=1, max_length=256),
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceKeysCreateOut:
        # Добавляет ключи только в Seller; маршрут не связан с заказами и не вызывает маркетплейс.
        product_id = normalized_product_id(external_product_id)
        prepared: list[str] = []
        seen: set[str] = set()
        duplicate_count = 0
        for raw_code in payload.codes:
            code = str(raw_code or "").strip()
            if not code:
                continue
            if len(code) > 1024:
                raise HTTPException(status_code=400, detail="Один из ключей длиннее 1024 символов")
            fingerprint = key_hash(code)
            if fingerprint in seen:
                duplicate_count += 1
                continue
            seen.add(fingerprint)
            prepared.append(code)
        if not prepared:
            raise HTTPException(status_code=400, detail="Добавьте хотя бы один непустой ключ")

        added = 0
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для добавления ключей")
            with connection.cursor() as cursor:
                if not connection_allows(
                    cursor, seller_user.workspace_id, connection_id, KEY_POOL_MANAGE,
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="Управление пулом недоступно на текущем тарифе магазина",
                    )
                secret = key_pool_secret()
                pool_id = ensure_pool(cursor, connection_id, product_id, seller_user.workspace_id)
                for code in prepared:
                    cursor.execute(
                        """
                        INSERT INTO seller.marketplace_keys (
                          pool_id, code_ciphertext, code_hash, code_suffix, key_origin,
                          expires_at, created_by_user_id
                        ) VALUES (
                          %s, pgp_sym_encrypt(%s, %s, 'cipher-algo=aes256, compress-algo=0'),
                          %s, %s, 'pool', %s, %s
                        )
                        ON CONFLICT (code_hash) DO NOTHING
                        RETURNING id
                        """,
                        (pool_id, code, secret, key_hash(code), code[-4:], payload.expires_at, seller_user.id),
                    )
                    if cursor.fetchone():
                        added += 1
        return MarketplaceKeysCreateOut(added=added, duplicates=duplicate_count + len(prepared) - added)
