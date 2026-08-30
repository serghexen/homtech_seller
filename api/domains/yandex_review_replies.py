"""Долговечная ручная отправка ответов на отзывы без слепых повторов."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable
from uuid import UUID

from domains.marketplace_connection_verification import YANDEX_MARKET_BASE_URL, _ssl_context
from domains.marketplace_sync_service import credentials_secret


REVIEW_REPLY_LOCK_SECONDS = 120


def review_reply_enabled() -> bool:
    return str(os.getenv("SELLER_YANDEX_REVIEW_REPLY_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes",
    }


def review_reply_timeout_seconds() -> int:
    return max(3, min(int(os.getenv("YANDEX_MARKET_REVIEW_REPLY_TIMEOUT_SECONDS", "20")), 60))


class YandexReviewReplyError(RuntimeError):
    def __init__(self, message: str, *, definite: bool) -> None:
        super().__init__(message)
        self.definite = definite


@dataclass(frozen=True)
class ReviewReplyPayload:
    job_id: int
    lock_token: UUID
    review_id: int
    business_id: int
    feedback_id: int
    response_text: str
    token: str


def send_yandex_review_reply(payload: ReviewReplyPayload) -> dict[str, Any]:
    body = json.dumps(
        {"feedbackId": payload.feedback_id, "comment": {"text": payload.response_text}},
        ensure_ascii=False,
    ).encode("utf-8")
    query = urllib.parse.urlencode({"sourceType": "SELLER"})
    request = urllib.request.Request(
        f"{YANDEX_MARKET_BASE_URL}/v2/businesses/{payload.business_id}/goods-feedback/comments/update?{query}",
        data=body,
        method="POST",
        headers={"Api-Key": payload.token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=review_reply_timeout_seconds(), context=_ssl_context(),
        ) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        # Ответ 4xx однозначно означает, что комментарий не принят. После 5xx или
        # сетевого сбоя результат неизвестен и автоматический повтор запрещён.
        raise YandexReviewReplyError(
            f"Яндекс Маркет отклонил ответ: HTTP {exc.code}; {detail[:300]}",
            definite=400 <= int(exc.code) < 500,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise YandexReviewReplyError(
            "Результат публикации ответа в Яндекс Маркете неизвестен",
            definite=False,
        ) from exc
    if not isinstance(value, dict) or str(value.get("status") or "OK") != "OK":
        raise YandexReviewReplyError("Яндекс Маркет не подтвердил публикацию ответа", definite=True)
    result = value.get("result") if isinstance(value.get("result"), dict) else {}
    return result


class YandexReviewReplyProcessor:
    def __init__(
        self,
        *,
        database_url: Callable[[], str],
        psycopg,
        sender: Callable[[ReviewReplyPayload], dict[str, Any]] = send_yandex_review_reply,
    ) -> None:
        self._database_url = database_url
        self._psycopg = psycopg
        self._sender = sender

    def recover_stale(self) -> tuple[int, int]:
        """До сети задание можно вернуть, после sending — только пометить unknown."""

        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.marketplace_review_reply_jobs
                    SET state='queued', lock_token=NULL, locked_until=NULL,
                        last_error='Worker перезапущен до публикации ответа', updated_at=now()
                    WHERE state='preparing' AND locked_until < now()
                    """
                )
                requeued = int(cursor.rowcount)
                cursor.execute(
                    """
                    UPDATE seller.marketplace_review_reply_jobs
                    SET state='unknown', unknown_at=now(), lock_token=NULL, locked_until=NULL,
                        last_error='Результат публикации неизвестен; автоматический повтор запрещён',
                        updated_at=now()
                    WHERE state='sending' AND locked_until < now()
                    """
                )
                unknown = int(cursor.rowcount)
            return requeued, unknown

    def process_pending_jobs(self, limit: int = 5) -> int:
        if not review_reply_enabled():
            return 0
        processed = 0
        for _index in range(max(1, min(int(limit), 50))):
            payload = self._claim_and_prepare()
            if payload is None:
                break
            processed += 1
            try:
                result = self._sender(payload)
            except YandexReviewReplyError as exc:
                self._finish(payload, state="failed" if exc.definite else "unknown", error=str(exc))
            except Exception:
                self._finish(
                    payload,
                    state="unknown",
                    error="Результат публикации ответа в Яндекс Маркете неизвестен",
                )
            else:
                self._finish(payload, state="submitted", result=result)
        return processed

    def _claim_and_prepare(self) -> ReviewReplyPayload | None:
        with self._psycopg.connect(self._database_url()) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT job.id
                        FROM seller.marketplace_review_reply_jobs AS job
                        JOIN seller.marketplace_reviews AS review
                          ON review.id=job.review_id
                         AND review.workspace_id=job.workspace_id
                         AND review.connection_id=job.connection_id
                        JOIN seller.marketplace_connections AS market ON market.id=job.connection_id
                        WHERE job.state='queued' AND market.status='active'
                          AND market.provider_code='yandex_market' AND market.review_reply_enabled=true
                          AND NOT EXISTS (
                            SELECT 1 FROM seller.marketplace_review_reply_jobs AS inflight
                            WHERE inflight.connection_id=job.connection_id
                              AND inflight.state IN ('preparing', 'sending')
                          )
                        ORDER BY job.queued_at, job.id
                        FOR UPDATE OF job SKIP LOCKED
                        LIMIT 1
                        """
                    )
                    row = cursor.fetchone()
                    if not row:
                        return None
                    job_id = int(row[0])
                    cursor.execute(
                        """
                        UPDATE seller.marketplace_review_reply_jobs
                        SET state='preparing', attempt_count=attempt_count + 1,
                            lock_token=gen_random_uuid(),
                            locked_until=now() + (%s * interval '1 second'), updated_at=now()
                        WHERE id=%s AND state='queued'
                        RETURNING lock_token
                        """,
                        (REVIEW_REPLY_LOCK_SECONDS, job_id),
                    )
                    claimed = cursor.fetchone()
                    if not claimed:
                        return None
                    lock_token = claimed[0]
            except self._psycopg.errors.UniqueViolation:
                connection.rollback()
                return None

            try:
                credential_key = credentials_secret()
            except RuntimeError as exc:
                self._fail_before_send(connection, job_id, lock_token, str(exc))
                return None

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT review.id, review.business_id, review.feedback_id, review.need_reaction,
                           job.response_text, market.status, market.provider_code,
                           market.review_reply_enabled,
                           pgp_sym_decrypt(market.token_ciphertext, %s)
                    FROM seller.marketplace_review_reply_jobs AS job
                    JOIN seller.marketplace_reviews AS review
                      ON review.id=job.review_id
                     AND review.workspace_id=job.workspace_id
                     AND review.connection_id=job.connection_id
                    JOIN seller.marketplace_connections AS market ON market.id=job.connection_id
                    WHERE job.id=%s AND job.state='preparing' AND job.lock_token=%s
                    FOR UPDATE OF job, review
                    """,
                    (credential_key, job_id, lock_token),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                validation_error = ""
                if not review_reply_enabled() or str(row[5]) != "active" or not bool(row[7]):
                    validation_error = "Публикация ответов для магазина выключена"
                elif str(row[6]) != "yandex_market":
                    validation_error = "Ответы поддерживаются только для Яндекс Маркета"
                elif not bool(row[3]):
                    validation_error = "Отзыв уже обработан"
                elif not str(row[1] or "").isdigit():
                    validation_error = "У отзыва не определён кабинет Яндекс Маркета"
                if validation_error:
                    self._fail_before_send(connection, job_id, lock_token, validation_error)
                    return None
                cursor.execute(
                    """
                    UPDATE seller.marketplace_review_reply_jobs
                    SET state='sending', sending_at=now(), updated_at=now()
                    WHERE id=%s AND state='preparing' AND lock_token=%s
                    """,
                    (job_id, lock_token),
                )
            return ReviewReplyPayload(
                job_id=job_id,
                lock_token=lock_token,
                review_id=int(row[0]),
                business_id=int(str(row[1])),
                feedback_id=int(row[2]),
                response_text=str(row[4]),
                token=str(row[8]),
            )

    @staticmethod
    def _fail_before_send(connection, job_id: int, lock_token: UUID, error: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE seller.marketplace_review_reply_jobs
                SET state='failed', failed_at=now(), last_error=%s,
                    lock_token=NULL, locked_until=NULL, updated_at=now()
                WHERE id=%s AND state='preparing' AND lock_token=%s
                """,
                (str(error)[:1000], job_id, lock_token),
            )

    def _finish(
        self,
        payload: ReviewReplyPayload,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        result = result or {}
        provider_comment_id = result.get("id")
        provider_status = str(result.get("status") or "")
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.marketplace_review_reply_jobs
                    SET state=%s,
                        provider_comment_id=%s,
                        provider_status=%s,
                        submitted_at=CASE WHEN %s='submitted' THEN now() ELSE submitted_at END,
                        unknown_at=CASE WHEN %s='unknown' THEN now() ELSE unknown_at END,
                        failed_at=CASE WHEN %s='failed' THEN now() ELSE failed_at END,
                        last_error=%s, lock_token=NULL, locked_until=NULL, updated_at=now()
                    WHERE id=%s AND state='sending' AND lock_token=%s
                    """,
                    (
                        state,
                        int(provider_comment_id) if str(provider_comment_id or "").isdigit() else None,
                        provider_status,
                        state,
                        state,
                        state,
                        str(error)[:1000],
                        payload.job_id,
                        payload.lock_token,
                    ),
                )
                if state == "submitted" and cursor.rowcount == 1:
                    cursor.execute(
                        """
                        UPDATE seller.marketplace_reviews
                        SET need_reaction=false, updated_at=now()
                        WHERE id=%s
                        """,
                        (payload.review_id,),
                    )


def build_yandex_review_reply_processor(*, database_url, psycopg, sender=send_yandex_review_reply):
    return YandexReviewReplyProcessor(database_url=database_url, psycopg=psycopg, sender=sender)
