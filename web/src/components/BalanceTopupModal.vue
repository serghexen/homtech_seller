<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { apiRequest } from '../api'
import sbpLogo from '../assets/sbp-logo.png'
import sbpSign from '../assets/sbp-sign.png'

const props = defineProps({
  open: { type: Boolean, default: false },
  balance: { type: Object, default: null },
})
const emit = defineEmits(['close', 'balance-updated'])

const amount = ref('1000')
const topup = ref(null)
const busy = ref(false)
const demoBusy = ref('')
const error = ref('')
let pollTimer = null

const amountKopecks = computed(() => {
  const normalized = amount.value.trim().replace(',', '.')
  if (!/^\d+(?:\.\d{0,2})?$/.test(normalized)) return 0
  return Math.round(Number(normalized) * 100)
})
const validAmount = computed(() => amountKopecks.value >= 10_000 && amountKopecks.value <= 10_000_000)
const formattedBalance = computed(() => formatRubles(props.balance?.available_amount || 0))
const isFinished = computed(() => ['confirmed', 'rejected', 'expired', 'cancelled', 'failed'].includes(topup.value?.state))
const statusLabel = computed(() => ({
  pending: 'Ожидаем оплату',
  confirmed: 'Баланс пополнен',
  rejected: 'Платёж отклонён',
  expired: 'Время оплаты истекло',
  cancelled: 'Платёж отменён',
  failed: 'Не удалось создать платёж',
  init_unknown: 'Уточняем статус платежа',
}[topup.value?.state] || 'Готовим платёж'))

function formatRubles(kopecks) {
  return new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB', maximumFractionDigits: 2 })
    .format(Number(kopecks || 0) / 100)
}

function chooseAmount(value) {
  amount.value = String(value)
  error.value = ''
}

function clearPolling() {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = null
}

async function pollTopup() {
  if (!topup.value?.id || isFinished.value || !props.open) return
  try {
    const result = await apiRequest(`/billing/topups/${topup.value.id}`)
    const previousState = topup.value.state
    topup.value = result
    if (result.state === 'confirmed' && previousState !== 'confirmed') emit('balance-updated')
  } catch {
    // Webhook и worker продолжают сверку; краткая ошибка polling не закрывает QR пользователя.
  }
  if (!isFinished.value && props.open) pollTimer = window.setTimeout(pollTopup, 2500)
}

async function createTopup() {
  if (!props.balance?.topups_enabled) {
    error.value = 'Тестовые платежи ещё не включены на сервере'
    return
  }
  if (!validAmount.value) {
    error.value = 'Введите сумму от 100 до 100 000 ₽'
    return
  }
  busy.value = true
  error.value = ''
  try {
    topup.value = await apiRequest('/billing/topups', {
      method: 'POST',
      body: JSON.stringify({ amount: amountKopecks.value }),
    })
    clearPolling()
    pollTimer = window.setTimeout(pollTopup, 1500)
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось сформировать QR-код'
  } finally {
    busy.value = false
  }
}

async function simulateDemo(outcome) {
  if (!topup.value?.id || demoBusy.value) return
  demoBusy.value = outcome
  error.value = ''
  try {
    topup.value = await apiRequest(`/billing/topups/${topup.value.id}/demo`, {
      method: 'POST',
      body: JSON.stringify({ outcome }),
    })
    clearPolling()
    pollTimer = window.setTimeout(pollTopup, 700)
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось запустить демо-сценарий'
  } finally {
    demoBusy.value = ''
  }
}

function startAgain() {
  clearPolling()
  topup.value = null
  error.value = ''
}

function close() {
  clearPolling()
  emit('close')
}

function onKeydown(event) {
  if (props.open && event.key === 'Escape') close()
}

watch(() => props.open, (open) => {
  if (!open) clearPolling()
  if (open && topup.value && !isFinished.value) pollTopup()
})

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => {
  clearPolling()
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="topup-modal">
      <div v-if="open" class="topup-backdrop" @click.self="close">
        <section class="topup-modal" role="dialog" aria-modal="true" aria-labelledby="topup-title">
          <button class="topup-close" type="button" aria-label="Закрыть пополнение" @click="close">×</button>

          <header class="topup-head">
            <span class="topup-head__mark"><img :src="sbpSign" alt="" /></span>
            <div>
              <span>{{ balance?.demo_mode ? 'ДЕМО · СБП' : 'СБП' }}</span>
              <h2 id="topup-title">Пополнить баланс</h2>
            </div>
          </header>

          <div class="topup-balance">
            <span>Баланс аккаунта</span>
            <strong>{{ formattedBalance }}</strong>
            <small>Общий для всех магазинов</small>
          </div>

          <template v-if="!topup">
            <form class="topup-form" @submit.prevent="createTopup">
              <label for="topup-amount">Сумма пополнения</label>
              <div class="topup-amount">
                <input
                  id="topup-amount"
                  v-model="amount"
                  type="text"
                  inputmode="decimal"
                  autocomplete="off"
                  aria-describedby="topup-limits"
                  @input="error = ''"
                />
                <span>₽</span>
              </div>
              <div class="topup-presets" aria-label="Быстрый выбор суммы">
                <button v-for="value in [500, 1000, 3000, 5000]" :key="value" type="button" @click="chooseAmount(value)">
                  {{ new Intl.NumberFormat('ru-RU').format(value) }} ₽
                </button>
              </div>
              <p id="topup-limits" class="topup-hint">От 100 до 100 000 ₽ · QR действует 15 минут</p>
              <p v-if="!balance?.topups_enabled" class="topup-disabled">
                Контур готов, но платежи выключены. Добавьте тестовые реквизиты Т-Банка в `.env` и включите kill switch.
              </p>
              <p v-if="error" class="topup-error" role="alert">{{ error }}</p>
              <button class="topup-submit" type="submit" :disabled="busy || !validAmount || !balance?.topups_enabled">
                <img :src="sbpSign" alt="" />
                <span>{{ busy ? 'Формируем QR…' : 'Сформировать QR' }}</span>
              </button>
            </form>
          </template>

          <div v-else class="topup-payment">
            <div class="topup-status" :class="`topup-status--${topup.state}`">
              <span class="topup-status__dot" aria-hidden="true"></span>
              <div><small>СТАТУС ПЛАТЕЖА</small><strong>{{ statusLabel }}</strong></div>
            </div>
            <div v-if="topup.qr_data_url && topup.state === 'pending'" class="topup-qr">
              <img class="topup-qr__brand" :src="sbpLogo" alt="Система быстрых платежей" />
              <img class="topup-qr__image" :src="topup.qr_data_url" alt="QR-код для оплаты через СБП" />
              <strong>К оплате: {{ formatRubles(topup.amount) }}</strong>
              <p>Отсканируйте QR камерой телефона и подтвердите платёж в приложении банка.</p>
            </div>
            <div v-if="balance?.demo_mode && topup.state === 'pending'" class="topup-demo">
              <span>ТЕСТОВЫЙ ТЕРМИНАЛ</span>
              <p>Эмулируйте ответ банка без реального списания.</p>
              <div>
                <button type="button" :disabled="Boolean(demoBusy)" @click="simulateDemo('success')">{{ demoBusy === 'success' ? 'Запускаем…' : 'Успех' }}</button>
                <button type="button" :disabled="Boolean(demoBusy)" @click="simulateDemo('rejected')">Отказ</button>
                <button type="button" :disabled="Boolean(demoBusy)" @click="simulateDemo('deadline_expired')">Таймаут</button>
              </div>
            </div>
            <p v-if="error" class="topup-error" role="alert">{{ error }}</p>
            <div v-else class="topup-result" :class="{ 'topup-result--success': topup.state === 'confirmed' }">
              <span aria-hidden="true">{{ topup.state === 'confirmed' ? '✓' : '!' }}</span>
              <strong>{{ statusLabel }}</strong>
              <p v-if="topup.state === 'confirmed'">{{ formatRubles(topup.amount) }} уже доступны на общем балансе аккаунта.</p>
              <p v-else>Деньги на баланс не начислены.</p>
            </div>
            <button v-if="isFinished" class="topup-again" type="button" @click="startAgain">Пополнить ещё</button>
          </div>

          <footer class="topup-footer">
            <span>Оплата проходит на стороне банка</span>
            <span>Безопасное соединение</span>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.topup-backdrop { position: fixed; z-index: 140; inset: 0; display: grid; place-items: center; padding: 24px; overflow: auto; background: rgba(3,7,20,.78); backdrop-filter: blur(13px); }
.topup-modal { position: relative; width: min(100%,520px); padding: 28px; overflow: hidden; border: 1px solid rgba(137,155,207,.34); border-radius: 28px; color: #eef3ff; background: radial-gradient(circle at 100% 0,rgba(59,91,198,.18),transparent 40%),linear-gradient(150deg,#111a38,#091027 74%); box-shadow: 0 35px 110px rgba(0,0,0,.55),inset 0 1px rgba(255,255,255,.04); }
.topup-close { position: absolute; z-index: 2; top: 18px; right: 18px; display: grid; width: 36px; height: 36px; place-items: center; padding: 0; border: 1px solid rgba(143,159,201,.25); border-radius: 12px; color: #aeb9d1; background: rgba(31,41,72,.72); font-size: 24px; line-height: 1; }
.topup-head { display: flex; align-items: center; gap: 14px; padding-right: 44px; }.topup-head__mark { display: grid; width: 52px; height: 52px; place-items: center; overflow: hidden; border: 1px solid rgba(130,146,193,.25); border-radius: 16px; background: #f5f1e8; }.topup-head__mark img { width: 34px; height: 28px; object-fit: contain; transform: scale(2); }.topup-head span { color: #6ee4c4; font-size: 10px; font-weight: 900; letter-spacing: .15em; }.topup-head h2 { margin: 3px 0 0; font-size: 28px; letter-spacing: -.035em; }
.topup-balance { display: grid; grid-template-columns: 1fr auto; align-items: end; gap: 3px 14px; margin: 24px 0; padding: 17px 18px; border: 1px solid rgba(123,145,208,.22); border-radius: 18px; background: rgba(21,31,65,.65); }.topup-balance span { color: #aab6d0; font-size: 12px; font-weight: 700; }.topup-balance strong { grid-row: 1 / 3; grid-column: 2; font-size: 22px; }.topup-balance small { color: #6f7e9f; font-size: 10px; }
.topup-form { display: grid; gap: 13px; }.topup-form > label { color: #cad3e7; font-size: 12px; font-weight: 800; }.topup-amount { position: relative; }.topup-amount input { width: 100%; height: 66px; padding: 0 58px 0 18px; border: 1px solid rgba(121,145,211,.38); border-radius: 17px; outline: 0; color: #fff; background: rgba(7,13,32,.72); font-size: 27px; font-weight: 850; font-variant-numeric: tabular-nums; }.topup-amount input:focus { border-color: #6386ff; box-shadow: 0 0 0 4px rgba(75,115,255,.15); }.topup-amount > span { position: absolute; top: 50%; right: 20px; color: #8592b1; font-size: 22px; transform: translateY(-50%); }
.topup-presets { display: grid; grid-template-columns: repeat(4,1fr); gap: 7px; }.topup-presets button,.topup-again { min-height: 38px; border: 1px solid rgba(139,157,205,.24); border-radius: 11px; color: #bec9e2; background: rgba(32,44,80,.6); font-size: 11px; font-weight: 800; }.topup-presets button:hover,.topup-again:hover { color: #fff; border-color: rgba(106,134,220,.54); background: rgba(43,59,104,.8); }
.topup-hint { margin: -3px 0 0; color: #7886a5; font-size: 10px; }.topup-disabled,.topup-error { margin: 0; padding: 11px 13px; border: 1px solid rgba(237,174,83,.28); border-radius: 12px; color: #e8c98f; background: rgba(103,67,25,.24); font-size: 11px; line-height: 1.55; }.topup-error { border-color: rgba(255,116,132,.3); color: #ffb6c0; background: rgba(98,31,47,.25); }
.topup-submit { display: flex; min-height: 54px; align-items: center; justify-content: center; gap: 12px; margin-top: 2px; overflow: hidden; border: 0; border-radius: 15px; color: #1d1346; background: #f5f1e8; box-shadow: 0 14px 28px rgba(0,0,0,.2); font-weight: 900; }.topup-submit img { width: 32px; height: 27px; object-fit: contain; transform: scale(1.8); }.topup-submit:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 17px 32px rgba(0,0,0,.28); }
.topup-payment { display: grid; gap: 16px; }.topup-status { display: flex; align-items: center; gap: 11px; padding: 12px 14px; border: 1px solid rgba(107,137,223,.25); border-radius: 14px; background: rgba(25,38,76,.58); }.topup-status__dot { width: 9px; height: 9px; border-radius: 50%; background: #f2bf5b; box-shadow: 0 0 0 5px rgba(242,191,91,.1); animation: topup-pulse 1.5s ease-in-out infinite; }.topup-status div { display: grid; gap: 2px; }.topup-status small { color: #7e8ba9; font-size: 8px; font-weight: 900; letter-spacing: .13em; }.topup-status strong { font-size: 13px; }.topup-status--confirmed .topup-status__dot { background: #55e2bd; box-shadow: 0 0 0 5px rgba(85,226,189,.11); animation: none; }
.topup-qr { display: grid; place-items: center; padding: 24px; border-radius: 22px; color: #1d1346; background: #f5f1e8; text-align: center; }.topup-qr__brand { width: 148px; height: auto; margin-bottom: 13px; object-fit: contain; }.topup-qr__image { width: min(100%,260px); aspect-ratio: 1; object-fit: contain; border: 10px solid #fff; border-radius: 12px; background: #fff; }.topup-qr strong { margin-top: 14px; font-size: 19px; }.topup-qr p { max-width: 340px; margin: 7px 0 0; color: #51496d; font-size: 11px; line-height: 1.5; }
.topup-result { display: grid; min-height: 240px; place-items: center; align-content: center; gap: 8px; padding: 28px; border: 1px solid rgba(242,174,88,.25); border-radius: 20px; background: rgba(87,54,30,.17); text-align: center; }.topup-result > span { display: grid; width: 52px; height: 52px; place-items: center; border-radius: 50%; color: #111a38; background: #eab563; font-size: 25px; font-weight: 900; }.topup-result strong { font-size: 19px; }.topup-result p { margin: 0; color: #aeb9d1; font-size: 12px; }.topup-result--success { border-color: rgba(85,226,189,.3); background: rgba(34,103,87,.18); }.topup-result--success > span { background: #55e2bd; }
.topup-demo { padding: 13px; border: 1px dashed rgba(119,142,208,.3); border-radius: 14px; background: rgba(16,25,55,.6); }.topup-demo > span { color: #7485ae; font-size: 8px; font-weight: 900; letter-spacing: .14em; }.topup-demo > p { margin: 4px 0 10px; color: #a3afca; font-size: 10px; }.topup-demo > div { display: grid; grid-template-columns: repeat(3,1fr); gap: 6px; }.topup-demo button { min-height: 34px; border: 1px solid rgba(125,148,211,.26); border-radius: 10px; color: #cbd5e9; background: rgba(37,51,91,.72); font-size: 10px; font-weight: 800; }.topup-demo button:first-child { color: #74e8c8; border-color: rgba(85,226,189,.3); }.topup-demo button:hover:not(:disabled) { border-color: rgba(130,157,230,.55); background: rgba(50,68,118,.88); }
.topup-again { justify-self: center; min-width: 150px; }.topup-footer { display: flex; justify-content: space-between; gap: 12px; margin-top: 22px; padding-top: 15px; border-top: 1px solid rgba(128,146,193,.16); color: #697794; font-size: 9px; }
.topup-modal-enter-active,.topup-modal-leave-active { transition: opacity .2s ease; }.topup-modal-enter-active .topup-modal,.topup-modal-leave-active .topup-modal { transition: transform .22s ease,opacity .2s ease; }.topup-modal-enter-from,.topup-modal-leave-to { opacity: 0; }.topup-modal-enter-from .topup-modal,.topup-modal-leave-to .topup-modal { opacity: 0; transform: translateY(14px) scale(.98); }
@keyframes topup-pulse { 50% { opacity: .45; transform: scale(.8); } }
@media (max-width:560px) { .topup-backdrop { align-items: end; padding: 0; }.topup-modal { width: 100%; max-height: 94vh; padding: 22px 18px; overflow-y: auto; border-right: 0; border-bottom: 0; border-left: 0; border-radius: 25px 25px 0 0; }.topup-presets { grid-template-columns: repeat(2,1fr); }.topup-footer { flex-direction: column; gap: 4px; }.topup-qr { padding: 18px; }.topup-qr__image { width: min(100%,230px); } }
@media (prefers-reduced-motion:reduce) { .topup-status__dot { animation: none; } }
</style>
