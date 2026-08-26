export function parseKeyLines(value) {
  // Принимает по одному коду на строку, удаляет пустые строки и повторы до отправки в API.
  const seen = new Set()
  return String(value || '')
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter((item) => {
      if (!item || seen.has(item)) return false
      seen.add(item)
      return true
    })
}

export function keyCountLabel(value) {
  // Склоняет слово «ключ» для коротких подписей в карточке товара.
  const count = Math.max(0, Number(value) || 0)
  const mod100 = count % 100
  const mod10 = count % 10
  const word = mod100 >= 11 && mod100 <= 14 ? 'ключей' : mod10 === 1 ? 'ключ' : mod10 >= 2 && mod10 <= 4 ? 'ключа' : 'ключей'
  return `${count} ${word}`
}

export function keyOrderLabel(key) {
  // Предпочитает понятный номер заказа Seller, а для импортированной истории извлекает его из CRM-ссылки.
  const orderId = String(key?.issued_order_id || '').trim()
  if (orderId) return `Заказ ${orderId}`
  const reference = String(key?.issued_order_ref || '').trim()
  if (!reference) return '—'
  const parts = reference.split(':').filter(Boolean)
  return parts.length >= 3 ? `Заказ ${parts.at(-2)}` : reference
}
