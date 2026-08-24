import assert from 'node:assert/strict'
import test from 'node:test'

import { normalizeEscapedLineBreaks } from '../src/utils/text.js'

test('converts escaped CRM line breaks without changing existing line breaks', () => {
  assert.equal(
    normalizeEscapedLineBreaks('Шаг 1\\nШаг 2\\r\\nШаг 3\nШаг 4'),
    'Шаг 1\nШаг 2\nШаг 3\nШаг 4',
  )
})

test('keeps regular backslashes intact', () => {
  assert.equal(normalizeEscapedLineBreaks('Путь C:\\codes'), 'Путь C:\\codes')
})
