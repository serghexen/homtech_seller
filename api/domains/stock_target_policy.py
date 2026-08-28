"""Единое правило выбора локального источника публикуемого остатка."""

from __future__ import annotations

from typing import Literal


StockTargetSource = Literal["manual", "pool"]


def stock_target_source(*, supplier_issue_enabled: bool, pool_issue_enabled: bool) -> StockTargetSource:
    """Пул управляет остатком только когда он первый реально доступный способ выдачи."""

    if not supplier_issue_enabled and pool_issue_enabled:
        return "pool"
    return "manual"


def stock_target_base(
    *,
    manual_stock: int | None,
    supplier_issue_enabled: bool,
    pool_issue_enabled: bool,
    pool_free_count: int,
) -> int | None:
    """Возвращает значение до применения маркетплейсных дневных ограничений."""

    if stock_target_source(
        supplier_issue_enabled=supplier_issue_enabled,
        pool_issue_enabled=pool_issue_enabled,
    ) == "pool":
        return max(0, int(pool_free_count or 0))
    if manual_stock is None:
        return None
    return max(0, int(manual_stock))
