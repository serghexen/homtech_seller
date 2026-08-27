export function connectionAccountValue(connection = {}) {
  if (connection.provider_code === 'ozon') return String(connection.client_id ?? '')
  return [connection.business_id, connection.campaign_id]
    .filter((value) => value !== null && value !== undefined && value !== '')
    .join(' / ')
}

export function connectionLastCheckedAt(connection = {}) {
  return connection.provider_code === 'ozon'
    ? connection.last_orders_poll_at
    : connection.last_checked_at
}
