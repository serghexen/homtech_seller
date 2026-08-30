import assert from 'node:assert/strict'
import test from 'node:test'

import { shouldShowDashboardSkeleton } from '../src/utils/dashboard.js'

test('dashboard keeps existing cards visible during a silent refresh', () => {
  assert.equal(shouldShowDashboardSkeleton(true, 0), true)
  assert.equal(shouldShowDashboardSkeleton(true, 5), false)
  assert.equal(shouldShowDashboardSkeleton(false, 0), false)
})
