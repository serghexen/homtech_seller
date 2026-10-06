// Источник и время относятся к показанной сумме, а не к открытию карточки или остатку.
export function supplierPriceDetails(item, mapping, quote, now = Date.now()) {
  const matches = (service, nominal) => Number(service) === Number(mapping.supplier_service_id)
    && String(nominal || '') === String(mapping.supplier_nominal_id || '')
  const selectedQuote = quote && matches(quote.service_id, quote.nominal_id) ? quote : null
  const positive = (amount) => Number.isFinite(Number(amount)) && Number(amount) > 0
  let price = selectedQuote && positive(selectedQuote.amount) ? selectedQuote : null
  if (!price && matches(item.supplier_service_id, item.supplier_nominal_id) && positive(item.supplier_quoted_amount)) {
    price = { amount: item.supplier_quoted_amount, checked_at: item.supplier_quoted_at, source: 'saved' }
  }
  const labels = { crm: 'Цена по данным CRM', provider: 'Цена по проверке поставщика', saved: 'Сохранённая цена связки' }
  const warning = selectedQuote?.warning || ''
  if (!price) return { amountLabel: '', label: 'Цена пока неизвестна', timeLabel: '', warning }
  const stamp = price.checked_at ? Date.parse(price.checked_at) : NaN
  const dated = Number.isFinite(stamp)
  const timeLabel = dated
    ? `${new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }).format(stamp)} МСК`
    : 'Время проверки неизвестно'
  const ageWarning = dated && now - stamp > 24 * 60 * 60 * 1000 ? 'Цена проверена более суток назад.' : ''
  return {
    amountLabel: new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(price.amount)),
    label: labels[price.source] || labels.saved,
    timeLabel: dated ? `Проверена ${timeLabel}` : timeLabel,
    warning: [warning, ageWarning].filter(Boolean).join(' '),
  }
}
