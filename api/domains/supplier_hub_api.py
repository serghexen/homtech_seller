"""Диагностический API связи Seller с Supplier Hub."""

from __future__ import annotations

from typing import Any, Callable
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from domains.supplier_hub_client import (
    SupplierHubClient,
    SupplierHubError,
    load_supplier_hub_settings,
    supplier_hub_status,
)
from domains.connection_entitlements import SUPPLIER_MAPPING_MANAGE, connection_allows
from domains.supplier_price_snapshot import cached_supplier_price


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
    connection_id: int = Field(gt=0)
    service_id: int = Field(gt=0)
    nominal_id: str = Field(default="", max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)


class SupplierHubQuoteOut(BaseModel):
    service_id: int
    amount: str
    currency: str = "RUB"
    provider_status: int | None = None
    provider_message: str = ""
    source: str = "provider"
    checked_at: datetime


class SupplierHubCachedPriceOut(BaseModel):
    service_id: int
    nominal_id: str
    amount: str | None = None
    currency: str = "RUB"
    source: str = "crm"
    checked_at: datetime | None = None
    last_attempt_at: datetime | None = None
    warning: str = ""


def mount_supplier_hub_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., Any],
    user_with_workspace: Callable,
) -> None:
    def require_supplier_mapping_access(user: Any, connection_id: int) -> None:
        with psycopg.connect(database_url()) as connection:
            seller_user = user_with_workspace(connection, user.user_id)
            if not seller_user:
                raise HTTPException(status_code=401, detail="Рабочая область недоступна")
            with connection.cursor() as cursor:
                allowed = connection_allows(
                    cursor, seller_user.workspace_id, connection_id, SUPPLIER_MAPPING_MANAGE,
                )
        if not allowed:
            raise HTTPException(status_code=403, detail="Настройка Supplier Hub доступна на тарифе Pro")

    @app.get("/integrations/supplier-hub/status", response_model=SupplierHubStatusOut)
    def read_supplier_hub_status(_user: Any = Depends(current_user)) -> SupplierHubStatusOut:
        # Не возвращает URL и ключ; недоступность Hub не делает весь Seller неработоспособным.
        return SupplierHubStatusOut(**supplier_hub_status())

    @app.get("/integrations/supplier-hub/services", response_model=SupplierHubServicesOut)
    def read_supplier_hub_services(
        connection_id: int = Query(gt=0),
        user: Any = Depends(current_user),
    ) -> SupplierHubServicesOut:
        require_supplier_mapping_access(user, connection_id)
        try:
            items = SupplierHubClient(load_supplier_hub_settings()).services()
        except SupplierHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return SupplierHubServicesOut(items=items)

    @app.get("/integrations/supplier-hub/cached-price", response_model=SupplierHubCachedPriceOut)
    def read_supplier_cached_price(
        connection_id: int = Query(gt=0),
        service_id: int = Query(gt=0),
        nominal_id: str = Query(default="", max_length=128),
        user: Any = Depends(current_user),
    ) -> SupplierHubCachedPriceOut:
        # Читаем готовый снимок CRM после проверки магазина; поставщика не опрашиваем.
        require_supplier_mapping_access(user, connection_id)
        try:
            snapshot = SupplierHubClient(load_supplier_hub_settings()).stock_snapshot()
            result = cached_supplier_price(snapshot, service_id, nominal_id)
        except (SupplierHubError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Не удалось получить цену из снимка CRM") from exc
        return SupplierHubCachedPriceOut(**result)

    @app.post("/integrations/supplier-hub/quote", response_model=SupplierHubQuoteOut)
    def read_supplier_hub_quote(
        payload: SupplierHubQuoteIn,
        user: Any = Depends(current_user),
    ) -> SupplierHubQuoteOut:
        # calculate/quote не создаёт покупку и доступен при выключенном purchase-флаге Hub.
        require_supplier_mapping_access(user, payload.connection_id)
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
            checked_at=datetime.now(timezone.utc),
        )
