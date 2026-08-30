<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { apiRequest } from '../api'
import yandexMarketLogo from '../assets/yandex-market-logo.png'

const props = defineProps({
  connections: { type: Array, default: () => [] },
  connectionId: { type: Number, default: null },
  connectionName: { type: String, default: '' },
  dialog: { type: Boolean, default: false },
})
const emit = defineEmits(['pending-change'])

const items = ref([])
const total = ref(0)
const pendingTotal = ref(0)
const state = ref('pending')
const page = ref(1)
const pageSize = 20
const selectedConnectionId = ref(props.connectionId)
const loading = ref(false)
const error = ref('')
const sendingReviewId = ref(0)
const drafts = reactive({})
let refreshTimer = null
let requestSequence = 0

const yandexConnections = computed(() => props.connections.filter((item) => item.provider_code === 'yandex_market'))
const connectionScoped = computed(() => Number.isInteger(props.connectionId) && props.connectionId > 0)
const selectedStoreName = computed(() => props.connectionName
  || yandexConnections.value.find((item) => item.id === selectedConnectionId.value)?.display_name
  || items.value[0]?.store_name
  || 'магазина')
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const hasActiveJobs = computed(() => items.value.some((item) => ['queued', 'preparing', 'sending'].includes(item.reply?.state)))

function queryString(values) {
  const query = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') query.set(key, String(value))
  })
  return query.toString()
}

function clearRefreshTimer() {
  if (refreshTimer) window.clearTimeout(refreshTimer)
  refreshTimer = null
}

function scheduleRefresh() {
  clearRefreshTimer()
  if (!hasActiveJobs.value) return
  refreshTimer = window.setTimeout(async () => {
    refreshTimer = null
    await loadReviews({ silent: true })
    scheduleRefresh()
  }, 3000)
}

async function loadReviews({ silent = false } = {}) {
  const sequence = ++requestSequence
  if (!silent) loading.value = true
  error.value = ''
  try {
    const query = queryString({
      connection_id: selectedConnectionId.value,
      state: state.value,
      page: page.value,
      page_size: pageSize,
    })
    const result = await apiRequest(`/marketplaces/reviews?${query}`)
    if (sequence !== requestSequence) return
    items.value = Array.isArray(result.items) ? result.items : []
    total.value = Number(result.total || 0)
    pendingTotal.value = Number(result.pending_total || 0)
    emit('pending-change', pendingTotal.value)
    items.value.forEach((review) => {
      if (drafts[review.id] === undefined) drafts[review.id] = ''
    })
    scheduleRefresh()
  } catch (requestError) {
    if (sequence === requestSequence) error.value = requestError.message || 'Не удалось загрузить отзывы'
  } finally {
    if (!silent && sequence === requestSequence) loading.value = false
  }
}

async function selectState(nextState) {
  if (!['pending', 'all'].includes(nextState)) return
  state.value = nextState
  page.value = 1
  await loadReviews()
}

async function selectConnection(connectionId) {
  selectedConnectionId.value = connectionId
  page.value = 1
  await loadReviews()
}

async function changePage(nextPage) {
  page.value = Math.min(Math.max(1, nextPage), pageCount.value)
  await loadReviews()
}

async function sendReply(review) {
  const text = String(drafts[review.id] || '').trim()
  if (!text || sendingReviewId.value) return
  sendingReviewId.value = review.id
  error.value = ''
  try {
    await apiRequest(`/marketplaces/reviews/${review.id}/reply`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    })
    drafts[review.id] = ''
    await loadReviews({ silent: true })
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось поставить ответ в очередь'
  } finally {
    sendingReviewId.value = 0
  }
}

function reviewDate(value) {
  if (!value) return 'Дата не указана'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Дата не указана'
  return new Intl.DateTimeFormat('ru-RU', {
    day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(date)
}

function replyLabel(reply) {
  if (!reply) return ''
  if (['queued', 'preparing'].includes(reply.state)) return 'Ответ в очереди'
  if (reply.state === 'sending') return 'Публикуем в Маркете'
  if (reply.state === 'submitted' && reply.provider_status === 'UNMODERATED') return 'Ответ на модерации'
  if (reply.state === 'submitted') return 'Ответ опубликован'
  if (reply.state === 'unknown') return 'Нужно проверить в кабинете Яндекса'
  return 'Ответ не отправлен'
}

onMounted(loadReviews)
onBeforeUnmount(() => {
  requestSequence += 1
  clearRefreshTimer()
})
</script>

<template>
  <section class="reviews-view" :class="{ 'reviews-view--dialog': dialog }" aria-labelledby="reviews-title">
    <header class="reviews-hero">
      <div>
        <p class="reviews-kicker">ГОЛОС ПОКУПАТЕЛЯ</p>
        <h1 id="reviews-title">Отзывы <span v-if="connectionScoped">· {{ selectedStoreName }}</span></h1>
        <p>Читайте опубликованные отзывы и отвечайте покупателям от имени выбранного магазина.</p>
      </div>
      <div class="reviews-hero__counter" aria-label="Отзывы, требующие ответа">
        <span>ТРЕБУЮТ ОТВЕТА</span>
        <strong>{{ pendingTotal }}</strong>
      </div>
    </header>

    <div class="reviews-toolbar">
      <div class="reviews-tabs" role="tablist" aria-label="Статус отзывов">
        <button type="button" :class="{ active: state === 'pending' }" @click="selectState('pending')">
          Требуют ответа <small>{{ pendingTotal }}</small>
        </button>
        <button type="button" :class="{ active: state === 'all' }" @click="selectState('all')">Все сохранённые</button>
      </div>
      <div v-if="!connectionScoped" class="reviews-stores" aria-label="Фильтр по магазину">
        <button type="button" :class="{ active: selectedConnectionId === null }" @click="selectConnection(null)">Все магазины</button>
        <button
          v-for="connection in yandexConnections"
          :key="connection.id"
          type="button"
          :class="{ active: selectedConnectionId === connection.id }"
          @click="selectConnection(connection.id)"
        >
          {{ connection.display_name }}
        </button>
      </div>
    </div>

    <p v-if="error" class="reviews-error" role="alert">{{ error }}</p>
    <div v-if="loading" class="reviews-empty">Загружаем отзывы…</div>
    <div v-else-if="!items.length" class="reviews-empty reviews-empty--clear">
      <span aria-hidden="true">✓</span>
      <h2>{{ state === 'pending' ? 'Все отзывы обработаны' : 'Сохранённых отзывов пока нет' }}</h2>
      <p>{{ state === 'pending' ? 'Новые отзывы появятся здесь после синхронизации с Яндекс Маркетом.' : 'Seller сохранит отзывы, когда Маркет передаст их при следующем обновлении.' }}</p>
    </div>

    <div v-else class="reviews-list">
      <article v-for="review in items" :key="review.id" class="review-card" :class="{ 'review-card--done': !review.need_reaction }">
        <div class="review-card__rail">
          <div class="review-market"><img :src="yandexMarketLogo" alt="" /></div>
          <div class="review-rating" :aria-label="`Оценка ${review.rating || 0} из 5`">
            <span v-for="star in 5" :key="star" :class="{ lit: star <= (review.rating || 0) }">★</span>
          </div>
          <span class="review-state" :class="{ 'review-state--done': !review.need_reaction }">
            {{ review.need_reaction ? 'ЖДЁТ ОТВЕТА' : 'ОБРАБОТАН' }}
          </span>
        </div>

        <div class="review-card__content">
          <header class="review-card__head">
            <div>
              <p>{{ review.store_name }}</p>
              <h2>{{ review.product_title || review.offer_id || 'Товар Яндекс Маркета' }}</h2>
            </div>
            <div class="review-card__byline">
              <strong>{{ review.author || 'Покупатель' }}</strong>
              <time>{{ reviewDate(review.created_at) }}</time>
            </div>
          </header>

          <div class="review-copy">
            <div v-if="review.advantages"><span>ПЛЮСЫ</span><p>{{ review.advantages }}</p></div>
            <div v-if="review.disadvantages" class="review-copy__minus"><span>МИНУСЫ</span><p>{{ review.disadvantages }}</p></div>
            <div v-if="review.comment" class="review-copy__main"><span>КОММЕНТАРИЙ</span><p>{{ review.comment }}</p></div>
            <p v-if="!review.advantages && !review.disadvantages && !review.comment" class="review-copy__silent">Покупатель оставил оценку без текста.</p>
          </div>

          <div v-if="review.photos.length" class="review-media">
            <img v-for="photo in review.photos" :key="photo" :src="photo" alt="Фотография покупателя к отзыву" loading="lazy" />
          </div>

          <div class="review-meta">
            <span v-if="review.external_order_id">Заказ №{{ review.external_order_id }}</span>
            <span v-if="review.offer_id">SKU {{ review.offer_id }}</span>
            <span v-if="review.recommended === true">Рекомендует товар</span>
          </div>

          <div v-if="review.reply" class="review-reply-state" :class="`review-reply-state--${review.reply.state}`">
            <div><span>{{ replyLabel(review.reply) }}</span><p>{{ review.reply.text }}</p></div>
            <small v-if="review.reply.last_error">{{ review.reply.last_error }}</small>
          </div>

          <form v-if="review.need_reaction && review.can_reply" class="review-compose" @submit.prevent="sendReply(review)">
            <label :for="`reply-${review.id}`">Ответ магазина</label>
            <textarea
              :id="`reply-${review.id}`"
              v-model="drafts[review.id]"
              maxlength="4096"
              rows="4"
              placeholder="Напишите покупателю человеческий, конкретный ответ…"
            ></textarea>
            <footer>
              <span>Без контактов магазина и внешних ссылок · {{ String(drafts[review.id] || '').length }}/4096</span>
              <button type="submit" :disabled="!String(drafts[review.id] || '').trim() || sendingReviewId === review.id">
                {{ sendingReviewId === review.id ? 'Ставим в очередь…' : 'Отправить ответ' }}
              </button>
            </footer>
          </form>
          <p v-else-if="review.need_reaction && !review.reply" class="review-compose-disabled">
            Ручные ответы подготовлены, но публикация для этого магазина пока не включена.
          </p>
        </div>
      </article>
    </div>

    <nav v-if="pageCount > 1" class="reviews-pagination" aria-label="Страницы отзывов">
      <button type="button" :disabled="page === 1" @click="changePage(page - 1)">← Назад</button>
      <span>{{ page }} / {{ pageCount }}</span>
      <button type="button" :disabled="page === pageCount" @click="changePage(page + 1)">Далее →</button>
    </nav>
  </section>
</template>

<style scoped>
.reviews-view { display:grid; gap:20px; margin-top:42px; color:#eef3ff; }
.reviews-view--dialog { margin-top:0; }
.reviews-hero { position:relative; display:flex; align-items:end; justify-content:space-between; gap:28px; padding:28px 30px; overflow:hidden; border:1px solid rgba(135,158,214,.22); border-radius:26px; background:linear-gradient(125deg,rgba(16,27,60,.92),rgba(29,37,67,.82)); box-shadow:inset 0 1px rgba(255,255,255,.035); }
.reviews-hero::after { position:absolute; right:-80px; bottom:-130px; width:320px; height:320px; border:1px solid rgba(255,205,82,.12); border-radius:50%; content:''; box-shadow:0 0 90px rgba(255,205,82,.04); }
.reviews-hero h1 { margin:3px 0 7px; font-size:clamp(42px,5vw,72px); line-height:.96; letter-spacing:-.065em; }
.reviews-hero h1 span { color:#ffd269; font-size:.38em; letter-spacing:-.025em; white-space:nowrap; }
.reviews-view--dialog .reviews-hero { padding-right:78px; }
.reviews-view--dialog .reviews-hero h1 { font-size:clamp(34px,4vw,58px); }
.reviews-hero p { margin:0; color:#aebbd5; line-height:1.55; }
.reviews-kicker { color:#f5c85d !important; font-size:10px; font-weight:900; letter-spacing:.17em; }
.reviews-hero__counter { position:relative; z-index:1; display:grid; min-width:176px; gap:2px; padding-left:22px; border-left:1px solid rgba(255,210,94,.28); text-align:right; }
.reviews-hero__counter span { color:#9daac4; font-size:9px; font-weight:900; letter-spacing:.14em; }
.reviews-hero__counter strong { color:#ffd260; font-size:54px; line-height:1; letter-spacing:-.06em; }
.reviews-toolbar { display:flex; align-items:center; justify-content:space-between; gap:14px; }
.reviews-tabs,.reviews-stores { display:flex; gap:6px; overflow:auto; padding:5px; border:1px solid rgba(139,158,208,.2); border-radius:16px; background:rgba(10,18,41,.54); }
.reviews-tabs button,.reviews-stores button { min-height:42px; flex:0 0 auto; padding:0 15px; border:1px solid transparent; border-radius:11px; color:#aeb9d1; background:transparent; font:inherit; font-size:12px; font-weight:800; }
.reviews-tabs button.active,.reviews-stores button.active { color:#fff; border-color:rgba(88,126,255,.58); background:linear-gradient(135deg,#234fda,#4169f0); }
.reviews-tabs small { margin-left:7px; padding:2px 7px; border-radius:999px; background:rgba(255,255,255,.12); }
.reviews-error { margin:0; padding:13px 15px; border:1px solid rgba(255,139,147,.35); border-radius:14px; color:#ffadb2; background:rgba(255,91,105,.08); }
.reviews-empty { display:grid; min-height:220px; place-items:center; padding:30px; border:1px dashed rgba(139,158,208,.24); border-radius:24px; color:#aeb9d2; background:rgba(12,20,44,.42); }
.reviews-empty--clear { align-content:center; gap:6px; text-align:center; }
.reviews-empty--clear > span { display:grid; width:54px; height:54px; place-items:center; margin-bottom:7px; border:1px solid rgba(78,230,189,.35); border-radius:17px; color:#58e5bd; background:rgba(78,230,189,.08); font-size:25px; }
.reviews-empty h2,.reviews-empty p { margin:0; }.reviews-empty h2 { color:#edf2ff; }.reviews-empty p { max-width:520px; line-height:1.55; }
.reviews-list { display:grid; gap:14px; }
.review-card { display:grid; grid-template-columns:145px minmax(0,1fr); overflow:hidden; border:1px solid rgba(139,160,210,.24); border-radius:24px; background:linear-gradient(145deg,rgba(25,39,76,.94),rgba(13,22,50,.97)); box-shadow:inset 0 1px rgba(255,255,255,.025); }
.review-card--done { opacity:.78; }
.review-card__rail { display:flex; align-items:center; flex-direction:column; gap:13px; padding:24px 16px; border-right:1px solid rgba(149,168,213,.16); background:linear-gradient(180deg,rgba(255,205,82,.055),transparent 62%); }
.review-market { display:grid; width:48px; height:48px; place-items:center; border:1px solid rgba(255,208,74,.27); border-radius:15px; background:rgba(255,255,255,.92); }.review-market img { width:34px; }
.review-rating { display:flex; gap:1px; color:#48536f; font-size:15px; }.review-rating .lit { color:#ffd15d; text-shadow:0 0 12px rgba(255,209,93,.22); }
.review-state { margin-top:auto; color:#ffd269; font-size:8px; font-weight:950; letter-spacing:.1em; text-align:center; }.review-state--done { color:#70d8bc; }
.review-card__content { min-width:0; padding:24px 26px; }
.review-card__head { display:flex; justify-content:space-between; gap:24px; padding-bottom:17px; border-bottom:1px dashed rgba(163,181,222,.2); }.review-card__head p { margin:0 0 4px; color:#7796f3; font-size:10px; font-weight:900; letter-spacing:.08em; text-transform:uppercase; }.review-card__head h2 { margin:0; color:#f6f8ff; font-size:19px; letter-spacing:-.025em; }.review-card__byline { display:grid; gap:3px; text-align:right; }.review-card__byline strong { color:#e8edfb; font-size:13px; }.review-card__byline time { color:#8f9bb7; font-size:11px; }
.review-copy { display:grid; gap:11px; padding:18px 0; }.review-copy > div { display:grid; grid-template-columns:110px minmax(0,1fr); gap:14px; }.review-copy span { color:#61d9b9; font-size:9px; font-weight:950; letter-spacing:.12em; }.review-copy__minus span { color:#ff9da4; }.review-copy__main span { color:#8ea9ff; }.review-copy p { margin:0; color:#d5deef; line-height:1.58; white-space:pre-wrap; }.review-copy__silent { color:#8e9ab6 !important; font-style:italic; }
.review-media { display:flex; gap:8px; padding-bottom:16px; overflow:auto; }.review-media img { width:88px; height:88px; flex:0 0 auto; border:1px solid rgba(153,173,220,.22); border-radius:14px; object-fit:cover; }
.review-meta { display:flex; flex-wrap:wrap; gap:7px; }.review-meta span { padding:5px 9px; border:1px solid rgba(139,158,208,.2); border-radius:999px; color:#98a6c2; background:rgba(8,14,34,.34); font-size:10px; }
.review-reply-state { display:grid; gap:7px; margin-top:17px; padding:14px 16px; border:1px solid rgba(83,222,184,.22); border-radius:15px; background:rgba(45,178,145,.06); }.review-reply-state span { color:#68dcbc; font-size:9px; font-weight:950; letter-spacing:.1em; }.review-reply-state p { margin:5px 0 0; color:#d9e4f4; line-height:1.5; white-space:pre-wrap; }.review-reply-state small { color:#ffabb0; }.review-reply-state--failed,.review-reply-state--unknown { border-color:rgba(255,148,155,.3); background:rgba(255,100,112,.06); }.review-reply-state--failed span,.review-reply-state--unknown span { color:#ffa8ae; }
.review-compose { display:grid; gap:9px; margin-top:18px; padding-top:17px; border-top:1px solid rgba(155,175,220,.17); }.review-compose label { color:#c8d2e7; font-size:10px; font-weight:900; letter-spacing:.09em; text-transform:uppercase; }.review-compose textarea { width:100%; box-sizing:border-box; resize:vertical; padding:14px 15px; border:1px solid rgba(136,158,212,.3); border-radius:15px; outline:none; color:#edf3ff; background:rgba(5,11,28,.58); font:inherit; line-height:1.5; }.review-compose textarea:focus { border-color:rgba(79,121,255,.72); box-shadow:0 0 0 3px rgba(71,111,245,.12); }.review-compose footer { display:flex; align-items:center; justify-content:space-between; gap:14px; }.review-compose footer span { color:#7f8da8; font-size:10px; }.review-compose button { min-height:43px; padding:0 17px; border:1px solid rgba(74,116,255,.78); border-radius:12px; color:#fff; background:linear-gradient(135deg,#2253e5,#496fff); font-weight:850; }.review-compose button:disabled { cursor:not-allowed; opacity:.45; }.review-compose-disabled { margin:17px 0 0; padding:12px 14px; border:1px dashed rgba(144,160,204,.22); border-radius:13px; color:#8f9bb5; font-size:12px; }
.reviews-pagination { display:flex; align-items:center; justify-content:center; gap:15px; padding:6px; color:#9eabc4; }.reviews-pagination button { min-height:42px; padding:0 14px; border:1px solid rgba(149,164,203,.25); border-radius:12px; color:#dce5f9; background:rgba(31,40,70,.72); font-weight:750; }.reviews-pagination button:disabled { opacity:.4; }
@media (max-width:800px) { .reviews-toolbar,.reviews-hero { align-items:stretch; flex-direction:column; }.reviews-hero__counter { width:fit-content; padding:12px 0 0; border-top:1px solid rgba(255,210,94,.22); border-left:0; text-align:left; }.reviews-stores { max-width:100%; }.review-card { grid-template-columns:1fr; }.review-card__rail { align-items:center; flex-direction:row; padding:13px 16px; border-right:0; border-bottom:1px solid rgba(149,168,213,.16); }.review-state { margin:0 0 0 auto; }.review-card__head { flex-direction:column; gap:9px; }.review-card__byline { text-align:left; }.review-copy > div { grid-template-columns:1fr; gap:4px; } }
@media (max-width:520px) { .reviews-view { margin-top:28px; }.reviews-view--dialog { margin-top:0; }.reviews-hero { padding:22px 20px; }.reviews-view--dialog .reviews-hero { padding:22px 58px 22px 20px; }.reviews-hero h1 span { display:block; margin-top:8px; font-size:.43em; white-space:normal; }.reviews-toolbar { align-items:stretch; }.reviews-tabs { display:grid; grid-template-columns:1fr 1fr; }.reviews-tabs button { padding:0 8px; }.review-card__content { padding:20px 17px; }.review-compose footer { align-items:stretch; flex-direction:column; }.review-compose button { width:100%; } }
</style>
