import test from 'node:test'
import assert from 'node:assert/strict'
import { supplierStockMessage } from '../src/utils/supplierStock.js'

test('просроченная проверка не обещает нулевой остаток или отправку', () => {
  assert.match(supplierStockMessage({ observation: 'stale', blocked: false }), /обнуление не выполняется/)
  assert.match(supplierStockMessage({ observation: 'common_error', blocked: true }), /ранее установленная блокировка/)
  assert.equal(supplierStockMessage({ observation: 'disabled' }), '')
})
test('наличие не сбрасывает дневной лимит', () => {
  assert.match(supplierStockMessage({ observation: 'available' }), /дневным лимитом/)
  assert.match(supplierStockMessage({ observation: 'zero' }), /остаток 0/)
})
