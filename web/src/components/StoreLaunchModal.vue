<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  connection: { type: Object, required: true },
  readiness: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  launching: { type: Boolean, default: false },
  error: { type: String, default: '' },
})
const emit = defineEmits(['close', 'retry', 'refresh', 'launch'])

const exclusiveConfirmed = ref(false)
const automaticStockEnabled = ref(false)
const blockedCount = computed(() => props.readiness?.checks?.filter((item) => item.state === 'blocked').length || 0)

watch(() => props.readiness, (value) => {
  automaticStockEnabled.value = Boolean(value?.automatic_stock_enabled)
}, { immediate: true })

function submit() {
  emit('launch', {
    confirm_exclusive_control: exclusiveConfirmed.value,
    automatic_stock_enabled: automaticStockEnabled.value,
  })
}
</script>

<template>
  <div class="launch-backdrop" @click.self="emit('close')">
    <section class="launch-modal" role="dialog" aria-modal="true" aria-labelledby="store-launch-title">
      <header class="launch-modal__head">
        <div>
          <span>ЗАПУСК МАГАЗИНА</span>
          <h1 id="store-launch-title">{{ connection.display_name }}</h1>
          <p>Одна проверка включает приём заказов, цепочку выдачи и отправку результата.</p>
        </div>
        <button type="button" aria-label="Закрыть" @click="emit('close')">×</button>
      </header>

      <div v-if="loading" class="launch-loading">
        <span></span><strong>Проверяем готовность магазина…</strong>
      </div>

      <template v-else-if="readiness">
        <div class="launch-flow" aria-label="Схема обработки заказа">
          <div><small>01</small><strong>Получить заказ</strong><span>Webhook или синхронизация</span></div>
          <i>→</i>
          <div class="launch-flow__chain"><small>02</small><strong>{{ readiness.chain.join(' → ') }}</strong><span>По тарифу {{ readiness.plan_name }}</span></div>
          <i>→</i>
          <div><small>03</small><strong>Отправить</strong><span>Только готовый комплект</span></div>
        </div>

        <section class="launch-checks">
          <div class="launch-checks__title">
            <div><span>ПРОВЕРКА ГОТОВНОСТИ</span><h2>{{ blockedCount ? `Осталось исправить: ${blockedCount}` : 'Можно запускать' }}</h2></div>
            <span class="launch-plan">{{ readiness.plan_name }}</span>
          </div>
          <ul>
            <li v-for="item in readiness.checks" :key="item.code" :class="`launch-check--${item.state}`">
              <span class="launch-check__mark">{{ item.state === 'ready' ? '✓' : item.state === 'warning' ? '!' : '×' }}</span>
              <div><strong>{{ item.title }}</strong><p>{{ item.detail }}</p></div>
            </li>
          </ul>
        </section>

        <section class="launch-options">
          <label>
            <input v-model="automaticStockEnabled" type="checkbox" />
            <span><strong>Автоматически обновлять остаток</strong><small>После успешной выдачи Seller опубликует рассчитанный остаток.</small></span>
          </label>
          <label class="launch-confirm">
            <input v-model="exclusiveConfirmed" type="checkbox" />
            <span><strong>Другой сервис больше не выдаёт заказы этого магазина</strong><small>Это защищает от двойной выдачи при переносе из CRM.</small></span>
          </label>
        </section>
      </template>

      <p v-if="error" class="launch-error">{{ error }}</p>
      <footer>
        <button class="launch-secondary" type="button" @click="emit('close')">Пока не запускать</button>
        <button v-if="error && !readiness" class="launch-secondary" type="button" @click="emit('retry')">Проверить ещё раз</button>
        <button v-if="readiness && blockedCount" class="launch-secondary launch-refresh" type="button" :disabled="loading" @click="emit('refresh')">Обновить данные</button>
        <button
          class="launch-primary"
          type="button"
          :disabled="loading || launching || !readiness?.can_launch || !exclusiveConfirmed"
          @click="submit"
        >
          {{ launching ? 'Запускаем…' : 'Запустить выдачу' }}
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.launch-backdrop{position:fixed;z-index:20;inset:0;display:grid;place-items:center;padding:24px;background:rgba(2,6,19,.76);backdrop-filter:blur(10px)}
.launch-modal{width:min(100%,980px);max-height:calc(100vh - 48px);overflow:auto;border:1px solid rgba(118,145,219,.34);border-radius:30px;color:#eef2ff;background:radial-gradient(circle at 92% 0,rgba(55,92,216,.22),transparent 33%),linear-gradient(145deg,#14213f,#0a1129 72%);box-shadow:0 35px 120px rgba(0,0,0,.5)}
.launch-modal__head{display:flex;justify-content:space-between;gap:24px;padding:34px 38px 29px;border-bottom:1px solid rgba(139,160,210,.2)}
.launch-modal__head span,.launch-checks__title span{color:#7898ff;font-size:10px;font-weight:900;letter-spacing:.15em}.launch-modal__head h1{margin:7px 0 6px;font-size:clamp(28px,4vw,48px);letter-spacing:-.055em}.launch-modal__head p{margin:0;color:#aeb9d4;line-height:1.5}.launch-modal__head button{width:48px;height:48px;padding:0;border:1px solid rgba(149,164,203,.28);border-radius:15px;color:#bdc8e0;background:rgba(20,31,61,.7);font-size:30px}
.launch-loading{display:flex;align-items:center;gap:14px;margin:34px 38px;padding:28px;border:1px solid rgba(91,123,255,.35);border-radius:20px;background:rgba(28,48,105,.24)}.launch-loading span{width:18px;height:18px;border:2px solid rgba(123,153,255,.25);border-top-color:#7697ff;border-radius:50%;animation:spin .7s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
.launch-flow{display:grid;grid-template-columns:minmax(140px,.8fr) auto minmax(280px,1.5fr) auto minmax(140px,.8fr);align-items:stretch;gap:11px;padding:28px 38px 0}.launch-flow>div{display:grid;align-content:center;gap:5px;min-height:105px;padding:17px;border:1px solid rgba(118,145,219,.25);border-radius:18px;background:rgba(17,29,63,.72)}.launch-flow small{color:#6f91ff;font-weight:900}.launch-flow strong{font-size:15px}.launch-flow span{color:#9fabca;font-size:11px}.launch-flow i{align-self:center;color:#6275a6;font-style:normal}.launch-flow__chain{border-color:rgba(80,230,193,.36)!important;background:linear-gradient(145deg,rgba(20,73,78,.42),rgba(20,38,72,.72))!important}
.launch-checks{margin:22px 38px 0;padding:23px;border:1px solid rgba(130,151,204,.25);border-radius:22px;background:rgba(8,16,38,.48)}.launch-checks__title{display:flex;align-items:center;justify-content:space-between;gap:16px}.launch-checks__title h2{margin:4px 0 0;font-size:22px}.launch-plan{padding:7px 11px;border:1px solid rgba(80,230,193,.32);border-radius:999px;color:#71e5c6!important;background:rgba(80,230,193,.08)}ul{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:20px 0 0;padding:0;list-style:none}li{display:flex;gap:11px;min-height:74px;padding:13px;border:1px solid rgba(130,151,204,.2);border-radius:15px;background:rgba(23,35,68,.55)}.launch-check__mark{display:grid;width:27px;height:27px;place-items:center;flex:0 0 auto;border-radius:9px;color:#50e6c1;background:rgba(80,230,193,.1);font-weight:900}.launch-check--warning .launch-check__mark{color:#ffc75a;background:rgba(255,199,90,.1)}.launch-check--blocked .launch-check__mark{color:#ff9b9f;background:rgba(255,150,155,.1)}li strong{font-size:13px}li p{margin:4px 0 0;color:#9daac8;font-size:11px;line-height:1.45}
.launch-options{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:20px 38px 0}.launch-options label{display:flex;gap:12px;padding:17px;border:1px solid rgba(130,151,204,.24);border-radius:17px;background:rgba(20,31,62,.65);cursor:pointer}.launch-options input{width:18px;height:18px;flex:0 0 auto;accent-color:#4d73ff}.launch-options span{display:grid;gap:4px}.launch-options strong{font-size:13px}.launch-options small{color:#9daac8;line-height:1.45}.launch-confirm{border-color:rgba(80,230,193,.28)!important}
.launch-error{margin:16px 38px 0;padding:12px 15px;border:1px solid rgba(255,150,155,.3);border-radius:13px;color:#ffaaa8;background:rgba(255,150,155,.07);font-size:13px}.launch-modal footer{display:flex;justify-content:flex-end;gap:11px;margin-top:27px;padding:24px 38px 32px;border-top:1px solid rgba(139,160,210,.18)}.launch-modal footer button{min-height:50px;padding:0 18px;border-radius:14px;font-weight:800}.launch-secondary{border:1px solid rgba(149,164,203,.28);color:#c4cee3;background:rgba(31,40,70,.72)}.launch-refresh{color:#8ea7ff;border-color:rgba(92,126,235,.45)}.launch-primary{border:0;color:#fff;background:linear-gradient(135deg,#1748dc,#4b73ff);box-shadow:0 13px 32px rgba(32,77,220,.28)}button:disabled{cursor:not-allowed;opacity:.48}
@media(max-width:720px){.launch-backdrop{padding:10px}.launch-modal{max-height:calc(100vh - 20px);border-radius:22px}.launch-modal__head,.launch-flow,.launch-checks,.launch-options,.launch-error{margin-left:18px;margin-right:18px}.launch-modal__head{padding:24px 18px}.launch-flow{grid-template-columns:1fr;padding:20px 0 0}.launch-flow i{display:none}.launch-checks{padding:17px}.launch-checks ul,.launch-options{grid-template-columns:1fr}.launch-modal footer{padding:20px 18px;flex-direction:column-reverse}.launch-modal footer button{width:100%}}
</style>
