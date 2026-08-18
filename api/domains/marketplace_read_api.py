"""Read-only снимки каталога и заказов подключенных маркетплейсов."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from domains.local_auth import AuthenticatedUser
from domains.marketplace_catalog_service import fetch_marketplace_catalog
from domains.marketplace_orders_service import fetch_marketplace_orders, normalize_marketplace_order_status


class MarketplaceCatalogItemOut(BaseModel):
    connection_id: int
    provider_code: str
    store_name: str
    external_product_id: str
    offer_id: str = ""
    sku: str = ""
    title: str = ""
    synced_at: datetime


class MarketplaceCatalogListOut(BaseModel):
    items: list[MarketplaceCatalogItemOut]
    total: int
    page: int
    page_size: int


class MarketplaceOrderItemOut(BaseModel):
    connection_id: int
    provider_code: str
    store_name: str
    external_order_id: str
    external_item_id: str
    offer_id: str = ""
    sku: str = ""
    title: str = ""
    quantity: int
    status: str
    provider_status: str = ""
    delivery_type: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    synced_at: datetime


class MarketplaceOrderListOut(BaseModel):
    items: list[MarketplaceOrderItemOut]
    total: int
    page: int
    page_size: int


class MarketplaceSnapshotSyncIn(BaseModel):
    connection_id: int | None = Field(default=None, gt=0)


class MarketplaceSnapshotSyncConnectionOut(BaseModel):
    connection_id: int
    store_name: str
    provider_code: str
    synced_items: int = 0
    error: str = ""


class MarketplaceSnapshotSyncOut(BaseModel):
    items: list[MarketplaceSnapshotSyncConnectionOut]


def first_text(*values: Any) -> str:
    # Берёт первое непустое значение из разных версий ответов Ozon и Яндекс Маркета.
    for value in values:
        if isinstance(value, list):
            for item in value:
                text = str(item or "").strip()
                if text:
                    return text
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""


def safe_int(value: Any, *, default: int = 1) -> int:
    # Не позволяет некорректному количеству из внешнего ответа сломать сохранение снимка.
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def optional_datetime(value: Any) -> datetime | None:
    # Преобразует даты маркетплейсов к UTC, а незнакомые форматы оставляет пустыми.
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_catalog_item(provider_code: str, payload: dict[str, Any]) -> dict[str, str] | None:
    # Выделяет стабильные поля карточки, а исходный ответ сохраняется отдельно для будущего расширения.
    if provider_code == "ozon":
        external_product_id = first_text(payload.get("product_id"), payload.get("id"), payload.get("offer_id"))
        offer_id = first_text(payload.get("offer_id"), payload.get("offer_code"))
        sku = first_text(payload.get("sku"), payload.get("fbo_sku"), payload.get("fbs_sku"), offer_id)
        title = first_text(payload.get("name"), payload.get("title"))
    elif provider_code == "yandex_market":
        offer = payload.get("offer") if isinstance(payload.get("offer"), dict) else {}
        mapping = payload.get("mapping") if isinstance(payload.get("mapping"), dict) else {}
        offer_id = first_text(offer.get("offerId"), payload.get("offerId"))
        external_product_id = offer_id
        # Для продавца важнее его SKU offerId, а marketSku остаётся в исходном снимке для будущих деталей.
        sku = offer_id or first_text(mapping.get("marketSku"))
        title = first_text(offer.get("name"), mapping.get("marketSkuName"))
    else:
        return None
    if not external_product_id:
        return None
    return {
        "external_product_id": external_product_id,
        "offer_id": offer_id,
        "sku": sku,
        "title": title,
    }


def normalize_order_items(provider_code: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    # Разворачивает многотоварный заказ в позиции, чтобы фильтры и будущая ручная выдача работали по SKU.
    if provider_code == "ozon":
        order_id = first_text(payload.get("posting_number"), payload.get("order_id"), payload.get("id"))
        provider_status = first_text(payload.get("status"), payload.get("posting_status"))
        substatus = first_text(payload.get("substatus"), payload.get("sub_status"))
        product_rows = payload.get("products") if isinstance(payload.get("products"), list) else []
        if not product_rows:
            product_rows = [payload]
        result: list[dict[str, Any]] = []
        for index, product in enumerate(product_rows, start=1):
            if not isinstance(product, dict):
                continue
            offer_id = first_text(product.get("offer_id"), product.get("offer_code"))
            sku = first_text(product.get("sku"), product.get("product_id"), offer_id)
            item_id = first_text(product.get("product_id"), product.get("sku"), offer_id, str(index))
            if not order_id or not item_id:
                continue
            result.append(
                {
                    "external_order_id": order_id,
                    "external_item_id": item_id,
                    "offer_id": offer_id,
                    "sku": sku,
                    "title": first_text(product.get("name"), product.get("title"), payload.get("product_name")),
                    "quantity": safe_int(product.get("quantity"), default=1),
                    "provider_status": provider_status,
                    "provider_substatus": substatus,
                    "normalized_status": normalize_marketplace_order_status(
                        provider_code=provider_code, status=provider_status, substatus=substatus,
                    ),
                    "delivery_type": first_text(payload.get("__marketplace_source"), payload.get("delivery_method")),
                    "created_at": optional_datetime(payload.get("in_process_at") or payload.get("created_at")),
                    "updated_at": optional_datetime(payload.get("updated_at") or payload.get("status_updated_at")),
                }
            )
        return result

    if provider_code == "yandex_market":
        order_id = first_text(payload.get("orderId"), payload.get("id"))
        provider_status = first_text(payload.get("status"))
        substatus = first_text(payload.get("substatus"), payload.get("subStatus"))
        product_rows = payload.get("items") if isinstance(payload.get("items"), list) else []
        result = []
        for index, product in enumerate(product_rows, start=1):
            if not isinstance(product, dict):
                continue
            offer_id = first_text(product.get("offerId"), product.get("offer_id"))
            item_id = first_text(product.get("id"), product.get("itemId"), offer_id, str(index))
            if not order_id or not item_id:
                continue
            result.append(
                {
                    "external_order_id": order_id,
                    "external_item_id": item_id,
                    "offer_id": offer_id,
                    "sku": offer_id,
                    "title": first_text(product.get("offerName"), product.get("name"), product.get("title")),
                    "quantity": safe_int(product.get("count") or product.get("quantity"), default=1),
                    "provider_status": provider_status,
                    "provider_substatus": substatus,
                    "normalized_status": normalize_marketplace_order_status(
                        provider_code=provider_code, status=provider_status, substatus=substatus,
                    ),
                    "delivery_type": first_text(payload.get("deliveryType"), payload.get("delivery", {}).get("type") if isinstance(payload.get("delivery"), dict) else ""),
                    "created_at": optional_datetime(payload.get("creationDate") or payload.get("createdAt")),
                    "updated_at": optional_datetime(payload.get("updatedAt") or payload.get("statusUpdateDate")),
                }
            )
        return result
    return []


def mount_marketplace_read_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Подключает только чтение и сохранение локальных снимков без операций в кабинетах маркетплейсов."""

    def credentials_secret() -> str:
        # Проверяет ключ до расшифровки токена и не позволяет запросу использовать частично настроенное окружение.
        value = str(os.getenv("MARKETPLACE_CREDENTIALS_SECRET", "")).strip()
        if len(value) < 32:
            raise HTTPException(status_code=503, detail="Не настроено защищённое хранение токенов маркетплейсов")
        return value

    def workspace_for_user(connection, user: AuthenticatedUser):
        # Получает рабочую область сессии на сервере, не доверяя идентификатору организации из браузера.
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        return seller_user

    def active_connections(connection, workspace_id: int, connection_id: int | None = None) -> list[tuple[Any, ...]]:
        # Расшифровывает реквизиты только на момент исходящего read-only запроса и только для своего workspace.
        where_connection = "AND id=%s" if connection_id else ""
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, provider_code, display_name, client_id, business_id, campaign_id,
                       pgp_sym_decrypt(token_ciphertext, %s)
                FROM seller.marketplace_connections
                WHERE workspace_id=%s AND status='active' {where_connection}
                ORDER BY created_at, id
                """,
                [credentials_secret(), workspace_id, *([connection_id] if connection_id else [])],
            )
            return cursor.fetchall()

    def sync_catalog_connection(connection, connection_row: tuple[Any, ...]) -> int:
        # Загружает карточки выбранного магазина, сохраняя снимок без изменения товарных данных в маркетплейсе.
        connection_id, provider_code, _name, client_id, business_id, _campaign_id, token = connection_row
        rows = fetch_marketplace_catalog(
            provider_code=str(provider_code),
            token=str(token),
            client_id=str(client_id or ""),
            business_id=int(business_id) if str(business_id or "").isdigit() else None,
        )
        normalized_rows = [(normalize_catalog_item(str(provider_code), item), item) for item in rows if isinstance(item, dict)]
        normalized_rows = [(item, payload) for item, payload in normalized_rows if item]
        with connection.cursor() as cursor:
            for item, raw_payload in normalized_rows:
                cursor.execute(
                    """
                    INSERT INTO seller.catalog_items(
                        connection_id, external_product_id, offer_id, sku, title, raw_payload, synced_at
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (connection_id, external_product_id) DO UPDATE SET
                        offer_id=EXCLUDED.offer_id, sku=EXCLUDED.sku, title=EXCLUDED.title,
                        raw_payload=EXCLUDED.raw_payload, synced_at=now()
                    """,
                    (
                        connection_id,
                        item["external_product_id"],
                        item["offer_id"],
                        item["sku"],
                        item["title"],
                        json.dumps(raw_payload, ensure_ascii=False),
                    ),
                )
        return len(normalized_rows)

    def sync_orders_connection(connection, connection_row: tuple[Any, ...]) -> int:
        # Загружает только свежий read-only снимок заказов, не подтверждая доставку и не выдавая ключи.
        connection_id, provider_code, _name, client_id, business_id, campaign_id, token = connection_row
        rows = fetch_marketplace_orders(
            provider_code=str(provider_code),
            token=str(token),
            client_id=str(client_id or ""),
            business_id=int(business_id) if str(business_id or "").isdigit() else None,
            campaign_id=int(campaign_id) if str(campaign_id or "").isdigit() else None,
        )
        normalized_rows = [
            (item, raw_payload)
            for raw_payload in rows if isinstance(raw_payload, dict)
            for item in normalize_order_items(str(provider_code), raw_payload)
        ]
        with connection.cursor() as cursor:
            for item, raw_payload in normalized_rows:
                cursor.execute(
                    """
                    INSERT INTO seller.order_items(
                        connection_id, external_order_id, external_item_id, offer_id, sku, title, quantity,
                        provider_status, provider_substatus, normalized_status, delivery_type,
                        created_at, updated_at, raw_payload, synced_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (connection_id, external_order_id, external_item_id) DO UPDATE SET
                        offer_id=EXCLUDED.offer_id, sku=EXCLUDED.sku, title=EXCLUDED.title,
                        quantity=EXCLUDED.quantity, provider_status=EXCLUDED.provider_status,
                        provider_substatus=EXCLUDED.provider_substatus, normalized_status=EXCLUDED.normalized_status,
                        delivery_type=EXCLUDED.delivery_type, created_at=COALESCE(EXCLUDED.created_at, seller.order_items.created_at),
                        updated_at=COALESCE(EXCLUDED.updated_at, seller.order_items.updated_at),
                        raw_payload=EXCLUDED.raw_payload, synced_at=now()
                    """,
                    (
                        connection_id,
                        item["external_order_id"],
                        item["external_item_id"],
                        item["offer_id"],
                        item["sku"],
                        item["title"],
                        item["quantity"],
                        item["provider_status"],
                        item["provider_substatus"],
                        item["normalized_status"],
                        item["delivery_type"],
                        item["created_at"],
                        item["updated_at"],
                        json.dumps(raw_payload, ensure_ascii=False),
                    ),
                )
        return len(normalized_rows)

    def save_sync_error(connection, connection_id: int, message: str) -> None:
        # Сохраняет короткую диагностику синхронизации, не меняя активный доступ и не раскрывая токен.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE seller.marketplace_connections SET last_error=%s, updated_at=now() WHERE id=%s",
                (message[:1000], connection_id),
            )

    def clear_sync_error(connection, connection_id: int) -> None:
        # Очищает прошлую ошибку только после полностью успешного обновления снимка выбранного магазина.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE seller.marketplace_connections SET last_error='', updated_at=now() WHERE id=%s",
                (connection_id,),
            )

    def sync_snapshot(kind: str, payload: MarketplaceSnapshotSyncIn, user: AuthenticatedUser) -> MarketplaceSnapshotSyncOut:
        # Обрабатывает каждый магазин отдельно, чтобы сбой одного кабинета не скрывал обновления остальных.
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            connection_rows = active_connections(connection, seller_user.workspace_id, payload.connection_id)
            if not connection_rows:
                raise HTTPException(status_code=404, detail="Активный подключенный магазин не найден")
            result: list[MarketplaceSnapshotSyncConnectionOut] = []
            for row in connection_rows:
                connection_id, provider_code, store_name = int(row[0]), str(row[1]), str(row[2])
                try:
                    synced_items = sync_catalog_connection(connection, row) if kind == "catalog" else sync_orders_connection(connection, row)
                except HTTPException as exc:
                    save_sync_error(connection, connection_id, str(exc.detail))
                    result.append(
                        MarketplaceSnapshotSyncConnectionOut(
                            connection_id=connection_id, provider_code=provider_code, store_name=store_name, error=str(exc.detail),
                        )
                    )
                    continue
                clear_sync_error(connection, connection_id)
                result.append(
                    MarketplaceSnapshotSyncConnectionOut(
                        connection_id=connection_id, provider_code=provider_code, store_name=store_name, synced_items=synced_items,
                    )
                )
        return MarketplaceSnapshotSyncOut(items=result)

    @app.post("/marketplaces/catalog/sync", response_model=MarketplaceSnapshotSyncOut)
    def sync_catalog(payload: MarketplaceSnapshotSyncIn, user: AuthenticatedUser = Depends(current_user)) -> MarketplaceSnapshotSyncOut:
        # Обновляет локальный снимок каталога и намеренно не содержит методов изменения карточек маркетплейса.
        return sync_snapshot("catalog", payload, user)

    @app.get("/marketplaces/catalog", response_model=MarketplaceCatalogListOut)
    def list_catalog(
        connection_id: int | None = Query(default=None, gt=0),
        query: str = Query(default="", max_length=160),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=24, ge=1, le=100),
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceCatalogListOut:
        # Отдаёт постраничный снимок своего workspace, чтобы длинный каталог не превращался в бесконечный список.
        search = str(query or "").strip()
        conditions = ["connection.workspace_id=%s"]
        params: list[Any] = []
        if connection_id:
            conditions.append("item.connection_id=%s")
            params.append(connection_id)
        if search:
            conditions.append("(item.title ILIKE %s OR item.sku ILIKE %s OR item.offer_id ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
        where = " AND ".join(conditions)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            base_params = [seller_user.workspace_id, *params]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM seller.catalog_items AS item JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id WHERE {where}",
                    base_params,
                )
                total = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""
                    SELECT item.connection_id, connection.provider_code, connection.display_name,
                           item.external_product_id, item.offer_id, item.sku, item.title, item.synced_at
                    FROM seller.catalog_items AS item
                    JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id
                    WHERE {where}
                    ORDER BY item.title ASC NULLS LAST, item.sku ASC, item.external_product_id ASC
                    LIMIT %s OFFSET %s
                    """,
                    [*base_params, page_size, (page - 1) * page_size],
                )
                rows = cursor.fetchall()
        return MarketplaceCatalogListOut(
            items=[MarketplaceCatalogItemOut(
                connection_id=int(row[0]), provider_code=str(row[1]), store_name=str(row[2]), external_product_id=str(row[3]),
                offer_id=str(row[4] or ""), sku=str(row[5] or ""), title=str(row[6] or ""), synced_at=row[7],
            ) for row in rows],
            total=total, page=page, page_size=page_size,
        )

    @app.post("/marketplaces/orders/sync", response_model=MarketplaceSnapshotSyncOut)
    def sync_orders(payload: MarketplaceSnapshotSyncIn, user: AuthenticatedUser = Depends(current_user)) -> MarketplaceSnapshotSyncOut:
        # Обновляет только локальный снимок свежих заказов без подтверждения доставки или отправки ключей.
        return sync_snapshot("orders", payload, user)

    @app.get("/marketplaces/orders", response_model=MarketplaceOrderListOut)
    def list_orders(
        connection_id: int | None = Query(default=None, gt=0),
        query: str = Query(default="", max_length=160),
        status: str = Query(default="", max_length=32),
        date_from: date | None = Query(default=None),
        date_to: date | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=24, ge=1, le=100),
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceOrderListOut:
        # Фильтрует сохранённые позиции заказов по дате и статусу без новых запросов к маркетплейсам.
        allowed_statuses = {"processing", "in_delivery", "delivered", "cancelled", "problem"}
        if status and status not in allowed_statuses:
            raise HTTPException(status_code=400, detail="Неизвестный статус заказа")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=400, detail="Дата начала не может быть позже даты окончания")
        search = str(query or "").strip()
        conditions = ["connection.workspace_id=%s"]
        params: list[Any] = []
        if connection_id:
            conditions.append("item.connection_id=%s")
            params.append(connection_id)
        if status:
            conditions.append("item.normalized_status=%s")
            params.append(status)
        if date_from:
            conditions.append("COALESCE(item.created_at, item.synced_at) >= %s")
            params.append(datetime.combine(date_from, time.min, tzinfo=timezone.utc))
        if date_to:
            conditions.append("COALESCE(item.created_at, item.synced_at) < %s")
            params.append(datetime.combine(date_to, time.min, tzinfo=timezone.utc) + timedelta(days=1))
        if search:
            conditions.append("(item.external_order_id ILIKE %s OR item.title ILIKE %s OR item.sku ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
        where = " AND ".join(conditions)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            base_params = [seller_user.workspace_id, *params]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM seller.order_items AS item JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id WHERE {where}",
                    base_params,
                )
                total = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""
                    SELECT item.connection_id, connection.provider_code, connection.display_name,
                           item.external_order_id, item.external_item_id, item.offer_id, item.sku, item.title,
                           item.quantity, item.normalized_status, item.provider_status, item.delivery_type,
                           item.created_at, item.updated_at, item.synced_at
                    FROM seller.order_items AS item
                    JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id
                    WHERE {where}
                    ORDER BY COALESCE(item.updated_at, item.created_at, item.synced_at) DESC,
                             item.external_order_id DESC, item.external_item_id ASC
                    LIMIT %s OFFSET %s
                    """,
                    [*base_params, page_size, (page - 1) * page_size],
                )
                rows = cursor.fetchall()
        return MarketplaceOrderListOut(
            items=[MarketplaceOrderItemOut(
                connection_id=int(row[0]), provider_code=str(row[1]), store_name=str(row[2]),
                external_order_id=str(row[3]), external_item_id=str(row[4]), offer_id=str(row[5] or ""),
                sku=str(row[6] or ""), title=str(row[7] or ""), quantity=int(row[8] or 0), status=str(row[9]),
                provider_status=str(row[10] or ""), delivery_type=str(row[11] or ""), created_at=row[12],
                updated_at=row[13], synced_at=row[14],
            ) for row in rows],
            total=total, page=page, page_size=page_size,
        )
