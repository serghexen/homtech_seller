<script setup>
import { onBeforeUnmount, onMounted } from 'vue'

const props = defineProps({
  item: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['confirm', 'cancel'])

function closeOnEscape(event) {
  if (event.key === 'Escape' && !props.busy) emit('cancel')
}

onMounted(() => window.addEventListener('keydown', closeOnEscape))
onBeforeUnmount(() => window.removeEventListener('keydown', closeOnEscape))
</script>

<template>
  <Teleport to="body">
    <Transition name="catalog-archive-confirm" appear>
      <div class="catalog-archive-confirm__backdrop" @click.self="!busy && emit('cancel')">
        <section class="catalog-archive-confirm" role="alertdialog" aria-modal="true" aria-labelledby="catalog-archive-title" aria-describedby="catalog-archive-description">
          <span class="catalog-archive-confirm__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M4 8h16v11H4z" /><path d="M3 5h18v3H3z" /><path d="M9 12h6" /></svg>
          </span>
          <p class="catalog-archive-confirm__eyebrow">ЯНДЕКС МАРКЕТ</p>
          <h2 id="catalog-archive-title">Перенести карточку в архив?</h2>
          <p id="catalog-archive-description">
            Она исчезнет из активного каталога маркетплейса. Настройки Seller, инструкция, ключи и история заказов сохранятся.
          </p>
          <div class="catalog-archive-confirm__product">
            <strong>{{ item.title || item.offer_id || 'Товар без названия' }}</strong>
            <span>SKU: {{ item.sku || item.offer_id || '—' }}</span>
          </div>
          <div class="catalog-archive-confirm__actions">
            <button class="catalog-archive-confirm__cancel" type="button" :disabled="busy" @click="emit('cancel')">Отмена</button>
            <button class="catalog-archive-confirm__submit" type="button" :disabled="busy" @click="emit('confirm')">
              {{ busy ? 'Переносим…' : 'В архив' }}
            </button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.catalog-archive-confirm__backdrop {
  position: fixed;
  z-index: 1300;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(3, 8, 25, .78);
  backdrop-filter: blur(10px);
}

.catalog-archive-confirm {
  width: min(100%, 510px);
  padding: 31px;
  border: 1px solid rgba(128, 151, 211, .35);
  border-radius: 25px;
  color: #eef3ff;
  background: linear-gradient(145deg, rgba(24, 40, 77, .99), rgba(10, 18, 43, .99));
  box-shadow: 0 28px 90px rgba(2, 7, 24, .58), inset 0 1px rgba(255, 255, 255, .04);
}

.catalog-archive-confirm__icon {
  display: grid;
  width: 48px;
  height: 48px;
  margin-bottom: 19px;
  place-items: center;
  border: 1px solid rgba(111, 140, 255, .46);
  border-radius: 14px;
  color: #8fa8ff;
  background: rgba(44, 78, 194, .17);
}

.catalog-archive-confirm__icon svg {
  width: 23px;
  height: 23px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.catalog-archive-confirm__eyebrow {
  margin: 0 0 8px;
  color: #7e9aff;
  font-size: 11px;
  font-weight: 900;
  letter-spacing: .14em;
}

.catalog-archive-confirm h2 {
  margin: 0;
  color: #f5f7ff;
  font-size: clamp(25px, 4vw, 34px);
  line-height: 1.08;
  letter-spacing: -.045em;
}

.catalog-archive-confirm > p:not(.catalog-archive-confirm__eyebrow) {
  margin: 15px 0 0;
  color: #b7c3de;
  font-size: 15px;
  line-height: 1.55;
}

.catalog-archive-confirm__product {
  display: grid;
  gap: 7px;
  margin: 24px 0;
  padding: 16px 17px;
  border: 1px solid rgba(128, 151, 211, .24);
  border-radius: 16px;
  background: rgba(7, 14, 35, .45);
}

.catalog-archive-confirm__product strong {
  overflow: hidden;
  color: #f2f5ff;
  font-size: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.catalog-archive-confirm__product span {
  color: #93a3c7;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.catalog-archive-confirm__actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.catalog-archive-confirm__actions button {
  min-height: 46px;
  padding: 0 19px;
  border-radius: 13px;
  font: inherit;
  font-weight: 850;
}

.catalog-archive-confirm__cancel {
  border: 1px solid rgba(136, 154, 201, .31);
  color: #c8d2e9;
  background: rgba(30, 42, 74, .75);
}

.catalog-archive-confirm__submit {
  border: 1px solid rgba(116, 145, 255, .65);
  color: #fff;
  background: linear-gradient(135deg, #2356e8, #4b73ff);
  box-shadow: 0 12px 28px rgba(35, 86, 232, .25);
}

.catalog-archive-confirm__actions button:disabled { opacity: .58; cursor: wait; }
.catalog-archive-confirm__actions button:focus-visible { outline: 3px solid rgba(103, 139, 255, .28); outline-offset: 2px; }
.catalog-archive-confirm-enter-active, .catalog-archive-confirm-leave-active { transition: opacity .18s ease; }
.catalog-archive-confirm-enter-active .catalog-archive-confirm, .catalog-archive-confirm-leave-active .catalog-archive-confirm { transition: transform .2s ease, opacity .18s ease; }
.catalog-archive-confirm-enter-from, .catalog-archive-confirm-leave-to { opacity: 0; }
.catalog-archive-confirm-enter-from .catalog-archive-confirm, .catalog-archive-confirm-leave-to .catalog-archive-confirm { opacity: 0; transform: translateY(9px) scale(.98); }

@media (max-width: 560px) {
  .catalog-archive-confirm__backdrop { padding: 14px; }
  .catalog-archive-confirm { padding: 24px 20px; border-radius: 21px; }
  .catalog-archive-confirm__actions { flex-direction: column-reverse; }
  .catalog-archive-confirm__actions button { width: 100%; }
}
</style>
