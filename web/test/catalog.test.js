import assert from 'node:assert/strict'
import test from 'node:test'
import { catalogEmptyStateMessage } from '../src/utils/catalog.js'
import { LIVE_SEARCH_DELAY_MS, liveSearchDelay } from '../src/utils/search.js'

test('live search starts with the first non-empty character', () => {
  assert.equal(liveSearchDelay('м'), LIVE_SEARCH_DELAY_MS)
  assert.equal(liveSearchDelay('  м  '), LIVE_SEARCH_DELAY_MS)
  assert.equal(liveSearchDelay(''), 0)
  assert.equal(liveSearchDelay('   '), 0)
})

test('catalog empty state distinguishes search results from an empty catalog', () => {
  assert.match(catalogEmptyStateMessage({ query: 'oo', state: 'active' }), /По запросу «oo» карточки не найдены/)
  assert.equal(catalogEmptyStateMessage({ state: 'archived' }), 'В архиве пока нет карточек.')
  assert.match(catalogEmptyStateMessage(), /Каталог пока пуст/)
})
