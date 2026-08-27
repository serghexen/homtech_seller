"""Долговечные Telegram-уведомления о выдачах, требующих оператора."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import signal
import time
from typing import Any
import urllib.error
import urllib.request
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row


LOGGER = logging.getLogger("seller_notifier")
NOTIFIER_CODE = "seller_fulfillment_alerts"
STOP_REQUESTED = False


class TelegramTemporaryError(RuntimeError):
    """Сетевая или временная серверная ошибка Telegram."""


class TelegramPermanentError(RuntimeError):
    """Однозначный отказ Telegram, который не исправится повтором."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    enabled: bool
    bot_token: str
    api_base: str
    poll_interval_seconds: float
    lease_seconds: int
    batch_size: int
    request_attempts: int


@dataclass(frozen=True)
class ClaimedDelivery:
    id: int
    event_id: int
    recipient_id: int
    chat_id: int
    event_type: str
    payload: dict[str, Any]
    attempt_count: int
    max_attempts: int
    lock_token: UUID


def env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, min(int(raw), maximum))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def load_settings() -> Settings:
    database_url = str(os.getenv("DATABASE_URL", "") or "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    enabled = env_bool("SELLER_TELEGRAM_NOTIFICATIONS_ENABLED")
    bot_token = str(os.getenv("SELLER_TELEGRAM_BOT_TOKEN", "") or "").strip()
    if enabled and not bot_token:
        raise RuntimeError("SELLER_TELEGRAM_BOT_TOKEN is required when notifications are enabled")
    return Settings(
        database_url=database_url,
        enabled=enabled,
        bot_token=bot_token,
        api_base=str(os.getenv("SELLER_TELEGRAM_API_BASE", "https://api.telegram.org") or "").strip().rstrip("/"),
        poll_interval_seconds=float(env_int("SELLER_TELEGRAM_POLL_SECONDS", 10, 5, 300)),
        lease_seconds=env_int("SELLER_TELEGRAM_LEASE_SECONDS", 90, 30, 600),
        batch_size=env_int("SELLER_TELEGRAM_BATCH_SIZE", 20, 1, 100),
        request_attempts=env_int("SELLER_TELEGRAM_REQUEST_ATTEMPTS", 3, 1, 5),
    )


def retry_delay_seconds(attempt_count: int) -> int:
    # Межцикловый backoff переживает рестарты и длительные сетевые проблемы.
    return min(15 * (2 ** max(0, min(int(attempt_count), 8) - 1)), 1800)


def provider_name(provider_code: str) -> str:
    return "Ozon" if str(provider_code or "") == "ozon" else "Яндекс Маркет"


def status_title(status: str) -> str:
    labels = {
        "manual_required": "Ожидает оператора",
        "reserved": "Комплект подготовлен",
        "sending": "Отправляется",
        "submitted": "Передан маркетплейсу",
        "unknown": "Требует сверки",
        "delivered": "Доставлен",
        "cancelled": "Отменён",
        "failed": "Ошибка",
    }
    return labels.get(str(status or "").strip().lower(), "Обрабатывается")


def notification_text(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "resolved":
        heading = "✅ Проблема решена"
    elif event_type == "cancelled":
        heading = "⚠️ Заказ отменён"
    elif event_type == "unknown":
        heading = "⚠️ Проверьте отправку"
    elif event_type == "error":
        heading = "⚠️ Ошибка выдачи"
    else:
        heading = "⚠️ Требуется оператор"

    lines = [
        heading,
        f"Магазин: {str(payload.get('store_name') or '—')[:160]}",
        f"Площадка: {provider_name(str(payload.get('provider_code') or ''))}",
        f"Заказ: {str(payload.get('external_order_id') or '—')[:160]}",
        f"Товар: {str(payload.get('title') or payload.get('offer_id') or 'Товар не указан')[:1000]}",
        f"Количество: {max(1, int(payload.get('quantity') or 1))}",
        f"Статус: {status_title(str(payload.get('status') or ''))}",
    ]
    error = str(payload.get("last_error") or "").strip()
    if event_type in {"unknown", "error"} and error:
        lines.append(f"Причина: {error[:1200]}")
    elif event_type == "manual_required":
        lines.append("Действие: откройте заказ в Seller и подготовьте выдачу.")
    return "\n".join(lines)


def _telegram_error_detail(body: str) -> tuple[str, int]:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return body[:500], 0
    description = str(data.get("description") or body)[:500] if isinstance(data, dict) else body[:500]
    parameters = data.get("parameters") if isinstance(data, dict) and isinstance(data.get("parameters"), dict) else {}
    return description, int(parameters.get("retry_after") or 0)


def telegram_request(settings: Settings, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{settings.api_base}/bot{settings.bot_token}/{method}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    last_error: Exception | None = None
    for attempt in range(1, settings.request_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, dict) or not data.get("ok"):
                description, retry_after = _telegram_error_detail(json.dumps(data, ensure_ascii=False))
                error_code = int(data.get("error_code") or 0) if isinstance(data, dict) else 0
                if error_code == 429 or error_code >= 500:
                    raise TelegramTemporaryError(description or "Telegram temporary error")
                raise TelegramPermanentError(description or "Telegram rejected request")
            return data
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            description, retry_after = _telegram_error_detail(body)
            if exc.code != 429 and exc.code < 500:
                raise TelegramPermanentError(f"Telegram HTTP {exc.code}: {description}") from exc
            last_error = TelegramTemporaryError(f"Telegram HTTP {exc.code}: {description}")
            delay = max(1, min(retry_after or attempt, 30))
        except TelegramPermanentError:
            raise
        except (TelegramTemporaryError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = TelegramTemporaryError(str(exc) or exc.__class__.__name__)
            delay = min(attempt, 5)
        if attempt < settings.request_attempts:
            LOGGER.warning("Telegram %s failed on attempt %s/%s", method, attempt, settings.request_attempts)
            time.sleep(delay)
    raise last_error or TelegramTemporaryError("Telegram request failed")


def send_text(settings: Settings, chat_id: int, text: str) -> int:
    data = telegram_request(settings, "sendMessage", {"chat_id": chat_id, "text": text})
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    message_id = int(result.get("message_id") or 0)
    if message_id <= 0:
        raise TelegramTemporaryError("Telegram did not return message_id")
    return message_id


def verify_bot(settings: Settings) -> str:
    data = telegram_request(settings, "getMe", {})
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    username = str(result.get("username") or "").strip()
    if not username:
        raise TelegramTemporaryError("Telegram getMe did not return bot username")
    return username


def recover_stale_deliveries(connection) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE seller.telegram_notification_deliveries
            SET state='retry', available_at=now(), locked_by=NULL, locked_until=NULL,
                last_error='Notifier был перезапущен до подтверждения Telegram', updated_at=now()
            WHERE state='sending' AND locked_until < now()
            """
        )
        recovered = cursor.rowcount
    connection.commit()
    return recovered


def materialize_deliveries(connection, limit: int = 500) -> int:
    # Создаёт отдельную доставку на каждый workspace-чат; пересечения пользователей невозможны.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH pending_events AS (
              SELECT event.id, event.workspace_id
              FROM seller.telegram_notification_events AS event
              WHERE EXISTS (
                SELECT 1 FROM seller.telegram_notification_recipients AS recipient
                WHERE recipient.workspace_id=event.workspace_id AND recipient.is_active=true
                  AND event.id > recipient.notifications_from_event_id
              )
              ORDER BY event.id
              LIMIT %s
            )
            INSERT INTO seller.telegram_notification_deliveries(event_id, recipient_id)
            SELECT event.id, recipient.id
            FROM pending_events AS event
            JOIN seller.telegram_notification_recipients AS recipient
              ON recipient.workspace_id=event.workspace_id
             AND recipient.is_active=true
             AND event.id > recipient.notifications_from_event_id
            ON CONFLICT (event_id, recipient_id) DO NOTHING
            """,
            (max(1, min(int(limit), 5000)),),
        )
        created = cursor.rowcount
    connection.commit()
    return created


def claim_delivery(connection, lease_seconds: int) -> ClaimedDelivery | None:
    lock_token = uuid4()
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            WITH candidate AS (
              SELECT delivery.id
              FROM seller.telegram_notification_deliveries AS delivery
              JOIN seller.telegram_notification_recipients AS recipient ON recipient.id=delivery.recipient_id
              JOIN seller.telegram_notification_events AS event ON event.id=delivery.event_id
              WHERE delivery.state IN ('queued','retry') AND delivery.available_at <= now()
                AND recipient.is_active=true AND recipient.workspace_id=event.workspace_id
              ORDER BY delivery.available_at, delivery.id
              FOR UPDATE OF delivery SKIP LOCKED
              LIMIT 1
            ), claimed AS (
              UPDATE seller.telegram_notification_deliveries AS delivery
              SET state='sending', attempt_count=attempt_count + 1, locked_by=%s,
                  locked_until=now() + (%s * interval '1 second'), last_error='', updated_at=now()
              FROM candidate
              WHERE delivery.id=candidate.id
              RETURNING delivery.id, delivery.event_id, delivery.recipient_id,
                        delivery.attempt_count, delivery.max_attempts
            )
            SELECT claimed.id, claimed.event_id, claimed.recipient_id, recipient.chat_id,
                   event.event_type, event.payload, claimed.attempt_count, claimed.max_attempts
            FROM claimed
            JOIN seller.telegram_notification_events AS event ON event.id=claimed.event_id
            JOIN seller.telegram_notification_recipients AS recipient ON recipient.id=claimed.recipient_id
            """,
            (lock_token, int(lease_seconds)),
        )
        row = cursor.fetchone()
    connection.commit()
    if not row:
        return None
    return ClaimedDelivery(
        id=int(row["id"]), event_id=int(row["event_id"]), recipient_id=int(row["recipient_id"]),
        chat_id=int(row["chat_id"]), event_type=str(row["event_type"]), payload=dict(row["payload"] or {}),
        attempt_count=int(row["attempt_count"]), max_attempts=int(row["max_attempts"]), lock_token=lock_token,
    )


def complete_delivery(connection, delivery: ClaimedDelivery, message_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE seller.telegram_notification_deliveries
            SET state='sent', telegram_message_id=%s, sent_at=now(), locked_by=NULL,
                locked_until=NULL, last_error='', updated_at=now()
            WHERE id=%s AND state='sending' AND locked_by=%s
            """,
            (message_id, delivery.id, delivery.lock_token),
        )
    connection.commit()


def fail_delivery(connection, delivery: ClaimedDelivery, error: Exception) -> str:
    permanent = isinstance(error, TelegramPermanentError)
    dead = permanent or delivery.attempt_count >= delivery.max_attempts
    target_state = "dead" if dead else "retry"
    delay = retry_delay_seconds(delivery.attempt_count)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE seller.telegram_notification_deliveries
            SET state=%s, available_at=CASE WHEN %s THEN available_at
                                           ELSE now() + (%s * interval '1 second') END,
                locked_by=NULL, locked_until=NULL, last_error=%s, updated_at=now()
            WHERE id=%s AND state='sending' AND locked_by=%s
            """,
            (target_state, dead, delay, str(error)[:2000], delivery.id, delivery.lock_token),
        )
    connection.commit()
    return target_state


def command_kind(text: Any) -> str:
    command = str(text or "").strip().split(maxsplit=1)[0].lower().split("@", 1)[0]
    if command in {"/start", "/subscribe"}:
        return "subscribe"
    if command in {"/stop", "/unsubscribe"}:
        return "unsubscribe"
    return ""


def read_update_offset(connection) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT telegram_update_offset FROM seller.telegram_bot_state WHERE notifier_code=%s",
            (NOTIFIER_CODE,),
        )
        row = cursor.fetchone()
    return int(row[0] or 0) if row else 0


def save_update_offset(connection, offset: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO seller.telegram_bot_state(notifier_code, telegram_update_offset, updated_at)
            VALUES (%s,%s,now())
            ON CONFLICT (notifier_code) DO UPDATE
            SET telegram_update_offset=GREATEST(seller.telegram_bot_state.telegram_update_offset,
                                                excluded.telegram_update_offset),
                updated_at=now()
            """,
            (NOTIFIER_CODE, max(0, int(offset))),
        )
    connection.commit()


def unsubscribe_chat(connection, chat_id: int) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE seller.telegram_notification_recipients
            SET is_active=false, updated_at=now()
            WHERE chat_id=%s AND is_active=true
            RETURNING id
            """,
            (chat_id,),
        )
        recipient_ids = [int(row[0]) for row in cursor.fetchall()]
        if recipient_ids:
            cursor.execute(
                """
                UPDATE seller.telegram_notification_deliveries
                SET state='dead', last_error='Получатель отключил уведомления', updated_at=now()
                WHERE recipient_id=ANY(%s) AND state IN ('queued','retry')
                """,
                (recipient_ids,),
            )
    connection.commit()
    return len(recipient_ids)


def resubscribe_known_chat(connection, chat_id: int) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(id),0) FROM seller.telegram_notification_events")
        watermark = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            """
            UPDATE seller.telegram_notification_recipients
            SET notifications_from_event_id=CASE WHEN is_active THEN notifications_from_event_id ELSE %s END,
                is_active=true, updated_at=now()
            WHERE chat_id=%s
            RETURNING id
            """,
            (watermark, chat_id),
        )
        rows = cursor.fetchall()
    connection.commit()
    return len(rows)


def sync_commands(settings: Settings) -> int:
    with psycopg.connect(settings.database_url) as connection:
        offset = read_update_offset(connection)
    data = telegram_request(
        settings, "getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]},
    )
    updates = data.get("result") if isinstance(data.get("result"), list) else []
    processed = 0
    for update in updates:
        if not isinstance(update, dict):
            continue
        update_id = int(update.get("update_id") or 0)
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = int(chat.get("id") or 0)
        action = command_kind(message.get("text"))
        if chat_id and action == "unsubscribe":
            with psycopg.connect(settings.database_url) as connection:
                changed = unsubscribe_chat(connection, chat_id)
            send_text(settings, chat_id, "Уведомления Seller отключены." if changed else "Уведомления уже отключены.")
        elif chat_id and action == "subscribe":
            with psycopg.connect(settings.database_url) as connection:
                changed = resubscribe_known_chat(connection, chat_id)
            reply = (
                "Уведомления Seller включены. Для отключения отправьте /stop."
                if changed else
                "Этот чат ещё не привязан к Seller. Обратитесь к владельцу рабочей области."
            )
            send_text(settings, chat_id, reply)
        if update_id:
            with psycopg.connect(settings.database_url) as connection:
                save_update_offset(connection, update_id + 1)
            processed += 1
    return processed


def run_cycle(settings: Settings) -> int:
    with psycopg.connect(settings.database_url) as connection:
        recovered = recover_stale_deliveries(connection)
        created = materialize_deliveries(connection)
    if recovered:
        LOGGER.warning("Recovered %s stale Telegram deliveries", recovered)
    if created:
        LOGGER.info("Created %s Telegram deliveries", created)
    try:
        sync_commands(settings)
    except Exception:
        # Сбой getUpdates не должен задерживать уже сохранённые уведомления заказов.
        LOGGER.exception("Cannot sync Telegram commands; delivery queue will continue")

    processed = 0
    for _ in range(settings.batch_size):
        with psycopg.connect(settings.database_url) as connection:
            delivery = claim_delivery(connection, settings.lease_seconds)
        if not delivery:
            break
        try:
            message_id = send_text(settings, delivery.chat_id, notification_text(delivery.event_type, delivery.payload))
            with psycopg.connect(settings.database_url) as connection:
                complete_delivery(connection, delivery, message_id)
            LOGGER.info("Telegram event %s was delivered to recipient %s", delivery.event_id, delivery.recipient_id)
        except Exception as exc:
            with psycopg.connect(settings.database_url) as connection:
                state = fail_delivery(connection, delivery, exc)
            LOGGER.exception("Telegram event %s delivery failed with state %s", delivery.event_id, state)
        processed += 1
    return processed


def request_stop(_signal: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if not settings.enabled:
        LOGGER.warning("Seller Telegram notifier is disabled")
    else:
        try:
            LOGGER.info("Seller Telegram notifier started as @%s", verify_bot(settings))
        except Exception:
            # Временная недоступность Telegram не останавливает процесс:
            # следующий цикл продолжит durable retry сохранённых уведомлений.
            LOGGER.exception("Cannot verify Telegram bot on startup; notifier will continue")
    while not STOP_REQUESTED:
        if settings.enabled:
            try:
                run_cycle(settings)
            except Exception:
                LOGGER.exception("Seller Telegram notifier cycle failed")
        deadline = time.monotonic() + settings.poll_interval_seconds
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
