const ACTIVE_SYNC_STATUSES = new Set(['queued', 'running'])

export function isSyncJobActive(job) {
  return ACTIVE_SYNC_STATUSES.has(job?.status)
}

export function syncActivityState(jobs, monitorError = '') {
  if (monitorError) return 'failed'
  if (jobs.some(isSyncJobActive)) return 'running'
  if (jobs.some((job) => job.status === 'failed')) return 'failed'
  if (jobs.length && jobs.every((job) => job.status === 'succeeded')) return 'succeeded'
  return 'idle'
}

function syncKind(jobs) {
  const kinds = new Set(jobs.map((job) => job.sync_kind).filter(Boolean))
  if (kinds.size !== 1) return 'data'
  return kinds.has('catalog') ? 'catalog' : 'orders'
}

export function syncActivityTitle(jobs, state) {
  const kind = syncKind(jobs)
  const labels = {
    running: {
      catalog: 'Обновляем каталог',
      orders: 'Обновляем заказы',
      data: 'Обновляем данные',
    },
    succeeded: {
      catalog: 'Каталог обновлён',
      orders: 'Заказы обновлены',
      data: 'Данные обновлены',
    },
    failed: {
      catalog: 'Не удалось обновить каталог',
      orders: 'Не удалось обновить заказы',
      data: 'Обновление завершилось с ошибкой',
    },
  }
  return labels[state]?.[kind] || 'Фоновое обновление'
}

export function syncActivityDetail(jobs, state, monitorError = '') {
  if (monitorError) return monitorError

  if (state === 'running') {
    const activeJobs = jobs.filter(isSyncJobActive)
    if (activeJobs.length === 1 && jobs.length === 1) {
      const job = activeJobs[0]
      return `${job.store_name || 'Магазин'} · ${job.status === 'queued' ? 'ожидает запуска' : 'получает свежие данные'}`
    }
    const completedCount = jobs.filter((job) => !isSyncJobActive(job)).length
    return `Завершено ${completedCount} из ${jobs.length} магазинов. Можно продолжать работу.`
  }

  if (state === 'failed') {
    const failedJob = jobs.find((job) => job.status === 'failed')
    if (!failedJob) return 'Не удалось получить состояние задания. Попробуйте обновить данные ещё раз.'
    const prefix = failedJob.store_name ? `${failedJob.store_name}: ` : ''
    return `${prefix}${failedJob.error || 'маркетплейс не отдал свежие данные'}`
  }

  if (state === 'succeeded') {
    const itemCount = jobs.reduce((total, job) => total + (Number(job.synced_items) || 0), 0)
    const storeCount = new Set(jobs.map((job) => job.connection_id).filter(Boolean)).size || jobs.length
    const storesLabel = storeCount === 1 ? 'магазин' : storeCount < 5 ? 'магазина' : 'магазинов'
    return `Обработано позиций: ${itemCount}. Обновлено: ${storeCount} ${storesLabel}.`
  }

  return ''
}
