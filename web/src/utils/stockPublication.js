export function supportsStockPublication(providerCode) {
  return ['yandex_market', 'ozon'].includes(String(providerCode || ''))
}
