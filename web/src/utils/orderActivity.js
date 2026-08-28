export const ORDER_ACTIVITY_VISIBLE_INTERVAL_MS = 10_000
export const ORDER_ACTIVITY_HIDDEN_INTERVAL_MS = 30_000
export const ORDER_TOAST_LIFETIME_MS = 9_000
export const ORDER_TOAST_VISIBLE_LIMIT = 3

export function orderActivityPreferenceKey(user = {}) {
  const workspace = user.workspace_id ?? user.workspace_name ?? 'workspace'
  const account = user.user_id ?? user.id ?? user.email ?? 'user'
  return `homtech-seller:order-popups:${workspace}:${account}`
}

export function readOrderPopupPreference(storage, user = {}) {
  if (!storage) return true
  try {
    return storage.getItem(orderActivityPreferenceKey(user)) !== 'disabled'
  } catch {
    return true
  }
}

export function writeOrderPopupPreference(storage, user = {}, enabled = true) {
  if (!storage) return
  try {
    storage.setItem(orderActivityPreferenceKey(user), enabled ? 'enabled' : 'disabled')
  } catch {
    // Недоступное локальное хранилище не должно останавливать обновление заказов.
  }
}

export function orderActivityIdentity(event = {}) {
  return `${event.connection_id ?? ''}:${event.external_order_id ?? ''}`
}

export function groupNewOrderEvents(events = []) {
  const groups = new Map()
  for (const event of events) {
    if (event.event_type !== 'new_order') continue
    const identity = orderActivityIdentity(event)
    if (!event.external_order_id || groups.has(identity)) {
      const current = groups.get(identity)
      if (current) current.quantity += Number(event.quantity || 0)
      continue
    }
    groups.set(identity, {
      identity,
      connection_id: Number(event.connection_id),
      provider_code: event.provider_code || '',
      store_name: event.store_name || '',
      external_order_id: event.external_order_id,
      title: event.title || '',
      quantity: Number(event.quantity || 0),
      occurred_at: event.occurred_at || '',
    })
  }
  return [...groups.values()]
}

export function visibleOrderToasts(groups = [], limit = ORDER_TOAST_VISIBLE_LIMIT) {
  const visible = groups.slice(0, Math.max(0, limit))
  const hiddenCount = Math.max(0, groups.length - visible.length)
  if (hiddenCount) visible.push({ identity: `summary:${Date.now()}`, is_summary: true, hidden_count: hiddenCount })
  return visible
}
