from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from .marketplace_connection_verification import OZON_SELLER_BASE_URL, YANDEX_MARKET_BASE_URL, _ssl_context


ORDER_INITIAL_BACKFILL_DAYS = 30
ORDER_SYNC_OVERLAP = timedelta(minutes=5)
ORDER_SYNC_CHUNK = timedelta(days=30)
YANDEX_ORDER_SYNC_CHUNK = timedelta(days=1)


class MarketplacePaginationError(RuntimeError):
    """Маркетплейс вернул некорректную последовательность токенов страниц."""


def _as_utc(value: datetime | None, *, default: datetime) -> datetime:
    # Нормализует watermark из PostgreSQL и тестов, чтобы интервалы не смешивали aware и naive datetime.
    if value is None:
        return default
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _utc_text(value: datetime) -> str:
    # Формирует единый ISO 8601 UTC для фильтров обоих маркетплейсов.
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _split_period(
    period_from: datetime, period_to: datetime, *, chunk: timedelta = ORDER_SYNC_CHUNK,
) -> list[tuple[datetime, datetime]]:
    # Делит длинный простой на ограниченные запросы без разрыва между интервалами.
    periods: list[tuple[datetime, datetime]] = []
    cursor = period_from
    while cursor < period_to:
        next_cursor = min(cursor + chunk, period_to)
        periods.append((cursor, next_cursor))
        cursor = next_cursor
    return periods


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


def _fetch_ozon_orders(
    *, client_id: str, token: str, synced_after: datetime | None = None, synced_before: datetime | None = None,
) -> list[dict[str, Any]]:
    # Объединяет цифровые и FBO-отправления, чтобы заказы услуг не пропадали из общего списка.
    period_to = _as_utc(synced_before, default=datetime.now(timezone.utc))
    rolling_from = period_to - timedelta(days=ORDER_INITIAL_BACKFILL_DAYS)
    last_sync = _as_utc(synced_after, default=rolling_from)
    period_from = min(last_sync - ORDER_SYNC_OVERLAP, rolling_from) if synced_after else rolling_from
    digital_rows: list[dict[str, Any]] = []
    fbo_rows: list[dict[str, Any]] = []
    for chunk_from, chunk_to in _split_period(period_from, period_to):
        digital_rows.extend(
            _fetch_ozon_digital_orders(
                client_id=client_id, token=token, period_from=chunk_from, period_to=chunk_to,
            )
        )
        fbo_rows.extend(
            _fetch_ozon_fbo_orders(
                client_id=client_id, token=token, period_from=chunk_from, period_to=chunk_to,
            )
        )
    rows_by_posting: dict[str, dict[str, Any]] = {}
    for row in [*digital_rows, *fbo_rows]:
        posting_number = str(row.get("posting_number") or "").strip()
        if posting_number:
            rows_by_posting[posting_number] = row
    return list(rows_by_posting.values())


def _fetch_ozon_digital_orders(
    *, client_id: str, token: str, period_from: datetime, period_to: datetime,
) -> list[dict[str, Any]]:
    # Читает цифровые отправления заданного периода без загрузки ключей и изменения статусов.
    rows: list[dict[str, Any]] = []
    cursor = ""
    headers = {"Client-Id": client_id, "Api-Key": token}
    for _page in range(100):
        payload = _request_json(
            f"{OZON_SELLER_BASE_URL}/v2/posting/digital/list",
            headers=headers,
            payload={
                "cursor": cursor,
                "filter": {
                    "since": _utc_text(period_from),
                    "to": _utc_text(period_to),
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


def _fetch_ozon_fbo_orders(
    *, client_id: str, token: str, period_from: datetime, period_to: datetime,
) -> list[dict[str, Any]]:
    # Читает FBO-заказы услуг заданного периода, которые Ozon возвращает массивом result.
    rows: list[dict[str, Any]] = []
    offset = 0
    limit = 100
    headers = {"Client-Id": client_id, "Api-Key": token}
    for _page in range(100):
        payload = _request_json(
            f"{OZON_SELLER_BASE_URL}/v2/posting/fbo/list",
            headers=headers,
            payload={
                "dir": "DESC",
                "filter": {
                    "since": _utc_text(period_from),
                    "to": _utc_text(period_to),
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


def _fetch_yandex_market_orders(
    *, business_id: int, campaign_id: int, token: str,
    synced_after: datetime | None = None, synced_before: datetime | None = None,
) -> list[dict[str, Any]]:
    # Первый запуск читает 30 дней DBS-заказов, следующие — изменения после успешного watermark.
    period_to = _as_utc(synced_before, default=datetime.now(timezone.utc))
    if synced_after is None:
        creation_to = period_to.date() + timedelta(days=1)
        creation_from = creation_to - timedelta(days=ORDER_INITIAL_BACKFILL_DAYS)
        date_filters = [
            {"creationDateFrom": chunk_from.date().isoformat(), "creationDateTo": chunk_to.date().isoformat()}
            for chunk_from, chunk_to in _split_period(
                datetime.combine(creation_from, datetime.min.time(), tzinfo=timezone.utc),
                datetime.combine(creation_to, datetime.min.time(), tzinfo=timezone.utc),
                chunk=YANDEX_ORDER_SYNC_CHUNK,
            )
        ]
    else:
        update_from = _as_utc(synced_after, default=period_to) - ORDER_SYNC_OVERLAP
        if update_from >= period_to:
            update_from = period_to - ORDER_SYNC_OVERLAP
        date_filters = [
            {"updateDateFrom": _utc_text(chunk_from), "updateDateTo": _utc_text(chunk_to)}
            for chunk_from, chunk_to in _split_period(
                update_from, period_to, chunk=YANDEX_ORDER_SYNC_CHUNK,
            )
        ]

    rows_by_order: dict[str, dict[str, Any]] = {}
    for dates in date_filters:
        page_token = ""
        seen_page_tokens: set[str] = set()
        while True:
            query = {"limit": "50"}
            if page_token:
                query["pageToken"] = page_token
            payload = _request_json(
                f"{YANDEX_MARKET_BASE_URL}/v1/businesses/{business_id}/orders?{urllib.parse.urlencode(query)}",
                headers={"Api-Key": token},
                payload={"campaignIds": [campaign_id], "programTypes": ["DBS"], "dates": dates},
            )
            result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
            orders = result.get("orders") if isinstance(result.get("orders"), list) else []
            for item in orders:
                if not isinstance(item, dict) or str(item.get("campaignId") or "") != str(campaign_id):
                    continue
                order_id = str(item.get("orderId") or item.get("id") or "").strip()
                if order_id:
                    rows_by_order[order_id] = item
            paging = result.get("paging") if isinstance(result.get("paging"), dict) else {}
            next_page_token = str(paging.get("nextPageToken") or "").strip()
            if not next_page_token:
                break
            if next_page_token == page_token or next_page_token in seen_page_tokens:
                raise MarketplacePaginationError("Яндекс Маркет зациклил постраничное чтение заказов")
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token
    return list(rows_by_order.values())


def fetch_marketplace_orders(
    *, provider_code: str, token: str, client_id: str, business_id: int | None, campaign_id: int | None,
    synced_after: datetime | None = None, synced_before: datetime | None = None,
) -> list[dict[str, Any]]:
    # Выбирает read-only адаптер заказов и намеренно не содержит операций выдачи или подтверждения.
    sync_period = {"synced_after": synced_after, "synced_before": synced_before}
    if provider_code == "ozon":
        return _fetch_ozon_orders(client_id=client_id, token=token, **sync_period)
    if provider_code == "yandex_market" and business_id and campaign_id:
        return _fetch_yandex_market_orders(
            business_id=business_id, campaign_id=campaign_id, token=token, **sync_period,
        )
    raise HTTPException(400, "Для чтения заказов не хватает реквизитов подключенного магазина")
