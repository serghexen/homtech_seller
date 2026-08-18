from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from .marketplace_connection_verification import OZON_SELLER_BASE_URL, YANDEX_MARKET_BASE_URL, _ssl_context


def normalize_marketplace_order_status(*, provider_code: str, status: str, substatus: str = "") -> str:
    # Приводит статусы разных маркетплейсов к малому справочнику, сохраняя неизвестные случаи для ручной проверки.
    normalized_status = str(status or "").strip().lower()
    normalized_substatus = str(substatus or "").strip().lower()
    if provider_code == "yandex_market":
        if normalized_status in {"processing", "pending", "reserved", "unpaid", "placing"}:
            return "processing"
        if normalized_status in {"delivery", "pickup"}:
            return "in_delivery"
        if normalized_status == "delivered":
            return "delivered"
        if normalized_status == "cancelled":
            return "cancelled"
        return "problem"
    if provider_code == "ozon":
        if normalized_status in {"done", "delivered"}:
            return "delivered"
        if "cancel" in normalized_status:
            return "cancelled"
        if normalized_status in {"delivering", "in_delivery"}:
            return "in_delivery"
        if normalized_status in {"awaiting_deliver", "awaiting_delivery", "awaiting_registration", "accepted"}:
            return "processing"
        return "problem"
    return "problem"


def _request_json(url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    # Выполняет только чтение заказов и не передает токены дальше внешнего API.
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=40, context=_ssl_context()) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(502, f"Маркетплейс не отдал заказы: HTTP {exc.code}; {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"Не удалось связаться с маркетплейсом: {exc.reason}")
    if not isinstance(value, dict):
        raise HTTPException(502, "Маркетплейс вернул некорректный список заказов")
    return value


def _fetch_ozon_orders(*, client_id: str, token: str) -> list[dict[str, Any]]:
    # Объединяет цифровые и FBO-отправления, чтобы заказы услуг не пропадали из общего списка.
    digital_rows = _fetch_ozon_digital_orders(client_id=client_id, token=token)
    fbo_rows = _fetch_ozon_fbo_orders(client_id=client_id, token=token)
    rows_by_posting: dict[str, dict[str, Any]] = {}
    for row in [*digital_rows, *fbo_rows]:
        posting_number = str(row.get("posting_number") or "").strip()
        if posting_number:
            rows_by_posting[posting_number] = row
    return list(rows_by_posting.values())


def _fetch_ozon_digital_orders(*, client_id: str, token: str) -> list[dict[str, Any]]:
    # Читает цифровые отправления за последние 24 часа без загрузки ключей и изменения статусов.
    rows: list[dict[str, Any]] = []
    cursor = ""
    now = datetime.now(timezone.utc)
    headers = {"Client-Id": client_id, "Api-Key": token}
    for _page in range(100):
        payload = _request_json(
            f"{OZON_SELLER_BASE_URL}/v2/posting/digital/list",
            headers=headers,
            payload={
                "cursor": cursor,
                "filter": {
                    "since": (now - timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    "to": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                },
                "limit": 100,
                "sort_dir": "DESC",
            },
        )
        postings = payload.get("postings") if isinstance(payload.get("postings"), list) else []
        rows.extend({**item, "__marketplace_source": "DIGITAL"} for item in postings if isinstance(item, dict))
        if not payload.get("has_next"):
            break
        next_cursor = str(payload.get("cursor") or "").strip()
        if not next_cursor or next_cursor == cursor:
            raise HTTPException(502, "Ozon не продвинул постраничное чтение заказов")
        cursor = next_cursor
    else:
        raise HTTPException(502, "Список заказов Ozon превысил безопасный лимит страниц")
    return rows


def _fetch_ozon_fbo_orders(*, client_id: str, token: str) -> list[dict[str, Any]]:
    # Читает FBO-заказы услуг за последние 24 часа, которые Ozon возвращает массивом result.
    rows: list[dict[str, Any]] = []
    offset = 0
    limit = 100
    now = datetime.now(timezone.utc)
    headers = {"Client-Id": client_id, "Api-Key": token}
    for _page in range(100):
        payload = _request_json(
            f"{OZON_SELLER_BASE_URL}/v2/posting/fbo/list",
            headers=headers,
            payload={
                "dir": "DESC",
                "filter": {
                    "since": (now - timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    "to": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    "status": "",
                },
                "limit": limit,
                "offset": offset,
                "with": {"analytics_data": False, "financial_data": False},
            },
        )
        postings = payload.get("result") if isinstance(payload.get("result"), list) else []
        rows.extend({**item, "__marketplace_source": "FBO"} for item in postings if isinstance(item, dict))
        if len(postings) < limit:
            break
        offset += len(postings)
    else:
        raise HTTPException(502, "Список FBO-заказов Ozon превысил безопасный лимит страниц")
    return rows


def _fetch_yandex_market_orders(*, business_id: int, campaign_id: int, token: str) -> list[dict[str, Any]]:
    # Читает все DBS-заказы за текущий календарный день и не подтверждает цифровую доставку.
    rows: list[dict[str, Any]] = []
    page_token = ""
    max_pages = 100
    today = datetime.now(timezone.utc).date()
    period_from = today
    period_to = today + timedelta(days=1)
    for page_number in range(1, max_pages + 1):
        query = {"limit": "50"}
        if page_token:
            query["pageToken"] = page_token
        payload = _request_json(
            f"{YANDEX_MARKET_BASE_URL}/v1/businesses/{business_id}/orders?{urllib.parse.urlencode(query)}",
            headers={"Api-Key": token},
            payload={
                "campaignIds": [campaign_id],
                "programTypes": ["DBS"],
                "dates": {"creationDateFrom": period_from.isoformat(), "creationDateTo": period_to.isoformat()},
            },
        )
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        orders = result.get("orders") if isinstance(result.get("orders"), list) else []
        rows.extend(
            item for item in orders
            if isinstance(item, dict) and str(item.get("campaignId") or "") == str(campaign_id)
        )
        paging = result.get("paging") if isinstance(result.get("paging"), dict) else {}
        next_page_token = str(paging.get("nextPageToken") or "").strip()
        if not next_page_token:
            break
        if next_page_token == page_token:
            raise HTTPException(502, "Яндекс Маркет не продвинул постраничное чтение заказов")
        if page_number == max_pages:
            raise HTTPException(502, "Список заказов Яндекс Маркета превысил безопасный лимит страниц")
        page_token = next_page_token
    return rows


def fetch_marketplace_orders(*, provider_code: str, token: str, client_id: str, business_id: int | None, campaign_id: int | None) -> list[dict[str, Any]]:
    # Выбирает read-only адаптер заказов и намеренно не содержит операций выдачи или подтверждения.
    if provider_code == "ozon":
        return _fetch_ozon_orders(client_id=client_id, token=token)
    if provider_code == "yandex_market" and business_id and campaign_id:
        return _fetch_yandex_market_orders(business_id=business_id, campaign_id=campaign_id, token=token)
    raise HTTPException(400, "Для чтения заказов не хватает реквизитов подключенного магазина")
