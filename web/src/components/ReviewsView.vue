<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { apiRequest } from '../api'
import ozonLogo from '../assets/ozon-logo.png'
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
const page = ref(1)
const pageSize = 20
const selectedConnectionId = ref(props.connectionId)
const loading = ref(false)
const error = ref('')
const sendingReviewId = ref(0)
const selectedPhoto = ref('')
const drafts = reactive({})
let refreshTimer = null
let requestSequence = 0

const marketplaceConnections = computed(() => props.connections.filter((item) => ['yandex_market', 'ozon'].includes(item.provider_code)))
const connectionScoped = computed(() => Number.isInteger(props.connectionId) && props.connectionId > 0)
const selectedStoreName = computed(() => props.connectionName
  || marketplaceConnections.value.find((item) => item.id === selectedConnectionId.value)?.display_name
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
      state: 'pending',
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
  if (reply.state === 'sending') return 'Публикуем ответ'
  if (reply.state === 'submitted' && reply.provider_status === 'UNMODERATED') return 'Ответ на модерации'
  if (reply.state === 'submitted') return 'Ответ опубликован'
  if (reply.state === 'unknown') return 'Нужно проверить в кабинете маркетплейса'
  return 'Ответ не отправлен'
}

function providerLogo(providerCode) {
  return providerCode === 'ozon' ? ozonLogo : yandexMarketLogo
}

function providerName(providerCode) {
  return providerCode === 'ozon' ? 'Ozon' : 'Яндекс Маркет'
}

function openPhoto(photo) {
  selectedPhoto.value = photo
}

function closePhoto() {
  selectedPhoto.value = ''
}

function handleGlobalKeydown(event) {
  if (event.key === 'Escape' && selectedPhoto.value) closePhoto()
}

onMounted(() => {
  window.addEventListener('keydown', handleGlobalKeydown)
  loadReviews()
})
onBeforeUnmount(() => {
  requestSequence += 1
  clearRefreshTimer()
  window.removeEventListener('keydown', handleGlobalKeydown)
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

    <div v-if="!connectionScoped" class="reviews-toolbar">
      <div class="reviews-stores" aria-label="Фильтр по магазину">
        <button type="button" :class="{ active: selectedConnectionId === null }" @click="selectConnection(null)">Все магазины</button>
        <button
          v-for="connection in marketplaceConnections"
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
      <h2>Все отзывы обработаны</h2>
      <p>Новые отзывы появятся здесь после синхронизации магазина.</p>
    </div>

    <div v-else class="reviews-list">
      <article v-for="review in items" :key="review.id" class="review-card" :class="{ 'review-card--done': !review.need_reaction }">
        <div class="review-card__rail">
          <div class="review-market" :class="`review-market--${review.provider_code}`">
            <img :src="providerLogo(review.provider_code)" :alt="providerName(review.provider_code)" />
          </div>
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
              <h2>{{ review.product_title || review.offer_id || `Товар ${providerName(review.provider_code)}` }}</h2>
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
            <button
              v-for="(photo, photoIndex) in review.photos"
              :key="photo"
              type="button"
              :aria-label="`Увеличить фотографию ${photoIndex + 1}`"
              @click="openPhoto(photo)"
            >
              <img :src="photo" :alt="`Фотография покупателя ${photoIndex + 1}`" loading="lazy" />
              <span aria-hidden="true">↗</span>
            </button>
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
              rows="3"
            ></textarea>
            <footer>
              <span>Без контактов магазина и внешних ссылок · {{ String(drafts[review.id] || '').length }}/4096</span>
              <button type="submit" :disabled="!String(drafts[review.id] || '').trim() || sendingReviewId === review.id">
                {{ sendingReviewId === review.id ? 'Ставим в очередь…' : 'Отправить ответ' }}
              </button>
            </footer>
          </form>
          <p v-else-if="review.need_reaction && !review.reply" class="review-compose-disabled">
            {{ review.reply_disabled_reason || 'Ручные ответы подготовлены, но публикация для этого магазина пока не включена.' }}
          </p>
        </div>
      </article>
    </div>

    <nav v-if="pageCount > 1" class="reviews-pagination" aria-label="Страницы отзывов">
      <button type="button" :disabled="page === 1" @click="changePage(page - 1)">← Назад</button>
      <span>{{ page }} / {{ pageCount }}</span>
      <button type="button" :disabled="page === pageCount" @click="changePage(page + 1)">Далее →</button>
    </nav>

    <Teleport to="body">
      <div
        v-if="selectedPhoto"
        class="review-lightbox"
        role="dialog"
        aria-modal="true"
        aria-label="Увеличенная фотография из отзыва"
        @click.self="closePhoto"
      >
        <button type="button" aria-label="Закрыть фотографию" @click="closePhoto">×</button>
        <img :src="selectedPhoto" alt="Увеличенная фотография покупателя" />
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.reviews-view { display:grid; gap:14px; margin-top:42px; color:#eef3ff; }
.reviews-view--dialog { margin-top:0; }
.reviews-hero { position:relative; display:flex; align-items:end; justify-content:space-between; gap:28px; padding:28px 30px; overflow:hidden; border:1px solid rgba(135,158,214,.22); border-radius:26px; background:linear-gradient(125deg,rgba(16,27,60,.92),rgba(29,37,67,.82)); box-shadow:inset 0 1px rgba(255,255,255,.035); }
.reviews-hero::after { position:absolute; right:-80px; bottom:-130px; width:320px; height:320px; border:1px solid rgba(255,205,82,.12); border-radius:50%; content:''; box-shadow:0 0 90px rgba(255,205,82,.04); }
.reviews-hero h1 { margin:3px 0 7px; font-size:clamp(42px,5vw,72px); line-height:.96; letter-spacing:-.065em; }
.reviews-hero h1 span { color:#ffd269; font-size:.38em; letter-spacing:-.025em; white-space:nowrap; }
.reviews-view--dialog .reviews-hero { padding:18px 64px 18px 22px; border-radius:20px; }
.reviews-view--dialog .reviews-hero h1 { font-size:clamp(29px,3vw,43px); }
.reviews-hero p { margin:0; color:#aebbd5; font-size:13px; line-height:1.45; }
.reviews-kicker { color:#f5c85d !important; font-size:9px; font-weight:900; letter-spacing:.17em; }
.reviews-hero__counter { position:relative; z-index:1; display:grid; min-width:138px; gap:2px; padding-left:18px; border-left:1px solid rgba(255,210,94,.28); text-align:right; }
.reviews-hero__counter span { color:#9daac4; font-size:9px; font-weight:900; letter-spacing:.14em; }
.reviews-hero__counter strong { color:#ffd260; font-size:40px; line-height:1; letter-spacing:-.06em; }
.reviews-toolbar { display:flex; align-items:center; justify-content:flex-end; gap:14px; }
.reviews-stores { display:flex; gap:4px; max-width:100%; overflow:auto; padding:4px; border:1px solid rgba(139,158,208,.2); border-radius:13px; background:rgba(10,18,41,.54); }
.reviews-stores button { min-height:36px; flex:0 0 auto; padding:0 13px; border:1px solid transparent; border-radius:9px; color:#aeb9d1; background:transparent; font:inherit; font-size:11px; font-weight:800; }
.reviews-stores button.active { color:#fff; border-color:rgba(88,126,255,.58); background:linear-gradient(135deg,#234fda,#4169f0); }
.reviews-error { margin:0; padding:13px 15px; border:1px solid rgba(255,139,147,.35); border-radius:14px; color:#ffadb2; background:rgba(255,91,105,.08); }
.reviews-empty { display:grid; min-height:220px; place-items:center; padding:30px; border:1px dashed rgba(139,158,208,.24); border-radius:24px; color:#aeb9d2; background:rgba(12,20,44,.42); }
.reviews-empty--clear { align-content:center; gap:6px; text-align:center; }
.reviews-empty--clear > span { display:grid; width:54px; height:54px; place-items:center; margin-bottom:7px; border:1px solid rgba(78,230,189,.35); border-radius:17px; color:#58e5bd; background:rgba(78,230,189,.08); font-size:25px; }
.reviews-empty h2,.reviews-empty p { margin:0; }.reviews-empty h2 { color:#edf2ff; }.reviews-empty p { max-width:520px; line-height:1.55; }
.reviews-list { display:grid; gap:10px; }
.review-card { display:grid; grid-template-columns:118px minmax(0,1fr); overflow:hidden; border:1px solid rgba(139,160,210,.24); border-radius:19px; background:linear-gradient(145deg,rgba(25,39,76,.94),rgba(13,22,50,.97)); box-shadow:inset 0 1px rgba(255,255,255,.025); }
.review-card--done { opacity:.78; }
.review-card__rail { display:flex; align-items:center; flex-direction:column; gap:10px; padding:18px 12px; border-right:1px solid rgba(149,168,213,.16); background:linear-gradient(180deg,rgba(255,205,82,.055),transparent 62%); }
.review-market { display:grid; width:40px; height:40px; place-items:center; overflow:hidden; border:1px solid rgba(255,208,74,.27); border-radius:12px; background:rgba(255,255,255,.92); }.review-market img { width:29px; height:29px; object-fit:contain; }.review-market--ozon img { width:40px; height:40px; object-fit:cover; transform:scale(1.25); }
.review-rating { display:flex; gap:1px; color:#48536f; font-size:13px; }.review-rating .lit { color:#ffd15d; text-shadow:0 0 12px rgba(255,209,93,.22); }
.review-state { margin-top:auto; color:#ffd269; font-size:8px; font-weight:950; letter-spacing:.1em; text-align:center; }.review-state--done { color:#70d8bc; }
.review-card__content { min-width:0; padding:18px 20px; overflow:hidden; }
.review-card__head { display:grid; grid-template-columns:minmax(0,1fr) max-content; align-items:start; gap:16px; padding-bottom:13px; border-bottom:1px dashed rgba(163,181,222,.2); }.review-card__head > div:first-child { min-width:0; }.review-card__head p { margin:0 0 4px; color:#7796f3; font-size:9px; font-weight:900; letter-spacing:.08em; text-transform:uppercase; }.review-card__head h2 { max-width:100%; margin:0; color:#f6f8ff; font-size:clamp(15px,1.7vw,18px); line-height:1.25; letter-spacing:-.025em; overflow-wrap:anywhere; text-wrap:pretty; }.review-card__byline { display:grid; max-width:190px; gap:3px; text-align:right; }.review-card__byline strong { color:#e8edfb; font-size:12px; line-height:1.25; overflow-wrap:anywhere; }.review-card__byline time { color:#8f9bb7; font-size:10px; white-space:nowrap; }
.review-copy { display:grid; gap:9px; padding:13px 0; }.review-copy > div { display:grid; grid-template-columns:104px minmax(0,1fr); align-items:start; gap:12px; }.review-copy span { padding-top:2px; color:#61d9b9; font-size:8px; font-weight:950; line-height:1.3; letter-spacing:.12em; }.review-copy__minus span { color:#ff9da4; }.review-copy__main span { color:#8ea9ff; }.review-copy p { min-width:0; margin:0; color:#d5deef; font-size:13px; line-height:1.5; overflow-wrap:anywhere; white-space:pre-wrap; }.review-copy__silent { color:#8e9ab6 !important; font-style:italic; }
.review-media { display:flex; gap:9px; padding:1px 0 13px; overflow:auto; }.review-media button { position:relative; width:96px; height:96px; flex:0 0 auto; overflow:hidden; padding:0; border:1px solid rgba(153,173,220,.28); border-radius:14px; background:#091127; cursor:zoom-in; box-shadow:0 8px 22px rgba(0,0,0,.18); transition:transform .18s,border-color .18s,box-shadow .18s; }.review-media button:hover { border-color:rgba(255,211,105,.58); transform:translateY(-2px); box-shadow:0 12px 28px rgba(0,0,0,.28); }.review-media img { width:100%; height:100%; object-fit:cover; }.review-media button span { position:absolute; right:6px; bottom:6px; display:grid; width:22px; height:22px; place-items:center; border:1px solid rgba(255,255,255,.25); border-radius:7px; color:#fff; background:rgba(4,9,24,.75); font-size:11px; backdrop-filter:blur(6px); }
.review-meta { display:flex; min-width:0; flex-wrap:wrap; gap:7px; }.review-meta span { max-width:100%; padding:5px 9px; border:1px solid rgba(139,158,208,.2); border-radius:999px; color:#98a6c2; background:rgba(8,14,34,.34); font-size:10px; overflow-wrap:anywhere; }
.review-reply-state { display:grid; gap:7px; margin-top:17px; padding:14px 16px; border:1px solid rgba(83,222,184,.22); border-radius:15px; background:rgba(45,178,145,.06); }.review-reply-state span { color:#68dcbc; font-size:9px; font-weight:950; letter-spacing:.1em; }.review-reply-state p { margin:5px 0 0; color:#d9e4f4; line-height:1.5; white-space:pre-wrap; }.review-reply-state small { color:#ffabb0; }.review-reply-state--failed,.review-reply-state--unknown { border-color:rgba(255,148,155,.3); background:rgba(255,100,112,.06); }.review-reply-state--failed span,.review-reply-state--unknown span { color:#ffa8ae; }
.review-compose { display:grid; min-width:0; gap:7px; margin-top:13px; padding-top:12px; border-top:1px solid rgba(155,175,220,.17); }.review-compose label { color:#c8d2e7; font-size:9px; font-weight:900; letter-spacing:.09em; text-transform:uppercase; }.review-compose textarea { display:block; width:100%; box-sizing:border-box; min-width:0; min-height:76px; resize:vertical; padding:11px 13px; border:1px solid rgba(136,158,212,.3); border-radius:12px; outline:none; color:#edf3ff; background:rgba(5,11,28,.58); font:inherit; font-size:13px; line-height:1.45; }.review-compose textarea:focus { border-color:rgba(79,121,255,.72); box-shadow:0 0 0 3px rgba(71,111,245,.12); }.review-compose footer { display:flex; min-width:0; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:9px 12px; }.review-compose footer span { min-width:0; color:#7f8da8; font-size:9px; overflow-wrap:anywhere; }.review-compose button { min-height:37px; margin-left:auto; padding:0 14px; border:1px solid rgba(74,116,255,.78); border-radius:10px; color:#fff; background:linear-gradient(135deg,#2253e5,#496fff); font-size:12px; font-weight:850; }.review-compose button:disabled { cursor:not-allowed; opacity:.45; }.review-compose-disabled { margin:13px 0 0; padding:10px 12px; border:1px dashed rgba(144,160,204,.22); border-radius:11px; color:#8f9bb5; font-size:11px; }
.reviews-pagination { display:flex; align-items:center; justify-content:center; gap:15px; padding:6px; color:#9eabc4; }.reviews-pagination button { min-height:42px; padding:0 14px; border:1px solid rgba(149,164,203,.25); border-radius:12px; color:#dce5f9; background:rgba(31,40,70,.72); font-weight:750; }.reviews-pagination button:disabled { opacity:.4; }
.review-lightbox { position:fixed; z-index:50; inset:0; display:grid; place-items:center; padding:clamp(18px,4vw,48px); background:rgba(2,5,16,.9); backdrop-filter:blur(14px); animation:review-lightbox-in .16s ease-out; }.review-lightbox > img { display:block; max-width:min(1180px,92vw); max-height:88vh; border:1px solid rgba(184,200,236,.28); border-radius:18px; object-fit:contain; background:#080e21; box-shadow:0 35px 100px rgba(0,0,0,.7); }.review-lightbox > button { position:absolute; top:clamp(18px,3vw,34px); right:clamp(18px,3vw,34px); display:grid; width:42px; height:42px; place-items:center; padding:0; border:1px solid rgba(190,204,237,.32); border-radius:12px; color:#eef3ff; background:rgba(12,20,43,.82); font-size:28px; line-height:1; backdrop-filter:blur(8px); }.review-lightbox > button:hover { border-color:rgba(255,211,105,.62); color:#ffd369; }@keyframes review-lightbox-in { from { opacity:0; } to { opacity:1; } }
@media (max-width:800px) { .reviews-toolbar,.reviews-hero { align-items:stretch; flex-direction:column; }.reviews-hero__counter { width:fit-content; padding:12px 0 0; border-top:1px solid rgba(255,210,94,.22); border-left:0; text-align:left; }.reviews-stores { max-width:100%; }.review-card { grid-template-columns:1fr; }.review-card__rail { align-items:center; flex-direction:row; padding:13px 16px; border-right:0; border-bottom:1px solid rgba(149,168,213,.16); }.review-state { margin:0 0 0 auto; }.review-card__head { grid-template-columns:1fr; gap:9px; }.review-card__byline { max-width:none; text-align:left; }.review-copy > div { grid-template-columns:92px minmax(0,1fr); gap:8px; } }
@media (max-width:520px) { .reviews-view { margin-top:28px; }.reviews-view--dialog { margin-top:0; }.reviews-hero { padding:20px 18px; }.reviews-view--dialog .reviews-hero { gap:14px; padding:16px 52px 16px 18px; }.reviews-hero h1 span { display:block; margin-top:6px; font-size:.43em; white-space:normal; }.reviews-view--dialog .reviews-hero p { font-size:12px; }.reviews-toolbar { align-items:stretch; }.review-card__content { padding:17px 15px; }.review-copy > div { grid-template-columns:1fr; gap:4px; }.review-media button { width:82px; height:82px; }.review-compose footer { align-items:stretch; flex-direction:column; }.review-compose button { width:100%; margin-left:0; } }
</style>
