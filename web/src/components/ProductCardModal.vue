<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { normalizeEscapedLineBreaks } from '../utils/text.js'

const props = defineProps({
  item: { type: Object, required: true },
  providerName: { type: String, required: true },
  providerLogo: { type: String, required: true },
  syncedLabel: { type: String, required: true },
  stockSyncedLabel: { type: String, default: '' },
  stockLoading: { type: Boolean, default: false },
  stockError: { type: String, default: '' },
  orders: { type: Array, default: () => [] },
  ordersTotal: { type: Number, default: 0 },
  ordersLoading: { type: Boolean, default: false },
  ordersError: { type: String, default: '' },
  ordersRefreshing: { type: Boolean, default: false },
})

const emit = defineEmits(['close', 'refresh-stock', 'refresh-orders'])
const openSection = ref('')
const imageFailed = ref(false)
const showProductImage = computed(() => Boolean(props.item.primary_image) && !imageFailed.value)
const canRefreshStock = computed(() => props.item.provider_code === 'yandex_market')
const currentStock = computed(() => Number.isInteger(props.item.available_stock) ? props.item.available_stock : '—')
const hasTransferredSalesLimit = computed(() => Boolean(props.item.stock_settings_available))
const hasSalesLimit = computed(() => hasTransferredSalesLimit.value && props.item.sales_limit !== null && props.item.sales_limit !== '')
const configuredStock = computed(() => hasTransferredSalesLimit.value ? Math.max(0, Number(props.item.manual_stock_limit) || 0) : 'Не перенесён')
const dailySalesLimit = computed(() => {
  if (!hasTransferredSalesLimit.value) return 'Не перенесён'
  if (!hasSalesLimit.value) return 'Без ограничений'
  return Math.max(0, Number(props.item.sales_limit) || 0)
})
const limitMetrics = computed(() => [
  { label: 'Продано', value: hasTransferredSalesLimit.value ? Math.max(0, Number(props.item.sales_limit_used) || 0) : '—' },
  { label: 'В резерве', value: hasTransferredSalesLimit.value ? Math.max(0, Number(props.item.sales_limit_reserved) || 0) : '—' },
  { label: 'Осталось', value: hasSalesLimit.value ? Math.max(0, Number(props.item.sales_limit_remaining) || 0) : '—' },
  { label: 'Дополнительно сегодня', value: hasTransferredSalesLimit.value ? `+${Math.max(0, Number(props.item.sales_limit_daily_extra) || 0)}` : '—' },
])
const limitHeadline = computed(() => {
  if (!hasTransferredSalesLimit.value) return 'Данные пока не перенесены'
  if (!hasSalesLimit.value) return 'Без ограничений'
  const remaining = Math.max(0, Number(props.item.sales_limit_remaining) || 0)
  return remaining === 0 ? 'Лимит исчерпан' : `${remaining} доступно сегодня`
})
const activationInstruction = computed(() => {
  if (!hasTransferredSalesLimit.value) return 'Инструкция пока не перенесена из CRM.'
  return normalizeEscapedLineBreaks(props.item.activation_instruction).trim() || 'Инструкция не указана.'
})

const detailFields = computed(() => [
  { label: 'Артикул продавца', value: props.item.market_sku || props.item.offer_id || props.item.external_product_id || '—' },
  { label: 'SKU', value: props.item.offer_id || props.item.sku || '—' },
  { label: 'Цена', value: props.item.price ? `${props.item.price} ${props.item.currency_code || ''}`.trim() : '—' },
  { key: 'stock', label: 'Остаток', value: Number.isInteger(props.item.available_stock) ? props.item.available_stock : '—' },
])

const workSections = computed(() => [
  {
    id: 'stock',
    number: '01',
    title: 'Остаток',
    description: 'Актуальное количество, доступное для продажи на маркетплейсе',
  },
  {
    id: 'orders',
    number: '02',
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

function close() {
  emit('close')
}

function closeOnEscape(event) {
  if (event.key === 'Escape') close()
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
              <span class="product-card-modal__mode">Только просмотр</span>
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
                </div>
                <h3>{{ item.title || item.offer_id || item.sku || 'Товар без названия' }}</h3>
                <dl class="product-overview__grid">
                  <template v-for="field in detailFields" :key="field.label">
                    <dt>{{ field.label }}</dt>
                    <dd v-if="field.key === 'stock'" class="product-overview__stock-cell">
                      <span :class="{ 'is-loading': stockLoading }">{{ stockLoading ? 'Проверяем…' : field.value }}</span>
                    </dd>
                    <dd v-else>{{ field.value }}</dd>
                  </template>
                </dl>
                <p v-if="stockError" class="product-overview__stock-error" role="alert">{{ stockError }}</p>
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
                      </section>

                      <section class="stock-readonly__field">
                        <span>Заданный остаток</span>
                        <output>{{ configuredStock }}</output>
                        <small>Целевое значение, сохранённое ранее в CRM</small>
                      </section>

                      <section class="stock-readonly__field">
                        <span>Дневной лимит</span>
                        <output>{{ dailySalesLimit }}</output>
                        <small>Максимальное количество продаж за день</small>
                      </section>
                    </div>

                    <section class="stock-limit" :class="{ 'stock-limit--pending': !hasTransferredSalesLimit }">
                      <div class="stock-limit__heading">
                        <div><span>Состояние лимита</span><strong>{{ limitHeadline }}</strong></div>
                        <span class="stock-limit__badge">Только просмотр</span>
                      </div>
                      <div class="stock-limit__metrics">
                        <div v-for="metric in limitMetrics" :key="metric.label"><span>{{ metric.label }}</span><strong>{{ metric.value }}</strong></div>
                      </div>
                    </section>

                    <section class="stock-instruction">
                      <span>Инструкция покупателю</span>
                      <p>{{ activationInstruction }}</p>
                    </section>

                    <p class="stock-readonly__notice">
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8v5" /><path d="M12 17h.01" /><circle cx="12" cy="12" r="9" /></svg>
                      Seller только показывает сохранённые параметры. Изменение и отправка данных в маркетплейс отключены.
                    </p>
                  </div>
                  <div v-else class="product-orders">
                    <div class="product-orders__toolbar">
                      <div><strong>Заказы товара</strong><span>Обновление выполняется по магазину</span></div>
                      <button type="button" :disabled="ordersLoading || ordersRefreshing" title="Получить свежие заказы магазина и обновить список этой карточки" @click="emit('refresh-orders')">
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

.product-overview__stock-cell {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.product-overview__stock-cell > span.is-loading {
  color: #91a6d8;
}

.product-overview__stock-error {
  margin: 9px 2px 0;
  color: #ffaaa8;
  font-size: 11px;
  line-height: 1.4;
}

@keyframes product-stock-spin {
  to { transform: rotate(360deg); }
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

.stock-readonly__field,
.stock-instruction {
  display: grid;
  gap: 8px;
  padding: 14px;
  border: 1px solid rgba(126, 151, 217, .22);
  border-radius: 13px;
  background: rgba(8, 15, 34, .42);
}

.stock-readonly__field > span,
.stock-instruction > span,
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
  grid-template-columns: repeat(4, minmax(0, 1fr));
  overflow: hidden;
  border: 1px solid rgba(126, 151, 217, .16);
  border-radius: 10px;
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

.stock-instruction p {
  min-height: 42px;
  margin: 0;
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
    grid-template-columns: 1fr 1fr;
  }

  .stock-limit__metrics > div:nth-child(2) {
    border-right: 0;
  }

  .stock-limit__metrics > div:nth-child(-n + 2) {
    border-bottom: 1px solid rgba(126, 151, 217, .14);
  }

  .product-card-modal__footer {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }
}
</style>
