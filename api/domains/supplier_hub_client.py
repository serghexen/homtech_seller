"""Безопасный read-only клиент внутреннего Supplier Hub."""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class SupplierHubError(RuntimeError):
    """Ошибка конфигурации или связи без раскрытия клиентского секрета."""

    def __init__(self, message: str, *, status_code: int | None = None, blocks_fallback: bool = True) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.blocks_fallback = blocks_fallback


@dataclass(frozen=True)
class SupplierHubSettings:
    base_url: str
    client_id: str
    client_key: str
    timeout_seconds: int
    fulfillment_enabled: bool

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.client_id and len(self.client_key) >= 32)


def _enabled(name: str) -> bool:
    return str(os.getenv(name, "false")).strip().lower() in {"1", "true", "yes"}


def load_supplier_hub_settings() -> SupplierHubSettings:
    return SupplierHubSettings(
        base_url=str(os.getenv("SUPPLIER_HUB_URL", "")).strip().rstrip("/"),
        client_id=str(os.getenv("SUPPLIER_HUB_CLIENT_ID", "seller")).strip(),
        client_key=str(os.getenv("SUPPLIER_HUB_CLIENT_KEY", "")).strip(),
        timeout_seconds=max(2, min(int(os.getenv("SUPPLIER_HUB_TIMEOUT_SECONDS", "10")), 60)),
        fulfillment_enabled=_enabled("SELLER_SUPPLIER_HUB_FULFILLMENT_ENABLED"),
    )


def _validate_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path not in {"", "/"}:
        raise SupplierHubError("Supplier Hub URL is invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SupplierHubError("Supplier Hub URL must not contain credentials or query parameters")
    if parsed.scheme == "https":
        return
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "host.docker.internal"}:
        return
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise SupplierHubError("Plain HTTP Supplier Hub URL must use a private address") from exc
    if not (address.is_private or address.is_loopback):
        raise SupplierHubError("Plain HTTP Supplier Hub URL must use a private address")


class SupplierHubClient:
    def __init__(self, settings: SupplierHubSettings):
        if not settings.base_url:
            raise SupplierHubError("Supplier Hub URL is not configured")
        _validate_base_url(settings.base_url)
        self.settings = settings

    def _request(
        self,
        path: str,
        *,
        authenticated: bool,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if authenticated:
            if not self.settings.configured:
                raise SupplierHubError("Supplier Hub client credentials are not configured")
            headers["X-Hub-Client"] = self.settings.client_id
            headers["X-Hub-Key"] = self.settings.client_key
        if request_id:
            headers["X-Request-ID"] = request_id
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.settings.base_url}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise SupplierHubError(
                    "Supplier Hub rejected client credentials", status_code=int(exc.code),
                ) from exc
            # 422 возникает до постановки покупки в очередь и допускает безопасный
            # fallback. Конфликт идемпотентности и любые серверные ошибки требуют
            # вмешательства или повторной сверки, а не следующего источника.
            raise SupplierHubError(
                f"Supplier Hub returned HTTP {exc.code}",
                status_code=int(exc.code),
                blocks_fallback=int(exc.code) != 422,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SupplierHubError("Supplier Hub is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SupplierHubError("Supplier Hub response is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupplierHubError("Supplier Hub returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SupplierHubError("Supplier Hub returned an unexpected response")
        return payload

    def _get(self, path: str, *, authenticated: bool) -> dict[str, Any]:
        return self._request(path, authenticated=authenticated)

    def live(self) -> dict[str, Any]:
        return self._get("/live", authenticated=False)

    def ready(self) -> dict[str, Any]:
        return self._get("/ready", authenticated=False)

    def services(self) -> list[dict[str, Any]]:
        payload = self._get("/v1/providers/interhub/services", authenticated=True)
        items = payload.get("items")
        if not isinstance(items, list):
            raise SupplierHubError("Supplier Hub returned an invalid service catalog")
        return [item for item in items if isinstance(item, dict)]

    def balance(self) -> dict[str, Any]:
        return self._get("/v1/providers/interhub/balance", authenticated=True)

    def observability_summary(self) -> dict[str, Any]:
        """Возвращает агрегаты покупок текущего consumer без раскрытия результатов."""
        return self._get("/v1/observability/summary", authenticated=True)

    def quote(self, *, service_id: int, nominal_id: str = "", params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_params = dict(params or {})
        if str(nominal_id or "").strip():
            request_params["nominal"] = str(nominal_id).strip()
        return self._request(
            "/v1/providers/interhub/quote",
            authenticated=True,
            method="POST",
            payload={"service_id": int(service_id), "account": "", "params": request_params},
        )

    def create_purchase(
        self,
        *,
        idempotency_key: str,
        service_id: int,
        max_amount: str,
        nominal_id: str = "",
        params: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        if not self.settings.fulfillment_enabled:
            raise SupplierHubError("Supplier Hub fulfillment is disabled")
        request_params = dict(params or {})
        if str(nominal_id or "").strip():
            request_params["nominal"] = str(nominal_id).strip()
        return self._request(
            "/v1/purchases",
            authenticated=True,
            method="POST",
            request_id=request_id,
            payload={
                "idempotency_key": str(idempotency_key),
                "provider_code": "interhub",
                "service_id": int(service_id),
                "max_amount": str(max_amount),
                "account": "",
                "params": request_params,
                "quantity": 1,
            },
        )

    def purchase(self, purchase_id: str) -> dict[str, Any]:
        return self._get(f"/v1/purchases/{purchase_id}", authenticated=True)

    def purchase_result(self, purchase_id: str) -> str:
        payload = self._get(f"/v1/purchases/{purchase_id}/result", authenticated=True)
        value = str(payload.get("value") or "").strip()
        if not value:
            raise SupplierHubError("Supplier Hub returned an empty purchase result")
        return value


def supplier_hub_status() -> dict[str, Any]:
    settings = load_supplier_hub_settings()
    status: dict[str, Any] = {
        "configured": settings.configured,
        "fulfillment_enabled": settings.fulfillment_enabled,
        "reachable": False,
        "hub_ready": False,
        "hub_version": "",
        "hub_purchases_enabled": False,
        "message": "Supplier Hub URL is not configured",
    }
    if not settings.base_url:
        return status
    try:
        client = SupplierHubClient(settings)
        live = client.live()
        ready = client.ready()
    except SupplierHubError as exc:
        status["message"] = str(exc)
        return status
    status.update(
        reachable=live.get("status") == "ok",
        hub_ready=ready.get("status") == "ready",
        hub_version=str(live.get("version") or ""),
        hub_purchases_enabled=bool(ready.get("purchases_enabled")),
        message="Supplier Hub is available",
    )
    return status
