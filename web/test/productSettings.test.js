import assert from 'node:assert/strict'
import test from 'node:test'

import {
  normalizeProductSettings,
  productSettingsEqual,
  validateProductSettings,
} from '../src/utils/productSettings.js'

test('normalizes local product settings without marketplace fields', () => {
  const result = normalizeProductSettings({
    manual_stock_limit: '5',
    sales_limit_enabled: false,
    sales_limit: '10',
    sales_limit_daily_extra: '2',
    activation_instruction: ' Первая строка\r\nВторая строка ',
  })

  assert.deepEqual(result, {
    manual_stock_limit: 5,
    sales_limit: null,
    sales_limit_daily_extra: 2,
    activation_instruction: 'Первая строка\nВторая строка',
  })
})

test('validates numeric limits before saving', () => {
  const invalid = normalizeProductSettings({
    manual_stock_limit: -1,
    sales_limit_enabled: true,
    sales_limit: 0,
    sales_limit_daily_extra: 0,
    activation_instruction: '',
  })
  assert.match(validateProductSettings(invalid), /Заданный остаток/)
})

test('detects unsaved settings changes', () => {
  const saved = { manual_stock_limit: 5, sales_limit: null, sales_limit_daily_extra: 0, activation_instruction: '' }
  assert.equal(productSettingsEqual(saved, { ...saved }), true)
  assert.equal(productSettingsEqual(saved, { ...saved, manual_stock_limit: 6 }), false)
})
