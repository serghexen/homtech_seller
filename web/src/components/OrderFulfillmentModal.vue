<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { keyCountLabel, parseKeyLines } from '../utils/keyPool.js'

const props = defineProps({
  order: { type: Object, required: true },
  detail: { type: Object, default: null },
  viewOnly: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  actionLoading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  revealedKeys: { type: Array, default: () => [] },
  revealedSupportMessage: { type: String, default: '' },
  revealLoading: { type: Boolean, default: false },
  revealError: { type: String, default: '' },
})

const emit = defineEmits(['close', 'prepare', 'prepare-manual', 'prepare-support', 'release', 'send', 'cancel-send', 'resolve-unknown', 'reveal'])
const sendConfirmation = ref(false)
const manualEntryOpen = ref(false)
const manualCodesRaw = ref('')
const unknownResolution = ref('')
const copiedKeyId = ref(0)
const supportMessageCopied = ref(false)
const marketplaceName = computed(() => props.detail?.provider_code === 'ozon' ? 'Ozon' : 'Яндекс Маркет')
const automationInProgress = computed(() => Boolean(props.detail?.automation_in_progress))
const waitingForYandexProcessing = computed(() => (
  props.detail?.provider_code === 'yandex_market'
  && !props.detail?.order_ready_for_fulfillment
  && ['PLACING', 'RESERVED', 'UNPAID', 'PENDING'].includes(String(props.detail?.provider_status || '').toUpperCase())
))

function formatDeadline(value) {
  if (!value) return ''
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return ''
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(parsed)
}

watch(() => props.detail?.outbound_state, () => {
  sendConfirmation.value = false
  unknownResolution.value = ''
})
watch(() => props.detail?.fulfillment_status, (status) => {
  if (status === 'reserved') {
    manualEntryOpen.value = false
    manualCodesRaw.value = ''
  }
})

const statusPresentation = computed(() => {
  const status = props.detail?.fulfillment_status || 'not_prepared'
  if (waitingForYandexProcessing.value) {
    return String(props.detail?.provider_status || '').toUpperCase() === 'UNPAID'
      ? { label: 'Ожидаем оплату', tone: 'active', copy: 'Seller начнёт подготовку только после статуса PROCESSING от Яндекс Маркета.' }
      : { label: 'Ожидаем Яндекс Маркет', tone: 'active', copy: 'Выдача начнётся только после перехода заказа в статус PROCESSING.' }
  }
  return {
    not_prepared: { label: 'Не подготовлена', tone: 'idle', copy: 'Ключи к заказу ещё не закреплены.' },
    pending: automationInProgress.value
      ? { label: 'Автовыдача запущена', tone: 'active', copy: 'Seller последовательно проверяет поставщика, пул и остальные настроенные способы.' }
      : { label: 'Ожидает подготовки', tone: 'idle', copy: 'Локальная выдача создана, комплект пока свободен.' },
    manual_required: { label: 'Нужен комплект', tone: 'warning', copy: props.detail?.last_error || 'В пуле пока нет полного комплекта.' },
    reserved: {
      label: props.detail?.delivery_source === 'support_message' ? 'Сообщение подготовлено' : 'Комплект закреплён',
      tone: 'ready',
      copy: props.detail?.delivery_source === 'support_message'
        ? 'Снимок сообщения поддержки зафиксирован в Seller и ещё не отправлен.'
        : 'Ключи зарезервированы внутри Seller и не отправлены покупателю.',
    },
    supplier_required: automationInProgress.value
      ? { label: 'Поставщик обрабатывает', tone: 'active', copy: props.detail?.last_error || 'Supplier Hub готовит комплект. Ручное вмешательство временно заблокировано.' }
      : { label: 'Нужен поставщик', tone: 'warning', copy: props.detail?.last_error || 'Для продолжения потребуется Supplier Hub.' },
    sending: { label: 'Отправляется', tone: 'active', copy: 'Состояние отправки нельзя откатывать автоматически.' },
    submitted: { label: 'Передано', tone: 'active', copy: 'Результат передан маркетплейсу и ожидает подтверждения.' },
    unknown: { label: 'Нужна сверка', tone: 'warning', copy: 'Результат внешней отправки пока неизвестен.' },
    delivered: { label: 'Доставлено', tone: 'ready', copy: 'Маркетплейс подтвердил доставку.' },
    closed_external: { label: 'Закрыто внешней системой', tone: 'muted', copy: 'Этот заказ был завершён вне Seller.' },
    cancelled: { label: 'Отменено', tone: 'muted', copy: 'Заказ отменён, локальный резерв отсутствует.' },
    failed: { label: 'Ошибка', tone: 'warning', copy: props.detail?.last_error || 'Подготовку нужно проверить вручную.' },
  }[status] || { label: 'Неизвестно', tone: 'muted', copy: 'Состояние пока не распознано.' }
})

const missingKeys = computed(() => Math.max(0, Number(props.detail?.quantity || props.order.quantity || 0) - Number(props.detail?.free_count || 0)))
const canPrepareCompleteSet = computed(() => Boolean(props.detail?.can_prepare) && missingKeys.value === 0)
const manualCodes = computed(() => parseKeyLines(manualCodesRaw.value))
const manualCodesComplete = computed(() => manualCodes.value.length === Number(props.detail?.quantity || 0))
const preparedCount = computed(() => props.detail?.delivery_source === 'support_message'
  ? Number(props.detail?.quantity || 0)
  : Number(props.detail?.reserved_count || 0))
const outboundPresentation = computed(() => {
  if (props.detail?.fulfillment_status === 'delivered') return `${marketplaceName.value} подтвердил доставку. Зарезервированный комплект окончательно списан из пула.`
  return ({
  queued: 'Отправка ожидает worker. Пока она не началась, её можно отменить.',
  preparing: 'Worker проверяет комплект и инструкцию перед внешним запросом.',
  sending: 'Запрос выполняется. Автоматический откат и повтор уже запрещены.',
  submitted: `${marketplaceName.value} принял комплект. Ожидаем подтверждение заказа при следующей сверке.`,
  unknown: `Нельзя отправлять повторно: сначала сверьте заказ в кабинете ${marketplaceName.value}.`,
  failed: props.detail?.outbound_last_error || `${marketplaceName.value} однозначно отклонил запрос. Комплект сохранён в резерве.`,
  cancelled: 'Постановка в очередь отменена. Комплект остался в резерве.',
  }[props.detail?.outbound_state] || '')
})

async function copyRevealedKey(item) {
  if (!item?.code || !navigator.clipboard) return
  await navigator.clipboard.writeText(item.code)
  copiedKeyId.value = item.id
  window.setTimeout(() => {
    if (copiedKeyId.value === item.id) copiedKeyId.value = 0
  }, 1800)
}

async function copySupportMessage() {
  if (!props.revealedSupportMessage || !navigator.clipboard) return
  await navigator.clipboard.writeText(props.revealedSupportMessage)
  supportMessageCopied.value = true
  window.setTimeout(() => {
    supportMessageCopied.value = false
  }, 1800)
}

function closeOnEscape(event) {
  if (event.key === 'Escape' && !props.actionLoading) emit('close')
}

onMounted(() => window.addEventListener('keydown', closeOnEscape))
onBeforeUnmount(() => window.removeEventListener('keydown', closeOnEscape))
</script>

<template>
  <Teleport to="body">
    <Transition name="fulfillment-card" appear>
      <div class="fulfillment-backdrop" @click.self="!actionLoading && emit('close')">
        <section class="fulfillment-card" role="dialog" aria-modal="true" aria-labelledby="fulfillment-title">
          <header class="fulfillment-card__header">
            <div>
              <span>{{ automationInProgress ? 'Автоматическая выдача' : order.status === 'processing' ? 'Локальная подготовка' : 'Результат выдачи' }}</span>
              <h2 id="fulfillment-title">Заказ №{{ order.external_order_id }}</h2>
            </div>
            <button type="button" aria-label="Закрыть" :disabled="actionLoading" @click="emit('close')">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
            </button>
          </header>

          <div v-if="loading" class="fulfillment-card__loading">
            <span class="fulfillment-card__pulse" />
            Проверяем локальную выдачу и пул ключей…
          </div>

          <template v-else-if="detail">
            <section class="fulfillment-product" :class="{ 'fulfillment-product--view': viewOnly }">
              <div v-if="!viewOnly" class="fulfillment-product__index">{{ String(detail.quantity).padStart(2, '0') }}</div>
              <div>
                <small>{{ detail.store_name }} · {{ marketplaceName }}</small>
                <h3>{{ detail.title || order.title || 'Товар без названия' }}</h3>
                <p>SKU: <strong>{{ detail.offer_id || order.offer_id || order.sku || '—' }}</strong></p>
                <p v-if="detail.fulfillment_deadline_at">Код ожидается до: <strong>{{ formatDeadline(detail.fulfillment_deadline_at) }}</strong></p>
              </div>
            </section>

            <section class="fulfillment-state" :class="`fulfillment-state--${statusPresentation.tone}`">
              <div class="fulfillment-state__signal"><span /></div>
              <div>
                <small>Состояние выдачи</small>
                <strong>{{ statusPresentation.label }}</strong>
                <p>{{ statusPresentation.copy }}</p>
              </div>
            </section>

            <div v-if="!viewOnly" class="fulfillment-metrics">
              <article>
                <span>Нужно для заказа</span>
                <strong>{{ detail.quantity }}</strong>
                <small>ключей</small>
              </article>
              <article>
                <span>Свободно в пуле</span>
                <strong>{{ detail.free_count }}</strong>
                <small>доступно сейчас</small>
              </article>
              <article :class="{ 'is-ready': preparedCount }">
                <span>{{ detail.delivery_source === 'support_message' ? 'Подготовлено' : 'Закреплено' }}</span>
                <strong>{{ preparedCount }}</strong>
                <small>{{ detail.delivery_source === 'support_message' ? 'сообщений' : 'только внутри Seller' }}</small>
              </article>
            </div>

            <div v-if="!viewOnly" class="fulfillment-safety" :class="{ 'is-outbound': detail.outbound_state, 'is-automatic': automationInProgress }">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10V7a5 5 0 0 1 10 0v3" /><rect x="5" y="10" width="14" height="10" rx="3" /><path d="M12 14v2" /></svg>
              <div>
                <strong>{{ automationInProgress ? 'Заказ контролирует автовыдача' : detail.outbound_state ? 'Внешняя отправка' : 'Защищённая подготовка' }}</strong>
                <p v-if="automationInProgress">Ручные действия недоступны. Если автоматическая цепочка не подготовит комплект, Seller передаст заказ оператору.</p>
                <p v-else-if="detail.outbound_state">{{ outboundPresentation }}</p>
                <p v-else>Открытые коды загружаются только по отдельному запросу оператора. Отправка начнётся после отдельного подтверждения.</p>
              </div>
            </div>

            <section v-if="detail.can_reveal_support_message" class="fulfillment-keys fulfillment-support-message">
              <header>
                <div class="fulfillment-keys__mark" aria-hidden="true">
                  <svg viewBox="0 0 24 24"><path d="M4 5h16v11H8l-4 3z" /><path d="M8 9h8M8 12h5" /></svg>
                </div>
                <div>
                  <small>{{ detail.fulfillment_status === 'delivered' ? 'Выдано покупателю' : 'Подготовлено к выдаче' }}</small>
                  <strong>{{ revealedSupportMessage ? 'Сообщение поддержки' : 'Сообщение хранится в Seller' }}</strong>
                  <p>{{ revealedSupportMessage ? 'Точный отправленный текст открыт только в этом окне.' : 'Покажем сохранённый снимок только после явного нажатия.' }}</p>
                </div>
                <button v-if="!revealedSupportMessage" type="button" :disabled="revealLoading" @click="emit('reveal')">
                  <span v-if="revealLoading" class="fulfillment-card__pulse fulfillment-card__pulse--small" aria-hidden="true" />
                  <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" /><circle cx="12" cy="12" r="2.5" /></svg>
                  {{ revealLoading ? 'Открываем…' : 'Показать сообщение' }}
                </button>
              </header>
              <div v-if="revealedSupportMessage" class="fulfillment-support-message__body">
                <p>{{ revealedSupportMessage }}</p>
                <button type="button" @click="copySupportMessage">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="11" height="11" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></svg>
                  <span>{{ supportMessageCopied ? 'Скопировано' : 'Копировать' }}</span>
                </button>
              </div>
              <p v-if="revealError" class="fulfillment-keys__error" role="alert">{{ revealError }}</p>
            </section>

            <section v-if="detail.can_reveal_keys" class="fulfillment-keys">
              <header>
                <div class="fulfillment-keys__mark" aria-hidden="true">
                  <svg viewBox="0 0 24 24"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" /><circle cx="12" cy="12" r="2.5" /></svg>
                </div>
                <div>
                  <small>{{ detail.fulfillment_status === 'delivered' ? 'Выдано покупателю' : 'Закреплено за заказом' }}</small>
                  <strong>{{ revealedKeys.length ? keyCountLabel(revealedKeys.length) : 'Ключ хранится в Seller' }}</strong>
                  <p>{{ revealedKeys.length ? 'Значение открыто только в этом окне.' : 'Покажем значение только после явного нажатия.' }}</p>
                </div>
                <button v-if="!revealedKeys.length" type="button" :disabled="revealLoading" @click="emit('reveal')">
                  <span v-if="revealLoading" class="fulfillment-card__pulse fulfillment-card__pulse--small" aria-hidden="true" />
                  <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" /><circle cx="12" cy="12" r="2.5" /></svg>
                  {{ revealLoading ? 'Открываем…' : 'Показать ключ' }}
                </button>
              </header>
              <div v-if="revealedKeys.length" class="fulfillment-keys__list">
                <article v-for="item in revealedKeys" :key="item.id">
                  <code>{{ item.code }}</code>
                  <button type="button" :aria-label="`Копировать ключ ${item.code}`" @click="copyRevealedKey(item)">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="11" height="11" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></svg>
                    <span>{{ copiedKeyId === item.id ? 'Скопировано' : 'Копировать' }}</span>
                  </button>
                </article>
              </div>
              <p v-if="revealError" class="fulfillment-keys__error" role="alert">{{ revealError }}</p>
            </section>

            <section v-if="!viewOnly && detail.can_resolve_unknown" class="fulfillment-reconciliation">
              <header>
                <div class="fulfillment-reconciliation__mark">!</div>
                <div>
                  <small>Ручная сверка</small>
                  <strong>Проверьте заказ в кабинете {{ marketplaceName }}</strong>
                  <p>Seller не получил однозначный ответ и не станет повторять отправку самостоятельно.</p>
                </div>
              </header>
              <div class="fulfillment-reconciliation__choices">
                <button
                  type="button"
                  :class="{ 'is-selected': unknownResolution === 'accepted' }"
                  :disabled="actionLoading"
                  @click="unknownResolution = 'accepted'"
                >
                  <span class="fulfillment-reconciliation__icon">✓</span>
                  <span><strong>Данные получены</strong><small>{{ marketplaceName }} показывает переданный ключ</small></span>
                </button>
                <button
                  type="button"
                  :class="{ 'is-selected': unknownResolution === 'not_accepted' }"
                  :disabled="actionLoading"
                  @click="unknownResolution = 'not_accepted'"
                >
                  <span class="fulfillment-reconciliation__icon">↻</span>
                  <span><strong>Данные не получены</strong><small>Вернуть комплект в резерв</small></span>
                </button>
              </div>
              <div v-if="unknownResolution" class="fulfillment-reconciliation__confirm">
                <p v-if="unknownResolution === 'accepted'">Комплект останется заблокированным. Seller будет ожидать подтверждение доставки от {{ marketplaceName }}.</p>
                <p v-else>Комплект вернётся в защищённый резерв. Повторная отправка всё равно потребует отдельного подтверждения.</p>
                <button
                  type="button"
                  :disabled="actionLoading"
                  @click="emit('resolve-unknown', unknownResolution)"
                >
                  {{ actionLoading ? 'Фиксируем…' : 'Подтвердить результат сверки' }}
                </button>
              </div>
            </section>

            <div v-if="!viewOnly && !automationInProgress && !detail.manual_actions_enabled" class="fulfillment-feature-lock">
              <span>Режим просмотра</span>
              Ручная подготовка выключена общим переключателем сервиса. Ни резерв, ни снятие резерва сейчас недоступны.
            </div>

            <section v-else-if="!viewOnly && detail.can_prepare_manual" class="fulfillment-preparation">
              <header>
                <div><small>Источник выдачи</small><strong>Выберите, что подготовить для этого заказа</strong></div>
                <span>Без отправки</span>
              </header>
              <div class="fulfillment-preparation__choices">
                <button type="button" :disabled="actionLoading || !canPrepareCompleteSet" :title="canPrepareCompleteSet ? 'Закрепить ключи из пула' : `Не хватает: ${missingKeys}`" @click="emit('prepare')">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10a5 5 0 1 1 4.6 5" /><path d="m9 14-6 6M5 18l2 2M8 15l2 2" /></svg>
                  <span><strong>Из пула</strong><small>{{ canPrepareCompleteSet ? `${detail.quantity} шт. доступно` : `Не хватает ${missingKeys}` }}</small></span>
                </button>
                <button type="button" :class="{ 'is-active': manualEntryOpen }" :disabled="actionLoading" @click="manualEntryOpen = !manualEntryOpen">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l11-11-4-4L4 16z" /><path d="m13 7 4 4" /></svg>
                  <span><strong>Ввести вручную</strong><small>Ровно {{ detail.quantity }} шт.</small></span>
                </button>
                <button type="button" :disabled="actionLoading || !detail.can_prepare_support" :title="detail.support_message_configured ? 'Подготовить сохранённое сообщение' : 'Сначала заполните сообщение в карточке товара'" @click="emit('prepare-support')">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H8l-4 3z" /><path d="M8 9h8M8 12h5" /></svg>
                  <span><strong>Через поддержку</strong><small>{{ detail.support_message_configured ? 'Сообщение настроено' : 'Текст не настроен' }}</small></span>
                </button>
              </div>
              <div v-if="manualEntryOpen" class="fulfillment-manual-entry">
                <label>
                  <span>Ключи — по одному на строку</span>
                  <textarea v-model="manualCodesRaw" :disabled="actionLoading" spellcheck="false" autocomplete="off" placeholder="AAAA-BBBB-CCCC" />
                  <small :class="{ 'is-complete': manualCodesComplete }">{{ keyCountLabel(manualCodes.length) }} из {{ detail.quantity }}</small>
                </label>
                <button type="button" :disabled="actionLoading || !manualCodesComplete" @click="emit('prepare-manual', manualCodes)">
                  {{ actionLoading ? 'Защищаем…' : 'Защифровать и закрепить' }}
                </button>
              </div>
              <p>Подготовка не отправляет данные в {{ marketplaceName }}. Перед отправкой появится отдельное подтверждение.</p>
            </section>

            <p v-if="error" class="fulfillment-card__error">{{ error }}</p>

            <footer class="fulfillment-card__actions">
              <template v-if="!viewOnly">
                <p v-if="detail.can_resolve_unknown">Сначала завершите ручную сверку выше. Повтор без подтверждения заблокирован.</p>
                <p v-else-if="automationInProgress">Автовыдача продолжает работу. Состояние окна обновляется автоматически.</p>
                <p v-else-if="!detail.manual_actions_enabled">Для включения требуется контролируемое переключение Seller с CRM.</p>
                <p v-else-if="detail.can_prepare_manual">Выберите источник выше. После подготовки комплект можно будет проверить и отправить.</p>
                <p v-else-if="detail.can_send && !sendConfirmation">Комплект готов. Отправка — отдельное необратимое действие.</p>
                <p v-else-if="detail.can_send">Проверьте заказ: после подтверждения worker передаст комплект в {{ marketplaceName }}.</p>
                <p v-else-if="detail.can_cancel_send">Задание ещё не взято worker-ом, поэтому его можно безопасно отменить.</p>
                <p v-else-if="detail.can_release">Резерв можно безопасно снять, пока отправка не поставлена в очередь.</p>
                <p v-else>Для этого состояния локальные действия недоступны.</p>
              </template>
              <button
                v-if="viewOnly"
                class="fulfillment-action fulfillment-action--quiet"
                type="button"
                @click="emit('close')"
              >
                Закрыть
              </button>
              <button
                v-else-if="detail.can_cancel_send"
                class="fulfillment-action fulfillment-action--release"
                type="button"
                :disabled="actionLoading"
                @click="emit('cancel-send')"
              >
                {{ actionLoading ? 'Отменяем…' : 'Отменить отправку' }}
              </button>
              <button
                v-else-if="detail.can_send && sendConfirmation"
                class="fulfillment-action fulfillment-action--send"
                type="button"
                :disabled="actionLoading"
                @click="emit('send')"
              >
                {{ actionLoading ? 'Ставим в очередь…' : 'Подтвердить отправку' }}
              </button>
              <button
                v-else-if="detail.can_send"
                class="fulfillment-action"
                type="button"
                :disabled="actionLoading"
                @click="sendConfirmation = true"
              >
                Отправить в {{ marketplaceName }}
              </button>
              <button
                v-else-if="detail.can_release"
                class="fulfillment-action fulfillment-action--release"
                type="button"
                :disabled="actionLoading"
                @click="emit('release')"
              >
                {{ actionLoading ? 'Снимаем резерв…' : 'Снять резерв' }}
              </button>
              <button v-else-if="detail.can_prepare_manual" class="fulfillment-action fulfillment-action--quiet" type="button" :disabled="actionLoading" @click="emit('close')">Закрыть</button>
              <button v-else class="fulfillment-action fulfillment-action--quiet" type="button" @click="emit('close')">Закрыть</button>
            </footer>
          </template>

          <p v-else class="fulfillment-card__error">{{ error || 'Не удалось прочитать локальную выдачу' }}</p>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.fulfillment-backdrop { position: fixed; z-index: 30; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(2,6,19,.76); backdrop-filter: blur(10px); }
.fulfillment-card { width: min(100%,760px); max-height: calc(100vh - 48px); overflow: auto; border: 1px solid rgba(125,151,220,.34); border-radius: 29px; color: #edf2ff; background: radial-gradient(circle at 100% 0,rgba(64,101,230,.18),transparent 36%),linear-gradient(145deg,#14213f,#0a1026 72%); box-shadow: 0 36px 120px rgba(0,0,0,.52); }
.fulfillment-card__header { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 26px 28px 22px; border-bottom: 1px solid rgba(137,158,208,.18); }
.fulfillment-card__header span { color: #7795ff; font-size: 10px; font-weight: 900; letter-spacing: .16em; text-transform: uppercase; }
.fulfillment-card__header h2 { margin: 6px 0 0; font-size: clamp(25px,4vw,38px); letter-spacing: -.055em; }
.fulfillment-card__header button { display: grid; width: 42px; height: 42px; place-items: center; padding: 0; border: 1px solid rgba(139,160,210,.28); border-radius: 13px; color: #aebbd7; background: rgba(18,30,60,.72); }
.fulfillment-card__header svg,.fulfillment-safety svg { width: 20px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.fulfillment-card__loading { min-height: 360px; display: grid; place-content: center; justify-items: center; gap: 18px; color: #aebad5; }
.fulfillment-card__pulse { width: 42px; aspect-ratio: 1; border: 2px solid rgba(84,123,255,.28); border-top-color: #6d8cff; border-radius: 50%; animation: fulfillment-spin .85s linear infinite; }
.fulfillment-card__pulse--small { width: 16px; border-width: 2px; }
.fulfillment-product { display: grid; grid-template-columns: 60px minmax(0,1fr); align-items: center; gap: 18px; margin: 26px 28px 18px; }
.fulfillment-product--view { grid-template-columns: minmax(0,1fr); }
.fulfillment-product__index { display: grid; width: 60px; height: 60px; place-items: center; border: 1px solid rgba(84,128,255,.56); border-radius: 18px; color: #83a0ff; background: rgba(41,74,177,.22); font: 900 15px/1 ui-monospace,SFMono-Regular,Menlo,monospace; }
.fulfillment-product small,.fulfillment-product p { color: #aab6d1; }
.fulfillment-product h3 { margin: 5px 0 7px; font-size: 19px; line-height: 1.3; letter-spacing: -.025em; }
.fulfillment-product p { margin: 0; font-size: 12px; }.fulfillment-product p strong { color: #e8edfb; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; }
.fulfillment-state { display: grid; grid-template-columns: 42px minmax(0,1fr); gap: 14px; margin: 0 28px 18px; padding: 17px; border: 1px solid rgba(137,158,208,.21); border-radius: 18px; background: rgba(11,20,44,.7); }
.fulfillment-state__signal { display: grid; place-items: center; }.fulfillment-state__signal > span { width: 12px; aspect-ratio: 1; border-radius: 50%; color: #8da0c9; background: currentColor; box-shadow: 0 0 0 7px color-mix(in srgb,currentColor 12%,transparent); }
.fulfillment-state small { display: block; margin-bottom: 4px; color: #8f9dbb; font-size: 9px; font-weight: 900; letter-spacing: .12em; text-transform: uppercase; }.fulfillment-state strong { font-size: 16px; }.fulfillment-state p { margin: 5px 0 0; color: #aeb9d4; font-size: 12px; line-height: 1.45; }
.fulfillment-state--ready { border-color: rgba(77,225,188,.38); }.fulfillment-state--ready .fulfillment-state__signal > span { color: #50e6bd; }.fulfillment-state--warning { border-color: rgba(255,192,89,.34); }.fulfillment-state--warning .fulfillment-state__signal > span { color: #ffc55d; }.fulfillment-state--active .fulfillment-state__signal > span { color: #6f91ff; }
.fulfillment-metrics { display: grid; grid-template-columns: repeat(3,1fr); gap: 11px; margin: 0 28px 18px; }.fulfillment-metrics article { padding: 16px; border: 1px solid rgba(137,158,208,.18); border-radius: 17px; background: rgba(17,28,57,.56); }.fulfillment-metrics article.is-ready { border-color: rgba(74,226,188,.35); background: rgba(24,74,74,.25); }.fulfillment-metrics span,.fulfillment-metrics small { display: block; color: #8f9dbb; font-size: 10px; }.fulfillment-metrics strong { display: block; margin: 8px 0 4px; font-size: 29px; line-height: 1; letter-spacing: -.05em; }
.fulfillment-safety { display: grid; grid-template-columns: 30px minmax(0,1fr); gap: 12px; margin: 0 28px 18px; padding: 15px; border: 1px dashed rgba(101,134,221,.33); border-radius: 16px; color: #8ea8f2; background: rgba(26,48,105,.17); }.fulfillment-safety p { margin: 4px 0 0; color: #9faccc; font-size: 11px; line-height: 1.5; }
.fulfillment-safety.is-outbound { border-style: solid; border-color: rgba(101,134,221,.42); }
.fulfillment-safety.is-automatic { border-style: solid; border-color: rgba(100,139,255,.52); background: linear-gradient(120deg,rgba(35,66,147,.28),rgba(21,42,95,.16)); box-shadow: inset 3px 0 0 rgba(100,139,255,.72); }
.fulfillment-keys { margin: 0 28px 18px; padding: 17px; border: 1px solid rgba(82,226,190,.28); border-radius: 19px; background: radial-gradient(circle at 100% 0,rgba(65,224,184,.09),transparent 38%),rgba(10,31,44,.4); }.fulfillment-keys > header { display: grid; grid-template-columns: 40px minmax(0,1fr) auto; align-items: center; gap: 12px; }.fulfillment-keys__mark { display: grid; width: 40px; height: 40px; place-items: center; border: 1px solid rgba(85,226,190,.4); border-radius: 12px; color: #5be4bd; background: rgba(53,190,155,.09); }.fulfillment-keys svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }.fulfillment-keys header small,.fulfillment-keys header strong { display: block; }.fulfillment-keys header small { margin-bottom: 4px; color: #59caaa; font-size: 9px; font-weight: 900; letter-spacing: .11em; text-transform: uppercase; }.fulfillment-keys header strong { color: #eafff9; font-size: 13px; }.fulfillment-keys header p { margin: 4px 0 0; color: #91aaab; font-size: 10px; }.fulfillment-keys header > button { display: inline-flex; min-height: 42px; align-items: center; justify-content: center; gap: 8px; padding: 0 13px; border: 1px solid rgba(78,224,187,.46); border-radius: 12px; color: #bffdec; background: rgba(35,128,111,.25); font-size: 10px; font-weight: 850; cursor: pointer; }.fulfillment-keys header > button:hover:not(:disabled) { border-color: rgba(91,236,199,.76); background: rgba(39,153,129,.34); }.fulfillment-keys header > button:disabled { cursor: wait; opacity: .65; }.fulfillment-keys__list { display: grid; gap: 8px; margin-top: 14px; padding-top: 14px; border-top: 1px solid rgba(94,205,178,.15); }.fulfillment-keys__list article { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: center; gap: 12px; padding: 11px 12px; border: 1px solid rgba(111,196,181,.18); border-radius: 13px; background: rgba(4,14,25,.58); }.fulfillment-keys__list code { min-width: 0; color: #dffff6; font: 12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; overflow-wrap: anywhere; }.fulfillment-keys__list button { display: inline-flex; min-height: 34px; align-items: center; gap: 6px; padding: 0 10px; border: 1px solid rgba(104,171,190,.25); border-radius: 10px; color: #9fd6d2; background: rgba(32,67,84,.44); font-size: 9px; cursor: pointer; }.fulfillment-keys__list button:hover { border-color: rgba(87,226,190,.5); color: #dffff6; }.fulfillment-keys__list button svg { width: 14px; height: 14px; }.fulfillment-keys__error { margin: 11px 0 0; color: #ffaaa8; font-size: 10px; }
.fulfillment-reconciliation { margin: 0 28px 18px; padding: 17px; border: 1px solid rgba(255,190,82,.38); border-radius: 19px; background: radial-gradient(circle at 100% 0,rgba(255,181,62,.12),transparent 38%),rgba(48,35,20,.28); box-shadow: inset 0 1px rgba(255,255,255,.025); }.fulfillment-reconciliation > header { display: grid; grid-template-columns: 38px minmax(0,1fr); align-items: start; gap: 12px; }.fulfillment-reconciliation__mark { display: grid; width: 38px; height: 38px; place-items: center; border: 1px solid rgba(255,198,95,.5); border-radius: 12px; color: #ffd078; background: rgba(255,183,61,.12); font: 900 17px/1 ui-monospace,SFMono-Regular,Menlo,monospace; }.fulfillment-reconciliation header small,.fulfillment-reconciliation header strong { display: block; }.fulfillment-reconciliation header small { margin-bottom: 4px; color: #e2ad56; font-size: 9px; font-weight: 900; letter-spacing: .12em; text-transform: uppercase; }.fulfillment-reconciliation header strong { color: #fff2d7; font-size: 14px; }.fulfillment-reconciliation header p { margin: 5px 0 0; color: #c8b998; font-size: 11px; line-height: 1.5; }.fulfillment-reconciliation__choices { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 9px; margin-top: 14px; }.fulfillment-reconciliation__choices button { display: grid; grid-template-columns: 30px minmax(0,1fr); align-items: center; gap: 10px; min-height: 66px; padding: 11px; text-align: left; border: 1px solid rgba(199,175,128,.22); border-radius: 14px; color: #e8ddc7; background: rgba(17,24,44,.62); }.fulfillment-reconciliation__choices button.is-selected { border-color: rgba(255,201,99,.7); background: rgba(117,75,25,.34); box-shadow: 0 0 0 2px rgba(255,190,73,.08); }.fulfillment-reconciliation__choices strong,.fulfillment-reconciliation__choices small { display: block; }.fulfillment-reconciliation__choices strong { font-size: 11px; }.fulfillment-reconciliation__choices small { margin-top: 4px; color: #a89d88; font-size: 9px; line-height: 1.3; }.fulfillment-reconciliation__icon { display: grid; width: 30px; height: 30px; place-items: center; border: 1px solid rgba(255,202,105,.32); border-radius: 10px; color: #ffd078; background: rgba(255,190,76,.08); font-weight: 900; }.fulfillment-reconciliation__confirm { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-top: 11px; padding: 11px 12px; border: 1px dashed rgba(255,199,96,.32); border-radius: 13px; background: rgba(10,15,29,.48); }.fulfillment-reconciliation__confirm p { margin: 0; color: #c7bba4; font-size: 9px; line-height: 1.45; }.fulfillment-reconciliation__confirm button { min-height: 40px; flex: 0 0 auto; padding: 0 13px; border: 1px solid rgba(255,196,87,.58); border-radius: 11px; color: #1a1308; background: linear-gradient(135deg,#ffc55d,#ffe08c); font-size: 9px; font-weight: 900; }
.fulfillment-feature-lock { margin: 0 28px 18px; padding: 12px 14px; border: 1px solid rgba(255,194,91,.25); border-radius: 14px; color: #b9c2d8; background: rgba(77,57,28,.22); font-size: 11px; line-height: 1.5; }.fulfillment-feature-lock span { display: inline-flex; margin-right: 7px; padding: 3px 7px; border-radius: 999px; color: #ffd178; background: rgba(255,193,82,.1); font-size: 9px; font-weight: 900; letter-spacing: .07em; text-transform: uppercase; }
.fulfillment-preparation { margin: 0 28px 18px; padding: 17px; border: 1px solid rgba(92,129,239,.3); border-radius: 19px; background: rgba(11,22,50,.66); }.fulfillment-preparation > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 13px; }.fulfillment-preparation > header small,.fulfillment-preparation > header strong { display: block; }.fulfillment-preparation > header small { margin-bottom: 4px; color: #7f91b8; font-size: 9px; font-weight: 900; letter-spacing: .12em; text-transform: uppercase; }.fulfillment-preparation > header strong { font-size: 13px; }.fulfillment-preparation > header > span { flex: 0 0 auto; padding: 5px 8px; border: 1px solid rgba(91,130,255,.28); border-radius: 999px; color: #8da8ff; font-size: 8px; font-weight: 900; letter-spacing: .08em; text-transform: uppercase; }
.fulfillment-preparation__choices { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 9px; }.fulfillment-preparation__choices > button { display: grid; grid-template-columns: 29px minmax(0,1fr); align-items: center; gap: 9px; min-height: 70px; padding: 11px; text-align: left; border: 1px solid rgba(128,151,210,.24); border-radius: 14px; color: #dce5f8; background: rgba(25,39,75,.62); }.fulfillment-preparation__choices > button:hover:not(:disabled),.fulfillment-preparation__choices > button.is-active { border-color: rgba(88,128,255,.66); background: rgba(37,70,171,.28); }.fulfillment-preparation__choices svg { width: 24px; fill: none; stroke: #83a0ff; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }.fulfillment-preparation__choices strong,.fulfillment-preparation__choices small { display: block; }.fulfillment-preparation__choices strong { font-size: 11px; }.fulfillment-preparation__choices small { margin-top: 4px; color: #8999ba; font-size: 9px; line-height: 1.3; }.fulfillment-preparation__choices button:disabled { opacity: .45; cursor: not-allowed; }
.fulfillment-manual-entry { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: end; gap: 11px; margin-top: 12px; padding-top: 12px; border-top: 1px solid rgba(133,155,210,.15); }.fulfillment-manual-entry label { display: grid; gap: 6px; }.fulfillment-manual-entry label > span { color: #aebad4; font-size: 10px; font-weight: 750; }.fulfillment-manual-entry textarea { min-height: 80px; padding: 10px 11px; border: 1px solid rgba(133,155,210,.28); border-radius: 11px; color: #edf2ff; background: rgba(5,11,27,.72); font: 11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; resize: vertical; }.fulfillment-manual-entry small { justify-self: end; color: #8797b8; font-size: 9px; }.fulfillment-manual-entry small.is-complete { color: #55dfb9; }.fulfillment-manual-entry > button { min-height: 44px; padding: 0 14px; border: 1px solid rgba(84,125,255,.55); border-radius: 12px; color: #fff; background: linear-gradient(135deg,#2253de,#466fff); font-size: 10px; font-weight: 850; }.fulfillment-manual-entry > button:disabled { opacity: .45; cursor: not-allowed; }.fulfillment-preparation > p { margin: 12px 0 0; color: #8292b3; font-size: 9px; line-height: 1.5; }
.fulfillment-card__error { margin: 0 28px 18px; color: #ffaaa8; font-size: 13px; }
.fulfillment-card__actions { display: flex; min-height: 88px; align-items: center; justify-content: flex-end; gap: 18px; padding: 18px 28px 24px; border-top: 1px solid rgba(137,158,208,.16); }.fulfillment-card__actions p { min-width: 0; flex: 1; margin: 0; color: #9faccc; font-size: 12px; line-height: 1.45; }
.fulfillment-action { min-height: 50px; padding: 0 19px; border: 1px solid rgba(91,130,255,.64); border-radius: 14px; color: #fff; background: linear-gradient(135deg,#2355e7,#4b73ff); box-shadow: 0 13px 32px rgba(32,77,220,.24); font-weight: 850; }.fulfillment-action--release { color: #ffb0ae; border-color: rgba(255,150,155,.34); background: rgba(92,37,53,.48); box-shadow: none; }.fulfillment-action--quiet { color: #c7d1e7; border-color: rgba(139,160,210,.28); background: rgba(26,38,71,.65); box-shadow: none; }.fulfillment-action:disabled { opacity: .46; cursor: not-allowed; box-shadow: none; }
.fulfillment-action--send { background: linear-gradient(135deg,#1944cc,#3e68ff); }
.fulfillment-card-enter-active,.fulfillment-card-leave-active { transition: opacity .2s ease; }.fulfillment-card-enter-active .fulfillment-card,.fulfillment-card-leave-active .fulfillment-card { transition: transform .22s ease,opacity .2s ease; }.fulfillment-card-enter-from,.fulfillment-card-leave-to { opacity: 0; }.fulfillment-card-enter-from .fulfillment-card,.fulfillment-card-leave-to .fulfillment-card { opacity: 0; transform: translateY(12px) scale(.985); }
@keyframes fulfillment-spin { to { transform: rotate(360deg); } }
@media (max-width:620px) { .fulfillment-backdrop { padding: 10px; }.fulfillment-card { max-height: calc(100vh - 20px); border-radius: 22px; }.fulfillment-card__header,.fulfillment-card__actions { padding-right: 18px; padding-left: 18px; }.fulfillment-product,.fulfillment-state,.fulfillment-metrics,.fulfillment-safety,.fulfillment-keys,.fulfillment-reconciliation,.fulfillment-feature-lock,.fulfillment-preparation,.fulfillment-card__error { margin-right: 18px; margin-left: 18px; }.fulfillment-metrics,.fulfillment-preparation__choices,.fulfillment-reconciliation__choices { grid-template-columns: 1fr; }.fulfillment-keys > header { grid-template-columns: 40px minmax(0,1fr); }.fulfillment-keys header > button { grid-column: 1 / -1; width: 100%; }.fulfillment-keys__list article { grid-template-columns: 1fr; }.fulfillment-keys__list button { justify-content: center; }.fulfillment-manual-entry { grid-template-columns: 1fr; }.fulfillment-reconciliation__confirm { align-items: stretch; flex-direction: column; }.fulfillment-reconciliation__confirm button { width: 100%; }.fulfillment-card__actions { align-items: stretch; flex-direction: column; }.fulfillment-action { width: 100%; } }
.fulfillment-support-message__body { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: start; gap: 14px; margin: 14px 0 0; padding: 14px; border-top: 1px solid rgba(94,205,178,.15); border-radius: 13px; background: rgba(4,14,25,.58); }.fulfillment-support-message__body p { margin: 0; color: #dffff6; font: 11px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; white-space: pre-wrap; overflow-wrap: anywhere; }.fulfillment-support-message__body button { display: inline-flex; min-height: 34px; align-items: center; justify-content: center; gap: 6px; padding: 0 10px; border: 1px solid rgba(104,171,190,.25); border-radius: 10px; color: #9fd6d2; background: rgba(32,67,84,.44); font-size: 9px; cursor: pointer; }.fulfillment-support-message__body button:hover { border-color: rgba(87,226,190,.5); color: #dffff6; }.fulfillment-support-message__body button svg { width: 14px; height: 14px; }
@media (max-width:620px) { .fulfillment-support-message__body { grid-template-columns: 1fr; }.fulfillment-support-message__body button { width: 100%; } }
</style>
