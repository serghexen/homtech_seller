"""Workspace-scoped просмотр отзывов и постановка только ручных ответов в outbox."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from domains.local_auth import AuthenticatedUser
from domains.ozon_review_replies import ozon_review_reply_enabled
from domains.yandex_review_replies import review_reply_enabled


ReplyState = Literal["queued", "preparing", "sending", "submitted", "unknown", "failed"]


class MarketplaceReviewReplyOut(BaseModel):
    id: int
    state: ReplyState
    text: str
    provider_status: str = ""
    last_error: str = ""
    created_at: datetime
    submitted_at: datetime | None = None


class MarketplaceReviewOut(BaseModel):
    id: int
    connection_id: int
    provider_code: str
    store_name: str
    external_review_id: str
    feedback_id: int | None = None
    external_order_id: str = ""
    offer_id: str = ""
    product_title: str = ""
    author: str = ""
    created_at: datetime | None = None
    need_reaction: bool
    rating: int | None = None
    comments_count: int = 0
    recommended: bool | None = None
    paid_amount: str | None = None
    advantages: str = ""
    disadvantages: str = ""
    comment: str = ""
    photos: list[str] = Field(default_factory=list)
    videos: list[str] = Field(default_factory=list)
    reply_allowed: bool = True
    reply_disabled_reason: str = ""
    can_reply: bool = False
    reply: MarketplaceReviewReplyOut | None = None


class MarketplaceReviewListOut(BaseModel):
    items: list[MarketplaceReviewOut]
    total: int
    pending_total: int
    page: int
    page_size: int


class MarketplaceReviewReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


def _money(value: Decimal | None) -> str | None:
    return format(value, ".2f") if value is not None else None


def _media_values(value, key: str) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        return []
    return [str(item) for item in value[key] if str(item or "").strip()]


def _provider_reply_enabled(provider_code: str) -> bool:
    if provider_code == "yandex_market":
        return review_reply_enabled()
    if provider_code == "ozon":
        return ozon_review_reply_enabled()
    return False


def mount_marketplace_review_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    def workspace_for_user(connection, user: AuthenticatedUser):
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        return seller_user

    @app.get("/marketplaces/reviews", response_model=MarketplaceReviewListOut)
    def list_marketplace_reviews(
        connection_id: int | None = Query(default=None, ge=1),
        state: Literal["pending", "all"] = "pending",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceReviewListOut:
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            filters = ["review.workspace_id=%s", "market.provider_code IN ('yandex_market','ozon')"]
            params: list[object] = [seller_user.workspace_id]
            if connection_id is not None:
                filters.append("review.connection_id=%s")
                params.append(connection_id)
            if state == "pending":
                filters.append("review.need_reaction=true")
            where = " AND ".join(filters)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT count(*), count(*) FILTER (WHERE review.need_reaction=true)
                    FROM seller.marketplace_reviews AS review
                    JOIN seller.marketplace_connections AS market
                      ON market.id=review.connection_id AND market.workspace_id=review.workspace_id
                    WHERE {where}
                    """,
                    tuple(params),
                )
                count_row = cursor.fetchone()
                total = int(count_row[0])
                # pending_total должен учитывать выбранный магазин, но не активную вкладку.
                pending_filters = [
                    "review.workspace_id=%s",
                    "market.provider_code IN ('yandex_market','ozon')",
                    "review.need_reaction=true",
                ]
                pending_params: list[object] = [seller_user.workspace_id]
                if connection_id is not None:
                    pending_filters.append("review.connection_id=%s")
                    pending_params.append(connection_id)
                cursor.execute(
                    f"""
                    SELECT count(*)
                    FROM seller.marketplace_reviews AS review
                    JOIN seller.marketplace_connections AS market
                      ON market.id=review.connection_id AND market.workspace_id=review.workspace_id
                    WHERE {' AND '.join(pending_filters)}
                    """,
                    tuple(pending_params),
                )
                pending_total = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""
                    SELECT review.id, review.connection_id, market.provider_code, market.display_name,
                           review.external_review_id, review.feedback_id,
                           review.external_order_id, review.offer_id,
                           COALESCE(product.title, ''), review.author, review.provider_created_at,
                           review.need_reaction, review.rating, review.comments_count,
                           review.recommended, review.paid_amount, review.advantages,
                           review.disadvantages, review.comment_text, review.media_json,
                           review.reply_allowed, market.status, market.review_reply_enabled,
                           reply.id, reply.state, reply.response_text,
                           reply.provider_status, reply.last_error, reply.created_at,
                           reply.submitted_at
                    FROM seller.marketplace_reviews AS review
                    JOIN seller.marketplace_connections AS market
                      ON market.id=review.connection_id AND market.workspace_id=review.workspace_id
                    LEFT JOIN LATERAL (
                      SELECT item.title
                      FROM seller.catalog_items AS item
                      WHERE item.connection_id=review.connection_id
                        AND (item.offer_id=review.offer_id OR item.external_product_id=review.offer_id
                             OR item.sku=review.offer_id)
                      ORDER BY item.is_present DESC, item.synced_at DESC
                      LIMIT 1
                    ) AS product ON true
                    LEFT JOIN LATERAL (
                      SELECT job.id, job.state, job.response_text, job.provider_status,
                             job.last_error, job.created_at, job.submitted_at
                      FROM seller.marketplace_review_reply_jobs AS job
                      WHERE job.review_id=review.id
                      ORDER BY job.created_at DESC, job.id DESC
                      LIMIT 1
                    ) AS reply ON true
                    WHERE {where}
                    ORDER BY review.need_reaction DESC,
                             review.provider_created_at DESC NULLS LAST, review.id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (*params, page_size, (page - 1) * page_size),
                )
                rows = cursor.fetchall()

        items: list[MarketplaceReviewOut] = []
        for row in rows:
            reply = None
            if row[23] is not None:
                reply = MarketplaceReviewReplyOut(
                    id=int(row[23]),
                    state=str(row[24]),
                    text=str(row[25]),
                    provider_status=str(row[26] or ""),
                    last_error=str(row[27] or ""),
                    created_at=row[28],
                    submitted_at=row[29],
                )
            active_reply = reply is not None and reply.state in {"queued", "preparing", "sending", "unknown"}
            provider_code = str(row[2])
            reply_allowed = bool(row[20])
            items.append(
                MarketplaceReviewOut(
                    id=int(row[0]),
                    connection_id=int(row[1]),
                    provider_code=provider_code,
                    store_name=str(row[3]),
                    external_review_id=str(row[4]),
                    feedback_id=int(row[5]) if row[5] is not None else None,
                    external_order_id=str(row[6] or ""),
                    offer_id=str(row[7] or ""),
                    product_title=str(row[8] or ""),
                    author=str(row[9] or ""),
                    created_at=row[10],
                    need_reaction=bool(row[11]),
                    rating=int(row[12]) if row[12] is not None else None,
                    comments_count=int(row[13] or 0),
                    recommended=row[14] if isinstance(row[14], bool) else None,
                    paid_amount=_money(row[15]),
                    advantages=str(row[16] or ""),
                    disadvantages=str(row[17] or ""),
                    comment=str(row[18] or ""),
                    photos=_media_values(row[19], "photos"),
                    videos=_media_values(row[19], "videos"),
                    reply_allowed=reply_allowed,
                    reply_disabled_reason=(
                        "Ozon не принимает ответы на отзывы без текста, фото или видео"
                        if provider_code == "ozon" and not reply_allowed else ""
                    ),
                    can_reply=bool(
                        _provider_reply_enabled(provider_code)
                        and row[21] == "active" and row[22] and row[11]
                        and reply_allowed and not active_reply
                    ),
                    reply=reply,
                )
            )
        return MarketplaceReviewListOut(
            items=items,
            total=total,
            pending_total=pending_total,
            page=page,
            page_size=page_size,
        )

    @app.post("/marketplaces/reviews/{review_id}/reply", response_model=MarketplaceReviewReplyOut, status_code=202)
    def reply_to_marketplace_review(
        review_id: int,
        payload: MarketplaceReviewReplyIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceReviewReplyOut:
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Напишите текст ответа")
        if len(text) > 4096:
            raise HTTPException(status_code=400, detail="Ответ не должен быть длиннее 4096 символов")
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT review.connection_id, review.need_reaction,
                           market.status, market.provider_code, market.review_reply_enabled,
                           review.reply_allowed
                    FROM seller.marketplace_reviews AS review
                    JOIN seller.marketplace_connections AS market
                      ON market.id=review.connection_id AND market.workspace_id=review.workspace_id
                    WHERE review.id=%s AND review.workspace_id=%s
                    FOR UPDATE OF review
                    """,
                    (review_id, seller_user.workspace_id),
                )
                review_row = cursor.fetchone()
                if not review_row:
                    raise HTTPException(status_code=404, detail="Отзыв не найден")
                provider_code = str(review_row[3])
                if str(review_row[2]) != "active" or provider_code not in {"yandex_market", "ozon"}:
                    raise HTTPException(status_code=409, detail="Магазин недоступен для ответа")
                if not _provider_reply_enabled(provider_code):
                    raise HTTPException(status_code=409, detail="Публикация ответов для этого маркетплейса пока выключена")
                if not bool(review_row[4]):
                    raise HTTPException(status_code=409, detail="Ответы для этого магазина пока выключены")
                if not bool(review_row[1]):
                    raise HTTPException(status_code=409, detail="Отзыв уже обработан")
                if not bool(review_row[5]):
                    raise HTTPException(
                        status_code=409,
                        detail="Ozon не принимает ответы на отзывы без текста, фото или видео",
                    )
                connection_id = int(review_row[0])
                cursor.execute(
                    """
                    INSERT INTO seller.marketplace_review_reply_jobs(
                      workspace_id, connection_id, review_id, actor_user_id, response_text
                    ) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (review_id)
                      WHERE state IN ('queued', 'preparing', 'sending', 'unknown')
                    DO NOTHING
                    RETURNING id, state, response_text, provider_status, last_error,
                              created_at, submitted_at
                    """,
                    (seller_user.workspace_id, connection_id, review_id, seller_user.id, text),
                )
                row = cursor.fetchone()
                if not row:
                    cursor.execute(
                        """
                        SELECT id, state, response_text, provider_status, last_error,
                               created_at, submitted_at
                        FROM seller.marketplace_review_reply_jobs
                        WHERE review_id=%s
                          AND state IN ('queued', 'preparing', 'sending', 'unknown')
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                        """,
                        (review_id,),
                    )
                    row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=409, detail="Не удалось поставить ответ в очередь")
        return MarketplaceReviewReplyOut(
            id=int(row[0]),
            state=str(row[1]),
            text=str(row[2]),
            provider_status=str(row[3] or ""),
            last_error=str(row[4] or ""),
            created_at=row[5],
            submitted_at=row[6],
        )
