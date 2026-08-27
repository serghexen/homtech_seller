export const CATALOG_SEARCH_DELAY_MS = 250

export function catalogSearchDelay(query) {
  return String(query ?? '').trim() ? CATALOG_SEARCH_DELAY_MS : 0
}

export function catalogEmptyStateMessage({ query = '', state = 'active' } = {}) {
  const search = String(query).trim()
  if (search) {
    return `По запросу «${search}» карточки не найдены. Попробуйте изменить запрос или очистить поиск.`
  }
  if (state === 'archived') return 'В архиве пока нет карточек.'
  return 'Каталог пока пуст. Нажмите «Обновить», чтобы загрузить товары из выбранного магазина.'
}
