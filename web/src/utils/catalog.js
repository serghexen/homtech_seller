export function catalogEmptyStateMessage({ query = '', state = 'active' } = {}) {
  const search = String(query).trim()
  if (search) {
    return `По запросу «${search}» карточки не найдены. Попробуйте изменить запрос или очистить поиск.`
  }
  if (state === 'archived') return 'В архиве пока нет карточек.'
  return 'Каталог пока пуст. Нажмите «Обновить», чтобы загрузить товары из выбранного магазина.'
}
