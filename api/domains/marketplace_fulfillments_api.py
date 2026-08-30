"""Защищённая локальная подготовка выдачи без раскрытия и отправки ключей."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from domains.connection_entitlements import (
    FULFILLMENT_MANUAL,
    FULFILLMENT_POOL,
    read_connection_access,
)
from domains.fulfillment_service import (
    manual_fulfillment_enabled,
    observe_order_fulfillments,
    prepare_manual_keys,
    prepare_support_message,
    release_pool_keys,
    reserve_pool_keys,
)
from domains.fulfillment_ownership import (
    automatic_fulfillment_resolver_enabled,
    automation_controls_fulfillment,
    manual_preparation_stage_ready,
)
from domains.local_auth import AuthenticatedUser
from domains.marketplace_order_eligibility import marketplace_order_allows_fulfillment
from domains.ozon_outbound import ozon_outbound_enabled
from domains.ozon_stock_queue import enqueue_ozon_stock_publication
from domains.yandex_market_outbound import key_pool_secret, yandex_outbound_enabled
from domains.yandex_market_stock_queue import enqueue_yandex_stock_publication


class FulfillmentIdentityIn(BaseModel):
    connection_id: int = Field(gt=0)
    external_order_id: str = Field(min_length=1, max_length=128)
    external_item_id: str = Field(min_length=1, max_length=256)


class FulfillmentManualKeysIn(FulfillmentIdentityIn):
    codes: list[str] = Field(min_length=1, max_length=100)


class FulfillmentUnknownResolutionIn(FulfillmentIdentityIn):
    resolution: Literal["accepted", "not_accepted"]
    comment: str = Field(default="", max_length=500)


class OrderFulfillmentOut(BaseModel):
    connection_id: int
    external_order_id: str
    external_item_id: str
    provider_code: str
    store_name: str
    connection_status: str
    order_status: str
    provider_status: str = ""
    digital_goods_type: str = ""
    order_ready_for_fulfillment: bool = False
    delivery_type: str = ""
    title: str
    offer_id: str
    quantity: int
    fulfillment_id: int | None = None
    fulfillment_status: str = "not_prepared"
    delivery_source: str = "unassigned"
    reserved_count: int = 0
    free_count: int = 0
    last_error: str = ""
    reserved_at: datetime | None = None
    fulfillment_deadline_at: datetime | None = None
    can_prepare: bool = False
    can_release: bool = False
    manual_actions_enabled: bool = False
    manual_access_enabled: bool = False
    pool_access_enabled: bool = False
    outbound_enabled: bool = False
    outbound_state: str = ""
    outbound_last_error: str = ""
    can_send: bool = False
    can_cancel_send: bool = False
    can_resolve_unknown: bool = False
    can_prepare_manual: bool = False
    can_prepare_support: bool = False
    support_message_configured: bool = False
    can_reveal_keys: bool = False
    can_reveal_support_message: bool = False
    automation_in_progress: bool = False


def mount_marketplace_fulfillment_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Подключает локальный резерв и постановку отправки в очередь без раскрытия ключей HTTP-процессу."""

    def workspace_for_user(connection, user: AuthenticatedUser):
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        return seller_user

    def normalized_identity(connection_id: int, order_id: str, item_id: str) -> tuple[int, str, str]:
        normalized_order_id = str(order_id or "").strip()
        normalized_item_id = str(item_id or "").strip()
        if not normalized_order_id or not normalized_item_id:
            raise HTTPException(status_code=400, detail="Не удалось определить позицию заказа")
        return int(connection_id), normalized_order_id, normalized_item_id

    def read_fulfillment(cursor, *, identity: tuple[int, str, str], workspace_id: int, role_code: str):
        connection_id, order_id, item_id = identity
        cursor.execute(
            """
            SELECT item.connection_id, item.external_order_id, item.external_item_id,
                   marketplace_connection.provider_code, marketplace_connection.display_name,
                   marketplace_connection.status, item.normalized_status, item.delivery_type, item.title,
                   item.offer_id, item.quantity,
                   fulfillment.id, fulfillment.status, fulfillment.delivery_source,
                   fulfillment.last_error, fulfillment.reserved_at,
                   COALESCE((
                     SELECT COUNT(*) FROM seller.fulfillment_key_reservations AS reservation
                     WHERE reservation.fulfillment_id=fulfillment.id
                       AND reservation.state IN ('reserved','consumed')
                   ), 0),
                   COALESCE((
                     SELECT COUNT(*)
                     FROM seller.marketplace_key_pools AS pool
                     JOIN seller.marketplace_keys AS key ON key.pool_id=pool.id
                     WHERE pool.connection_id=item.connection_id
                       AND pool.external_product_id=item.offer_id
                       AND key.key_origin='pool'
                       AND key.status='free'
                       AND (key.expires_at IS NULL OR key.expires_at >= current_date)
                   ), 0),
                   marketplace_connection.fulfillment_outbound_enabled,
                   COALESCE(settings.activation_instruction, imported_settings.activation_instruction, ''),
                   COALESCE(outbound.state, ''), COALESCE(outbound.last_error, ''),
                   COALESCE(fulfillment.support_message_snapshot, ''),
                   CASE WHEN COALESCE(settings.support_message_overridden, false)
                     THEN settings.support_message ELSE COALESCE(imported_settings.support_message, '') END,
                   CASE WHEN COALESCE(settings.support_message_overridden, false)
                     THEN settings.support_message_delivery_enabled
                     ELSE COALESCE(imported_settings.support_message_delivery_enabled, false) END,
                   item.fulfillment_deadline_at,
                   COALESCE(fulfillment.handling_mode, 'unassigned'),
                   COALESCE(
                     fulfillment.resolver_lock_token IS NOT NULL
                     AND fulfillment.resolver_locked_until >= now(),
                     false
                   ),
                   EXISTS (
                     SELECT 1
                     FROM seller.supplier_purchase_attempts AS attempt
                     WHERE attempt.fulfillment_id=fulfillment.id
                       AND attempt.result_key_id IS NULL
                       AND (
                         attempt.state IN ('queued','created','checked','payment_started','processing','requires_attention')
                         OR attempt.blocks_fallback=true
                       )
                   ),
                   item.provider_status,
                   item.raw_payload #>> '{delivery,digitalGoods,type}'
            FROM seller.order_items AS item
            JOIN seller.marketplace_connections AS marketplace_connection
              ON marketplace_connection.id=item.connection_id
            LEFT JOIN seller.order_fulfillments AS fulfillment
              ON fulfillment.connection_id=item.connection_id
             AND fulfillment.external_order_id=item.external_order_id
             AND fulfillment.external_item_id=item.external_item_id
            LEFT JOIN seller.product_card_settings AS settings
              ON settings.connection_id=item.connection_id
             AND settings.external_product_id=item.offer_id
            LEFT JOIN seller.yandex_product_settings_snapshot AS imported_settings
              ON imported_settings.connection_id=item.connection_id
             AND imported_settings.external_product_id=item.offer_id
            LEFT JOIN seller.fulfillment_outbound_jobs AS outbound
              ON outbound.fulfillment_id=fulfillment.id
            WHERE item.connection_id=%s AND item.external_order_id=%s AND item.external_item_id=%s
              AND marketplace_connection.workspace_id=%s
            """,
            (connection_id, order_id, item_id, workspace_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Позиция заказа не найдена")
        access = read_connection_access(cursor, workspace_id, connection_id)
        manual_access_enabled = access.allows(FULFILLMENT_MANUAL)
        pool_access_enabled = access.allows(FULFILLMENT_POOL)
        fulfillment_status = str(row[12] or "not_prepared")
        actions_enabled = manual_fulfillment_enabled()
        provider_code = str(row[3])
        provider_outbound_enabled = (
            yandex_outbound_enabled() if provider_code == "yandex_market" else ozon_outbound_enabled()
        )
        outbound_available = provider_outbound_enabled and bool(row[18])
        outbound_state = str(row[20] or "")
        outbound_active = outbound_state in {"queued", "preparing", "sending", "submitted", "unknown"}
        can_manage = role_code in {"owner", "operator"}
        resolver_enabled = automatic_fulfillment_resolver_enabled()
        handling_mode = str(row[26] or "unassigned") if len(row) > 26 else "unassigned"
        resolver_active = bool(row[27]) if len(row) > 27 else False
        supplier_attempt_active = bool(row[28]) if len(row) > 28 else False
        provider_status = str(row[29] or "") if len(row) > 29 else ""
        digital_goods_type = str(row[30] or "") if len(row) > 30 else ""
        order_ready_for_fulfillment = marketplace_order_allows_fulfillment(
            provider_code=provider_code,
            normalized_status=str(row[6] or ""),
            provider_status=provider_status,
            delivery_type=str(row[7] or ""),
            digital_goods_type=digital_goods_type,
        )
        automation_in_progress = automation_controls_fulfillment(
            fulfillment_status=fulfillment_status,
            handling_mode=handling_mode,
            outbound_state=outbound_state,
            resolver_enabled=resolver_enabled,
            resolver_active=resolver_active,
            supplier_attempt_active=supplier_attempt_active,
        )
        preparation_stage_ready = manual_preparation_stage_ready(
            fulfillment_status=fulfillment_status,
            handling_mode=handling_mode,
            resolver_enabled=resolver_enabled,
            automation_in_progress=automation_in_progress,
        )
        preparation_stage_available = bool(
            actions_enabled
            and can_manage
            and provider_code in {"yandex_market", "ozon"}
            and str(row[5]) == "active"
            and order_ready_for_fulfillment
            and not automation_in_progress
            and preparation_stage_ready
        )
        can_prepare_manual = bool(preparation_stage_available and manual_access_enabled)
        can_prepare = bool(can_prepare_manual and pool_access_enabled)
        support_message_configured = bool(str(row[23] or "").strip()) and bool(row[24])
        prepared_material_complete = (
            bool(str(row[22] or "").strip())
            if str(row[13] or "") == "support_message"
            else int(row[16] or 0) == int(row[10] or 0)
        )
        return OrderFulfillmentOut(
            connection_id=int(row[0]), external_order_id=str(row[1]), external_item_id=str(row[2]),
            provider_code=str(row[3]), store_name=str(row[4]), connection_status=str(row[5]),
            order_status=str(row[6]), provider_status=provider_status,
            digital_goods_type=digital_goods_type,
            order_ready_for_fulfillment=order_ready_for_fulfillment,
            delivery_type=str(row[7] or ""), title=str(row[8] or ""),
            offer_id=str(row[9] or ""), quantity=int(row[10] or 0),
            fulfillment_id=int(row[11]) if row[11] is not None else None,
            fulfillment_status=fulfillment_status, delivery_source=str(row[13] or "unassigned"),
            last_error=str(row[14] or ""), reserved_at=row[15],
            fulfillment_deadline_at=row[25] if len(row) > 25 else None,
            reserved_count=int(row[16] or 0), free_count=int(row[17] or 0),
            can_prepare=can_prepare,
            can_release=bool(
                actions_enabled and can_manage and fulfillment_status == "reserved"
                and not outbound_active and not automation_in_progress
            ),
            manual_actions_enabled=actions_enabled,
            manual_access_enabled=manual_access_enabled,
            pool_access_enabled=pool_access_enabled,
            outbound_enabled=outbound_available,
            outbound_state=outbound_state,
            outbound_last_error=str(row[21] or ""),
            can_send=bool(
                actions_enabled and manual_access_enabled and outbound_available and can_manage
                and provider_code in {"yandex_market", "ozon"} and str(row[5]) == "active"
                and order_ready_for_fulfillment
                and fulfillment_status == "reserved" and prepared_material_complete
                and (provider_code == "ozon" or bool(str(row[19] or "").strip()))
                and outbound_state in {"", "failed", "cancelled"}
                and not automation_in_progress
            ),
            can_cancel_send=bool(
                actions_enabled and can_manage and outbound_state == "queued" and not automation_in_progress
            ),
            can_resolve_unknown=bool(
                actions_enabled and can_manage and provider_code in {"yandex_market", "ozon"}
                and fulfillment_status == "unknown" and outbound_state == "unknown"
            ),
            can_prepare_manual=can_prepare_manual,
            can_prepare_support=bool(
                can_prepare_manual and provider_code == "yandex_market" and support_message_configured
            ),
            support_message_configured=support_message_configured,
            can_reveal_keys=bool(can_manage and int(row[16] or 0) > 0),
            can_reveal_support_message=bool(
                can_manage
                and str(row[13] or "") == "support_message"
                and bool(str(row[22] or "").strip())
            ),
            automation_in_progress=automation_in_progress,
        )

    def require_manual_feature_enabled() -> None:
        if not manual_fulfillment_enabled():
            raise HTTPException(status_code=503, detail="Ручная подготовка выдачи временно отключена")

    def require_manual_action_allowed(detail: OrderFulfillmentOut, *, requires_pool: bool = False) -> None:
        if detail.provider_code not in {"yandex_market", "ozon"}:
            raise HTTPException(status_code=409, detail="Маркетплейс пока не поддерживает локальную выдачу")
        if detail.connection_status != "active":
            raise HTTPException(status_code=409, detail="Магазин отключён")
        if not detail.order_ready_for_fulfillment:
            if detail.provider_code == "yandex_market" and detail.provider_status.upper() != "PROCESSING":
                raise HTTPException(status_code=409, detail="Дождитесь оплаты и статуса PROCESSING от Яндекс Маркета")
            raise HTTPException(status_code=409, detail="Маркетплейс ещё не разрешил выдачу этого цифрового заказа")
        if not detail.manual_access_enabled or (requires_pool and not detail.pool_access_enabled):
            raise HTTPException(
                status_code=403,
                detail="Текущий тариф магазина не разрешает эту подготовку выдачи",
            )
        allowed = detail.can_prepare if requires_pool else detail.can_prepare_manual
        if not allowed:
            if detail.automation_in_progress:
                raise HTTPException(
                    status_code=409,
                    detail="Заказ обрабатывается автовыдачей. Ручной ввод станет доступен после передачи оператору",
                )
            raise HTTPException(status_code=409, detail="Заказ пока не передан на ручную подготовку")

    def lock_manual_preparation(connection, fulfillment_id: int) -> None:
        # Повторяет проверку под блокировкой строки: worker не сможет начать
        # покупку между чтением окна и ручным закреплением комплекта.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT fulfillment.status, fulfillment.handling_mode,
                       COALESCE(
                         fulfillment.resolver_lock_token IS NOT NULL
                         AND fulfillment.resolver_locked_until >= now(),
                         false
                       ),
                       EXISTS (
                         SELECT 1
                         FROM seller.supplier_purchase_attempts AS attempt
                         WHERE attempt.fulfillment_id=fulfillment.id
                           AND attempt.result_key_id IS NULL
                           AND (
                             attempt.state IN ('queued','created','checked','payment_started','processing','requires_attention')
                             OR attempt.blocks_fallback=true
                           )
                       )
                FROM seller.order_fulfillments AS fulfillment
                WHERE fulfillment.id=%s
                FOR UPDATE
                """,
                (fulfillment_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=409, detail="Локальная выдача не найдена")
        status, handling_mode = str(row[0]), str(row[1] or "unassigned")
        resolver_active, supplier_attempt_active = bool(row[2]), bool(row[3])
        if resolver_active or supplier_attempt_active:
            raise HTTPException(status_code=409, detail="Автовыдача уже обрабатывает этот заказ")
        if handling_mode == "automatic" and status != "manual_required":
            raise HTTPException(status_code=409, detail="Автовыдача ещё не передала заказ оператору")
        if automatic_fulfillment_resolver_enabled() and status != "manual_required":
            raise HTTPException(
                status_code=409,
                detail="Дождитесь завершения автовыдачи или передачи заказа оператору",
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE seller.order_fulfillments SET handling_mode='manual', updated_at=now() WHERE id=%s",
                (fulfillment_id,),
            )

    def fulfillment_id_after_observe(connection, identity: tuple[int, str, str]) -> int:
        observe_order_fulfillments(connection, connection_id=identity[0], external_order_id=identity[1])
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id FROM seller.order_fulfillments
                WHERE connection_id=%s AND external_order_id=%s AND external_item_id=%s
                """,
                identity,
            )
            row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=409, detail="Не удалось создать локальную выдачу")
        return int(row[0])

    @app.get("/marketplaces/orders/fulfillment", response_model=OrderFulfillmentOut)
    def get_order_fulfillment(
        connection_id: int = Query(gt=0),
        external_order_id: str = Query(min_length=1, max_length=128),
        external_item_id: str = Query(min_length=1, max_length=256),
        user: AuthenticatedUser = Depends(current_user),
    ) -> OrderFulfillmentOut:
        # Просмотр показывает только состояние и количество, но не значения зарезервированных ключей.
        identity = normalized_identity(connection_id, external_order_id, external_item_id)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                return read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )

    @app.post("/marketplaces/orders/fulfillment/prepare", response_model=OrderFulfillmentOut)
    def prepare_order_fulfillment(
        payload: FulfillmentIdentityIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> OrderFulfillmentOut:
        # Явное действие оператора обходит только auto-флаги; оно не расшифровывает ключ и не вызывает маркетплейс.
        require_manual_feature_enabled()
        identity = normalized_identity(payload.connection_id, payload.external_order_id, payload.external_item_id)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для подготовки выдачи")
            with connection.cursor() as cursor:
                detail = read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )
            require_manual_action_allowed(detail, requires_pool=True)
            fulfillment_id = fulfillment_id_after_observe(connection, identity)
            lock_manual_preparation(connection, fulfillment_id)
            result = reserve_pool_keys(
                connection, fulfillment_id=fulfillment_id, require_automatic_gates=False,
            )
            if result.state in {"missing", "skipped"}:
                raise HTTPException(status_code=409, detail=result.reason or "Не удалось зарезервировать ключи")
            with connection.cursor() as cursor:
                return read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )

    @app.post("/marketplaces/orders/fulfillment/prepare-manual", response_model=OrderFulfillmentOut)
    def prepare_order_fulfillment_manually(
        payload: FulfillmentManualKeysIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> OrderFulfillmentOut:
        require_manual_feature_enabled()
        identity = normalized_identity(payload.connection_id, payload.external_order_id, payload.external_item_id)
        prepared_codes = [str(code or "").strip() for code in payload.codes]
        if any(not code or len(code) > 1024 for code in prepared_codes):
            raise HTTPException(status_code=400, detail="Каждый ключ должен быть непустым и не длиннее 1024 символов")
        try:
            encryption_secret = key_pool_secret()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Не настроено защищённое хранение ключей Seller") from exc
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для ручной выдачи")
            with connection.cursor() as cursor:
                detail = read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )
            require_manual_action_allowed(detail)
            fulfillment_id = fulfillment_id_after_observe(connection, identity)
            lock_manual_preparation(connection, fulfillment_id)
            try:
                result = prepare_manual_keys(
                    connection, fulfillment_id=fulfillment_id, codes=prepared_codes,
                    encryption_secret=encryption_secret, user_id=seller_user.id,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if result.state != "reserved":
                raise HTTPException(status_code=409, detail=result.reason or "Не удалось закрепить ручной комплект")
            with connection.cursor() as cursor:
                return read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )

    @app.post("/marketplaces/orders/fulfillment/prepare-support", response_model=OrderFulfillmentOut)
    def prepare_order_fulfillment_support(
        payload: FulfillmentIdentityIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> OrderFulfillmentOut:
        require_manual_feature_enabled()
        identity = normalized_identity(payload.connection_id, payload.external_order_id, payload.external_item_id)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для выдачи через поддержку")
            with connection.cursor() as cursor:
                detail = read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )
                require_manual_action_allowed(detail)
                cursor.execute(
                    """
                    SELECT CASE WHEN COALESCE(settings.support_message_overridden, false)
                             THEN settings.support_message ELSE COALESCE(imported_settings.support_message, '') END,
                           CASE WHEN COALESCE(settings.support_message_overridden, false)
                             THEN settings.support_message_delivery_enabled
                             ELSE COALESCE(imported_settings.support_message_delivery_enabled, false) END
                    FROM seller.order_items AS item
                    LEFT JOIN seller.product_card_settings AS settings
                      ON settings.connection_id=item.connection_id
                     AND settings.external_product_id=item.offer_id
                    LEFT JOIN seller.yandex_product_settings_snapshot AS imported_settings
                      ON imported_settings.connection_id=item.connection_id
                     AND imported_settings.external_product_id=item.offer_id
                    WHERE item.connection_id=%s AND item.external_order_id=%s AND item.external_item_id=%s
                    """,
                    identity,
                )
                support_row = cursor.fetchone() or ("", False)
                message = str(support_row[0] or "").strip() if bool(support_row[1]) else ""
            fulfillment_id = fulfillment_id_after_observe(connection, identity)
            lock_manual_preparation(connection, fulfillment_id)
            result = prepare_support_message(
                connection, fulfillment_id=fulfillment_id, message=message, user_id=seller_user.id,
            )
            if result.state != "reserved":
                raise HTTPException(status_code=409, detail=result.reason or "Не удалось подготовить сообщение поддержки")
            with connection.cursor() as cursor:
                return read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )

    @app.post("/marketplaces/orders/fulfillment/release", response_model=OrderFulfillmentOut)
    def release_order_fulfillment(
        payload: FulfillmentIdentityIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> OrderFulfillmentOut:
        # Ручное снятие возможно только до sending; отправленные или неопределённые состояния не откатываются.
        require_manual_feature_enabled()
        identity = normalized_identity(payload.connection_id, payload.external_order_id, payload.external_item_id)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для снятия резерва")
            with connection.cursor() as cursor:
                detail = read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )
            if not detail.fulfillment_id:
                raise HTTPException(status_code=409, detail="Для заказа ещё нет локальной выдачи")
            if not detail.can_release:
                if detail.automation_in_progress:
                    raise HTTPException(status_code=409, detail="Автовыдача управляет резервом этого заказа")
                raise HTTPException(status_code=409, detail="Резерв сейчас нельзя снять")
            if detail.outbound_state in {"queued", "preparing", "sending", "submitted", "unknown"}:
                raise HTTPException(status_code=409, detail="Сначала завершите или сверьте внешнюю отправку")
            result = release_pool_keys(connection, fulfillment_id=detail.fulfillment_id)
            if result.state != "pending":
                raise HTTPException(status_code=409, detail=result.reason or "Резерв уже нельзя снять")
            with connection.cursor() as cursor:
                return read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )

    @app.post("/marketplaces/orders/fulfillment/send", response_model=OrderFulfillmentOut)
    def send_order_fulfillment(
        payload: FulfillmentIdentityIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> OrderFulfillmentOut:
        # API только ставит неизменяемую ссылку на выдачу в очередь; расшифровка и HTTP-вызов живут в worker-е.
        require_manual_feature_enabled()
        identity = normalized_identity(payload.connection_id, payload.external_order_id, payload.external_item_id)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для отправки выдачи")
            with connection.cursor() as cursor:
                detail = read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )
                provider_enabled = (
                    yandex_outbound_enabled()
                    if detail.provider_code == "yandex_market"
                    else ozon_outbound_enabled()
                )
                if not provider_enabled:
                    raise HTTPException(status_code=503, detail="Внешняя отправка временно отключена")
                if not detail.can_send or not detail.fulfillment_id:
                    raise HTTPException(
                        status_code=409,
                        detail=detail.outbound_last_error or "Комплект или инструкция ещё не готовы к отправке",
                    )
                cursor.execute(
                    """
                    INSERT INTO seller.fulfillment_outbound_jobs(fulfillment_id, requested_by_user_id)
                    VALUES (%s,%s)
                    ON CONFLICT (fulfillment_id) DO UPDATE SET
                      requested_by_user_id=EXCLUDED.requested_by_user_id,
                      state='queued', last_error='', request_fingerprint='',
                      lock_token=NULL, locked_until=NULL, queued_at=now(),
                      sending_at=NULL, submitted_at=NULL, unknown_at=NULL,
                      failed_at=NULL, cancelled_at=NULL, updated_at=now()
                    WHERE seller.fulfillment_outbound_jobs.state IN ('failed', 'cancelled')
                    RETURNING id
                    """,
                    (detail.fulfillment_id, seller_user.id),
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=409, detail="Отправка уже поставлена в очередь или требует сверки")
                cursor.execute(
                    """
                    INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status, details)
                    VALUES (%s,'outbound_queued','reserved','reserved',jsonb_build_object('requested_by_user_id', %s))
                    """,
                    (detail.fulfillment_id, seller_user.id),
                )
                return read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )

    @app.post("/marketplaces/orders/fulfillment/cancel-send", response_model=OrderFulfillmentOut)
    def cancel_order_fulfillment_send(
        payload: FulfillmentIdentityIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> OrderFulfillmentOut:
        # Отмена безопасна только пока worker ещё не взял задание и не начал сетевую операцию.
        require_manual_feature_enabled()
        identity = normalized_identity(payload.connection_id, payload.external_order_id, payload.external_item_id)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для отмены отправки")
            with connection.cursor() as cursor:
                detail = read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )
                if not detail.fulfillment_id or detail.outbound_state != "queued":
                    raise HTTPException(status_code=409, detail="Отменить можно только задание, которое ещё ожидает worker")
                cursor.execute(
                    """
                    UPDATE seller.fulfillment_outbound_jobs
                    SET state='cancelled', cancelled_at=now(), last_error='', updated_at=now()
                    WHERE fulfillment_id=%s AND state='queued'
                    """,
                    (detail.fulfillment_id,),
                )
                if cursor.rowcount != 1:
                    raise HTTPException(status_code=409, detail="Worker уже начал обработку; отмена небезопасна")
                cursor.execute(
                    """
                    INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status)
                    VALUES (%s,'outbound_cancelled','reserved','reserved')
                    """,
                    (detail.fulfillment_id,),
                )
                return read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )

    @app.post("/marketplaces/orders/fulfillment/resolve-unknown", response_model=OrderFulfillmentOut)
    def resolve_unknown_order_fulfillment(
        payload: FulfillmentUnknownResolutionIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> OrderFulfillmentOut:
        """Фиксирует результат ручной сверки с маркетплейсом без внешнего HTTP-запроса."""

        require_manual_feature_enabled()
        identity = normalized_identity(payload.connection_id, payload.external_order_id, payload.external_item_id)
        comment = str(payload.comment or "").strip()
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            if seller_user.role_code not in {"owner", "operator"}:
                raise HTTPException(status_code=403, detail="Недостаточно прав для сверки отправки")
            with connection.cursor() as cursor:
                detail = read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )
                if not detail.fulfillment_id or not detail.can_resolve_unknown:
                    raise HTTPException(status_code=409, detail="Эта отправка уже сверена или не находится в состоянии unknown")
                cursor.execute(
                    """
                    SELECT fulfillment.status, fulfillment.reservation_ref, outbound.state
                    FROM seller.order_fulfillments AS fulfillment
                    JOIN seller.fulfillment_outbound_jobs AS outbound
                      ON outbound.fulfillment_id=fulfillment.id
                    WHERE fulfillment.id=%s
                    FOR UPDATE OF fulfillment, outbound
                    """,
                    (detail.fulfillment_id,),
                )
                state = cursor.fetchone()
                if not state or str(state[0]) != "unknown" or str(state[2]) != "unknown":
                    raise HTTPException(status_code=409, detail="Состояние изменилось; обновите карточку заказа")

                if payload.resolution == "accepted":
                    cursor.execute(
                        """
                        UPDATE seller.fulfillment_outbound_jobs
                        SET state='submitted', submitted_at=COALESCE(submitted_at, now()),
                            last_error='', lock_token=NULL, locked_until=NULL, updated_at=now()
                        WHERE fulfillment_id=%s AND state='unknown'
                        """,
                        (detail.fulfillment_id,),
                    )
                    cursor.execute(
                        """
                        UPDATE seller.order_fulfillments
                        SET status='submitted', submitted_at=COALESCE(submitted_at, now()),
                            last_error='', updated_at=now()
                        WHERE id=%s AND status='unknown'
                        """,
                        (detail.fulfillment_id,),
                    )
                    if detail.provider_code == "ozon":
                        enqueue_ozon_stock_publication(cursor, fulfillment_id=detail.fulfillment_id)
                    else:
                        enqueue_yandex_stock_publication(cursor, fulfillment_id=detail.fulfillment_id)
                    event_type, target_status = "outbound_unknown_resolved_accepted", "submitted"
                else:
                    if not detail.order_ready_for_fulfillment:
                        raise HTTPException(
                            status_code=409,
                            detail="Повтор можно разрешить только пока заказ остаётся в обработке",
                        )
                    cursor.execute(
                        """
                        UPDATE seller.marketplace_keys AS key
                        SET status='reserved', updated_at=now()
                        WHERE key.id IN (
                          SELECT reservation.key_id
                          FROM seller.fulfillment_key_reservations AS reservation
                          WHERE reservation.fulfillment_id=%s AND reservation.state='reserved'
                        )
                          AND key.status='sending' AND key.issued_order_ref=%s
                        """,
                        (detail.fulfillment_id, str(state[1])),
                    )
                    cursor.execute(
                        """
                        UPDATE seller.fulfillment_outbound_jobs
                        SET state='failed', failed_at=now(),
                            last_error='Оператор подтвердил, что маркетплейс не получил данные',
                            lock_token=NULL, locked_until=NULL, updated_at=now()
                        WHERE fulfillment_id=%s AND state='unknown'
                        """,
                        (detail.fulfillment_id,),
                    )
                    cursor.execute(
                        """
                        UPDATE seller.order_fulfillments
                        SET status='reserved',
                            last_error='Оператор разрешил повторную отправку после сверки', updated_at=now()
                        WHERE id=%s AND status='unknown'
                        """,
                        (detail.fulfillment_id,),
                    )
                    event_type, target_status = "outbound_unknown_resolved_not_accepted", "reserved"

                cursor.execute(
                    """
                    INSERT INTO seller.fulfillment_events(
                      fulfillment_id, event_type, from_status, to_status, details
                    ) VALUES (
                      %s,%s,'unknown',%s,
                      jsonb_build_object('resolution', %s, 'user_id', %s, 'comment', %s)
                    )
                    """,
                    (
                        detail.fulfillment_id, event_type, target_status,
                        payload.resolution, seller_user.id, comment,
                    ),
                )
                return read_fulfillment(
                    cursor, identity=identity, workspace_id=seller_user.workspace_id, role_code=seller_user.role_code,
                )
