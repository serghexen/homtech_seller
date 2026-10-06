import test from 'node:test'
import assert from 'node:assert/strict'
import { supplierPriceDetails } from '../src/utils/supplierPrice.js'

const item = { supplier_service_id: 42, supplier_nominal_id: '2150', supplier_quoted_amount: '1500', supplier_quoted_at: '2026-01-01T08:00:00Z' }
const quote = { service_id: 42, nominal_id: '2150', amount: '1732.40', source: 'crm', checked_at: '2026-10-06T08:00:00Z', last_attempt_at: '2026-10-06T09:00:00Z' }
const now = Date.parse('2026-10-06T10:00:00Z')

test('CRM price uses successful price check time in Moscow, not last attempt', () => {
  const value = supplierPriceDetails(item, item, quote, now)
  assert.equal(value.label, 'Цена по данным CRM')
  assert.match(value.amountLabel, /1\s732,40/)
  assert.equal(value.timeLabel, 'Проверена 06.10.2026, 11:00 МСК')
  assert.equal(value.warning, '')
})

test('failed or absent CRM check explicitly falls back to saved binding date', () => {
  for (const value of [null, { ...quote, amount: null, warning: 'Нет цены CRM' }]) {
    const result = supplierPriceDetails(item, item, value, now)
    assert.equal(result.label, 'Сохранённая цена связки')
    assert.match(result.timeLabel, /01.01.2026/)
    assert.match(result.warning, /более суток/)
  }
  assert.equal(supplierPriceDetails({ ...item, supplier_quoted_at: null }, item, null, now).timeLabel, 'Время проверки неизвестно')
})

test('live quote is labelled independently and failed CRM check preserves warning', () => {
  assert.equal(supplierPriceDetails(item, item, { ...quote, source: 'provider' }, now).label, 'Цена по проверке поставщика')
  assert.equal(supplierPriceDetails(item, item, { ...quote, warning: 'Последняя попытка не удалась' }, now).warning, 'Последняя попытка не удалась')
})

test('switching service or nominal cannot show another mapping price', () => {
  for (const change of [{ supplier_service_id: 43 }, { supplier_nominal_id: '950' }]) {
    const result = supplierPriceDetails(item, { ...item, ...change }, quote, now)
    assert.equal(result.amountLabel, '')
    assert.equal(result.label, 'Цена пока неизвестна')
    assert.equal(result.timeLabel, '')
  }
})
