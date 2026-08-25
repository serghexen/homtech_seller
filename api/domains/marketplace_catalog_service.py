from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import HTTPException

from .marketplace_connection_verification import OZON_SELLER_BASE_URL, YANDEX_MARKET_BASE_URL, _ssl_context


def _request_json(url: str, *, method: str, headers: dict[str, str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    # Выполняет ограниченные JSON-запросы адаптера и не пишет секреты в ошибку или журнал.
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
        method=method,
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=40, context=_ssl_context()) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(502, f"Маркетплейс не отдал данные: HTTP {exc.code}; {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"Не удалось связаться с маркетплейсом: {exc.reason}")
    if not isinstance(value, dict):
        raise HTTPException(502, "Маркетплейс вернул некорректные данные")
    return value


def _fetch_ozon_catalog(*, client_id: str, token: str) -> list[dict[str, Any]]:
    # Читает список и детали товаров Ozon, чтобы получить названия без изменения карточек или остатков.
    rows: list[dict[str, Any]] = []
    last_id = ""
    headers = {"Client-Id": client_id, "Api-Key": token}
    for _page in range(1000):
        payload = _request_json(
            f"{OZON_SELLER_BASE_URL}/v3/product/list", method="POST", headers=headers,
            payload={"filter": {"offer_id": [], "product_id": [], "visibility": "ALL"}, "last_id": last_id, "limit": 1000},
        )
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        items = result.get("items") if isinstance(result.get("items"), list) else []
        rows.extend(item for item in items if isinstance(item, dict))
        next_last_id = str(result.get("last_id") or "").strip()
        if not items or not next_last_id:
            break
        if next_last_id == last_id:
            raise HTTPException(502, "Ozon не продвинул постраничное чтение каталога")
        last_id = next_last_id
    else:
        raise HTTPException(502, "Каталог Ozon превысил безопасный лимит страниц")
    details: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(rows), 1000):
        ids = [item.get("product_id") for item in rows[offset:offset + 1000] if str(item.get("product_id") or "").isdigit()]
        if not ids:
            continue
        payload = _request_json(
            f"{OZON_SELLER_BASE_URL}/v3/product/info/list", method="POST", headers=headers,
            payload={"offer_id": [], "product_id": ids, "sku": []},
        )
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        for item in payload.get("items") if isinstance(payload.get("items"), list) else result.get("items", []):
            # В подробном ответе Ozon идентификатор называется id, а в списке — product_id, поэтому связываем оба варианта.
            product_id = str(item.get("product_id") or item.get("id") or "").strip() if isinstance(item, dict) else ""
            if product_id.isdigit():
                details[product_id] = item
    return [{**row, **details.get(str(row.get("product_id") or row.get("id") or ""), {})} for row in rows]


def _fetch_yandex_catalog(*, business_id: int, campaign_id: int | None, token: str) -> list[dict[str, Any]]:
    # Читает активные и архивные карточки выбранного магазина постранично, не изменяя офферы.
    rows: list[dict[str, Any]] = []
    for archived in (False, True):
        page_token = ""
        for _page in range(1000):
            query: dict[str, int | str] = {"limit": 100}
            if page_token:
                query["pageToken"] = page_token
            payload = _request_json(
                f"{YANDEX_MARKET_BASE_URL}/v2/businesses/{business_id}/offer-mappings?{urllib.parse.urlencode(query)}",
                method="POST", headers={"Api-Key": token}, payload={"archived": archived},
            )
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            items = result.get("offerMappings") if isinstance(result.get("offerMappings"), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                offer = item.get("offer") if isinstance(item.get("offer"), dict) else {}
                campaigns = offer.get("campaigns") if isinstance(offer.get("campaigns"), list) else []
                campaign_ids = {
                    int(campaign.get("campaignId"))
                    for campaign in campaigns
                    if isinstance(campaign, dict) and str(campaign.get("campaignId") or "").isdigit()
                }
                if campaign_id is not None and campaign_ids and campaign_id not in campaign_ids:
                    continue
                rows.append({**item, "offer": {**offer, "archived": archived}})
            paging = result.get("paging") if isinstance(result.get("paging"), dict) else {}
            next_page_token = str(paging.get("nextPageToken") or "").strip()
            if not next_page_token:
                break
            if next_page_token == page_token:
                raise HTTPException(502, "Яндекс Маркет не продвинул постраничное чтение каталога")
            page_token = next_page_token
        else:
            raise HTTPException(502, "Каталог Яндекс Маркета превысил безопасный лимит страниц")
    return rows


def update_yandex_catalog_archive(
    *, business_id: int, token: str, offer_id: str, archived: bool,
) -> dict[str, Any]:
    # Использует штатные archive/unarchive методы Яндекса и не меняет остатки либо содержимое карточки.
    normalized_offer_id = str(offer_id or "").strip()
    if not normalized_offer_id:
        raise HTTPException(400, "Не удалось определить SKU карточки")
    action = "archive" if archived else "unarchive"
    return _request_json(
        f"{YANDEX_MARKET_BASE_URL}/v2/businesses/{business_id}/offer-mappings/{action}",
        method="POST", headers={"Api-Key": token}, payload={"offerIds": [normalized_offer_id]},
    )


def _fetch_yandex_stocks(*, campaign_id: int, token: str, offer_ids: list[str]) -> dict[str, dict[str, Any]]:
    # Читает остатки Яндекс Маркета пакетами по 500 SKU. POST этого метода ничего не меняет в кабинете.
    normalized_ids = list(dict.fromkeys(str(offer_id or "").strip() for offer_id in offer_ids if str(offer_id or "").strip()))
    stocks_by_offer: dict[str, dict[str, Any]] = {
        offer_id: {"found": False, "available_stock": None, "updated_at": ""}
        for offer_id in normalized_ids
    }
    headers = {"Api-Key": token}
    for offset in range(0, len(normalized_ids), 500):
        batch = normalized_ids[offset:offset + 500]
        payload = _request_json(
            f"{YANDEX_MARKET_BASE_URL}/v2/campaigns/{campaign_id}/offers/stocks",
            method="POST",
            headers=headers,
            payload={"offerIds": batch, "withTurnover": False},
        )
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        warehouses = result.get("warehouses") if isinstance(result.get("warehouses"), list) else []
        for warehouse in warehouses:
            offers = warehouse.get("offers") if isinstance(warehouse, dict) and isinstance(warehouse.get("offers"), list) else []
            for offer in offers:
                offer_id = str(offer.get("offerId") or "").strip() if isinstance(offer, dict) else ""
                if offer_id not in stocks_by_offer:
                    continue
                snapshot = stocks_by_offer[offer_id]
                snapshot["found"] = True
                snapshot["available_stock"] = int(snapshot["available_stock"] or 0)
                snapshot["updated_at"] = max(str(snapshot["updated_at"] or ""), str(offer.get("updatedAt") or "").strip())
                rows = offer.get("stocks") if isinstance(offer.get("stocks"), list) else []
                for stock in rows:
                    if not isinstance(stock, dict) or str(stock.get("type") or "").upper() != "AVAILABLE":
                        continue
                    try:
                        snapshot["available_stock"] += max(0, int(stock.get("count") or 0))
                    except (TypeError, ValueError):
                        continue
    return stocks_by_offer


def fetch_marketplace_catalog(
    *, provider_code: str, token: str, client_id: str, business_id: int | None, campaign_id: int | None,
) -> list[dict[str, Any]]:
    # Выбирает строго read-only адаптер нужного маркетплейса и не предоставляет операций отправки.
    if provider_code == "ozon":
        return _fetch_ozon_catalog(client_id=client_id, token=token)
    if provider_code == "yandex_market" and business_id:
        return _fetch_yandex_catalog(business_id=business_id, campaign_id=campaign_id, token=token)
    raise HTTPException(400, "Для чтения каталога не хватает реквизитов подключенного магазина")


def fetch_marketplace_stocks(
    *, provider_code: str, token: str, campaign_id: int | None, offer_ids: list[str],
) -> dict[str, dict[str, Any]]:
    # На текущем этапе отдельный снимок остатков нужен Яндекс Маркету; Ozon продолжает отдавать их в деталях товара.
    if provider_code == "yandex_market" and campaign_id:
        return _fetch_yandex_stocks(campaign_id=campaign_id, token=token, offer_ids=offer_ids)
    if provider_code == "ozon":
        return {}
    raise HTTPException(400, "Для чтения остатков не хватает идентификатора магазина")
