import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const modalSource = readFileSync(new URL('../src/components/BalanceTopupModal.vue', import.meta.url), 'utf8')

test('header exposes one workspace balance action without a store selector', () => {
  assert.match(appSource, /class="balance-topup-toggle"/)
  assert.match(appSource, /Пополнить общий баланс через СБП/)
  assert.doesNotMatch(modalSource, /selectedConnectionId|connection_id|Выберите магазин/)
})

test('topup submits kopecks and polls the workspace-scoped status endpoint', () => {
  assert.match(modalSource, /JSON\.stringify\(\{ amount: amountKopecks\.value \}\)/)
  assert.match(modalSource, /`\/billing\/topups\/\$\{topup\.value\.id\}`/)
  assert.match(modalSource, /result\.state === 'confirmed'/)
})

test('demo controls use the dedicated SBP test scenario', () => {
  assert.match(modalSource, /simulateDemo\('success'\)/)
  assert.match(modalSource, /simulateDemo\('rejected'\)/)
  assert.match(modalSource, /simulateDemo\('deadline_expired'\)/)
})
