export const LIVE_SEARCH_DELAY_MS = 250

export function liveSearchDelay(query) {
  return String(query ?? '').trim() ? LIVE_SEARCH_DELAY_MS : 0
}
