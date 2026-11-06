<template>
  <div class="approvals">
    <div v-if="!inline" class="page-header">
      <router-link :to="`/task/${id}`" class="back-btn">&larr;</router-link>
      <div>
        <h2>{{ $t('approve.title') }}</h2>
        <p>{{ $t('approve.subtitle') }}</p>
      </div>
      <div v-if="items.length" class="ml-auto header-actions">
        <button
          v-if="pendingItems.length > 1"
          class="btn btn-primary btn-sm"
          :disabled="bulkSubmitting"
          @click="approveAll"
        >
          {{ bulkSubmitting
            ? $t('approve.approvingAll')
            : $t('approve.approveAll', { count: pendingItems.length }) }}
        </button>
        <span class="badge badge-neutral">{{ $t('approve.pendingCount', { count: pendingItems.length }) }}</span>
      </div>
    </div>
    <div v-else class="inline-header">
      <div>
        <h4 class="mb-0">{{ $t('approve.title') }}</h4>
        <span v-if="items.length" class="inline-count">{{ $t('approve.pendingCount', { count: pendingItems.length }) }}</span>
      </div>
      <div class="header-actions">
        <button
          v-if="pendingItems.length > 1"
          class="btn btn-primary btn-sm"
          :disabled="bulkSubmitting"
          @click="approveAll"
        >
          {{ bulkSubmitting
            ? $t('approve.approvingAll')
            : $t('approve.approveAll', { count: pendingItems.length }) }}
        </button>
        <router-link :to="`/task/${id}/approve`" class="btn btn-ghost btn-sm">{{ $t('approve.openFullPage') }}</router-link>
      </div>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="loading-block">
      <div v-for="i in 3" :key="i" class="skeleton" style="height:120px;margin-bottom:var(--space-md);border-radius:var(--radius-lg);"></div>
    </div>

    <!-- Empty -->
    <div v-else-if="items.length === 0" class="card" style="text-align:center;padding:var(--space-3xl);">
      <div class="empty-state">
        <div class="empty-state-icon">✅</div>
        <div class="empty-state-title">{{ $t('approve.noApprovals') }}</div>
        <div class="empty-state-desc">{{ $t('approve.noApprovalsDesc') }}</div>
        <router-link :to="`/task/${id}`" class="btn btn-secondary btn-sm mt-md">
          {{ $t('approve.backToTask') }}
        </router-link>
      </div>
    </div>

    <div v-if="!loading && settledOnly" class="card" style="text-align:center;padding:var(--space-xl);margin-bottom:var(--space-md);">
      <div class="empty-state">
        <div class="empty-state-title">{{ $t('approve.noPendingStuck') }}</div>
        <div class="empty-state-desc">{{ $t('approve.noPendingStuckDesc') }}</div>
        <router-link :to="`/task/${id}`" class="btn btn-secondary btn-sm mt-md">
          {{ $t('approve.backToTask') }}
        </router-link>
      </div>
    </div>

    <!-- Approval Cards -->
    <TransitionGroup name="list" tag="div" v-if="!loading && items.length">
      <div
        v-for="(item, index) in items"
        :key="item.id"
        class="card approval-card"
        :class="{
          'approval-pending': item._status === 'review',
          'approval-approved': item._status === 'approved',
          'approval-rejected': item._status === 'rejected'
        }"
        :style="{ animationDelay: index * 0.06 + 's' }"
      >
        <!-- Header -->
        <div class="approval-header">
          <div class="flex items-center gap-sm">
            <span class="artifact-icon">{{ artifactIcon(item.artifact_type) }}</span>
            <span class="badge badge-info">{{ item.artifact_type }}</span>
            <span
              :class="[
                'badge approval-status-badge',
                item._status === 'approved' ? 'badge-success' :
                item._status === 'rejected' ? 'badge-error' : 'badge-warning'
              ]"
            >
              {{ item._status === 'review' ? $t('approve.pending') : item._status }}
            </span>
          </div>
        </div>

        <!-- Content -->
        <div class="approval-title-row">
          <h3 class="approval-title">{{ item.title }}</h3>
          <router-link
            v-if="item.artifact_type === 'design_plan'"
            :to="`/task/${id}/approval/${item.id}`"
            class="btn btn-ghost btn-sm"
          >
            {{ t('approve.viewDetail') }}
          </router-link>
        </div>
        <pre class="approval-desc">{{ item.description }}</pre>

        <!-- Actions (only when in review) -->
        <Transition name="fade">
          <div v-if="item._status === 'review'" class="approval-actions">
            <input
              v-model="item._comment"
              class="input"
              :placeholder="$t('approve.commentPlaceholder')"
            />
            <div class="approval-btns">
              <button class="btn btn-danger btn-sm" :disabled="bulkSubmitting" @click="respond(item, false)">
                {{ $t('approve.reject') }}
              </button>
              <button class="btn btn-primary btn-sm" :disabled="bulkSubmitting" @click="respond(item, true)">
                {{ $t('approve.approveDeploy') }}
              </button>
            </div>
          </div>
        </Transition>

        <!-- Result feedback -->
        <div v-if="item._status === 'approved'" class="approval-result success">
          ✓ {{ $t('approve.approvedDeploy') }}
        </div>
        <div v-else-if="item._status === 'rejected'" class="approval-result rejected">
          ✗ {{ $t('approve.rejectedRevision') }}
        </div>
      </div>
    </TransitionGroup>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { storeToRefs } from 'pinia'
import { useMessage } from 'naive-ui'
import { api } from '../utils/api.js'
import { useTaskStore } from '../stores/task.js'

const { t } = useI18n()
const message = useMessage()

const props = defineProps({
  inline: { type: Boolean, default: false },
})

const route = useRoute()
const id = route.params.id
const items = ref([])
const loading = ref(true)
const bulkSubmitting = ref(false)
const pendingItems = computed(() => items.value.filter(i => i._status === 'review'))
const settledOnly = computed(() => items.value.length > 0 && pendingItems.value.length === 0)

/* Live updates: the global store owns the task WS connection — reload the
   list (throttled) whenever an approval/clarification event arrives, plus
   a slow poll while the tab is visible as a fallback. */
const store = useTaskStore()
const { hitlEventSeq } = storeToRefs(store)

let refreshThrottle = null
watch(hitlEventSeq, () => {
  if (refreshThrottle) return
  refreshThrottle = setTimeout(() => { refreshThrottle = null; load() }, 1000)
})

let pollTimer = null

const ARTIFACT_ICONS = {
  design_plan: '📐',
  drift_fix: '🩹',
  execution: '⚡',
  deep_explore: '🔍',
}
function artifactIcon(type) {
  return ARTIFACT_ICONS[type] || '📋'
}

async function load() {
  try {
    const data = await api.getApprovals(id)
    // Preserve local review state (comment draft / just-submitted result)
    // so a background refresh never clobbers what the user is doing.
    const prev = new Map(items.value.map(i => [i.id, i]))
    items.value = (data || []).map(item => {
      const old = prev.get(item.id)
      const settled = old?._status === 'approved' || old?._status === 'rejected'
      return {
        ...item,
        _status: settled ? old._status : (item.status === 'pending' ? 'review' : item.status),
        _comment: old?._comment || '',
      }
    })
  } catch (e) {
    console.error('Failed to load approvals:', e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  store.setContext(id)
  await load()
  pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') load()
  }, 10000)
})

onUnmounted(() => {
  clearInterval(pollTimer)
  pollTimer = null
  clearTimeout(refreshThrottle)
})

async function respond(item, approved) {
  try {
    await api.respondApproval(id, {
      approval_id: item.id,
      approved,
      comment: item._comment || '',
    })
    item._status = approved ? 'approved' : 'rejected'
    store.refreshCounts()
  } catch (e) {
    message.error(t('common.error') + ': ' + e.message)
  }
}

async function approveAll() {
  const pending = pendingItems.value
  if (!pending.length || bulkSubmitting.value) return
  if (!window.confirm(t('approve.approveAllConfirm', { count: pending.length }))) return
  bulkSubmitting.value = true
  try {
    await api.bulkApprove(id, { comment: '' })
    for (const item of pending) item._status = 'approved'
    store.refreshCounts()
    message.success(t('approve.approveAllDone'))
  } catch (e) {
    message.error(t('common.error') + ': ' + e.message)
  } finally {
    bulkSubmitting.value = false
  }
}
</script>

<style scoped>
/* ── Loading ─────────────────────────────────────────────── */
.loading-block {
  padding: var(--space-md) 0;
}

/* ── Card ────────────────────────────────────────────────── */
.approval-card {
  margin-bottom: var(--space-md);
  border-left: 4px solid transparent;
  transition:
    border-color var(--duration-base) var(--spring-default),
    box-shadow var(--duration-base) var(--spring-default);
  animation: card-enter 0.4s var(--spring-default) both;
}

@keyframes card-enter {
  from {
    opacity: 0;
    transform: translateY(12px);
  }
}

.artifact-icon {
  font-size: 1.25rem;
  line-height: 1;
}

.approval-pending {
  border-left-color: var(--color-warning);
  box-shadow: 0 4px 20px rgba(255,149,0,0.12);
}

.approval-approved {
  border-left-color: var(--color-success);
}

.approval-rejected {
  border-left-color: var(--color-error);
}

.approval-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-sm);
}

.approval-status-badge {
  transition: all var(--duration-base) var(--spring-default);
}

.approval-title {
  margin-bottom: var(--space-xs);
  font-size: 1.0625rem;
}

.approval-desc {
  background: var(--color-bg);
  padding: var(--space-md);
  border-radius: var(--radius-md);
  font-size: 0.8125rem;
  white-space: pre-wrap;
  font-family: var(--font-mono);
  color: var(--color-text-secondary);
  line-height: 1.6;
}

/* ── Actions ─────────────────────────────────────────────── */
.approval-actions {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-top: var(--space-md);
  padding-top: var(--space-md);
  border-top: 1px solid var(--color-border);
}

.approval-btns {
  display: flex;
  gap: var(--space-sm);
  flex-shrink: 0;
}

/* ── Result ──────────────────────────────────────────────── */
.approval-result {
  margin-top: var(--space-md);
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-sm);
  font-size: 0.8125rem;
  font-weight: 500;
  animation: result-in 0.3s var(--spring-bounce);
}

@keyframes result-in {
  from {
    opacity: 0;
    transform: scale(0.95);
  }
}

.approval-result.success {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.approval-result.rejected {
  background: var(--color-error-bg);
  color: var(--color-error);
}

/* ── Utility ─────────────────────────────────────────────── */
.ml-auto { margin-left: auto; }
.mt-md { margin-top: var(--space-md); }

.header-actions {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-shrink: 0;
}
</style>
