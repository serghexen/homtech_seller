import assert from 'node:assert/strict'
import test from 'node:test'

import { addMonthsToDate, keyCountLabel, keyOrderLabel, parseKeyLines } from '../src/utils/keyPool.js'

test('parseKeyLines removes blanks and duplicate keys without changing order', () => {
  assert.deepEqual(parseKeyLines(' ONE \n\nTWO\r\nONE\n THREE '), ['ONE', 'TWO', 'THREE'])
})

test('keyCountLabel uses Russian plural forms', () => {
  assert.equal(keyCountLabel(1), '1 ключ')
  assert.equal(keyCountLabel(3), '3 ключа')
  assert.equal(keyCountLabel(12), '12 ключей')
})

test('keyOrderLabel shows Seller order id and understands imported CRM references', () => {
  assert.equal(keyOrderLabel({ issued_order_id: '59942082307', issued_order_ref: 'technical' }), 'Заказ 59942082307')
  assert.equal(keyOrderLabel({ issued_order_ref: 'yandex:joycards:59941300226:1162705155' }), 'Заказ 59941300226')
  assert.equal(keyOrderLabel({}), '—')
})

test('addMonthsToDate follows CRM quick date controls and clamps month end', () => {
  assert.equal(addMonthsToDate('', 1, new Date(2026, 7, 27)), '2026-09-27')
  assert.equal(addMonthsToDate('2026-01-31', 1), '2026-02-28')
  assert.equal(addMonthsToDate('2024-02-29', 12), '2025-02-28')
})
