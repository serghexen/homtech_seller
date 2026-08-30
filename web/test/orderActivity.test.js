import assert from 'node:assert/strict'
import test from 'node:test'

import {
  groupNewOrderEvents,
  orderActivityPreferenceKey,
  readOrderPopupPreference,
  visibleOrderToasts,
  writeOrderPopupPreference,
} from '../src/utils/orderActivity.js'

test('new order events are grouped per connection and external order', () => {
  const groups = groupNewOrderEvents([
    { event_type: 'new_order', connection_id: 11, external_order_id: 'A-1', quantity: 2, store_name: 'One' },
    { event_type: 'new_order', connection_id: 11, external_order_id: 'A-1', quantity: 1, store_name: 'One' },
    { event_type: 'new_order', connection_id: 12, external_order_id: 'A-1', quantity: 4, store_name: 'Two' },
    { event_type: 'status_changed', connection_id: 11, external_order_id: 'A-2', quantity: 1 },
  ])

  assert.equal(groups.length, 2)
  assert.equal(groups[0].quantity, 3)
  assert.equal(groups[1].quantity, 4)
  assert.notEqual(groups[0].identity, groups[1].identity)
})

test('fulfillment start is treated as a new working order', () => {
  const groups = groupNewOrderEvents([
    { event_type: 'fulfillment_started', connection_id: 4, external_order_id: 'PAID', quantity: 1 },
    { event_type: 'status_changed', connection_id: 4, external_order_id: 'PAID', quantity: 1 },
  ])

  assert.deepEqual(groups.map((item) => item.external_order_id), ['PAID'])
})

test('popup preference is isolated by workspace and user and defaults to enabled', () => {
  const values = new Map()
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }
  const first = { workspace_id: 7, user_id: 3 }
  const second = { workspace_id: 8, user_id: 3 }

  assert.equal(readOrderPopupPreference(storage, first), true)
  writeOrderPopupPreference(storage, first, false)
  assert.equal(readOrderPopupPreference(storage, first), false)
  assert.equal(readOrderPopupPreference(storage, second), true)
  assert.notEqual(orderActivityPreferenceKey(first), orderActivityPreferenceKey(second))
})

test('burst keeps a compact visible stack and adds a summary', () => {
  const groups = Array.from({ length: 6 }, (_, index) => ({ identity: String(index) }))
  const visible = visibleOrderToasts(groups, 3)

  assert.equal(visible.length, 4)
  assert.equal(visible[3].is_summary, true)
  assert.equal(visible[3].hidden_count, 3)
})
