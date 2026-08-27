"""Локальная основа выдачи без раскрытия ключей и внешней отправки."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os

from domains.yandex_market_stock_queue import enqueue_yandex_stock_publication
from domains.ozon_stock_queue import enqueue_ozon_stock_publication


@dataclass(frozen=True)
class ReservationResult:
    fulfillment_id: int
    state: str
    reserved_key_ids: tuple[int, ...] = ()
    reason: str = ""


def automatic_pool_reservation_enabled() -> bool:
    # Глобальный аварийный выключатель имеет приоритет над настройками магазина и товара.
    return str(os.getenv("SELLER_POOL_RESERVATION_ENABLED", "false")).strip().lower() in {"1", "true", "yes"}


def manual_fulfillment_enabled() -> bool:
    # Ручные действия включаются отдельно от webhook и автоматического резерва только на время контролируемого перехода.
    return str(os.getenv("SELLER_MANUAL_FULFILLMENT_ENABLED", "false")).strip().lower() in {"1", "true", "yes"}


def observe_order_fulfillments(connection, *, connection_id: int, external_order_id: str) -> list[int]:
    """Создаёт локальные выдачи для PROCESSING и безопасно закрывает уже наблюдаемые позиции."""

    observed_ids: list[int] = []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT item.external_item_id, item.offer_id, item.quantity,
                   item.normalized_status, item.delivery_type, market.provider_code
            FROM seller.order_items AS item
            JOIN seller.marketplace_connections AS market ON market.id=item.connection_id
            WHERE item.connection_id=%s AND item.external_order_id=%s
            ORDER BY item.external_item_id
            FOR UPDATE OF item
            """,
            (connection_id, str(external_order_id)),
        )
        order_items = cursor.fetchall()
        for order_item in order_items:
            external_item_id, offer_id, quantity, normalized_status, delivery_type, *provider_values = order_item
            provider_code = str(provider_values[0] or "yandex_market") if provider_values else "yandex_market"
            item_id = str(external_item_id)
            product_id = str(offer_id or "").strip()
            item_quantity = max(0, int(quantity or 0))
            market_state = str(normalized_status or "problem")
            is_digital = str(delivery_type or "").strip().upper() == "DIGITAL"
            reservation_ref = f"seller:{provider_code}:{connection_id}:{external_order_id}:{item_id}"

            if market_state == "processing" and is_digital and product_id and item_quantity > 0:
                cursor.execute(
                    """
                    INSERT INTO seller.order_fulfillments(
                      connection_id, external_order_id, external_item_id, offer_id,
                      requested_quantity, reservation_ref
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (connection_id, external_order_id, external_item_id) DO UPDATE SET
                      offer_id=CASE
                        WHEN seller.order_fulfillments.status IN ('pending', 'manual_required', 'supplier_required')
                          THEN EXCLUDED.offer_id ELSE seller.order_fulfillments.offer_id END,
                      requested_quantity=CASE
                        WHEN seller.order_fulfillments.status IN ('pending', 'manual_required', 'supplier_required')
                          THEN EXCLUDED.requested_quantity ELSE seller.order_fulfillments.requested_quantity END,
                      updated_at=now()
                    RETURNING id
                    """,
                    (connection_id, str(external_order_id), item_id, product_id, item_quantity, reservation_ref),
                )
                observed_ids.append(int(cursor.fetchone()[0]))
                continue

            cursor.execute(
                """
                SELECT id, status, reservation_ref
                FROM seller.order_fulfillments
                WHERE connection_id=%s AND external_order_id=%s AND external_item_id=%s
                FOR UPDATE
                """,
                (connection_id, str(external_order_id), item_id),
            )
            fulfillment = cursor.fetchone()
            if not fulfillment:
                continue
            fulfillment_id, current_status, current_ref = int(fulfillment[0]), str(fulfillment[1]), str(fulfillment[2])
            observed_ids.append(fulfillment_id)

            if market_state == "cancelled" and current_status in {
                "pending", "reserved", "manual_required", "supplier_required", "failed",
            }:
                _release_reserved_keys(cursor, fulfillment_id=fulfillment_id, reservation_ref=current_ref, reason="order_cancelled")
                _transition(
                    cursor, fulfillment_id=fulfillment_id, from_status=current_status, to_status="cancelled",
                    event_type="order_cancelled", timestamp_column="cancelled_at",
                )
            elif market_state == "delivered" and current_status not in {"delivered", "cancelled", "closed_external"}:
                target_status = "delivered" if current_status in {"sending", "submitted", "unknown"} else "closed_external"
                if target_status == "delivered":
                    _consume_reserved_keys(
                        cursor, fulfillment_id=fulfillment_id, reservation_ref=current_ref,
                    )
                    # Подтверждённый Маркетом DELIVERED автоматически снимает
                    # неопределённость сетевого ответа: ручная сверка уже не нужна.
                    cursor.execute(
                        """
                        UPDATE seller.fulfillment_outbound_jobs
                        SET state='submitted', submitted_at=COALESCE(submitted_at, now()),
                            last_error='', lock_token=NULL, locked_until=NULL, updated_at=now()
                        WHERE fulfillment_id=%s AND state IN ('sending', 'unknown')
                        """,
                        (fulfillment_id,),
                    )
                else:
                    # Если Seller не начинал отправку, Маркет завершил заказ другой системой: локальные ключи освобождаем.
                    _release_reserved_keys(
                        cursor, fulfillment_id=fulfillment_id, reservation_ref=current_ref,
                        reason="delivered_external",
                    )
                _transition(
                    cursor, fulfillment_id=fulfillment_id, from_status=current_status, to_status=target_status,
                    event_type="market_delivered", timestamp_column="delivered_at",
                    delivery_source=None if target_status == "delivered" else "external",
                )
                if target_status == "delivered":
                    if provider_code == "yandex_market":
                        enqueue_yandex_stock_publication(cursor, fulfillment_id=fulfillment_id)
                    elif provider_code == "ozon":
                        enqueue_ozon_stock_publication(cursor, fulfillment_id=fulfillment_id)
            elif not is_digital and current_status in {
                "pending", "reserved", "manual_required", "supplier_required", "failed",
            }:
                # Старые ошибочно созданные выдачи для физических заказов безопасно закрываются.
                _release_reserved_keys(
                    cursor,
                    fulfillment_id=fulfillment_id,
                    reservation_ref=current_ref,
                    reason="non_digital_order",
                )
                _close_external_fulfillment(
                    cursor,
                    fulfillment_id=fulfillment_id,
                    from_status=current_status,
                    event_type="non_digital_order",
                )
    return observed_ids


def reserve_pool_keys(
    connection, *, fulfillment_id: int, require_automatic_gates: bool = True,
) -> ReservationResult:
    """Атомарно закрепляет полный комплект, не читая и не расшифровывая значения ключей."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fulfillment.id, fulfillment.connection_id, fulfillment.offer_id,
                   fulfillment.requested_quantity, fulfillment.status, fulfillment.reservation_ref,
                   marketplace_connection.fulfillment_reservation_enabled,
                   COALESCE(policy.pool_issue_enabled, settings.pool_issue_enabled, false), pool.id
            FROM seller.order_fulfillments AS fulfillment
            JOIN seller.marketplace_connections AS marketplace_connection
              ON marketplace_connection.id=fulfillment.connection_id
            LEFT JOIN seller.product_card_settings AS settings
              ON settings.connection_id=fulfillment.connection_id
             AND settings.external_product_id=fulfillment.offer_id
            LEFT JOIN seller.product_fulfillment_policies AS policy
              ON policy.connection_id=fulfillment.connection_id
             AND policy.external_product_id=fulfillment.offer_id
            LEFT JOIN seller.marketplace_key_pools AS pool
              ON pool.connection_id=fulfillment.connection_id
             AND pool.external_product_id=fulfillment.offer_id
            WHERE fulfillment.id=%s
            FOR UPDATE OF fulfillment
            """,
            (fulfillment_id,),
        )
        fulfillment = cursor.fetchone()
        if not fulfillment:
            return ReservationResult(int(fulfillment_id), "missing", reason="Выдача не найдена")

        row_id = int(fulfillment[0])
        required_quantity = int(fulfillment[3])
        current_status = str(fulfillment[4])
        reservation_ref = str(fulfillment[5])
        store_enabled = bool(fulfillment[6])
        product_enabled = bool(fulfillment[7])
        pool_id = int(fulfillment[8]) if fulfillment[8] is not None else None

        if current_status == "reserved":
            cursor.execute(
                """
                SELECT key_id FROM seller.fulfillment_key_reservations
                WHERE fulfillment_id=%s AND state='reserved'
                ORDER BY id
                """,
                (row_id,),
            )
            existing = tuple(int(row[0]) for row in cursor.fetchall())
            if len(existing) != required_quantity:
                raise RuntimeError("Нарушена целостность зарезервированного комплекта ключей")
            return ReservationResult(row_id, "reserved", existing)

        if current_status not in {"pending", "manual_required", "supplier_required"}:
            return ReservationResult(row_id, "skipped", reason=f"Статус {current_status} не допускает резерв")
        if require_automatic_gates and not store_enabled:
            return ReservationResult(row_id, "skipped", reason="Резервирование выключено для магазина")
        if require_automatic_gates and not product_enabled:
            return ReservationResult(row_id, "skipped", reason="Выдача из пула выключена для товара")
        if pool_id is None:
            return _mark_manual_required(cursor, row_id, current_status, "Для товара не создан пул ключей")

        cursor.execute(
            """
            SELECT id
            FROM seller.marketplace_keys
            WHERE pool_id=%s AND key_origin='pool' AND status='free'
              AND (expires_at IS NULL OR expires_at >= current_date)
            ORDER BY expires_at ASC NULLS LAST, created_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (pool_id, required_quantity),
        )
        key_ids = tuple(int(row[0]) for row in cursor.fetchall())
        if len(key_ids) != required_quantity:
            return _mark_manual_required(
                cursor, row_id, current_status,
                f"В пуле нет полного комплекта: требуется {required_quantity}, доступно {len(key_ids)}",
            )

        cursor.execute(
            """
            UPDATE seller.marketplace_keys
            SET status='reserved', issued_order_ref=%s, reserved_at=now(), updated_at=now()
            WHERE id=ANY(%s) AND status='free'
            RETURNING id
            """,
            (reservation_ref, list(key_ids)),
        )
        updated_ids = tuple(sorted(int(row[0]) for row in cursor.fetchall()))
        if updated_ids != tuple(sorted(key_ids)):
            raise RuntimeError("Не удалось атомарно закрепить полный комплект ключей")

        cursor.executemany(
            """
            INSERT INTO seller.fulfillment_key_reservations(fulfillment_id, key_id, order_ref)
            VALUES (%s,%s,%s)
            """,
            [(row_id, key_id, reservation_ref) for key_id in key_ids],
        )
        cursor.execute(
            """
            UPDATE seller.order_fulfillments
            SET status='reserved', delivery_source='pool', reserved_at=now(), last_error='', updated_at=now()
            WHERE id=%s
            """,
            (row_id,),
        )
        cursor.execute(
            """
            INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status, details)
            VALUES (%s, 'pool_reserved', %s, 'reserved', jsonb_build_object('quantity', %s))
            """,
            (row_id, current_status, required_quantity),
        )
        return ReservationResult(row_id, "reserved", key_ids)


def release_pool_keys(connection, *, fulfillment_id: int, reason: str = "operator_released") -> ReservationResult:
    """Снимает только неотправленный локальный резерв и оставляет выдачу готовой к повторной подготовке."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, status, reservation_ref
            FROM seller.order_fulfillments
            WHERE id=%s
            FOR UPDATE
            """,
            (fulfillment_id,),
        )
        fulfillment = cursor.fetchone()
        if not fulfillment:
            return ReservationResult(int(fulfillment_id), "missing", reason="Выдача не найдена")
        row_id, current_status, reservation_ref = int(fulfillment[0]), str(fulfillment[1]), str(fulfillment[2])
        if current_status != "reserved":
            return ReservationResult(row_id, "skipped", reason=f"Статус {current_status} не допускает снятие резерва")

        _release_reserved_keys(
            cursor,
            fulfillment_id=row_id,
            reservation_ref=reservation_ref,
            reason=str(reason or "operator_released")[:200],
        )
        cursor.execute(
            """
            UPDATE seller.order_fulfillments
            SET status='pending', delivery_source='unassigned', reserved_at=NULL,
                support_message_snapshot='', last_error='', updated_at=now()
            WHERE id=%s
            """,
            (row_id,),
        )
        cursor.execute(
            """
            INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status, details)
            VALUES (%s, 'pool_released', 'reserved', 'pending', jsonb_build_object('reason', %s))
            """,
            (row_id, str(reason or "operator_released")[:200]),
        )
        return ReservationResult(row_id, "pending")


def prepare_manual_keys(
    connection, *, fulfillment_id: int, codes: list[str], encryption_secret: str, user_id: int,
) -> ReservationResult:
    """Шифрует и закрепляет ровно один ручной комплект, не помещая его сначала в свободный пул."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fulfillment.id, fulfillment.connection_id, fulfillment.offer_id,
                   fulfillment.requested_quantity, fulfillment.status, fulfillment.reservation_ref,
                   fulfillment.external_order_id, fulfillment.external_item_id
            FROM seller.order_fulfillments AS fulfillment
            WHERE fulfillment.id=%s
            FOR UPDATE
            """,
            (fulfillment_id,),
        )
        row = cursor.fetchone()
        if not row:
            return ReservationResult(int(fulfillment_id), "missing", reason="Выдача не найдена")
        row_id, connection_id, offer_id = int(row[0]), int(row[1]), str(row[2])
        quantity, current_status, reservation_ref = int(row[3]), str(row[4]), str(row[5])
        if current_status not in {"pending", "manual_required", "supplier_required"}:
            return ReservationResult(row_id, "skipped", reason=f"Статус {current_status} не допускает ручной комплект")
        if len(codes) != quantity:
            return ReservationResult(row_id, "skipped", reason=f"Для позиции требуется ключей: {quantity}")
        fingerprints = [_manual_key_hash(code) for code in codes]
        if len(set(fingerprints)) != quantity:
            return ReservationResult(row_id, "skipped", reason="Один и тот же ключ нельзя указать дважды")
        cursor.execute(
            """
            INSERT INTO seller.marketplace_key_pools(connection_id, external_product_id)
            VALUES (%s,%s)
            ON CONFLICT (connection_id, external_product_id) DO UPDATE SET updated_at=now()
            RETURNING id
            """,
            (connection_id, offer_id),
        )
        pool_id = int(cursor.fetchone()[0])
        key_ids: list[int] = []
        for code, fingerprint in zip(codes, fingerprints, strict=True):
            cursor.execute(
                """
                INSERT INTO seller.marketplace_keys(
                  pool_id, code_ciphertext, code_hash, code_suffix, status,
                  key_origin, issued_order_ref, reserved_at, created_by_user_id
                ) VALUES (
                  %s, pgp_sym_encrypt(%s,%s,'cipher-algo=aes256, compress-algo=0'),
                  %s,%s,'reserved','order',%s,now(),%s
                )
                ON CONFLICT (code_hash) DO NOTHING
                RETURNING id
                """,
                (pool_id, code, encryption_secret, fingerprint, code[-4:], reservation_ref, user_id),
            )
            inserted = cursor.fetchone()
            if not inserted:
                raise ValueError("Один из ключей уже сохранён в Seller или закреплён за другим заказом")
            key_ids.append(int(inserted[0]))
        cursor.executemany(
            """
            INSERT INTO seller.fulfillment_key_reservations(fulfillment_id, key_id, order_ref)
            VALUES (%s,%s,%s)
            """,
            [(row_id, key_id, reservation_ref) for key_id in key_ids],
        )
        cursor.execute(
            """
            UPDATE seller.order_fulfillments
            SET status='reserved', delivery_source='manual', reserved_at=now(),
                support_message_snapshot='', last_error='', updated_at=now()
            WHERE id=%s
            """,
            (row_id,),
        )
        cursor.execute(
            """
            INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status, details)
            VALUES (%s,'manual_keys_prepared',%s,'reserved',jsonb_build_object('quantity', %s, 'user_id', %s))
            """,
            (row_id, current_status, quantity, user_id),
        )
        return ReservationResult(row_id, "reserved", tuple(key_ids))


def prepare_support_message(connection, *, fulfillment_id: int, message: str, user_id: int) -> ReservationResult:
    """Фиксирует снимок сообщения для заказа отдельно от лицензионных ключей."""

    normalized = str(message or "").strip()
    if not normalized:
        return ReservationResult(int(fulfillment_id), "skipped", reason="Сообщение поддержки не заполнено")
    if len(normalized) > 2000:
        return ReservationResult(int(fulfillment_id), "skipped", reason="Сообщение поддержки длиннее 2000 символов")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, status FROM seller.order_fulfillments WHERE id=%s FOR UPDATE
            """,
            (fulfillment_id,),
        )
        row = cursor.fetchone()
        if not row:
            return ReservationResult(int(fulfillment_id), "missing", reason="Выдача не найдена")
        row_id, current_status = int(row[0]), str(row[1])
        if current_status not in {"pending", "manual_required"}:
            return ReservationResult(row_id, "skipped", reason=f"Статус {current_status} не допускает сообщение поддержки")
        cursor.execute(
            """
            UPDATE seller.order_fulfillments
            SET status='reserved', delivery_source='support_message', support_message_snapshot=%s,
                reserved_at=now(), last_error='', updated_at=now()
            WHERE id=%s
            """,
            (normalized, row_id),
        )
        cursor.execute(
            """
            INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status, details)
            VALUES (%s,'support_message_prepared',%s,'reserved',jsonb_build_object('user_id', %s))
            """,
            (row_id, current_status, user_id),
        )
        return ReservationResult(row_id, "reserved")


def _manual_key_hash(value: str) -> str:
    return sha256(f"seller-marketplace-key:v1:{value}".encode("utf-8")).hexdigest()


def _mark_manual_required(cursor, fulfillment_id: int, from_status: str, reason: str) -> ReservationResult:
    cursor.execute(
        """
        UPDATE seller.order_fulfillments
        SET status='manual_required', last_error=%s, updated_at=now()
        WHERE id=%s
        """,
        (reason, fulfillment_id),
    )
    cursor.execute(
        """
        INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status, details)
        VALUES (%s, 'pool_unavailable', %s, 'manual_required', jsonb_build_object('reason', %s))
        """,
        (fulfillment_id, from_status, reason),
    )
    return ReservationResult(fulfillment_id, "manual_required", reason=reason)


def _release_reserved_keys(cursor, *, fulfillment_id: int, reservation_ref: str, reason: str) -> None:
    # Освобождает только ключи, которые ещё не перешли в sending/delivered и принадлежат этой выдаче.
    cursor.execute(
        """
        WITH released AS (
          UPDATE seller.fulfillment_key_reservations
          SET state='released', released_at=now(), release_reason=%s, updated_at=now()
          WHERE fulfillment_id=%s AND state='reserved'
          RETURNING key_id
        )
        UPDATE seller.marketplace_keys AS key
        SET status=CASE WHEN key.key_origin='pool' THEN 'free' ELSE 'disabled' END,
            issued_order_ref=CASE WHEN key.key_origin='pool' THEN '' ELSE key.issued_order_ref END,
            reserved_at=NULL, updated_at=now()
        WHERE key.id IN (SELECT key_id FROM released)
          AND key.status='reserved' AND key.issued_order_ref=%s
        """,
        (reason, fulfillment_id, reservation_ref),
    )


def _consume_reserved_keys(cursor, *, fulfillment_id: int, reservation_ref: str) -> None:
    # Только подтверждение Маркета окончательно помечает комплект доставленным.
    cursor.execute(
        """
        WITH consumed AS (
          UPDATE seller.fulfillment_key_reservations
          SET state='consumed', consumed_at=now(), updated_at=now()
          WHERE fulfillment_id=%s AND state='reserved'
          RETURNING key_id
        )
        UPDATE seller.marketplace_keys AS key
        SET status='delivered', issued_at=COALESCE(issued_at, now()), updated_at=now()
        WHERE key.id IN (SELECT key_id FROM consumed)
          AND key.status IN ('reserved', 'sending') AND key.issued_order_ref=%s
        """,
        (fulfillment_id, reservation_ref),
    )


def _close_external_fulfillment(cursor, *, fulfillment_id: int, from_status: str, event_type: str) -> None:
    cursor.execute(
        """
        UPDATE seller.order_fulfillments
        SET status='closed_external', delivery_source='external', last_error='', updated_at=now()
        WHERE id=%s
        """,
        (fulfillment_id,),
    )
    cursor.execute(
        """
        INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status)
        VALUES (%s,%s,%s,'closed_external')
        """,
        (fulfillment_id, event_type, from_status),
    )


def _transition(
    cursor, *, fulfillment_id: int, from_status: str, to_status: str,
    event_type: str, timestamp_column: str, delivery_source: str | None = None,
) -> None:
    if timestamp_column not in {"cancelled_at", "delivered_at"}:
        raise ValueError("Unsupported fulfillment timestamp column")
    source_sql = ", delivery_source=%s" if delivery_source is not None else ""
    params: list[object] = [to_status]
    if delivery_source is not None:
        params.append(delivery_source)
    params.append(fulfillment_id)
    cursor.execute(
        f"""
        UPDATE seller.order_fulfillments
        SET status=%s, {timestamp_column}=now(), last_error='', updated_at=now(){source_sql}
        WHERE id=%s
        """,
        params,
    )
    cursor.execute(
        """
        INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status)
        VALUES (%s,%s,%s,%s)
        """,
        (fulfillment_id, event_type, from_status, to_status),
    )
