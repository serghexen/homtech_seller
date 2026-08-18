const apiBase = import.meta.env.VITE_API_BASE || '/api'

export async function apiRequest(path, options = {}) {
  // Выполняет запрос с HttpOnly-сессией и одинаково обрабатывает ошибки API для форм входа.
  const response = await fetch(`${apiBase}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  if (response.status === 204) return null
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(payload?.detail || 'Не удалось выполнить запрос')
  }
  return payload
}
