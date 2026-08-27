import assert from 'node:assert/strict'
import test from 'node:test'

import { isOrderFulfillmentViewOnly } from '../src/utils/orderFulfillment.js'

test('only processing orders open the manual fulfillment workflow', () => {
  assert.equal(isOrderFulfillmentViewOnly({ status: 'processing' }), false)
  assert.equal(isOrderFulfillmentViewOnly({ status: 'delivered' }), true)
  assert.equal(isOrderFulfillmentViewOnly({ status: 'in_delivery' }), true)
})
