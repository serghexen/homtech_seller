<script setup>
import { onMounted, ref } from 'vue'
import { apiRequest } from '../api.js'

const amount = ref('100')
const info = ref({ enabled: false, items: [] })
const loading = ref(false)
const error = ref('')
const link = ref('')
let creationKey
let requestedAmount

async function refresh() {
  try { info.value = await apiRequest('/steam-topups') }
  catch (failure) { error.value = failure.message }
}

async function create() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  // Не меняем сумму и ключ при потере ответа на создание ссылки.
  creationKey ||= crypto.randomUUID()
  requestedAmount ||= amount.value
  try {
    const result = await apiRequest('/steam-topups', {
      method: 'POST', body: JSON.stringify({ amount: requestedAmount, request_key: creationKey }),
    })
    const url = new URL('https://market.homtech.app/')
    url.hash = new URLSearchParams({ topup: result.token }).toString()
    link.value = url.toString()
    creationKey = null
    requestedAmount = null
    await refresh()
  } catch (failure) { error.value = failure.message }
  finally { loading.value = false }
}

async function cancel(id) {
  try {
    await apiRequest(`/steam-topups/${id}/cancel`, { method: 'POST' })
    await refresh()
  } catch (failure) { error.value = failure.message }
}

async function reconcile(id) {
  try {
    await apiRequest(`/steam-topups/${id}/reconcile`, { method: 'POST' })
    await refresh()
  } catch (failure) { error.value = failure.message }
}

const states = { ready: 'Ожидает логин', queued: 'В очереди', processing: 'В обработке', succeeded: 'Пополнено', failed: 'Отказ', attention: 'Нужна сверка', cancelled: 'Отменена' }
onMounted(refresh)
</script>

<template>
  <section class="steam-pilot">
    <p class="kicker">РУЧНОЙ ПИЛОТ</p>
    <h1>Пополнение Steam</h1>
    <p>Создайте персональную ссылку с фиксированной суммой. Магазин и заказ пока не нужны.</p>
    <p v-if="!info.enabled" class="steam-pilot__notice">Пилот выключен. Для включения нужно настроить разрешение и бюджет вашей рабочей области.</p>
    <form @submit.prevent="create">
      <label>Сумма к отправке поставщику, ₽<input v-model="amount" type="number" min="16.99" :max="info.max_amount || 100" step="0.01" required :disabled="loading || !info.enabled" /></label>
      <button type="submit" class="primary-button" :disabled="loading || !info.enabled">{{ loading ? 'Создаём…' : 'Создать ссылку' }}</button>
    </form>
    <p>При нажатии «Пополнить» по ссылке будет использован баланс поставщика. Итог в валюте Steam зависит от конвертации.</p>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <div v-if="link" class="steam-pilot__link">
      <label>Персональная ссылка<input :value="link" readonly aria-label="Персональная ссылка" @focus="$event.target.select()" /></label>
      <a :href="link" target="_blank" rel="noopener noreferrer">Открыть форму пополнения ↗</a>
    </div>
    <div class="steam-pilot__history">
      <h2>Последние ссылки</h2><button type="button" @click="refresh">Обновить</button>
      <p v-if="info.queue">В очереди: {{ info.queue.depth }} · Требуют сверки: {{ info.queue.attention_count }} · Повторов связи: {{ info.queue.retries }}</p>
      <article v-for="item in info.items" :key="item.id">
        <strong>{{ item.amount }} ₽</strong><span>{{ item.account || 'Логин ещё не введён' }}</span><span>{{ states[item.state] }}</span>
        <button v-if="item.state === 'ready'" type="button" @click="cancel(item.id)">Отменить ссылку</button>
        <button v-if="item.state === 'attention'" type="button" @click="reconcile(item.id)">Сверить прежнюю операцию</button>
      </article>
    </div>
  </section>
</template>

<style scoped>
.steam-pilot { max-width: 900px; margin: 24px auto; padding: 30px; border: 1px solid #334269; border-radius: 24px; background: #111a35; }
.steam-pilot h1 { font-size: 34px; margin: 8px 0 16px; }
.steam-pilot p { color: #aeb9d4; line-height: 1.6; }
.steam-pilot form { display: flex; gap: 16px; align-items: end; margin: 22px 0; }
.steam-pilot label { display: grid; gap: 8px; flex: 1; color: #d2d9ed; font-size: 13px; }
.steam-pilot input { width: 100%; padding: 13px; border: 1px solid #455679; border-radius: 10px; background: #0b1228; color: white; font: inherit; }
.steam-pilot__link { padding: 18px; background: #1a2747; border-radius: 12px; }
.steam-pilot__link a { display: inline-block; color: #c6adff; margin-top: 12px; }
.steam-pilot__history { margin-top: 30px; }
.steam-pilot__history article { display: flex; flex-wrap: wrap; align-items: center; gap: 18px; padding: 16px 0; border-bottom: 1px solid #334269; }
.steam-pilot__history button { color: #bbc9ee; background: none; border: 1px solid #455679; border-radius: 8px; padding: 8px 12px; cursor: pointer; }
@media (max-width: 620px) { .steam-pilot { padding: 20px; } .steam-pilot form { align-items: stretch; flex-direction: column; } }
</style>
