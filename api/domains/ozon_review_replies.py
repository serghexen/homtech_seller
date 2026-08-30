"""Долговечная ручная отправка ответов на отзывы Ozon без слепых повторов."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable
from uuid import UUID

from domains.marketplace_connection_verification import OZON_SELLER_BASE_URL, _ssl_context
from domains.marketplace_sync_service import credentials_secret


REVIEW_REPLY_LOCK_SECONDS = 120


def ozon_review_reply_enabled() -> bool:
    return str(os.getenv("SELLER_OZON_REVIEW_REPLY_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes",
    }


def ozon_review_reply_timeout_seconds() -> int:
    return max(3, min(int(os.getenv("OZON_REVIEW_REPLY_TIMEOUT_SECONDS", "20")), 60))


class OzonReviewReplyError(RuntimeError):
    def __init__(self, message: str, *, definite: bool) -> None:
        super().__init__(message)
        self.definite = definite


@dataclass(frozen=True)
class OzonReviewReplyPayload:
    job_id: int
    lock_token: UUID
    review_id: int
    external_review_id: str
    response_text: str
    client_id: str
    token: str


def send_ozon_review_reply(payload: OzonReviewReplyPayload) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{OZON_SELLER_BASE_URL}/v1/review/comment/create",
        data=json.dumps(
            {
                "review_id": payload.external_review_id,
                "text": payload.response_text,
                "mark_review_as_processed": True,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        method="POST",
        headers={
            "Client-Id": payload.client_id,
            "Api-Key": payload.token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=ozon_review_reply_timeout_seconds(), context=_ssl_context(),
        ) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise OzonReviewReplyError(
            f"Ozon отклонил ответ: HTTP {exc.code}; {detail[:300]}",
            definite=400 <= int(exc.code) < 500,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise OzonReviewReplyError(
            "Результат публикации ответа в Ozon неизвестен",
            definite=False,
        ) from exc
    if not isinstance(value, dict) or not str(value.get("comment_id") or "").strip():
        # HTTP 2xx уже мог применить действие, даже если подтверждение неожиданного формата.
        # Такое задание нельзя разрешать повторить автоматически или из интерфейса.
        raise OzonReviewReplyError("Ozon не подтвердил публикацию ответа", definite=False)
    return value


class OzonReviewReplyProcessor:
    def __init__(
        self,
        *,
        database_url: Callable[[], str],
        psycopg,
        sender: Callable[[OzonReviewReplyPayload], dict[str, Any]] = send_ozon_review_reply,
    ) -> None:
        self._database_url = database_url
        self._psycopg = psycopg
        self._sender = sender

    def recover_stale(self) -> tuple[int, int]:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.marketplace_review_reply_jobs AS job
                    SET state='queued', lock_token=NULL, locked_until=NULL,
                        last_error='Worker перезапущен до публикации ответа', updated_at=now()
                    FROM seller.marketplace_reviews AS review
                    WHERE review.id=job.review_id AND review.provider_code='ozon'
                      AND job.state='preparing' AND job.locked_until < now()
                    """
                )
                requeued = int(cursor.rowcount)
                cursor.execute(
                    """
                    UPDATE seller.marketplace_review_reply_jobs AS job
                    SET state='unknown', unknown_at=now(), lock_token=NULL, locked_until=NULL,
                        last_error='Результат публикации неизвестен; автоматический повтор запрещён',
                        updated_at=now()
                    FROM seller.marketplace_reviews AS review
                    WHERE review.id=job.review_id AND review.provider_code='ozon'
                      AND job.state='sending' AND job.locked_until < now()
                    """
                )
                unknown = int(cursor.rowcount)
            return requeued, unknown

    def process_pending_jobs(self, limit: int = 5) -> int:
        if not ozon_review_reply_enabled():
            return 0
        processed = 0
        for _index in range(max(1, min(int(limit), 50))):
            payload = self._claim_and_prepare()
            if payload is None:
                break
            processed += 1
            try:
                result = self._sender(payload)
            except OzonReviewReplyError as exc:
                self._finish(payload, state="failed" if exc.definite else "unknown", error=str(exc))
            except Exception:
                self._finish(payload, state="unknown", error="Результат публикации ответа в Ozon неизвестен")
            else:
                self._finish(payload, state="submitted", result=result)
        return processed

    def _claim_and_prepare(self) -> OzonReviewReplyPayload | None:
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
                          AND review.provider_code='ozon' AND market.provider_code='ozon'
                          AND market.review_reply_enabled=true
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
                    SELECT review.id, review.external_review_id, review.need_reaction,
                           review.reply_allowed, job.response_text, market.status,
                           market.provider_code, market.review_reply_enabled, market.client_id,
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
                if not ozon_review_reply_enabled() or str(row[5]) != "active" or not bool(row[7]):
                    validation_error = "Публикация ответов для магазина выключена"
                elif str(row[6]) != "ozon":
                    validation_error = "Задание не принадлежит Ozon"
                elif not bool(row[2]):
                    validation_error = "Отзыв уже обработан"
                elif not bool(row[3]):
                    validation_error = "Ozon не принимает ответы на отзывы без текста, фото или видео"
                elif not str(row[1] or "").strip() or not str(row[8] or "").strip():
                    validation_error = "У отзыва или магазина отсутствует внешний идентификатор"
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
            return OzonReviewReplyPayload(
                job_id=job_id,
                lock_token=lock_token,
                review_id=int(row[0]),
                external_review_id=str(row[1]),
                response_text=str(row[4]),
                client_id=str(row[8]),
                token=str(row[9]),
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
        payload: OzonReviewReplyPayload,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        result = result or {}
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.marketplace_review_reply_jobs
                    SET state=%s, provider_comment_id=%s, provider_status=%s,
                        submitted_at=CASE WHEN %s='submitted' THEN now() ELSE submitted_at END,
                        unknown_at=CASE WHEN %s='unknown' THEN now() ELSE unknown_at END,
                        failed_at=CASE WHEN %s='failed' THEN now() ELSE failed_at END,
                        last_error=%s, lock_token=NULL, locked_until=NULL, updated_at=now()
                    WHERE id=%s AND state='sending' AND lock_token=%s
                    """,
                    (
                        state,
                        str(result.get("comment_id") or "") or None,
                        "PROCESSED" if state == "submitted" else "",
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


def build_ozon_review_reply_processor(*, database_url, psycopg, sender=send_ozon_review_reply):
    return OzonReviewReplyProcessor(database_url=database_url, psycopg=psycopg, sender=sender)
