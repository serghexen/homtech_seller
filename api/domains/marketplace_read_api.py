"""Read-only снимки каталога и заказов подключенных маркетплейсов."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_UP
from typing import Any, Callable, Literal
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from domains.buyer_text import normalize_buyer_text
from domains.local_auth import AuthenticatedUser
from domains.marketplace_catalog_service import fetch_marketplace_stocks
from domains.marketplace_orders_service import normalize_marketplace_order_status
from domains.supplier_hub_client import SupplierHubClient, SupplierHubError, load_supplier_hub_settings
from domains.workspace_entitlements import SUPPLIER_MAPPING_MANAGE, workspace_allows


CATALOG_SEARCH_EXPRESSIONS = (
    "item.title",
    "item.sku",
    "item.offer_id",
    "COALESCE(item.raw_payload #>> '{mapping,marketSku}', '')",
)
ORDER_SEARCH_EXPRESSIONS = (
    "item.external_order_id",
    "item.title",
    "item.sku",
    "item.offer_id",
)

SUPPLIER_PRICE_GUARD_MULTIPLIER = Decimal("1.05")


def yandex_stock_publication_enabled() -> bool:
    """Проверяет тот же глобальный kill switch, не связывая HTTP API с модулем worker."""

    return str(os.getenv("SELLER_YANDEX_STOCK_OUTBOUND_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes",
    }


def supplier_price_guard(amount: Decimal) -> Decimal:
    """Добавляет небольшой внутренний запас к котировке, не показывая его оператору."""
    return (amount * SUPPLIER_PRICE_GUARD_MULTIPLIER).quantize(Decimal("0.01"), rounding=ROUND_UP)


class MarketplaceCatalogItemOut(BaseModel):
    connection_id: int
    provider_code: str
    store_name: str
    external_product_id: str
    offer_id: str = ""
    sku: str = ""
    title: str = ""
    archived: bool = False
    primary_image: str = ""
    marketplace_url: str = ""
    market_sku: str = ""
    price: str = ""
    currency_code: str = ""
    available_stock: int | None = None
    stock_synced_at: datetime | None = None
    stock_settings_available: bool = False
    sales_metrics_available: bool = False
    manual_stock_limit: int | None = None
    published_stock: int | None = None
    activation_instruction: str = ""
    support_message: str = ""
    support_message_delivery_enabled: bool = False
    pool_issue_enabled: bool = False
    supplier_issue_enabled: bool = False
    supplier_mapping_enabled: bool = False
    supplier_service_id: int | None = None
    supplier_nominal_id: str = ""
    supplier_max_amount: Decimal | None = None
    supplier_quoted_amount: Decimal | None = None
    supplier_quoted_at: datetime | None = None
    sales_limit: int | None = None
    sales_limit_daily_extra: int | None = None
    sales_limit_day: date | None = None
    sales_limit_revision: int | None = None
    sales_limit_used: int | None = None
    sales_limit_reserved: int | None = None
    sales_limit_remaining: int | None = None
    sales_limit_exhausted_at: datetime | None = None
    archived_by_sales_limit: bool = False
    settings_source_updated_at: datetime | None = None
    settings_imported_at: datetime | None = None
    settings_saved_at: datetime | None = None
    synced_at: datetime


class MarketplaceCatalogListOut(BaseModel):
    items: list[MarketplaceCatalogItemOut]
    total: int
    page: int
    page_size: int
    active_total: int = 0
    archived_total: int = 0


class MarketplaceCatalogStockRefreshIn(BaseModel):
    connection_id: int = Field(gt=0)
    offer_id: str = Field(min_length=1, max_length=256)


class MarketplaceCatalogStockOut(BaseModel):
    connection_id: int
    offer_id: str
    available_stock: int
    checked_at: datetime
    provider_updated_at: str = ""


class MarketplaceCatalogStockPublishIn(BaseModel):
    connection_id: int = Field(gt=0)
    external_product_id: str = Field(min_length=1, max_length=256)
    target_stock: int = Field(ge=0, le=1_000_000)


class MarketplaceCatalogStockPublicationOut(BaseModel):
    job_id: int
    connection_id: int
    external_product_id: str
    requested_stock: int
    target_stock: int | None = None
    state: Literal["queued", "preparing", "sending", "succeeded", "failed"]
    last_error: str = ""
    created_at: datetime
    updated_at: datetime
    succeeded_at: datetime | None = None
    failed_at: datetime | None = None


class MarketplaceCatalogSettingsIn(BaseModel):
    connection_id: int = Field(gt=0)
    external_product_id: str = Field(min_length=1, max_length=256)
    manual_stock_limit: int = Field(ge=0, le=1_000_000)
    sales_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    sales_limit_daily_extra: int = Field(default=0, ge=0, le=1_000_000)
    activation_instruction: str = Field(default="", max_length=10_000)
    support_message: str = Field(default="", max_length=2_000)
    support_message_delivery_enabled: bool = False
    pool_issue_enabled: bool = False
    supplier_issue_enabled: bool = False
    supplier_service_id: int | None = Field(default=None, gt=0)
    supplier_nominal_id: str = Field(default="", max_length=128)
    supplier_max_amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)


class MarketplaceCatalogSettingsOut(BaseModel):
    connection_id: int
    external_product_id: str
    manual_stock_limit: int
    sales_limit: int | None = None
    sales_limit_daily_extra: int
    sales_limit_day: date
    activation_instruction: str
    support_message: str
    support_message_delivery_enabled: bool
    pool_issue_enabled: bool
    supplier_issue_enabled: bool
    supplier_service_id: int | None = None
    supplier_nominal_id: str = ""
    supplier_max_amount: Decimal | None = None
    supplier_quoted_amount: Decimal | None = None
    supplier_quoted_at: datetime | None = None
    settings_saved_at: datetime


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


def marketplace_order_from_row(row: tuple[Any, ...]) -> MarketplaceOrderItemOut:
    return MarketplaceOrderItemOut(
        connection_id=int(row[0]), provider_code=str(row[1]), store_name=str(row[2]),
        external_order_id=str(row[3]), external_item_id=str(row[4]), offer_id=str(row[5] or ""),
        sku=str(row[6] or ""), title=str(row[7] or ""), quantity=int(row[8] or 0), status=str(row[9]),
        provider_status=str(row[10] or ""), delivery_type=str(row[11] or ""), created_at=row[12],
        updated_at=row[13], synced_at=row[14],
    )


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


def catalog_primary_image(provider_code: str, payload: Any) -> str:
    # Берёт ссылку на главное изображение из уже сохранённого ответа, не запрашивая карточку у маркетплейса повторно.
    if not isinstance(payload, dict):
        return ""
    if provider_code == "yandex_market":
        offer = payload.get("offer") if isinstance(payload.get("offer"), dict) else {}
        pictures = offer.get("pictures") if isinstance(offer.get("pictures"), list) else []
        return first_text(pictures)
    if provider_code == "ozon":
        return first_text(payload.get("primary_image"), payload.get("images"))
    return ""


def catalog_marketplace_url(provider_code: str, payload: Any, *, sku: str = "") -> str:
    # Использует готовую витринную ссылку маркетплейса и не доверяет произвольным доменам из внешнего ответа.
    candidates: list[str] = []
    if isinstance(payload, dict) and provider_code == "yandex_market":
        showcase_urls = payload.get("showcaseUrls") if isinstance(payload.get("showcaseUrls"), list) else []
        ordered_urls = sorted(
            (item for item in showcase_urls if isinstance(item, dict)),
            key=lambda item: str(item.get("showcaseType") or "").upper() != "B2C",
        )
        candidates.extend(first_text(item.get("showcaseUrl")) for item in ordered_urls)
    elif isinstance(payload, dict) and provider_code == "ozon":
        candidates.extend(first_text(payload.get(key)) for key in ("product_url", "marketing_url", "url"))

    allowed_hosts = {
        "yandex_market": {"market.yandex.ru"},
        "ozon": {"ozon.ru", "www.ozon.ru"},
    }.get(provider_code, set())
    for candidate in candidates:
        parsed = urlparse(candidate)
        if parsed.scheme == "https" and str(parsed.hostname or "").lower() in allowed_hosts:
            return candidate

    normalized_sku = str(sku or "").strip()
    if provider_code == "ozon" and normalized_sku.isdigit():
        return f"https://www.ozon.ru/product/{quote(normalized_sku, safe='')}/"
    return ""


def catalog_card_details(provider_code: str, payload: Any) -> dict[str, Any]:
    # Достаёт параметры карточки из уже сохранённого снимка. Остаток не подменяется выдуманным значением,
    # потому что Яндекс отдаёт его отдельным методом, который в read-only Seller пока не перенесён.
    if not isinstance(payload, dict):
        return {"market_sku": "", "price": "", "currency_code": "", "available_stock": None, "stock_synced_at": None}
    if provider_code == "yandex_market":
        offer = payload.get("offer") if isinstance(payload.get("offer"), dict) else {}
        mapping = payload.get("mapping") if isinstance(payload.get("mapping"), dict) else {}
        basic_price = offer.get("basicPrice") if isinstance(offer.get("basicPrice"), dict) else {}
        seller_snapshot = payload.get("_sellerSnapshot") if isinstance(payload.get("_sellerSnapshot"), dict) else {}
        available_stock = seller_snapshot.get("availableStock")
        try:
            available_stock = max(0, int(available_stock)) if available_stock is not None else None
        except (TypeError, ValueError):
            available_stock = None
        return {
            "market_sku": first_text(mapping.get("marketSku")),
            "price": first_text(basic_price.get("value")),
            "currency_code": first_text(basic_price.get("currencyId")),
            "available_stock": available_stock,
            "stock_synced_at": first_text(seller_snapshot.get("stockCheckedAt")) or None,
        }
    if provider_code == "ozon":
        return {
            "market_sku": "",
            "price": first_text(payload.get("price"), payload.get("marketing_price")),
            "currency_code": first_text(payload.get("currency_code"), payload.get("currency")),
            "available_stock": None,
            "stock_synced_at": None,
        }
    return {"market_sku": "", "price": "", "currency_code": "", "available_stock": None, "stock_synced_at": None}


def catalog_payload_with_stock(
    payload: Any, *, available_stock: int, checked_at: datetime, provider_updated_at: str = "",
) -> dict[str, Any]:
    # Добавляет к исходному ответу только локальные метаданные чтения, не меняя поля карточки маркетплейса.
    result = dict(payload) if isinstance(payload, dict) else {}
    result["_sellerSnapshot"] = {
        "availableStock": max(0, int(available_stock)),
        "stockCheckedAt": checked_at.isoformat(),
        "stockUpdatedAt": str(provider_updated_at or "").strip(),
    }
    return result


def safe_int(value: Any, *, default: int = 1) -> int:
    # Не позволяет некорректному количеству из внешнего ответа сломать сохранение снимка.
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def ilike_search_condition(search: str, expressions: tuple[str, ...]) -> tuple[str, list[str]]:
    """Собирает поиск по тем же идентификаторам, которые видит оператор в интерфейсе."""
    cleaned = str(search or "").strip()
    if not cleaned:
        return "", []
    pattern = f"%{cleaned}%"
    return f"({' OR '.join(f'{expression} ILIKE %s' for expression in expressions)})", [pattern] * len(expressions)


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


def normalize_catalog_item(provider_code: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    # Выделяет стабильные поля карточки, а исходный ответ сохраняется отдельно для будущего расширения.
    if provider_code == "ozon":
        external_product_id = first_text(payload.get("product_id"), payload.get("id"), payload.get("offer_id"))
        offer_id = first_text(payload.get("offer_id"), payload.get("offer_code"))
        sku = first_text(payload.get("sku"), payload.get("fbo_sku"), payload.get("fbs_sku"), offer_id)
        title = first_text(payload.get("name"), payload.get("title"))
        visibility = str(payload.get("visibility") or "").strip().upper()
        is_archived = bool(payload.get("archived")) or visibility == "ARCHIVED"
    elif provider_code == "yandex_market":
        offer = payload.get("offer") if isinstance(payload.get("offer"), dict) else {}
        mapping = payload.get("mapping") if isinstance(payload.get("mapping"), dict) else {}
        offer_id = first_text(offer.get("offerId"), payload.get("offerId"))
        external_product_id = offer_id
        # Для продавца важнее его SKU offerId, а marketSku остаётся в исходном снимке для будущих деталей.
        sku = offer_id or first_text(mapping.get("marketSku"))
        title = first_text(offer.get("name"), mapping.get("marketSkuName"))
        is_archived = bool(offer.get("archived"))
    else:
        return None
    if not external_product_id:
        return None
    return {
        "external_product_id": external_product_id,
        "offer_id": offer_id,
        "sku": sku,
        "title": title,
        "is_archived": is_archived,
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

    def stock_publication_from_row(row) -> MarketplaceCatalogStockPublicationOut:
        return MarketplaceCatalogStockPublicationOut(
            job_id=int(row[0]), connection_id=int(row[1]), external_product_id=str(row[2]),
            requested_stock=int(row[3]), target_stock=int(row[4]) if row[4] is not None else None,
            state=str(row[5]), last_error=str(row[6] or ""), created_at=row[7], updated_at=row[8],
            succeeded_at=row[9], failed_at=row[10],
        )

    def credentials_secret() -> str:
        # Расшифровывает API-Key только на время одного read-only запроса остатка.
        value = str(os.getenv("MARKETPLACE_CREDENTIALS_SECRET", "")).strip()
        if len(value) < 32:
            raise HTTPException(status_code=503, detail="Не настроено защищённое чтение токена маркетплейса")
        return value

    @app.get("/marketplaces/catalog", response_model=MarketplaceCatalogListOut)
    def list_catalog(
        connection_id: int | None = Query(default=None, gt=0),
        query: str = Query(default="", max_length=160),
        state: Literal["active", "archived"] = Query(default="active"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=24, ge=1, le=100),
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceCatalogListOut:
        # Отдаёт постраничный снимок своего workspace, чтобы длинный каталог не превращался в бесконечный список.
        search = str(query or "").strip()
        scope_conditions = ["connection.workspace_id=%s", "item.is_present=true"]
        scope_params: list[Any] = []
        if connection_id:
            scope_conditions.append("item.connection_id=%s")
            scope_params.append(connection_id)
        conditions = [*scope_conditions, "item.is_archived=%s"]
        params: list[Any] = [*scope_params, state == "archived"]
        search_condition, search_params = ilike_search_condition(search, CATALOG_SEARCH_EXPRESSIONS)
        if search_condition:
            conditions.append(search_condition)
            params.extend(search_params)
        where = " AND ".join(conditions)
        scope_where = " AND ".join(scope_conditions)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            base_params = [seller_user.workspace_id, *params]
            count_params = [seller_user.workspace_id, *scope_params]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT COUNT(*) FILTER (WHERE item.is_archived=false),
                           COUNT(*) FILTER (WHERE item.is_archived=true)
                    FROM seller.catalog_items AS item
                    JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id
                    WHERE {scope_where}
                    """,
                    count_params,
                )
                catalog_counts = cursor.fetchone() or (0, 0)
                active_total = int(catalog_counts[0] or 0)
                archived_total = int(catalog_counts[1] or 0)
                cursor.execute(
                    f"SELECT COUNT(*) FROM seller.catalog_items AS item JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id WHERE {where}",
                    base_params,
                )
                total = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""
                    SELECT item.connection_id, connection.provider_code, connection.display_name,
                           item.external_product_id, item.offer_id, item.sku, item.title, item.synced_at,
                           item.raw_payload,
                           local_settings.connection_id IS NOT NULL,
                           settings.connection_id IS NOT NULL,
                           COALESCE(local_settings.manual_stock_limit, settings.manual_stock_limit),
                           COALESCE(local_settings.published_stock, settings.published_stock),
                           COALESCE(local_settings.activation_instruction, settings.activation_instruction),
                           CASE WHEN local_settings.connection_id IS NOT NULL
                             THEN local_settings.sales_limit ELSE settings.sales_limit END,
                           CASE
                             WHEN local_settings.connection_id IS NOT NULL THEN
                               CASE WHEN local_settings.sales_limit_day=CURRENT_DATE
                                 THEN local_settings.sales_limit_daily_extra ELSE 0 END
                             ELSE CASE WHEN settings.sales_limit_day=CURRENT_DATE
                               THEN COALESCE(settings.sales_limit_daily_extra, 0) ELSE 0 END
                           END,
                           CASE WHEN local_settings.connection_id IS NOT NULL
                             THEN local_settings.sales_limit_day ELSE settings.sales_limit_day END,
                           settings.sales_limit_revision, settings.sales_limit_used,
                           settings.sales_limit_reserved, settings.sales_limit_remaining,
                           settings.sales_limit_exhausted_at, settings.archived_by_sales_limit,
                           settings.source_updated_at, settings.imported_at,
                           local_settings.updated_at, item.is_archived,
                           CASE WHEN COALESCE(local_settings.support_message_overridden, false)
                             THEN local_settings.support_message ELSE COALESCE(settings.support_message, '') END,
                           COALESCE(policy.support_message_delivery_enabled,
                             CASE WHEN COALESCE(local_settings.support_message_overridden, false)
                               THEN local_settings.support_message_delivery_enabled
                               ELSE COALESCE(settings.support_message_delivery_enabled, false) END),
                           COALESCE(policy.pool_issue_enabled, local_settings.pool_issue_enabled, false),
                           COALESCE(policy.supplier_issue_enabled, false),
                           COALESCE(supplier.enabled, false), supplier.service_id,
                           COALESCE(supplier.nominal_id, ''), supplier.max_amount,
                           supplier.quoted_amount, supplier.quoted_at
                    FROM seller.catalog_items AS item
                    JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id
                    LEFT JOIN seller.yandex_product_settings_snapshot AS settings
                      ON settings.connection_id=item.connection_id
                     AND settings.external_product_id=item.external_product_id
                    LEFT JOIN seller.product_card_settings AS local_settings
                      ON local_settings.connection_id=item.connection_id
                     AND local_settings.external_product_id=item.external_product_id
                    LEFT JOIN seller.product_fulfillment_policies AS policy
                      ON policy.connection_id=item.connection_id
                     AND policy.external_product_id=item.external_product_id
                    LEFT JOIN LATERAL (
                      SELECT mapping.enabled, mapping.service_id, mapping.nominal_id,
                             mapping.max_amount, mapping.quoted_amount, mapping.quoted_at
                      FROM seller.product_supplier_mappings AS mapping
                      WHERE mapping.connection_id=item.connection_id
                        AND mapping.external_product_id=item.external_product_id
                      ORDER BY mapping.priority, mapping.id
                      LIMIT 1
                    ) AS supplier ON true
                    WHERE {where}
                    ORDER BY item.title ASC NULLS LAST, item.sku ASC, item.external_product_id ASC
                    LIMIT %s OFFSET %s
                    """,
                    [*base_params, page_size, (page - 1) * page_size],
                )
                rows = cursor.fetchall()
        items: list[MarketplaceCatalogItemOut] = []
        for row in rows:
            provider_code = str(row[1])
            details = catalog_card_details(provider_code, row[8])
            has_local_settings = bool(row[9])
            has_imported_settings = bool(row[10])
            has_settings = has_local_settings or has_imported_settings
            items.append(MarketplaceCatalogItemOut(
                connection_id=int(row[0]), provider_code=provider_code, store_name=str(row[2]), external_product_id=str(row[3]),
                offer_id=str(row[4] or ""), sku=str(row[5] or ""), title=str(row[6] or ""), synced_at=row[7],
                archived=bool(row[26]),
                primary_image=catalog_primary_image(provider_code, row[8]),
                marketplace_url=catalog_marketplace_url(provider_code, row[8], sku=str(row[5] or "")),
                stock_settings_available=has_settings,
                sales_metrics_available=has_imported_settings,
                manual_stock_limit=int(row[11]) if has_settings else None,
                published_stock=int(row[12]) if row[12] is not None else None,
                activation_instruction=str(row[13] or "") if has_settings else "",
                sales_limit=int(row[14]) if row[14] is not None else None,
                sales_limit_daily_extra=int(row[15]) if has_settings else None,
                sales_limit_day=row[16] if has_settings else None,
                sales_limit_revision=int(row[17]) if has_imported_settings else None,
                sales_limit_used=int(row[18]) if has_imported_settings else None,
                sales_limit_reserved=int(row[19]) if has_imported_settings else None,
                sales_limit_remaining=int(row[20]) if row[20] is not None else None,
                sales_limit_exhausted_at=row[21] if has_imported_settings else None,
                archived_by_sales_limit=bool(row[22]) if has_imported_settings else False,
                settings_source_updated_at=row[23] if has_imported_settings else None,
                settings_imported_at=row[24] if has_imported_settings else None,
                settings_saved_at=row[25] if has_local_settings else None,
                support_message=str(row[27] or "") if has_settings else "",
                support_message_delivery_enabled=bool(row[28]) if has_settings else False,
                pool_issue_enabled=bool(row[29]),
                supplier_issue_enabled=bool(row[30]), supplier_mapping_enabled=bool(row[31]),
                supplier_service_id=int(row[32]) if row[32] is not None else None,
                supplier_nominal_id=str(row[33] or ""), supplier_max_amount=row[34],
                supplier_quoted_amount=row[35], supplier_quoted_at=row[36],
                **details,
            ))
        return MarketplaceCatalogListOut(
            items=items,
            total=total, page=page, page_size=page_size,
            active_total=active_total, archived_total=archived_total,
        )

    @app.post("/marketplaces/catalog/settings", response_model=MarketplaceCatalogSettingsOut)
    def save_catalog_settings(
        payload: MarketplaceCatalogSettingsIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceCatalogSettingsOut:
        # Сохраняет только локальные параметры Seller. Здесь намеренно нет токена и вызова API маркетплейса.
        product_id = str(payload.external_product_id).strip()
        instruction = normalize_buyer_text(payload.activation_instruction)
        support_message = normalize_buyer_text(payload.support_message)
        nominal_id = str(payload.supplier_nominal_id or "").strip()
        # Сначала читаем существующую связку и закрываем транзакцию. Внешний quote
        # нельзя выполнять с открытой транзакцией PostgreSQL: медленный поставщик не
        # должен удерживать соединение или блокировки Seller.
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT mapping.service_id, mapping.nominal_id, mapping.max_amount,
                           mapping.quoted_amount, mapping.quoted_at,
                           COALESCE(policy.supplier_issue_enabled, false)
                    FROM seller.catalog_items AS item
                    JOIN seller.marketplace_connections AS marketplace_connection
                      ON marketplace_connection.id=item.connection_id
                    LEFT JOIN seller.product_supplier_mappings AS mapping
                      ON mapping.connection_id=item.connection_id
                     AND mapping.external_product_id=item.external_product_id
                     AND mapping.provider_code='interhub' AND mapping.priority=1
                    LEFT JOIN seller.product_fulfillment_policies AS policy
                      ON policy.connection_id=item.connection_id
                     AND policy.external_product_id=item.external_product_id
                    WHERE item.connection_id=%s AND item.external_product_id=%s
                      AND item.is_present=true AND marketplace_connection.workspace_id=%s
                    LIMIT 1
                    """,
                    (payload.connection_id, product_id, seller_user.workspace_id),
                )
                existing_mapping = cursor.fetchone()
                if not existing_mapping:
                    raise HTTPException(status_code=404, detail="Карточка товара не найдена")
                supplier_mapping_allowed = workspace_allows(
                    cursor, seller_user.workspace_id, SUPPLIER_MAPPING_MANAGE,
                )

        existing_service_id = int(existing_mapping[0]) if existing_mapping[0] is not None else None
        supplier_fields_changed = (
            bool(payload.supplier_issue_enabled) != bool(existing_mapping[5])
            or payload.supplier_service_id != existing_service_id
            or nominal_id != str(existing_mapping[1] or "")
        )
        if supplier_fields_changed and not supplier_mapping_allowed:
            raise HTTPException(status_code=403, detail="Настройка Supplier Hub доступна на тарифе Pro")
        if supplier_mapping_allowed and payload.supplier_issue_enabled and payload.supplier_service_id is None:
            raise HTTPException(
                status_code=400,
                detail="Для автовыдачи выберите товар Supplier Hub",
            )

        supplier_max_amount: Decimal | None = None
        supplier_quoted_amount: Decimal | None = None
        supplier_quoted_at: datetime | None = None
        if payload.supplier_service_id is not None:
            same_mapping = (
                existing_mapping[0] is not None
                and int(existing_mapping[0]) == payload.supplier_service_id
                and str(existing_mapping[1] or "") == nominal_id
            )
            if same_mapping and (not supplier_mapping_allowed or (
                existing_mapping[2] is not None and existing_mapping[3] is not None
            )):
                supplier_max_amount = Decimal(existing_mapping[2]) if existing_mapping[2] is not None else None
                supplier_quoted_amount = Decimal(existing_mapping[3]) if existing_mapping[3] is not None else None
                supplier_quoted_at = existing_mapping[4]
            else:
                try:
                    quote = SupplierHubClient(load_supplier_hub_settings()).quote(
                        service_id=payload.supplier_service_id,
                        nominal_id=nominal_id,
                    )
                except SupplierHubError as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                if not bool(quote.get("success")) or not quote.get("fixed_amount"):
                    raise HTTPException(
                        status_code=422,
                        detail=str(quote.get("message") or "Поставщик не вернул актуальную цену"),
                    )
                try:
                    supplier_quoted_amount = Decimal(str(quote["fixed_amount"]))
                except (ArithmeticError, ValueError) as exc:
                    raise HTTPException(status_code=502, detail="Поставщик вернул некорректную цену") from exc
                if supplier_quoted_amount <= 0:
                    raise HTTPException(status_code=502, detail="Поставщик вернул некорректную цену")
                supplier_max_amount = supplier_price_guard(supplier_quoted_amount)
                supplier_quoted_at = datetime.now(timezone.utc)

        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                supplier_mapping_allowed_current = workspace_allows(
                    cursor, seller_user.workspace_id, SUPPLIER_MAPPING_MANAGE,
                )
                if supplier_fields_changed and not supplier_mapping_allowed_current:
                    raise HTTPException(status_code=403, detail="Настройка Supplier Hub доступна на тарифе Pro")
                cursor.execute(
                    """
                    SELECT 1
                    FROM seller.catalog_items AS item
                    JOIN seller.marketplace_connections AS marketplace_connection
                      ON marketplace_connection.id=item.connection_id
                    WHERE item.connection_id=%s AND item.external_product_id=%s
                      AND item.is_present=true AND marketplace_connection.workspace_id=%s
                    LIMIT 1
                    """,
                    (payload.connection_id, product_id, seller_user.workspace_id),
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=404, detail="Карточка товара не найдена")
                cursor.execute(
                    """
                    INSERT INTO seller.product_card_settings (
                      connection_id, external_product_id, manual_stock_limit,
                      sales_limit, sales_limit_daily_extra, sales_limit_day, activation_instruction,
                      support_message, support_message_delivery_enabled, support_message_overridden,
                      pool_issue_enabled, updated_by_user_id
                    ) VALUES (%s,%s,%s,%s,%s,CURRENT_DATE,%s,%s,%s,true,%s,%s)
                    ON CONFLICT (connection_id, external_product_id) DO UPDATE SET
                      manual_stock_limit=EXCLUDED.manual_stock_limit,
                      sales_limit=EXCLUDED.sales_limit,
                      sales_limit_daily_extra=EXCLUDED.sales_limit_daily_extra,
                      sales_limit_day=CURRENT_DATE,
                      activation_instruction=EXCLUDED.activation_instruction,
                      support_message=EXCLUDED.support_message,
                      support_message_delivery_enabled=EXCLUDED.support_message_delivery_enabled,
                      support_message_overridden=true,
                      pool_issue_enabled=EXCLUDED.pool_issue_enabled,
                      updated_by_user_id=EXCLUDED.updated_by_user_id,
                      updated_at=now()
                    RETURNING connection_id, external_product_id, manual_stock_limit,
                              sales_limit, sales_limit_daily_extra, sales_limit_day,
                              activation_instruction, support_message,
                              support_message_delivery_enabled, pool_issue_enabled, updated_at
                    """,
                    (
                        payload.connection_id, product_id, payload.manual_stock_limit,
                        payload.sales_limit, payload.sales_limit_daily_extra, instruction, support_message,
                        payload.support_message_delivery_enabled,
                        payload.pool_issue_enabled,
                        seller_user.id,
                    ),
                )
                row = cursor.fetchone()
                cursor.execute(
                    """
                    INSERT INTO seller.product_fulfillment_policies(
                      connection_id, external_product_id, supplier_issue_enabled,
                      pool_issue_enabled, support_message_delivery_enabled,
                      source_system, source_updated_at, updated_by_user_id
                    ) VALUES (%s,%s,%s,%s,%s,'seller',now(),%s)
                    ON CONFLICT (connection_id, external_product_id) DO UPDATE SET
                      supplier_issue_enabled=EXCLUDED.supplier_issue_enabled,
                      pool_issue_enabled=EXCLUDED.pool_issue_enabled,
                      support_message_delivery_enabled=EXCLUDED.support_message_delivery_enabled,
                      source_system='seller', source_updated_at=now(),
                      updated_by_user_id=EXCLUDED.updated_by_user_id, updated_at=now()
                    """,
                    (
                        payload.connection_id, product_id, payload.supplier_issue_enabled,
                        payload.pool_issue_enabled, payload.support_message_delivery_enabled,
                        seller_user.id,
                    ),
                )
                if supplier_mapping_allowed_current and payload.supplier_service_id is not None and supplier_max_amount is not None:
                    cursor.execute(
                        """
                        INSERT INTO seller.product_supplier_mappings(
                          connection_id, external_product_id, provider_code, priority,
                          enabled, service_id, nominal_id, params, max_amount,
                          quoted_amount, quoted_at,
                          source_system, source_updated_at, updated_by_user_id
                        ) VALUES (%s,%s,'interhub',1,%s,%s,%s,'{}'::jsonb,%s,%s,%s,'seller',now(),%s)
                        ON CONFLICT (connection_id, external_product_id, provider_code, priority) DO UPDATE SET
                          enabled=EXCLUDED.enabled, service_id=EXCLUDED.service_id,
                          nominal_id=EXCLUDED.nominal_id, max_amount=EXCLUDED.max_amount,
                          quoted_amount=EXCLUDED.quoted_amount, quoted_at=EXCLUDED.quoted_at,
                          source_system='seller', source_updated_at=now(),
                          updated_by_user_id=EXCLUDED.updated_by_user_id, updated_at=now()
                        """,
                        (
                            payload.connection_id, product_id, payload.supplier_issue_enabled,
                            payload.supplier_service_id, nominal_id,
                            supplier_max_amount, supplier_quoted_amount, supplier_quoted_at,
                            seller_user.id,
                        ),
                    )
                elif supplier_mapping_allowed_current:
                    cursor.execute(
                        """
                        UPDATE seller.product_supplier_mappings
                        SET enabled=false, source_system='seller', source_updated_at=now(),
                            updated_by_user_id=%s, updated_at=now()
                        WHERE connection_id=%s AND external_product_id=%s
                          AND provider_code='interhub' AND priority=1
                        """,
                        (seller_user.id, payload.connection_id, product_id),
                    )
        return MarketplaceCatalogSettingsOut(
            connection_id=int(row[0]), external_product_id=str(row[1]),
            manual_stock_limit=int(row[2]), sales_limit=int(row[3]) if row[3] is not None else None,
            sales_limit_daily_extra=int(row[4]), sales_limit_day=row[5],
            activation_instruction=str(row[6] or ""), support_message=str(row[7] or ""),
            support_message_delivery_enabled=bool(row[8]), pool_issue_enabled=bool(row[9]),
            supplier_issue_enabled=payload.supplier_issue_enabled,
            supplier_service_id=payload.supplier_service_id,
            supplier_nominal_id=nominal_id,
            supplier_max_amount=supplier_max_amount,
            supplier_quoted_amount=supplier_quoted_amount,
            supplier_quoted_at=supplier_quoted_at,
            settings_saved_at=row[10],
        )

    @app.post(
        "/marketplaces/catalog/stock/publications",
        response_model=MarketplaceCatalogStockPublicationOut,
    )
    def publish_catalog_stock(
        payload: MarketplaceCatalogStockPublishIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceCatalogStockPublicationOut:
        """Сохраняет намерение оператора; внешний PUT выполняет только stock worker."""

        product_id = str(payload.external_product_id).strip()
        if not yandex_stock_publication_enabled():
            raise HTTPException(status_code=409, detail="Ручная публикация остатков выключена в Seller")
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для публикации остатка")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT marketplace_connection.provider_code, marketplace_connection.status,
                           marketplace_connection.stock_outbound_enabled, item.is_archived,
                           local_settings.manual_stock_limit
                    FROM seller.catalog_items AS item
                    JOIN seller.marketplace_connections AS marketplace_connection
                      ON marketplace_connection.id=item.connection_id
                    LEFT JOIN seller.product_card_settings AS local_settings
                      ON local_settings.connection_id=item.connection_id
                     AND local_settings.external_product_id=item.external_product_id
                    WHERE item.connection_id=%s AND item.external_product_id=%s
                      AND item.is_present=true AND marketplace_connection.workspace_id=%s
                    LIMIT 1
                    """,
                    (payload.connection_id, product_id, seller_user.workspace_id),
                )
                card = cursor.fetchone()
                if not card:
                    raise HTTPException(status_code=404, detail="Карточка товара не найдена")
                if str(card[0]) != "yandex_market":
                    raise HTTPException(status_code=400, detail="Публикация остатка доступна только для Яндекс Маркета")
                if str(card[1]) != "active" or not bool(card[2]):
                    raise HTTPException(status_code=409, detail="Синхронизация остатков магазина выключена")
                if bool(card[3]):
                    raise HTTPException(status_code=409, detail="Нельзя публиковать остаток архивной карточки")
                if card[4] is None or int(card[4]) != payload.target_stock:
                    raise HTTPException(
                        status_code=409,
                        detail="Сначала сохраните заданный остаток в Seller",
                    )
                cursor.execute(
                    """
                    INSERT INTO seller.yandex_stock_outbound_jobs(
                      fulfillment_id, job_kind, connection_id, external_product_id,
                      requested_stock, requested_by_user_id, next_attempt_at
                    ) VALUES (NULL,'manual',%s,%s,%s,%s,now())
                    ON CONFLICT (connection_id, external_product_id)
                      WHERE job_kind='manual' AND state IN ('queued','preparing','sending')
                    DO NOTHING
                    RETURNING id, connection_id, external_product_id, requested_stock,
                              target_stock, state, last_error, created_at, updated_at,
                              succeeded_at, failed_at
                    """,
                    (payload.connection_id, product_id, payload.target_stock, seller_user.id),
                )
                row = cursor.fetchone()
                if not row:
                    cursor.execute(
                        """
                        SELECT id, connection_id, external_product_id, requested_stock,
                               target_stock, state, last_error, created_at, updated_at,
                               succeeded_at, failed_at
                        FROM seller.yandex_stock_outbound_jobs
                        WHERE connection_id=%s AND external_product_id=%s
                          AND job_kind='manual' AND state IN ('queued','preparing','sending')
                        ORDER BY id DESC LIMIT 1
                        """,
                        (payload.connection_id, product_id),
                    )
                    row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=409, detail="Не удалось поставить публикацию в очередь")
        return stock_publication_from_row(row)

    @app.get(
        "/marketplaces/catalog/stock/publications/{job_id}",
        response_model=MarketplaceCatalogStockPublicationOut,
    )
    def get_catalog_stock_publication(
        job_id: int,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceCatalogStockPublicationOut:
        """Возвращает подтверждённое состояние ручной публикации из локальной очереди."""

        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT job.id, job.connection_id, job.external_product_id, job.requested_stock,
                           job.target_stock, job.state, job.last_error, job.created_at, job.updated_at,
                           job.succeeded_at, job.failed_at
                    FROM seller.yandex_stock_outbound_jobs AS job
                    JOIN seller.marketplace_connections AS marketplace_connection
                      ON marketplace_connection.id=job.connection_id
                    WHERE job.id=%s AND job.job_kind='manual'
                      AND marketplace_connection.workspace_id=%s
                    LIMIT 1
                    """,
                    (job_id, seller_user.workspace_id),
                )
                row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Публикация остатка не найдена")
        return stock_publication_from_row(row)

    @app.post("/marketplaces/catalog/stock/refresh", response_model=MarketplaceCatalogStockOut)
    def refresh_catalog_stock(
        payload: MarketplaceCatalogStockRefreshIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceCatalogStockOut:
        # Интерактивно читает один остаток. Вызов к Яндексу использует только POST просмотра и не публикует значение.
        offer_id = str(payload.offer_id).strip()
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT connection.provider_code, connection.campaign_id,
                           pgp_sym_decrypt(connection.token_ciphertext, %s), item.raw_payload
                    FROM seller.catalog_items AS item
                    JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id
                    WHERE connection.id=%s AND connection.workspace_id=%s AND connection.status='active'
                      AND item.is_present=true AND item.is_archived=false AND item.offer_id=%s
                    LIMIT 1
                    """,
                    (credentials_secret(), payload.connection_id, seller_user.workspace_id, offer_id),
                )
                row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Карточка или подключенный магазин не найдены")
        provider_code, campaign_id, token, raw_payload = row
        if str(provider_code) != "yandex_market":
            raise HTTPException(status_code=400, detail="Интерактивное обновление остатка пока доступно для Яндекс Маркета")
        stocks = fetch_marketplace_stocks(
            provider_code=str(provider_code),
            token=str(token),
            campaign_id=int(campaign_id) if str(campaign_id or "").isdigit() else None,
            offer_ids=[offer_id],
        )
        stock = stocks.get(offer_id, {})
        if not stock.get("found") or stock.get("available_stock") is None:
            raise HTTPException(status_code=502, detail="Яндекс Маркет не вернул актуальный остаток этой карточки")
        checked_at = datetime.now(timezone.utc)
        available_stock = max(0, int(stock["available_stock"]))
        provider_updated_at = str(stock.get("updated_at") or "")
        persisted_payload = catalog_payload_with_stock(
            raw_payload,
            available_stock=available_stock,
            checked_at=checked_at,
            provider_updated_at=provider_updated_at,
        )
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.catalog_items AS item
                    SET raw_payload=%s::jsonb
                    FROM seller.marketplace_connections AS connection
                    WHERE item.connection_id=connection.id AND connection.id=%s
                      AND connection.workspace_id=%s AND item.offer_id=%s
                    """,
                    (json.dumps(persisted_payload, ensure_ascii=False), payload.connection_id, seller_user.workspace_id, offer_id),
                )
                if cursor.rowcount != 1:
                    raise HTTPException(status_code=409, detail="Карточка изменилась во время обновления остатка")
        return MarketplaceCatalogStockOut(
            connection_id=payload.connection_id,
            offer_id=offer_id,
            available_stock=available_stock,
            checked_at=checked_at,
            provider_updated_at=provider_updated_at,
        )

    @app.get("/marketplaces/catalog/orders", response_model=MarketplaceOrderListOut)
    def list_catalog_item_orders(
        connection_id: int = Query(gt=0),
        external_product_id: str = Query(min_length=1, max_length=256),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceOrderListOut:
        # Использует только локальный снимок и точные идентификаторы товара внутри одного подключения.
        product_id = str(external_product_id).strip()
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT item.offer_id, item.sku
                    FROM seller.catalog_items AS item
                    JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id
                    WHERE item.connection_id=%s AND item.external_product_id=%s AND item.is_present=true
                      AND connection.workspace_id=%s
                    LIMIT 1
                    """,
                    (connection_id, product_id, seller_user.workspace_id),
                )
                product = cursor.fetchone()
                if not product:
                    raise HTTPException(status_code=404, detail="Карточка товара не найдена")

                offer_id = str(product[0] or "").strip()
                sku = str(product[1] or "").strip()
                identity_conditions: list[str] = []
                identity_params: list[Any] = []
                if offer_id:
                    identity_conditions.append("item.offer_id=%s")
                    identity_params.append(offer_id)
                if sku and sku != offer_id:
                    identity_conditions.append("item.sku=%s")
                    identity_params.append(sku)
                if not identity_conditions:
                    return MarketplaceOrderListOut(items=[], total=0, page=page, page_size=page_size)

                identity_where = " OR ".join(identity_conditions)
                base_params = [seller_user.workspace_id, connection_id, *identity_params]
                cursor.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM seller.order_items AS item
                    JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id
                    WHERE connection.workspace_id=%s AND item.connection_id=%s
                      AND ({identity_where})
                    """,
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
                    WHERE connection.workspace_id=%s AND item.connection_id=%s
                      AND ({identity_where})
                    ORDER BY COALESCE(item.updated_at, item.created_at, item.synced_at) DESC,
                             item.external_order_id DESC, item.external_item_id ASC
                    LIMIT %s OFFSET %s
                    """,
                    [*base_params, page_size, (page - 1) * page_size],
                )
                rows = cursor.fetchall()
        return MarketplaceOrderListOut(
            items=[marketplace_order_from_row(row) for row in rows],
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
        search_condition, search_params = ilike_search_condition(search, ORDER_SEARCH_EXPRESSIONS)
        if search_condition:
            conditions.append(search_condition)
            params.extend(search_params)
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
            items=[marketplace_order_from_row(row) for row in rows],
            total=total, page=page, page_size=page_size,
        )
