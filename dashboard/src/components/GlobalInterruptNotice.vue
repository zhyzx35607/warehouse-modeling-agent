<template>
  <n-modal v-model:show="showModal" :mask-closable="false" class="interrupt-modal-wrap">
    <div class="interrupt-modal" :class="isApproval ? 'interrupt-modal-approval' : 'interrupt-modal-clarify'">
      <div class="interrupt-modal-icon">{{ isApproval ? '✋' : '💬' }}</div>
      <h2 class="interrupt-modal-title">
        {{ isApproval ? $t('interrupt.approvalTitle') : $t('interrupt.clarificationTitle') }}
      </h2>
      <p class="interrupt-modal-desc">
        {{ isApproval
          ? $t('interrupt.approvalDesc', { count: itemCount })
          : $t('interrupt.clarificationDesc', { count: itemCount }) }}
      </p>

      <ul v-if="itemLabels.length" class="interrupt-modal-items">
        <li v-for="(label, i) in itemLabels" :key="i">{{ label }}</li>
      </ul>

      <div class="interrupt-modal-actions">
        <n-button size="large" quaternary @click="dismiss">
          {{ $t('interrupt.later') }}
        </n-button>
        <n-button size="large" type="primary" @click="goHandle">
          {{ $t('interrupt.goHandle') }} →
        </n-button>
      </div>
    </div>
  </n-modal>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { storeToRefs } from 'pinia'
import { useNotification, NModal, NButton } from 'naive-ui'
import { useTaskStore } from '../stores/task.js'

const router = useRouter()
const { t } = useI18n()
const notification = useNotification()
const store = useTaskStore()
const { taskId, interruptSeq, lastInterrupt } = storeToRefs(store)

const showModal = ref(false)
// The interrupt event seq the user has dismissed — a NEW interrupt re-opens
// the modal even if they snoozed an earlier one.
let dismissedSeq = 0

const isApproval = computed(() =>
  (lastInterrupt.value?.data?.interrupt_type || lastInterrupt.value?.data?.type) === 'approval'
)

const itemCount = computed(() => {
  const d = lastInterrupt.value?.data?.details || {}
  return d.items_count ?? d.blocking_count ?? 0
})

const itemLabels = computed(() => {
  const d = lastInterrupt.value?.data?.details || {}
  const items = d.items || d.questions || []
  return items
    .map(it => it.title || it.question || '')
    .filter(Boolean)
    .slice(0, 5)
})

watch(interruptSeq, (seq) => {
  if (!seq || seq === dismissedSeq) return
  // Already on the page that handles this interrupt? The center modal would
  // just block the work the user is doing — the corner notification is
  // enough there.
  const destPath = isApproval.value ? `/task/${taskId.value}/approve` : `/task/${taskId.value}/clarify`
  if (router.currentRoute.value.path !== destPath) {
    showModal.value = true
  }
  notification.warning({
    title: isApproval.value ? t('interrupt.approvalTitle') : t('interrupt.clarificationTitle'),
    content: isApproval.value
      ? t('interrupt.approvalDesc', { count: itemCount.value })
      : t('interrupt.clarificationDesc', { count: itemCount.value }),
    duration: 6000,
  })
})

function dismiss() {
  dismissedSeq = interruptSeq.value
  showModal.value = false
}

function goHandle() {
  dismissedSeq = interruptSeq.value
  showModal.value = false
  const dest = isApproval.value ? `/task/${taskId.value}/approve` : `/task/${taskId.value}/clarify`
  router.push(dest)
}
</script>

<style scoped>
.interrupt-modal {
  width: min(520px, 92vw);
  padding: var(--space-xl) var(--space-xl) var(--space-lg);
  border-radius: var(--radius-2xl);
  background: var(--color-bg-glass-heavy);
  backdrop-filter: blur(24px) saturate(1.6);
  -webkit-backdrop-filter: blur(24px) saturate(1.6);
  border: 1px solid var(--color-border);
  box-shadow: var(--shadow-modal);
  text-align: center;
}

.interrupt-modal-approval {
  border-top: 3px solid var(--color-warning);
}

.interrupt-modal-clarify {
  border-top: 3px solid var(--color-accent);
}

.interrupt-modal-icon {
  font-size: 2.5rem;
  margin-bottom: var(--space-sm);
  animation: interrupt-icon-pulse 1.6s ease-in-out infinite;
}

@keyframes interrupt-icon-pulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.15); }
}

.interrupt-modal-title {
  margin: 0 0 var(--space-xs);
  font-size: 1.375rem;
  color: var(--color-text);
}

.interrupt-modal-desc {
  color: var(--color-text-secondary);
  font-size: 0.9375rem;
  margin-bottom: var(--space-md);
}

.interrupt-modal-items {
  list-style: none;
  text-align: left;
  margin: 0 0 var(--space-lg);
  padding: var(--space-md);
  background: var(--color-bg);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
}

.interrupt-modal-items li {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  padding: 3px 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.interrupt-modal-items li::before {
  content: '•';
  color: var(--color-accent);
  margin-right: 6px;
}

.interrupt-modal-actions {
  display: flex;
  justify-content: center;
  gap: var(--space-md);
}
</style>
