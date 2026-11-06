<template>
  <div class="task-dashboard">
    <!-- ── Top Bar ─────────────────────────────────────────── -->
    <div class="top-bar">
      <router-link to="/" class="back-btn">&larr;</router-link>
      <div class="flex-1">
        <h2 class="task-title">
          {{ $t('taskMonitor.title') }}
          <span class="task-id-badge">{{ id?.slice(0, 12) }}</span>
        </h2>
        <p class="task-goal-text">{{ task?.modeling_goal || fullState?.modeling_goal || $t('taskMonitor.loading') }}</p>
      </div>
      <div class="header-badges">
        <button
          v-if="isLive"
          class="btn btn-danger btn-sm"
          :disabled="cancelling"
          @click="doCancel"
        >
          <span v-if="cancelling" class="btn-spinner-sm" style="margin-right:6px;"></span>
          {{ cancelling ? $t('taskMonitor.cancelling') : $t('taskMonitor.cancelTask') }}
        </button>
        <span :class="['badge', statusBadgeClass]">
          <span v-if="isLive" class="badge-dot" style="margin-right:4px;"></span>
          {{ currentStatus || task?.status || fullState?.task?.status || $t('taskMonitor.connecting') }}
        </span>
        <span
          :class="['badge', wsConnected ? 'badge-success' : 'badge-neutral']"
          :title="wsConnected ? $t('taskMonitor.liveConnection') : $t('taskMonitor.disconnected')"
        >
          {{ wsConnected ? $t('taskMonitor.live') : $t('taskMonitor.offline') }}
        </span>
      </div>
    </div>

    <!-- ── Interrupt Banner ────────────────────────────────── -->
    <Transition name="fade">
      <div v-if="isAwaitingHuman && (countsLoaded ? displayClarificationCount > 0 || displayApprovalCount > 0 : true)" class="card interrupt-banner">
        <div class="interrupt-icon">✋</div>
        <div class="interrupt-body">
          <h3 class="interrupt-title">{{ $t('taskMonitor.humanInputRequired') }}</h3>
          <p class="interrupt-desc">
            <template v-if="currentStatus === 'grilling'">
              {{ $t('taskMonitor.interruptGrilling') }}
            </template>
            <template v-else-if="currentStatus === 'awaiting_approval'">
              {{ $t('taskMonitor.interruptApproval') }}
            </template>
            <template v-else>
              {{ $t('taskMonitor.interruptGeneric') }}
            </template>
          </p>
          <div class="interrupt-actions">
            <router-link v-if="currentStatus === 'grilling'" :to="`/task/${id}/clarify`" class="btn btn-primary">
              {{ $t('taskMonitor.answerQuestions', { count: displayClarificationCount }) }}
            </router-link>
            <router-link v-if="currentStatus === 'awaiting_approval'" :to="`/task/${id}/approve`" class="btn btn-primary">
              {{ $t('taskMonitor.reviewApprovals', { count: displayApprovalCount }) }}
            </router-link>
          </div>
        </div>
      </div>
    </Transition>

    <Transition name="fade">
      <div v-if="isStuckApproval" class="card recovery-banner">
        <div class="recovery-icon">✋</div>
        <div class="recovery-body">
          <h3 class="recovery-title">{{ $t('taskMonitor.stuckApprovalTitle') }}</h3>
          <p class="recovery-desc">{{ $t('taskMonitor.stuckApprovalDesc') }}</p>
          <div class="recovery-actions">
            <button class="btn btn-primary" @click="doRecover" :disabled="recovering">
              <span v-if="recovering" class="btn-spinner-sm" style="margin-right:6px;"></span>
              {{ recovering ? $t('task.recovery.recovering') : $t('task.recovery.btn') }}
            </button>
          </div>
          <p v-if="recoveryMsg" class="recovery-msg" :class="{ 'text-success': recoveryOk, 'text-error': !recoveryOk }">
            {{ recoveryMsg }}
          </p>
        </div>
      </div>
    </Transition>

    <!-- ── Recovery Banner (failed tasks) ──────────────────── -->
    <Transition name="fade">
      <div v-if="isFailed" class="card recovery-banner">
        <div class="recovery-icon">🔄</div>
        <div class="recovery-body">
          <h3 class="recovery-title">
            {{ $t(isValidationFailure ? 'task.recovery.validationTitle' : 'task.recovery.title') }}
          </h3>
          <p class="recovery-desc">
            {{ $t(isValidationFailure ? 'task.recovery.validationDesc' : 'task.recovery.desc') }}
            <span v-if="fullState?.recovery_count > 0" class="recovery-attempts">
              ({{ $t('common.attempt') }} {{ fullState.recovery_count }})
            </span>
          </p>
          <p v-if="failureHint" class="recovery-failure-hint">
            {{ failureHint }}
          </p>
          <div v-if="!isValidationFailure" class="recovery-actions">
            <button
              class="btn btn-primary"
              @click="doRecover"
              :disabled="recovering"
            >
              <span v-if="recovering" class="btn-spinner-sm" style="margin-right:6px;"></span>
              {{ recovering ? $t('task.recovery.recovering') : $t('task.recovery.btn') }}
            </button>
          </div>
          <p v-else class="recovery-msg text-error">
            {{ $t('task.recovery.validationAction') }}
          </p>
          <p v-if="recoveryMsg" class="recovery-msg" :class="{ 'text-success': recoveryOk, 'text-error': !recoveryOk }">
            {{ recoveryMsg }}
          </p>
        </div>
      </div>
    </Transition>

    <!-- ── Node Timeline ───────────────────────────────────── -->
    <NodeTimeline
      :timeline="allTimeline"
      :currentNode="currentNode"
      @selectNode="onSelectNode"
    />

    <!-- ── Tab Layout ──────────────────────────────────────── -->
    <div class="dashboard-layout">
      <!-- Left Sidebar Tabs -->
      <nav class="sidebar-tabs">
        <div class="sidebar-section-label">{{ $t('taskMonitor.sidebar.panels') }}</div>
        <button
          v-for="tab in tabs"
          :key="tab.key"
          :class="['tab-btn', { 'tab-active': activeTab === tab.key }]"
          @click="activeTab = tab.key"
          :title="tab.label"
        >
          <span class="tab-icon">{{ tab.icon }}</span>
          <span class="tab-label">{{ tab.label }}</span>
          <span v-if="tab.count !== null" class="tab-count">{{ tab.count }}</span>
        </button>

        <!-- Quick links to sub-pages (design lives in the 设计 tab above) -->
        <div class="sidebar-divider"></div>
        <div class="sidebar-section-label">{{ $t('taskMonitor.sidebar.pages') }}</div>
        <router-link :to="`/task/${id}/clarify`" class="tab-btn tab-link">
          <span class="tab-icon">💬</span>
          <span class="tab-label">{{ $t('taskMonitor.sidebar.questions') }}</span>
          <span v-if="displayClarificationCount" class="count-badge">{{ displayClarificationCount }}</span>
          <span class="tab-ext">↗</span>
        </router-link>
        <router-link :to="`/task/${id}/approve`" class="tab-btn tab-link">
          <span class="tab-icon">✅</span>
          <span class="tab-label">{{ $t('taskMonitor.sidebar.approvals') }}</span>
          <span v-if="task?.mode === 'auto'" class="auto-tag">{{ $t('taskMonitor.sidebar.auto') }}</span>
          <span v-else-if="displayApprovalCount" class="count-badge">{{ displayApprovalCount }}</span>
          <span class="tab-ext">↗</span>
        </router-link>
        <router-link :to="`/task/${id}/report`" class="tab-btn tab-link">
          <span class="tab-icon">📋</span>
          <span class="tab-label">{{ $t('taskMonitor.sidebar.report') }}</span>
          <span class="tab-ext">↗</span>
        </router-link>
      </nav>

      <!-- Main Content Area -->
      <main class="tab-content">
        <!-- Overview Tab -->
        <div v-show="activeTab === 'overview'" class="tab-panel">
          <div class="overview-grid">
            <div class="card stat-card">
              <div class="stat-label">{{ $t('taskMonitor.stats.phase') }}</div>
              <div class="stat-value">{{ formatPhase(displayPhase) }}</div>
            </div>
            <div class="card stat-card">
              <div class="stat-label">{{ $t('taskMonitor.stats.nodesCompleted') }}</div>
              <div class="stat-value">{{ completedNodesAnim }} / {{ totalNodes }}</div>
            </div>
            <div class="card stat-card">
              <div class="stat-label">{{ $t('taskMonitor.stats.clarifications') }}</div>
              <div class="stat-value">{{ clarificationsAnim }}</div>
            </div>
            <div class="card stat-card">
              <div class="stat-label">{{ $t('taskMonitor.stats.approvals') }}</div>
              <div class="stat-value">{{ approvalsAnim }}</div>
            </div>
            <div class="card stat-card">
              <div class="stat-label">{{ $t('taskMonitor.stats.errors') }}</div>
              <div class="stat-value" :class="{ 'text-error': errorCount > 0 }">{{ errorCountAnim }}</div>
            </div>
            <div class="card stat-card">
              <div class="stat-label">{{ $t('taskMonitor.stats.warnings') }}</div>
              <div class="stat-value" :class="{ 'text-warning': warningCount > 0 }">{{ warningCountAnim }}</div>
            </div>
            <div class="card stat-card">
              <div class="stat-label">{{ $t('taskMonitor.stats.recovery') }}</div>
              <div class="stat-value" :class="{ 'text-error': (fullState?.recovery_count || 0) > 0 }">
                {{ recoveryCountAnim }}
              </div>
            </div>
          </div>

          <LifecycleFeed
            :auditLog="fullState?.audit_log || []"
            :timeline="allTimeline"
            :thoughts="allThoughts"
            :wsEvents="wsEvents"
          />
        </div>

        <div v-if="hasStageOperations" v-show="activeTab === 'stages'" class="tab-panel">
          <StageOperations :task-id="id" />
        </div>

        <!-- LLM Tab -->
        <div v-show="activeTab === 'llm'" class="tab-panel">
          <LLMThoughts :thoughts="allThoughts" />
        </div>

        <!-- Design Tab -->
        <div v-show="activeTab === 'design'" class="tab-panel">
          <ArtifactPreview
            :designPlan="fullState?.design_plan || null"
            :taskId="id"
          />
        </div>

        <!-- Validation Tab -->
        <div v-show="activeTab === 'validation'" class="tab-panel">
          <ValidationPanel :results="fullState?.validation_results || []" />
        </div>

        <!-- Execution Tab -->
        <div v-show="activeTab === 'execution'" class="tab-panel">
          <ExecutionPanel
            :results="fullState?.execution_results || []"
            :errorClassifications="fullState?.error_classifications || []"
          />
        </div>

        <!-- KG Tab -->
        <div v-show="activeTab === 'kg'" class="tab-panel">
          <KGSnapshot
            :assetMap="fullState?.data_asset_map || fullState?.asset_map || null"
            :kgExportId="fullState?.kg_export_id || ''"
            :taskId="id"
          />
        </div>

        <!-- Errors / Warnings Tab -->
        <div v-show="activeTab === 'errors'" class="tab-panel">
          <div v-if="errorCount === 0 && warningCount === 0 && infoNotices.length === 0" class="empty-state" style="padding:var(--space-2xl);">
            <div class="empty-state-icon">✨</div>
            <div class="empty-state-title">{{ $t('taskMonitor.errorsTab.noErrors') }}</div>
          </div>
          <div v-else>
            <section v-if="errorCount > 0" class="issue-section issue-section-error">
              <div class="issue-section-heading">
                <div>
                  <h4 class="issue-section-title text-error">{{ $t('taskMonitor.errorsTab.errors', { count: errorCount }) }}</h4>
                  <p>{{ $t('taskMonitor.errorsTab.errorsHint') }}</p>
                </div>
              </div>
              <article v-for="(issue, i) in readableErrors" :key="'e'+i" class="issue-card issue-card-error">
                <div class="issue-icon" aria-hidden="true">!</div>
                <div class="issue-content">
                  <h5>{{ issue.title }}</h5>
                  <p class="issue-summary">{{ issue.summary }}</p>
                  <div v-if="issue.action" class="issue-action">
                    <span>{{ $t('taskMonitor.errorsTab.nextStep') }}</span>
                    {{ issue.action }}
                  </div>
                  <details class="issue-details">
                    <summary>{{ $t('taskMonitor.errorsTab.technicalDetails') }}</summary>
                    <code>{{ issue.raw }}</code>
                  </details>
                </div>
              </article>
            </section>

            <section v-if="warningCount > 0" class="issue-section issue-section-warning">
              <div class="issue-section-heading">
                <div>
                  <h4 class="issue-section-title text-warning">{{ $t('taskMonitor.errorsTab.warnings', { count: warningCount }) }}</h4>
                  <p>{{ $t('taskMonitor.errorsTab.warningsHint') }}</p>
                </div>
              </div>
              <article v-for="(issue, i) in readableWarnings" :key="'w'+i" class="issue-card issue-card-warning">
                <div class="issue-icon" aria-hidden="true">i</div>
                <div class="issue-content">
                  <h5>{{ issue.title }}</h5>
                  <p class="issue-summary">{{ issue.summary }}</p>
                  <div v-if="issue.action" class="issue-action">
                    <span>{{ $t('taskMonitor.errorsTab.suggestion') }}</span>
                    {{ issue.action }}
                  </div>
                  <details class="issue-details">
                    <summary>{{ $t('taskMonitor.errorsTab.technicalDetails') }}</summary>
                    <code>{{ issue.raw }}</code>
                  </details>
                </div>
              </article>
            </section>

            <details v-if="infoNotices.length > 0" class="run-records">
              <summary>{{ $t('taskMonitor.errorsTab.runRecords', { count: infoNotices.length }) }}</summary>
              <div class="run-record-list">
                <div v-for="(notice, i) in infoNotices" :key="'info'+i" class="run-record-item">
                  <span class="run-record-dot"></span>
                  <div>
                    <strong>{{ notice.title }}</strong>
                    <p>{{ notice.summary }}</p>
                  </div>
                </div>
              </div>
            </details>
          </div>
        </div>
      </main>
    </div>

    <!-- ── Error Banner ────────────────────────────────────── -->
    <Transition name="fade">
      <div v-if="wsError" class="card error-banner">
        <span class="badge badge-error">{{ $t('taskMonitor.errorBanner') }}</span>
        <span class="error-text">{{ wsError }}</span>
        <button class="btn btn-ghost btn-sm" @click="connect" style="margin-left:auto;">{{ $t('taskMonitor.retry') }}</button>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, reactive } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { storeToRefs } from 'pinia'
import { useTaskStore } from '../stores/task.js'
import { NODE_DEFS, TOTAL_NODES } from '../utils/nodeDefs.js'
import { translateError } from '../utils/failureHints.js'
import { useCountUp } from '../utils/useCountUp.js'
import { api } from '../utils/api.js'

import NodeTimeline from '../components/task/NodeTimeline.vue'
import LLMThoughts from '../components/task/LLMThoughts.vue'
import ArtifactPreview from '../components/task/ArtifactPreview.vue'
import ValidationPanel from '../components/task/ValidationPanel.vue'
import ExecutionPanel from '../components/task/ExecutionPanel.vue'
import LifecycleFeed from '../components/task/LifecycleFeed.vue'
import KGSnapshot from '../components/task/KGSnapshot.vue'
import StageOperations from '../components/task/StageOperations.vue'

const route = useRoute()
const id = route.params.id

/* ── Global task store: shared WS connection + live state ──
   One connection per task context shared by every page; HITL events
   trigger immediate count refreshes and throttled full-state reloads
   inside the store, so this page (and the header badge, and the
   interrupt notice) stay live without a manual refresh. */
const store = useTaskStore()
const {
  wsConnected, wsEvents, currentPhase, currentStatus, interruptType,
  wsError, wsTimeline, wsThoughts, currentNode,
  task, fullState, countsLoaded,
  allTimeline, isAwaitingHuman,
  displayClarificationCount, displayApprovalCount,
} = storeToRefs(store)
const connect = () => store.connect()

/* Raw phase strings like "human_approval" overflow stat cards; render a
   friendly Title Case label instead. */
function formatPhase(p) {
  if (!p) return '—'
  return String(p).split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

/* The PG task.phase column lags behind real progress (it stayed on
   "design_ods" while the timeline was already deploying). Derive the phase
   card from the freshest source: the live node while running, the last
   timeline node for finished tasks, then the persisted phase as fallback. */
const displayPhase = computed(() => {
  if (currentNode.value) return currentNode.value
  if (currentPhase.value && isLive.value) return currentPhase.value
  const tl = allTimeline.value
  if (tl.length) return tl[tl.length - 1].node
  return currentPhase.value || task.value?.phase || fullState.value?.task?.phase
})

const activeTab = ref('overview')
const selectedNodeDetail = ref(null)
const persistedThoughts = ref([])  // LLM traces loaded from API for completed tasks
const hasStageOperations = ref(false)

// Recovery state
const recovering = ref(false)
const recoveryMsg = ref('')
const recoveryOk = ref(false)
const cancelling = ref(false)

const { t } = useI18n()

function readableIssue(message, type = 'warning') {
  const raw = String(message || '').trim()
  const base = { raw, kind: type, title: '', summary: '', action: '' }
  let match

  if (/Critical design invariants failed/i.test(raw)) {
    return {
      ...base,
      title: t('taskMonitor.errorsTab.messages.designBlockedTitle'),
      summary: t('taskMonitor.errorsTab.messages.designBlockedSummary'),
      action: t('taskMonitor.errorsTab.messages.designBlockedAction'),
    }
  }

  match = raw.match(/Explored\s+'[^']+(?:#(\w+))?':\s*(\d+)\s+tables?,\s*(\d+)\s+relationships?/i)
  if (match) {
    const layer = (match[1] || '').toUpperCase() || t('taskMonitor.errorsTab.messages.sourceDatabase')
    return {
      ...base,
      kind: 'info',
      title: t('taskMonitor.errorsTab.messages.exploredTitle', { layer }),
      summary: t('taskMonitor.errorsTab.messages.exploredSummary', { tables: match[2], relations: match[3] }),
    }
  }

  match = raw.match(/Detected\s+(\d+)\s+potentially sensitive fields?/i)
  if (match) {
    return {
      ...base,
      kind: 'info',
      title: t('taskMonitor.errorsTab.messages.sensitiveTitle'),
      summary: t('taskMonitor.errorsTab.messages.sensitiveSummary', { count: match[1] }),
    }
  }

  if (/Source tables are from existing warehouse layer|no DWD designs generated/i.test(raw)) {
    return {
      ...base,
      kind: 'info',
      title: t('taskMonitor.errorsTab.messages.reuseWarehouseTitle'),
      summary: t('taskMonitor.errorsTab.messages.reuseWarehouseSummary'),
    }
  }

  match = raw.match(/\[Sandbox\]\s*([^:]+):\s*(\d+)\/(\d+)\s+SQL artifacts INVALID/i)
  if (match) {
    return {
      ...base,
      kind: 'error',
      title: t('taskMonitor.errorsTab.messages.sqlInvalidTitle'),
      summary: t('taskMonitor.errorsTab.messages.sqlInvalidSummary', { invalid: match[2], total: match[3] }),
      action: t('taskMonitor.errorsTab.messages.sqlInvalidAction'),
    }
  }

  if (/Grain .* requires both columns|missing in provided design/i.test(raw)) {
    return {
      ...base,
      kind: 'error',
      title: t('taskMonitor.errorsTab.messages.grainMissingTitle'),
      summary: t('taskMonitor.errorsTab.messages.grainMissingSummary'),
      action: t('taskMonitor.errorsTab.messages.grainMissingAction'),
    }
  }

  if (/UNIQUE KEY must list exactly|ordered prefix of the schema/i.test(raw)) {
    return {
      ...base,
      kind: 'error',
      title: t('taskMonitor.errorsTab.messages.keyOrderTitle'),
      summary: t('taskMonitor.errorsTab.messages.keyOrderSummary'),
      action: t('taskMonitor.errorsTab.messages.keyOrderAction'),
    }
  }

  match = raw.match(
    /(?:materialized targets are empty|物化表没有数据)\s*:\s*(.+)/i,
  )
  if (match) {
    const tables = String(match[1] || '').trim()
    const none = /no DWS\/ADS targets found|未找到/i.test(tables)
    return {
      ...base,
      kind: 'error',
      title: t('taskMonitor.errorsTab.messages.emptyTargetsTitle'),
      summary: none
        ? t('taskMonitor.errorsTab.messages.emptyTargetsNoneSummary')
        : t('taskMonitor.errorsTab.messages.emptyTargetsSummary', { tables }),
      action: t('taskMonitor.errorsTab.messages.emptyTargetsAction'),
    }
  }

  const isChinese = /[\u3400-\u9fff]/.test(raw)
  return {
    ...base,
    title: type === 'error'
      ? t('taskMonitor.errorsTab.messages.genericErrorTitle')
      : t('taskMonitor.errorsTab.messages.genericWarningTitle'),
    summary: isChinese && raw.length <= 160
      ? raw
      : (type === 'error'
          ? t('taskMonitor.errorsTab.messages.genericErrorSummary')
          : t('taskMonitor.errorsTab.messages.genericWarningSummary')),
    action: type === 'error'
      ? t('taskMonitor.errorsTab.messages.genericErrorAction')
      : t('taskMonitor.errorsTab.messages.genericWarningAction'),
  }
}

/* ── Tabs definition ──────────────────────────────────── */
const tabs = computed(() => {
  const items = [
    { key: 'overview', icon: '📊', label: t('taskMonitor.tabs.overview'), count: null },
    { key: 'llm', icon: '🧠', label: t('taskMonitor.tabs.llmThinking'), count: allThoughts.value.length },
    { key: 'design', icon: '📐', label: t('taskMonitor.tabs.design'), count: designArtifactCount.value },
    { key: 'validation', icon: '✅', label: t('taskMonitor.tabs.validation'), count: (fullState.value?.validation_results || []).length },
    { key: 'execution', icon: '⚡', label: t('taskMonitor.tabs.execution'), count: (fullState.value?.execution_results || []).length },
    { key: 'kg', icon: '🔗', label: t('taskMonitor.tabs.kg'), count: null },
    { key: 'errors', icon: '⚠', label: t('taskMonitor.tabs.errors'), count: errorCount.value + warningCount.value },
  ]
  if (hasStageOperations.value) {
    items.splice(1, 0, { key: 'stages', icon: '▦', label: t('taskMonitor.tabs.stages'), count: null })
  }
  return items
})

/* ── Computed counts ──────────────────────────────────── */
const allThoughts = computed(() => {
  // Merge real-time WebSocket thoughts with persisted LLM traces from API.
  // WebSocket events take precedence (more complete summaries) but persisted
  // traces fill the gap when the task page is opened after completion.
  const ws = wsThoughts.value || []
  const persisted = persistedThoughts.value || []

  // Build a set of signatures already present in wsThoughts to deduplicate
  const seen = new Set()
  for (const t of ws) {
    seen.add(`${t.node || ''}::${t.prompt_summary || ''}::${t.response_summary || ''}`)
  }

  const merged = [...ws]
  for (const t of persisted) {
    const sig = `${t.node || ''}::${t.prompt_summary || ''}::${t.response_summary || ''}`
    if (!seen.has(sig)) {
      merged.push(t)
      seen.add(sig)
    }
  }

  // Sort by implicit order (persisted traces come with created_at if available)
  return merged
})

const wsEventsList = computed(() => wsEvents.value || [])

const isLive = computed(() => {
  const live = ['pending', 'running', 'exploring', 'designing', 'validating', 'deploying', 'executing', 'grilling', 'awaiting_approval']
  const status = currentStatus.value || task.value?.status || fullState.value?.task?.status || ''
  return live.includes(status)
})

const effectiveStatus = computed(() =>
  currentStatus.value || task.value?.status || fullState.value?.task?.status || ''
)

const isFailed = computed(() => effectiveStatus.value === 'failed')
const isStuckApproval = computed(() =>
  effectiveStatus.value === 'awaiting_approval'
  && countsLoaded.value
  && displayApprovalCount.value === 0
)

const isValidationFailure = computed(() => {
  if (fullState.value?.failure_kind === 'validation' || task.value?.failure_kind === 'validation') return true
  if (fullState.value?.validation_blocking) return true
  return (fullState.value?.errors || []).some(message =>
    /Critical design invariants failed|no SQL artifacts were generated|Missing design plan/i.test(String(message))
  )
})

/** Plain-language summary of why the task failed (shown in the banner). */
const failureHint = computed(() => {
  if (!isFailed.value) return ''
  const errors = fullState.value?.errors || []
  if (!errors.length) return ''
  return translateError(errors[errors.length - 1])
})

const statusBadgeClass = computed(() => {
  const map = {
    exploring: 'badge-info', designing: 'badge-info',
    validating: 'badge-warning', deploying: 'badge-warning',
    executing: 'badge-warning', running: 'badge-warning',
    grilling: 'badge-info', awaiting_approval: 'badge-warning',
    completed: 'badge-success', failed: 'badge-error',
    cancelled: 'badge-neutral', pending: 'badge-neutral',
  }
  return map[currentStatus.value] || map[fullState.value?.task?.status] || 'badge-neutral'
})

const totalNodes = TOTAL_NODES
const completedNodes = computed(() => {
  // Clamp to known nodes — duplicated timeline entries (pre-fix crash
  // recoveries) must not push the counter past the total.
  const known = new Set(NODE_DEFS.map(d => d.key))
  const done = new Set(
    allTimeline.value.filter(t => t.status === 'completed' && known.has(t.node)).map(t => t.node)
  )
  return Math.min(done.size, totalNodes)
})

const clarificationsTotal = computed(() => {
  const c = fullState.value?.clarifications || []
  return c.length
})

const approvalsTotal = computed(() => {
  const a = fullState.value?.approval_items || []
  return a.length
})

const formattedErrors = computed(() =>
  (fullState.value?.errors || []).map(message => readableIssue(message, 'error'))
)
const formattedWarnings = computed(() =>
  (fullState.value?.warnings || []).map(message => readableIssue(message, 'warning'))
)
const readableErrors = computed(() => [
  ...formattedErrors.value,
  ...formattedWarnings.value.filter(issue => issue.kind === 'error'),
])
const readableWarnings = computed(() => formattedWarnings.value.filter(issue => issue.kind === 'warning'))
const infoNotices = computed(() => formattedWarnings.value.filter(issue => issue.kind === 'info'))
const errorCount = computed(() => readableErrors.value.length)
const warningCount = computed(() => readableWarnings.value.length)

/* Animated stat counters */
const completedNodesAnim = useCountUp(completedNodes)
const clarificationsAnim = useCountUp(clarificationsTotal)
const approvalsAnim = useCountUp(approvalsTotal)
const errorCountAnim = useCountUp(errorCount)
const warningCountAnim = useCountUp(warningCount)
const recoveryCountAnim = useCountUp(computed(() => fullState.value?.recovery_count || 0))

const designArtifactCount = computed(() => {
  const plan = fullState.value?.design_plan
  if (!plan) return 0
  return (plan.sql_artifacts || []).length
})

// Once the dedicated endpoints have loaded, zero is a real count. Using `||`
// here made zero fall back to the historical total and kept the interrupt
// banner visible after every question had been answered.
// (display counts come from the store's countsLoaded-aware getters)

/* ── Node selection ───────────────────────────────────── */
function onSelectNode(node) {
  selectedNodeDetail.value = node
}

/* ── Data loading ─────────────────────────────────────── */
async function loadLLMTraces() {
  try {
    const result = await api.getLLMTraces(id, '?limit=200')
    const traces = result?.traces || []
    // Map llm_trace rows to the thought format expected by LLMThoughts.vue
    persistedThoughts.value = traces.map(t => ({
      node: t.node || '',
      prompt_summary: (t.user_message || '').slice(0, 300),
      response_summary: (t.response || '').slice(0, 300),
      duration_ms: t.duration_ms || 0,
      created_at: t.created_at || '',
    }))
  } catch (e) {
    // Non-critical — thoughts are supplemental observability
    console.warn('Failed to load LLM traces:', e)
  }
}

async function loadStageAvailability() {
  try {
    const result = await api.getTaskStages(id)
    hasStageOperations.value = (result?.stages || []).length > 0
  } catch (error) {
    // Stage operations are additive; legacy monitoring must remain available.
    hasStageOperations.value = false
    console.warn('Failed to detect stage operations:', error)
  }
}

async function doRecover() {
  recovering.value = true
  recoveryMsg.value = ''
  try {
    const result = await api.recoverTask(id)
    if (result.status === 'recovering') {
      recoveryOk.value = true
      recoveryMsg.value = t('taskMonitor.recoveryMsg.started')
      // Reconnect WebSocket and refresh state after a short delay
      setTimeout(() => {
        store.connect()
        store.loadTask()
        store.loadFullState()
        loadLLMTraces()
        store.refreshCounts()
      }, 1500)
    } else {
      recoveryOk.value = false
      recoveryMsg.value = result.message || t('taskMonitor.recoveryMsg.notStarted')
    }
  } catch (e) {
    recoveryOk.value = false
    recoveryMsg.value = e.message || t('taskMonitor.recoveryMsg.failed')
  } finally {
    recovering.value = false
  }
}

async function doCancel() {
  if (!window.confirm(t('taskMonitor.cancelConfirm'))) return
  cancelling.value = true
  try {
    await api.cancelTask(id)
    currentStatus.value = 'cancelled'
    await Promise.all([store.loadTask(), store.loadFullState()])
  } catch (e) {
    window.alert(`${t('taskMonitor.cancelFailed')}: ${e.message}`)
  } finally {
    cancelling.value = false
  }
}

/* ── Lifecycle ────────────────────────────────────────── */
onMounted(async () => {
  // Bind the global store to this task: loads task/full-state/counts and
  // connects the shared WebSocket when the task is active.
  store.setContext(id)

  // Load persisted LLM traces (backfills thinking data for completed tasks
  // where WebSocket events are no longer available)
  await Promise.all([loadLLMTraces(), loadStageAvailability()])
})

// Poll counts when in interrupt state (WS events already trigger instant
// refreshes — this is the fallback for a dropped connection)
let pollTimer = null
watch(isAwaitingHuman, (val) => {
  if (val) {
    store.refreshCounts()
    pollTimer = setInterval(() => store.refreshCounts(), 5000)
  } else {
    clearInterval(pollTimer)
    pollTimer = null
  }
})

onUnmounted(() => {
  clearInterval(pollTimer)
  pollTimer = null
})
</script>

<style scoped>
/* ── Top Bar ──────────────────────────────────────────── */
.top-bar {
  display: flex;
  align-items: flex-start;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
}

.task-title {
  margin: 0;
  font-size: 1.25rem;
}

.task-id-badge {
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
  font-size: 0.75rem;
  font-weight: 400;
  background: var(--color-bg);
  padding: 2px 8px;
  border-radius: var(--radius-xs);
  margin-left: var(--space-sm);
}

.task-goal-text {
  max-width: 480px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin: 4px 0 0;
  color: var(--color-text-secondary);
  font-size: 0.875rem;
}

.header-badges {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-shrink: 0;
}

.flex-1 { flex: 1; }

/* ── Interrupt Banner ─────────────────────────────────── */
.interrupt-banner {
  display: flex;
  align-items: flex-start;
  gap: var(--space-md);
  background: linear-gradient(135deg, var(--color-warning-bg, #fff3cd), var(--color-bg-glass));
  border: 2px solid var(--color-warning);
  border-radius: var(--radius-lg);
  padding: var(--space-lg);
  margin-bottom: var(--space-lg);
  backdrop-filter: blur(12px) saturate(1.4);
  -webkit-backdrop-filter: blur(12px) saturate(1.4);
  box-shadow: 0 8px 32px rgba(255,149,0,0.18);
  animation: interrupt-pulse 2s ease-in-out infinite;
}

@keyframes interrupt-pulse {
  0%, 100% { border-color: var(--color-warning); }
  50% { border-color: var(--color-warning); }
}

.interrupt-icon {
  font-size: 2rem;
  flex-shrink: 0;
  animation: interrupt-wave 1.5s ease-in-out infinite;
}

@keyframes interrupt-wave {
  0%, 100% { transform: rotate(0deg); }
  25% { transform: rotate(-10deg); }
  75% { transform: rotate(10deg); }
}

.interrupt-body { flex: 1; }

.interrupt-title {
  margin: 0 0 var(--space-xs);
  font-size: 1.125rem;
  color: var(--color-text);
}

.interrupt-desc {
  margin: 0 0 var(--space-md);
  font-size: 0.875rem;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.interrupt-actions {
  display: flex;
  gap: var(--space-sm);
  flex-wrap: wrap;
}

/* ── Dashboard Layout ─────────────────────────────────── */
.dashboard-layout {
  display: flex;
  gap: var(--space-lg);
  margin-top: var(--space-lg);
}

/* ── Sidebar Tabs ─────────────────────────────────────── */
.sidebar-tabs {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 160px;
  max-width: 180px;
  flex-shrink: 0;
}

.sidebar-divider {
  height: 1px;
  background: var(--color-border);
  margin: var(--space-sm) 0;
}

.tab-btn {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: 8px 12px;
  border: none;
  border-radius: var(--radius-md);
  background: transparent;
  cursor: pointer;
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  transition: all var(--duration-fast) var(--spring-default);
  text-decoration: none;
  text-align: left;
  width: 100%;
}

.tab-btn:hover {
  background: var(--color-bg);
  color: var(--color-text);
}

.tab-active {
  background: var(--color-accent-bg, rgba(0,122,255,0.08)) !important;
  color: var(--color-accent) !important;
  font-weight: 500;
}

.tab-link:hover {
  color: var(--color-accent);
}

/* Quick links read as "external pages", not tabs */
.tab-link {
  border: 1px dashed var(--color-border);
  margin-bottom: 2px;
}

.tab-link:hover {
  border-color: var(--color-accent);
  background: var(--color-accent-glass);
}

.sidebar-section-label {
  font-size: 0.625rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--color-text-quaternary);
  padding: 0 12px;
  margin-bottom: 4px;
}

.auto-tag {
  font-size: 0.625rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  padding: 1px 6px;
  border-radius: 10px;
  background: var(--color-accent-glass);
  color: var(--color-accent);
}

.tab-icon {
  font-size: 1rem;
  flex-shrink: 0;
  width: 22px;
  text-align: center;
}

.tab-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tab-count {
  font-size: 0.6875rem;
  padding: 1px 6px;
  border-radius: 10px;
  background: var(--color-bg);
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.tab-active .tab-count {
  background: var(--color-accent);
  color: white;
}

.count-badge {
  font-size: 0.625rem;
  padding: 1px 5px;
  border-radius: 8px;
  background: var(--color-warning);
  color: white;
  font-weight: 600;
}

.tab-ext {
  font-size: 0.625rem;
  color: var(--color-text-tertiary);
  opacity: 0;
  transition: opacity var(--duration-fast);
}

.tab-link:hover .tab-ext {
  opacity: 1;
}

/* ── Tab Content ──────────────────────────────────────── */
.tab-content {
  flex: 1;
  min-width: 0;
}

.tab-panel {
  animation: tab-in 0.3s var(--spring-default);
}

@keyframes tab-in {
  from { opacity: 0; transform: translateX(4px); }
}

/* ── Overview Grid ────────────────────────────────────── */
.overview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
}

.stat-card {
  padding: var(--space-md);
  text-align: center;
  transition:
    transform var(--duration-base) var(--spring-default),
    box-shadow var(--duration-base) var(--spring-default);
  animation: stat-card-enter 0.45s var(--spring-default) both;
}
.stat-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-elevated);
}
.stat-card:nth-child(1) { animation-delay: 0.02s; }
.stat-card:nth-child(2) { animation-delay: 0.06s; }
.stat-card:nth-child(3) { animation-delay: 0.10s; }
.stat-card:nth-child(4) { animation-delay: 0.14s; }
.stat-card:nth-child(5) { animation-delay: 0.18s; }
.stat-card:nth-child(6) { animation-delay: 0.22s; }
.stat-card:nth-child(7) { animation-delay: 0.26s; }

@keyframes stat-card-enter {
  from {
    opacity: 0;
    transform: translateY(10px) scale(0.97);
  }
}

.stat-label {
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: var(--space-xs);
}

.stat-value {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
  word-break: break-word;
  overflow-wrap: anywhere;
  min-width: 0;
}

.text-error { color: var(--color-error) !important; }
.text-warning { color: var(--color-warning) !important; }

/* ── Human-readable issues ────────────────────────────── */
.issue-section { margin-bottom: var(--space-xl); }

.issue-section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: var(--space-md);
}

.issue-section-title { margin: 0 0 4px; font-size: 1rem; }
.issue-section-heading p { margin: 0; color: var(--color-text-tertiary); font-size: 0.8125rem; }

.issue-card {
  display: flex;
  align-items: flex-start;
  gap: var(--space-md);
  padding: var(--space-lg);
  margin-bottom: var(--space-sm);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-bg);
}

.issue-card-error { border-left: 3px solid var(--color-error); }
.issue-card-warning { border-left: 3px solid var(--color-warning); }

.issue-icon {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  flex: 0 0 24px;
  border-radius: 50%;
  font-size: 0.75rem;
  font-weight: 700;
  color: white;
  background: var(--color-warning);
}

.issue-card-error .issue-icon { background: var(--color-error); }
.issue-content { min-width: 0; flex: 1; }
.issue-content h5 { margin: 1px 0 6px; font-size: 0.9375rem; color: var(--color-text); }
.issue-summary { margin: 0; line-height: 1.65; color: var(--color-text-secondary); }

.issue-action {
  margin-top: var(--space-sm);
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--color-bg-secondary);
  color: var(--color-text-secondary);
  font-size: 0.8125rem;
  line-height: 1.6;
}

.issue-action span { margin-right: 6px; font-weight: 600; color: var(--color-text); }

.issue-details { margin-top: var(--space-sm); font-size: 0.75rem; color: var(--color-text-tertiary); }
.issue-details summary { cursor: pointer; user-select: none; }
.issue-details code {
  display: block;
  margin-top: 8px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--color-bg-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.55;
}

.run-records {
  padding: var(--space-md) var(--space-lg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  color: var(--color-text-secondary);
}

.run-records > summary { cursor: pointer; font-weight: 600; }
.run-record-list { margin-top: var(--space-md); }
.run-record-item { display: flex; gap: 10px; padding: 8px 0; }
.run-record-item strong { font-size: 0.8125rem; color: var(--color-text); }
.run-record-item p { margin: 2px 0 0; font-size: 0.75rem; color: var(--color-text-tertiary); }
.run-record-dot { width: 6px; height: 6px; margin-top: 7px; border-radius: 50%; background: var(--color-success); flex: 0 0 6px; }

/* ── Error Banner ─────────────────────────────────────── */
.error-banner {
  margin-top: var(--space-md);
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  border-left: 4px solid var(--color-error);
  animation: shake-in 0.4s var(--spring-default);
}

@keyframes shake-in {
  0%   { transform: translateX(-4px); opacity: 0; }
  25%  { transform: translateX(3px); }
  50%  { transform: translateX(-2px); }
  75%  { transform: translateX(1px); }
  100% { transform: translateX(0); opacity: 1; }
}

.error-text {
  color: var(--color-error);
  font-size: 0.875rem;
}

/* ── Recovery Banner ───────────────────────────────────── */
.recovery-banner {
  display: flex;
  align-items: flex-start;
  gap: var(--space-md);
  background: linear-gradient(135deg, var(--color-accent-bg, rgba(0,122,255,0.06)), var(--color-bg));
  border: 2px solid var(--color-accent, #007AFF);
  border-radius: var(--radius-lg);
  padding: var(--space-lg);
  margin-bottom: var(--space-lg);
  animation: recovery-pulse 2.5s ease-in-out infinite;
}

@keyframes recovery-pulse {
  0%, 100% { border-color: var(--color-accent, #007AFF); }
  50% { border-color: var(--color-accent-hover); }
}

.recovery-icon {
  font-size: 2rem;
  flex-shrink: 0;
  animation: recovery-spin 3s linear infinite;
}

@keyframes recovery-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.recovery-body { flex: 1; }

.recovery-title {
  margin: 0 0 var(--space-xs);
  font-size: 1.125rem;
  color: var(--color-text);
}

.recovery-desc {
  margin: 0 0 var(--space-md);
  font-size: 0.875rem;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.recovery-failure-hint {
  margin: 0 0 var(--space-md);
  padding: var(--space-sm) var(--space-md);
  font-size: 0.8125rem;
  line-height: 1.6;
  color: var(--color-error, #FF3B30);
  background: rgba(255, 59, 48, 0.08);
  border-radius: var(--radius-sm, 6px);
}

.recovery-attempts {
  font-weight: 600;
  color: var(--color-accent);
}

.recovery-actions {
  display: flex;
  gap: var(--space-sm);
  flex-wrap: wrap;
  align-items: center;
}

.recovery-msg {
  margin: var(--space-sm) 0 0;
  font-size: 0.8125rem;
}

.text-success { color: var(--color-success, #34C759) !important; }

/* ── Responsive ───────────────────────────────────────── */
@media (max-width: 768px) {
  .dashboard-layout {
    flex-direction: column;
  }

  .sidebar-tabs {
    flex-direction: row;
    flex-wrap: wrap;
    min-width: 0;
    max-width: none;
    gap: 4px;
  }

  .tab-btn {
    width: auto;
  }

  .tab-label {
    display: none;
  }

  .sidebar-divider {
    display: none;
  }

  .tab-ext {
    display: none;
  }
}

/* ── Badge dot animation ──────────────────────────────── */
.badge-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: dot-pulse 1.5s ease-in-out infinite;
}

@keyframes dot-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}
</style>
