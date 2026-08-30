"""Read-only сбор показателей главной из Partner API Яндекс Маркета."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import HTTPException

from domains.marketplace_connection_verification import YANDEX_MARKET_BASE_URL, _ssl_context


def dashboard_insights_enabled() -> bool:
    """Глобальный kill switch не требует рестарта worker-а и не включает внешние изменения."""

    return str(os.getenv("SELLER_DASHBOARD_INSIGHTS_ENABLED", "true")).strip().lower() in {
        "1", "true", "yes",
    }


def _request_json(url: str, *, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Api-Key": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=40, context=_ssl_context()) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(502, f"Яндекс Маркет не отдал показатели: HTTP {exc.code}; {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"Не удалось получить показатели Яндекс Маркета: {exc.reason}")
    if not isinstance(value, dict):
        raise HTTPException(502, "Яндекс Маркет вернул некорректные показатели")
    return value


def _result_rows(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    rows = result.get(key) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _next_page_token(payload: dict[str, Any]) -> str:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    paging = result.get("paging") if isinstance(result, dict) and isinstance(result.get("paging"), dict) else {}
    return str(
        paging.get("nextPageToken")
        or paging.get("next_page_token")
        or (result.get("nextPageToken") if isinstance(result, dict) else "")
        or ""
    ).strip()


def _paged_yandex_rows(
    *, business_id: int, token: str, resource: str, result_key: str, body: dict[str, Any], limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_token = ""
    seen_page_tokens: set[str] = set()
    while True:
        query: dict[str, str | int] = {"limit": limit}
        if page_token:
            query["pageToken"] = page_token
        url = (
            f"{YANDEX_MARKET_BASE_URL}/v2/businesses/{business_id}/{resource}?"
            f"{urllib.parse.urlencode(query)}"
        )
        response = _request_json(url, token=token, payload=body)
        rows.extend(_result_rows(response, result_key))
        next_token = _next_page_token(response)
        if not next_token:
            break
        if next_token in seen_page_tokens:
            raise HTTPException(502, "Яндекс Маркет не продвинул постраничное чтение показателей")
        seen_page_tokens.add(next_token)
        page_token = next_token
    return rows


def fetch_yandex_pending_reviews(*, business_id: int, token: str) -> list[dict[str, Any]]:
    # NEED_REACTION соответствует отзывам, на которые продавцу нужно отреагировать.
    return _paged_yandex_rows(
        business_id=business_id,
        token=token,
        resource="goods-feedback",
        result_key="feedbacks",
        body={"reactionStatus": "NEED_REACTION"},
        limit=50,
    )


def fetch_yandex_pending_chats(*, business_id: int, token: str) -> list[dict[str, Any]]:
    # Это число диалогов, ожидающих продавца, а не число отдельных непрочитанных сообщений.
    return _paged_yandex_rows(
        business_id=business_id,
        token=token,
        resource="chats",
        result_key="chats",
        body={"types": ["CHAT"], "statuses": ["NEW", "WAITING_FOR_PARTNER"]},
        limit=20,
    )


def review_order_id(review: dict[str, Any]) -> str:
    identifiers = review.get("identifiers") if isinstance(review.get("identifiers"), dict) else {}
    return str(identifiers.get("orderId") or identifiers.get("order_id") or "").strip()


def chat_campaign_id(chat: dict[str, Any]) -> str:
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    return str(context.get("campaignId") or context.get("campaign_id") or "").strip()


def belongs_to_connection(value: str, expected_values: set[str], *, only_connection: bool) -> bool:
    # При единственном магазине бизнес-ответ можно привязать целиком; при нескольких нужны идентификаторы.
    return only_connection or value in expected_values


def sync_dashboard_connection(connection, connection_row: tuple[Any, ...]) -> int:
    connection_id, provider_code, _name, _client_id, business_id, campaign_id, token, *_rest = connection_row
    if str(provider_code) != "yandex_market":
        raise RuntimeError("Показатели отзывов и сообщений пока поддерживаются только для Яндекс Маркета")
    if not dashboard_insights_enabled():
        raise RuntimeError("Фоновое обновление показателей отключено")
    if not str(business_id or "").isdigit() or not str(campaign_id or "").isdigit():
        raise RuntimeError("У подключения Яндекс Маркета не указаны business_id или campaign_id")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT workspace_id FROM seller.marketplace_connections WHERE id=%s",
            (int(connection_id),),
        )
        workspace_row = cursor.fetchone()
        if not workspace_row:
            raise RuntimeError("Подключение магазина не найдено")
        workspace_id = int(workspace_row[0])
        cursor.execute(
            """
            SELECT count(*)
            FROM seller.marketplace_connections
            WHERE workspace_id=%s AND provider_code='yandex_market' AND business_id=%s
              AND status='active'
            """,
            (workspace_id, str(business_id)),
        )
        only_connection = int(cursor.fetchone()[0]) == 1
        cursor.execute(
            "SELECT external_order_id FROM seller.marketplace_orders WHERE connection_id=%s",
            (int(connection_id),),
        )
        order_ids = {str(row[0]) for row in cursor.fetchall()}

    reviews = fetch_yandex_pending_reviews(business_id=int(business_id), token=str(token))
    chats = fetch_yandex_pending_chats(business_id=int(business_id), token=str(token))
    matched_reviews = sum(
        belongs_to_connection(review_order_id(review), order_ids, only_connection=only_connection)
        for review in reviews
    )
    expected_campaigns = {str(campaign_id)}
    matched_chats = sum(
        belongs_to_connection(chat_campaign_id(chat), expected_campaigns, only_connection=only_connection)
        for chat in chats
    )
    unassigned_reviews = max(0, len(reviews) - matched_reviews)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO seller.marketplace_dashboard_snapshots(
                connection_id, workspace_id, pending_reviews_count, pending_chats_count,
                unassigned_reviews_count, last_successful_sync_at, last_attempt_at,
                next_refresh_at, last_error
            ) VALUES (%s, %s, %s, %s, %s, now(), now(), now() + interval '10 minutes', '')
            ON CONFLICT (connection_id) DO UPDATE SET
                workspace_id=EXCLUDED.workspace_id,
                pending_reviews_count=EXCLUDED.pending_reviews_count,
                pending_chats_count=EXCLUDED.pending_chats_count,
                unassigned_reviews_count=EXCLUDED.unassigned_reviews_count,
                last_successful_sync_at=now(), last_attempt_at=now(), last_error='', updated_at=now()
            """,
            (int(connection_id), workspace_id, matched_reviews, matched_chats, unassigned_reviews),
        )
    return matched_reviews + matched_chats
