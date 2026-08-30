export function shouldShowDashboardSkeleton(loading, itemCount) {
  return Boolean(loading) && Number(itemCount || 0) === 0
}
