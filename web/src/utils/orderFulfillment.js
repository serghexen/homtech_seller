export function isOrderFulfillmentViewOnly(order) {
  return order?.status !== 'processing'
}
