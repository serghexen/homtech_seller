export function isOrderFulfillmentViewOnly(order) {
  return order?.status !== 'processing'
}

export function canOpenOrderFulfillment(order, fallbackProviderCode = '') {
  const providerCode = order?.provider_code || fallbackProviderCode
  if (!['yandex_market', 'ozon'].includes(providerCode)) return false

  const isProcessingDigital = order?.status === 'processing'
    && String(order?.delivery_type || '').trim().toUpperCase() === 'DIGITAL'
  return isProcessingDigital || Boolean(order?.has_fulfillment_result || order?.has_fulfillment_keys)
}
