import assert from 'node:assert/strict'
import test from 'node:test'

import { canOpenOrderFulfillment, isOrderFulfillmentViewOnly } from '../src/utils/orderFulfillment.js'

test('only processing orders open the manual fulfillment workflow', () => {
  assert.equal(isOrderFulfillmentViewOnly({ status: 'processing' }), false)
  assert.equal(isOrderFulfillmentViewOnly({ status: 'delivered' }), true)
  assert.equal(isOrderFulfillmentViewOnly({ status: 'in_delivery' }), true)
})

test('delivered Ozon orders remain viewable when Seller stores their keys', () => {
  assert.equal(canOpenOrderFulfillment({
    provider_code: 'ozon',
    status: 'delivered',
    delivery_type: 'FBO',
    has_fulfillment_keys: true,
  }), true)
  assert.equal(canOpenOrderFulfillment({
    provider_code: 'ozon',
    status: 'delivered',
    delivery_type: 'FBO',
    has_fulfillment_keys: false,
    has_fulfillment_result: false,
  }), false)
})

test('delivered support orders remain viewable when Seller stores their sent message', () => {
  assert.equal(canOpenOrderFulfillment({
    provider_code: 'yandex_market',
    status: 'delivered',
    delivery_type: 'DIGITAL',
    has_fulfillment_keys: false,
    has_fulfillment_result: true,
  }), true)
})

test('processing digital orders open before keys are prepared', () => {
  assert.equal(canOpenOrderFulfillment({
    provider_code: 'ozon',
    status: 'processing',
    delivery_type: 'DIGITAL',
    has_fulfillment_keys: false,
  }), true)
  assert.equal(canOpenOrderFulfillment({
    provider_code: 'unknown',
    status: 'processing',
    delivery_type: 'DIGITAL',
    has_fulfillment_keys: true,
  }), false)
})
