<template>
  <div class="execution-panel">
    <div class="section-header">
      <h4 class="mb-0">{{ $t('components.execution.header') }}</h4>
      <span class="section-count">{{ $t('components.execution.tasks', { count: results.length }) }}</span>
    </div>

    <div v-if="results.length === 0" class="empty-state" style="padding:var(--space-xl);">
      <div class="empty-state-icon">⚡</div>
      <div class="empty-state-title">{{ $t('components.execution.noResults') }}</div>
      <div class="empty-state-desc">{{ $t('components.execution.noResultsDesc') }}</div>
    </div>

    <div v-else>
      <div class="exec-summary">
        <span class="badge badge-success">{{ $t('components.execution.success', { count: successCount }) }}</span>
        <span v-if="failCount" class="badge badge-error">{{ $t('components.execution.failed', { count: failCount }) }}</span>
        <span class="exec-total-rows" v-if="totalRows">{{ $t('components.execution.totalRows', { count: totalRows.toLocaleString() }) }}</span>
      </div>

      <div class="exec-table-wrap">
        <table class="exec-table">
          <thead>
            <tr>
              <th>{{ $t('components.execution.taskInstance') }}</th>
              <th>{{ $t('components.execution.status') }}</th>
              <th>{{ $t('components.execution.duration') }}</th>
              <th>{{ $t('components.execution.rows') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in results" :key="i" :class="{ 'row-failed': !r.success }">
              <td class="exec-task-id">{{ (r.task_instance_id || '').slice(0, 20) }}</td>
              <td>
                <span :class="['badge', r.success ? 'badge-success' : 'badge-error']">
                  {{ r.success ? $t('components.execution.ok') : $t('components.execution.fail') }}
                </span>
              </td>
              <td class="exec-duration">{{ computeDuration(r.start_time, r.end_time) }}</td>
              <td class="exec-rows">{{ summarizeRows(r.rows_written) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Error classifications -->
      <div v-if="errorClassifications.length > 0" class="error-class-section">
        <h5 class="mb-sm">{{ $t('components.execution.errorClassifications') }}</h5>
        <div v-for="(e, ei) in errorClassifications" :key="ei" class="error-class-card">
          <div class="error-class-header">
            <span :class="['badge', errorTypeBadge(e.error_type)]">{{ e.error_type }}</span>
            <span v-if="e.llm_classified" class="badge badge-info ml-xs">LLM</span>
            <span class="error-class-summary">{{ e.summary }}</span>
            <span class="error-class-action">{{ e.action }}</span>
          </div>
          <div v-if="e.details || e.fix_suggestion || e.raw_log" class="error-class-body">
            <div v-if="e.details" class="error-detail">
              <strong>{{ $t('components.execution.details') }}:</strong>
              <p>{{ e.details }}</p>
            </div>
            <div v-if="e.fix_suggestion" class="error-fix">
              <strong>{{ $t('components.execution.fixSuggestion') }}:</strong>
              <p>{{ e.fix_suggestion }}</p>
            </div>
            <details v-if="e.raw_log" class="error-log-excerpt">
              <summary>{{ $t('components.execution.logExcerpt') }}</summary>
              <pre>{{ extractErrorExcerpt(e.raw_log) }}</pre>
            </details>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  results: { type: Array, default: () => [] },
  errorClassifications: { type: Array, default: () => [] },
})

const successCount = computed(() => props.results.filter(r => r.success).length)
const failCount = computed(() => props.results.filter(r => !r.success).length)
const totalRows = computed(() => {
  let total = 0
  for (const r of props.results) {
    if (r.rows_written) {
      for (const v of Object.values(r.rows_written)) total += Number(v) || 0
    }
  }
  return total
})

function computeDuration(start, end) {
  if (!start || !end) return '—'
  const ms = new Date(end) - new Date(start)
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${(ms / 60000).toFixed(1)}m`
}

function summarizeRows(rows) {
  if (!rows || Object.keys(rows).length === 0) return '—'
  const total = Object.values(rows).reduce((s, v) => s + Number(v), 0)
  return total.toLocaleString()
}

function errorTypeBadge(type) {
  const map = { transient: 'badge-warning', resource: 'badge-warning', data: 'badge-error', logic: 'badge-error', config: 'badge-error', dependency: 'badge-error' }
  return map[type] || 'badge-neutral'
}

// Pull the real error lines out of the full DS log (the failing SQL error,
// exception stack, etc.) so the expander shows detail, not the whole 100KB log.
function extractErrorExcerpt(rawLog) {
  if (!rawLog) return ''
  const lines = rawLog.split('\n')
  const errorPattern = /(ERROR|errCode|SQLException|detailMessage|Exception|Traceback)/i
  const matched = lines.filter(l => errorPattern.test(l))
  const tail = lines.slice(-15)
  return [...new Set([...matched, ...tail])].slice(0, 40).join('\n')
}
</script>

<style scoped>
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-md);
}

.mb-0 { margin-bottom: 0; }
.mb-sm { margin-bottom: var(--space-sm); }

.section-count {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.exec-summary {
  display: flex;
  gap: var(--space-sm);
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: var(--space-md);
}

.exec-total-rows {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  margin-left: auto;
}

.exec-table-wrap {
  overflow-x: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.exec-table {
  width: 100%;
  font-size: 0.8125rem;
  border-collapse: collapse;
}

.exec-table th {
  text-align: left;
  padding: var(--space-xs) var(--space-sm);
  background: var(--color-bg);
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 1px solid var(--color-border);
}

.exec-table td {
  padding: var(--space-xs) var(--space-sm);
  border-bottom: 1px solid var(--color-border);
}

.row-failed { background: rgba(255,59,48,0.03); }

.exec-task-id {
  font-family: var(--font-mono);
  font-size: 0.71875rem;
}

.exec-duration {
  font-variant-numeric: tabular-nums;
  font-size: 0.75rem;
  color: var(--color-text-secondary);
}

.exec-rows {
  font-variant-numeric: tabular-nums;
  font-size: 0.75rem;
}

.error-class-section {
  margin-top: var(--space-lg);
}

.error-class-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  padding: var(--space-xs) var(--space-sm);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  margin-bottom: 4px;
  font-size: 0.8125rem;
}

.error-class-header {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  width: 100%;
}

.error-class-summary {
  flex: 1;
  color: var(--color-text-secondary);
}

.error-class-action {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
}

.error-class-body {
  padding-top: var(--space-xs);
  font-size: 0.8125rem;
  line-height: 1.55;
}

.error-detail,
.error-fix {
  margin-bottom: var(--space-xs);
}

.error-detail p,
.error-fix p {
  margin: 2px 0 0;
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.error-log-excerpt pre {
  margin-top: var(--space-xs);
  padding: var(--space-sm);
  background: var(--color-bg-secondary);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 0.71875rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 400px;
  overflow-y: auto;
}

.ml-xs { margin-left: var(--space-xs); }
</style>
