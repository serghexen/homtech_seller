"""Долговечная цепочка Supplier Hub -> пул -> поддержка -> оператор."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from typing import Any
from uuid import UUID, uuid4

from domains.buyer_text import normalize_buyer_text
from domains.fulfillment_service import prepare_support_message, reserve_pool_keys
from domains.supplier_hub_client import (
    SupplierHubClient,
    SupplierHubError,
    load_supplier_hub_settings,
)
from domains.workspace_entitlements import FULFILLMENT_SUPPLIER, workspace_allows
from domains.yandex_market_outbound import key_pool_secret


HUB_BLOCKING_STATES = {"created", "checked", "payment_started", "processing", "requires_attention"}
HUB_TERMINAL_STATES = {"succeeded", "failed"}


def automatic_fulfillment_resolver_enabled() -> bool:
    # Главный аварийный выключатель всей автоматической цепочки. Его включение
    # не отменяет отдельные флаги Supplier Hub, пула, магазина и outbound.
    return str(os.getenv("SELLER_FULFILLMENT_RESOLVER_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes",
    }


def automatic_pool_enabled() -> bool:
    return str(os.getenv("SELLER_POOL_RESERVATION_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes",
    }


def poll_delay_seconds() -> int:
    return max(2, min(int(os.getenv("SUPPLIER_HUB_POLL_SECONDS", "5")), 300))


def _decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result >= 0 else None


@dataclass(frozen=True)
class FulfillmentContext:
    fulfillment_id: int
    connection_id: int
    external_order_id: str
    external_item_id: str
    offer_id: str
    quantity: int
    status: str
    reservation_ref: str
    order_status: str
    delivery_type: str
    provider_code: str
    activation_instruction: str
    store_local_enabled: bool
    store_supplier_enabled: bool
    supplier_issue_enabled: bool
    pool_issue_enabled: bool
    support_issue_enabled: bool
    support_message: str
    mapping_id: int | None
    service_id: int | None
    nominal_id: str
    params: dict[str, Any]
    max_amount: Decimal | None
    workspace_id: int
    supplier_access_enabled: bool


class SupplierFulfillmentProcessor:
    def __init__(self, *, database_url, psycopg, client_factory=None) -> None:
        self._database_url = database_url
        self._psycopg = psycopg
        self._client_factory = client_factory or (lambda: SupplierHubClient(load_supplier_hub_settings()))

    def recover_stale(self) -> int:
        if not automatic_fulfillment_resolver_enabled():
            return 0
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.order_fulfillments
                    SET resolver_lock_token=NULL, resolver_locked_until=NULL,
                        next_resolve_at=LEAST(next_resolve_at, now()), updated_at=now()
                    WHERE resolver_lock_token IS NOT NULL AND resolver_locked_until < now()
                      AND status IN ('pending', 'manual_required', 'supplier_required')
                    """
                )
                return int(cursor.rowcount)

    def process_pending(self, limit: int = 5) -> int:
        if not automatic_fulfillment_resolver_enabled():
            return 0
        processed = 0
        for _ in range(max(1, min(int(limit), 50))):
            claimed = self._claim()
            if claimed is None:
                break
            fulfillment_id, lock_token = claimed
            processed += 1
            try:
                self._resolve(fulfillment_id)
            except Exception as exc:
                # В журнал не попадают request params и результаты покупки.
                self._schedule(
                    fulfillment_id,
                    lock_token,
                    reason=str(exc or exc.__class__.__name__)[:1000],
                    delay=poll_delay_seconds(),
                )
            else:
                self._schedule(fulfillment_id, lock_token, reason="", delay=poll_delay_seconds())
        return processed

    def _claim(self) -> tuple[int, UUID] | None:
        lock_token = uuid4()
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT fulfillment.id
                    FROM seller.order_fulfillments AS fulfillment
                    JOIN seller.marketplace_connections AS market
                      ON market.id=fulfillment.connection_id
                    WHERE fulfillment.status IN ('pending', 'manual_required', 'supplier_required')
                      AND fulfillment.next_resolve_at <= now()
                      AND (fulfillment.resolver_locked_until IS NULL OR fulfillment.resolver_locked_until < now())
                      AND market.status='active'
                    ORDER BY fulfillment.next_resolve_at, fulfillment.updated_at, fulfillment.id
                    FOR UPDATE OF fulfillment SKIP LOCKED
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if not row:
                    return None
                fulfillment_id = int(row[0])
                cursor.execute(
                    """
                    UPDATE seller.order_fulfillments
                    SET resolver_lock_token=%s, resolver_locked_until=now() + interval '2 minutes', updated_at=now()
                    WHERE id=%s
                    """,
                    (lock_token, fulfillment_id),
                )
        return fulfillment_id, lock_token

    def _schedule(self, fulfillment_id: int, lock_token: UUID, *, reason: str, delay: int) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.order_fulfillments
                    SET resolver_lock_token=NULL, resolver_locked_until=NULL,
                        next_resolve_at=now() + (%s * interval '1 second'),
                        last_error=CASE WHEN %s<>'' AND status IN ('pending','supplier_required') THEN %s ELSE last_error END,
                        updated_at=now()
                    WHERE id=%s AND resolver_lock_token=%s
                    """,
                    (int(delay), reason, reason, fulfillment_id, lock_token),
                )

    def _context(self, fulfillment_id: int) -> FulfillmentContext | None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT fulfillment.id, fulfillment.connection_id, fulfillment.external_order_id,
                           fulfillment.external_item_id, fulfillment.offer_id, fulfillment.requested_quantity,
                           fulfillment.status, fulfillment.reservation_ref,
                           order_item.normalized_status, order_item.delivery_type,
                           market.fulfillment_reservation_enabled, market.supplier_fulfillment_enabled,
                           COALESCE(policy.supplier_issue_enabled, false),
                           COALESCE(policy.pool_issue_enabled, local_settings.pool_issue_enabled, false),
                           COALESCE(
                             policy.support_message_delivery_enabled,
                             CASE WHEN COALESCE(local_settings.support_message_overridden, false)
                               THEN local_settings.support_message_delivery_enabled
                               ELSE imported_settings.support_message_delivery_enabled END,
                             false
                           ),
                           CASE WHEN COALESCE(local_settings.support_message_overridden, false)
                             THEN local_settings.support_message
                             ELSE COALESCE(imported_settings.support_message, '') END,
                           mapping.id, mapping.service_id, mapping.nominal_id, mapping.params, mapping.max_amount,
                           market.provider_code,
                           CASE WHEN local_settings.connection_id IS NOT NULL
                               THEN local_settings.activation_instruction
                               ELSE COALESCE(imported_settings.activation_instruction, '') END,
                           market.workspace_id
                    FROM seller.order_fulfillments AS fulfillment
                    JOIN seller.order_items AS order_item
                      ON order_item.connection_id=fulfillment.connection_id
                     AND order_item.external_order_id=fulfillment.external_order_id
                     AND order_item.external_item_id=fulfillment.external_item_id
                    JOIN seller.marketplace_connections AS market ON market.id=fulfillment.connection_id
                    LEFT JOIN seller.product_fulfillment_policies AS policy
                      ON policy.connection_id=fulfillment.connection_id
                     AND policy.external_product_id=fulfillment.offer_id
                    LEFT JOIN seller.product_card_settings AS local_settings
                      ON local_settings.connection_id=fulfillment.connection_id
                     AND local_settings.external_product_id=fulfillment.offer_id
                    LEFT JOIN seller.yandex_product_settings_snapshot AS imported_settings
                      ON imported_settings.connection_id=fulfillment.connection_id
                     AND imported_settings.external_product_id=fulfillment.offer_id
                    LEFT JOIN LATERAL (
                      SELECT supplier.id, supplier.service_id, supplier.nominal_id,
                             supplier.params, supplier.max_amount
                      FROM seller.product_supplier_mappings AS supplier
                      WHERE supplier.connection_id=fulfillment.connection_id
                        AND supplier.external_product_id=fulfillment.offer_id
                        AND supplier.enabled=true
                      ORDER BY supplier.priority, supplier.id
                      LIMIT 1
                    ) AS mapping ON true
                    WHERE fulfillment.id=%s
                    """,
                    (fulfillment_id,),
                )
                row = cursor.fetchone()
                supplier_access_enabled = bool(row) and workspace_allows(
                    cursor, int(row[23]), FULFILLMENT_SUPPLIER,
                )
        if not row:
            return None
        params = row[19] if isinstance(row[19], dict) else {}
        return FulfillmentContext(
            fulfillment_id=int(row[0]), connection_id=int(row[1]),
            external_order_id=str(row[2]), external_item_id=str(row[3]), offer_id=str(row[4]),
            quantity=int(row[5]), status=str(row[6]), reservation_ref=str(row[7]),
            order_status=str(row[8]), delivery_type=str(row[9] or ""),
            provider_code=str(row[21] or ""), activation_instruction=str(row[22] or ""),
            store_local_enabled=bool(row[10]), store_supplier_enabled=bool(row[11]),
            supplier_issue_enabled=bool(row[12]), pool_issue_enabled=bool(row[13]),
            support_issue_enabled=bool(row[14]), support_message=str(row[15] or "").strip(),
            mapping_id=int(row[16]) if row[16] is not None else None,
            service_id=int(row[17]) if row[17] is not None else None,
            nominal_id=str(row[18] or ""), params=dict(params), max_amount=_decimal(row[20]),
            workspace_id=int(row[23]), supplier_access_enabled=supplier_access_enabled,
        )

    def _supplier_access_enabled(self, workspace_id: int) -> bool:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                return workspace_allows(cursor, workspace_id, FULFILLMENT_SUPPLIER)

    def _resolve(self, fulfillment_id: int) -> None:
        context = self._context(fulfillment_id)
        if not context or context.status not in {"pending", "manual_required", "supplier_required"}:
            return
        if context.order_status != "processing" or context.delivery_type.strip().upper() != "DIGITAL":
            return

        # Инструкция обязательна для цифровой выдачи Яндекс Маркета, но это не
        # общее требование поставщика: например, Ozon сможет работать без неё.
        # Проверяем до покупки/резерва, чтобы не получить оплаченный ключ,
        # который затем нельзя отправить покупателю.
        if (
            context.provider_code == "yandex_market"
            and not normalize_buyer_text(context.activation_instruction)
        ):
            self._mark_manual(
                context.fulfillment_id,
                "Не заполнена инструкция покупателю для Яндекс Маркета",
            )
            return

        if context.supplier_issue_enabled and context.supplier_access_enabled:
            if not context.mapping_id or not context.service_id or not context.max_amount:
                self._wait_for_supplier(context, "Для карточки не завершена настройка Supplier Hub")
                return
            hub_settings = load_supplier_hub_settings()
            if not context.store_supplier_enabled or not hub_settings.fulfillment_enabled:
                self._wait_for_supplier(context, "Автовыдача Supplier Hub выключена защитным переключателем")
                return
            supplier_result = self._resolve_supplier(context)
            if supplier_result == "reserved":
                self._queue_outbound(context.fulfillment_id)
                return
            if supplier_result == "blocked":
                return

        elif context.supplier_issue_enabled and self._attempt_rows(context.fulfillment_id):
            # После downgrade не начинаем новую покупку, но обязательно доводим
            # уже известную Hub операцию до безопасного состояния.
            supplier_result = self._resolve_supplier(context)
            if supplier_result == "reserved":
                self._queue_outbound(context.fulfillment_id)
                return
            if supplier_result == "blocked":
                return

        if context.pool_issue_enabled and context.store_local_enabled and automatic_pool_enabled():
            self._reset_for_fallback(context.fulfillment_id)
            with self._psycopg.connect(self._database_url()) as connection:
                result = reserve_pool_keys(connection, fulfillment_id=context.fulfillment_id)
                connection.commit()
            if result.state == "reserved":
                self._queue_outbound(context.fulfillment_id)
                return

        if context.support_issue_enabled and context.store_local_enabled and context.support_message:
            self._reset_for_fallback(context.fulfillment_id)
            with self._psycopg.connect(self._database_url()) as connection:
                result = prepare_support_message(
                    connection, fulfillment_id=context.fulfillment_id,
                    message=context.support_message, user_id=0,
                )
                connection.commit()
            if result.state == "reserved":
                self._queue_outbound(context.fulfillment_id)
                return

        self._mark_manual(context.fulfillment_id, "Автоматические способы не подготовили полный комплект")

    def _ensure_attempts(self, context: FulfillmentContext) -> None:
        assert context.mapping_id is not None and context.max_amount is not None
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                for unit_index in range(1, context.quantity + 1):
                    idempotency_key = (
                        f"seller:yandex:{context.connection_id}:{context.external_order_id}:"
                        f"{context.external_item_id}:{unit_index}"
                    )
                    cursor.execute(
                        """
                        INSERT INTO seller.supplier_purchase_attempts(
                          fulfillment_id, supplier_mapping_id, unit_index,
                          idempotency_key, max_amount
                        ) VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT (fulfillment_id, unit_index) DO NOTHING
                        """,
                        (context.fulfillment_id, context.mapping_id, unit_index, idempotency_key, context.max_amount),
                    )

    def _attempt_rows(self, fulfillment_id: int) -> list[tuple[Any, ...]]:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT attempt.id, attempt.unit_index, attempt.idempotency_key,
                           attempt.request_id, attempt.hub_purchase_id, attempt.state,
                           attempt.max_amount, attempt.blocks_fallback,
                           attempt.result_available, attempt.result_key_id,
                           mapping.service_id, mapping.nominal_id, mapping.params
                    FROM seller.supplier_purchase_attempts AS attempt
                    JOIN seller.product_supplier_mappings AS mapping ON mapping.id=attempt.supplier_mapping_id
                    WHERE attempt.fulfillment_id=%s
                    ORDER BY attempt.unit_index
                    """,
                    (fulfillment_id,),
                )
                return list(cursor.fetchall())

    def _resolve_supplier(self, context: FulfillmentContext) -> str:
        if context.supplier_access_enabled:
            self._ensure_attempts(context)
        client = self._client_factory()
        for row in self._attempt_rows(context.fulfillment_id):
            attempt_id, idempotency_key, request_id = int(row[0]), str(row[2]), str(row[3] or "")
            purchase_id, state = str(row[4] or ""), str(row[5])
            result_key_id = int(row[9]) if row[9] is not None else None
            if result_key_id is not None or state == "failed":
                continue
            try:
                if not purchase_id:
                    # Тариф перечитывается непосредственно перед необратимой покупкой.
                    # Уже созданные покупки после downgrade продолжаем только сверять.
                    if not self._supplier_access_enabled(context.workspace_id):
                        self._mark_attempt_failed(attempt_id, "Автовыдача Supplier Hub недоступна на текущем тарифе")
                        continue
                    request_id = request_id or f"seller-{uuid4()}"
                    payload = client.create_purchase(
                        idempotency_key=idempotency_key,
                        service_id=int(row[10]), max_amount=str(row[6]),
                        nominal_id=str(row[11] or ""),
                        params=row[12] if isinstance(row[12], dict) else {},
                        request_id=request_id,
                    )
                else:
                    payload = client.purchase(purchase_id)
                self._save_hub_state(attempt_id, payload, request_id=request_id)
                state = str(payload.get("state") or "")
                purchase_id = str(payload.get("id") or purchase_id)
                if state == "succeeded" and bool(payload.get("result_available")):
                    code = client.purchase_result(purchase_id)
                    self._store_supplier_result(context, attempt_id, purchase_id, code)
            except SupplierHubError as exc:
                if exc.blocks_fallback:
                    self._attempt_error(attempt_id, str(exc), blocks_fallback=True)
                    self._wait_for_supplier(context, str(exc))
                    return "blocked"
                self._mark_attempt_failed(attempt_id, str(exc))

        attempts = self._attempt_rows(context.fulfillment_id)
        if any(
            row[9] is None and (str(row[5]) in HUB_BLOCKING_STATES or bool(row[7]))
            for row in attempts
        ):
            self._wait_for_supplier(context, "Supplier Hub ещё обрабатывает покупку")
            return "blocked"
        successful_ids = [int(row[9]) for row in attempts if row[9] is not None]
        if len(successful_ids) == context.quantity:
            return "reserved" if self._reserve_supplier_results(context, successful_ids) else "blocked"
        # Результаты уже сохранены свободными в локальном пуле. Если одна из
        # единиц окончательно не куплена, полный комплект может собрать fallback.
        return "failed"

    def _save_hub_state(self, attempt_id: int, payload: dict[str, Any], *, request_id: str) -> None:
        state = str(payload.get("state") or "")
        if state not in HUB_BLOCKING_STATES | HUB_TERMINAL_STATES:
            raise SupplierHubError("Supplier Hub returned an unknown purchase state")
        purchase_id = str(payload.get("id") or "")
        if not purchase_id:
            raise SupplierHubError("Supplier Hub did not return a purchase id")
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.supplier_purchase_attempts
                    SET hub_purchase_id=%s::uuid, request_id=%s, state=%s,
                        amount=%s, blocks_fallback=%s, result_available=%s,
                        provider_status=%s, provider_message=%s, last_error='',
                        next_poll_at=now() + (%s * interval '1 second'),
                        completed_at=CASE WHEN %s IN ('succeeded','failed') THEN now() ELSE completed_at END,
                        updated_at=now()
                    WHERE id=%s
                    """,
                    (
                        purchase_id, str(payload.get("request_id") or request_id), state,
                        _decimal(payload.get("amount")), bool(payload.get("blocks_fallback")),
                        bool(payload.get("result_available")), payload.get("provider_status"),
                        str(payload.get("provider_message") or "")[:2000], poll_delay_seconds(), state, attempt_id,
                    ),
                )

    def _attempt_error(self, attempt_id: int, message: str, *, blocks_fallback: bool) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.supplier_purchase_attempts
                    SET blocks_fallback=%s, last_error=%s,
                        next_poll_at=now() + (%s * interval '1 second'), updated_at=now()
                    WHERE id=%s
                    """,
                    (blocks_fallback, message[:1000], poll_delay_seconds(), attempt_id),
                )

    def _mark_attempt_failed(self, attempt_id: int, message: str) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.supplier_purchase_attempts
                    SET state='failed', blocks_fallback=false, last_error=%s,
                        completed_at=now(), updated_at=now()
                    WHERE id=%s
                    """,
                    (message[:1000], attempt_id),
                )

    def _store_supplier_result(
        self, context: FulfillmentContext, attempt_id: int, purchase_id: str, code: str,
    ) -> None:
        secret = key_pool_secret()
        fingerprint = sha256(f"seller-marketplace-key:v1:{code}".encode("utf-8")).hexdigest()
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT result_key_id FROM seller.supplier_purchase_attempts WHERE id=%s FOR UPDATE",
                    (attempt_id,),
                )
                existing = cursor.fetchone()
                if not existing or existing[0] is not None:
                    return
                cursor.execute(
                    """
                    INSERT INTO seller.marketplace_key_pools(connection_id, external_product_id)
                    VALUES (%s,%s)
                    ON CONFLICT (connection_id, external_product_id)
                    DO UPDATE SET updated_at=now()
                    RETURNING id
                    """,
                    (context.connection_id, context.offer_id),
                )
                pool_id = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    INSERT INTO seller.marketplace_keys(
                      pool_id, code_ciphertext, code_hash, code_suffix, status,
                      key_origin, source_system, source_reference
                    ) VALUES (
                      %s, pgp_sym_encrypt(%s,%s,'cipher-algo=aes256, compress-algo=0'),
                      %s,%s,'free','order','supplier_hub',%s
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (pool_id, code, secret, fingerprint, code[-4:], purchase_id),
                )
                inserted = cursor.fetchone()
                if not inserted:
                    cursor.execute(
                        """
                        SELECT id FROM seller.marketplace_keys
                        WHERE source_system='supplier_hub' AND source_reference=%s
                        """,
                        (purchase_id,),
                    )
                    inserted = cursor.fetchone()
                if not inserted:
                    raise RuntimeError("Supplier Hub вернул код, уже сохранённый из другого источника")
                cursor.execute(
                    """
                    UPDATE seller.supplier_purchase_attempts
                    SET result_key_id=%s, result_available=true, state='succeeded',
                        blocks_fallback=true, completed_at=now(), last_error='', updated_at=now()
                    WHERE id=%s
                    """,
                    (int(inserted[0]), attempt_id),
                )

    def _reserve_supplier_results(self, context: FulfillmentContext, key_ids: list[int]) -> bool:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status, reservation_ref FROM seller.order_fulfillments WHERE id=%s FOR UPDATE",
                    (context.fulfillment_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return False
                current_status, reservation_ref = str(row[0]), str(row[1])
                if current_status == "reserved":
                    return True
                if current_status not in {"pending", "manual_required", "supplier_required"}:
                    return False
                cursor.execute(
                    """
                    UPDATE seller.marketplace_keys
                    SET status='reserved', issued_order_ref=%s, reserved_at=now(), updated_at=now()
                    WHERE id=ANY(%s) AND key_origin='order' AND status='free'
                    RETURNING id
                    """,
                    (reservation_ref, key_ids),
                )
                updated = sorted(int(item[0]) for item in cursor.fetchall())
                if updated != sorted(key_ids):
                    raise RuntimeError("Не удалось закрепить полный комплект Supplier Hub")
                cursor.executemany(
                    """
                    INSERT INTO seller.fulfillment_key_reservations(fulfillment_id, key_id, order_ref)
                    VALUES (%s,%s,%s)
                    """,
                    [(context.fulfillment_id, key_id, reservation_ref) for key_id in key_ids],
                )
                cursor.execute(
                    """
                    UPDATE seller.order_fulfillments
                    SET status='reserved', delivery_source='supplier', reserved_at=now(),
                        last_error='', updated_at=now()
                    WHERE id=%s
                    """,
                    (context.fulfillment_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO seller.fulfillment_events(
                      fulfillment_id, event_type, from_status, to_status, details
                    ) VALUES (%s,'supplier_reserved',%s,'reserved',jsonb_build_object('quantity', %s))
                    """,
                    (context.fulfillment_id, current_status, len(key_ids)),
                )
        return True

    def _wait_for_supplier(self, context: FulfillmentContext, reason: str) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.order_fulfillments
                    SET status='supplier_required', last_error=%s,
                        next_resolve_at=now() + (%s * interval '1 second'), updated_at=now()
                    WHERE id=%s AND status IN ('pending','manual_required','supplier_required')
                    """,
                    (reason[:1000], poll_delay_seconds(), context.fulfillment_id),
                )

    def _reset_for_fallback(self, fulfillment_id: int) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.order_fulfillments
                    SET status='pending', delivery_source='unassigned', last_error='', updated_at=now()
                    WHERE id=%s AND status IN ('manual_required','supplier_required')
                    """,
                    (fulfillment_id,),
                )

    def _mark_manual(self, fulfillment_id: int, reason: str) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.order_fulfillments
                    SET status='manual_required', delivery_source='unassigned', last_error=%s,
                        next_resolve_at=now() + interval '1 day', updated_at=now()
                    WHERE id=%s AND status IN ('pending','manual_required','supplier_required')
                    """,
                    (reason[:1000], fulfillment_id),
                )

    def _queue_outbound(self, fulfillment_id: int) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO seller.fulfillment_outbound_jobs(fulfillment_id)
                    VALUES (%s)
                    ON CONFLICT (fulfillment_id) DO UPDATE SET
                      state='queued', last_error='', queued_at=now(), updated_at=now()
                    WHERE seller.fulfillment_outbound_jobs.state IN ('failed','cancelled')
                    """,
                    (fulfillment_id,),
                )


def build_supplier_fulfillment_processor(*, database_url, psycopg) -> SupplierFulfillmentProcessor:
    return SupplierFulfillmentProcessor(database_url=database_url, psycopg=psycopg)
