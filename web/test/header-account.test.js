import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')

test('profile and logout are separate header controls', () => {
  const profileButton = appSource.match(/<button\s+class="profile-button"[\s\S]*?<\/button>/)?.[0] || ''
  const logoutButton = appSource.match(/<button class="logout-button"[\s\S]*?<\/button>/)?.[0] || ''

  assert.match(profileButton, /disabled/)
  assert.doesNotMatch(profileButton, /@click="logout"/)
  assert.match(logoutButton, /@click="logout"/)
  assert.match(logoutButton, />Выйти</)
})

test('header does not present a workspace-wide tariff', () => {
  assert.doesNotMatch(appSource, /class="plan-badge"/)
  assert.match(appSource, /Подписка · \{\{ item\.plan_name \}\}/)
  assert.match(appSource, /Тариф магазина/)
})
