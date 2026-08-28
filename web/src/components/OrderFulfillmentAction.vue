<script setup>
import { computed } from 'vue'

import { orderFulfillmentAction } from '../utils/orderFulfillment.js'

const props = defineProps({
  order: { type: Object, required: true },
  fallbackProviderCode: { type: String, default: '' },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['open'])
const action = computed(() => orderFulfillmentAction(props.order, props.fallbackProviderCode))
const automaticTitle = computed(() => (
  props.order?.fulfillment_status === 'not_prepared'
    ? 'Передан в автовыдачу'
    : 'Автовыдача работает'
))
const title = computed(() => ({
  automatic: automaticTitle.value,
  operator: 'Нужна ручная выдача',
  view: 'Посмотреть выдачу',
  attention: 'Выдача требует проверки',
}[action.value] || ''))
</script>

<template>
  <span
    v-if="action === 'automatic'"
    class="fulfillment-action fulfillment-action--automatic"
    :class="{ 'fulfillment-action--compact': compact }"
    :title="title"
    :aria-label="title"
    role="status"
  >
    <span class="fulfillment-action__orbit" aria-hidden="true"></span>
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="7.4" />
      <path d="M12 7.8v4.7l3.1 1.8" />
    </svg>
  </span>
  <button
    v-else-if="action !== 'none'"
    class="fulfillment-action"
    :class="[`fulfillment-action--${action}`, { 'fulfillment-action--compact': compact }]"
    type="button"
    :title="title"
    :aria-label="`${title}: заказ ${order.external_order_id}`"
    @click="emit('open')"
  >
    <svg v-if="action === 'view'" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3.8 12s3-5 8.2-5 8.2 5 8.2 5-3 5-8.2 5-8.2-5-8.2-5Z" />
      <circle cx="12" cy="12" r="2.2" />
    </svg>
    <svg v-else-if="action === 'attention'" viewBox="0 0 24 24" aria-hidden="true">
      <path d="m12 4 8 15H4Z" />
      <path d="M12 9v4.5M12 16.8v.1" />
    </svg>
    <svg v-else viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 8.5 12 4l8 4.5v8L12 21l-8-4.5z" />
      <path d="m4 8.5 8 4.5 8-4.5M12 13v8" />
      <path d="M8.5 6 16 10.2" />
    </svg>
  </button>
</template>

<style scoped>
.fulfillment-action {
  position: relative;
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  flex: 0 0 auto;
  overflow: hidden;
  padding: 0;
  border: 1px solid rgba(92, 132, 255, .5);
  border-radius: 11px;
  color: #94aaff;
  background: rgba(37, 68, 163, .25);
  transition: color .18s, border-color .18s, background .18s, transform .18s, box-shadow .18s;
}

button.fulfillment-action { cursor: pointer; }

.fulfillment-action--compact {
  width: 30px;
  height: 30px;
  border-radius: 9px;
}

.fulfillment-action svg {
  position: relative;
  z-index: 1;
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.fulfillment-action--compact svg { width: 15px; height: 15px; }

button.fulfillment-action:hover,
button.fulfillment-action:focus-visible {
  color: #fff;
  border-color: rgba(102, 139, 255, .78);
  background: rgba(54, 88, 195, .5);
  box-shadow: 0 8px 22px rgba(21, 55, 164, .22);
  transform: translateY(-1px);
  outline: none;
}

.fulfillment-action--operator {
  color: #ffc75a;
  border-color: rgba(255, 199, 90, .52);
  background: rgba(255, 174, 45, .1);
}

button.fulfillment-action--operator:hover,
button.fulfillment-action--operator:focus-visible {
  border-color: rgba(255, 207, 105, .82);
  background: rgba(255, 174, 45, .2);
}

.fulfillment-action--attention {
  color: #ff969b;
  border-color: rgba(255, 105, 114, .52);
  background: rgba(255, 82, 94, .1);
}

button.fulfillment-action--attention:hover,
button.fulfillment-action--attention:focus-visible {
  border-color: rgba(255, 139, 146, .82);
  background: rgba(255, 82, 94, .2);
}

.fulfillment-action--automatic {
  color: #78a9ff;
  border-color: rgba(83, 135, 255, .42);
  background: radial-gradient(circle at 50% 50%, rgba(45, 90, 215, .2), rgba(19, 37, 83, .22));
}

.fulfillment-action__orbit {
  position: absolute;
  inset: 4px;
  border: 1.5px solid transparent;
  border-top-color: rgba(104, 157, 255, .95);
  border-right-color: rgba(104, 157, 255, .22);
  border-radius: 50%;
  animation: fulfillment-orbit 1.35s linear infinite;
}

@keyframes fulfillment-orbit { to { transform: rotate(360deg); } }

@media (prefers-reduced-motion: reduce) {
  .fulfillment-action__orbit { animation: none; border-color: rgba(104, 157, 255, .5); }
}
</style>
