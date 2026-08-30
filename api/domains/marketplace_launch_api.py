"""Self-service preflight и атомарный запуск выдачи отдельного магазина."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from domains.local_auth import AuthenticatedUser
from domains.connection_entitlements import (
    FULFILLMENT_MANUAL,
    FULFILLMENT_POOL,
    FULFILLMENT_SUPPLIER,
    read_connection_access,
)


LaunchCheckState = Literal["ready", "warning", "blocked"]


class MarketplaceLaunchCheckOut(BaseModel):
    code: str
    title: str
    state: LaunchCheckState
    detail: str


class MarketplaceLaunchReadinessOut(BaseModel):
    connection_id: int
    provider_code: str
    display_name: str
    launch_state: str
    can_launch: bool
    plan_code: str
    plan_name: str
    chain: list[str]
    automatic_stock_enabled: bool
    checks: list[MarketplaceLaunchCheckOut]


class MarketplaceLaunchIn(BaseModel):
    confirm_exclusive_control: bool = False
    automatic_stock_enabled: bool = False


def _env_enabled(name: str) -> bool:
    return str(os.getenv(name, "false")).strip().lower() in {"1", "true", "yes"}


def _platform_switches(provider_code: str) -> tuple[bool, str]:
    required = {
        "SELLER_FULFILLMENT_RESOLVER_ENABLED": "обработчик цепочки выдачи",
        "SELLER_POOL_RESERVATION_ENABLED": "резервирование пула",
        "SELLER_MANUAL_FULFILLMENT_ENABLED": "ручная выдача",
        "SELLER_OZON_OUTBOUND_ENABLED" if provider_code == "ozon" else "SELLER_YANDEX_OUTBOUND_ENABLED":
            "отправка в маркетплейс",
    }
    missing = [label for name, label in required.items() if not _env_enabled(name)]
    if missing:
        return False, "На платформе выключены: " + ", ".join(missing)
    return True, "Общие аварийные переключатели платформы включены"


def _check(code: str, title: str, state: LaunchCheckState, detail: str) -> MarketplaceLaunchCheckOut:
    return MarketplaceLaunchCheckOut(code=code, title=title, state=state, detail=detail)


def mount_marketplace_launch_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Подключает запуск, всегда ограниченный workspace из текущей сессии."""

    def workspace_for_user(connection, user: AuthenticatedUser):
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        return seller_user

    def require_owner(seller_user) -> None:
        if seller_user.role_code != "owner":
            raise HTTPException(status_code=403, detail="Запуск магазина доступен только владельцу")

    def read_readiness(cursor, *, workspace_id: int, connection_id: int) -> MarketplaceLaunchReadinessOut:
        cursor.execute(
            """
            SELECT id, provider_code, display_name, status, launch_state,
                   stock_outbound_enabled, last_error
            FROM seller.marketplace_connections
            WHERE id=%s AND workspace_id=%s
            """,
            (connection_id, workspace_id),
        )
        connection_row = cursor.fetchone()
        if not connection_row:
            raise HTTPException(status_code=404, detail="Подключенный магазин не найден")

        provider_code = str(connection_row[1])
        launch_state = str(connection_row[4])
        access = read_connection_access(cursor, workspace_id, connection_id)

        cursor.execute(
            """
            SELECT
              EXISTS(SELECT 1 FROM seller.marketplace_sync_jobs
                     WHERE connection_id=%s AND sync_kind='catalog' AND status='succeeded'),
              (SELECT max(finished_at) FROM seller.marketplace_sync_jobs
               WHERE connection_id=%s AND sync_kind='orders' AND status='succeeded'),
              (SELECT count(*) FROM seller.catalog_items
               WHERE connection_id=%s AND is_present=true AND is_archived=false),
              ((SELECT count(*)
               FROM seller.order_fulfillments AS fulfillment
               LEFT JOIN seller.fulfillment_outbound_jobs AS outbound
                 ON outbound.fulfillment_id=fulfillment.id
               WHERE fulfillment.connection_id=%s
                 AND (fulfillment.status IN ('sending','unknown')
                      OR outbound.state IN ('sending','unknown')))
               + (SELECT count(*)
                  FROM seller.supplier_purchase_attempts AS attempt
                  JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=attempt.fulfillment_id
                  WHERE fulfillment.connection_id=%s
                    AND attempt.state IN ('created','checked','payment_started','processing','requires_attention'))
               + (SELECT count(*)
                  FROM seller.yandex_stock_outbound_jobs AS stock_job
                  JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=stock_job.fulfillment_id
                  WHERE fulfillment.connection_id=%s AND stock_job.state='sending')
               + (SELECT count(*)
                  FROM seller.ozon_stock_outbound_jobs AS stock_job
                  LEFT JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=stock_job.fulfillment_id
                  WHERE COALESCE(fulfillment.connection_id, stock_job.connection_id)=%s
                    AND stock_job.state='sending')),
              (SELECT count(*)
               FROM seller.product_fulfillment_policies AS policy
               WHERE policy.connection_id=%s
                 AND (policy.supplier_issue_enabled=true OR policy.pool_issue_enabled=true
                      OR policy.support_message_delivery_enabled=true))
            """,
            (
                connection_id, connection_id, connection_id,
                connection_id, connection_id, connection_id, connection_id,
                connection_id,
            ),
        )
        catalog_synced, orders_snapshot_at, catalog_count, unsafe_count, configured_count = cursor.fetchone()
        orders_snapshot_fresh = bool(
            orders_snapshot_at
            and orders_snapshot_at >= datetime.now(timezone.utc) - timedelta(minutes=5)
        )

        webhook_seen_at = None
        if provider_code == "yandex_market":
            cursor.execute(
                """
                SELECT max(received_at)
                FROM seller.yandex_webhook_events
                WHERE connection_id=%s AND order_id<>''
                """,
                (connection_id,),
            )
            webhook_seen_at = cursor.fetchone()[0]

        checks: list[MarketplaceLaunchCheckOut] = []
        is_active = str(connection_row[3]) == "active"
        checks.append(_check(
            "connection", "Подключение магазина",
            "ready" if is_active else "blocked",
            "API-доступ активен" if is_active else "Сначала подключите магазин повторно",
        ))
        checks.append(_check(
            "catalog", "Каталог",
            "ready" if bool(catalog_synced) and int(catalog_count) > 0 else "blocked",
            f"Синхронизировано активных карточек: {int(catalog_count)}"
            if bool(catalog_synced) else "Первичная синхронизация каталога ещё не завершена",
        ))
        checks.append(_check(
            "orders", "Заказы",
            "ready" if orders_snapshot_fresh else "blocked",
            f"Актуальный снимок получен: {orders_snapshot_at:%d.%m.%Y %H:%M}"
            if orders_snapshot_fresh else (
                "Перед запуском обновите заказы — снимок должен быть не старше 5 минут"
                if orders_snapshot_at else "Первичная синхронизация заказов ещё не завершена"
            ),
        ))
        checks.append(_check(
            "unfinished", "Незавершённые отправки",
            "ready" if int(unsafe_count) == 0 else "blocked",
            "Неопределённых внешних операций нет" if int(unsafe_count) == 0
            else f"Нужно проверить незавершённые операции: {int(unsafe_count)}",
        ))
        platform_ready, platform_detail = _platform_switches(provider_code)
        checks.append(_check(
            "platform", "Контур Seller",
            "ready" if platform_ready else "blocked", platform_detail,
        ))
        base_plan_ready = access.allows(FULFILLMENT_MANUAL) and access.allows(FULFILLMENT_POOL)
        checks.append(_check(
            "subscription", "Подписка магазина",
            "ready" if base_plan_ready else "blocked",
            f"Тариф {access.plan_name} активен" if base_plan_ready
            else "Тариф магазина не разрешает запуск выдачи",
        ))
        checks.append(_check(
            "sources", "Способы выдачи",
            "ready" if int(configured_count) > 0 else "warning",
            f"Настроенных карточек: {int(configured_count)}" if int(configured_count) > 0
            else "Автоматические источники ещё не настроены — заказ перейдёт на ручной ввод",
        ))
        if provider_code == "yandex_market":
            checks.append(_check(
                "webhook", "Онлайн-уведомления Яндекса",
                "ready" if webhook_seen_at else "warning",
                f"Последнее событие: {webhook_seen_at:%d.%m.%Y %H:%M}" if webhook_seen_at
                else "Событий этого магазина ещё не было; резервная синхронизация заказов останется включена",
            ))
        if str(connection_row[6] or ""):
            checks.append(_check(
                "last_error", "Последняя синхронизация", "warning", str(connection_row[6]),
            ))

        chain = ["Поставщик"] if access.allows(FULFILLMENT_SUPPLIER) else []
        if access.allows(FULFILLMENT_POOL):
            chain.append("Пул ключей")
        if access.allows(FULFILLMENT_MANUAL):
            chain.extend(["Поддержка", "Ручной ввод"])
        can_launch = launch_state == "running" or not any(item.state == "blocked" for item in checks)
        return MarketplaceLaunchReadinessOut(
            connection_id=int(connection_row[0]), provider_code=provider_code,
            display_name=str(connection_row[2]), launch_state=launch_state,
            can_launch=can_launch, plan_code=access.plan_code, plan_name=access.plan_name,
            chain=chain, automatic_stock_enabled=bool(connection_row[5]), checks=checks,
        )

    @app.get(
        "/marketplaces/connections/{connection_id}/launch-readiness",
        response_model=MarketplaceLaunchReadinessOut,
    )
    def launch_readiness(
        connection_id: int,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceLaunchReadinessOut:
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                return read_readiness(
                    cursor, workspace_id=seller_user.workspace_id, connection_id=connection_id,
                )

    @app.post(
        "/marketplaces/connections/{connection_id}/launch",
        response_model=MarketplaceLaunchReadinessOut,
    )
    def launch_connection(
        connection_id: int,
        payload: MarketplaceLaunchIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceLaunchReadinessOut:
        if not payload.confirm_exclusive_control:
            raise HTTPException(
                status_code=400,
                detail="Подтвердите, что другая система больше не выдаёт заказы этого магазина",
            )
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            require_owner(seller_user)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (20_260_828, int(connection_id) % 2_147_483_647),
                )
                readiness = read_readiness(
                    cursor, workspace_id=seller_user.workspace_id, connection_id=connection_id,
                )
                if not readiness.can_launch:
                    raise HTTPException(status_code=409, detail="Магазин ещё не готов к запуску")
                supplier_enabled = "Поставщик" in readiness.chain
                polling_interval = 60 if readiness.provider_code == "ozon" else 300
                cursor.execute(
                    """
                    UPDATE seller.marketplace_connections
                    SET launch_state='running',
                        fulfillment_started_at=COALESCE(fulfillment_started_at, now()),
                        fulfillment_started_by_user_id=COALESCE(fulfillment_started_by_user_id, %s),
                        exclusive_control_confirmed_at=now(),
                        webhook_processing_enabled=(provider_code='yandex_market'),
                        fulfillment_reservation_enabled=true,
                        fulfillment_outbound_enabled=true,
                        supplier_fulfillment_enabled=%s,
                        stock_outbound_enabled=%s,
                        orders_polling_enabled=true,
                        orders_poll_interval_seconds=%s,
                        next_orders_poll_at=now() + (((id * 37) %% %s) * interval '1 second'),
                        last_orders_poll_error='', updated_at=now()
                    WHERE id=%s AND workspace_id=%s AND status='active'
                    RETURNING id
                    """,
                    (
                        seller_user.id, supplier_enabled, payload.automatic_stock_enabled,
                        polling_interval, polling_interval, connection_id, seller_user.workspace_id,
                    ),
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=404, detail="Активный магазин не найден")
                cursor.execute(
                    """
                    INSERT INTO seller.marketplace_connection_launch_events(
                      workspace_id, connection_id, from_state, to_state,
                      automatic_stock_enabled, actor_user_id, readiness_snapshot
                    ) VALUES (%s,%s,%s,'running',%s,%s,%s::jsonb)
                    """,
                    (
                        seller_user.workspace_id, connection_id, readiness.launch_state,
                        payload.automatic_stock_enabled, seller_user.id,
                        json.dumps(readiness.model_dump(mode="json"), ensure_ascii=False),
                    ),
                )
                return read_readiness(
                    cursor, workspace_id=seller_user.workspace_id, connection_id=connection_id,
                )
