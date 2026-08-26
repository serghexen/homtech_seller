<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import { keyCountLabel, parseKeyLines } from '../utils/keyPool.js'
import { normalizeEscapedLineBreaks } from '../utils/text.js'
import { normalizeProductSettings, productSettingsEqual, validateProductSettings } from '../utils/productSettings.js'

const props = defineProps({
  item: { type: Object, required: true },
  providerName: { type: String, required: true },
  providerLogo: { type: String, required: true },
  syncedLabel: { type: String, required: true },
  stockSyncedLabel: { type: String, default: '' },
  stockLoading: { type: Boolean, default: false },
  stockError: { type: String, default: '' },
  stockRefreshEnabled: { type: Boolean, default: true },
  settingsSaving: { type: Boolean, default: false },
  settingsError: { type: String, default: '' },
  settingsNotice: { type: String, default: '' },
  supplierServices: { type: Array, default: () => [] },
  supplierServicesLoading: { type: Boolean, default: false },
  supplierServicesError: { type: String, default: '' },
  supplierQuote: { type: Object, default: null },
  supplierQuoteLoading: { type: Boolean, default: false },
  supplierQuoteError: { type: String, default: '' },
  orders: { type: Array, default: () => [] },
  ordersTotal: { type: Number, default: 0 },
  ordersLoading: { type: Boolean, default: false },
  ordersError: { type: String, default: '' },
  ordersRefreshing: { type: Boolean, default: false },
  ordersRefreshEnabled: { type: Boolean, default: true },
  keyPool: { type: Object, default: () => ({ free_count: 0, total: 0, items: [] }) },
  keyPoolLoading: { type: Boolean, default: false },
  keyPoolSaving: { type: Boolean, default: false },
  keyPoolError: { type: String, default: '' },
  keyPoolNotice: { type: String, default: '' },
  keyPoolCanManage: { type: Boolean, default: false },
})

const emit = defineEmits(['close', 'refresh-stock', 'refresh-orders', 'save-settings', 'load-key-pool', 'add-keys', 'quote-supplier'])
const openSection = ref('delivery')
const openDeliveryMethod = ref('')
const supplierSearch = ref('')
const supplierPickerOpen = ref(false)
const imageFailed = ref(false)
const settingsFormError = ref('')
const settingsForm = reactive({
  manual_stock_limit: Math.max(0, Number(props.item.manual_stock_limit) || 0),
  sales_limit_enabled: props.item.sales_limit !== null && props.item.sales_limit !== '',
  sales_limit: props.item.sales_limit !== null && props.item.sales_limit !== '' ? Math.max(1, Number(props.item.sales_limit) || 1) : 1,
  sales_limit_daily_extra: Math.max(0, Number(props.item.sales_limit_daily_extra) || 0),
  activation_instruction: normalizeEscapedLineBreaks(props.item.activation_instruction).trim(),
  support_message: normalizeEscapedLineBreaks(props.item.support_message).trim(),
  support_message_delivery_enabled: Boolean(props.item.support_message_delivery_enabled),
  pool_issue_enabled: Boolean(props.item.pool_issue_enabled),
  supplier_issue_enabled: Boolean(props.item.supplier_issue_enabled),
  supplier_service_id: props.item.supplier_service_id || '',
  supplier_nominal_id: props.item.supplier_nominal_id || '',
  supplier_max_amount: props.item.supplier_max_amount || '',
})
const keyPoolForm = reactive({ codes_raw: '', expires_at: '' })
const keyPoolFormError = ref('')
const showProductImage = computed(() => Boolean(props.item.primary_image) && !imageFailed.value)
const canRefreshStock = computed(() => props.item.provider_code === 'yandex_market' && props.stockRefreshEnabled)
const currentStock = computed(() => Number.isInteger(props.item.available_stock) ? props.item.available_stock : '—')
const hasSalesMetrics = computed(() => Boolean(props.item.sales_metrics_available))
const hasSalesLimit = computed(() => settingsForm.sales_limit_enabled)
const limitMetrics = computed(() => [
  { label: 'Продано', value: hasSalesMetrics.value ? Math.max(0, Number(props.item.sales_limit_used) || 0) : '—' },
  { label: 'В резерве', value: hasSalesMetrics.value ? Math.max(0, Number(props.item.sales_limit_reserved) || 0) : '—' },
  { label: 'Осталось по снимку', value: hasSalesMetrics.value && props.item.sales_limit_remaining !== null ? Math.max(0, Number(props.item.sales_limit_remaining) || 0) : '—' },
])
const limitHeadline = computed(() => {
  if (!hasSalesLimit.value) return 'Без ограничений'
  return `${Math.max(1, Number(settingsForm.sales_limit) || 1)} продаж в день`
})
const hasActivationInstruction = computed(() => Boolean(settingsForm.activation_instruction.trim()))
const activationInstructionDescription = computed(() => {
  return hasActivationInstruction.value ? 'Текст для покупателя заполнен' : 'Текст для покупателя не указан'
})
const deliveryPriority = computed(() => [
  ...(settingsForm.supplier_issue_enabled ? ['Поставщик'] : []),
  ...(settingsForm.pool_issue_enabled ? ['Список ключей'] : []),
  ...(settingsForm.support_message_delivery_enabled ? ['Поддержка'] : []),
  'Ручной ввод',
])
const deliveryDescription = computed(() => `Приоритет: ${deliveryPriority.value.join(' → ')}`)
const normalizedSettings = computed(() => normalizeProductSettings(settingsForm))
const savedSettings = computed(() => normalizeProductSettings({
  manual_stock_limit: Math.max(0, Number(props.item.manual_stock_limit) || 0),
  sales_limit_enabled: props.item.sales_limit !== null && props.item.sales_limit !== '',
  sales_limit: props.item.sales_limit,
  sales_limit_daily_extra: Math.max(0, Number(props.item.sales_limit_daily_extra) || 0),
  activation_instruction: normalizeEscapedLineBreaks(props.item.activation_instruction),
  support_message: normalizeEscapedLineBreaks(props.item.support_message),
  support_message_delivery_enabled: Boolean(props.item.support_message_delivery_enabled),
  pool_issue_enabled: Boolean(props.item.pool_issue_enabled),
  supplier_issue_enabled: Boolean(props.item.supplier_issue_enabled),
  supplier_service_id: props.item.supplier_service_id || '',
  supplier_nominal_id: props.item.supplier_nominal_id || '',
  supplier_max_amount: props.item.supplier_max_amount || '',
}))
const settingsDirty = computed(() => !productSettingsEqual(normalizedSettings.value, savedSettings.value))
const instructionLength = computed(() => settingsForm.activation_instruction.length)
const supportMessageLength = computed(() => settingsForm.support_message.length)
const keyPoolDraftCodes = computed(() => parseKeyLines(keyPoolForm.codes_raw))
const keyPoolDraftDirty = computed(() => Boolean(keyPoolForm.codes_raw.trim() || keyPoolForm.expires_at))
const keyPoolPageCount = computed(() => Math.max(1, Math.ceil((Number(props.keyPool.total) || 0) / (Number(props.keyPool.page_size) || 20))))
const selectedSupplierService = computed(() => {
  const serviceId = Number(settingsForm.supplier_service_id)
  return props.supplierServices.find((service) => Number(service.service_id) === serviceId) || null
})
const supplierNominalField = computed(() => {
  const fields = Array.isArray(selectedSupplierService.value?.fields) ? selectedSupplierService.value.fields : []
  return fields.find((field) => String(field?.name || '').toLowerCase() === 'nominal') || null
})
const supplierNominalOptions = computed(() => {
  const values = Array.isArray(supplierNominalField.value?.value_list) ? supplierNominalField.value.value_list : []
  return values.map((value) => {
    if (value && typeof value === 'object') {
      const id = value.id ?? value.value ?? value.title
      return { id: String(id ?? ''), title: String(value.title ?? value.value ?? id ?? '') }
    }
    return { id: String(value ?? ''), title: String(value ?? '') }
  }).filter((value) => value.id)
})
const supplierNominalRequired = computed(() => Boolean(supplierNominalField.value?.required))
const supplierMappingComplete = computed(() => Boolean(settingsForm.supplier_service_id)
  && (!supplierNominalRequired.value || Boolean(String(settingsForm.supplier_nominal_id || '').trim())))
const selectedSupplierNominal = computed(() => supplierNominalOptions.value.find(
  (option) => option.id === String(settingsForm.supplier_nominal_id || ''),
) || null)
const supplierServiceSummary = computed(() => {
  if (selectedSupplierService.value) {
    const nominal = selectedSupplierNominal.value?.title || settingsForm.supplier_nominal_id
    return nominal ? `${selectedSupplierService.value.title} · ${nominal}` : selectedSupplierService.value.title
  }
  return settingsForm.supplier_service_id ? `Interhub · услуга #${settingsForm.supplier_service_id}` : 'Выберите товар Interhub'
})
const filteredSupplierServices = computed(() => {
  const needle = supplierSearch.value.trim().toLocaleLowerCase('ru-RU')
  const items = needle
    ? props.supplierServices.filter((service) => [service.title, service.category, service.service_id]
      .some((value) => String(value || '').toLocaleLowerCase('ru-RU').includes(needle)))
    : props.supplierServices
  return items.slice(0, 40)
})
const supplierCurrentPrice = computed(() => {
  const quote = props.supplierQuote
  const quoteMatches = quote
    && Number(quote.service_id) === Number(settingsForm.supplier_service_id)
    && String(quote.nominal_id || '') === String(settingsForm.supplier_nominal_id || '')
  if (quoteMatches) return quote.amount
  const savedMappingMatches = Number(props.item.supplier_service_id) === Number(settingsForm.supplier_service_id)
    && String(props.item.supplier_nominal_id || '') === String(settingsForm.supplier_nominal_id || '')
  return savedMappingMatches ? props.item.supplier_quoted_amount : null
})
const supplierCurrentPriceLabel = computed(() => {
  const amount = Number(supplierCurrentPrice.value)
  if (!Number.isFinite(amount) || amount <= 0) return ''
  return new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(amount)
})

const detailFields = computed(() => [
  { label: 'Артикул продавца', value: props.item.market_sku || props.item.offer_id || props.item.external_product_id || '—' },
  { label: 'SKU', value: props.item.offer_id || props.item.sku || '—' },
  { label: 'Цена', value: props.item.price ? `${props.item.price} ${props.item.currency_code || ''}`.trim() : '—' },
])

const workSections = computed(() => [
  {
    id: 'delivery',
    number: '01',
    title: 'Выдача',
    description: deliveryDescription.value,
  },
  {
    id: 'stock',
    number: '02',
    title: 'Остаток',
    description: 'Актуальное количество, доступное для продажи на маркетплейсе',
  },
  {
    id: 'instruction',
    number: '03',
    title: 'Инструкция',
    description: activationInstructionDescription.value,
  },
  {
    id: 'orders',
    number: '04',
    title: 'Заказы',
    description: props.ordersLoading
      ? 'Загружаем историю продаж этой карточки'
      : props.ordersTotal
        ? `${orderCountLabel(props.ordersTotal)} в локальном снимке`
        : 'История продаж именно этой карточки товара',
  },
])

function orderCountLabel(value) {
  const count = Math.max(0, Number(value) || 0)
  const mod100 = count % 100
  const mod10 = count % 10
  const word = mod100 >= 11 && mod100 <= 14 ? 'заказов' : mod10 === 1 ? 'заказ' : mod10 >= 2 && mod10 <= 4 ? 'заказа' : 'заказов'
  return `${count} ${word}`
}

function orderStatusLabel(status) {
  return {
    processing: 'В процессе',
    in_delivery: 'Доставляется',
    delivered: 'Доставлен',
    cancelled: 'Отменён',
    problem: 'Проблема',
  }[status] || 'Неизвестно'
}

function formatOrderDate(value) {
  if (!value) return 'Дата не указана'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return 'Дата не указана'
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(parsed)
}

function toggleSection(sectionId) {
  openSection.value = openSection.value === sectionId ? '' : sectionId
}

function toggleDeliveryMethod(methodId) {
  openDeliveryMethod.value = openDeliveryMethod.value === methodId ? '' : methodId
}

function supplierServiceDisplay(service) {
  return String(service?.title || `Услуга #${service?.service_id || ''}`)
}

function selectSupplierService(service) {
  const changed = Number(settingsForm.supplier_service_id) !== Number(service.service_id)
  settingsForm.supplier_service_id = Number(service.service_id)
  supplierSearch.value = supplierServiceDisplay(service)
  supplierPickerOpen.value = false
  if (changed) {
    settingsForm.supplier_nominal_id = ''
    settingsForm.supplier_max_amount = ''
    settingsForm.supplier_issue_enabled = false
  }
  const nominalField = Array.isArray(service.fields)
    ? service.fields.find((field) => String(field?.name || '').toLowerCase() === 'nominal')
    : null
  if (!nominalField?.required) requestSupplierQuote()
}

function clearSupplierService() {
  supplierSearch.value = ''
  settingsForm.supplier_service_id = ''
  settingsForm.supplier_nominal_id = ''
  settingsForm.supplier_max_amount = ''
  settingsForm.supplier_issue_enabled = false
  supplierPickerOpen.value = true
}

function requestSupplierQuote() {
  if (!supplierMappingComplete.value || props.supplierQuoteLoading) return
  settingsForm.supplier_max_amount = ''
  emit('quote-supplier', {
    service_id: Number(settingsForm.supplier_service_id),
    nominal_id: String(settingsForm.supplier_nominal_id || '').trim(),
  })
}

function handleSupplierNominalChange() {
  settingsForm.supplier_max_amount = ''
  settingsForm.supplier_issue_enabled = false
  requestSupplierQuote()
}

function resetSettingsForm() {
  settingsForm.manual_stock_limit = savedSettings.value.manual_stock_limit
  settingsForm.sales_limit_enabled = savedSettings.value.sales_limit !== null
  settingsForm.sales_limit = savedSettings.value.sales_limit || 1
  settingsForm.sales_limit_daily_extra = savedSettings.value.sales_limit_daily_extra
  settingsForm.activation_instruction = savedSettings.value.activation_instruction
  settingsForm.support_message = savedSettings.value.support_message
  settingsForm.support_message_delivery_enabled = savedSettings.value.support_message_delivery_enabled
  settingsForm.pool_issue_enabled = savedSettings.value.pool_issue_enabled
  settingsForm.supplier_issue_enabled = savedSettings.value.supplier_issue_enabled
  settingsForm.supplier_service_id = savedSettings.value.supplier_service_id || ''
  settingsForm.supplier_nominal_id = savedSettings.value.supplier_nominal_id
  settingsForm.supplier_max_amount = savedSettings.value.supplier_max_amount || ''
  supplierSearch.value = selectedSupplierService.value ? supplierServiceDisplay(selectedSupplierService.value) : ''
  supplierPickerOpen.value = false
  settingsFormError.value = ''
}

function saveSettings() {
  settingsFormError.value = ''
  const values = normalizedSettings.value
  settingsFormError.value = validateProductSettings(values)
  if (settingsFormError.value) return
  emit('save-settings', values)
}

function keyStatusLabel(status) {
  return {
    free: 'Свободен',
    reserved: 'В резерве',
    sending: 'Отправляется',
    delivered: 'Выдан',
    expired: 'Истёк',
    disabled: 'Отключён',
  }[status] || 'Неизвестно'
}

function submitKeyPool() {
  // Проверяет форму до отправки и передаёт API только уникальные непустые строки.
  keyPoolFormError.value = ''
  if (!keyPoolDraftCodes.value.length) {
    keyPoolFormError.value = 'Добавьте хотя бы один ключ — по одному на строку'
    return
  }
  if (keyPoolDraftCodes.value.length > 1000) {
    keyPoolFormError.value = 'За один раз можно добавить не более 1000 ключей'
    return
  }
  emit('add-keys', {
    codes: keyPoolDraftCodes.value,
    expires_at: keyPoolForm.expires_at || null,
  })
}

watch(() => props.keyPoolNotice, (notice) => {
  // После подтверждённого сохранения удаляет открытые коды из памяти формы.
  if (!notice) return
  keyPoolForm.codes_raw = ''
  keyPoolForm.expires_at = ''
  keyPoolFormError.value = ''
})

watch(() => settingsForm.support_message, (message) => {
  if (!message.trim()) settingsForm.support_message_delivery_enabled = false
})

watch(selectedSupplierService, (service) => {
  if (service && !supplierPickerOpen.value) supplierSearch.value = supplierServiceDisplay(service)
}, { immediate: true })

function close() {
  if ((settingsDirty.value || keyPoolDraftDirty.value) && !props.settingsSaving && !props.keyPoolSaving
    && !window.confirm('Закрыть карточку без сохранения изменений?')) return
  emit('close')
}

function closeOnEscape(event) {
  if (event.key !== 'Escape') return
  if (supplierPickerOpen.value) {
    supplierPickerOpen.value = false
    return
  }
  close()
}

onMounted(() => window.addEventListener('keydown', closeOnEscape))
onBeforeUnmount(() => window.removeEventListener('keydown', closeOnEscape))
</script>

<template>
  <Teleport to="body">
    <Transition name="product-card" appear>
      <div class="product-card-backdrop" @click.self="close">
        <section class="product-card-modal" role="dialog" aria-modal="true" aria-labelledby="product-card-title">
          <header class="product-card-modal__header">
            <h2 id="product-card-title">Карточка товара</h2>
            <div class="product-card-modal__head-actions">
              <span v-if="item.archived" class="product-card-modal__archive-state">В архиве</span>
              <span class="product-card-modal__mode">Настройки Seller</span>
              <button type="button" aria-label="Вернуться к каталогу" title="К каталогу" @click="close">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m11 18-6-6 6-6" /></svg>
              </button>
              <button class="product-card-modal__close" type="button" aria-label="Закрыть" title="Закрыть" @click="close">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
              </button>
            </div>
          </header>

          <div class="product-card-modal__body">
            <section class="product-overview" :class="{ 'has-image': showProductImage }">
              <div v-if="showProductImage" class="product-overview__image-wrap">
                <img :src="item.primary_image" :alt="`Изображение товара ${item.title || item.offer_id || ''}`" referrerpolicy="no-referrer" @error="imageFailed = true" />
              </div>
              <div class="product-overview__info">
                <div class="product-overview__source">
                  <span class="product-overview__logo" :class="`product-overview__logo--${item.provider_code}`"><img :src="providerLogo" alt="" /></span>
                  <span>{{ providerName }}</span>
                  <small v-if="item.archived">Архивная карточка</small>
                </div>
                <h3>{{ item.title || item.offer_id || item.sku || 'Товар без названия' }}</h3>
                <dl class="product-overview__grid">
                  <template v-for="field in detailFields" :key="field.label">
                    <dt>{{ field.label }}</dt>
                    <dd>{{ field.value }}</dd>
                  </template>
                </dl>
              </div>
            </section>

            <div class="product-work-blocks">
              <section v-for="section in workSections" :key="section.id" class="product-work-block" :class="{ 'is-open': openSection === section.id }">
                <button class="product-work-block__toggle" type="button" :aria-expanded="openSection === section.id" @click="toggleSection(section.id)">
                  <span class="product-work-block__number">{{ section.number }}</span>
                  <span class="product-work-block__copy"><strong>{{ section.title }}</strong><small>{{ section.description }}</small></span>
                  <svg class="product-work-block__chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 9 5 5 5-5" /></svg>
                </button>
                <div v-if="openSection === section.id" class="product-work-block__body">
                  <div v-if="section.id === 'stock'" class="stock-readonly">
                    <div class="stock-readonly__fields">
                      <section class="stock-readonly__field stock-readonly__field--primary">
                        <span>Остаток на маркетплейсе</span>
                        <div class="stock-readonly__value-row">
                          <output :class="{ 'is-loading': stockLoading }" aria-live="polite">{{ stockLoading ? 'Проверяем…' : currentStock }}</output>
                          <button v-if="canRefreshStock" class="stock-readonly__refresh" type="button" :disabled="stockLoading" title="Запросить актуальный остаток" @click="emit('refresh-stock')">
                            <svg :class="{ 'is-spinning': stockLoading }" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.7" /><path d="M20 4v6h-6" /></svg>
                            <span>{{ stockLoading ? 'Обновляем' : 'Обновить' }}</span>
                          </button>
                        </div>
                        <small>{{ stockSyncedLabel ? `Проверено ${stockSyncedLabel}` : 'Актуальность ещё не проверена' }}</small>
                        <p v-if="stockError" class="stock-readonly__error" role="alert">{{ stockError }}</p>
                      </section>

                      <label class="stock-readonly__field stock-settings__field">
                        <span>Заданный остаток</span>
                        <input v-model.number="settingsForm.manual_stock_limit" type="number" min="0" max="1000000" step="1" inputmode="numeric" />
                        <small>Локальное целевое значение Seller</small>
                      </label>

                      <section class="stock-readonly__field stock-settings__field">
                        <span>Дневной лимит</span>
                        <label class="stock-limit-switch">
                          <input v-model="settingsForm.sales_limit_enabled" type="checkbox" />
                          <span aria-hidden="true"></span>
                          <strong>{{ settingsForm.sales_limit_enabled ? 'Ограничен' : 'Без ограничений' }}</strong>
                        </label>
                        <input v-if="settingsForm.sales_limit_enabled" v-model.number="settingsForm.sales_limit" type="number" min="1" max="1000000" step="1" inputmode="numeric" aria-label="Количество продаж в день" />
                        <small>Максимальное количество продаж за день</small>
                      </section>
                    </div>

                    <section class="stock-limit">
                      <div class="stock-limit__heading">
                        <div><span>Состояние лимита</span><strong>{{ limitHeadline }}</strong></div>
                        <span class="stock-limit__badge">Локальная настройка</span>
                      </div>
                      <label class="stock-limit__extra">
                        <span>Дополнительно сегодня</span>
                        <input v-model.number="settingsForm.sales_limit_daily_extra" type="number" min="0" max="1000000" step="1" inputmode="numeric" />
                        <small>Временная прибавка к дневному лимиту, сохранённая в Seller</small>
                      </label>
                      <div class="stock-limit__metrics">
                        <div v-for="metric in limitMetrics" :key="metric.label"><span>{{ metric.label }}</span><strong>{{ metric.value }}</strong></div>
                      </div>
                      <p v-if="!hasSalesMetrics" class="stock-limit__metrics-note">Статистика использования появится после получения соответствующего снимка.</p>
                    </section>

                    <p class="stock-readonly__notice">
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8v5" /><path d="M12 17h.01" /><circle cx="12" cy="12" r="9" /></svg>
                      Эти значения сохраняются только в Seller. Отправка остатков и лимитов в маркетплейс не выполняется.
                    </p>
                  </div>
                  <div v-else-if="section.id === 'instruction'" class="product-instruction">
                    <section class="product-instruction__content" :class="{ 'product-instruction__content--empty': !hasActivationInstruction }">
                      <span class="product-instruction__icon" aria-hidden="true">
                        <svg viewBox="0 0 24 24"><path d="M3.5 5.5c2.8-.8 5.6-.2 8.5 1.7v12c-2.9-1.9-5.7-2.5-8.5-1.7z" /><path d="M20.5 5.5c-2.8-.8-5.6-.2-8.5 1.7v12c2.9-1.9 5.7-2.5 8.5-1.7z" /></svg>
                      </span>
                      <label class="product-instruction__editor">
                        <span>Текст для покупателя</span>
                        <textarea v-model="settingsForm.activation_instruction" maxlength="10000" placeholder="Например: как активировать код и куда обратиться при затруднениях" />
                        <small>{{ instructionLength.toLocaleString('ru-RU') }} / 10 000</small>
                      </label>
                    </section>
                  </div>
                  <div v-else-if="section.id === 'delivery'" class="product-delivery">
                    <header class="product-delivery__summary">
                      <div>
                        <span>Порядок выдачи</span>
                        <strong>{{ deliveryPriority.join(' → ') }}</strong>
                        <p>Seller проверит включённые способы сверху вниз. Ручной ввод всегда остаётся последним безопасным вариантом.</p>
                      </div>
                      <span class="product-delivery__local-badge">Настройка без запуска</span>
                    </header>

                    <div class="product-delivery__methods">
                      <article class="product-delivery-method" :class="{ 'is-enabled': settingsForm.supplier_issue_enabled, 'is-expanded': openDeliveryMethod === 'supplier' }">
                        <div class="product-delivery-method__head product-delivery-method__head--expandable">
                          <span class="product-delivery-method__number">01</span>
                          <button class="product-delivery-method__open" type="button" :aria-expanded="openDeliveryMethod === 'supplier'" @click="toggleDeliveryMethod('supplier')">
                            <span class="product-delivery-method__icon" aria-hidden="true">
                              <svg viewBox="0 0 24 24"><path d="M4 8.5 12 4l8 4.5v8L12 21l-8-4.5z" /><path d="m4 8.5 8 4.5 8-4.5M12 13v8" /></svg>
                            </span>
                            <span class="product-delivery-method__copy">
                              <strong>Автовыдача от поставщика</strong>
                              <small>{{ supplierServiceSummary }}</small>
                            </span>
                            <svg class="product-delivery-method__chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 9 5 5 5-5" /></svg>
                          </button>
                          <label class="product-delivery-switch" :class="{ 'is-active': settingsForm.supplier_issue_enabled, 'is-disabled': !supplierMappingComplete || !supplierCurrentPriceLabel }" title="Использовать Supplier Hub первым способом">
                            <input v-model="settingsForm.supplier_issue_enabled" type="checkbox" :disabled="!supplierMappingComplete || !supplierCurrentPriceLabel" />
                            <span aria-hidden="true"></span>
                            <span class="sr-only">Использовать автовыдачу от поставщика</span>
                          </label>
                        </div>
                        <div v-if="openDeliveryMethod === 'supplier'" class="product-delivery-method__panel product-delivery-supplier">
                          <div class="product-delivery-supplier__fields">
                            <label class="product-delivery-supplier__service">
                              <span>Товар Interhub</span>
                              <div class="supplier-combobox">
                                <svg class="supplier-combobox__search" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m16 16 4 4" /></svg>
                                <input
                                  v-model="supplierSearch"
                                  type="search"
                                  autocomplete="off"
                                  :placeholder="supplierServicesLoading ? 'Загружаем товары…' : 'Найдите товар или регион'"
                                  :disabled="supplierServicesLoading"
                                  @focus="supplierPickerOpen = true"
                                  @input="supplierPickerOpen = true"
                                />
                                <button v-if="settingsForm.supplier_service_id" class="supplier-combobox__clear" type="button" aria-label="Очистить выбранный товар" @click="clearSupplierService">×</button>
                                <svg class="supplier-combobox__chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 9 5 5 5-5" /></svg>
                                <div v-if="supplierPickerOpen && !supplierServicesLoading" class="supplier-combobox__menu">
                                  <button
                                    v-for="service in filteredSupplierServices"
                                    :key="service.service_id"
                                    type="button"
                                    :class="{ 'is-selected': Number(service.service_id) === Number(settingsForm.supplier_service_id) }"
                                    @mousedown.prevent="selectSupplierService(service)"
                                  >
                                    <strong>{{ supplierServiceDisplay(service) }}</strong>
                                    <small>{{ service.category || `Interhub · услуга #${service.service_id}` }}</small>
                                  </button>
                                  <p v-if="!filteredSupplierServices.length">По вашему запросу ничего не найдено</p>
                                </div>
                              </div>
                            </label>
                            <label v-if="supplierNominalField" class="product-delivery-supplier__nominal">
                              <span>Номинал</span>
                              <select v-if="supplierNominalOptions.length" v-model="settingsForm.supplier_nominal_id" @change="handleSupplierNominalChange">
                                <option value="" disabled>Выберите номинал</option>
                                <option v-for="nominal in supplierNominalOptions" :key="nominal.id" :value="nominal.id">{{ nominal.title }}</option>
                              </select>
                              <input v-else v-model="settingsForm.supplier_nominal_id" type="text" maxlength="128" placeholder="Укажите номинал" @change="handleSupplierNominalChange" />
                            </label>
                          </div>
                          <p v-if="supplierServicesError" class="supplier-inline-error">{{ supplierServicesError }}</p>
                          <p v-if="supplierQuoteError" class="supplier-inline-error">{{ supplierQuoteError }}</p>
                          <p v-if="supplierQuoteLoading">Актуальная цена: <strong>уточняем…</strong></p>
                          <p v-else-if="supplierCurrentPriceLabel">Актуальная цена: <strong>{{ supplierCurrentPriceLabel }} ₽</strong></p>
                        </div>
                      </article>

                      <article class="product-delivery-method" :class="{ 'is-enabled': settingsForm.pool_issue_enabled, 'is-expanded': openDeliveryMethod === 'pool' }">
                        <div class="product-delivery-method__head product-delivery-method__head--expandable">
                          <span class="product-delivery-method__number">02</span>
                          <button class="product-delivery-method__open" type="button" :aria-expanded="openDeliveryMethod === 'pool'" @click="toggleDeliveryMethod('pool')">
                            <span class="product-delivery-method__icon" aria-hidden="true">
                              <svg viewBox="0 0 24 24"><path d="M7 10a5 5 0 1 1 4.6 5" /><path d="m9 14-6 6M5 18l2 2M8 15l2 2" /></svg>
                            </span>
                            <span class="product-delivery-method__copy">
                              <strong>Список ключей</strong>
                              <small>{{ keyPoolLoading ? 'Проверяем пул…' : `${keyCountLabel(keyPool.free_count || 0)} доступно · ${keyCountLabel(keyPool.total || 0)} всего` }}</small>
                            </span>
                            <svg class="product-delivery-method__chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 9 5 5 5-5" /></svg>
                          </button>
                          <label class="product-delivery-switch" :class="{ 'is-active': settingsForm.pool_issue_enabled }" title="Использовать сохранённый пул вторым способом">
                            <input v-model="settingsForm.pool_issue_enabled" type="checkbox" />
                            <span aria-hidden="true"></span>
                            <span class="sr-only">Использовать список ключей</span>
                          </label>
                        </div>

                        <div v-if="openDeliveryMethod === 'pool'" class="product-delivery-method__panel product-key-pool">
                          <div class="product-key-pool__stats" aria-label="Состояние пула ключей">
                            <section class="product-key-pool__stat product-key-pool__stat--free">
                              <span>Доступно</span><strong>{{ keyPool.free_count || 0 }}</strong><small>готовы к будущей выдаче</small>
                            </section>
                            <section class="product-key-pool__stat">
                              <span>В резерве</span><strong>{{ keyPool.reserved_count || 0 }}</strong><small>включая отправляемые</small>
                            </section>
                            <section class="product-key-pool__stat">
                              <span>Выдано</span><strong>{{ keyPool.delivered_count || 0 }}</strong><small>история пула</small>
                            </section>
                            <section class="product-key-pool__stat">
                              <span>Всего</span><strong>{{ keyPool.total || 0 }}</strong><small>сохранено в Seller</small>
                            </section>
                          </div>

                          <form v-if="keyPoolCanManage" class="product-key-pool__form" @submit.prevent="submitKeyPool">
                            <div class="product-key-pool__form-copy">
                              <span class="product-key-pool__form-icon" aria-hidden="true">
                                <svg viewBox="0 0 24 24"><path d="M7 10a5 5 0 1 1 4.6 5" /><path d="m9 14-6 6M5 18l2 2M8 15l2 2" /></svg>
                              </span>
                              <div><strong>Добавить ключи</strong><p>По одному ключу на строку. Повторы будут пропущены.</p></div>
                            </div>
                            <label class="product-key-pool__codes">
                              <span>Ключи</span>
                              <textarea v-model="keyPoolForm.codes_raw" :disabled="keyPoolSaving" spellcheck="false" autocomplete="off" placeholder="AAAA-BBBB-CCCC&#10;DDDD-EEEE-FFFF" />
                              <small>{{ keyCountLabel(keyPoolDraftCodes.length) }} подготовлено</small>
                            </label>
                            <label class="product-key-pool__expiry">
                              <span>Срок действия</span>
                              <input v-model="keyPoolForm.expires_at" :disabled="keyPoolSaving" type="date" />
                              <small>Можно оставить пустым</small>
                            </label>
                            <button class="product-key-pool__submit" type="submit" :disabled="keyPoolSaving || !keyPoolDraftCodes.length">
                              <span v-if="keyPoolSaving" class="product-settings-save__spinner" aria-hidden="true"></span>
                              <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
                              {{ keyPoolSaving ? 'Сохраняем…' : 'Добавить в пул' }}
                            </button>
                          </form>
                          <p v-else class="product-key-pool__readonly">Ваша роль позволяет просматривать пул, но не добавлять ключи.</p>

                          <p v-if="keyPoolFormError || keyPoolError" class="product-key-pool__message product-key-pool__message--error" role="alert">{{ keyPoolFormError || keyPoolError }}</p>
                          <p v-else-if="keyPoolNotice" class="product-key-pool__message product-key-pool__message--ok" role="status">{{ keyPoolNotice }}</p>

                          <div v-if="keyPoolLoading && !keyPool.items?.length" class="product-key-pool__state" aria-live="polite">
                            <span class="product-orders__spinner" aria-hidden="true"></span><span>Загружаем пул…</span>
                          </div>
                          <div v-else-if="!keyPool.items?.length" class="product-key-pool__state product-key-pool__state--empty">
                            <span>Пока нет сохранённых ключей</span><small>Добавьте первую пачку или перенесите свободный пул из CRM.</small>
                          </div>
                          <template v-else>
                            <div class="product-key-pool__list-head">
                              <strong>Сохранённые ключи</strong><span>Открытые значения не загружаются в браузер</span>
                            </div>
                            <div class="product-key-pool__list">
                              <article v-for="key in keyPool.items" :key="key.id" class="product-key-pool__item">
                                <code>{{ key.masked_code }}</code>
                                <span class="product-key-pool__status" :class="`product-key-pool__status--${key.status}`">{{ keyStatusLabel(key.status) }}</span>
                                <time :datetime="key.created_at">{{ formatOrderDate(key.created_at) }}</time>
                                <small>{{ key.expires_at ? `Действует до ${key.expires_at}` : 'Без срока действия' }}</small>
                              </article>
                            </div>
                            <div v-if="keyPoolPageCount > 1" class="product-key-pool__pager">
                              <button type="button" :disabled="keyPoolLoading || keyPool.page <= 1" @click="emit('load-key-pool', keyPool.page - 1)">Назад</button>
                              <span>{{ keyPool.page }} / {{ keyPoolPageCount }}</span>
                              <button type="button" :disabled="keyPoolLoading || keyPool.page >= keyPoolPageCount" @click="emit('load-key-pool', keyPool.page + 1)">Вперёд</button>
                            </div>
                          </template>

                          <p class="stock-readonly__notice">
                            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8v5" /><path d="M12 17h.01" /><circle cx="12" cy="12" r="9" /></svg>
                            Включение способа сохраняет приоритет карточки, но не запускает выдачу и ничего не отправляет в Яндекс.
                          </p>
                        </div>
                      </article>

                      <article class="product-delivery-method" :class="{ 'is-enabled': settingsForm.support_message_delivery_enabled, 'is-expanded': openDeliveryMethod === 'support' }">
                        <div class="product-delivery-method__head product-delivery-method__head--expandable">
                          <span class="product-delivery-method__number">03</span>
                          <button class="product-delivery-method__open" type="button" :aria-expanded="openDeliveryMethod === 'support'" @click="toggleDeliveryMethod('support')">
                            <span class="product-delivery-method__icon" aria-hidden="true">
                              <svg viewBox="0 0 24 24"><path d="M4 5h16v11H8l-4 3z" /><path d="M8 9h8M8 12h5" /></svg>
                            </span>
                            <span class="product-delivery-method__copy">
                              <strong>Выдача через поддержку</strong>
                              <small>{{ settingsForm.support_message.trim() ? 'Сообщение покупателю заполнено' : 'Нужно заполнить сообщение покупателю' }}</small>
                            </span>
                            <svg class="product-delivery-method__chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 9 5 5 5-5" /></svg>
                          </button>
                          <label class="product-delivery-switch" :class="{ 'is-active': settingsForm.support_message_delivery_enabled, 'is-disabled': !settingsForm.support_message.trim() }" :title="settingsForm.support_message.trim() ? 'Использовать сообщение после списка ключей' : 'Сначала заполните сообщение'">
                            <input v-model="settingsForm.support_message_delivery_enabled" type="checkbox" :disabled="!settingsForm.support_message.trim()" />
                            <span aria-hidden="true"></span>
                            <span class="sr-only">Использовать выдачу через поддержку</span>
                          </label>
                        </div>
                        <div v-if="openDeliveryMethod === 'support'" class="product-delivery-method__panel product-delivery-support">
                          <label>
                            <span>Сообщение покупателю</span>
                            <textarea v-model="settingsForm.support_message" maxlength="2000" placeholder="Например: чтобы получить код, напишите в поддержку заказа" />
                            <small>{{ supportMessageLength.toLocaleString('ru-RU') }} / 2 000</small>
                          </label>
                          <p>Текст хранится как шаблон карточки. Он попадёт в выдачу только при включённом способе.</p>
                        </div>
                      </article>

                      <article class="product-delivery-method is-manual">
                        <div class="product-delivery-method__head">
                          <span class="product-delivery-method__number">04</span>
                          <span class="product-delivery-method__icon" aria-hidden="true">
                            <svg viewBox="0 0 24 24"><path d="M4 20h4l11-11-4-4L4 16z" /><path d="m13 7 4 4" /></svg>
                          </span>
                          <div class="product-delivery-method__copy">
                            <strong>Ручной ввод</strong>
                            <small>Заказ попадёт оператору, если предыдущие способы не дали ключ</small>
                          </div>
                          <span class="product-delivery-method__status is-safe">Всегда последний</span>
                        </div>
                      </article>
                    </div>

                    <p class="product-delivery__notice">
                      Сейчас сохраняется только политика карточки. Общие переключатели Seller остаются выключены, поэтому автоматическая выдача не начнётся.
                    </p>
                  </div>
                  <div v-else class="product-orders">
                    <div class="product-orders__toolbar">
                      <div><strong>Заказы товара</strong><span>Обновление выполняется по магазину</span></div>
                      <button type="button" :disabled="ordersLoading || ordersRefreshing || !ordersRefreshEnabled" :title="ordersRefreshEnabled ? 'Получить свежие заказы магазина и обновить список этой карточки' : 'Магазин отключён: доступен только сохранённый снимок'" @click="emit('refresh-orders')">
                        <svg :class="{ 'is-spinning': ordersRefreshing }" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.7" /><path d="M20 4v6h-6" /></svg>
                        <span>{{ ordersRefreshing ? 'Обновляем' : 'Обновить' }}</span>
                      </button>
                    </div>
                    <div v-if="ordersLoading && !orders.length" class="product-orders__state" aria-live="polite" aria-busy="true">
                      <span class="product-orders__spinner" aria-hidden="true"></span>
                      <div><strong>Загружаем заказы</strong><p>Читаем сохранённые позиции этой карточки.</p></div>
                    </div>
                    <div v-else-if="ordersError && !orders.length" class="product-orders__state product-orders__state--error" role="alert">
                      <div><strong>Не удалось загрузить заказы</strong><p>{{ ordersError }}</p></div>
                    </div>
                    <div v-else-if="!orders.length" class="product-orders__state product-orders__state--empty">
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4h10v16H7z" /><path d="M9.5 8h5M9.5 12h5M9.5 16h3" /></svg>
                      <div><strong>Заказов пока нет</strong><p>В локальном снимке не найдено продаж этой карточки.</p></div>
                    </div>
                    <template v-else>
                      <header class="product-orders__summary">
                        <strong>{{ orderCountLabel(ordersTotal) }}</strong>
                        <span v-if="ordersRefreshing">Получаем свежие данные…</span>
                        <span v-else-if="ordersTotal > orders.length">Показаны последние {{ orders.length }}</span>
                        <span v-else>Все найденные заказы</span>
                      </header>
                      <p v-if="ordersError" class="product-orders__inline-error" role="alert">{{ ordersError }}</p>
                      <div class="product-orders__list">
                        <article v-for="order in orders" :key="`${order.external_order_id}-${order.external_item_id}`" class="product-order">
                          <div class="product-order__head">
                            <strong>Заказ №{{ order.external_order_id }}</strong>
                            <span class="product-order__status" :class="`product-order__status--${order.status}`">{{ orderStatusLabel(order.status) }}</span>
                          </div>
                          <div class="product-order__meta">
                            <time :datetime="order.updated_at || order.created_at || order.synced_at">{{ formatOrderDate(order.updated_at || order.created_at || order.synced_at) }}</time>
                            <span>Количество: <strong>{{ order.quantity }}</strong></span>
                          </div>
                        </article>
                      </div>
                    </template>
                    <p class="product-orders__notice">Seller показывает заказы из локального снимка. Новых запросов к маркетплейсу при открытии карточки нет.</p>
                  </div>
                </div>
              </section>
            </div>

            <section class="product-settings-savebar" :class="{ 'is-dirty': settingsDirty }" aria-live="polite">
              <div class="product-settings-savebar__copy">
                <span class="product-settings-savebar__mark" aria-hidden="true">
                  <svg viewBox="0 0 24 24"><path d="M6 4h10l3 3v13H6z" /><path d="M9 4v6h7V4M9 16h7" /></svg>
                </span>
                <div>
                  <strong>{{ settingsDirty ? 'Есть несохранённые изменения' : settingsNotice || (item.settings_saved_at ? 'Локальные настройки сохранены' : 'Готово к настройке') }}</strong>
                  <p v-if="settingsFormError || settingsError" class="product-settings-savebar__error">{{ settingsFormError || settingsError }}</p>
                  <p v-else>{{ item.settings_saved_at ? 'Последнее сохранение находится в Seller' : 'Маркетплейс не получит эти значения' }}</p>
                </div>
              </div>
              <div class="product-settings-savebar__actions">
                <button v-if="settingsDirty" class="product-settings-reset" type="button" :disabled="settingsSaving" @click="resetSettingsForm">Отменить изменения</button>
                <button class="product-settings-save" type="button" :disabled="!settingsDirty || settingsSaving" @click="saveSettings">
                  <span v-if="settingsSaving" class="product-settings-save__spinner" aria-hidden="true"></span>
                  <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h10l3 3v13H6z" /><path d="M9 4v6h7V4M9 16h7" /></svg>
                  <span>{{ settingsSaving ? 'Сохраняем…' : 'Сохранить в Seller' }}</span>
                </button>
              </div>
            </section>

            <footer class="product-card-modal__footer">
              <span>{{ item.stock_synced_at ? 'Остаток проверен' : 'Последнее обновление снимка' }}</span>
              <time :datetime="item.stock_synced_at || item.synced_at">{{ stockSyncedLabel || syncedLabel }}</time>
            </footer>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.product-card-backdrop {
  position: fixed;
  z-index: 20;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(2, 6, 19, .76);
  backdrop-filter: blur(10px);
}

.product-card-modal {
  width: min(100%, 900px);
  max-height: calc(100vh - 48px);
  overflow: hidden;
  padding: 0;
  border: 1px solid rgba(137, 159, 211, .32);
  border-radius: 27px;
  background: radial-gradient(circle at 96% -10%, rgba(54, 90, 213, .19), transparent 32%), linear-gradient(145deg, #14203d, #0a1128);
  box-shadow: 0 36px 120px rgba(0, 0, 0, .55), 0 0 75px rgba(34, 72, 196, .12);
}

.product-card-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 22px;
  padding: 20px 22px;
  border-bottom: 1px solid rgba(137, 159, 211, .19);
  background: rgba(10, 17, 38, .54);
}

.product-card-modal__header > h2 {
  min-width: 0;
  margin: 0;
  color: #f2f5ff;
  font-size: clamp(18px, 2.4vw, 23px);
  line-height: 1.15;
  letter-spacing: -.035em;
}

.product-card-modal__head-actions {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
}

.product-card-modal__mode {
  margin-right: 3px;
  padding: 7px 10px;
  border: 1px solid rgba(120, 148, 224, .3);
  border-radius: 999px;
  color: #9eafd5;
  background: rgba(34, 52, 94, .54);
  font-size: 9px;
  font-weight: 850;
  letter-spacing: .08em;
  text-transform: uppercase;
}

.product-card-modal__archive-state {
  padding: 7px 10px;
  border: 1px solid rgba(116, 142, 210, .34);
  border-radius: 999px;
  color: #c2cce2;
  background: rgba(24, 36, 68, .58);
  font-size: 9px;
  font-weight: 900;
  letter-spacing: .08em;
  text-transform: uppercase;
}

.product-card-modal__head-actions button {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  padding: 0;
  border: 1px solid rgba(126, 151, 217, .3);
  border-radius: 11px;
  color: #a8b6d5;
  background: rgba(24, 36, 68, .72);
  transition: color .16s, border-color .16s, background .16s;
}

.product-card-modal__head-actions button:hover {
  color: #fff;
  border-color: rgba(91, 123, 255, .65);
  background: rgba(49, 80, 186, .4);
}

.product-card-modal__head-actions .product-card-modal__close:hover {
  border-color: rgba(255, 150, 155, .5);
  color: #ffaaa8;
  background: rgba(255, 150, 155, .08);
}

.product-card-modal__head-actions svg {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.product-card-modal__body {
  max-height: calc(100vh - 130px);
  overflow: auto;
  padding: 22px;
}

.product-overview {
  min-width: 0;
}

.product-overview.has-image {
  display: grid;
  grid-template-columns: minmax(175px, 235px) minmax(0, 1fr);
  align-items: start;
  gap: 17px;
}

.product-overview__info {
  min-width: 0;
}

.product-overview__image-wrap {
  display: flex;
  min-height: 210px;
  align-items: center;
  justify-content: center;
  padding: 14px;
  border: 1px solid rgba(137, 159, 211, .23);
  border-radius: 14px;
  background: rgba(16, 25, 44, .5);
}

.product-overview__image-wrap > img {
  display: block;
  width: 100%;
  max-width: 210px;
  max-height: 285px;
  border-radius: 10px;
  object-fit: contain;
}

.product-overview__source {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 12px;
  color: #9faccc;
  font-size: 12px;
  font-weight: 750;
}

.product-overview__source small {
  padding: 3px 7px;
  border: 1px solid rgba(124, 147, 207, .3);
  border-radius: 999px;
  color: #9aaacb;
  font-size: 9px;
  letter-spacing: .04em;
  text-transform: uppercase;
}

.product-overview__logo {
  display: inline-grid;
  width: 31px;
  height: 31px;
  place-items: center;
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, .16);
  border-radius: 9px;
  background: #fff;
}

.product-overview__logo img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.product-overview__logo--ozon img {
  transform: scale(1.32);
}

.product-overview h3 {
  margin: 0 0 15px;
  color: #f4f7ff;
  font-size: 19px;
  line-height: 1.35;
}

.product-overview__grid {
  display: grid;
  grid-template-columns: minmax(155px, .75fr) minmax(0, 1.45fr);
  margin: 0;
  overflow: hidden;
  border: 1px solid rgba(137, 159, 211, .23);
  border-radius: 14px;
}

.product-overview__grid dt,
.product-overview__grid dd {
  margin: 0;
  padding: 11px 13px;
  border-bottom: 1px solid rgba(137, 159, 211, .18);
}

@keyframes product-stock-spin {
  to { transform: rotate(360deg); }
}

.stock-readonly__error {
  margin: 7px 0 0;
  color: #ffaaa8;
  font-size: 11px;
  line-height: 1.4;
}

.product-overview__grid dt {
  color: #96a4c2;
  background: rgba(255, 255, 255, .025);
}

.product-overview__grid dd {
  min-width: 0;
  color: #e7ecf8;
  word-break: break-word;
}

.product-overview__grid dt:last-of-type,
.product-overview__grid dd:last-child {
  border-bottom: 0;
}

.product-work-blocks {
  display: grid;
  gap: 10px;
  margin-top: 15px;
}

.product-work-block {
  overflow: hidden;
  border: 1px solid rgba(122, 147, 191, .22);
  border-radius: 15px;
  background: rgba(11, 21, 41, .56);
  transition: border-color .16s, background .16s;
}

.product-work-block.is-open {
  border-color: rgba(83, 229, 186, .34);
  background: rgba(83, 229, 186, .035);
}

.product-work-block__toggle {
  display: grid;
  width: 100%;
  min-height: 72px;
  grid-template-columns: 42px minmax(0, 1fr) 22px;
  align-items: center;
  gap: 12px;
  padding: 13px 16px;
  border: 0;
  color: #edf1ff;
  background: transparent;
  text-align: left;
}

.product-work-block__toggle:hover {
  background: rgba(83, 229, 186, .055);
}

.product-work-block__number {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border: 1px solid rgba(83, 229, 186, .28);
  border-radius: 11px;
  color: #53e5ba;
  background: rgba(83, 229, 186, .09);
  font-size: 12px;
  font-weight: 850;
  letter-spacing: .04em;
}

.product-work-block__copy {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.product-work-block__copy strong {
  font-size: 15px;
  line-height: 1.15;
}

.product-work-block__copy small {
  color: #96a4c2;
  font-size: 11px;
  line-height: 1.25;
}

.product-work-block__chevron {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: #96a4c2;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform .16s, stroke .16s;
}

.product-work-block.is-open .product-work-block__chevron {
  stroke: #53e5ba;
  transform: rotate(180deg);
}

.product-work-block__body {
  padding: 15px 16px 16px;
  border-top: 1px solid rgba(122, 147, 191, .15);
}

.product-work-block__placeholder {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px;
  border: 1px dashed rgba(126, 151, 217, .26);
  border-radius: 13px;
  background: rgba(8, 15, 34, .4);
}

.product-work-block__placeholder > svg {
  width: 20px;
  height: 20px;
  flex: 0 0 auto;
  fill: none;
  stroke: #7f91bd;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.product-work-block__placeholder strong {
  color: #cbd5eb;
  font-size: 13px;
}

.product-work-block__placeholder p {
  margin: 4px 0 0;
  color: #8998b9;
  font-size: 12px;
  line-height: 1.5;
}

.product-orders {
  display: grid;
  gap: 10px;
}

.product-orders__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 11px 12px;
  border: 1px solid rgba(83, 229, 186, .2);
  border-radius: 12px;
  background: linear-gradient(145deg, rgba(83, 229, 186, .06), rgba(8, 15, 34, .42));
}

.product-orders__toolbar > div {
  display: grid;
  gap: 2px;
}

.product-orders__toolbar > div > strong {
  color: #dfe8f9;
  font-size: 12px;
}

.product-orders__toolbar > div > span {
  color: #7f91b3;
  font-size: 9px;
}

.product-orders__toolbar > button {
  display: inline-flex;
  min-height: 32px;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
  padding: 0 10px;
  border: 1px solid rgba(83, 229, 186, .32);
  border-radius: 9px;
  color: #72e7c5;
  background: rgba(83, 229, 186, .08);
  font-size: 10px;
  font-weight: 850;
}

.product-orders__toolbar > button:hover:not(:disabled) {
  border-color: rgba(83, 229, 186, .62);
  color: #b6f9e6;
  background: rgba(83, 229, 186, .14);
}

.product-orders__toolbar > button svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.product-orders__toolbar > button svg.is-spinning {
  animation: product-stock-spin .8s linear infinite;
}

.product-orders__summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 0 2px 2px;
}

.product-orders__summary > strong {
  color: #edf2ff;
  font-size: 13px;
}

.product-orders__summary > span {
  color: #8191b3;
  font-size: 10px;
}

.product-orders__list {
  display: grid;
  max-height: 340px;
  gap: 8px;
  overflow: auto;
  padding-right: 3px;
  scrollbar-color: rgba(113, 138, 201, .42) transparent;
  scrollbar-width: thin;
}

.product-order {
  display: grid;
  gap: 9px;
  padding: 12px 13px;
  border: 1px solid rgba(126, 151, 217, .2);
  border-radius: 12px;
  background: linear-gradient(145deg, rgba(23, 37, 72, .76), rgba(8, 15, 34, .55));
}

.product-order__head,
.product-order__meta {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}

.product-order__head > strong {
  overflow: hidden;
  color: #e8edfb;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.product-order__status {
  flex: 0 0 auto;
  padding: 4px 7px;
  border: 1px solid currentColor;
  border-radius: 999px;
  color: #ffc75a;
  background: rgba(255, 199, 90, .07);
  font-size: 8px;
  font-weight: 900;
  letter-spacing: .035em;
  text-transform: uppercase;
}

.product-order__status--in_delivery {
  color: #65b5ff;
  background: rgba(101, 181, 255, .07);
}

.product-order__status--delivered {
  color: #4ee6bd;
  background: rgba(78, 230, 189, .07);
}

.product-order__status--cancelled,
.product-order__status--problem {
  color: #ff969b;
  background: rgba(255, 150, 155, .07);
}

.product-order__meta {
  padding-top: 8px;
  border-top: 1px dashed rgba(150, 171, 217, .15);
  color: #8f9fbe;
  font-size: 10px;
}

.product-order__meta strong {
  color: #dbe4f8;
}

.product-orders__state {
  display: flex;
  min-height: 82px;
  align-items: center;
  gap: 12px;
  padding: 14px;
  border: 1px dashed rgba(126, 151, 217, .26);
  border-radius: 13px;
  background: rgba(8, 15, 34, .4);
}

.product-orders__state > svg {
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  fill: none;
  stroke: #7f91bd;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.product-orders__state strong {
  color: #cbd5eb;
  font-size: 12px;
}

.product-orders__state p {
  margin: 3px 0 0;
  color: #8998b9;
  font-size: 11px;
  line-height: 1.45;
}

.product-orders__state--error {
  border-color: rgba(255, 150, 155, .3);
  background: rgba(255, 150, 155, .06);
}

.product-orders__state--error strong,
.product-orders__state--error p {
  color: #ffaaa8;
}

.product-orders__spinner {
  width: 21px;
  height: 21px;
  flex: 0 0 auto;
  border: 2px solid rgba(105, 135, 212, .25);
  border-top-color: #6f91ff;
  border-radius: 50%;
  animation: product-stock-spin .8s linear infinite;
}

.product-orders__notice {
  margin: 0;
  padding: 9px 11px;
  border-radius: 9px;
  color: #7183aa;
  background: rgba(126, 151, 217, .045);
  font-size: 9px;
  line-height: 1.4;
}

.product-orders__inline-error {
  margin: 0;
  padding: 8px 10px;
  border: 1px solid rgba(255, 150, 155, .24);
  border-radius: 9px;
  color: #ffaaa8;
  background: rgba(255, 150, 155, .055);
  font-size: 10px;
  line-height: 1.4;
}

.stock-readonly {
  display: grid;
  gap: 12px;
}

.stock-readonly__fields {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.stock-readonly__field {
  display: grid;
  gap: 8px;
  padding: 14px;
  border: 1px solid rgba(126, 151, 217, .22);
  border-radius: 13px;
  background: rgba(8, 15, 34, .42);
}

.stock-readonly__field > span,
.stock-limit__heading > div > span {
  color: #96a4c2;
  font-size: 10px;
  font-weight: 850;
  letter-spacing: .07em;
  text-transform: uppercase;
}

.stock-readonly__field output {
  color: #eef3ff;
  font-size: 22px;
  font-weight: 850;
  line-height: 1.1;
}

.stock-readonly__field output.is-loading {
  color: #91a6d8;
  font-size: 15px;
}

.stock-readonly__field > small {
  color: #7f8fb0;
  font-size: 10px;
  line-height: 1.35;
}

.stock-settings__field input,
.stock-limit__extra input,
.product-instruction__editor textarea {
  width: 100%;
  border: 1px solid rgba(126, 151, 217, .28);
  outline: none;
  color: #eef3ff;
  background: rgba(5, 11, 26, .64);
  transition: border-color .16s, box-shadow .16s, background .16s;
}

.stock-settings__field > input,
.stock-limit__extra input {
  min-height: 39px;
  padding: 0 11px;
  border-radius: 9px;
  font-size: 15px;
  font-weight: 800;
}

.stock-settings__field input:focus,
.stock-limit__extra input:focus,
.product-instruction__editor textarea:focus {
  border-color: rgba(83, 125, 255, .8);
  background: rgba(9, 18, 43, .82);
  box-shadow: 0 0 0 3px rgba(75, 115, 255, .13);
}

.stock-limit-switch {
  display: flex;
  min-height: 30px;
  align-items: center;
  gap: 9px;
  cursor: pointer;
}

.stock-limit-switch > input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.stock-limit-switch > span {
  position: relative;
  width: 36px;
  height: 21px;
  flex: 0 0 auto;
  border: 1px solid rgba(126, 151, 217, .35);
  border-radius: 999px;
  background: rgba(34, 47, 78, .84);
  transition: border-color .16s, background .16s;
}

.stock-limit-switch > span::after {
  content: '';
  position: absolute;
  top: 3px;
  left: 3px;
  width: 13px;
  height: 13px;
  border-radius: 50%;
  background: #aeb9d4;
  transition: transform .16s, background .16s;
}

.stock-limit-switch > input:checked + span {
  border-color: rgba(83, 229, 186, .55);
  background: rgba(41, 151, 122, .34);
}

.stock-limit-switch > input:checked + span::after {
  background: #65e8c2;
  transform: translateX(15px);
}

.stock-limit-switch > input:focus-visible + span {
  box-shadow: 0 0 0 3px rgba(75, 115, 255, .2);
}

.stock-limit-switch strong {
  color: #dce5f7;
  font-size: 12px;
}

.stock-readonly__field--primary {
  border-color: rgba(83, 229, 186, .25);
  background: linear-gradient(145deg, rgba(83, 229, 186, .07), rgba(8, 15, 34, .42));
}

.stock-readonly__value-row {
  display: flex;
  min-height: 34px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.stock-readonly__refresh {
  display: inline-flex;
  min-height: 32px;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  border: 1px solid rgba(83, 229, 186, .32);
  border-radius: 9px;
  color: #72e7c5;
  background: rgba(83, 229, 186, .08);
  font-size: 10px;
  font-weight: 850;
}

.stock-readonly__refresh:hover:not(:disabled) {
  border-color: rgba(83, 229, 186, .62);
  color: #b6f9e6;
  background: rgba(83, 229, 186, .14);
}

.stock-readonly__refresh svg,
.stock-readonly__notice svg {
  width: 14px;
  height: 14px;
  flex: 0 0 auto;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.stock-readonly__refresh svg.is-spinning {
  animation: product-stock-spin .8s linear infinite;
}

.stock-limit {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid rgba(91, 123, 255, .28);
  border-radius: 13px;
  background: linear-gradient(145deg, rgba(48, 79, 185, .11), rgba(8, 15, 34, .42));
}

.stock-limit--pending {
  border-style: dashed;
  background: rgba(8, 15, 34, .42);
}

.stock-limit__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.stock-limit__heading > div {
  display: grid;
  gap: 4px;
}

.stock-limit__heading strong {
  color: #e9eefb;
  font-size: 13px;
}

.stock-limit__badge {
  padding: 5px 8px;
  border: 1px solid rgba(126, 151, 217, .24);
  border-radius: 999px;
  color: #92a2c5;
  background: rgba(30, 45, 79, .52);
  font-size: 8px;
  font-weight: 850;
  letter-spacing: .06em;
  text-transform: uppercase;
  white-space: nowrap;
}

.stock-limit__metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  overflow: hidden;
  border: 1px solid rgba(126, 151, 217, .16);
  border-radius: 10px;
}

.stock-limit__extra {
  display: grid;
  grid-template-columns: minmax(140px, .7fr) minmax(110px, .35fr) minmax(220px, 1fr);
  align-items: center;
  gap: 11px;
  padding: 10px 11px;
  border: 1px solid rgba(126, 151, 217, .16);
  border-radius: 10px;
  background: rgba(6, 13, 31, .34);
}

.stock-limit__extra > span {
  color: #c6d0e5;
  font-size: 11px;
  font-weight: 800;
}

.stock-limit__extra > small,
.stock-limit__metrics-note {
  color: #7f8fb0;
  font-size: 9px;
  line-height: 1.4;
}

.stock-limit__metrics-note {
  margin: -3px 0 0;
}

.stock-limit__metrics > div {
  display: grid;
  gap: 4px;
  padding: 10px;
  border-right: 1px solid rgba(126, 151, 217, .14);
}

.stock-limit__metrics > div:last-child {
  border-right: 0;
}

.stock-limit__metrics span {
  color: #7f8fb0;
  font-size: 9px;
}

.stock-limit__metrics strong {
  color: #dce5f7;
  font-size: 13px;
}

.product-instruction {
  display: grid;
  gap: 10px;
}

.product-instruction__content {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  align-items: start;
  gap: 12px;
  padding: 16px;
  border: 1px solid rgba(83, 125, 255, .34);
  border-radius: 13px;
  background: linear-gradient(145deg, rgba(39, 75, 190, .12), rgba(8, 15, 34, .46));
}

.product-instruction__editor {
  display: grid;
  gap: 7px;
}

.product-instruction__editor > span {
  color: #dce5f7;
  font-size: 12px;
  font-weight: 800;
}

.product-instruction__editor textarea {
  min-height: 155px;
  padding: 12px 13px;
  border-radius: 11px;
  font-size: 12px;
  line-height: 1.55;
  resize: vertical;
}

.product-instruction__editor small {
  justify-self: end;
  color: #7183aa;
  font-size: 9px;
}

.product-instruction__editor > p {
  margin: 0;
  color: #7889ad;
  font-size: 9px;
  line-height: 1.45;
}

.product-instruction__support-switch {
  display: inline-flex;
  width: fit-content;
  align-items: center;
  gap: 8px;
  color: #aab8d6;
  cursor: pointer;
}

.product-instruction__support-switch input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.product-instruction__support-switch > span {
  position: relative;
  width: 31px;
  height: 18px;
  border: 1px solid rgba(128, 151, 210, .35);
  border-radius: 999px;
  background: rgba(8, 15, 34, .72);
}

.product-instruction__support-switch > span::after {
  position: absolute;
  width: 12px;
  height: 12px;
  top: 2px;
  left: 2px;
  border-radius: 50%;
  background: #8e9bb8;
  content: '';
  transition: transform .16s ease, background .16s ease;
}

.product-instruction__support-switch input:checked + span {
  border-color: rgba(70, 112, 255, .7);
  background: rgba(41, 82, 222, .45);
}

.product-instruction__support-switch input:checked + span::after {
  background: #edf2ff;
  transform: translateX(13px);
}

.product-instruction__support-switch input:disabled ~ * {
  opacity: .45;
}

.product-instruction__support-switch strong {
  font-size: 10px;
}

.product-instruction__content--empty {
  border-color: rgba(126, 151, 217, .22);
  border-style: dashed;
  background: rgba(8, 15, 34, .42);
}

.product-instruction__icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  flex: 0 0 auto;
  border: 1px solid rgba(83, 125, 255, .5);
  border-radius: 10px;
  color: #86a0ff;
  background: rgba(39, 75, 190, .2);
}

.product-instruction__content--empty .product-instruction__icon {
  border-color: rgba(225, 233, 255, .3);
  color: #e5ebfa;
  background: rgba(255, 255, 255, .025);
}

.product-instruction__icon svg {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.product-instruction__content > p {
  min-height: 38px;
  margin: 5px 0 0;
  color: #cbd5eb;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
}

.stock-readonly__notice {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 0;
  padding: 10px 11px;
  border-radius: 10px;
  color: #7f91bd;
  background: rgba(126, 151, 217, .055);
  font-size: 10px;
  line-height: 1.45;
}

.product-settings-savebar {
  position: sticky;
  z-index: 3;
  bottom: -22px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 15px;
  padding: 12px 13px;
  border: 1px solid rgba(126, 151, 217, .24);
  border-radius: 15px;
  background: rgba(10, 18, 39, .96);
  box-shadow: 0 -12px 34px rgba(4, 8, 23, .22);
  backdrop-filter: blur(12px);
  transition: border-color .16s, box-shadow .16s;
}

.product-settings-savebar.is-dirty {
  border-color: rgba(83, 125, 255, .58);
  box-shadow: 0 -12px 36px rgba(4, 8, 23, .28), 0 0 0 1px rgba(75, 115, 255, .08) inset;
}

.product-settings-savebar__copy,
.product-settings-savebar__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.product-settings-savebar__copy {
  min-width: 0;
}

.product-settings-savebar__mark {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  flex: 0 0 auto;
  border: 1px solid rgba(126, 151, 217, .24);
  border-radius: 10px;
  color: #8fa2cf;
  background: rgba(30, 43, 76, .58);
}

.is-dirty .product-settings-savebar__mark {
  color: #86a0ff;
  border-color: rgba(83, 125, 255, .45);
  background: rgba(39, 75, 190, .16);
}

.product-settings-savebar__mark svg,
.product-settings-save svg {
  width: 17px;
  height: 17px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.product-settings-savebar__copy strong {
  color: #e8edf9;
  font-size: 12px;
}

.product-settings-savebar__copy p {
  margin: 3px 0 0;
  color: #7f8fb0;
  font-size: 9px;
}

.product-settings-savebar__copy .product-settings-savebar__error {
  color: #ffaaa8;
}

.product-settings-reset,
.product-settings-save {
  min-height: 38px;
  padding: 0 12px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 850;
}

.product-settings-reset {
  border: 1px solid rgba(149, 164, 203, .24);
  color: #aeb9d4;
  background: rgba(27, 38, 67, .62);
}

.product-settings-save {
  display: inline-flex;
  min-width: 145px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid rgba(75, 115, 255, .78);
  color: #fff;
  background: linear-gradient(135deg, #1748dc, #4b73ff);
  box-shadow: 0 9px 23px rgba(32, 77, 220, .22);
}

.product-settings-save__spinner {
  width: 15px;
  height: 15px;
  border: 2px solid rgba(255, 255, 255, .28);
  border-top-color: #fff;
  border-radius: 50%;
  animation: product-stock-spin .8s linear infinite;
}

.product-card-modal__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  margin-top: 18px;
  padding: 14px 1px 0;
  border-top: 1px dashed rgba(150, 171, 217, .2);
  color: #8998b9;
  font-size: 12px;
}

.product-card-modal__footer time {
  color: #c4cde1;
}

.product-card-enter-active,
.product-card-leave-active {
  transition: opacity .18s ease;
}

.product-card-enter-active .product-card-modal,
.product-card-leave-active .product-card-modal {
  transition: transform .2s ease, opacity .18s ease;
}

.product-card-enter-from,
.product-card-leave-to {
  opacity: 0;
}

.product-card-enter-from .product-card-modal,
.product-card-leave-to .product-card-modal {
  opacity: 0;
  transform: translateY(10px) scale(.985);
}

.product-delivery {
  display: grid;
  gap: 13px;
}

.product-delivery__summary {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding: 15px 16px;
  border: 1px solid rgba(83, 125, 255, .32);
  border-radius: 14px;
  background: radial-gradient(circle at 100% 0, rgba(70, 112, 255, .16), transparent 48%), rgba(8, 15, 34, .5);
}

.product-delivery__summary > div {
  display: grid;
  gap: 5px;
}

.product-delivery__summary span:first-child {
  color: #7f94c5;
  font-size: 8px;
  font-weight: 900;
  letter-spacing: .12em;
  text-transform: uppercase;
}

.product-delivery__summary strong {
  color: #edf2ff;
  font-size: 15px;
  line-height: 1.35;
}

.product-delivery__summary p {
  margin: 0;
  color: #8999bb;
  font-size: 10px;
  line-height: 1.45;
}

.product-delivery__local-badge {
  flex: 0 0 auto;
  padding: 7px 9px;
  border: 1px solid rgba(126, 151, 217, .28);
  border-radius: 999px;
  color: #a7b5d2 !important;
  background: rgba(26, 39, 72, .72);
  font-size: 8px !important;
  letter-spacing: .07em !important;
  white-space: nowrap;
}

.product-delivery__methods {
  display: grid;
  gap: 8px;
}

.product-delivery-method {
  overflow: hidden;
  border: 1px solid rgba(126, 151, 217, .21);
  border-radius: 15px;
  background: rgba(8, 15, 34, .48);
  transition: border-color .18s, background .18s, box-shadow .18s;
}

.product-delivery-method.is-enabled {
  border-color: rgba(83, 125, 255, .52);
  background: linear-gradient(145deg, rgba(34, 70, 181, .12), rgba(8, 15, 34, .54));
}

.product-delivery-method.is-expanded {
  position: relative;
  z-index: 3;
  overflow: visible;
  box-shadow: 0 16px 38px rgba(1, 5, 17, .18);
}

.product-delivery-method.is-future {
  border-style: dashed;
  opacity: .72;
}

.product-delivery-method.is-manual {
  border-color: rgba(126, 151, 217, .16);
}

.product-delivery-method__head {
  display: grid;
  grid-template-columns: 42px 36px minmax(0, 1fr) auto auto;
  min-height: 68px;
  align-items: center;
  gap: 11px;
  padding: 11px 13px;
}

.product-delivery-method__head--expandable {
  grid-template-columns: 42px minmax(0, 1fr) auto;
}

.product-delivery-method__head--expandable .product-delivery-method__open {
  grid-column: 2;
}

.product-delivery-method__head--expandable .product-delivery-switch {
  grid-column: 3;
}

.product-delivery-method__number {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border: 1px solid rgba(74, 213, 180, .38);
  border-radius: 11px;
  color: #52dfbd;
  background: rgba(35, 137, 121, .12);
  font: 900 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
}

.product-delivery-method__icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid rgba(126, 151, 217, .25);
  border-radius: 10px;
  color: #93a8d8;
  background: rgba(31, 45, 78, .6);
}

.product-delivery-method.is-enabled .product-delivery-method__icon {
  color: #87a0ff;
  border-color: rgba(83, 125, 255, .48);
  background: rgba(39, 75, 190, .18);
}

.product-delivery-method__icon svg,
.product-delivery-method__chevron {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.product-delivery-method__copy {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.product-delivery-method__copy strong {
  color: #dfe7f8;
  font-size: 12px;
}

.product-delivery-method__copy small {
  overflow: hidden;
  color: #8292b3;
  font-size: 9px;
  line-height: 1.4;
  text-overflow: ellipsis;
}

.product-delivery-method__open {
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr) 18px;
  min-width: 0;
  align-items: center;
  gap: 11px;
  padding: 0;
  border: 0;
  color: #91a3ca;
  background: transparent;
  text-align: left;
}

.product-delivery-method__chevron {
  transition: transform .18s;
}

.product-delivery-method.is-expanded .product-delivery-method__chevron {
  transform: rotate(180deg);
}

.product-delivery-method__status {
  padding: 6px 8px;
  border: 1px solid rgba(126, 151, 217, .22);
  border-radius: 999px;
  color: #91a0be;
  background: rgba(30, 42, 72, .66);
  font-size: 8px;
  font-weight: 850;
  white-space: nowrap;
}

.product-delivery-method__status.is-safe {
  color: #9fb1d7;
}

.product-delivery-switch {
  position: relative;
  display: inline-flex;
  width: 38px;
  height: 22px;
  flex: 0 0 auto;
  cursor: pointer;
}

.product-delivery-switch input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.product-delivery-switch > span:first-of-type {
  position: relative;
  width: 38px;
  height: 22px;
  border: 1px solid rgba(126, 151, 217, .34);
  border-radius: 999px;
  background: rgba(40, 51, 80, .9);
  transition: border-color .16s, background .16s;
}

.product-delivery-switch > span:first-of-type::after {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #8a96b0;
  content: '';
  transition: transform .16s, background .16s;
}

.product-delivery-switch.is-active > span:first-of-type {
  border-color: rgba(69, 108, 255, .72);
  background: rgba(36, 78, 221, .68);
}

.product-delivery-switch.is-active > span:first-of-type::after {
  background: #f1f4ff;
  transform: translateX(16px);
}

.product-delivery-switch.is-disabled {
  cursor: not-allowed;
  opacity: .48;
}

.product-delivery-method__panel {
  padding: 14px;
  border-top: 1px solid rgba(126, 151, 217, .15);
  background: rgba(5, 11, 27, .28);
}

.product-delivery-supplier {
  display: grid;
  gap: 10px;
}

.product-delivery-supplier__fields {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(220px, 1fr);
  gap: 10px;
}

.product-delivery-supplier label {
  display: grid;
  gap: 7px;
}

.product-delivery-supplier label > span {
  color: #dce5f7;
  font-size: 11px;
  font-weight: 800;
}

.product-delivery-supplier input,
.product-delivery-supplier select {
  min-width: 0;
  min-height: 42px;
  padding: 0 11px;
  border: 1px solid rgba(126, 151, 217, .28);
  border-radius: 11px;
  outline: none;
  color: #eef3ff;
  background: rgba(5, 11, 26, .72);
}

.product-delivery-supplier select {
  width: 100%;
}

.product-delivery-supplier input:focus,
.product-delivery-supplier select:focus {
  border-color: rgba(83, 125, 255, .78);
}

.supplier-combobox {
  position: relative;
}

.supplier-combobox > input {
  width: 100%;
  padding-right: 68px;
  padding-left: 38px;
}

.supplier-combobox > input::-webkit-search-cancel-button {
  display: none;
}

.supplier-combobox__search,
.supplier-combobox__chevron {
  position: absolute;
  z-index: 2;
  top: 50%;
  width: 17px;
  height: 17px;
  fill: none;
  stroke: #91a1c5;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 2;
  pointer-events: none;
  transform: translateY(-50%);
}

.supplier-combobox__search {
  left: 13px;
}

.supplier-combobox__chevron {
  right: 12px;
}

.supplier-combobox__clear {
  position: absolute;
  z-index: 3;
  top: 50%;
  right: 36px;
  display: grid;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  place-items: center;
  color: #aab6d0;
  background: rgba(139, 153, 185, .16);
  cursor: pointer;
  transform: translateY(-50%);
}

.supplier-combobox__clear:hover {
  color: #eef3ff;
  background: rgba(139, 153, 185, .28);
}

.supplier-combobox__menu {
  position: absolute;
  z-index: 20;
  top: calc(100% + 7px);
  right: 0;
  left: 0;
  display: grid;
  max-height: 290px;
  padding: 7px;
  overflow-y: auto;
  border: 1px solid rgba(92, 129, 224, .55);
  border-radius: 13px;
  box-shadow: 0 18px 45px rgba(1, 6, 19, .55);
  background: #101b35;
}

.supplier-combobox__menu button {
  display: grid;
  gap: 3px;
  padding: 10px 12px;
  border: 0;
  border-radius: 9px;
  text-align: left;
  color: #eaf0ff;
  background: transparent;
  cursor: pointer;
}

.supplier-combobox__menu button:hover,
.supplier-combobox__menu button.is-selected {
  background: rgba(61, 95, 176, .34);
}

.supplier-combobox__menu strong {
  font-size: 12px;
  line-height: 1.35;
}

.supplier-combobox__menu small {
  color: #91a1c5;
  font-size: 10px;
}

.supplier-combobox__menu > p {
  padding: 12px;
}

.product-delivery-supplier p {
  margin: 0;
  color: #94a4c7;
  font-size: 11px;
  line-height: 1.5;
}

.product-delivery-supplier p strong {
  color: #e8edfb;
}

.product-delivery-supplier .supplier-inline-error {
  color: #ff9b9b;
}

.product-delivery-support {
  display: grid;
  gap: 8px;
}

.product-delivery-support label {
  display: grid;
  gap: 7px;
}

.product-delivery-support label > span {
  color: #dce5f7;
  font-size: 11px;
  font-weight: 800;
}

.product-delivery-support textarea {
  min-height: 110px;
  padding: 11px 12px;
  border: 1px solid rgba(126, 151, 217, .28);
  border-radius: 11px;
  outline: none;
  color: #eef3ff;
  background: rgba(5, 11, 26, .72);
  font-size: 11px;
  line-height: 1.5;
  resize: vertical;
}

.product-delivery-support textarea:focus {
  border-color: rgba(83, 125, 255, .78);
  box-shadow: 0 0 0 3px rgba(75, 115, 255, .12);
}

.product-delivery-support small {
  justify-self: end;
  color: #7183aa;
  font-size: 9px;
}

.product-delivery-support p,
.product-delivery__notice {
  margin: 0;
  color: #7889ad;
  font-size: 9px;
  line-height: 1.45;
}

.product-delivery__notice {
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(126, 151, 217, .055);
}

.product-key-pool {
  display: grid;
  gap: 12px;
}

.product-key-pool__stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  overflow: hidden;
  border: 1px solid rgba(126, 151, 217, .2);
  border-radius: 13px;
  background: rgba(8, 15, 34, .42);
}

.product-key-pool__stat {
  display: grid;
  min-width: 0;
  gap: 3px;
  padding: 13px 14px;
  border-right: 1px solid rgba(126, 151, 217, .14);
}

.product-key-pool__stat:last-child {
  border-right: 0;
}

.product-key-pool__stat > span {
  color: #8292b4;
  font-size: 8px;
  font-weight: 850;
  letter-spacing: .075em;
  text-transform: uppercase;
}

.product-key-pool__stat > strong {
  color: #edf2ff;
  font-size: 23px;
  line-height: 1.05;
}

.product-key-pool__stat--free > strong {
  color: #58e3bd;
}

.product-key-pool__stat > small {
  overflow: hidden;
  color: #7081a5;
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.product-key-pool__form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 170px auto;
  align-items: end;
  gap: 12px;
  padding: 14px;
  border: 1px solid rgba(79, 119, 255, .34);
  border-radius: 14px;
  background: linear-gradient(135deg, rgba(48, 91, 239, .1), rgba(8, 15, 34, .5));
}

.product-key-pool__form-copy {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 10px;
}

.product-key-pool__form-copy strong {
  color: #e9eeff;
  font-size: 12px;
}

.product-key-pool__form-copy p {
  margin: 2px 0 0;
  color: #8494b6;
  font-size: 10px;
}

.product-key-pool__form-icon {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 auto;
  place-items: center;
  border: 1px solid rgba(79, 119, 255, .48);
  border-radius: 10px;
  color: #7f9aff;
  background: rgba(48, 91, 239, .13);
}

.product-key-pool__form-icon svg,
.product-key-pool__submit svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.product-key-pool__codes,
.product-key-pool__expiry {
  display: grid;
  gap: 6px;
  color: #a6b3ce;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .05em;
  text-transform: uppercase;
}

.product-key-pool__codes textarea,
.product-key-pool__expiry input {
  box-sizing: border-box;
  width: 100%;
  min-height: 82px;
  padding: 10px 12px;
  border: 1px solid rgba(126, 151, 217, .28);
  border-radius: 11px;
  outline: none;
  color: #e5ebfb;
  background: rgba(4, 10, 27, .74);
  font: inherit;
  font-size: 11px;
  line-height: 1.5;
  resize: vertical;
}

.product-key-pool__expiry input {
  min-height: 42px;
  resize: none;
  color-scheme: dark;
}

.product-key-pool__codes textarea:focus,
.product-key-pool__expiry input:focus {
  border-color: rgba(83, 126, 255, .72);
  box-shadow: 0 0 0 3px rgba(48, 91, 239, .09);
}

.product-key-pool__codes small,
.product-key-pool__expiry small {
  color: #7182a7;
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0;
  text-transform: none;
}

.product-key-pool__submit {
  display: inline-flex;
  min-height: 42px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 0 16px;
  border: 1px solid rgba(87, 126, 255, .62);
  border-radius: 11px;
  color: #fff;
  background: linear-gradient(135deg, #2455e8, #4c70ff);
  box-shadow: 0 10px 24px rgba(35, 79, 225, .2);
  font: inherit;
  font-size: 10px;
  font-weight: 850;
  cursor: pointer;
}

.product-key-pool__submit:disabled {
  cursor: not-allowed;
  opacity: .55;
}

.product-key-pool__message,
.product-key-pool__readonly {
  margin: 0;
  padding: 9px 11px;
  border-radius: 9px;
  color: #8e9dbc;
  background: rgba(126, 151, 217, .055);
  font-size: 10px;
}

.product-key-pool__message--error {
  color: #ffaaa8;
  background: rgba(255, 150, 155, .075);
}

.product-key-pool__message--ok {
  color: #72e7c5;
  background: rgba(83, 229, 186, .075);
}

.product-key-pool__state {
  display: flex;
  min-height: 70px;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 14px;
  border: 1px dashed rgba(126, 151, 217, .25);
  border-radius: 13px;
  color: #aab6d0;
  background: rgba(8, 15, 34, .34);
  font-size: 11px;
}

.product-key-pool__state--empty {
  flex-direction: column;
}

.product-key-pool__state small {
  color: #7586aa;
}

.product-key-pool__list-head,
.product-key-pool__pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.product-key-pool__list-head strong {
  color: #dce5f8;
  font-size: 11px;
}

.product-key-pool__list-head span,
.product-key-pool__pager span {
  color: #7788ac;
  font-size: 9px;
}

.product-key-pool__list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  max-height: 310px;
  gap: 8px;
  overflow: auto;
  padding-right: 3px;
}

.product-key-pool__item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 5px 10px;
  padding: 11px 12px;
  border: 1px solid rgba(126, 151, 217, .18);
  border-radius: 11px;
  background: rgba(8, 15, 34, .45);
}

.product-key-pool__item code {
  overflow: hidden;
  color: #dfe8fb;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
}

.product-key-pool__item time,
.product-key-pool__item small {
  color: #7182a7;
  font-size: 8px;
}

.product-key-pool__status {
  padding: 3px 6px;
  border: 1px solid currentColor;
  border-radius: 999px;
  color: #58e3bd;
  font-size: 7px;
  font-weight: 900;
  letter-spacing: .04em;
  text-transform: uppercase;
}

.product-key-pool__status--reserved,
.product-key-pool__status--sending {
  color: #ffc75a;
}

.product-key-pool__status--delivered {
  color: #7da0ff;
}

.product-key-pool__status--expired,
.product-key-pool__status--disabled {
  color: #98a3ba;
}

.product-key-pool__pager {
  justify-content: center;
}

.product-key-pool__pager button {
  padding: 6px 10px;
  border: 1px solid rgba(126, 151, 217, .25);
  border-radius: 8px;
  color: #aebbd5;
  background: rgba(18, 29, 59, .72);
  font: inherit;
  font-size: 9px;
  cursor: pointer;
}

.product-key-pool__pager button:disabled {
  cursor: not-allowed;
  opacity: .45;
}

@media (max-width: 660px) {
  .product-card-backdrop {
    padding: 12px;
  }

  .product-card-modal {
    max-height: calc(100vh - 24px);
    border-radius: 21px;
  }

  .product-card-modal__header {
    align-items: flex-start;
    flex-direction: column;
    padding: 18px;
  }

  .product-card-modal__head-actions {
    width: 100%;
  }

  .product-card-modal__mode {
    margin-right: auto;
  }

  .product-card-modal__body {
    max-height: calc(100vh - 150px);
    padding: 18px;
  }

  .product-overview__grid {
    grid-template-columns: 1fr;
  }

  .product-overview.has-image {
    grid-template-columns: 1fr;
  }

  .product-delivery-supplier__fields {
    grid-template-columns: 1fr;
  }

  .product-orders__summary,
  .product-orders__toolbar,
  .product-order__head,
  .product-order__meta {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }

  .product-overview__image-wrap {
    min-height: 180px;
  }

  .product-overview__grid dt,
  .product-overview__grid dd {
    border-bottom: 1px solid rgba(137, 159, 211, .18);
  }

  .product-overview__grid dd:last-child {
    border-bottom: 0;
  }

  .product-work-block__toggle {
    grid-template-columns: 38px minmax(0, 1fr) 18px;
    gap: 9px;
    padding: 12px;
  }

  .stock-readonly__fields {
    grid-template-columns: 1fr;
  }

  .stock-limit__metrics {
    grid-template-columns: 1fr;
  }

  .stock-limit__metrics > div {
    border-right: 0;
    border-bottom: 1px solid rgba(126, 151, 217, .14);
  }

  .stock-limit__metrics > div:last-child {
    border-bottom: 0;
  }

  .stock-limit__extra {
    grid-template-columns: 1fr;
  }

  .product-instruction__content {
    grid-template-columns: 1fr;
  }

  .product-delivery__summary {
    flex-direction: column;
  }

  .product-delivery-method__head {
    grid-template-columns: 38px minmax(0, 1fr) auto;
    gap: 9px;
  }

  .product-delivery-method__head > .product-delivery-method__icon {
    display: none;
  }

  .product-delivery-method__head > .product-delivery-method__copy {
    grid-column: 2;
  }

  .product-delivery-method__open {
    grid-template-columns: 34px minmax(0, 1fr) 16px;
  }

  .product-delivery-method__status {
    grid-column: 2;
    justify-self: start;
  }

  .product-delivery-switch {
    grid-column: 3;
    grid-row: 1;
  }

  .product-key-pool__stats,
  .product-key-pool__list,
  .product-key-pool__form {
    grid-template-columns: 1fr;
  }

  .product-key-pool__stat {
    border-right: 0;
    border-bottom: 1px solid rgba(126, 151, 217, .14);
  }

  .product-key-pool__stat:last-child {
    border-bottom: 0;
  }

  .product-settings-savebar,
  .product-settings-savebar__actions {
    align-items: stretch;
    flex-direction: column;
  }

  .product-settings-savebar__actions {
    width: 100%;
  }

  .product-settings-savebar__actions button {
    width: 100%;
  }

  .product-card-modal__footer {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }
}
</style>
