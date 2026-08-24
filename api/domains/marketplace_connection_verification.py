from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import HTTPException


OZON_SELLER_BASE_URL = "https://api-seller.ozon.ru"
YANDEX_MARKET_BASE_URL = "https://api.partner.market.yandex.ru"


def _ssl_context() -> ssl.SSLContext:
    # Использует certifi для доверенной цепочки Ozon и Яндекса, когда системные сертификаты неполны.
    ca_cert_path = str(os.getenv("MARKETPLACE_CA_CERT_PATH", "")).strip()
    if ca_cert_path:
        return ssl.create_default_context(cafile=ca_cert_path)
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _read_json(request: urllib.request.Request, *, timeout: int = 20) -> dict[str, Any]:
    # Выполняет короткую проверку реквизитов и отдает только разобранный JSON без записи токена в логи.
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        if exc.code in {401, 403}:
            raise HTTPException(400, "Маркетплейс отклонил реквизиты или у токена нет нужного доступа")
        raise HTTPException(502, f"Маркетплейс вернул ошибку {exc.code}: {message[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"Не удалось связаться с маркетплейсом: {exc.reason}")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "Маркетплейс вернул некорректный ответ при проверке") from exc
    if not isinstance(value, dict):
        raise HTTPException(502, "Маркетплейс вернул неожиданный ответ при проверке")
    return value


def verify_ozon_connection(*, client_id: str, token: str) -> None:
    # Проверяет пару Ozon Client ID и API Key безопасным запросом до сохранения кабинета.
    request = urllib.request.Request(
        f"{OZON_SELLER_BASE_URL}/v1/description-category/tree",
        data=b"{}",
        method="POST",
        headers={"Client-Id": client_id, "Api-Key": token, "Content-Type": "application/json"},
    )
    _read_json(request)


def discover_yandex_market_stores(*, token: str) -> list[dict[str, Any]]:
    # Находит доступные кабинеты и магазины по API-Key, чтобы пользователь не вводил технические ID вручную.
    rows: list[Any] = []
    page_token = ""
    for _page in range(1000):
        query: dict[str, int | str] = {"limit": 100}
        if page_token:
            query["pageToken"] = page_token
        request = urllib.request.Request(
            f"{YANDEX_MARKET_BASE_URL}/v2/campaigns?{urllib.parse.urlencode(query)}",
            method="GET",
            headers={"Api-Key": token, "Content-Type": "application/json"},
        )
        payload = _read_json(request)
        campaigns = payload.get("campaigns") if isinstance(payload.get("campaigns"), list) else []
        rows.extend(campaigns)
        paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
        next_page_token = str(paging.get("nextPageToken") or "")
        if not next_page_token:
            break
        if next_page_token == page_token:
            raise HTTPException(502, "Яндекс Маркет не продвинул постраничное чтение магазинов")
        page_token = next_page_token
    else:
        raise HTTPException(502, "Список магазинов Яндекс Маркета превысил безопасный лимит страниц")

    stores: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        business = row.get("business") if isinstance(row.get("business"), dict) else {}
        business_id = row.get("businessId", business.get("id"))
        campaign_id = row.get("campaignId", row.get("id"))
        try:
            normalized_business_id, normalized_campaign_id = int(business_id), int(campaign_id)
        except (TypeError, ValueError):
            continue
        key = normalized_business_id, normalized_campaign_id
        if key in seen:
            continue
        seen.add(key)
        title = str(row.get("name") or row.get("domain") or business.get("name") or f"Магазин {normalized_campaign_id}").strip()
        stores.append({"business_id": normalized_business_id, "campaign_id": normalized_campaign_id, "display_name": title[:120]})
    if not stores:
        raise HTTPException(400, "По этому API-Key не найдено доступных магазинов Яндекс Маркета")
    return stores
