export function shouldShowDashboardSkeleton(loading, itemCount) {
  return Boolean(loading) && Number(itemCount || 0) === 0
}

export function dashboardChatCount(item) {
  if (item?.pending_chats === null || item?.pending_chats === undefined) return '—'
  return item.pending_chats_capped ? '99+' : String(item.pending_chats)
}
