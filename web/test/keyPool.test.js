import assert from 'node:assert/strict'
import test from 'node:test'

import { keyCountLabel, parseKeyLines } from '../src/utils/keyPool.js'

test('parseKeyLines removes blanks and duplicate keys without changing order', () => {
  assert.deepEqual(parseKeyLines(' ONE \n\nTWO\r\nONE\n THREE '), ['ONE', 'TWO', 'THREE'])
})

test('keyCountLabel uses Russian plural forms', () => {
  assert.equal(keyCountLabel(1), '1 ключ')
  assert.equal(keyCountLabel(3), '3 ключа')
  assert.equal(keyCountLabel(12), '12 ключей')
})
