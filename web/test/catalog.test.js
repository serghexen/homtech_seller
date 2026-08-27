import assert from 'node:assert/strict'
import test from 'node:test'
import {
  CATALOG_SEARCH_DELAY_MS,
  catalogEmptyStateMessage,
  catalogSearchDelay,
} from '../src/utils/catalog.js'

test('catalog live search starts with the first non-empty character', () => {
  assert.equal(catalogSearchDelay('м'), CATALOG_SEARCH_DELAY_MS)
  assert.equal(catalogSearchDelay('  м  '), CATALOG_SEARCH_DELAY_MS)
  assert.equal(catalogSearchDelay(''), 0)
  assert.equal(catalogSearchDelay('   '), 0)
})

test('catalog empty state distinguishes search results from an empty catalog', () => {
  assert.match(catalogEmptyStateMessage({ query: 'oo', state: 'active' }), /По запросу «oo» карточки не найдены/)
  assert.equal(catalogEmptyStateMessage({ state: 'archived' }), 'В архиве пока нет карточек.')
  assert.match(catalogEmptyStateMessage(), /Каталог пока пуст/)
})
