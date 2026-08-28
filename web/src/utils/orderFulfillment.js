export function isOrderFulfillmentViewOnly(order) {
  return order?.status !== 'processing'
}

export function orderFulfillmentAction(order, fallbackProviderCode = '') {
  const providerCode = order?.provider_code || fallbackProviderCode
  if (!['yandex_market', 'ozon'].includes(providerCode)) return 'none'

  const serverAction = String(order?.fulfillment_action || '').trim()
  if (['none', 'automatic', 'operator', 'view', 'attention'].includes(serverAction)) {
    return serverAction
  }

  // Совместимость во время поэтапного обновления API и web.
  const isProcessingDigital = order?.status === 'processing'
    && String(order?.delivery_type || '').trim().toUpperCase() === 'DIGITAL'
  if (isProcessingDigital) return 'operator'
  if (order?.has_fulfillment_result || order?.has_fulfillment_keys) return 'view'
  return 'none'
}

export function canOpenOrderFulfillment(order, fallbackProviderCode = '') {
  return ['operator', 'view', 'attention'].includes(orderFulfillmentAction(order, fallbackProviderCode))
}
