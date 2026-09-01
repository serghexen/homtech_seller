"""Локальный read-only контракт показателей главной Seller."""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from domains.local_auth import AuthenticatedUser


MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


class MarketplaceDashboardItemOut(BaseModel):
    connection_id: int
    provider_code: str
    store_name: str
    status: str
    sales_today: str | None = None
    sales_month: str | None = None
    currency_code: str = ""
    pending_reviews: int | None = None
    pending_chats: int | None = None
    pending_chats_capped: bool = False
    insights_last_successful_sync_at: datetime | None = None
    insights_next_refresh_at: datetime | None = None
    insights_error: str = ""
    plan_code: str = "basic"
    plan_name: str = "Basic"
    subscription_status: str = "inactive"
    subscription_revision: int = 0
    subscription_days_remaining: int | None = None
    subscription_unlimited: bool = False


class MarketplaceDashboardListOut(BaseModel):
    items: list[MarketplaceDashboardItemOut]


def money_text(value: Decimal | None) -> str | None:
    return format(value, ".2f") if value is not None else None


def subscription_days(valid_until: datetime | None, *, now: datetime | None = None) -> int | None:
    if valid_until is None:
        return None
    current = now or datetime.now(timezone.utc)
    valid = valid_until if valid_until.tzinfo else valid_until.replace(tzinfo=timezone.utc)
    return max(0, (valid.astimezone(MOSCOW_TIMEZONE).date() - current.astimezone(MOSCOW_TIMEZONE).date()).days)


def sales_period_starts(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = (now or datetime.now(timezone.utc)).astimezone(MOSCOW_TIMEZONE)
    today = datetime.combine(current.date(), time.min, tzinfo=MOSCOW_TIMEZONE)
    month = datetime.combine(current.date().replace(day=1), time.min, tzinfo=MOSCOW_TIMEZONE)
    return today.astimezone(timezone.utc), month.astimezone(timezone.utc)


def dashboard_item_out(row: tuple) -> MarketplaceDashboardItemOut:
    """Сопоставляет SQL-строку с API-контрактом в одном тестируемом месте."""

    insights_ready = row[10] is not None
    remaining_days = subscription_days(row[16])
    return MarketplaceDashboardItemOut(
        connection_id=int(row[0]),
        provider_code=str(row[1]),
        store_name=str(row[2]),
        status=str(row[3]),
        sales_today=money_text(row[4]),
        sales_month=money_text(row[5]),
        currency_code=str(row[6] or ("RUB" if str(row[1]) == "ozon" else "RUR")),
        pending_reviews=int(row[7]) if insights_ready else None,
        pending_chats=int(row[8]) if insights_ready else None,
        pending_chats_capped=bool(row[9]) if insights_ready else False,
        insights_last_successful_sync_at=row[10],
        insights_next_refresh_at=row[11],
        insights_error=str(row[12] or ""),
        plan_code=str(row[13]),
        plan_name=str(row[14]),
        subscription_status=str(row[15]),
        subscription_revision=int(row[17]),
        subscription_days_remaining=remaining_days,
        subscription_unlimited=bool(row[18]) and row[16] is None,
    )


def mount_marketplace_dashboard_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Отдаёт только снимок workspace текущей сессии, не вызывает маркетплейсы из HTTP."""

    @app.get("/marketplaces/dashboard", response_model=MarketplaceDashboardListOut)
    def marketplace_dashboard(
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceDashboardListOut:
        day_start, month_start = sales_period_starts()
        with psycopg.connect(database_url()) as connection:
            seller_user = user_with_workspace(connection, user.user_id)
            if not seller_user:
                raise HTTPException(status_code=401, detail="Рабочая область недоступна")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT marketplace.id, marketplace.provider_code, marketplace.display_name,
                           marketplace.status,
                           sales.sales_today, sales.sales_month, COALESCE(sales.currency_code, ''),
                           snapshot.pending_reviews_count, snapshot.pending_chats_count,
                           COALESCE(snapshot.pending_chats_capped, false),
                           snapshot.last_successful_sync_at, snapshot.next_refresh_at,
                           COALESCE(snapshot.last_error, ''),
                           COALESCE(plan.code, 'basic'), COALESCE(plan.display_name, 'Basic'),
                           COALESCE(subscription.status, 'inactive'), subscription.valid_until,
                           COALESCE(subscription.revision, 0), subscription.connection_id IS NOT NULL
                    FROM seller.marketplace_connections AS marketplace
                    LEFT JOIN LATERAL (
                        SELECT
                            COALESCE(sum(order_row.sales_amount) FILTER (
                                WHERE order_row.created_at >= %s
                            ), 0) AS sales_today,
                            COALESCE(sum(order_row.sales_amount) FILTER (
                                WHERE order_row.created_at >= %s
                            ), 0) AS sales_month,
                            max(NULLIF(order_row.currency_code, '')) AS currency_code
                        FROM seller.marketplace_orders AS order_row
                        WHERE order_row.connection_id=marketplace.id
                          AND order_row.created_at >= %s
                          AND order_row.sales_amount IS NOT NULL
                          AND order_row.is_fake=false
                          AND order_row.normalized_status <> 'cancelled'
                    ) AS sales ON true
                    LEFT JOIN seller.marketplace_dashboard_snapshots AS snapshot
                      ON snapshot.connection_id=marketplace.id
                     AND snapshot.workspace_id=marketplace.workspace_id
                    LEFT JOIN seller.marketplace_connection_subscriptions AS subscription
                      ON subscription.connection_id=marketplace.id
                     AND subscription.workspace_id=marketplace.workspace_id
                    LEFT JOIN seller.plans AS plan ON plan.id=subscription.plan_id
                    WHERE marketplace.workspace_id=%s
                    ORDER BY marketplace.created_at, marketplace.id
                    """,
                    (day_start, month_start, month_start, seller_user.workspace_id),
                )
                rows = cursor.fetchall()

        return MarketplaceDashboardListOut(items=[dashboard_item_out(row) for row in rows])
