"""Единый fail-closed допуск заказа к подготовке и внешней выдаче."""

from __future__ import annotations


YANDEX_DIGITAL_CODE_TYPES = frozenset({"EMAIL", "ACTIVATION_CODE"})


def marketplace_order_allows_fulfillment(
    *,
    provider_code: str,
    normalized_status: str,
    provider_status: str,
    delivery_type: str,
    digital_goods_type: str = "",
) -> bool:
    """Возвращает True только для состояния, допускающего необратимую выдачу.

    Общий ``normalized_status`` нужен интерфейсу, но для Яндекса он намеренно
    объединяет несколько предшествующих состояний. Поэтому внешняя выдача
    опирается ещё и на точный статус провайдера и тип цифрового сценария.
    Неизвестные и будущие значения блокируются по умолчанию.
    """

    provider = str(provider_code or "").strip().lower()
    if str(normalized_status or "").strip().lower() != "processing":
        return False
    if str(delivery_type or "").strip().upper() != "DIGITAL":
        return False
    if provider == "yandex_market":
        return (
            str(provider_status or "").strip().upper() == "PROCESSING"
            and str(digital_goods_type or "").strip().upper() in YANDEX_DIGITAL_CODE_TYPES
        )
    return provider == "ozon"
