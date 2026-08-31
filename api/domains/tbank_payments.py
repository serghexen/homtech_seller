"""Пополнение общего баланса Seller через динамический QR СБП Т-Банка."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from domains.local_auth import AuthenticatedUser


FINAL_FAILURE_STATUSES = {"REJECTED", "REVERSED", "CANCELED", "DEADLINE_EXPIRED"}
FINAL_TOPUP_STATES = {"confirmed", "rejected", "expired", "cancelled", "failed"}
RECEIPT_TAXATIONS = {"osn", "usn_income", "usn_income_outcome", "esn", "patent"}
RECEIPT_TAXES = {
    "none", "vat0", "vat5", "vat7", "vat10", "vat22", "vat105", "vat107", "vat110", "vat122",
}


class TBankError(RuntimeError):
    """Ошибка ответа эквайринга с признаком неопределённого сетевого результата."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


@dataclass(frozen=True)
class TBankSettings:
    base_url: str
    terminal_key: str
    password: str
    notification_url: str
    success_url: str
    fail_url: str
    timeout_seconds: int


class WorkspaceBalanceOut(BaseModel):
    available_amount: int
    reserved_amount: int
    currency: str = "RUB"
    topups_enabled: bool
    demo_mode: bool
    min_topup_amount: int
    max_topup_amount: int


class WorkspaceTopupCreateIn(BaseModel):
    amount: int = Field(description="Сумма в копейках")


class WorkspaceTopupDemoIn(BaseModel):
    outcome: Literal["success", "rejected", "deadline_expired"]


class WorkspaceTopupOut(BaseModel):
    id: UUID
    order_id: str
    amount: int
    currency: str
    state: str
    provider_status: str
    qr_data_url: str
    expires_at: datetime | None
    confirmed_at: datetime | None
    created_at: datetime


def _bool_env(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def topups_enabled() -> bool:
    return _bool_env("SELLER_TBANK_TOPUPS_ENABLED")


def demo_mode() -> bool:
    return _bool_env("SELLER_TBANK_DEMO_MODE", "true")


def min_topup_amount() -> int:
    return max(1_000, int(os.getenv("SELLER_TBANK_TOPUP_MIN_AMOUNT", "10000")))


def max_topup_amount() -> int:
    return max(min_topup_amount(), int(os.getenv("SELLER_TBANK_TOPUP_MAX_AMOUNT", "10000000")))


def tbank_settings() -> TBankSettings:
    settings = TBankSettings(
        base_url=str(os.getenv("TBANK_BASE_URL", "https://securepay.tinkoff.ru/v2")).strip().rstrip("/"),
        terminal_key=str(os.getenv("TBANK_TERMINAL_KEY", "")).strip(),
        password=str(os.getenv("TBANK_PASSWORD", "")).strip(),
        notification_url=str(
            os.getenv("TBANK_NOTIFICATION_URL", "https://seller.homtech.app/api/payments/tbank/notifications")
        ).strip(),
        success_url=str(os.getenv("TBANK_SUCCESS_URL", "https://seller.homtech.app/")).strip(),
        fail_url=str(os.getenv("TBANK_FAIL_URL", "https://seller.homtech.app/")).strip(),
        timeout_seconds=max(3, min(int(os.getenv("TBANK_REQUEST_TIMEOUT_SECONDS", "15")), 60)),
    )
    if not settings.terminal_key or not settings.password:
        raise TBankError("Не заданы TBANK_TERMINAL_KEY и TBANK_PASSWORD")
    return settings


def topup_receipt(*, amount: int) -> dict[str, Any]:
    """Формирует чек ФФД 1.2 для услуги пополнения баланса."""
    email = str(os.getenv("TBANK_RECEIPT_EMAIL", "")).strip().lower()
    taxation = str(os.getenv("TBANK_RECEIPT_TAXATION", "")).strip().lower()
    tax = str(os.getenv("TBANK_RECEIPT_TAX", "")).strip().lower()
    if not email or len(email) > 64 or "@" not in email:
        raise TBankError("Не задан корректный TBANK_RECEIPT_EMAIL")
    if taxation not in RECEIPT_TAXATIONS:
        raise TBankError("Не задан корректный TBANK_RECEIPT_TAXATION")
    if tax not in RECEIPT_TAXES:
        raise TBankError("Не задан корректный TBANK_RECEIPT_TAX")
    return {
        "FfdVersion": "1.2",
        "Email": email,
        "Taxation": taxation,
        "Items": [
            {
                "Name": "Услуга пополнения баланса HomTech Seller",
                "Price": amount,
                "Quantity": 1,
                "Amount": amount,
                "Tax": tax,
                "PaymentMethod": "full_payment",
                "PaymentObject": "service",
                "MeasurementUnit": "шт",
            }
        ],
    }


def _token_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def make_token(payload: dict[str, Any], password: str) -> str:
    """Считает SHA-256 только по корневым скалярным полям согласно протоколу Т-Банка."""
    values: dict[str, Any] = {"Password": password}
    for key, value in payload.items():
        if key in {"Token", "Password"} or value is None or isinstance(value, (dict, list, tuple)):
            continue
        values[key] = value
    source = "".join(_token_value(values[key]) for key in sorted(values))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def notification_token_is_valid(payload: dict[str, Any], password: str) -> bool:
    supplied = str(payload.get("Token") or "")
    return bool(supplied) and hmac.compare_digest(supplied.lower(), make_token(payload, password).lower())


def _ssl_context() -> ssl.SSLContext:
    ca_bundle = str(os.getenv("TBANK_CA_BUNDLE", "")).strip()
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class TBankClient:
    def __init__(self, settings: TBankSettings) -> None:
        self.settings = settings

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {**payload, "TerminalKey": self.settings.terminal_key}
        body["Token"] = make_token(body, self.settings.password)
        request = urllib.request.Request(
            f"{self.settings.base_url}/{method}",
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.timeout_seconds, context=_ssl_context()
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                error_payload = {}
            message = str(error_payload.get("Message") or error_payload.get("Details") or f"HTTP {exc.code}")
            raise TBankError(message, uncertain=int(exc.code) in {408, 429} or int(exc.code) >= 500) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise TBankError("Т-Банк временно не ответил", uncertain=True) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise TBankError("Т-Банк вернул некорректный ответ", uncertain=True) from exc
        if not isinstance(result, dict):
            raise TBankError("Т-Банк вернул некорректный ответ", uncertain=True)
        if not bool(result.get("Success")):
            code = str(result.get("ErrorCode") or "")
            message = str(result.get("Message") or result.get("Details") or "Платёж отклонён")
            raise TBankError(f"{message} ({code})" if code else message)
        return result

    def init(
        self, *, order_id: str, amount: int, expires_at: datetime, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        return self.call(
            "Init",
            {
                "Amount": amount,
                "OrderId": order_id,
                "Description": "Пополнение баланса HomTech Seller",
                "PayType": "O",
                "NotificationURL": self.settings.notification_url,
                "SuccessURL": self.settings.success_url,
                "FailURL": self.settings.fail_url,
                "RedirectDueDate": expires_at.isoformat(timespec="seconds"),
                "Receipt": receipt,
            },
        )

    def get_qr(self, payment_id: str) -> dict[str, Any]:
        return self.call("GetQr", {"PaymentId": payment_id, "DataType": "IMAGE", "PaymentMethod": "SBP"})

    def get_state(self, payment_id: str) -> dict[str, Any]:
        return self.call("GetState", {"PaymentId": payment_id})

    def simulate_sbp(self, payment_id: str, outcome: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"PaymentId": payment_id}
        if outcome == "rejected":
            payload["IsRejected"] = True
        elif outcome == "deadline_expired":
            payload["IsDeadlineExpired"] = True
        return self.call("SbpPayTest", payload)


def qr_data_url(value: Any) -> str:
    """Принимает SVG/base64 от GetQr и возвращает только безопасный URL для элемента img."""
    raw = str(value or "").strip()
    if raw.startswith("data:image/svg+xml;base64,"):
        encoded = raw.split(",", 1)[1]
        try:
            svg = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise TBankError("Т-Банк вернул повреждённый QR-код") from exc
    elif raw.startswith("<svg") or raw.startswith("<?xml"):
        svg = raw
    else:
        try:
            svg = base64.b64decode(raw, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise TBankError("Т-Банк вернул QR-код неизвестного формата") from exc
    lowered = svg.lower()
    if "<svg" not in lowered or any(marker in lowered for marker in ("<script", "javascript:", "onload=")):
        raise TBankError("Т-Банк вернул небезопасное изображение QR-кода")
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def provider_state(status: str) -> str:
    normalized = str(status or "").strip().upper()
    if normalized == "CONFIRMED":
        return "confirmed"
    if normalized == "DEADLINE_EXPIRED":
        return "expired"
    if normalized == "CANCELED":
        return "cancelled"
    if normalized in FINAL_FAILURE_STATUSES:
        return "rejected"
    return "pending"


def should_apply_provider_state(current_state: str, incoming_state: str) -> bool:
    """Не позволяет запоздалому промежуточному статусу откатить завершённый платёж."""
    current = str(current_state or "").strip().lower()
    incoming = str(incoming_state or "").strip().lower()
    if current == "confirmed":
        return incoming == "confirmed"
    if incoming == "confirmed":
        return True
    if current in FINAL_TOPUP_STATES:
        return incoming == current
    return True


def _topup_out(row) -> WorkspaceTopupOut:
    return WorkspaceTopupOut(
        id=row[0], order_id=str(row[1]), amount=int(row[2]), currency=str(row[3]), state=str(row[4]),
        provider_status=str(row[5] or ""), qr_data_url=str(row[6] or ""), expires_at=row[7],
        confirmed_at=row[8], created_at=row[9],
    )


TOPUP_SELECT = """
SELECT public_id, order_id, amount, currency, state, provider_status, qr_data_url,
       expires_at, confirmed_at, created_at
FROM seller.workspace_balance_topups
"""


def _credit_confirmed_topup(cursor, *, topup_id: int, workspace_id: int, payment_id: str, amount: int) -> None:
    """Начисляет деньги ровно один раз, даже если один webhook доставлен повторно."""
    cursor.execute(
        "INSERT INTO seller.workspace_balance_accounts(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (workspace_id,),
    )
    cursor.execute(
        """
        INSERT INTO seller.workspace_balance_ledger(
          workspace_id, topup_id, entry_type, amount, business_key, metadata
        ) VALUES (%s, %s, 'topup', %s, %s, jsonb_build_object('provider', 'tbank', 'payment_id', %s::text))
        ON CONFLICT (business_key) DO NOTHING
        RETURNING id
        """,
        (workspace_id, topup_id, amount, f"tbank:confirmed:{payment_id}", payment_id),
    )
    if cursor.fetchone():
        cursor.execute(
            """
            UPDATE seller.workspace_balance_accounts
               SET available_amount=available_amount+%s, updated_at=now()
             WHERE workspace_id=%s
            """,
            (amount, workspace_id),
        )


@dataclass(frozen=True)
class ClaimedTopup:
    id: int
    workspace_id: int
    payment_id: str
    amount: int
    lock_token: UUID
    attempt_count: int


class TBankReconciliationProcessor:
    """Сверяет незавершённые платежи общей долговечной очередью worker-а."""

    def __init__(self, *, database_url: Callable[[], str], psycopg, client_factory=TBankClient) -> None:
        self._database_url = database_url
        self._psycopg = psycopg
        self._client_factory = client_factory

    def process_pending(self, limit: int = 5) -> int:
        if not topups_enabled():
            return 0
        settings = tbank_settings()
        processed = 0
        for _ in range(max(1, min(int(limit), 50))):
            claimed = self._claim()
            if claimed is None:
                break
            processed += 1
            try:
                result = self._client_factory(settings).get_state(claimed.payment_id)
                response_payment_id = str(result.get("PaymentId") or claimed.payment_id)
                response_amount = int(result.get("Amount"))
                if response_payment_id != claimed.payment_id or response_amount != claimed.amount:
                    raise TBankError("GetState вернул платёж с несовпадающими реквизитами")
                self._finish(claimed, str(result.get("Status") or ""))
            except Exception as exc:
                self._retry(claimed, str(exc))
        return processed

    def _claim(self) -> ClaimedTopup | None:
        token = uuid4()
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, workspace_id, provider_payment_id, amount, reconcile_attempt_count
                    FROM seller.workspace_balance_topups
                    WHERE state='pending' AND provider_payment_id IS NOT NULL
                      AND next_reconcile_at<=now()
                      AND (reconcile_locked_until IS NULL OR reconcile_locked_until<now())
                    ORDER BY next_reconcile_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if not row:
                    return None
                cursor.execute(
                    """
                    UPDATE seller.workspace_balance_topups
                       SET reconcile_lock_token=%s, reconcile_locked_until=now()+interval '90 seconds',
                           reconcile_attempt_count=reconcile_attempt_count+1, updated_at=now()
                     WHERE id=%s
                    """,
                    (token, row[0]),
                )
        return ClaimedTopup(
            id=int(row[0]), workspace_id=int(row[1]), payment_id=str(row[2]), amount=int(row[3]),
            lock_token=token, attempt_count=int(row[4]) + 1,
        )

    def _finish(self, claimed: ClaimedTopup, status: str) -> None:
        state = provider_state(status)
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state FROM seller.workspace_balance_topups WHERE id=%s AND reconcile_lock_token=%s FOR UPDATE",
                    (claimed.id, claimed.lock_token),
                )
                current_row = cursor.fetchone()
                if not current_row:
                    return
                if not should_apply_provider_state(str(current_row[0]), state):
                    cursor.execute(
                        """
                        UPDATE seller.workspace_balance_topups
                           SET next_reconcile_at=NULL, reconcile_lock_token=NULL,
                               reconcile_locked_until=NULL, last_error='', updated_at=now()
                         WHERE id=%s
                        """,
                        (claimed.id,),
                    )
                    return
                if state == "confirmed":
                    _credit_confirmed_topup(
                        cursor, topup_id=claimed.id, workspace_id=claimed.workspace_id,
                        payment_id=claimed.payment_id, amount=claimed.amount,
                    )
                cursor.execute(
                    """
                    UPDATE seller.workspace_balance_topups
                       SET provider_status=%s, state=%s,
                           confirmed_at=CASE WHEN %s='confirmed' THEN COALESCE(confirmed_at, now()) ELSE confirmed_at END,
                           next_reconcile_at=CASE WHEN %s='pending' THEN now()+interval '1 minute' ELSE NULL END,
                           reconcile_lock_token=NULL, reconcile_locked_until=NULL, last_error='', updated_at=now()
                     WHERE id=%s
                    """,
                    (status.upper(), state, state, state, claimed.id),
                )

    def _retry(self, claimed: ClaimedTopup, message: str) -> None:
        delay = min(15 * (2 ** max(0, min(claimed.attempt_count, 8) - 1)), 900)
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.workspace_balance_topups
                       SET next_reconcile_at=now()+(%s*interval '1 second'), reconcile_lock_token=NULL,
                           reconcile_locked_until=NULL, last_error=%s, updated_at=now()
                     WHERE id=%s AND reconcile_lock_token=%s AND state='pending'
                    """,
                    (delay, message[:500], claimed.id, claimed.lock_token),
                )


def build_tbank_reconciliation_processor(*, database_url: Callable[[], str], psycopg) -> TBankReconciliationProcessor:
    return TBankReconciliationProcessor(database_url=database_url, psycopg=psycopg)


def mount_tbank_payment_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Подключает workspace-scoped API баланса и публичный подписанный webhook."""

    def workspace_for_user(connection, user: AuthenticatedUser):
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        return seller_user

    def require_topup_role(seller_user) -> None:
        if seller_user.role_code not in {"owner", "operator"}:
            raise HTTPException(status_code=403, detail="Пополнять баланс может владелец или оператор")

    @app.get("/billing/balance", response_model=WorkspaceBalanceOut)
    def get_balance(user: AuthenticatedUser = Depends(current_user)) -> WorkspaceBalanceOut:
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO seller.workspace_balance_accounts(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (seller_user.workspace_id,),
                )
                cursor.execute(
                    "SELECT available_amount, reserved_amount, currency FROM seller.workspace_balance_accounts WHERE workspace_id=%s",
                    (seller_user.workspace_id,),
                )
                row = cursor.fetchone()
        configured = False
        try:
            tbank_settings()
            configured = True
        except TBankError:
            pass
        return WorkspaceBalanceOut(
            available_amount=int(row[0]), reserved_amount=int(row[1]), currency=str(row[2]),
            topups_enabled=topups_enabled() and configured, demo_mode=demo_mode(),
            min_topup_amount=min_topup_amount(), max_topup_amount=max_topup_amount(),
        )

    @app.post("/billing/topups", response_model=WorkspaceTopupOut, status_code=201)
    def create_topup(payload: WorkspaceTopupCreateIn, user: AuthenticatedUser = Depends(current_user)) -> WorkspaceTopupOut:
        if not topups_enabled():
            raise HTTPException(status_code=503, detail="Пополнение баланса пока выключено")
        if payload.amount < min_topup_amount() or payload.amount > max_topup_amount():
            raise HTTPException(
                status_code=400,
                detail=f"Введите сумму от {min_topup_amount() // 100} до {max_topup_amount() // 100} ₽",
            )
        try:
            settings = tbank_settings()
            receipt = topup_receipt(amount=payload.amount)
        except TBankError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        order_id = f"seller_{uuid4().hex}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            require_topup_role(seller_user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO seller.workspace_balance_topups(
                      workspace_id, created_by_user_id, terminal_key, order_id, amount,
                      state, expires_at, next_reconcile_at
                    ) VALUES (%s, %s, %s, %s, %s, 'init_pending', %s, %s)
                    RETURNING id, public_id
                    """,
                    (seller_user.workspace_id, seller_user.id, settings.terminal_key, order_id, payload.amount,
                     expires_at, datetime.now(timezone.utc) + timedelta(minutes=1)),
                )
                topup_id, public_id = cursor.fetchone()
        client = TBankClient(settings)
        try:
            init_result = client.init(
                order_id=order_id, amount=payload.amount, expires_at=expires_at, receipt=receipt
            )
            payment_id = str(init_result.get("PaymentId") or "").strip()
            if not payment_id:
                raise TBankError("Т-Банк не вернул идентификатор платежа", uncertain=True)
        except TBankError as exc:
            with psycopg.connect(database_url()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE seller.workspace_balance_topups
                           SET state=%s, last_error=%s, updated_at=now()
                         WHERE id=%s
                        """,
                        ("init_unknown" if exc.uncertain else "failed", str(exc)[:500], topup_id),
                    )
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        provider_status = str(init_result.get("Status") or "NEW")
        with psycopg.connect(database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.workspace_balance_topups
                       SET provider_payment_id=%s, provider_status=%s, state='pending', updated_at=now()
                     WHERE id=%s AND state='init_pending'
                    """,
                    (payment_id, provider_status, topup_id),
                )
        try:
            qr_result = client.get_qr(payment_id)
            qr_url = qr_data_url(qr_result.get("Data"))
        except TBankError as exc:
            with psycopg.connect(database_url()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE seller.workspace_balance_topups SET last_error=%s, updated_at=now() WHERE id=%s",
                        (str(exc)[:500], topup_id),
                    )
            raise HTTPException(status_code=502, detail="Платёж создан, но QR-код пока недоступен. Повторите открытие формы.") from exc
        with psycopg.connect(database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE seller.workspace_balance_topups SET qr_data_url=%s, last_error='', updated_at=now() WHERE id=%s",
                    (qr_url, topup_id),
                )
                cursor.execute(f"{TOPUP_SELECT} WHERE public_id=%s AND workspace_id=%s", (public_id, seller_user.workspace_id))
                row = cursor.fetchone()
        return _topup_out(row)

    @app.get("/billing/topups/{topup_id}", response_model=WorkspaceTopupOut)
    def get_topup(topup_id: UUID, user: AuthenticatedUser = Depends(current_user)) -> WorkspaceTopupOut:
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            with connection.cursor() as cursor:
                cursor.execute(f"{TOPUP_SELECT} WHERE public_id=%s AND workspace_id=%s", (topup_id, seller_user.workspace_id))
                row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Пополнение не найдено")
        return _topup_out(row)

    @app.post("/billing/topups/{topup_id}/demo", response_model=WorkspaceTopupOut)
    def simulate_demo_topup(
        topup_id: UUID,
        payload: WorkspaceTopupDemoIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> WorkspaceTopupOut:
        if not topups_enabled() or not demo_mode():
            raise HTTPException(status_code=404, detail="Демо-сценарий недоступен")
        try:
            settings = tbank_settings()
        except TBankError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if "DEMO" not in settings.terminal_key.upper():
            raise HTTPException(status_code=403, detail="SbpPayTest разрешён только для DEMO-терминала")
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            require_topup_role(seller_user)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT provider_payment_id, state
                    FROM seller.workspace_balance_topups
                    WHERE public_id=%s AND workspace_id=%s
                    """,
                    (topup_id, seller_user.workspace_id),
                )
                row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Пополнение не найдено")
        payment_id, state = str(row[0] or ""), str(row[1])
        if state != "pending" or not payment_id:
            raise HTTPException(status_code=409, detail="Платёж уже завершён или ещё не создан")
        try:
            TBankClient(settings).simulate_sbp(payment_id, payload.outcome)
        except TBankError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        with psycopg.connect(database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.workspace_balance_topups
                       SET next_reconcile_at=now(), last_error='', updated_at=now()
                     WHERE public_id=%s AND workspace_id=%s AND state='pending'
                    """,
                    (topup_id, seller_user.workspace_id),
                )
                cursor.execute(
                    f"{TOPUP_SELECT} WHERE public_id=%s AND workspace_id=%s",
                    (topup_id, seller_user.workspace_id),
                )
                result_row = cursor.fetchone()
        return _topup_out(result_row)

    @app.post("/payments/tbank/notifications", response_class=PlainTextResponse)
    async def tbank_notification(request: Request) -> PlainTextResponse:
        body = await request.body()
        max_body_bytes = max(1_024, min(int(os.getenv("TBANK_NOTIFICATION_MAX_BODY_BYTES", "65536")), 1_048_576))
        if len(body) > max_body_bytes:
            raise HTTPException(status_code=413, detail="Payload is too large")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid payload")
        try:
            settings = tbank_settings()
        except TBankError as exc:
            raise HTTPException(status_code=503, detail="Payment notifications are not configured") from exc
        if str(payload.get("TerminalKey") or "") != settings.terminal_key:
            raise HTTPException(status_code=403, detail="Unknown terminal")
        if not notification_token_is_valid(payload, settings.password):
            raise HTTPException(status_code=403, detail="Invalid token")
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        order_id = str(payload.get("OrderId") or "")
        payment_id = str(payload.get("PaymentId") or "")
        status = str(payload.get("Status") or "").upper()
        try:
            amount = int(payload.get("Amount"))
        except (TypeError, ValueError):
            amount = None
        with psycopg.connect(database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO seller.tbank_payment_events(
                      event_fingerprint, terminal_key, order_id, provider_payment_id,
                      provider_status, amount, signature_valid
                    ) VALUES (%s, %s, %s, %s, %s, %s, true)
                    ON CONFLICT (event_fingerprint) DO NOTHING
                    RETURNING id
                    """,
                    (fingerprint, settings.terminal_key, order_id, payment_id, status, amount),
                )
                event_row = cursor.fetchone()
                if not event_row:
                    return PlainTextResponse("OK")
                event_id = int(event_row[0])
                cursor.execute(
                    """
                    SELECT id, workspace_id, amount, provider_payment_id, state
                    FROM seller.workspace_balance_topups
                    WHERE order_id=%s AND terminal_key=%s
                    FOR UPDATE
                    """,
                    (order_id, settings.terminal_key),
                )
                topup = cursor.fetchone()
                if not topup:
                    cursor.execute(
                        "UPDATE seller.tbank_payment_events SET processing_state='ignored', processed_at=now(), last_error='unknown order' WHERE id=%s",
                        (event_id,),
                    )
                    return PlainTextResponse("OK")
                topup_db_id, workspace_id, expected_amount, known_payment_id, current_state = topup
                if not payment_id or str(known_payment_id or "") not in {"", payment_id} or amount != int(expected_amount):
                    cursor.execute(
                        "UPDATE seller.tbank_payment_events SET processing_state='failed', processed_at=now(), last_error='payment identity mismatch' WHERE id=%s",
                        (event_id,),
                    )
                    raise HTTPException(status_code=409, detail="Payment identity mismatch")
                state = provider_state(status)
                if should_apply_provider_state(str(current_state), state):
                    if state == "confirmed":
                        _credit_confirmed_topup(
                            cursor, topup_id=int(topup_db_id), workspace_id=int(workspace_id),
                            payment_id=payment_id, amount=int(expected_amount),
                        )
                    cursor.execute(
                        """
                        UPDATE seller.workspace_balance_topups
                           SET provider_payment_id=COALESCE(provider_payment_id, %s), provider_status=%s,
                               state=%s, confirmed_at=CASE WHEN %s='confirmed' THEN COALESCE(confirmed_at, now()) ELSE confirmed_at END,
                               next_reconcile_at=CASE WHEN %s='pending' THEN now()+interval '1 minute' ELSE NULL END,
                               last_error='', updated_at=now()
                         WHERE id=%s
                        """,
                        (payment_id, status, state, state, state, topup_db_id),
                    )
                cursor.execute(
                    "UPDATE seller.tbank_payment_events SET processing_state='processed', processed_at=now() WHERE id=%s",
                    (event_id,),
                )
        return PlainTextResponse("OK")
