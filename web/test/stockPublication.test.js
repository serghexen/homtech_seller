import assert from 'node:assert/strict'
import test from 'node:test'

import { supportsStockPublication } from '../src/utils/stockPublication.js'

test('manual stock publication is available for both supported marketplaces', () => {
  assert.equal(supportsStockPublication('yandex_market'), true)
  assert.equal(supportsStockPublication('ozon'), true)
  assert.equal(supportsStockPublication('unknown'), false)
})
