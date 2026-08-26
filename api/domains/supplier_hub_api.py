"""Диагностический API связи Seller с Supplier Hub."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from domains.supplier_hub_client import (
    SupplierHubClient,
    SupplierHubError,
    load_supplier_hub_settings,
    supplier_hub_status,
)


class SupplierHubStatusOut(BaseModel):
    configured: bool
    fulfillment_enabled: bool
    reachable: bool
    hub_ready: bool
    hub_version: str
    hub_purchases_enabled: bool
    message: str


class SupplierHubServicesOut(BaseModel):
    items: list[dict[str, Any]]


class SupplierHubQuoteIn(BaseModel):
    service_id: int = Field(gt=0)
    nominal_id: str = Field(default="", max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)


class SupplierHubQuoteOut(BaseModel):
    service_id: int
    amount: str
    currency: str = "RUB"
    provider_status: int | None = None
    provider_message: str = ""


def mount_supplier_hub_routes(app: FastAPI, *, current_user: Callable[..., Any]) -> None:
    @app.get("/integrations/supplier-hub/status", response_model=SupplierHubStatusOut)
    def read_supplier_hub_status(_user: Any = Depends(current_user)) -> SupplierHubStatusOut:
        # Не возвращает URL и ключ; недоступность Hub не делает весь Seller неработоспособным.
        return SupplierHubStatusOut(**supplier_hub_status())

    @app.get("/integrations/supplier-hub/services", response_model=SupplierHubServicesOut)
    def read_supplier_hub_services(_user: Any = Depends(current_user)) -> SupplierHubServicesOut:
        try:
            items = SupplierHubClient(load_supplier_hub_settings()).services()
        except SupplierHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return SupplierHubServicesOut(items=items)

    @app.post("/integrations/supplier-hub/quote", response_model=SupplierHubQuoteOut)
    def read_supplier_hub_quote(
        payload: SupplierHubQuoteIn,
        _user: Any = Depends(current_user),
    ) -> SupplierHubQuoteOut:
        # calculate/quote не создаёт покупку и доступен при выключенном purchase-флаге Hub.
        try:
            result = SupplierHubClient(load_supplier_hub_settings()).quote(
                service_id=payload.service_id,
                nominal_id=payload.nominal_id,
                params=payload.params,
            )
        except SupplierHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not bool(result.get("success")) or not result.get("fixed_amount"):
            raise HTTPException(
                status_code=422,
                detail=str(result.get("message") or "Поставщик не вернул доступную цену"),
            )
        return SupplierHubQuoteOut(
            service_id=payload.service_id,
            amount=str(result.get("fixed_amount") or ""),
            currency="RUB",
            provider_status=result.get("status"),
            provider_message=str(result.get("message") or ""),
        )
