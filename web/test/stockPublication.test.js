import assert from 'node:assert/strict'
import test from 'node:test'

import {
  poolControlsStock,
  stockPublicationTarget,
  supportsStockPublication,
} from '../src/utils/stockPublication.js'

test('manual stock publication is available for both supported marketplaces', () => {
  assert.equal(supportsStockPublication('yandex_market'), true)
  assert.equal(supportsStockPublication('ozon'), true)
  assert.equal(supportsStockPublication('unknown'), false)
})

test('pool controls stock only when supplier fulfillment is disabled', () => {
  assert.equal(poolControlsStock({ supplier_issue_enabled: false, pool_issue_enabled: true }), true)
  assert.equal(poolControlsStock({ supplier_issue_enabled: true, pool_issue_enabled: true }), false)
  assert.equal(poolControlsStock({ supplier_issue_enabled: false, pool_issue_enabled: false }), false)
})

test('publication target follows current free pool count and preserves manual fallback', () => {
  assert.equal(stockPublicationTarget(
    { manual_stock_limit: 8, supplier_issue_enabled: false, pool_issue_enabled: true },
    { free_count: 3 },
  ), 3)
  assert.equal(stockPublicationTarget(
    { manual_stock_limit: 8, supplier_issue_enabled: true, pool_issue_enabled: true },
    { free_count: 3 },
  ), 8)
})
