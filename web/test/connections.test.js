import assert from 'node:assert/strict'
import test from 'node:test'

import { connectionAccountValue, connectionLastCheckedAt } from '../src/utils/connections.js'

test('store cards use one account field for Ozon and Yandex Market', () => {
  assert.equal(connectionAccountValue({ provider_code: 'ozon', client_id: '3313715' }), '3313715')
  assert.equal(connectionAccountValue({
    provider_code: 'yandex_market',
    business_id: 48186803,
    campaign_id: 70940298,
  }), '48186803 / 70940298')
})

test('store cards resolve the marketplace-specific successful check timestamp', () => {
  assert.equal(connectionLastCheckedAt({ provider_code: 'ozon', last_orders_poll_at: 'ozon-check' }), 'ozon-check')
  assert.equal(connectionLastCheckedAt({ provider_code: 'yandex_market', last_checked_at: 'yandex-check' }), 'yandex-check')
})
