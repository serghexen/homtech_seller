"""Цена из готового снимка CRM: дата успешного расчёта не равна дате попытки."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def price_time(value):
    # Дата без часового пояса не позволяет достоверно показать время проверки.
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp if stamp.tzinfo and stamp <= datetime.now(timezone.utc) else None
    except (TypeError, ValueError):
        return None


def cached_supplier_price(snapshot, service_id, nominal_id):
    # Совпадение проверяем по услуге и номиналу; старый контракт допускает отсутствие цены.
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1 or not isinstance(snapshot.get("items"), list):
        raise ValueError("Invalid supplier snapshot")
    result = dict(service_id=service_id, nominal_id=nominal_id, amount=None,
                  currency="RUB", source="crm", checked_at=None, last_attempt_at=None,
                  warning="В снимке CRM нет подтверждённой цены для этой связки.")
    matches = [item for item in snapshot["items"] if isinstance(item, dict)
               and str(item.get("service_id")) == str(service_id)
               and str(item.get("nominal_id", "")) == nominal_id]
    if len(matches) > 1:
        raise ValueError("Duplicate supplier price")
    if not matches:
        return result
    item = matches[0]
    checked_at = price_time(item.get("price_updated_at"))
    try:
        amount = Decimal(str(item.get("price")))
    except InvalidOperation:
        return result
    if not amount.is_finite() or amount <= 0 or not checked_at or item.get("currency") not in {"RUB", "RUR"}:
        return result
    warnings = []
    if snapshot.get("catalog_error"):
        warnings.append("Последнее обновление каталога CRM завершилось ошибкой.")
    if item.get("price_error"):
        warnings.append("Последняя проверка цены в CRM завершилась ошибкой; показана последняя успешная цена.")
    if item.get("status") != "active":
        warnings.append("Доступность этого номинала в CRM не подтверждена.")
    result.update(amount=str(amount), checked_at=checked_at,
                  last_attempt_at=price_time(item.get("price_checked_at")), warning=" ".join(warnings))
    return result
