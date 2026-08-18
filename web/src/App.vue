<script setup>
import { onMounted, reactive, ref } from 'vue'
import { apiRequest } from './api'
import homtechLogo from './assets/homtech-logo.png'

const mode = ref('login')
const loading = ref(false)
const error = ref('')
const user = ref(null)
const form = reactive({
  email: '',
  password: '',
  display_name: '',
  workspace_name: '',
})

function switchMode(nextMode) {
  // Переключает сценарий входа без потери введённого email и показывает только нужные поля.
  mode.value = nextMode
  error.value = ''
}

async function submit() {
  // Отправляет регистрацию или вход и сохраняет в памяти только безопасный профиль, а не токен сессии.
  loading.value = true
  error.value = ''
  try {
    const path = mode.value === 'register' ? '/auth/register' : '/auth/login'
    const body = mode.value === 'register'
      ? { ...form }
      : { email: form.email, password: form.password }
    const result = await apiRequest(path, { method: 'POST', body: JSON.stringify(body) })
    user.value = result.user
    form.password = ''
  } catch (requestError) {
    error.value = requestError.message || 'Не удалось войти'
  } finally {
    loading.value = false
  }
}

async function logout() {
  // Завершает локальную сессию Seller и возвращает пользователя на защищённый экран входа.
  await apiRequest('/auth/logout', { method: 'POST' }).catch(() => null)
  user.value = null
  form.password = ''
  mode.value = 'login'
}

onMounted(async () => {
  // Восстанавливает сессию после обновления страницы, не читая HttpOnly cookie из JavaScript.
  try {
    const result = await apiRequest('/auth/me')
    user.value = result.user
  } catch {
    user.value = null
  }
})
</script>

<template>
  <main class="auth-shell">
    <div class="auth-shell__glow auth-shell__glow--left"></div>
    <div class="auth-shell__glow auth-shell__glow--right"></div>

    <header class="auth-brand">
      <img :src="homtechLogo" alt="HomTech" />
      <span class="auth-brand__subtitle">API Seller</span>
    </header>

    <section v-if="user" class="welcome-card" aria-live="polite">
      <p class="auth-kicker">АККАУНТ ГОТОВ</p>
      <h1>Добро пожаловать,<br /><em>{{ user.display_name || user.email }}</em></h1>
      <p class="welcome-card__workspace">Рабочая область: <strong>{{ user.workspace_name }}</strong></p>
      <div class="welcome-card__rule"></div>
      <p class="welcome-card__hint">Следующим шагом здесь появятся подключение магазинов, единый каталог и заказы.</p>
      <button class="auth-button auth-button--ghost" type="button" @click="logout">Выйти из аккаунта</button>
    </section>

    <section v-else class="auth-card">
      <div class="auth-card__intro">
        <p class="auth-kicker">{{ mode === 'register' ? 'НОВАЯ ОРГАНИЗАЦИЯ' : 'ЛИЧНЫЙ КАБИНЕТ' }}</p>
        <h1>{{ mode === 'register' ? 'Начните продавать\nцифровые товары.' : 'Войдите\nв Seller.' }}</h1>
        <p>{{ mode === 'register' ? 'Создадим организацию — к ней будут привязаны магазины и будущий тариф.' : 'Используйте отдельный аккаунт HomTech Seller.' }}</p>
      </div>

      <form class="auth-form" @submit.prevent="submit">
        <label v-if="mode === 'register'">
          <span>Ваше имя</span>
          <input v-model.trim="form.display_name" autocomplete="name" maxlength="120" placeholder="Например, Сергей" />
        </label>
        <label v-if="mode === 'register'">
          <span>Название организации</span>
          <input v-model.trim="form.workspace_name" required autocomplete="organization" maxlength="160" placeholder="Например, ASAT Games" />
        </label>
        <label>
          <span>Email</span>
          <input v-model.trim="form.email" required type="email" autocomplete="email" placeholder="you@company.ru" />
        </label>
        <label>
          <span>Пароль</span>
          <input v-model="form.password" required type="password" :minlength="mode === 'register' ? 10 : 1" autocomplete="current-password" placeholder="Не менее 10 символов" />
        </label>
        <p v-if="error" class="auth-form__error">{{ error }}</p>
        <button class="auth-button" type="submit" :disabled="loading">{{ loading ? 'Проверяем…' : mode === 'register' ? 'Создать организацию' : 'Войти' }}</button>
      </form>

      <footer class="auth-card__footer">
        <span>{{ mode === 'register' ? 'Уже есть аккаунт?' : 'Первый раз в Seller?' }}</span>
        <button type="button" @click="switchMode(mode === 'register' ? 'login' : 'register')">{{ mode === 'register' ? 'Войти' : 'Создать организацию' }}</button>
      </footer>
    </section>
  </main>
</template>

<style>
:root { color-scheme: dark; font-family: "Avenir Next", "Segoe UI", sans-serif; background: #080d20; color: #edf1ff; }
* { box-sizing: border-box; }
body { min-width: 320px; min-height: 100vh; margin: 0; }
button, input { font: inherit; }
button { cursor: pointer; }
.auth-shell { position: relative; display: grid; min-height: 100vh; padding: clamp(24px, 5vw, 76px); overflow: hidden; background: radial-gradient(circle at 0 38%, rgba(42, 230, 191, .18), transparent 30%), radial-gradient(circle at 100% 26%, rgba(241, 152, 87, .14), transparent 34%), #080d20; }
.auth-shell__glow { position: absolute; width: 44vw; aspect-ratio: 1; border: 1px solid rgba(114, 135, 185, .18); border-radius: 50%; pointer-events: none; }
.auth-shell__glow--left { bottom: -27vw; left: -17vw; }
.auth-shell__glow--right { top: -34vw; right: -11vw; }
.auth-brand { position: relative; z-index: 1; display: flex; align-items: center; gap: clamp(10px, 1.3vw, 18px); align-self: start; }
.auth-brand img { display: block; width: clamp(164px, 17vw, 252px); height: auto; max-height: 56px; object-fit: contain; }
.auth-brand__subtitle { padding-left: clamp(10px, 1.3vw, 18px); border-left: 1px solid rgba(144, 160, 204, .33); color: #aeb9d4; font-size: clamp(13px, 1.25vw, 17px); font-weight: 720; letter-spacing: -.025em; }
.auth-card, .welcome-card { position: relative; z-index: 1; width: min(100%, 870px); align-self: center; justify-self: center; display: grid; grid-template-columns: minmax(250px, .9fr) minmax(280px, 1fr); gap: clamp(30px, 6vw, 85px); padding: clamp(30px, 5vw, 66px); border: 1px solid rgba(144, 160, 204, .25); border-radius: 30px; background: linear-gradient(140deg, rgba(22, 33, 62, .97), rgba(10, 15, 34, .97)); box-shadow: 0 30px 90px rgba(0, 0, 0, .32); }
.auth-card__intro h1, .welcome-card h1 { margin: 12px 0 18px; white-space: pre-line; font-size: clamp(36px, 4.3vw, 65px); line-height: .95; letter-spacing: -.075em; }
.auth-card__intro h1 em, .welcome-card h1 em { color: #50e6c1; font-family: Georgia, serif; font-weight: 600; }
.auth-card__intro p, .welcome-card__hint { max-width: 330px; margin: 0; color: #aeb9d4; line-height: 1.55; }
.auth-kicker { margin: 0; color: #4fe3bf; font-size: 12px; font-weight: 800; letter-spacing: .14em; }
.auth-form { display: grid; gap: 18px; align-content: center; }
.auth-form label { display: grid; gap: 7px; color: #c3cbe0; font-size: 13px; font-weight: 750; }
.auth-form input { width: 100%; height: 52px; padding: 0 16px; border: 1px solid rgba(149, 164, 203, .28); border-radius: 13px; outline: none; color: #eef3ff; background: rgba(6, 11, 27, .66); transition: border-color .2s, box-shadow .2s; }
.auth-form input:focus { border-color: #57e6c4; box-shadow: 0 0 0 4px rgba(80, 230, 193, .12); }
.auth-form__error { margin: -2px 0 0; color: #ffaaa8; font-size: 13px; }
.auth-button { min-height: 54px; margin-top: 5px; border: 0; border-radius: 14px; color: #06111e; background: linear-gradient(135deg, #48e3bd, #73e7c8); font-weight: 850; box-shadow: 0 13px 32px rgba(62, 230, 189, .18); transition: transform .2s, filter .2s; }
.auth-button:hover:not(:disabled) { transform: translateY(-2px); filter: brightness(1.06); }
.auth-button:disabled { cursor: wait; opacity: .65; }
.auth-card__footer { grid-column: 2; display: flex; gap: 8px; color: #9eaac5; font-size: 13px; }
.auth-card__footer button { padding: 0; border: 0; color: #54e6c2; background: transparent; font-weight: 750; }
.welcome-card { display: block; width: min(100%, 610px); }
.welcome-card__workspace { margin: 29px 0 24px; color: #b6c1db; }
.welcome-card__workspace strong { color: #f2f5ff; }
.welcome-card__rule { height: 1px; margin-bottom: 24px; background: rgba(147, 164, 207, .25); }
.welcome-card .auth-button { width: 100%; margin-top: 30px; }
.auth-button--ghost { border: 1px solid rgba(145, 162, 205, .34); color: #d7def0; background: transparent; box-shadow: none; }
@media (max-width: 720px) { .auth-shell { padding: 22px; } .auth-brand img { width: 160px; } .auth-brand__subtitle { font-size: 13px; } .auth-card { grid-template-columns: 1fr; padding: 32px 25px; border-radius: 23px; } .auth-card__footer { grid-column: 1; flex-wrap: wrap; } .auth-card__intro h1 { font-size: 45px; } }
</style>
