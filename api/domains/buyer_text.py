"""Нормализация текстов, которые Seller передаёт покупателю маркетплейса."""

from __future__ import annotations


def normalize_buyer_text(value: object) -> str:
    """Превращает legacy ``\\n`` из CRM в реальные переносы и выравнивает CRLF."""

    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Старый экспорт CRM сохранял управляющие последовательности как обычный текст.
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    return text.strip()
