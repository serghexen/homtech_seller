"""Read-only снимки каталога и заказов подключенных маркетплейсов."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from domains.local_auth import AuthenticatedUser
from domains.marketplace_orders_service import normalize_marketplace_order_status


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
                    "updated_at": optional_datetime(
                        payload.get("updateDate") or payload.get("updatedAt") or payload.get("statusUpdateDate")
                    ),
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

    def workspace_for_user(connection, user: AuthenticatedUser):
        # Получает рабочую область сессии на сервере, не доверяя идентификатору организации из браузера.
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        return seller_user

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
