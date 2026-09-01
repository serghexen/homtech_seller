import assert from 'node:assert/strict'
import test from 'node:test'

import { dashboardChatCount, shouldShowDashboardSkeleton } from '../src/utils/dashboard.js'

test('dashboard keeps existing cards visible during a silent refresh', () => {
  assert.equal(shouldShowDashboardSkeleton(true, 0), true)
  assert.equal(shouldShowDashboardSkeleton(true, 5), false)
  assert.equal(shouldShowDashboardSkeleton(false, 0), false)
})

test('dashboard marks a bounded Yandex chat count without hiding missing data', () => {
  assert.equal(dashboardChatCount({ pending_chats: 100, pending_chats_capped: true }), '99+')
  assert.equal(dashboardChatCount({ pending_chats: 17, pending_chats_capped: false }), '17')
  assert.equal(dashboardChatCount({ pending_chats: null, pending_chats_capped: false }), '—')
})
