"""Read-only сбор показателей главной из API Яндекс Маркета и Ozon."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import HTTPException

from domains.marketplace_connection_verification import (
    OZON_SELLER_BASE_URL,
    YANDEX_MARKET_BASE_URL,
    _ssl_context,
)


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


def _request_ozon_json(
    url: str, *, client_id: str, token: str, payload: dict[str, Any], feature: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Client-Id": client_id,
            "Api-Key": token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40, context=_ssl_context()) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(502, f"Ozon не отдал {feature}: HTTP {exc.code}; {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"Не удалось получить {feature} Ozon: {exc.reason}")
    if not isinstance(value, dict):
        raise HTTPException(502, f"Ozon вернул некорректные данные: {feature}")
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


def fetch_ozon_pending_reviews(*, client_id: str, token: str) -> list[dict[str, Any]]:
    """Читает необработанные отзывы новой v2-модели без финансовых методов."""

    reviews_by_id: dict[str, dict[str, Any]] = {}
    last_id = ""
    seen_last_ids: set[str] = set()
    for _page in range(1000):
        body: dict[str, Any] = {
            "filters": {"status": "UNPROCESSED"},
            "limit": 100,
            "sort_dir": "DESC",
        }
        if last_id:
            body["last_id"] = last_id
        payload = _request_ozon_json(
            f"{OZON_SELLER_BASE_URL}/v2/review/list",
            client_id=client_id,
            token=token,
            payload=body,
            feature="список отзывов",
        )
        rows = payload.get("reviews") if isinstance(payload.get("reviews"), list) else []
        for review in rows:
            review_id = str(review.get("id") or "").strip() if isinstance(review, dict) else ""
            if review_id:
                reviews_by_id[review_id] = review
        if not payload.get("has_next"):
            break
        next_last_id = str(payload.get("last_id") or "").strip()
        if not next_last_id or next_last_id == last_id or next_last_id in seen_last_ids:
            raise HTTPException(502, "Ozon не продвинул постраничное чтение отзывов")
        seen_last_ids.add(next_last_id)
        last_id = next_last_id
    else:
        raise HTTPException(502, "Список отзывов Ozon превысил безопасный лимит страниц")
    return list(reviews_by_id.values())


def fetch_ozon_unread_buyer_messages(*, client_id: str, token: str) -> int:
    """Считает сообщения покупателей, исключая поддержку и системные чаты Ozon."""

    unread = 0
    cursor = ""
    seen_cursors: set[str] = set()
    for _page in range(1000):
        body: dict[str, Any] = {
            "filter": {"chat_status": "OPENED", "unread_only": True},
            "limit": 100,
        }
        if cursor:
            body["cursor"] = cursor
        payload = _request_ozon_json(
            f"{OZON_SELLER_BASE_URL}/v3/chat/list",
            client_id=client_id,
            token=token,
            payload=body,
            feature="список непрочитанных сообщений",
        )
        chats = payload.get("chats") if isinstance(payload.get("chats"), list) else []
        for row in chats:
            if not isinstance(row, dict):
                continue
            chat = row.get("chat") if isinstance(row.get("chat"), dict) else {}
            if str(chat.get("chat_type") or "") not in {"Buyer_Seller", "Buyer_Seller_Select"}:
                continue
            unread += _integer(row.get("unread_count"), minimum=0) or 0
        if not payload.get("has_next"):
            break
        next_cursor = str(payload.get("cursor") or "").strip()
        if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            raise HTTPException(502, "Ozon не продвинул постраничное чтение чатов")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise HTTPException(502, "Список чатов Ozon превысил безопасный лимит страниц")
    return unread


def review_order_id(review: dict[str, Any]) -> str:
    identifiers = review.get("identifiers") if isinstance(review.get("identifiers"), dict) else {}
    return str(identifiers.get("orderId") or identifiers.get("order_id") or "").strip()


def review_feedback_id(review: dict[str, Any]) -> int | None:
    value = review.get("feedbackId") or review.get("feedback_id") or review.get("id")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _integer(value: Any, *, minimum: int = 0, maximum: int | None = None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < minimum or (maximum is not None and parsed > maximum):
        return None
    return parsed


def save_pending_reviews(
    connection,
    *,
    workspace_id: int,
    connection_id: int,
    business_id: str,
    reviews: list[dict[str, Any]],
) -> int:
    """Атомарно заменяет локальный набор отзывов, требующих реакции конкретного магазина."""

    persisted = 0
    with connection.cursor() as cursor:
        # Этот шаг выполняется только после полного успешного ответа Маркета. Если дальнейший
        # upsert сломается, транзакция откатит и снятие need_reaction.
        cursor.execute(
            """
            UPDATE seller.marketplace_reviews
            SET need_reaction=false, updated_at=now()
            WHERE workspace_id=%s AND connection_id=%s AND need_reaction=true
            """,
            (workspace_id, connection_id),
        )
        for review in reviews:
            feedback_id = review_feedback_id(review)
            if feedback_id is None:
                continue
            identifiers = review.get("identifiers") if isinstance(review.get("identifiers"), dict) else {}
            description = review.get("description") if isinstance(review.get("description"), dict) else {}
            statistics = review.get("statistics") if isinstance(review.get("statistics"), dict) else {}
            media = review.get("media") if isinstance(review.get("media"), dict) else {}
            cursor.execute(
                """
                INSERT INTO seller.marketplace_reviews(
                  workspace_id, connection_id, provider_code, external_review_id,
                  business_id, feedback_id, reply_allowed,
                  external_order_id, offer_id, author, provider_created_at,
                  need_reaction, rating, comments_count, recommended, paid_amount,
                  advantages, disadvantages, comment_text, media_json, raw_payload,
                  last_seen_at, updated_at
                ) VALUES (
                  %s,%s,'yandex_market',%s,%s,%s,true,
                  %s,%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,now(),now()
                )
                ON CONFLICT (workspace_id, connection_id, provider_code, external_review_id) DO UPDATE SET
                  external_order_id=EXCLUDED.external_order_id,
                  offer_id=EXCLUDED.offer_id,
                  author=EXCLUDED.author,
                  provider_created_at=EXCLUDED.provider_created_at,
                  need_reaction=true,
                  reply_allowed=true,
                  rating=EXCLUDED.rating,
                  comments_count=EXCLUDED.comments_count,
                  recommended=EXCLUDED.recommended,
                  paid_amount=EXCLUDED.paid_amount,
                  advantages=EXCLUDED.advantages,
                  disadvantages=EXCLUDED.disadvantages,
                  comment_text=EXCLUDED.comment_text,
                  media_json=EXCLUDED.media_json,
                  raw_payload=EXCLUDED.raw_payload,
                  last_seen_at=now(), updated_at=now()
                """,
                (
                    workspace_id,
                    connection_id,
                    str(feedback_id),
                    business_id,
                    feedback_id,
                    str(identifiers.get("orderId") or "").strip(),
                    str(identifiers.get("offerId") or "").strip(),
                    str(review.get("author") or "").strip(),
                    review.get("createdAt"),
                    _integer(statistics.get("rating"), minimum=1, maximum=5),
                    _integer(statistics.get("commentsCount"), minimum=0) or 0,
                    statistics.get("recommended") if isinstance(statistics.get("recommended"), bool) else None,
                    statistics.get("paidAmount"),
                    str(description.get("advantages") or "").strip(),
                    str(description.get("disadvantages") or "").strip(),
                    str(description.get("comment") or "").strip(),
                    json.dumps(media, ensure_ascii=False),
                    json.dumps(review, ensure_ascii=False),
                ),
            )
            persisted += 1
    return persisted


def save_ozon_pending_reviews(
    connection,
    *,
    workspace_id: int,
    connection_id: int,
    reviews: list[dict[str, Any]],
) -> int:
    """Атомарно заменяет только необработанные отзывы выбранного Ozon-магазина."""

    persisted = 0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE seller.marketplace_reviews
            SET need_reaction=false, updated_at=now()
            WHERE workspace_id=%s AND connection_id=%s
              AND provider_code='ozon' AND need_reaction=true
            """,
            (workspace_id, connection_id),
        )
        for review in reviews:
            review_id = str(review.get("id") or "").strip()
            if not review_id:
                continue
            text = str(review.get("text") or "").strip()
            photos_count = _integer(review.get("photos_amount"), minimum=0) or 0
            videos_count = _integer(review.get("videos_amount"), minimum=0) or 0
            reply_allowed = bool(text or photos_count or videos_count)
            media = {
                "photos": [],
                "videos": [],
                "photos_count": photos_count,
                "videos_count": videos_count,
            }
            cursor.execute(
                """
                INSERT INTO seller.marketplace_reviews(
                  workspace_id, connection_id, provider_code, external_review_id,
                  business_id, feedback_id, reply_allowed,
                  external_order_id, offer_id, author, provider_created_at,
                  need_reaction, rating, comments_count, recommended, paid_amount,
                  advantages, disadvantages, comment_text, media_json, raw_payload,
                  last_seen_at, updated_at
                ) VALUES (
                  %s,%s,'ozon',%s,'',NULL,%s,
                  '',%s,'Покупатель Ozon',%s,true,%s,%s,NULL,NULL,
                  '','',%s,%s::jsonb,%s::jsonb,now(),now()
                )
                ON CONFLICT (workspace_id, connection_id, provider_code, external_review_id) DO UPDATE SET
                  offer_id=EXCLUDED.offer_id,
                  provider_created_at=EXCLUDED.provider_created_at,
                  need_reaction=true,
                  reply_allowed=EXCLUDED.reply_allowed,
                  rating=EXCLUDED.rating,
                  comments_count=EXCLUDED.comments_count,
                  comment_text=EXCLUDED.comment_text,
                  media_json=EXCLUDED.media_json,
                  raw_payload=EXCLUDED.raw_payload,
                  last_seen_at=now(), updated_at=now()
                """,
                (
                    workspace_id,
                    connection_id,
                    review_id,
                    reply_allowed,
                    str(review.get("sku") or "").strip(),
                    review.get("published_at"),
                    _integer(review.get("rating"), minimum=1, maximum=5),
                    _integer(review.get("comments_amount"), minimum=0) or 0,
                    text,
                    json.dumps(media, ensure_ascii=False),
                    json.dumps(review, ensure_ascii=False),
                ),
            )
            persisted += 1
    return persisted


def chat_campaign_id(chat: dict[str, Any]) -> str:
    context = chat.get("context") if isinstance(chat.get("context"), dict) else {}
    return str(context.get("campaignId") or context.get("campaign_id") or "").strip()


def belongs_to_connection(value: str, expected_values: set[str], *, only_connection: bool) -> bool:
    # При единственном магазине бизнес-ответ можно привязать целиком; при нескольких нужны идентификаторы.
    return only_connection or value in expected_values


def sync_dashboard_connection(connection, connection_row: tuple[Any, ...]) -> int:
    connection_id, provider_code, _name, client_id, business_id, campaign_id, token, *_rest = connection_row
    if not dashboard_insights_enabled():
        raise RuntimeError("Фоновое обновление показателей отключено")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT workspace_id FROM seller.marketplace_connections WHERE id=%s",
            (int(connection_id),),
        )
        workspace_row = cursor.fetchone()
        if not workspace_row:
            raise RuntimeError("Подключение магазина не найдено")
        workspace_id = int(workspace_row[0])

    if str(provider_code) == "ozon":
        if not str(client_id or "").strip() or not str(token or "").strip():
            raise RuntimeError("У подключения Ozon не указаны Client-Id или Api-Key")
        reviews = fetch_ozon_pending_reviews(client_id=str(client_id), token=str(token))
        unread_messages = fetch_ozon_unread_buyer_messages(client_id=str(client_id), token=str(token))
        matched_reviews = save_ozon_pending_reviews(
            connection,
            workspace_id=workspace_id,
            connection_id=int(connection_id),
            reviews=reviews,
        )
        matched_chats = unread_messages
        unassigned_reviews = 0
    elif str(provider_code) == "yandex_market":
        if not str(business_id or "").isdigit() or not str(campaign_id or "").isdigit():
            raise RuntimeError("У подключения Яндекс Маркета не указаны business_id или campaign_id")
        with connection.cursor() as cursor:
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
        matched_review_rows = [
            review for review in reviews
            if belongs_to_connection(review_order_id(review), order_ids, only_connection=only_connection)
        ]
        matched_reviews = len(matched_review_rows)
        expected_campaigns = {str(campaign_id)}
        matched_chats = sum(
            belongs_to_connection(chat_campaign_id(chat), expected_campaigns, only_connection=only_connection)
            for chat in chats
        )
        unassigned_reviews = max(0, len(reviews) - matched_reviews)
        save_pending_reviews(
            connection,
            workspace_id=workspace_id,
            connection_id=int(connection_id),
            business_id=str(business_id),
            reviews=matched_review_rows,
        )
    else:
        raise RuntimeError("Маркетплейс не поддерживает показатели главной")

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
