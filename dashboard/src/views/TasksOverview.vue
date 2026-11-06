<template>
  <div class="tasks-overview">
    <div class="page-header">
      <router-link to="/" class="back-btn">&larr;</router-link>
      <div class="flex-1">
        <h2>{{ $t('tasks.title') }}</h2>
        <p>{{ $t('tasks.subtitle') }}</p>
      </div>
      <div class="header-actions">
        <select v-model="filterStatus" class="select" @change="loadTasks">
          <option value="">{{ $t('tasks.allStatus') }}</option>
          <option>pending</option>
          <option>running</option>
          <option>completed</option>
          <option>failed</option>
          <option>cancelled</option>
        </select>
        <button
          v-if="failedCount > 0"
          class="btn btn-danger btn-sm"
          @click="showCleanAllConfirm = true"
          :disabled="cleaning"
        >
          <span v-if="cleaning" class="btn-spinner-sm"></span>
          {{ cleaning ? $t('tasks.cleaning') : $t('tasks.cleanBtn') + ` (${failedCount})` }}
        </button>
        <button class="btn btn-primary btn-sm" @click="loadTasks" :disabled="loading">
          <span v-if="loading" class="btn-spinner-sm"></span>
          {{ $t('tasks.refresh') }}
        </button>
      </div>
    </div>

    <!-- Summary Stat Cards -->
    <Transition name="fade">
      <div v-if="tasks.length" class="stats-row">
        <div
          class="stat-card stat-total"
          v-motion
          :initial="{ opacity: 0, y: 14 }"
          :enter="{ opacity: 1, y: 0, transition: { duration: 400 } }"
        >
          <span class="stat-num">{{ tasks.length }}</span>
          <span class="stat-label">{{ $t('tasks.total') }}</span>
        </div>
        <div
          v-for="(s, i) in statuses"
          :key="s.key"
          class="stat-card"
          :style="{ '--stat-color': s.color }"
          v-motion
          :initial="{ opacity: 0, y: 14 }"
          :enter="{ opacity: 1, y: 0, transition: { duration: 400, delay: 60 * (i + 1) } }"
        >
          <span class="stat-num" :style="{ color: s.color }">{{ s.count }}</span>
          <span class="stat-label">{{ s.label }}</span>
        </div>
      </div>
    </Transition>

    <!-- Task Table -->
    <div v-if="tasks.length" class="card table-card">
      <div class="table-wrapper">
        <table class="task-table">
          <thead>
            <tr>
              <th>{{ $t('tasks.taskId') }}</th>
              <th>{{ $t('tasks.goal') }}</th>
              <th>{{ $t('tasks.status') }}</th>
              <th class="th-phase">{{ $t('tasks.phase') }}</th>
              <th class="th-time">{{ $t('tasks.created') }}</th>
              <th class="th-time">{{ $t('tasks.updated') }}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="task in tasks"
              :key="task.task_id"
              class="task-table-row"
              @click="$router.push(`/task/${task.task_id}`)"
            >
              <td>
                <code class="task-id">{{ task.task_id?.slice(0, 8) }}</code>
              </td>
              <td>
                <span class="task-goal" :title="task.modeling_goal">
                  {{ task.modeling_goal?.slice(0, 60) || '-' }}
                </span>
              </td>
              <td>
                <span class="badge status-pill" :class="statusBadge(task.status)">
                  <span v-if="isLiveStatus(task.status)" class="status-dot"></span>
                  {{ task.status }}
                </span>
              </td>
              <td class="td-phase">
                <span class="phase-label-sm">{{ task.phase || '-' }}</span>
              </td>
              <td class="time-cell">{{ formatTime(task.created_at) }}</td>
              <td class="time-cell">{{ formatTime(task.updated_at) }}</td>
              <td>
                <div class="row-actions">
                  <router-link
                    :to="`/task/${task.task_id}`"
                    class="btn btn-secondary btn-sm"
                    @click.stop
                  >
                    {{ $t('tasks.open') }}
                  </router-link>
                  <button
                    v-if="task.status === 'failed'"
                    class="btn btn-recover btn-sm"
                    @click.stop="recoverTask(task)"
                    :disabled="recovering.has(task.task_id)"
                    :title="$t('tasks.recoverOne')"
                  >
                    <span v-if="recovering.has(task.task_id)" class="btn-spinner-sm"></span>
                    {{ recovering.has(task.task_id) ? '' : '⟳' }}
                  </button>
                  <button
                    v-if="task.status === 'failed'"
                    class="btn btn-danger-outline btn-sm"
                    @click.stop="confirmDeleteOne(task)"
                    :disabled="cleaning"
                    :title="$t('tasks.cleanOne')"
                  >
                    ×
                  </button>
                  <button
                    v-if="isLiveStatus(task.status)"
                    class="btn btn-danger btn-sm"
                    @click.stop="cancelRunningTask(task)"
                    :disabled="cancelling.has(task.task_id)"
                    :title="$t('tasks.cancelTask')"
                  >
                    <span v-if="cancelling.has(task.task_id)" class="btn-spinner-sm"></span>
                    {{ cancelling.has(task.task_id) ? '' : '■' }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Empty State -->
    <div v-else-if="!loading" class="card" style="text-align:center;padding:var(--space-3xl);">
      <div class="empty-state">
        <div class="empty-state-icon">📋</div>
        <div class="empty-state-title">{{ $t('tasks.noTasks') }}</div>
        <div class="empty-state-desc">{{ $t('tasks.emptyDesc') }}</div>
        <router-link to="/" class="btn btn-primary mt-md">
          {{ $t('tasks.createFirst') }}
        </router-link>
      </div>
    </div>

    <!-- Loading -->
    <div v-else class="loading-section">
      <div class="skeleton skeleton-title" style="width:30%;"></div>
      <div v-for="i in 4" :key="i" class="skeleton skeleton-card" style="margin-bottom:8px;height:48px;"></div>
    </div>

    <!-- Confirmation Dialog -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showCleanAllConfirm || deleteTarget" class="modal-overlay" @click.self="cancelDelete">
          <div class="modal-card">
            <div class="modal-header">
              <h3>{{ $t('tasks.cleanFailed') }}</h3>
            </div>
            <div class="modal-body">
              <p v-if="showCleanAllConfirm">
                {{ $t('tasks.cleanAllConfirm', { count: failedCount }) }}
              </p>
              <p v-else-if="deleteTarget">
                {{ $t('tasks.cleanConfirm') }}
                <br />
                <code class="modal-task-id">{{ deleteTarget.task_id?.slice(0, 8) }}</code>
                <span class="modal-task-goal">{{ deleteTarget.modeling_goal?.slice(0, 60) }}</span>
              </p>
            </div>
            <div class="modal-footer">
              <button class="btn btn-secondary" @click="cancelDelete" :disabled="cleaning">
                {{ $t('home.cancel') }}
              </button>
              <button class="btn btn-danger" @click="executeCleanup" :disabled="cleaning">
                <span v-if="cleaning" class="btn-spinner-sm"></span>
                {{ cleaning ? $t('tasks.cleaning') : $t('common.confirm') }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { api } from '../utils/api.js'

const { t } = useI18n()
const message = useMessage()

const tasks = ref([])
const loading = ref(false)
const filterStatus = ref('')
const cleaning = ref(false)
const recovering = ref(new Set())
const cancelling = ref(new Set())
const showCleanAllConfirm = ref(false)
const deleteTarget = ref(null)

async function loadTasks() {
  loading.value = true
  try {
    const resp = await api.fetch(`/api/v1/tasks?status=${filterStatus.value}`)
    if (resp.ok) {
      const data = await resp.json()
      tasks.value = data.tasks || data.items || []
    }
  } catch {
    tasks.value = []
  } finally {
    loading.value = false
  }
}

const failedCount = computed(() =>
  tasks.value.filter(t => t.status === 'failed').length
)

const statuses = computed(() => {
  const keys = ['running', 'completed', 'failed', 'pending', 'cancelled']
  const colors = {
    running: 'var(--color-warning)', completed: 'var(--color-success)',
    failed: 'var(--color-error)', pending: 'var(--color-text-secondary)',
    cancelled: 'var(--color-text-tertiary)',
  }
  const labels = {
    running: t('tasks.statusRunning'), completed: t('tasks.statusDone'),
    failed: t('tasks.statusFailed'), pending: t('tasks.statusPending'),
    cancelled: t('tasks.statusCancelled'),
  }
  return keys.map(k => ({
    key: k, count: tasks.value.filter(t => t.status === k).length,
    color: colors[k], label: labels[k],
  }))
})

const LIVE_STATUSES = new Set(['running', 'executing', 'exploring', 'designing', 'validating', 'deploying', 'grilling', 'awaiting_approval'])
function isLiveStatus(s) {
  return LIVE_STATUSES.has(s)
}

function confirmDeleteOne(task) {
  deleteTarget.value = task
  showCleanAllConfirm.value = false
}

function cancelDelete() {
  showCleanAllConfirm.value = false
  deleteTarget.value = null
}

async function executeCleanup() {
  cleaning.value = true
  try {
    const taskId = deleteTarget.value?.task_id || ''
    const result = await api.cleanupTasks(taskId)
    showCleanAllConfirm.value = false
    deleteTarget.value = null
    message.success(t('tasks.cleanSuccess', { count: result.deleted_count }))
    await loadTasks()
  } catch (e) {
    message.error(`${t('tasks.cleanError')}: ${e.message}`)
  } finally {
    cleaning.value = false
  }
}

async function recoverTask(task) {
  const taskId = task.task_id
  recovering.value.add(taskId)
  try {
    const result = await api.recoverTask(taskId)
    if (result.status === 'recovering') {
      message.success(t('tasks.recoverSuccess'))
    } else {
      message.warning(`${t('tasks.recover')}: ${result.message}`)
    }
    await loadTasks()
  } catch (e) {
    message.error(`${t('tasks.recoverError')}: ${e.message}`)
  } finally {
    recovering.value.delete(taskId)
    recovering.value = new Set(recovering.value) // reactivity trigger
  }
}

async function cancelRunningTask(task) {
  if (!window.confirm(t('tasks.cancelConfirm'))) return
  const taskId = task.task_id
  cancelling.value.add(taskId)
  try {
    await api.cancelTask(taskId)
    message.success(t('tasks.cancelSuccess'))
    await loadTasks()
  } catch (e) {
    message.error(`${t('tasks.cancelError')}: ${e.message}`)
  } finally {
    cancelling.value.delete(taskId)
    cancelling.value = new Set(cancelling.value)
  }
}

function statusBadge(s) {
  const map = {
    completed: 'badge-success', failed: 'badge-error',
    running: 'badge-warning', executing: 'badge-warning',
    pending: 'badge-neutral', cancelled: 'badge-neutral',
  }
  return map[s] || 'badge-neutral'
}

function formatTime(d) {
  if (!d) return '-'
  const t = new Date(d)
  return t.toLocaleDateString() + ' ' + t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

onMounted(loadTasks)
</script>

<style scoped>
/* ── Header Actions ──────────────────────────────────────── */
.header-actions {
  display: flex;
  gap: var(--space-sm);
  flex-shrink: 0;
}

.btn-spinner-sm {
  width: 12px;
  height: 12px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
  display: inline-block;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ── Stats Row ───────────────────────────────────────────── */
.stats-row {
  display: flex;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
  flex-wrap: wrap;
}

.stat-card {
  flex: 1;
  min-width: 90px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-md);
  text-align: center;
  transition:
    box-shadow var(--duration-base) var(--spring-default),
    border-color var(--duration-base) var(--spring-default),
    transform var(--duration-base) var(--spring-default);
}
.stat-card:hover {
  box-shadow: var(--shadow-elevated);
  transform: translateY(-2px);
}
.stat-card.stat-total {
  border-color: var(--color-accent);
  background: var(--color-accent-glass);
}

.stat-num {
  display: block;
  font-size: 1.75rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}

.stat-label {
  font-size: 0.6875rem;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-top: 2px;
  display: block;
}

/* ── Table ───────────────────────────────────────────────── */
.table-card {
  padding: 0;
  overflow: hidden;
}

.table-wrapper {
  overflow-x: auto;
}

.task-table {
  width: 100%;
  border-collapse: collapse;
  min-width: 700px;
}

.task-table th {
  text-align: left;
  padding: 12px var(--space-sm);
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 2px solid var(--color-border);
  background: var(--color-bg);
  position: sticky;
  top: 0;
}

.task-table td {
  padding: 10px var(--space-sm);
  border-bottom: 1px solid var(--color-border);
  font-size: 0.8125rem;
}

.task-table-row {
  cursor: pointer;
  transition: background var(--duration-fast) var(--spring-default);
}
.task-table-row:hover {
  background: var(--color-bg);
}
.task-table-row:active {
  background: var(--color-phase-bg);
}

.task-id {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--color-text-secondary);
  background: var(--color-bg);
  padding: 2px 6px;
  border-radius: var(--radius-xs);
}

.task-goal {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: block;
  max-width: 280px;
}

.time-cell {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.phase-label-sm {
  font-size: 0.75rem;
  color: var(--color-text-secondary);
  background: var(--color-phase-bg);
  padding: 2px 8px;
  border-radius: var(--radius-full);
}

.th-phase, .td-phase { display: none; }
.th-time { display: none; }

@media (min-width: 640px) {
  .th-phase, .td-phase { display: table-cell; }
}
@media (min-width: 768px) {
  .th-time { display: table-cell; }
}

/* ── Status pill with breathing dot for live statuses ──── */
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: status-breathe 1.6s ease-in-out infinite;
}

@keyframes status-breathe {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.75); }
}

/* ── Loading ─────────────────────────────────────────────── */
.loading-section {
  padding: var(--space-xl) 0;
}

/* ── Row Actions ─────────────────────────────────────────── */
.row-actions {
  display: flex;
  gap: 6px;
  align-items: center;
}

/* ── Danger Button ───────────────────────────────────────── */
.btn-danger {
  background: var(--color-danger, #FF3B30);
  color: #fff;
  border: 1px solid var(--color-danger, #FF3B30);
}
.btn-danger:hover {
  filter: brightness(1.1);
}

.btn-danger-outline {
  background: transparent;
  color: var(--color-danger, #FF3B30);
  border: 1px solid var(--color-danger, #FF3B30);
  padding: 2px 8px;
  font-size: 1rem;
  line-height: 1.2;
  font-weight: 700;
}
.btn-danger-outline:hover {
  background: var(--color-danger, #FF3B30);
  color: #fff;
}

/* ── Recover Button ──────────────────────────────────────── */
.btn-recover {
  background: transparent;
  color: var(--color-accent, #007AFF);
  border: 1px solid var(--color-accent, #007AFF);
  padding: 2px 8px;
  font-size: 1rem;
  line-height: 1.2;
  font-weight: 700;
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: all var(--duration-fast) var(--spring-default);
}
.btn-recover:hover:not(:disabled) {
  background: var(--color-accent, #007AFF);
  color: #fff;
}
.btn-recover:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ── Modal ───────────────────────────────────────────────── */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(4px);
}

.modal-card {
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-elevated);
  width: min(420px, 90vw);
  overflow: hidden;
}

.modal-header {
  padding: var(--space-md) var(--space-lg);
  border-bottom: 1px solid var(--color-border);
}
.modal-header h3 {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
}

.modal-body {
  padding: var(--space-lg);
  font-size: 0.875rem;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.modal-task-id {
  display: inline-block;
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--color-accent);
  background: var(--color-accent-glass);
  padding: 1px 6px;
  border-radius: var(--radius-xs);
  margin-right: 6px;
  vertical-align: middle;
}

.modal-task-goal {
  color: var(--color-text);
  vertical-align: middle;
}

.modal-footer {
  padding: var(--space-md) var(--space-lg);
  border-top: 1px solid var(--color-border);
  display: flex;
  gap: var(--space-sm);
  justify-content: flex-end;
}

/* ── Modal Transitions ───────────────────────────────────── */
.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.2s ease;
}
.modal-enter-active .modal-card,
.modal-leave-active .modal-card {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
.modal-enter-from .modal-card {
  transform: scale(0.95);
  opacity: 0;
}
.modal-leave-to .modal-card {
  transform: scale(0.95);
  opacity: 0;
}
</style>
