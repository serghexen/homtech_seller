export function supportsStockPublication(providerCode) {
  return ['yandex_market', 'ozon'].includes(String(providerCode || ''))
}

export function poolControlsStock(settings) {
  return !settings?.supplier_issue_enabled && settings?.pool_issue_enabled === true
}

export function stockPublicationTarget(settings, keyPool) {
  if (poolControlsStock(settings)) return Math.max(0, Math.trunc(Number(keyPool?.free_count) || 0))
  return Math.trunc(Number(settings?.manual_stock_limit))
}
