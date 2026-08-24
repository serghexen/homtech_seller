<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { apiRequest } from './api'
import homtechLogo from './assets/homtech-wordmark.png'
import ozonLogo from './assets/ozon-logo.png'
import yandexMarketLogo from './assets/yandex-market-logo.png'
import HamsterLoader from './components/HamsterLoader.vue'
import ProductCardModal from './components/ProductCardModal.vue'
import packageMetadata from '../package.json'

const appVersion = packageMetadata.version
const mode = ref('login')
const loading = ref(false)
const error = ref('')
const notice = ref('')
const user = ref(null)
const isRestoringSession = ref(true)
const connections = ref([])
const connectionsLoading = ref(false)
const connectionActionId = ref(null)
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
const ordersFiltersOpen = ref(false)
const isSyncing = ref(false)
const isConnectionModalOpen = ref(false)
const isSavingConnection = ref(false)
const isDiscoveringStores = ref(false)
const connectionError = ref('')
const yandexStores = ref([])
const selectedYandexStore = ref(null)
const selectedCatalogItem = ref(null)
const selectedStockLoading = ref(false)
const selectedStockError = ref('')
const selectedProductOrders = ref([])
const selectedProductOrdersTotal = ref(0)
const selectedProductOrdersLoading = ref(false)
const selectedProductOrdersError = ref('')
const form = reactive({ email: '', password: '', display_name: '' })
const connectionForm = reactive({ provider_code: 'ozon', display_name: '', client_id: '', token: '' })

const isYandex = computed(() => connectionForm.provider_code === 'yandex_market')
const activeConnections = computed(() => connections.value.filter((connection) => connection.status === 'active'))
const hasActiveConnections = computed(() => activeConnections.value.length > 0)
const pageSize = 24
const catalogPageCount = computed(() => Math.max(1, Math.ceil(catalogTotal.value / pageSize)))
const ordersPageCount = computed(() => Math.max(1, Math.ceil(ordersTotal.value / pageSize)))
const appliedOrderFilterCount = computed(() => ['status', 'date_from', 'date_to'].filter((key) => appliedOrderFilters[key]).length)

function switchMode(nextMode) {
  // Переключает сценарий входа без потери введённого email и показывает только нужные поля.
  mode.value = nextMode
  error.value = ''
}

function providerName(providerCode) {
  // Превращает технический код адаптера в единое название, понятное оператору в карточках и форме.
  return providerCode === 'yandex_market' ? 'Яндекс Маркет' : 'Ozon'
}

function providerLogo(providerCode) {
  // Использует локальные фирменные логотипы без обращения к внешним ресурсам.
  return providerCode === 'yandex_market' ? yandexMarketLogo : ozonLogo
}

async function openProductCard(item) {
  // Сразу показывает карточку, затем параллельно читает её локальные заказы и актуальный остаток Яндекс Маркета.
  selectedCatalogItem.value = item
  selectedStockError.value = ''
  selectedProductOrders.value = []
  selectedProductOrdersTotal.value = 0
  selectedProductOrdersError.value = ''
  const requests = [loadSelectedProductOrders()]
  if (item.provider_code === 'yandex_market') requests.push(refreshSelectedProductStock())
  await Promise.allSettled(requests)
}

function closeProductCard() {
  selectedCatalogItem.value = null
  selectedStockLoading.value = false
  selectedStockError.value = ''
  selectedProductOrders.value = []
  selectedProductOrdersTotal.value = 0
  selectedProductOrdersLoading.value = false
  selectedProductOrdersError.value = ''
}

async function loadSelectedProductOrders() {
  // Берёт только позиции этой карточки из локального снимка Seller, не обращаясь к маркетплейсу.
  const item = selectedCatalogItem.value
  if (!item || selectedProductOrdersLoading.value) return
  const identity = `${item.connection_id}:${item.external_product_id}`
  selectedProductOrdersLoading.value = true
  selectedProductOrdersError.value = ''
  try {
    const query = queryString({
      connection_id: item.connection_id,
      external_product_id: item.external_product_id,
      page: 1,
      page_size: 20,
    })
    const result = await apiRequest(`/marketplaces/catalog/orders?${query}`)
    if (!selectedCatalogItem.value || `${selectedCatalogItem.value.connection_id}:${selectedCatalogItem.value.external_product_id}` !== identity) return
    selectedProductOrders.value = result.items
    selectedProductOrdersTotal.value = result.total
  } catch (requestError) {
    if (selectedCatalogItem.value && `${selectedCatalogItem.value.connection_id}:${selectedCatalogItem.value.external_product_id}` === identity) {
      selectedProductOrdersError.value = requestError.message || 'Не удалось загрузить заказы карточки'
    }
  } finally {
    if (selectedCatalogItem.value && `${selectedCatalogItem.value.connection_id}:${selectedCatalogItem.value.external_product_id}` === identity) {
      selectedProductOrdersLoading.value = false
    }
  }
}

async function refreshSelectedProductStock() {
  // Обновляет только остаток одной открытой карточки read-only запросом, не перезагружая весь каталог.
  const item = selectedCatalogItem.value
  if (!item || item.provider_code !== 'yandex_market' || selectedStockLoading.value) return
  const identity = `${item.connection_id}:${item.offer_id}`
  selectedStockLoading.value = true
  selectedStockError.value = ''
  try {
    const result = await apiRequest('/marketplaces/catalog/stock/refresh', {
      method: 'POST',
      body: JSON.stringify({ connection_id: item.connection_id, offer_id: item.offer_id }),
    })
    if (!selectedCatalogItem.value || `${selectedCatalogItem.value.connection_id}:${selectedCatalogItem.value.offer_id}` !== identity) return
    selectedCatalogItem.value.available_stock = result.available_stock
    selectedCatalogItem.value.stock_synced_at = result.checked_at
    const listItem = catalogItems.value.find((candidate) => `${candidate.connection_id}:${candidate.offer_id}` === identity)
    if (listItem) {
      listItem.available_stock = result.available_stock
      listItem.stock_synced_at = result.checked_at
    }
  } catch (requestError) {
    if (selectedCatalogItem.value && `${selectedCatalogItem.value.connection_id}:${selectedCatalogItem.value.offer_id}` === identity) {
      selectedStockError.value = requestError.message || 'Не удалось получить актуальный остаток'
    }
  } finally {
    if (selectedCatalogItem.value && `${selectedCatalogItem.value.connection_id}:${selectedCatalogItem.value.offer_id}` === identity) {
      selectedStockLoading.value = false
    }
  }
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

function wait(milliseconds) {
  // Неблокирующая пауза нужна только для короткого polling статуса фоновых заданий.
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}

async function waitForSyncJobs(jobIds) {
  // Ждёт завершения долговечных заданий, не удерживая исходный HTTP-запрос открытым.
  const deadline = Date.now() + 15 * 60 * 1000
  while (Date.now() < deadline) {
    const result = await apiRequest(`/marketplaces/sync-jobs?job_ids=${encodeURIComponent(jobIds.join(','))}`)
    if (result.items.length !== jobIds.length) throw new Error('Не удалось получить состояние всех заданий')
    if (result.items.every((item) => item.status === 'succeeded' || item.status === 'failed')) return result.items
    await wait(1000)
  }
  throw new Error('Синхронизация продолжается в фоне. Обновите страницу немного позже.')
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
  if (!user.value || !hasActiveConnections.value) return
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
  if (!user.value || !hasActiveConnections.value) return
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
  if (!hasActiveConnections.value) return
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
  // Ставит выбранные магазины в очередь, ждёт статусы и затем перечитывает локальный снимок.
  const kind = activeSection.value === 'catalog' ? 'catalog' : 'orders'
  isSyncing.value = true
  error.value = ''
  try {
    const result = await apiRequest(`/marketplaces/${kind}/sync`, {
      method: 'POST',
      body: JSON.stringify({ connection_id: selectedConnectionId.value }),
    })
    const jobIds = result.items.map((item) => item.id)
    if (!jobIds.length) throw new Error('Не удалось поставить синхронизацию в очередь')
    const completedJobs = await waitForSyncJobs(jobIds)
    const failedJobs = completedJobs.filter((item) => item.status === 'failed')
    if (failedJobs.length) error.value = failedJobs.map((item) => `${item.store_name}: ${item.error}`).join(' ')
    if (kind === 'catalog') await loadCatalog()
    else await loadOrders()
    await loadConnections()
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

async function handleCatalogSearchInput(event) {
  // Очистка строки сразу возвращает полный каталог, как и в поиске по заказам.
  catalogSearch.value = event.target.value.trim()
  if (catalogSearch.value) return
  await applyCatalogSearch()
}

async function applyOrderFilters() {
  // Копирует черновик фильтров после нажатия «Применить» и только затем обновляет список заказов.
  Object.assign(appliedOrderFilters, orderFilters)
  ordersPage.value = 1
  await loadOrders()
  ordersFiltersOpen.value = false
}

async function handleOrderSearchInput(event) {
  // После очистки уже применённого запроса сразу возвращает полный список заказов.
  orderFilters.query = event.target.value.trim()
  if (orderFilters.query || !appliedOrderFilters.query) return
  await applyOrderFilters()
}

async function resetOrderFilters() {
  // Сбрасывает и черновик, и применённые условия, чтобы вернуть полный снимок заказов одной кнопкой.
  Object.assign(orderFilters, { query: '', status: '', date_from: '', date_to: '' })
  Object.assign(appliedOrderFilters, orderFilters)
  ordersPage.value = 1
  await loadOrders()
  ordersFiltersOpen.value = false
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
  // Отправляет регистрацию или вход и после успеха сразу открывает кабинет созданного аккаунта.
  loading.value = true
  error.value = ''
  try {
    const path = mode.value === 'register' ? '/auth/register' : '/auth/login'
    const body = mode.value === 'register'
      ? { email: form.email, password: form.password, display_name: form.display_name }
      : { email: form.email, password: form.password }
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
  // Проверяет ключ у маркетплейса и только затем создаёт или обновляет подключение текущего аккаунта.
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

async function toggleConnection(connection) {
  // Делает отключение обратимым и явно сообщает результат вместо исчезновения единственной кнопки.
  const shouldEnable = connection.status === 'disabled'
  connectionActionId.value = connection.id
  error.value = ''
  notice.value = ''
  try {
    await apiRequest(`/marketplaces/connections/${connection.id}/${shouldEnable ? 'enable' : 'disable'}`, { method: 'POST' })
    await loadConnections()
    notice.value = shouldEnable ? `Магазин «${connection.display_name}» снова подключён` : `Магазин «${connection.display_name}» отключён. Данные сохранены.`
  } catch (requestError) {
    error.value = requestError.message || (shouldEnable ? 'Не удалось подключить магазин снова' : 'Не удалось отключить магазин')
  } finally {
    connectionActionId.value = null
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
  } finally {
    isRestoringSession.value = false
  }
})
</script>

<template>
  <main class="app-shell">
    <div class="app-shell__glow app-shell__glow--left"></div>
    <div class="app-shell__glow app-shell__glow--right"></div>
    <p class="app-version" :aria-label="`Версия Seller ${appVersion}`">SELLER · v{{ appVersion }}</p>

    <header class="app-header">
      <div class="app-brand"><img :src="homtechLogo" alt="HomTech" /><span>Seller</span></div>
      <button v-if="user" class="profile-button" type="button" @click="logout">{{ user.display_name || user.email }} · Выйти</button>
    </header>

    <section v-if="isRestoringSession" class="session-loader" aria-live="polite" aria-busy="true">
      <HamsterLoader label="Открываем Seller…" />
    </section>

    <section v-else-if="user" class="seller-dashboard" aria-live="polite">
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
      <p v-if="notice" class="form-success" role="status">{{ notice }}</p>
      <div v-if="activeSection === 'stores' && connectionsLoading" class="empty-state">Загружаем подключённые магазины…</div>
      <div v-else-if="activeSection === 'stores'" class="connection-grid">
        <article v-for="connection in connections" :key="connection.id" class="connection-card" :class="{ 'connection-card--disabled': connection.status === 'disabled' }">
          <div class="connection-card__head">
            <div class="market-mark" :class="`market-mark--${connection.provider_code}`"><img :src="providerLogo(connection.provider_code)" alt="" /></div>
            <span class="connection-status" :class="`connection-status--${connection.status}`">{{ connectionStatus(connection.status) }}</span>
          </div>
          <div><h2>{{ connection.display_name }}</h2><p>{{ providerName(connection.provider_code) }}</p></div>
          <dl>
            <div><dt>API-ключ</dt><dd>{{ connection.token_masked }}</dd></div>
            <div v-if="connection.provider_code === 'ozon'"><dt>Client ID</dt><dd>{{ connection.client_id }}</dd></div>
            <div v-else><dt>Кабинет / магазин</dt><dd>{{ connection.business_id }} / {{ connection.campaign_id }}</dd></div>
          </dl>
          <footer>
            <span>{{ connection.status === 'disabled' ? 'Данные магазина сохранены' : 'Подключён' }}</span>
            <button
              type="button"
              :class="{ 'connection-card__action--enable': connection.status === 'disabled' }"
              :disabled="connectionActionId === connection.id"
              @click="toggleConnection(connection)"
            >
              {{ connectionActionId === connection.id ? 'Подождите…' : connection.status === 'disabled' ? 'Подключить снова' : 'Отключить' }}
            </button>
          </footer>
        </article>
        <button class="connection-add-card" type="button" @click="openConnectionModal"><strong>+</strong><span>Подключить магазин</span></button>
      </div>

      <section v-if="activeSection === 'catalog' || activeSection === 'orders'" class="snapshot-view">
        <Transition name="snapshot-loader">
          <div v-if="isSyncing" class="snapshot-sync-overlay" aria-live="assertive" aria-busy="true">
            <div class="snapshot-sync-overlay__card">
              <HamsterLoader :label="activeSection === 'catalog' ? 'Обновляем каталог…' : 'Обновляем заказы…'" />
            </div>
          </div>
        </Transition>

        <div v-if="!hasActiveConnections" class="section-gate" aria-live="polite">
          <span class="section-gate__mark" aria-hidden="true">+</span>
          <p class="kicker">ПЕРВЫЙ ШАГ</p>
          <h1>{{ activeSection === 'catalog' ? 'Каталог появится после подключения магазина' : 'Заказы появятся после подключения магазина' }}</h1>
          <p>{{ activeSection === 'catalog' ? '' : '' }}</p>
          <button class="primary-button" type="button" @click="openConnectionModal">Подключить магазин</button>
        </div>

        <template v-else>
          <div class="snapshot-toolbar">
            <div class="snapshot-search">
              <label>{{ activeSection === 'catalog' ? 'Поиск' : 'Поиск по заказам' }}</label>
              <div v-if="activeSection === 'catalog'" class="snapshot-search__row snapshot-search__row--catalog">
                <input
                  v-model.trim="catalogSearch"
                  type="search"
                  placeholder="Название, артикул или SKU"
                  @input="handleCatalogSearchInput"
                  @keyup.enter="applyCatalogSearch"
                />
                <Transition name="search-action">
                  <button v-if="catalogSearch.trim()" class="search-submit" type="button" @click="applyCatalogSearch">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
                    <span>Найти</span>
                  </button>
                </Transition>
              </div>
              <div v-else class="snapshot-search__row">
                <input
                  v-model.trim="orderFilters.query"
                  type="search"
                  placeholder="Номер заказа, товар или SKU"
                  @input="handleOrderSearchInput"
                  @keyup.enter="applyOrderFilters"
                />
                <Transition name="search-action">
                  <button v-if="orderFilters.query.trim()" class="search-submit" type="button" @click="applyOrderFilters">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
                    <span>Найти</span>
                  </button>
                </Transition>
                <button
                  class="filter-toggle"
                  :class="{ 'filter-toggle--active': ordersFiltersOpen || appliedOrderFilterCount }"
                  type="button"
                  :aria-expanded="ordersFiltersOpen"
                  aria-controls="orders-advanced-filters"
                  @click="ordersFiltersOpen = !ordersFiltersOpen"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4" /></svg>
                  <span>Фильтры</span>
                  <small v-if="appliedOrderFilterCount">{{ appliedOrderFilterCount }}</small>
                </button>
              </div>
            </div>
            <div class="store-filters" aria-label="Фильтр по магазину">
              <button :class="{ active: selectedConnectionId === null }" type="button" @click="selectConnection(null)">Все магазины</button>
              <button v-for="connection in activeConnections" :key="connection.id" :class="{ active: selectedConnectionId === connection.id }" type="button" @click="selectConnection(connection.id)">
                <span class="market-mark" :class="`market-mark--${connection.provider_code}`"><img :src="providerLogo(connection.provider_code)" alt="" /></span>{{ connection.display_name }}
              </button>
            </div>
            <button class="sync-button" type="button" :disabled="isSyncing" :title="selectedConnectionId ? 'Обновить выбранный магазин' : 'Обновить все активные магазины'" @click="syncCurrentSnapshot">
              <span :class="{ spinning: isSyncing }">↻</span><span class="sync-button__text">{{ isSyncing ? 'Обновляем…' : 'Обновить' }}</span>
            </button>
          </div>

          <Transition name="order-filters">
            <div v-if="activeSection === 'orders' && ordersFiltersOpen" id="orders-advanced-filters" class="orders-filter-row">
              <div class="orders-filter-row__period">
                <span>Период</span>
                <label><span class="sr-only">Дата начала</span><input v-model="orderFilters.date_from" type="date" /></label>
                <span class="date-divider">—</span>
                <label><span class="sr-only">Дата окончания</span><input v-model="orderFilters.date_to" type="date" /></label>
              </div>
              <label class="status-select"><span>Статус</span><select v-model="orderFilters.status"><option value="">Все статусы</option><option value="processing">В процессе</option><option value="in_delivery">Доставляется</option><option value="delivered">Доставлен</option><option value="cancelled">Отменён</option><option value="problem">Проблема</option></select></label>
              <div class="orders-filter-row__actions">
                <button class="primary-button filter-apply" type="button" @click="applyOrderFilters">Применить</button>
                <button class="filter-reset" type="button" @click="resetOrderFilters">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4v6h6" /><path d="M5.6 15a7 7 0 1 0 1.2-7.7L4 10" /></svg>
                  <span>Сбросить</span>
                </button>
              </div>
            </div>
          </Transition>

          <p class="snapshot-count">Найдено: {{ activeSection === 'catalog' ? catalogTotal : ordersTotal }}</p>
          <div v-if="(activeSection === 'catalog' && catalogLoading) || (activeSection === 'orders' && ordersLoading)" class="empty-state">Загружаем локальный снимок…</div>
          <div v-else-if="activeSection === 'catalog' && !catalogItems.length" class="empty-state">Каталог пока пуст. Нажмите «Обновить», чтобы прочитать товары из выбранного магазина.</div>
          <div v-else-if="activeSection === 'orders' && !orders.length" class="empty-state">Заказов пока нет в снимке. Нажмите «Обновить», чтобы прочитать свежие заказы.</div>

          <div v-else-if="activeSection === 'catalog'" class="snapshot-grid">
            <article
              v-for="item in catalogItems"
              :key="`${item.connection_id}-${item.external_product_id}`"
              class="snapshot-card catalog-card"
              role="button"
              tabindex="0"
              :aria-label="`Открыть карточку товара ${item.title || item.sku || ''}`"
              @click="openProductCard(item)"
              @keydown.enter.prevent="openProductCard(item)"
              @keydown.space.prevent="openProductCard(item)"
            >
              <div class="snapshot-card__head"><span class="market-mark" :class="`market-mark--${item.provider_code}`"><img :src="providerLogo(item.provider_code)" alt="" /></span><div><h2>{{ item.title || 'Без названия' }}</h2><p>{{ item.store_name }} · {{ providerName(item.provider_code) }}</p></div><span class="catalog-card__open" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M14 5h5v5" /><path d="m10 14 9-9" /><path d="M19 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" /></svg></span></div>
              <div class="snapshot-card__footer catalog-card__footer"><div class="snapshot-card__facts"><span>SKU: <strong>{{ item.sku || item.offer_id || '—' }}</strong></span><span class="catalog-card__stock">Остаток: <strong>{{ Number.isInteger(item.available_stock) ? item.available_stock : '—' }}</strong></span></div><time :datetime="item.stock_synced_at || item.synced_at">{{ formatDate(item.stock_synced_at || item.synced_at) }}</time></div>
            </article>
          </div>
          <div v-else class="snapshot-grid">
            <article v-for="item in orders" :key="`${item.connection_id}-${item.external_order_id}-${item.external_item_id}`" class="snapshot-card order-card">
              <div class="snapshot-card__head"><span class="market-mark" :class="`market-mark--${item.provider_code}`"><img :src="providerLogo(item.provider_code)" alt="" /></span><div><h2>Заказ №{{ item.external_order_id }}</h2><p>{{ item.store_name }} · {{ providerName(item.provider_code) }}</p></div><span class="order-status" :class="`order-status--${item.status}`">{{ orderStatus(item.status) }}</span></div>
              <div class="order-card__body"><strong>{{ item.title || 'Товар без названия' }}</strong></div>
              <div class="snapshot-card__footer"><span>SKU: <strong>{{ item.sku || item.offer_id || '—' }}</strong></span><time :datetime="item.updated_at || item.created_at || item.synced_at">{{ formatDate(item.updated_at || item.created_at || item.synced_at) }}</time></div>
            </article>
          </div>
          <div v-if="(activeSection === 'catalog' ? catalogPageCount : ordersPageCount) > 1" class="pagination">
            <button type="button" :disabled="(activeSection === 'catalog' ? catalogPage : ordersPage) === 1" @click="changePage((activeSection === 'catalog' ? catalogPage : ordersPage) - 1)">← Назад</button>
            <span>Страница {{ activeSection === 'catalog' ? catalogPage : ordersPage }} из {{ activeSection === 'catalog' ? catalogPageCount : ordersPageCount }}</span>
            <button type="button" :disabled="(activeSection === 'catalog' ? catalogPage : ordersPage) === (activeSection === 'catalog' ? catalogPageCount : ordersPageCount)" @click="changePage((activeSection === 'catalog' ? catalogPage : ordersPage) + 1)">Далее →</button>
          </div>
        </template>
      </section>
    </section>

    <section v-else class="auth-card">
      <div class="auth-card__intro">
        <p class="kicker">{{ mode === 'register' ? 'НОВЫЙ АККАУНТ' : 'HOMTECH SELLER' }}</p>
        <h1>{{ mode === 'register' ? 'Создайте\nаккаунт.' : 'Управляйте\nмагазинами.' }}</h1>
        <p>{{ mode === 'register' ? 'Подключите магазины и управляйте каталогом и заказами в одном кабинете.' : 'Подключайте маркетплейсы и следите за каталогом и заказами в одном кабинете.' }}</p>
      </div>
      <form class="auth-form" @submit.prevent="submit">
        <label v-if="mode === 'register'"><span>Ваше имя</span><input v-model.trim="form.display_name" autocomplete="name" maxlength="120" /></label>
        <label><span>Email</span><input v-model.trim="form.email" required type="email" autocomplete="email" /></label>
        <label><span>Пароль</span><input v-model="form.password" required type="password" autocomplete="current-password" /></label>
        <p v-if="error" class="form-error">{{ error }}</p>
        <button class="primary-button" type="submit" :disabled="loading">{{ loading ? 'Проверяем…' : mode === 'register' ? 'Создать аккаунт' : 'Войти' }}</button>
      </form>
      <footer class="auth-card__footer"><span>{{ mode === 'register' ? 'Уже есть аккаунт?' : 'Нет аккаунта?' }}</span><button type="button" @click="switchMode(mode === 'register' ? 'login' : 'register')">{{ mode === 'register' ? 'Войти' : 'Зарегистрироваться' }}</button></footer>
    </section>

    <ProductCardModal
      v-if="selectedCatalogItem"
      :item="selectedCatalogItem"
      :provider-name="providerName(selectedCatalogItem.provider_code)"
      :provider-logo="providerLogo(selectedCatalogItem.provider_code)"
      :synced-label="formatDate(selectedCatalogItem.synced_at)"
      :stock-synced-label="formatDate(selectedCatalogItem.stock_synced_at)"
      :stock-loading="selectedStockLoading"
      :stock-error="selectedStockError"
      :orders="selectedProductOrders"
      :orders-total="selectedProductOrdersTotal"
      :orders-loading="selectedProductOrdersLoading"
      :orders-error="selectedProductOrdersError"
      @refresh-stock="refreshSelectedProductStock"
      @close="closeProductCard"
    />

    <div v-if="isConnectionModalOpen" class="modal-backdrop" @click.self="closeConnectionModal">
      <section class="connection-modal" role="dialog" aria-modal="true" aria-labelledby="connection-title">
        <button class="modal-close" type="button" aria-label="Закрыть" @click="closeConnectionModal">×</button>
        <p class="kicker">НОВОЕ ПОДКЛЮЧЕНИЕ</p><h1 id="connection-title">Подключить магазин</h1>
        <div class="provider-picker">
          <button type="button" :class="{ active: !isYandex }" @click="chooseProvider('ozon')"><span class="market-mark market-mark--ozon"><img :src="ozonLogo" alt="" /></span>Ozon</button>
          <button type="button" :class="{ active: isYandex }" @click="chooseProvider('yandex_market')"><span class="market-mark market-mark--yandex_market"><img :src="yandexMarketLogo" alt="" /></span>Яндекс Маркет</button>
        </div>
        <form class="connection-form" @submit.prevent="saveConnection">
          <label><span>Название магазина</span><input v-model.trim="connectionForm.display_name" :readonly="isYandex && !!selectedYandexStore" required /></label>
          <label v-if="!isYandex"><span>Client ID кабинета</span><input v-model.trim="connectionForm.client_id" required inputmode="numeric" /></label>
          <label><span>{{ isYandex ? 'API-Key' : 'API Key' }}</span><textarea v-model="connectionForm.token" required /></label>
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
.app-version { position: fixed; z-index: 0; right: clamp(20px,3vw,50px); bottom: 22px; margin: 0; color: rgba(148,163,199,.42); font-family: ui-monospace,SFMono-Regular,Menlo,monospace; font-size: 10px; font-weight: 700; letter-spacing: .16em; pointer-events: none; user-select: none; }
.app-header { position: relative; z-index: 1; display: flex; align-items: center; justify-content: space-between; min-height: 92px; padding: 18px 25px; border: 1px solid rgba(144,160,204,.25); border-radius: 25px; background: rgba(13,20,43,.88); }
.app-brand { display: flex; align-items: center; gap: 14px; } .app-brand img { width: clamp(160px,17vw,235px); max-height: 45px; object-fit: contain; } .app-brand span { padding-left: 14px; border-left: 1px solid rgba(144,160,204,.32); color: #b9c4dc; font-weight: 750; }
.profile-button, .seller-nav__item, .secondary-button { border: 1px solid rgba(149,164,203,.28); border-radius: 14px; color: #dce5f9; background: rgba(31,40,70,.72); font-weight: 750; } .profile-button { padding: 11px 15px; font-size: 13px; }
.session-loader { position: relative; z-index: 1; display: grid; width: max-content; max-width: 100%; place-items: center; margin: 18vh auto 0; padding: 22px 28px 20px; border: 1px solid rgba(144,160,204,.25); border-radius: 22px; background: linear-gradient(140deg,rgba(22,33,62,.96),rgba(10,15,34,.96)); box-shadow: 0 24px 70px rgba(0,0,0,.28); }
.seller-dashboard { position: relative; z-index: 1; width: min(100%,1760px); margin: 42px auto 0; } .seller-nav { display: flex; width: max-content; max-width: 100%; gap: 9px; padding: 7px; border: 1px solid rgba(149,164,203,.27); border-radius: 19px; background: rgba(10,17,37,.74); } .seller-nav__item { min-height: 46px; padding: 0 20px; font-size: 15px; } .seller-nav__item--active { color: #fff; border-color: rgba(75,115,255,.68); background: linear-gradient(115deg, var(--brand-blue), var(--brand-blue-bright)); } .seller-nav small { margin-left: 5px; color: #efb44b; }
.dashboard-heading { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin: 46px 0 27px; } .dashboard-heading h1, .connection-modal h1 { margin: 8px 0 10px; font-size: clamp(35px,4vw,52px); letter-spacing: -.065em; } .dashboard-heading p:not(.kicker) { max-width: 630px; margin: 0; color: #aeb9d4; line-height: 1.55; } .kicker { margin: 0; color: #7290ff; font-size: 12px; font-weight: 850; letter-spacing: .14em; text-transform: uppercase; }
.primary-button { min-height: 52px; padding: 0 20px; border: 0; border-radius: 14px; color: #fff; background: linear-gradient(135deg,var(--brand-blue),var(--brand-blue-bright)); font-weight: 850; box-shadow: 0 13px 32px rgba(32,77,220,.28); transition: transform .2s, filter .2s; } .primary-button:hover:not(:disabled) { transform: translateY(-2px); filter: brightness(1.08); }
.connection-grid { display: grid; grid-template-columns: repeat(3,minmax(255px,1fr)); gap: 20px; } .connection-card, .connection-add-card { min-height: 300px; padding: 26px; border: 1px solid rgba(135,157,207,.24); border-radius: 25px; background: linear-gradient(145deg,rgba(20,31,60,.95),rgba(12,18,42,.93)); } .connection-card--disabled { border-color: rgba(135,157,207,.16); background: linear-gradient(145deg,rgba(18,27,51,.82),rgba(11,16,36,.86)); } .connection-card--disabled > :not(footer) { opacity: .66; } .connection-card { display: grid; gap: 16px; } .connection-card__head, .connection-card footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; } .connection-card h2 { margin: 0; font-size: 24px; letter-spacing: -.045em; } .connection-card p { margin: 5px 0 0; color: #aeb9d4; }
.market-mark { display: inline-grid; width: 43px; height: 43px; place-items: center; flex: 0 0 auto; overflow: hidden; border: 1px solid rgba(255,255,255,.17); border-radius: 12px; background: #fff; box-shadow: 0 7px 18px rgba(0,0,0,.16); } .market-mark img { display: block; width: 100%; height: 100%; object-fit: cover; } .market-mark--ozon img { transform: scale(1.32); } .market-mark--yandex_market img { transform: scale(1.03); } .connection-status { color: var(--success); font-size: 12px; font-weight: 900; letter-spacing: .06em; text-transform: uppercase; } .connection-status::before { content: ''; display: inline-block; width: 8px; height: 8px; margin-right: 7px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 5px rgba(80,230,193,.1); } .connection-status--disabled { color: #aeb9d4; }
.connection-card dl { display: grid; margin: 0; border-top: 1px solid rgba(145,164,205,.19); } .connection-card dl div { display: flex; justify-content: space-between; gap: 12px; padding: 11px 0; border-bottom: 1px solid rgba(145,164,205,.19); } .connection-card dt { color: #aeb9d4; } .connection-card dd { margin: 0; color: #f0f3fc; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; font-weight: 700; } .connection-card footer { color: #aeb9d4; font-size: 13px; } .connection-card footer button { padding: 0; border: 0; color: #ff9a9b; background: transparent; font-weight: 800; } .connection-card footer .connection-card__action--enable { color: #7894ff; }
.connection-add-card { display: grid; place-content: center; justify-items: center; gap: 10px; color: #b7c2da; border-style: dashed; background: rgba(18,29,56,.38); } .connection-add-card strong { color: var(--brand-blue-bright); font-size: 47px; line-height: 1; font-weight: 400; } .connection-add-card span { font-weight: 800; } .empty-state { display: grid; min-height: 230px; place-items: center; color: #b7c2da; border: 1px dashed rgba(145,164,205,.35); border-radius: 24px; }
.section-gate { position: relative; min-height: 430px; display: grid; place-content: center; justify-items: center; overflow: hidden; padding: clamp(34px,6vw,76px); border: 1px dashed rgba(126,151,213,.32); border-radius: 28px; background: radial-gradient(circle at 50% 24%,rgba(52,92,231,.18),transparent 43%),rgba(13,22,48,.52); text-align: center; } .section-gate::after { content: ''; position: absolute; width: 330px; aspect-ratio: 1; bottom: -260px; border: 1px solid rgba(100,130,207,.16); border-radius: 50%; } .section-gate__mark { display: grid; width: 58px; height: 58px; place-items: center; margin-bottom: 18px; border: 1px solid rgba(91,123,255,.46); border-radius: 18px; color: #84a0ff; background: rgba(38,68,165,.22); font-size: 34px; font-weight: 300; line-height: 1; box-shadow: 0 16px 45px rgba(20,55,181,.18); } .section-gate h1 { max-width: 680px; margin: 12px 0 16px; font-size: clamp(31px,4vw,52px); line-height: 1.02; letter-spacing: -.06em; } .section-gate > p:not(.kicker) { max-width: 590px; margin: 0 0 28px; color: #aeb9d4; font-size: 16px; line-height: 1.6; } .section-gate .primary-button { min-width: 220px; }
.auth-card { position: relative; z-index: 1; width: min(100%,870px); display: grid; grid-template-columns: minmax(250px,.9fr) minmax(280px,1fr); gap: clamp(30px,6vw,85px); margin: 12vh auto 0; padding: clamp(30px,5vw,66px); border: 1px solid rgba(144,160,204,.25); border-radius: 30px; background: linear-gradient(140deg,rgba(22,33,62,.97),rgba(10,15,34,.97)); box-shadow: 0 30px 90px rgba(0,0,0,.32); } .auth-card__intro h1 { margin: 12px 0 18px; white-space: pre-line; font-size: clamp(36px,4.3vw,65px); line-height: .95; letter-spacing: -.075em; } .auth-card__intro p:not(.kicker) { margin: 0; color: #aeb9d4; line-height: 1.55; }
.auth-form, .connection-form { display: grid; gap: 16px; align-content: center; } .auth-form label, .connection-form label { display: grid; gap: 7px; color: #c3cbe0; font-size: 13px; font-weight: 750; } .auth-form input, .connection-form input, .connection-form textarea { width: 100%; min-height: 52px; padding: 0 16px; border: 1px solid rgba(149,164,203,.28); border-radius: 13px; outline: none; color: #eef3ff; background: rgba(6,11,27,.66); transition: border-color .2s,box-shadow .2s; } .connection-form textarea { min-height: 105px; padding: 13px 16px; resize: vertical; } .auth-form input:focus, .connection-form input:focus, .connection-form textarea:focus { border-color: var(--brand-blue-bright); box-shadow: 0 0 0 4px var(--brand-blue-soft); } .form-error, .form-success { margin: 0 0 14px; font-size: 13px; } .form-error { color: #ffaaa8; } .form-success { color: var(--success); } .auth-card__footer { grid-column: 2; display: flex; gap: 8px; color: #9eaac5; font-size: 13px; } .auth-card__footer button { padding: 0; border: 0; color: #7894ff; background: transparent; font-weight: 750; }
.modal-backdrop { position: fixed; z-index: 5; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(2,6,19,.7); backdrop-filter: blur(9px); } .connection-modal { position: relative; width: min(100%,670px); max-height: calc(100vh - 48px); overflow: auto; padding: clamp(28px,4vw,46px); border: 1px solid rgba(146,164,205,.3); border-radius: 26px; background: linear-gradient(145deg,#14203d,#0b1128); box-shadow: 0 30px 100px rgba(0,0,0,.45); } .modal-close { position: absolute; top: 18px; right: 22px; padding: 0; border: 0; color: #bac4dc; background: transparent; font-size: 38px; line-height: 1; }
.provider-picker { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 26px 0; } .provider-picker button { display: flex; align-items: center; gap: 10px; padding: 12px; border: 1px solid rgba(149,164,203,.28); border-radius: 14px; color: #dce5f9; background: rgba(31,40,70,.72); font-weight: 800; } .provider-picker button.active { border-color: var(--brand-blue-bright); background: linear-gradient(115deg,rgba(23,72,220,.38),rgba(75,115,255,.22)); } .provider-picker .market-mark { width: 35px; height: 35px; border-radius: 10px; }
.yandex-discovery { display: grid; gap: 13px; } .secondary-button { min-height: 48px; padding: 0 16px; } .store-choice { display: grid; gap: 8px; } .store-choice > span { color: #b7c2da; font-size: 13px; font-weight: 750; } .store-choice button { display: flex; justify-content: space-between; padding: 12px; border: 1px solid rgba(149,164,203,.25); border-radius: 12px; color: #edf1ff; background: rgba(24,34,61,.7); text-align: left; } .store-choice button.active { border-color: var(--brand-blue-bright); background: var(--brand-blue-soft); } .store-choice small { color: #aeb9d4; } .connection-form__actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 9px; } .connection-form__actions .primary-button { min-width: 190px; }
.snapshot-view { display: grid; gap: 18px; margin-top: 46px; } .snapshot-toolbar { display: grid; grid-template-columns: minmax(260px, 1.2fr) minmax(0, 1.6fr) auto; align-items: end; gap: 14px; } .snapshot-search { display: grid; gap: 7px; } .snapshot-search label, .orders-filter-row label > span { color: #c3cbe0; font-size: 12px; font-weight: 850; letter-spacing: .06em; text-transform: uppercase; } .snapshot-search input, .orders-filter-row input, .orders-filter-row select { width: 100%; min-height: 50px; padding: 0 15px; border: 1px solid rgba(149,164,203,.28); border-radius: 13px; outline: none; color: #eef3ff; background: rgba(6,11,27,.66); } .orders-filter-row input, .orders-filter-row select { height: 52px; min-height: 52px; } .store-filters { display: flex; min-width: 0; gap: 8px; overflow-x: auto; padding-bottom: 1px; } .store-filters button { display: inline-flex; min-height: 50px; align-items: center; gap: 8px; flex: 0 0 auto; padding: 0 13px; border: 1px solid rgba(149,164,203,.28); border-radius: 13px; color: #cbd5eb; background: rgba(31,40,70,.72); font-weight: 800; } .store-filters button.active { color: #fff; border-color: rgba(75,115,255,.68); background: linear-gradient(115deg, var(--brand-blue), var(--brand-blue-bright)); } .store-filters .market-mark { width: 25px; height: 25px; border-radius: 8px; font-size: 8px; } .store-filters .market-mark--yandex_market { font-size: 16px; } .sync-button { display: inline-flex; min-width: 142px; min-height: 52px; align-items: center; justify-content: center; gap: 9px; padding: 0 17px; border: 1px solid #ee6cb5; border-radius: 50px; color: #fff; background: linear-gradient(140deg,#f13b9e,#cf206e); box-shadow: 0 12px 28px rgba(242,52,152,.27); font-weight: 850; } .sync-button > span:first-child { font-size: 25px; line-height: 1; } .spinning { animation: snapshot-spin .8s linear infinite; } @keyframes snapshot-spin { to { transform: rotate(360deg); } }
.snapshot-search__row { display: grid; grid-template-columns: minmax(0,1fr) auto auto; gap: 8px; } .search-submit, .filter-toggle { display: inline-flex; min-height: 50px; align-items: center; justify-content: center; gap: 8px; padding: 0 14px; border-radius: 13px; font-weight: 800; } .search-submit { border: 1px solid rgba(75,115,255,.76); color: #fff; background: linear-gradient(135deg,var(--brand-blue),var(--brand-blue-bright)); box-shadow: 0 10px 24px rgba(32,77,220,.22); } .search-submit svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; } .search-submit:hover { filter: brightness(1.08); } .search-action-enter-active, .search-action-leave-active { transition: opacity .15s ease, transform .15s ease; } .search-action-enter-from, .search-action-leave-to { opacity: 0; transform: translateX(-5px); } .filter-toggle { border: 1px solid rgba(149,164,203,.28); color: #cbd5eb; background: rgba(31,40,70,.72); } .filter-toggle svg { width: 18px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; } .filter-toggle small { display: grid; min-width: 20px; height: 20px; place-items: center; border-radius: 50%; color: #fff; background: var(--brand-blue-bright); font-size: 11px; } .filter-toggle--active { border-color: rgba(91,123,255,.64); color: #fff; background: rgba(42,67,145,.42); }
.snapshot-sync-overlay { position: fixed; z-index: 4; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(5,9,23,.66); backdrop-filter: blur(7px); } .snapshot-sync-overlay::before { content: ''; position: absolute; width: min(520px,72vw); aspect-ratio: 1; border-radius: 50%; background: radial-gradient(circle,rgba(45,83,219,.2),transparent 68%); pointer-events: none; } .snapshot-sync-overlay__card { position: relative; padding: 28px 38px 25px; border: 1px solid rgba(139,160,210,.28); border-radius: 26px; background: linear-gradient(145deg,rgba(21,33,63,.97),rgba(10,16,37,.97)); box-shadow: 0 28px 90px rgba(0,0,0,.42),0 0 55px rgba(40,79,219,.12); } .snapshot-loader-enter-active, .snapshot-loader-leave-active { transition: opacity .2s ease; } .snapshot-loader-enter-from, .snapshot-loader-leave-to { opacity: 0; }
.orders-filter-row { display: flex; flex-wrap: wrap; align-items: end; gap: 18px; padding: 16px 18px; border: 1px solid rgba(144,160,204,.17); border-radius: 18px; background: rgba(14,22,48,.52); box-shadow: inset 0 1px rgba(255,255,255,.02); } .orders-filter-row__period { position: relative; display: flex; align-items: end; gap: 9px; } .orders-filter-row__period > span:first-child, .orders-filter-row label > span { color: #c3cbe0; font-size: 12px; font-weight: 850; letter-spacing: .06em; text-transform: uppercase; } .orders-filter-row__period > span:first-child { position: absolute; top: 0; left: 0; } .orders-filter-row__period label { padding-top: 19px; } .orders-filter-row label { display: grid; gap: 7px; min-width: 150px; } .orders-filter-row .status-select { position: relative; min-width: 175px; } .orders-filter-row .status-select::after { content: ''; position: absolute; right: 17px; bottom: 22px; width: 7px; height: 7px; border-right: 2px solid #8794b2; border-bottom: 2px solid #8794b2; transform: rotate(45deg); pointer-events: none; } .orders-filter-row select { padding-right: 42px; appearance: none; -webkit-appearance: none; } .date-divider { padding-bottom: 17px; color: #71809f; } .orders-filter-row__actions { display: flex; align-items: flex-end; gap: 9px; } .filter-apply, .filter-reset { height: 52px; min-height: 52px; } .filter-reset { display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 0 16px; border: 1px solid rgba(255,150,155,.34); border-radius: 14px; color: #ffaaa8; background: rgba(255,150,155,.07); font-weight: 800; transition: border-color .2s, background .2s, transform .2s; } .filter-reset svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; } .filter-reset:hover { border-color: rgba(255,170,168,.62); background: rgba(255,150,155,.13); transform: translateY(-1px); } .order-filters-enter-active, .order-filters-leave-active { overflow: hidden; transition: opacity .18s ease,transform .18s ease; } .order-filters-enter-from, .order-filters-leave-to { opacity: 0; transform: translateY(-6px); } .snapshot-count { margin: 3px 0 0; color: #9eabc7; font-size: 13px; font-weight: 750; }
.snapshot-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 16px; } .snapshot-card { min-height: 152px; display: grid; gap: 15px; padding: 21px; border: 1px solid rgba(139,160,210,.25); border-radius: 22px; background: linear-gradient(145deg,rgba(27,43,81,.96),rgba(15,24,54,.98)); box-shadow: inset 0 1px rgba(255,255,255,.025); } .snapshot-card__head { display: flex; min-width: 0; align-items: center; gap: 13px; } .snapshot-card__head > div { min-width: 0; flex: 1 1 auto; } .snapshot-card h2 { overflow: hidden; margin: 0; color: #f6f8ff; font-size: 17px; line-height: 1.25; letter-spacing: -.03em; text-overflow: ellipsis; white-space: nowrap; } .snapshot-card p { overflow: hidden; margin: 5px 0 0; color: #b8c3dd; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; } .catalog-card { cursor: pointer; transition: border-color .18s,box-shadow .18s,transform .18s; } .catalog-card:hover,.catalog-card:focus-visible { border-color: rgba(91,123,255,.58); box-shadow: inset 0 1px rgba(255,255,255,.04),0 14px 34px rgba(6,16,48,.26); transform: translateY(-2px); outline: none; } .catalog-card:focus-visible { box-shadow: inset 0 1px rgba(255,255,255,.04),0 0 0 3px rgba(75,115,255,.22),0 14px 34px rgba(6,16,48,.26); } .catalog-card__open { display: grid; width: 39px; height: 39px; place-items: center; flex: 0 0 auto; padding: 0; border: 1px solid rgba(126,151,217,.3); border-radius: 12px; color: #9cadd5; background: rgba(17,28,57,.72); transition: color .18s,border-color .18s,background .18s,transform .18s; } .catalog-card__open svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; } .catalog-card:hover .catalog-card__open,.catalog-card:focus-visible .catalog-card__open { color: #fff; border-color: rgba(91,123,255,.72); background: rgba(49,80,186,.48); transform: translateY(-1px); } .snapshot-card__footer { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding-top: 14px; border-top: 1px dashed rgba(164,182,224,.24); color: #bfc9df; font-size: 13px; } .snapshot-card__facts { display: flex; min-width: 0; align-items: center; gap: 18px; } .catalog-card__stock { color: #9fdccb; white-space: nowrap; } .catalog-card__stock strong { color: #58e5bd; } .snapshot-card__footer strong { color: #f2f5ff; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; } .snapshot-card__footer time { flex: 0 0 auto; color: #aeb9d4; font-size: 12px; } .order-card { min-height: 171px; } .order-card__body { min-width: 0; } .order-card__body > strong { display: -webkit-box; overflow: hidden; color: #eef2fc; line-height: 1.42; white-space: normal; -webkit-box-orient: vertical; -webkit-line-clamp: 2; } .order-status { flex: 0 0 auto; max-width: 120px; padding: 6px 9px; border: 1px solid currentColor; border-radius: 999px; font-size: 10px; font-weight: 900; letter-spacing: .035em; text-align: center; text-transform: uppercase; white-space: nowrap; } .order-status--processing { color: #ffc75a; background: rgba(255,199,90,.08); } .order-status--in_delivery { color: #65b5ff; background: rgba(101,181,255,.08); } .order-status--delivered { color: #4ee6bd; background: rgba(78,230,189,.08); } .order-status--cancelled, .order-status--problem { color: #ff969b; background: rgba(255,150,155,.08); } .pagination { display: flex; align-items: center; justify-content: center; gap: 16px; padding-top: 7px; color: #b7c2da; font-size: 14px; } .pagination button { min-height: 42px; padding: 0 14px; border: 1px solid rgba(149,164,203,.28); border-radius: 12px; color: #dce5f9; background: rgba(31,40,70,.72); font-weight: 750; } .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; }
@media (max-width:900px) { .connection-grid { grid-template-columns: repeat(2,minmax(255px,1fr)); } .dashboard-heading { align-items: start; flex-direction: column; } .snapshot-toolbar { grid-template-columns: minmax(0,1fr) auto; } .snapshot-search { grid-column: 1 / -1; } } @media (max-width:660px) { .app-shell { padding: 16px 16px 44px; } .app-version { right: 16px; bottom: 11px; font-size: 9px; } .app-header { min-height: auto; padding: 14px; border-radius: 19px; } .app-brand img { width: 147px; } .app-brand span { display: none; } .profile-button { max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } .session-loader { margin-top: 18vh; } .seller-dashboard { margin-top: 26px; } .seller-nav { width: 100%; gap: 4px; } .seller-nav__item { flex: 1; padding: 0 9px; font-size: 13px; } .seller-nav small { display: none; } .dashboard-heading { margin: 30px 0 22px; } .dashboard-heading h1 { font-size: 40px; } .connection-grid, .snapshot-grid { grid-template-columns: 1fr; } .connection-card, .connection-add-card { min-height: 265px; } .snapshot-toolbar { grid-template-columns: 1fr; } .snapshot-search__row { grid-template-columns: 1fr auto; } .snapshot-search__row > input { grid-column: 1 / -1; } .filter-toggle, .sync-button { justify-self: start; } .orders-filter-row__period { flex-wrap: wrap; } .orders-filter-row__actions { width: 100%; } .auth-card { grid-template-columns: 1fr; margin-top: 58px; padding: 32px 25px; border-radius: 23px; } .auth-card__footer { grid-column: 1; flex-wrap: wrap; } .auth-card__intro h1 { font-size: 45px; } .provider-picker { grid-template-columns: 1fr; } .connection-form__actions { flex-direction: column-reverse; } .connection-form__actions .primary-button { width: 100%; } }
</style>
