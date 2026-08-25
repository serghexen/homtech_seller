"""Безопасный приём API-уведомлений Яндекс Маркета в долговечный inbox Seller."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
import json
import os
from typing import Any, Callable, Iterable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


IpNetwork = IPv4Network | IPv6Network

# Официальные сети Маркета фиксируются рядом с кодом, чтобы изменение источников проходило review.
YANDEX_MARKET_NOTIFICATION_NETWORKS: tuple[IpNetwork, ...] = (
    ip_network("5.45.207.0/25"),
    ip_network("141.8.142.0/25"),
    ip_network("5.255.253.0/25"),
)

# API доступен только через loopback и приватную Docker-сеть, поэтому эти прокси можно считать доверенными.
DEFAULT_TRUSTED_PROXY_NETWORKS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


def first_text(*values: Any) -> str:
    # Берёт первое непустое поле из разных вариантов уведомлений Яндекса.
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def parse_event_time(value: Any) -> datetime | None:
    # Нормализует корректное ISO-время, а неизвестный формат оставляет только в сыром payload.
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def notification_fingerprint(payload: dict[str, Any]) -> str:
    # Стабильный хеш превращает повторную доставку того же события в один inbox-элемент.
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


def parse_networks(value: str) -> tuple[IpNetwork, ...]:
    # Проверяет конфигурацию прокси заранее и не позволяет молча ослабить проверку источника.
    networks: list[IpNetwork] = []
    for item in str(value or "").split(","):
        normalized = item.strip()
        if normalized:
            networks.append(ip_network(normalized, strict=False))
    return tuple(networks)


def trusted_proxy_networks() -> tuple[IpNetwork, ...]:
    # Доверенные сети задают только инфраструктурный путь до API, но не список отправителей Яндекса.
    configured = os.getenv("YANDEX_MARKET_TRUSTED_PROXY_NETWORKS", DEFAULT_TRUSTED_PROXY_NETWORKS)
    try:
        return parse_networks(configured)
    except ValueError as exc:
        raise RuntimeError("YANDEX_MARKET_TRUSTED_PROXY_NETWORKS contains invalid network") from exc


def address_in_networks(value: str, networks: Iterable[IpNetwork]) -> bool:
    # Сравнивает адрес только с сетью той же версии IP.
    try:
        address = ip_address(str(value or "").strip())
    except ValueError:
        return False
    return any(address.version == network.version and address in network for network in networks)


def normalized_ip(value: str) -> str:
    # Убирает неоднозначное написание IPv6 и отбрасывает не-IP элементы заголовка прокси.
    try:
        return str(ip_address(str(value or "").strip()))
    except ValueError:
        return ""


def source_ip_from_proxy_chain(
    peer_ip: str,
    forwarded_for: str = "",
    real_ip: str = "",
    *,
    trusted_networks: Iterable[IpNetwork],
) -> str:
    # Идёт по цепочке справа налево и не доверяет подставленному X-Forwarded-For от внешнего клиента.
    normalized_peer = normalized_ip(peer_ip)
    if not normalized_peer:
        return ""
    if not address_in_networks(normalized_peer, trusted_networks):
        return normalized_peer

    forwarded_candidates = [part.strip() for part in str(forwarded_for or "").split(",") if part.strip()]
    if not forwarded_candidates and str(real_ip or "").strip():
        forwarded_candidates = [str(real_ip).strip()]
    chain = [*forwarded_candidates, normalized_peer]
    valid_chain = [normalized for candidate in chain if (normalized := normalized_ip(candidate))]
    if not valid_chain:
        return normalized_peer
    for candidate in reversed(valid_chain):
        if not address_in_networks(candidate, trusted_networks):
            return candidate
    return valid_chain[0]


def yandex_market_source_ip(request: Request) -> str:
    # Восстанавливает реальный адрес через проверенную цепочку reverse proxy Seller.
    peer_ip = request.client.host if request.client else ""
    return source_ip_from_proxy_chain(
        peer_ip,
        request.headers.get("x-forwarded-for", ""),
        request.headers.get("x-real-ip", ""),
        trusted_networks=trusted_proxy_networks(),
    )


def is_yandex_market_source(source_ip: str) -> bool:
    # Принимает уведомления только из опубликованных Яндексом подсетей.
    return address_in_networks(source_ip, YANDEX_MARKET_NOTIFICATION_NETWORKS)


def webhook_processing_enabled() -> bool:
    # До контролируемого переключения с CRM события только сохраняются со статусом paused.
    return str(os.getenv("YANDEX_MARKET_WEBHOOK_PROCESSING_ENABLED", "false")).strip().lower() in {"1", "true", "yes"}


def webhook_max_body_bytes() -> int:
    # Ограничивает память на публичном endpoint и оставляет запас для штатных payload Маркета.
    configured = int(os.getenv("YANDEX_MARKET_WEBHOOK_MAX_BODY_BYTES", "1048576"))
    return max(1024, min(configured, 10 * 1024 * 1024))


async def read_json_object(request: Request) -> dict[str, Any]:
    # Читает тело по частям, чтобы отклонить слишком большой запрос до полного накопления в памяти.
    body = bytearray()
    limit = webhook_max_body_bytes()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="Yandex Market notification is too large")
        body.extend(chunk)
    try:
        payload = json.loads(bytes(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Yandex Market notification must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Yandex Market notification must be JSON object")
    return payload


def integration_response(event_time: Any = None) -> dict[str, str]:
    # Для PING возвращает исходное время, как требует контракт проверки интеграции Яндекса.
    response_time = first_text(event_time) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "version": str(os.getenv("YANDEX_MARKET_WEBHOOK_INTEGRATION_VERSION", "1.0.0") or "1.0.0").strip(),
        "name": str(os.getenv("YANDEX_MARKET_WEBHOOK_INTEGRATION_NAME", "HomTech Seller") or "HomTech Seller").strip(),
        "time": response_time,
    }


def save_event(
    *,
    database_url: Callable[[], str],
    psycopg,
    payload: dict[str, Any],
    source_ip: str,
    processing_enabled: bool,
) -> int:
    # Сохраняет событие и связывает его с магазином, не запуская выдачу или внешний запрос.
    notification_type = first_text(payload.get("notificationType"))
    campaign_id = first_text(payload.get("campaignId"))
    order_id = first_text(payload.get("orderId"))
    event_time_raw = first_text(
        payload.get("updatedAt"),
        payload.get("createdAt"),
        payload.get("requestedAt"),
        payload.get("time"),
    )
    processing_state = "ignored" if notification_type.upper() == "PING" else ("received" if processing_enabled else "paused")

    with psycopg.connect(database_url()) as connection:
        connection_row = None
        if campaign_id:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, workspace_id, status, webhook_processing_enabled
                    FROM seller.marketplace_connections
                    WHERE provider_code='yandex_market' AND campaign_id=%s
                    ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'disabled' THEN 1 ELSE 2 END, id
                    LIMIT 1
                    """,
                    (campaign_id,),
                )
                connection_row = cursor.fetchone()

        connection_id = int(connection_row[0]) if connection_row else None
        workspace_id = int(connection_row[1]) if connection_row else None
        store_processing_enabled = bool(
            connection_row
            and str(connection_row[2]) == "active"
            and connection_row[3]
            and processing_enabled
        )
        if notification_type.upper() != "PING":
            processing_state = "received" if store_processing_enabled else "paused"
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO seller.yandex_webhook_events (
                  workspace_id, connection_id, event_fingerprint, notification_type,
                  campaign_id, order_id, provider_status, provider_substatus,
                  event_time, source_ip, payload_json, processing_enabled_at_receive, processing_state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (event_fingerprint) DO UPDATE
                SET duplicate_count=seller.yandex_webhook_events.duplicate_count + 1,
                    workspace_id=COALESCE(seller.yandex_webhook_events.workspace_id, EXCLUDED.workspace_id),
                    connection_id=COALESCE(seller.yandex_webhook_events.connection_id, EXCLUDED.connection_id),
                    processing_enabled_at_receive=(
                      seller.yandex_webhook_events.processing_enabled_at_receive
                      OR EXCLUDED.processing_enabled_at_receive
                    ),
                    processing_state=CASE
                      WHEN seller.yandex_webhook_events.processing_state='paused'
                       AND EXCLUDED.processing_enabled_at_receive
                      THEN 'received'
                      ELSE seller.yandex_webhook_events.processing_state
                    END,
                    next_attempt_at=CASE
                      WHEN seller.yandex_webhook_events.processing_state='paused'
                       AND EXCLUDED.processing_enabled_at_receive
                      THEN now()
                      ELSE seller.yandex_webhook_events.next_attempt_at
                    END,
                    last_received_at=now(), source_ip=EXCLUDED.source_ip, updated_at=now()
                RETURNING id
                """,
                (
                    workspace_id,
                    connection_id,
                    notification_fingerprint(payload),
                    notification_type,
                    campaign_id,
                    order_id,
                    first_text(payload.get("status")),
                    first_text(payload.get("substatus")),
                    parse_event_time(event_time_raw),
                    source_ip,
                    json.dumps(payload, ensure_ascii=False),
                    store_processing_enabled,
                    processing_state,
                ),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise RuntimeError("Yandex Market notification was not persisted")
    return int(row[0])


def mount_yandex_market_webhook_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    source_ip_resolver: Callable[[Request], str] = yandex_market_source_ip,
    processing_enabled: Callable[[], bool] = webhook_processing_enabled,
) -> None:
    """Подключает публичный endpoint без Seller-сессии, защищённый сетями Яндекс Маркета."""

    @app.post("/marketplaces/yandex/notifications/notification", include_in_schema=False)
    @app.post("/marketplaces/yandex/notifications", include_in_schema=False)
    async def receive_yandex_market_notification(request: Request):
        # Проверяет источник до чтения тела и не сохраняет запросы из неизвестных сетей.
        source_ip = source_ip_resolver(request)
        if not is_yandex_market_source(source_ip):
            raise HTTPException(status_code=403, detail="Yandex Market notification source is not allowed")

        payload = await read_json_object(request)
        notification_type = first_text(payload.get("notificationType"))
        if not notification_type:
            raise HTTPException(status_code=400, detail="Yandex Market notificationType is required")

        save_event(
            database_url=database_url,
            psycopg=psycopg,
            payload=payload,
            source_ip=source_ip,
            processing_enabled=processing_enabled(),
        )
        event_time = first_text(payload.get("time"), payload.get("updatedAt"), payload.get("createdAt"))
        return JSONResponse(status_code=200, content=integration_response(event_time))
