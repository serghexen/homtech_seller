import assert from 'node:assert/strict'
import test from 'node:test'
import {
  isSyncJobActive,
  syncActivityDetail,
  syncActivityState,
  syncActivityTitle,
} from '../src/utils/sync.js'

test('active sync jobs remain visible while queued or running', () => {
  assert.equal(isSyncJobActive({ status: 'queued' }), true)
  assert.equal(isSyncJobActive({ status: 'running' }), true)
  assert.equal(isSyncJobActive({ status: 'succeeded' }), false)
})

test('catalog sync presentation describes background progress', () => {
  const jobs = [
    { status: 'succeeded', sync_kind: 'catalog', connection_id: 'one', synced_items: 12 },
    { status: 'running', sync_kind: 'catalog', connection_id: 'two', store_name: 'JoyCards' },
  ]
  const state = syncActivityState(jobs)

  assert.equal(state, 'running')
  assert.equal(syncActivityTitle(jobs, state), 'Обновляем каталог')
  assert.match(syncActivityDetail(jobs, state), /Завершено 1 из 2/)
})

test('completed and failed sync jobs have distinct summaries', () => {
  const completed = [
    { status: 'succeeded', sync_kind: 'orders', connection_id: 'one', synced_items: 8 },
    { status: 'succeeded', sync_kind: 'orders', connection_id: 'two', synced_items: 5 },
  ]
  const failed = [{ status: 'failed', sync_kind: 'catalog', store_name: 'JoyCards', error: 'Ключ отозван' }]

  assert.equal(syncActivityTitle(completed, syncActivityState(completed)), 'Заказы обновлены')
  assert.match(syncActivityDetail(completed, 'succeeded'), /13/)
  assert.equal(syncActivityState(failed), 'failed')
  assert.match(syncActivityDetail(failed, 'failed'), /JoyCards: Ключ отозван/)
})
