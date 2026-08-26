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
    support_message: ' Обратитесь\r\nв поддержку ',
    support_message_delivery_enabled: true,
    pool_issue_enabled: true,
    supplier_issue_enabled: true,
    supplier_service_id: '11125',
    supplier_nominal_id: '250',
    supplier_max_amount: '487.76',
  })

  assert.deepEqual(result, {
    manual_stock_limit: 5,
    sales_limit: null,
    sales_limit_daily_extra: 2,
    activation_instruction: 'Первая строка\nВторая строка',
    support_message: 'Обратитесь\nв поддержку',
    support_message_delivery_enabled: true,
    pool_issue_enabled: true,
    supplier_issue_enabled: true,
    supplier_service_id: 11125,
    supplier_nominal_id: '250',
    supplier_max_amount: 487.76,
  })
})

test('requires complete Supplier Hub mapping before enabling supplier delivery', () => {
  const invalid = normalizeProductSettings({
    manual_stock_limit: 0,
    sales_limit_enabled: false,
    sales_limit_daily_extra: 0,
    supplier_issue_enabled: true,
  })
  assert.match(validateProductSettings(invalid), /выберите товар/)
})

test('validates numeric limits before saving', () => {
  const invalid = normalizeProductSettings({
    manual_stock_limit: -1,
    sales_limit_enabled: true,
    sales_limit: 0,
    sales_limit_daily_extra: 0,
    activation_instruction: '',
    support_message: '',
    support_message_delivery_enabled: false,
    pool_issue_enabled: false,
  })
  assert.match(validateProductSettings(invalid), /Заданный остаток/)
})

test('detects unsaved settings changes', () => {
  const saved = { manual_stock_limit: 5, sales_limit: null, sales_limit_daily_extra: 0, activation_instruction: '', support_message: '', support_message_delivery_enabled: false, pool_issue_enabled: false }
  assert.equal(productSettingsEqual(saved, { ...saved }), true)
  assert.equal(productSettingsEqual(saved, { ...saved, manual_stock_limit: 6 }), false)
})
