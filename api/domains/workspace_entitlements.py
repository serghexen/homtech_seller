"""Тарифные возможности workspace, применяемые без рестарта Seller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


FULFILLMENT_MANUAL = "fulfillment.manual"
KEY_POOL_MANAGE = "key_pool.manage"
FULFILLMENT_POOL = "fulfillment.pool"
SUPPLIER_MAPPING_MANAGE = "supplier_mapping.manage"
FULFILLMENT_SUPPLIER = "fulfillment.supplier"


@dataclass(frozen=True)
class WorkspaceAccess:
    plan_code: str
    plan_name: str
    subscription_status: str
    capabilities: frozenset[str]
    revision: int

    def allows(self, capability: str) -> bool:
        return capability in self.capabilities


def read_workspace_access(cursor, workspace_id: int) -> WorkspaceAccess:
    """Собирает эффективные права тарифа с учётом срока и точечных overrides."""
    cursor.execute(
        """
        SELECT plan.id, plan.code, plan.display_name, subscription.status,
               subscription.valid_until, subscription.grace_until, subscription.revision
        FROM seller.workspace_subscriptions AS subscription
        JOIN seller.plans AS plan ON plan.id=subscription.plan_id
        WHERE subscription.workspace_id=%s AND plan.is_active=true
        LIMIT 1
        """,
        (workspace_id,),
    )
    row = cursor.fetchone()
    if not row:
        # Отсутствующая подписка не должна случайно открыть коммерческую функцию.
        return WorkspaceAccess("basic", "Basic", "inactive", frozenset(), 0)

    now = datetime.now(timezone.utc)
    status = str(row[3])
    valid_until = row[4]
    grace_until = row[5]
    base_is_active = status in {"trialing", "active"}
    if status == "past_due" and grace_until is not None and grace_until > now:
        base_is_active = True
    if valid_until is not None and valid_until <= now:
        base_is_active = False

    capabilities: set[str] = set()
    if base_is_active:
        cursor.execute(
            """
            SELECT entitlement.capability_code
            FROM seller.plan_entitlements AS entitlement
            WHERE entitlement.plan_id=%s AND entitlement.enabled=true
            """,
            (int(row[0]),),
        )
        capabilities.update(str(item[0]) for item in cursor.fetchall())

    cursor.execute(
        """
        SELECT capability_code, enabled
        FROM seller.workspace_entitlement_overrides
        WHERE workspace_id=%s AND (expires_at IS NULL OR expires_at>now())
        """,
        (workspace_id,),
    )
    for capability_code, enabled in cursor.fetchall():
        if bool(enabled):
            capabilities.add(str(capability_code))
        else:
            capabilities.discard(str(capability_code))

    return WorkspaceAccess(
        plan_code=str(row[1]),
        plan_name=str(row[2]),
        subscription_status=status,
        capabilities=frozenset(capabilities),
        revision=int(row[6]),
    )


def workspace_allows(cursor, workspace_id: int, capability: str) -> bool:
    return read_workspace_access(cursor, workspace_id).allows(capability)
