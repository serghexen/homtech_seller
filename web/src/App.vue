<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { apiRequest } from './api'
import homtechLogo from './assets/homtech-wordmark.png'

const mode = ref('login')
const loading = ref(false)
const error = ref('')
const user = ref(null)
const connections = ref([])
const connectionsLoading = ref(false)
const activeSection = ref('stores')
const catalogItems = ref([])
const catalogTotal = ref(0)
const catalogPage = ref(1)
const catalogLoading = ref(false)
const orders = ref([])
const ordersTotal = ref(0)
const ordersPage = ref(1)
const ordersLoading = ref(false)
const selectedConnectionId = ref(null)
const catalogSearch = ref('')
const orderFilters = reactive({ query: '', status: '', date_from: '', date_to: '' })
const appliedOrderFilters = reactive({ query: '', status: '', date_from: '', date_to: '' })
const isSyncing = ref(false)
const isConnectionModalOpen = ref(false)
const isSavingConnection = ref(false)
const isDiscoveringStores = ref(false)
const connectionError = ref('')
const yandexStores = ref([])
const selectedYandexStore = ref(null)
const form = reactive({ email: '', password: '', display_name: '', workspace_name: '' })
const connectionForm = reactive({ provider_code: 'ozon', display_name: '', client_id: '', token: '' })

const isYandex = computed(() => connectionForm.provider_code === 'yandex_market')
const pageSize = 24
const catalogPageCount = computed(() => Math.max(1, Math.ceil(catalogTotal.value / pageSize)))
const ordersPageCount = computed(() => Math.max(1, Math.ceil(ordersTotal.value / pageSize)))

function switchMode(nextMode) {
  // Переключает сценарий входа без потери введённого email и показывает только нужные поля.
  mode.value = nextMode
  error.value = ''
}

function providerName(providerCode) {
  // Превращает технический код адаптера в единое название, понятное оператору в карточках и форме.
  return providerCode === 'yandex_market' ? 'Яндекс Маркет' : 'Ozon'
}

function providerMark(providerCode) {
  // Показывает компактный знак маркетплейса без зависимости от внешней сети и сторонних изображений.
  return providerCode === 'yandex_market' ? 'Я' : 'OZ'
}

function connectionStatus(status) {
  // Даёт человеку понятное состояние подключения вместо технических кодов базы данных.
  return status === 'active' ? 'Активен' : status === 'disabled' ? 'Отключён' : 'Требует проверки'
}

function orderStatus(status) {
  // Отображает короткий единый справочник статусов, чтобы интерфейс не зависел от кодов разных маркетплейсов.
  return {
    processing: 'В процессе',
    in_delivery: 'Доставляется',
    delivered: 'Доставлен',
    cancelled: 'Отменён',
    problem: 'Проблема',
  }[status] || 'Проблема'
}

function formatDate(value) {
  // Показывает дату снимка в локальном времени оператора и не выводит технический ISO-формат API.
  if (!value) return '—'
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function queryString(params) {
  // Собирает только заполненные фильтры, чтобы API получал простой и воспроизводимый запрос списка.
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) search.set(key, String(value))
  })
  return search.toString()
}

async function loadConnections() {
  // Загружает только магазины рабочей области текущей сессии, а не общий список системы.
  if (!user.value) return
  connectionsLoading.value = true
  try {
    const result = await apiRequest('/marketplaces/connections')
    connections.value = result.items
    if (result.workspace_name) user.value.workspace_name = result.workspace_name
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось загрузить магазины'
  } finally {
    connectionsLoading.value = false
  }
}

async function loadCatalog() {
  // Загружает страницу сохранённого каталога без нового обращения к маркетплейсу на каждый поиск.
  if (!user.value) return
  catalogLoading.value = true
  try {
    const query = queryString({ connection_id: selectedConnectionId.value, query: catalogSearch.value, page: catalogPage.value, page_size: pageSize })
    const result = await apiRequest(`/marketplaces/catalog?${query}`)
    catalogItems.value = result.items
    catalogTotal.value = result.total
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось загрузить каталог'
  } finally {
    catalogLoading.value = false
  }
}

async function loadOrders() {
  // Загружает постраничный локальный снимок заказов с уже применёнными фильтрами.
  if (!user.value) return
  ordersLoading.value = true
  try {
    const query = queryString({ connection_id: selectedConnectionId.value, ...appliedOrderFilters, page: ordersPage.value, page_size: pageSize })
    const result = await apiRequest(`/marketplaces/orders?${query}`)
    orders.value = result.items
    ordersTotal.value = result.total
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось загрузить заказы'
  } finally {
    ordersLoading.value = false
  }
}

async function changeSection(section) {
  // Переключает рабочий раздел и загружает его локальный снимок только при первом открытии или возврате.
  activeSection.value = section
  error.value = ''
  if (section === 'catalog') await loadCatalog()
  if (section === 'orders') await loadOrders()
}

async function selectConnection(connectionId) {
  // Меняет фильтр магазина для текущего раздела и возвращает пользователя на первую страницу списка.
  selectedConnectionId.value = connectionId
  if (activeSection.value === 'catalog') {
    catalogPage.value = 1
    await loadCatalog()
  }
  if (activeSection.value === 'orders') {
    ordersPage.value = 1
    await loadOrders()
  }
}

async function syncCurrentSnapshot() {
  // Обновляет только выбранный магазин или все активные и затем перечитывает локальный снимок из БД.
  const kind = activeSection.value === 'catalog' ? 'catalog' : 'orders'
  isSyncing.value = true
  error.value = ''
  try {
    const result = await apiRequest(`/marketplaces/${kind}/sync`, {
      method: 'POST',
      body: JSON.stringify({ connection_id: selectedConnectionId.value }),
    })
    const errors = result.items.filter((item) => item.error)
    if (errors.length) error.value = errors.map((item) => `${item.store_name}: ${item.error}`).join(' ')
    if (kind === 'catalog') await loadCatalog()
    else await loadOrders()
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось обновить снимок'
  } finally {
    isSyncing.value = false
  }
}

async function applyCatalogSearch() {
  // Применяет поиск каталога отдельным действием, чтобы не отправлять запрос при каждом символе.
  catalogPage.value = 1
  await loadCatalog()
}

async function applyOrderFilters() {
  // Копирует черновик фильтров после нажатия «Применить» и только затем обновляет список заказов.
  Object.assign(appliedOrderFilters, orderFilters)
  ordersPage.value = 1
  await loadOrders()
}

async function resetOrderFilters() {
  // Сбрасывает и черновик, и применённые условия, чтобы вернуть полный снимок заказов одной кнопкой.
  Object.assign(orderFilters, { query: '', status: '', date_from: '', date_to: '' })
  Object.assign(appliedOrderFilters, orderFilters)
  ordersPage.value = 1
  await loadOrders()
}

async function changePage(nextPage) {
  // Переключает страницу активного раздела в допустимых границах без изменения выбранных фильтров.
  if (activeSection.value === 'catalog') {
    catalogPage.value = Math.min(Math.max(1, nextPage), catalogPageCount.value)
    await loadCatalog()
  }
  if (activeSection.value === 'orders') {
    ordersPage.value = Math.min(Math.max(1, nextPage), ordersPageCount.value)
    await loadOrders()
  }
}

async function submit() {
  // Отправляет регистрацию или вход и после успеха сразу открывает данные созданной организации.
  loading.value = true
  error.value = ''
  try {
    const path = mode.value === 'register' ? '/auth/register' : '/auth/login'
    const body = mode.value === 'register' ? { ...form } : { email: form.email, password: form.password }
    const result = await apiRequest(path, { method: 'POST', body: JSON.stringify(body) })
    user.value = result.user
    form.password = ''
    await loadConnections()
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось войти'
  } finally {
    loading.value = false
  }
}

async function logout() {
  // Сначала подтверждает удаление HttpOnly cookie на сервере, чтобы обновление страницы не восстановило сессию.
  error.value = ''
  try {
    await apiRequest('/auth/logout', { method: 'POST' })
    user.value = null
    connections.value = []
    catalogItems.value = []
    orders.value = []
    selectedConnectionId.value = null
    activeSection.value = 'stores'
    form.password = ''
    mode.value = 'login'
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось выйти из аккаунта'
  }
}

function resetConnectionForm(providerCode = 'ozon') {
  // Сбрасывает введённый ключ при закрытии формы, чтобы он не оставался в памяти интерфейса дольше нужного.
  connectionForm.provider_code = providerCode
  connectionForm.display_name = ''
  connectionForm.client_id = ''
  connectionForm.token = ''
  yandexStores.value = []
  selectedYandexStore.value = null
  connectionError.value = ''
}

function openConnectionModal() {
  // Открывает чистую форму нового кабинета и не редактирует сохранённые реквизиты без отдельного сценария.
  resetConnectionForm()
  isConnectionModalOpen.value = true
}

function closeConnectionModal() {
  // Закрывает форму и очищает секретный API-ключ вне зависимости от результата предыдущего запроса.
  isConnectionModalOpen.value = false
  resetConnectionForm(connectionForm.provider_code)
}

function chooseProvider(providerCode) {
  // Меняет состав обязательных полей для Ozon и Маркета, не перенося ключ между разными сервисами.
  resetConnectionForm(providerCode)
}

async function discoverYandexStores() {
  // Ищет доступные кабинеты по API-Key, чтобы пользователь выбирал магазин, а не вводил его ID вручную.
  if (!connectionForm.token.trim()) {
    connectionError.value = 'Вставьте API-Key Яндекс Маркета'
    return
  }
  isDiscoveringStores.value = true
  connectionError.value = ''
  try {
    const result = await apiRequest('/marketplaces/connections/discover', {
      method: 'POST',
      body: JSON.stringify({ provider_code: 'yandex_market', token: connectionForm.token }),
    })
    yandexStores.value = result.items
    if (result.items.length === 1) selectYandexStore(result.items[0])
  } catch (requestError) {
    connectionError.value = requestError.message || 'Не удалось найти магазины'
  } finally {
    isDiscoveringStores.value = false
  }
}

function selectYandexStore(store) {
  // Запоминает выбранный кабинет из проверенного списка и подставляет его имя в будущую карточку.
  selectedYandexStore.value = store
  connectionForm.display_name = store.display_name
}

async function saveConnection() {
  // Проверяет ключ у маркетплейса и только затем создаёт или обновляет подключение текущей организации.
  const token = connectionForm.token.trim()
  if (!connectionForm.display_name.trim() && !isYandex.value) {
    connectionError.value = 'Укажите название магазина'
    return
  }
  if (!token) {
    connectionError.value = 'Вставьте API-ключ из кабинета маркетплейса'
    return
  }
  if (isYandex.value && !selectedYandexStore.value) {
    await discoverYandexStores()
    return
  }
  isSavingConnection.value = true
  connectionError.value = ''
  try {
    await apiRequest('/marketplaces/connections', {
      method: 'POST',
      body: JSON.stringify({
        provider_code: connectionForm.provider_code,
        display_name: connectionForm.display_name.trim(),
        token,
        client_id: connectionForm.client_id.trim(),
        business_id: selectedYandexStore.value?.business_id,
        campaign_id: selectedYandexStore.value?.campaign_id,
      }),
    })
    closeConnectionModal()
    await loadConnections()
  } catch (requestError) {
    connectionError.value = requestError.message || 'Не удалось подключить магазин'
  } finally {
    isSavingConnection.value = false
  }
}

async function disableConnection(connectionId) {
  // Отключает только выбранный кабинет, сохраняя его историю для будущих отчётов и повторной проверки.
  try {
    await apiRequest(`/marketplaces/connections/${connectionId}/disable`, { method: 'POST' })
    await loadConnections()
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось отключить магазин'
  }
}

onMounted(async () => {
  // Восстанавливает сессию после обновления страницы, не читая HttpOnly cookie из JavaScript.
  try {
    const result = await apiRequest('/auth/me')
    user.value = result.user
    await loadConnections()
  } catch {
    user.value = null
  }
})
</script>

<template>
  <main class="app-shell">
    <div class="app-shell__glow app-shell__glow--left"></div>
    <div class="app-shell__glow app-shell__glow--right"></div>

    <header class="app-header">
      <div class="app-brand"><img :src="homtechLogo" alt="HomTech" /><span>Seller</span></div>
      <button v-if="user" class="profile-button" type="button" @click="logout">{{ user.display_name || user.email }} · Выйти</button>
    </header>

    <section v-if="user" class="seller-dashboard" aria-live="polite">
      <nav class="seller-nav" aria-label="Разделы Seller">
        <button class="seller-nav__item" :class="{ 'seller-nav__item--active': activeSection === 'stores' }" type="button" @click="changeSection('stores')">Магазины</button>
        <button class="seller-nav__item" :class="{ 'seller-nav__item--active': activeSection === 'catalog' }" type="button" @click="changeSection('catalog')">Каталог</button>
        <button class="seller-nav__item" :class="{ 'seller-nav__item--active': activeSection === 'orders' }" type="button" @click="changeSection('orders')">Заказы</button>
      </nav>

      <div v-if="activeSection === 'stores'" class="dashboard-heading">
        <div>
          <p class="kicker">{{ user.workspace_name }}</p>
          <h1>Магазины</h1>
          <p>Подключите кабинеты, чтобы получать единый каталог и заказы. Seller пока работает с маркетплейсами только в режиме чтения.</p>
        </div>
      </div>
      <p v-if="error" class="form-error">{{ error }}</p>
      <div v-if="activeSection === 'stores' && connectionsLoading" class="empty-state">Загружаем подключённые магазины…</div>
      <div v-else-if="activeSection === 'stores'" class="connection-grid">
        <article v-for="connection in connections" :key="connection.id" class="connection-card" :class="{ 'connection-card--disabled': connection.status === 'disabled' }">
          <div class="connection-card__head">
            <div class="market-mark" :class="`market-mark--${connection.provider_code}`">{{ providerMark(connection.provider_code) }}</div>
            <span class="connection-status" :class="`connection-status--${connection.status}`">{{ connectionStatus(connection.status) }}</span>
          </div>
          <div><h2>{{ connection.display_name }}</h2><p>{{ providerName(connection.provider_code) }}</p></div>
          <dl>
            <div><dt>API-ключ</dt><dd>{{ connection.token_masked }}</dd></div>
            <div v-if="connection.provider_code === 'ozon'"><dt>Client ID</dt><dd>{{ connection.client_id }}</dd></div>
            <div v-else><dt>Кабинет / магазин</dt><dd>{{ connection.business_id }} / {{ connection.campaign_id }}</dd></div>
          </dl>
          <footer><time :datetime="connection.created_at">Подключён</time><button v-if="connection.status !== 'disabled'" type="button" @click="disableConnection(connection.id)">Отключить</button></footer>
        </article>
        <button class="connection-add-card" type="button" @click="openConnectionModal"><strong>+</strong><span>Подключить магазин</span></button>
      </div>

      <section v-if="activeSection === 'catalog' || activeSection === 'orders'" class="snapshot-view">
        <div class="snapshot-toolbar">
          <div class="snapshot-search">
            <label>{{ activeSection === 'catalog' ? 'Поиск' : 'Поиск по заказам' }}</label>
            <input
              v-if="activeSection === 'catalog'"
              v-model.trim="catalogSearch"
              type="search"
              placeholder="Название, артикул или SKU"
              @keyup.enter="applyCatalogSearch"
            />
            <input
              v-else
              v-model.trim="orderFilters.query"
              type="search"
              placeholder="Номер заказа, товар или SKU"
              @keyup.enter="applyOrderFilters"
            />
          </div>
          <div class="store-filters" aria-label="Фильтр по магазину">
            <button :class="{ active: selectedConnectionId === null }" type="button" @click="selectConnection(null)">Все магазины</button>
            <button v-for="connection in connections" :key="connection.id" :class="{ active: selectedConnectionId === connection.id }" type="button" @click="selectConnection(connection.id)">
              <span class="market-mark" :class="`market-mark--${connection.provider_code}`">{{ providerMark(connection.provider_code) }}</span>{{ connection.display_name }}
            </button>
          </div>
          <button class="sync-button" type="button" :disabled="isSyncing" :title="selectedConnectionId ? 'Обновить выбранный магазин' : 'Обновить все активные магазины'" @click="syncCurrentSnapshot">
            <span :class="{ spinning: isSyncing }">↻</span><span class="sync-button__text">{{ isSyncing ? 'Обновляем…' : 'Обновить' }}</span>
          </button>
        </div>

        <div v-if="activeSection === 'orders'" class="orders-filter-row">
          <label><span>Период</span><input v-model="orderFilters.date_from" type="date" /></label>
          <span class="date-divider">—</span>
          <label><span class="sr-only">Дата окончания</span><input v-model="orderFilters.date_to" type="date" /></label>
          <label class="status-select"><span>Статус</span><select v-model="orderFilters.status"><option value="">Все статусы</option><option value="processing">В процессе</option><option value="in_delivery">Доставляется</option><option value="delivered">Доставлен</option><option value="cancelled">Отменён</option><option value="problem">Проблема</option></select></label>
          <button class="primary-button filter-apply" type="button" @click="applyOrderFilters">Применить</button>
          <button class="filter-reset" type="button" @click="resetOrderFilters">Сбросить</button>
        </div>
        <div v-else class="catalog-search-actions"><button class="primary-button filter-apply" type="button" @click="applyCatalogSearch">Найти</button></div>

        <p class="snapshot-count">Найдено: {{ activeSection === 'catalog' ? catalogTotal : ordersTotal }}</p>
        <div v-if="(activeSection === 'catalog' && catalogLoading) || (activeSection === 'orders' && ordersLoading)" class="empty-state">Загружаем локальный снимок…</div>
        <div v-else-if="activeSection === 'catalog' && !catalogItems.length" class="empty-state">Каталог пока пуст. Нажмите «Обновить», чтобы прочитать товары из выбранного магазина.</div>
        <div v-else-if="activeSection === 'orders' && !orders.length" class="empty-state">Заказов пока нет в снимке. Нажмите «Обновить», чтобы прочитать свежие заказы.</div>

        <div v-else-if="activeSection === 'catalog'" class="snapshot-grid">
          <article v-for="item in catalogItems" :key="`${item.connection_id}-${item.external_product_id}`" class="snapshot-card catalog-card">
            <div class="snapshot-card__head"><span class="market-mark" :class="`market-mark--${item.provider_code}`">{{ providerMark(item.provider_code) }}</span><div><h2>{{ item.title || 'Без названия' }}</h2><p>{{ item.store_name }} · {{ providerName(item.provider_code) }}</p></div></div>
            <div class="snapshot-card__footer"><span>SKU: <strong>{{ item.sku || item.offer_id || '—' }}</strong></span><time :datetime="item.synced_at">{{ formatDate(item.synced_at) }}</time></div>
          </article>
        </div>
        <div v-else class="snapshot-grid">
          <article v-for="item in orders" :key="`${item.connection_id}-${item.external_order_id}-${item.external_item_id}`" class="snapshot-card order-card">
            <div class="snapshot-card__head"><span class="market-mark" :class="`market-mark--${item.provider_code}`">{{ providerMark(item.provider_code) }}</span><div><h2>Заказ №{{ item.external_order_id }}</h2><p>{{ item.store_name }} · {{ providerName(item.provider_code) }}</p></div></div>
            <div class="order-card__body"><strong>{{ item.title || 'Товар без названия' }}</strong><span class="order-status" :class="`order-status--${item.status}`">{{ orderStatus(item.status) }}</span></div>
            <div class="snapshot-card__footer"><span>SKU: <strong>{{ item.sku || item.offer_id || '—' }}</strong></span><time :datetime="item.updated_at || item.created_at || item.synced_at">{{ formatDate(item.updated_at || item.created_at || item.synced_at) }}</time></div>
          </article>
        </div>
        <div v-if="(activeSection === 'catalog' ? catalogPageCount : ordersPageCount) > 1" class="pagination">
          <button type="button" :disabled="(activeSection === 'catalog' ? catalogPage : ordersPage) === 1" @click="changePage((activeSection === 'catalog' ? catalogPage : ordersPage) - 1)">← Назад</button>
          <span>Страница {{ activeSection === 'catalog' ? catalogPage : ordersPage }} из {{ activeSection === 'catalog' ? catalogPageCount : ordersPageCount }}</span>
          <button type="button" :disabled="(activeSection === 'catalog' ? catalogPage : ordersPage) === (activeSection === 'catalog' ? catalogPageCount : ordersPageCount)" @click="changePage((activeSection === 'catalog' ? catalogPage : ordersPage) + 1)">Далее →</button>
        </div>
      </section>
    </section>

    <section v-else class="auth-card">
      <div class="auth-card__intro">
        <p class="kicker">{{ mode === 'register' ? 'НОВАЯ ОРГАНИЗАЦИЯ' : 'HOMTECH SELLER' }}</p>
        <h1>{{ mode === 'register' ? 'Объедините\nмагазины.' : 'Управляйте\nмагазинами.' }}</h1>
        <p>{{ mode === 'register' ? 'Создайте единое пространство для магазинов, каталога и заказов.' : 'Подключайте маркетплейсы и следите за каталогом и заказами в одном кабинете.' }}</p>
      </div>
      <form class="auth-form" @submit.prevent="submit">
        <label v-if="mode === 'register'"><span>Ваше имя</span><input v-model.trim="form.display_name" autocomplete="name" maxlength="120" placeholder="Например, Сергей" /></label>
        <label v-if="mode === 'register'"><span>Название организации</span><input v-model.trim="form.workspace_name" required autocomplete="organization" maxlength="160" placeholder="Например, ASAT Games" /></label>
        <label><span>Email</span><input v-model.trim="form.email" required type="email" autocomplete="email" placeholder="you@company.ru" /></label>
        <label><span>Пароль</span><input v-model="form.password" required type="password" :minlength="mode === 'register' ? 10 : 1" autocomplete="current-password" placeholder="Не менее 10 символов" /></label>
        <p v-if="error" class="form-error">{{ error }}</p>
        <button class="primary-button" type="submit" :disabled="loading">{{ loading ? 'Проверяем…' : mode === 'register' ? 'Создать организацию' : 'Войти' }}</button>
      </form>
      <footer class="auth-card__footer"><span>{{ mode === 'register' ? 'Уже есть аккаунт?' : 'Нет аккаунта?' }}</span><button type="button" @click="switchMode(mode === 'register' ? 'login' : 'register')">{{ mode === 'register' ? 'Войти' : 'Зарегистрироваться' }}</button></footer>
    </section>

    <div v-if="isConnectionModalOpen" class="modal-backdrop" @click.self="closeConnectionModal">
      <section class="connection-modal" role="dialog" aria-modal="true" aria-labelledby="connection-title">
        <button class="modal-close" type="button" aria-label="Закрыть" @click="closeConnectionModal">×</button>
        <p class="kicker">НОВОЕ ПОДКЛЮЧЕНИЕ</p><h1 id="connection-title">Подключить магазин</h1>
        <div class="provider-picker">
          <button type="button" :class="{ active: !isYandex }" @click="chooseProvider('ozon')"><span class="market-mark market-mark--ozon">OZ</span>Ozon</button>
          <button type="button" :class="{ active: isYandex }" @click="chooseProvider('yandex_market')"><span class="market-mark market-mark--yandex_market">Я</span>Яндекс Маркет</button>
        </div>
        <form class="connection-form" @submit.prevent="saveConnection">
          <label><span>Название магазина</span><input v-model.trim="connectionForm.display_name" :readonly="isYandex && !!selectedYandexStore" required placeholder="Например, ASAT Games" /></label>
          <label v-if="!isYandex"><span>Client ID кабинета</span><input v-model.trim="connectionForm.client_id" required inputmode="numeric" placeholder="Например, 3313715" /></label>
          <label><span>{{ isYandex ? 'API-Key' : 'API Key' }}</span><textarea v-model="connectionForm.token" required :placeholder="isYandex ? 'Вставьте API-Key Яндекс Маркета' : 'Вставьте API Key из кабинета Ozon'" /></label>
          <div v-if="isYandex" class="yandex-discovery">
            <button class="secondary-button" type="button" :disabled="isDiscoveringStores" @click="discoverYandexStores">{{ isDiscoveringStores ? 'Ищем магазины…' : 'Найти магазины' }}</button>
            <div v-if="yandexStores.length" class="store-choice">
              <span>Выберите кабинет</span>
              <button v-for="store in yandexStores" :key="store.campaign_id" type="button" :class="{ active: selectedYandexStore?.campaign_id === store.campaign_id }" @click="selectYandexStore(store)">{{ store.display_name }} <small>№{{ store.campaign_id }}</small></button>
            </div>
          </div>
          <p v-if="connectionError" class="form-error">{{ connectionError }}</p>
          <div class="connection-form__actions"><button class="secondary-button" type="button" @click="closeConnectionModal">Отмена</button><button class="primary-button" type="submit" :disabled="isSavingConnection">{{ isSavingConnection ? 'Проверяем…' : isYandex && !selectedYandexStore ? 'Найти магазины' : 'Подключить магазин' }}</button></div>
        </form>
      </section>
    </div>
  </main>
</template>

<style>
:root { --brand-blue: #1748dc; --brand-blue-bright: #4b73ff; --brand-blue-soft: rgba(75,115,255,.16); --success: #50e6c1; color-scheme: dark; font-family: "Avenir Next", "Segoe UI", sans-serif; background: #0a1025; color: #edf1ff; }
* { box-sizing: border-box; } body { min-width: 320px; min-height: 100vh; margin: 0; } button, input, textarea { font: inherit; } button { cursor: pointer; } button:disabled { cursor: not-allowed; opacity: .56; }
.app-shell { position: relative; min-height: 100vh; padding: 28px clamp(24px, 4vw, 72px) 72px; overflow: hidden; background: radial-gradient(circle at 0 38%, rgba(33,76,211,.2), transparent 32%), radial-gradient(circle at 100% 26%, rgba(241,152,87,.1), transparent 34%), #0a1025; }
.app-shell__glow { position: absolute; width: 43vw; aspect-ratio: 1; border: 1px solid rgba(114,135,185,.14); border-radius: 50%; pointer-events: none; } .app-shell__glow--left { bottom: -27vw; left: -17vw; } .app-shell__glow--right { top: -34vw; right: -11vw; }
.app-header { position: relative; z-index: 1; display: flex; align-items: center; justify-content: space-between; min-height: 92px; padding: 18px 25px; border: 1px solid rgba(144,160,204,.25); border-radius: 25px; background: rgba(13,20,43,.88); }
.app-brand { display: flex; align-items: center; gap: 14px; } .app-brand img { width: clamp(160px,17vw,235px); max-height: 45px; object-fit: contain; } .app-brand span { padding-left: 14px; border-left: 1px solid rgba(144,160,204,.32); color: #b9c4dc; font-weight: 750; }
.profile-button, .seller-nav__item, .secondary-button { border: 1px solid rgba(149,164,203,.28); border-radius: 14px; color: #dce5f9; background: rgba(31,40,70,.72); font-weight: 750; } .profile-button { padding: 11px 15px; font-size: 13px; }
.seller-dashboard { position: relative; z-index: 1; width: min(100%,1560px); margin: 42px auto 0; } .seller-nav { display: flex; width: max-content; max-width: 100%; gap: 9px; padding: 7px; border: 1px solid rgba(149,164,203,.27); border-radius: 19px; background: rgba(10,17,37,.74); } .seller-nav__item { min-height: 46px; padding: 0 20px; font-size: 15px; } .seller-nav__item--active { color: #fff; border-color: rgba(75,115,255,.68); background: linear-gradient(115deg, var(--brand-blue), var(--brand-blue-bright)); } .seller-nav small { margin-left: 5px; color: #efb44b; }
.dashboard-heading { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin: 46px 0 27px; } .dashboard-heading h1, .connection-modal h1 { margin: 8px 0 10px; font-size: clamp(35px,4vw,52px); letter-spacing: -.065em; } .dashboard-heading p:not(.kicker) { max-width: 630px; margin: 0; color: #aeb9d4; line-height: 1.55; } .kicker { margin: 0; color: #7290ff; font-size: 12px; font-weight: 850; letter-spacing: .14em; text-transform: uppercase; }
.primary-button { min-height: 52px; padding: 0 20px; border: 0; border-radius: 14px; color: #fff; background: linear-gradient(135deg,var(--brand-blue),var(--brand-blue-bright)); font-weight: 850; box-shadow: 0 13px 32px rgba(32,77,220,.28); transition: transform .2s, filter .2s; } .primary-button:hover:not(:disabled) { transform: translateY(-2px); filter: brightness(1.08); }
.connection-grid { display: grid; grid-template-columns: repeat(3,minmax(255px,1fr)); gap: 20px; } .connection-card, .connection-add-card { min-height: 300px; padding: 26px; border: 1px solid rgba(135,157,207,.24); border-radius: 25px; background: linear-gradient(145deg,rgba(20,31,60,.95),rgba(12,18,42,.93)); } .connection-card--disabled { opacity: .66; } .connection-card { display: grid; gap: 16px; } .connection-card__head, .connection-card footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; } .connection-card h2 { margin: 0; font-size: 24px; letter-spacing: -.045em; } .connection-card p { margin: 5px 0 0; color: #aeb9d4; }
.market-mark { display: inline-grid; width: 43px; height: 43px; place-items: center; flex: 0 0 auto; border-radius: 12px; color: #fff; font-size: 12px; font-weight: 900; } .market-mark--ozon { background: #2f63fa; } .market-mark--yandex_market { color: #121726; background: #ffd062; font-size: 25px; } .connection-status { color: var(--success); font-size: 12px; font-weight: 900; letter-spacing: .06em; text-transform: uppercase; } .connection-status::before { content: ''; display: inline-block; width: 8px; height: 8px; margin-right: 7px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 5px rgba(80,230,193,.1); } .connection-status--disabled { color: #aeb9d4; }
.connection-card dl { display: grid; margin: 0; border-top: 1px solid rgba(145,164,205,.19); } .connection-card dl div { display: flex; justify-content: space-between; gap: 12px; padding: 11px 0; border-bottom: 1px solid rgba(145,164,205,.19); } .connection-card dt { color: #aeb9d4; } .connection-card dd { margin: 0; color: #f0f3fc; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; font-weight: 700; } .connection-card footer { color: #aeb9d4; font-size: 13px; } .connection-card footer button { padding: 0; border: 0; color: #ff9a9b; background: transparent; font-weight: 800; }
.connection-add-card { display: grid; place-content: center; justify-items: center; gap: 10px; color: #b7c2da; border-style: dashed; background: rgba(18,29,56,.38); } .connection-add-card strong { color: var(--brand-blue-bright); font-size: 47px; line-height: 1; font-weight: 400; } .connection-add-card span { font-weight: 800; } .empty-state { display: grid; min-height: 230px; place-items: center; color: #b7c2da; border: 1px dashed rgba(145,164,205,.35); border-radius: 24px; }
.auth-card { position: relative; z-index: 1; width: min(100%,870px); display: grid; grid-template-columns: minmax(250px,.9fr) minmax(280px,1fr); gap: clamp(30px,6vw,85px); margin: 12vh auto 0; padding: clamp(30px,5vw,66px); border: 1px solid rgba(144,160,204,.25); border-radius: 30px; background: linear-gradient(140deg,rgba(22,33,62,.97),rgba(10,15,34,.97)); box-shadow: 0 30px 90px rgba(0,0,0,.32); } .auth-card__intro h1 { margin: 12px 0 18px; white-space: pre-line; font-size: clamp(36px,4.3vw,65px); line-height: .95; letter-spacing: -.075em; } .auth-card__intro p:not(.kicker) { margin: 0; color: #aeb9d4; line-height: 1.55; }
.auth-form, .connection-form { display: grid; gap: 16px; align-content: center; } .auth-form label, .connection-form label { display: grid; gap: 7px; color: #c3cbe0; font-size: 13px; font-weight: 750; } .auth-form input, .connection-form input, .connection-form textarea { width: 100%; min-height: 52px; padding: 0 16px; border: 1px solid rgba(149,164,203,.28); border-radius: 13px; outline: none; color: #eef3ff; background: rgba(6,11,27,.66); transition: border-color .2s,box-shadow .2s; } .connection-form textarea { min-height: 105px; padding: 13px 16px; resize: vertical; } .auth-form input:focus, .connection-form input:focus, .connection-form textarea:focus { border-color: var(--brand-blue-bright); box-shadow: 0 0 0 4px var(--brand-blue-soft); } .form-error { margin: 0; color: #ffaaa8; font-size: 13px; } .auth-card__footer { grid-column: 2; display: flex; gap: 8px; color: #9eaac5; font-size: 13px; } .auth-card__footer button { padding: 0; border: 0; color: #7894ff; background: transparent; font-weight: 750; }
.modal-backdrop { position: fixed; z-index: 5; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(2,6,19,.7); backdrop-filter: blur(9px); } .connection-modal { position: relative; width: min(100%,670px); max-height: calc(100vh - 48px); overflow: auto; padding: clamp(28px,4vw,46px); border: 1px solid rgba(146,164,205,.3); border-radius: 26px; background: linear-gradient(145deg,#14203d,#0b1128); box-shadow: 0 30px 100px rgba(0,0,0,.45); } .modal-close { position: absolute; top: 18px; right: 22px; padding: 0; border: 0; color: #bac4dc; background: transparent; font-size: 38px; line-height: 1; }
.provider-picker { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 26px 0; } .provider-picker button { display: flex; align-items: center; gap: 10px; padding: 12px; border: 1px solid rgba(149,164,203,.28); border-radius: 14px; color: #dce5f9; background: rgba(31,40,70,.72); font-weight: 800; } .provider-picker button.active { border-color: var(--brand-blue-bright); background: linear-gradient(115deg,rgba(23,72,220,.38),rgba(75,115,255,.22)); } .provider-picker .market-mark { width: 35px; height: 35px; border-radius: 10px; }
.yandex-discovery { display: grid; gap: 13px; } .secondary-button { min-height: 48px; padding: 0 16px; } .store-choice { display: grid; gap: 8px; } .store-choice > span { color: #b7c2da; font-size: 13px; font-weight: 750; } .store-choice button { display: flex; justify-content: space-between; padding: 12px; border: 1px solid rgba(149,164,203,.25); border-radius: 12px; color: #edf1ff; background: rgba(24,34,61,.7); text-align: left; } .store-choice button.active { border-color: var(--brand-blue-bright); background: var(--brand-blue-soft); } .store-choice small { color: #aeb9d4; } .connection-form__actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 9px; } .connection-form__actions .primary-button { min-width: 190px; }
.snapshot-view { display: grid; gap: 18px; margin-top: 46px; } .snapshot-toolbar { display: grid; grid-template-columns: minmax(260px, 1.2fr) minmax(0, 1.6fr) auto; align-items: end; gap: 14px; } .snapshot-search { display: grid; gap: 7px; } .snapshot-search label, .orders-filter-row label > span { color: #c3cbe0; font-size: 12px; font-weight: 850; letter-spacing: .06em; text-transform: uppercase; } .snapshot-search input, .orders-filter-row input, .orders-filter-row select { width: 100%; min-height: 50px; padding: 0 15px; border: 1px solid rgba(149,164,203,.28); border-radius: 13px; outline: none; color: #eef3ff; background: rgba(6,11,27,.66); } .store-filters { display: flex; min-width: 0; gap: 8px; overflow-x: auto; padding-bottom: 1px; } .store-filters button { display: inline-flex; min-height: 50px; align-items: center; gap: 8px; flex: 0 0 auto; padding: 0 13px; border: 1px solid rgba(149,164,203,.28); border-radius: 13px; color: #cbd5eb; background: rgba(31,40,70,.72); font-weight: 800; } .store-filters button.active { color: #fff; border-color: rgba(75,115,255,.68); background: linear-gradient(115deg, var(--brand-blue), var(--brand-blue-bright)); } .store-filters .market-mark { width: 25px; height: 25px; border-radius: 8px; font-size: 8px; } .store-filters .market-mark--yandex_market { font-size: 16px; } .sync-button { display: inline-flex; min-width: 142px; min-height: 52px; align-items: center; justify-content: center; gap: 9px; padding: 0 17px; border: 1px solid #ee6cb5; border-radius: 50px; color: #fff; background: linear-gradient(140deg,#f13b9e,#cf206e); box-shadow: 0 12px 28px rgba(242,52,152,.27); font-weight: 850; } .sync-button > span:first-child { font-size: 25px; line-height: 1; } .spinning { animation: snapshot-spin .8s linear infinite; } @keyframes snapshot-spin { to { transform: rotate(360deg); } }
.orders-filter-row { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; padding: 14px; border: 1px solid rgba(144,160,204,.17); border-radius: 18px; background: rgba(14,22,48,.56); } .orders-filter-row label { display: grid; gap: 7px; min-width: 150px; } .orders-filter-row .status-select { min-width: 175px; } .date-divider { align-self: end; padding-bottom: 15px; color: #9daaca; } .filter-apply { min-height: 50px; } .filter-reset { min-height: 50px; padding: 0 13px; border: 0; color: #ffaaa8; background: transparent; font-weight: 800; } .catalog-search-actions { display: flex; justify-content: flex-start; } .snapshot-count { margin: 3px 0 0; color: #b7c2da; font-size: 14px; font-weight: 750; }
.snapshot-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 16px; } .snapshot-card { min-height: 152px; display: grid; gap: 15px; padding: 21px; border: 1px solid rgba(139,160,210,.25); border-radius: 22px; background: linear-gradient(145deg,rgba(27,43,81,.96),rgba(15,24,54,.98)); box-shadow: inset 0 1px rgba(255,255,255,.025); } .snapshot-card__head { display: flex; min-width: 0; align-items: center; gap: 13px; } .snapshot-card__head > div { min-width: 0; } .snapshot-card h2 { overflow: hidden; margin: 0; color: #f6f8ff; font-size: 17px; line-height: 1.25; letter-spacing: -.03em; text-overflow: ellipsis; white-space: nowrap; } .snapshot-card p { overflow: hidden; margin: 5px 0 0; color: #b8c3dd; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; } .snapshot-card__footer { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding-top: 14px; border-top: 1px dashed rgba(164,182,224,.24); color: #bfc9df; font-size: 13px; } .snapshot-card__footer strong { color: #f2f5ff; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; } .snapshot-card__footer time { flex: 0 0 auto; color: #aeb9d4; font-size: 12px; } .order-card { min-height: 171px; } .order-card__body { display: flex; align-items: center; justify-content: space-between; gap: 18px; } .order-card__body > strong { overflow: hidden; color: #eef2fc; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; } .order-status { flex: 0 0 auto; font-size: 12px; font-weight: 900; letter-spacing: .04em; text-transform: uppercase; } .order-status--processing { color: #ffc75a; } .order-status--in_delivery { color: #65b5ff; } .order-status--delivered { color: #4ee6bd; } .order-status--cancelled, .order-status--problem { color: #ff969b; } .pagination { display: flex; align-items: center; justify-content: center; gap: 16px; padding-top: 7px; color: #b7c2da; font-size: 14px; } .pagination button { min-height: 42px; padding: 0 14px; border: 1px solid rgba(149,164,203,.28); border-radius: 12px; color: #dce5f9; background: rgba(31,40,70,.72); font-weight: 750; } .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; }
@media (max-width:900px) { .connection-grid { grid-template-columns: repeat(2,minmax(255px,1fr)); } .dashboard-heading { align-items: start; flex-direction: column; } } @media (max-width:660px) { .app-shell { padding: 16px 16px 44px; } .app-header { min-height: auto; padding: 14px; border-radius: 19px; } .app-brand img { width: 147px; } .app-brand span { display: none; } .profile-button { max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } .seller-dashboard { margin-top: 26px; } .seller-nav { width: 100%; gap: 4px; } .seller-nav__item { flex: 1; padding: 0 9px; font-size: 13px; } .seller-nav small { display: none; } .dashboard-heading { margin: 30px 0 22px; } .dashboard-heading h1 { font-size: 40px; } .connection-grid { grid-template-columns: 1fr; } .connection-card, .connection-add-card { min-height: 265px; } .auth-card { grid-template-columns: 1fr; margin-top: 58px; padding: 32px 25px; border-radius: 23px; } .auth-card__footer { grid-column: 1; flex-wrap: wrap; } .auth-card__intro h1 { font-size: 45px; } .provider-picker { grid-template-columns: 1fr; } .connection-form__actions { flex-direction: column-reverse; } .connection-form__actions .primary-button { width: 100%; } }
</style>
