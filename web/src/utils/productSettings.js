export const PRODUCT_SETTING_MAX = 1_000_000
export const PRODUCT_INSTRUCTION_MAX = 10_000

export function normalizeProductSettings(values) {
  return {
    manual_stock_limit: Math.trunc(Number(values.manual_stock_limit)),
    sales_limit: values.sales_limit_enabled ? Math.trunc(Number(values.sales_limit)) : null,
    sales_limit_daily_extra: Math.trunc(Number(values.sales_limit_daily_extra)),
    activation_instruction: String(values.activation_instruction || '').replace(/\r\n?/g, '\n').trim(),
  }
}

export function validateProductSettings(values) {
  if (!Number.isInteger(values.manual_stock_limit) || values.manual_stock_limit < 0 || values.manual_stock_limit > PRODUCT_SETTING_MAX) {
    return 'Заданный остаток должен быть целым числом от 0 до 1 000 000'
  }
  if (values.sales_limit !== null && (!Number.isInteger(values.sales_limit) || values.sales_limit < 1 || values.sales_limit > PRODUCT_SETTING_MAX)) {
    return 'Дневной лимит должен быть целым числом от 1 до 1 000 000'
  }
  if (!Number.isInteger(values.sales_limit_daily_extra) || values.sales_limit_daily_extra < 0 || values.sales_limit_daily_extra > PRODUCT_SETTING_MAX) {
    return 'Дополнительный лимит должен быть целым числом от 0 до 1 000 000'
  }
  if (values.activation_instruction.length > PRODUCT_INSTRUCTION_MAX) {
    return 'Инструкция не должна превышать 10 000 символов'
  }
  return ''
}

export function productSettingsEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right)
}
