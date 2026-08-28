"""Чистые правила владения выдачей между автоматикой и оператором."""

from __future__ import annotations

import os


def automatic_fulfillment_resolver_enabled() -> bool:
    return str(os.getenv("SELLER_FULFILLMENT_RESOLVER_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes",
    }


def automation_controls_fulfillment(
    *,
    fulfillment_status: str,
    handling_mode: str,
    outbound_state: str,
    resolver_enabled: bool,
    resolver_active: bool,
    supplier_attempt_active: bool,
) -> bool:
    return bool(
        resolver_active
        or supplier_attempt_active
        or (
            handling_mode == "automatic"
            and (
                fulfillment_status in {"pending", "supplier_required", "sending", "submitted"}
                or (fulfillment_status == "reserved" and outbound_state in {"", "queued", "preparing"})
            )
        )
        or (
            resolver_enabled
            and handling_mode == "unassigned"
            and fulfillment_status in {"not_prepared", "pending", "supplier_required"}
        )
    )


def manual_preparation_stage_ready(
    *, fulfillment_status: str, handling_mode: str, resolver_enabled: bool, automation_in_progress: bool,
) -> bool:
    if automation_in_progress:
        return False
    if fulfillment_status == "manual_required" and handling_mode != "automatic":
        return True
    return bool(
        not resolver_enabled
        and handling_mode != "automatic"
        and fulfillment_status in {"not_prepared", "pending", "supplier_required"}
    )
