"""Тарифные возможности конкретного подключения магазина."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


FULFILLMENT_MANUAL = "fulfillment.manual"
KEY_POOL_MANAGE = "key_pool.manage"
FULFILLMENT_POOL = "fulfillment.pool"
SUPPLIER_MAPPING_MANAGE = "supplier_mapping.manage"
FULFILLMENT_SUPPLIER = "fulfillment.supplier"


@dataclass(frozen=True)
class ConnectionAccess:
    connection_id: int
    plan_code: str
    plan_name: str
    subscription_status: str
    capabilities: frozenset[str]
    revision: int

    def allows(self, capability: str) -> bool:
        return capability in self.capabilities


def read_connection_access(cursor, workspace_id: int, connection_id: int) -> ConnectionAccess:
    """Собирает права одного магазина и проверяет его принадлежность workspace."""
    return read_connection_accesses(cursor, workspace_id, [connection_id])[connection_id]


def read_connection_accesses(
    cursor,
    workspace_id: int,
    connection_ids: list[int] | tuple[int, ...] | set[int],
) -> dict[int, ConnectionAccess]:
    """Пакетно читает тарифы без N+1 запросов для списков из 20–100 магазинов."""
    requested_ids = sorted({int(connection_id) for connection_id in connection_ids})
    if not requested_ids:
        return {}

    cursor.execute(
        """
        SELECT connection.id, plan.id, plan.code, plan.display_name, subscription.status,
               subscription.valid_until, subscription.grace_until, subscription.revision
        FROM seller.marketplace_connections AS connection
        JOIN seller.marketplace_connection_subscriptions AS subscription
          ON subscription.connection_id=connection.id
         AND subscription.workspace_id=connection.workspace_id
        JOIN seller.plans AS plan ON plan.id=subscription.plan_id
        WHERE connection.workspace_id=%s AND connection.id=ANY(%s) AND plan.is_active=true
        """,
        (workspace_id, requested_ids),
    )
    subscription_rows = {int(row[0]): row for row in cursor.fetchall()}

    now = datetime.now(timezone.utc)
    active_plan_ids: set[int] = set()
    base_capabilities: dict[int, set[str]] = {connection_id: set() for connection_id in requested_ids}
    for connection_id, row in subscription_rows.items():
        status = str(row[4])
        valid_until = row[5]
        grace_until = row[6]
        base_is_active = status in {"trialing", "active"}
        if status == "past_due" and grace_until is not None and grace_until > now:
            base_is_active = True
        if valid_until is not None and valid_until <= now:
            base_is_active = False
        if base_is_active:
            active_plan_ids.add(int(row[1]))

    entitlements_by_plan: dict[int, set[str]] = {}
    if active_plan_ids:
        cursor.execute(
            """
            SELECT entitlement.plan_id, entitlement.capability_code
            FROM seller.plan_entitlements AS entitlement
            WHERE entitlement.plan_id=ANY(%s) AND entitlement.enabled=true
            """,
            (sorted(active_plan_ids),),
        )
        for plan_id, capability_code in cursor.fetchall():
            entitlements_by_plan.setdefault(int(plan_id), set()).add(str(capability_code))

    for connection_id, row in subscription_rows.items():
        status = str(row[4])
        valid_until = row[5]
        grace_until = row[6]
        base_is_active = status in {"trialing", "active"}
        if status == "past_due" and grace_until is not None and grace_until > now:
            base_is_active = True
        if valid_until is not None and valid_until <= now:
            base_is_active = False
        if base_is_active:
            base_capabilities[connection_id].update(entitlements_by_plan.get(int(row[1]), set()))

    cursor.execute(
        """
        SELECT connection_id, capability_code, enabled
        FROM seller.marketplace_connection_entitlement_overrides
        WHERE workspace_id=%s AND connection_id=ANY(%s)
          AND (expires_at IS NULL OR expires_at>now())
        """,
        (workspace_id, requested_ids),
    )
    for connection_id, capability_code, enabled in cursor.fetchall():
        capabilities = base_capabilities[int(connection_id)]
        if bool(enabled):
            capabilities.add(str(capability_code))
        else:
            capabilities.discard(str(capability_code))

    result: dict[int, ConnectionAccess] = {}
    for connection_id in requested_ids:
        row = subscription_rows.get(connection_id)
        if not row:
            # Отсутствующий магазин или подписка никогда не открывают коммерческую функцию.
            result[connection_id] = ConnectionAccess(
                connection_id, "basic", "Basic", "inactive", frozenset(), 0,
            )
            continue
        result[connection_id] = ConnectionAccess(
            connection_id=connection_id,
            plan_code=str(row[2]),
            plan_name=str(row[3]),
            subscription_status=str(row[4]),
            capabilities=frozenset(base_capabilities[connection_id]),
            revision=int(row[7]),
        )
    return result


def connection_allows(cursor, workspace_id: int, connection_id: int, capability: str) -> bool:
    return read_connection_access(cursor, workspace_id, connection_id).allows(capability)
