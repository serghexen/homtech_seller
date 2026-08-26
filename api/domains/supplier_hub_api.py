"""Диагностический API связи Seller с Supplier Hub."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from domains.supplier_hub_client import supplier_hub_status


class SupplierHubStatusOut(BaseModel):
    configured: bool
    fulfillment_enabled: bool
    reachable: bool
    hub_ready: bool
    hub_version: str
    hub_purchases_enabled: bool
    message: str


def mount_supplier_hub_routes(app: FastAPI, *, current_user: Callable[..., Any]) -> None:
    @app.get("/integrations/supplier-hub/status", response_model=SupplierHubStatusOut)
    def read_supplier_hub_status(_user: Any = Depends(current_user)) -> SupplierHubStatusOut:
        # Не возвращает URL и ключ; недоступность Hub не делает весь Seller неработоспособным.
        return SupplierHubStatusOut(**supplier_hub_status())
