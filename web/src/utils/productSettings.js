export const PRODUCT_SETTING_MAX = 1_000_000
export const PRODUCT_INSTRUCTION_MAX = 10_000
export const PRODUCT_SUPPORT_MESSAGE_MAX = 2_000

export function normalizeProductSettings(values) {
  return {
    manual_stock_limit: Math.trunc(Number(values.manual_stock_limit)),
    sales_limit: values.sales_limit_enabled ? Math.trunc(Number(values.sales_limit)) : null,
    sales_limit_daily_extra: Math.trunc(Number(values.sales_limit_daily_extra)),
    activation_instruction: String(values.activation_instruction || '').replace(/\r\n?/g, '\n').trim(),
    support_message: String(values.support_message || '').replace(/\r\n?/g, '\n').trim(),
    support_message_delivery_enabled: Boolean(values.support_message_delivery_enabled),
    pool_issue_enabled: Boolean(values.pool_issue_enabled),
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
  if (values.support_message.length > PRODUCT_SUPPORT_MESSAGE_MAX) {
    return 'Сообщение поддержки не должно превышать 2 000 символов'
  }
  if (values.support_message_delivery_enabled && !values.support_message) {
    return 'Для выдачи через поддержку сначала заполните сообщение'
  }
  return ''
}

export function productSettingsEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right)
}
