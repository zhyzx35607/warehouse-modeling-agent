<template>
  <div class="validation-panel">
    <div class="section-header">
      <h4 class="mb-0">{{ $t('components.validation.header') }}</h4>
      <span class="section-count">{{ $t('components.validation.checks', { count: results.length }) }}</span>
    </div>

    <div v-if="results.length === 0" class="empty-state" style="padding:var(--space-xl);">
      <div class="empty-state-icon">✅</div>
      <div class="empty-state-title">{{ $t('components.validation.noResults') }}</div>
      <div class="empty-state-desc">{{ $t('components.validation.noResultsDesc') }}</div>
    </div>

    <div v-else>
      <div class="validation-summary">
        <span class="badge badge-success">{{ $t('components.validation.passed', { count: passedCount }) }}</span>
        <span v-if="failedCount" class="badge badge-error">{{ $t('components.validation.failed', { count: failedCount }) }}</span>
        <span v-if="pendingCount" class="badge badge-warning">{{ $t('components.validation.pending', { count: pendingCount }) }}</span>
      </div>

      <div v-for="(r, i) in results" :key="i" class="val-card" :class="{ 'val-failed': r.passed === false, 'val-passed': r.passed, 'val-pending': r.passed === null }">
        <div class="val-card-header">
          <span class="val-status-icon">
            {{ r.passed === false ? '✗' : r.passed ? '✓' : '○' }}
          </span>
          <span class="badge" :class="layerBadgeClass(r.layer)">{{ readableLayer(r.layer) }}</span>
          <span class="val-level">{{ readableLevel(r.level) }}</span>
          <span class="val-time" v-if="r.executed_at">{{ formatTime(r.executed_at) }}</span>
        </div>

        <div v-if="r.failures?.length" class="val-failures">
          <div v-for="(f, fi) in r.failures" :key="fi" class="val-failure-item">
            <div class="val-failure-copy">
              <strong class="val-failure-title">{{ readableFailure(f, r.layer).title }}</strong>
              <p class="val-fail-detail">{{ readableFailure(f, r.layer).summary }}</p>
              <p class="val-fail-action">
                <span>{{ $t('components.validation.suggestion') }}</span>
                {{ readableFailure(f, r.layer).action }}
              </p>
              <details class="val-technical-detail">
                <summary>{{ $t('components.validation.technicalDetails') }}</summary>
                <code>{{ f.check }}</code>
                <p>{{ f.detail }}</p>
              </details>
            </div>
          </div>
        </div>

        <div v-if="r.semantic_assertions?.length" class="val-assertions">
          <div class="val-assert-label">{{ $t('components.validation.semanticAssertions') }}</div>
          <div v-for="(a, ai) in r.semantic_assertions" :key="ai" class="val-assert-item">
            <span>{{ a.metric }}: {{ a.expected_range }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  results: { type: Array, default: () => [] },
})

const { t } = useI18n()

const passedCount = computed(() => props.results.filter(r => r.passed === true).length)
const failedCount = computed(() => props.results.filter(r => r.passed === false).length)
const pendingCount = computed(() => props.results.filter(r => r.passed === null || r.passed === undefined).length)

function layerBadgeClass(layer) {
  const map = { ODS: 'badge-info', DIM: '', DWD: 'badge-warning', DWS: 'badge-error', ADS: 'badge-info' }
  return map[layer] || 'badge-neutral'
}

function readableLayer(layer) {
  const key = String(layer || '').toUpperCase()
  const known = ['ODS', 'DIM', 'DWD', 'DWS', 'ADS', 'DESIGN_REVIEW']
  return known.includes(key)
    ? t(`components.validation.layers.${key}`)
    : (layer || t('components.validation.design'))
}

function readableLevel(level) {
  const value = String(level || '').split('.').pop().toLowerCase()
  const known = ['structure', 'consistency', 'semantic']
  return known.includes(value)
    ? t(`components.validation.levels.${value}`)
    : t('components.validation.levels.general')
}

function failureCategory(check = '') {
  const key = String(check).toLowerCase().replaceAll('-', '_')
  if (/warehouse_sources_qualified|cross_database_source/.test(key)) return 'sourceQualification'
  if (/ads_dws_lineage|upstream_measure_validation/.test(key)) return 'sourceLineage'
  if (/metric_lineage|atomic_metric|parent_metrics|metric_registry/.test(key)) return 'metricLineage'
  if (/grain/.test(key) && !/unique_key/.test(key)) return 'grain'
  if (/unique_key|ordered_prefix/.test(key)) return 'uniqueKey'
  if (/comment/.test(key)) return 'comments'
  if (/load_sql|materialization_sql_complete|ddl_completeness|ddl_missing/.test(key)) return 'materialization'
  if (/dimension_columns|dimensions_declared|metric_fields_materialized/.test(key)) return 'dimensions'
  if (/sql_not_empty|valid_statement_type|double_semicolon/.test(key)) return 'sql'
  return 'generic'
}

function readableFailure(failure, layer) {
  const category = failureCategory(failure?.check)
  const path = `components.validation.messages.${category}`
  return {
    title: t(`${path}.title`, { layer: layer || t('components.validation.design') }),
    summary: t(`${path}.summary`, { layer: layer || t('components.validation.design') }),
    action: t(`${path}.action`, { layer: layer || t('components.validation.design') }),
  }
}

function formatTime(ts) {
  if (!ts) return ''
  return new Date(ts).toLocaleTimeString('en-GB', { hour12: false })
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

.section-count {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.validation-summary {
  display: flex;
  gap: var(--space-sm);
  margin-bottom: var(--space-md);
  flex-wrap: wrap;
}

.val-card {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-sm) var(--space-md);
  margin-bottom: var(--space-sm);
}

.val-failed { border-left: 3px solid var(--color-error); }
.val-passed { border-left: 3px solid var(--color-success); }
.val-pending { border-left: 3px solid var(--color-warning); }

.val-card-header {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.val-status-icon {
  font-size: 0.875rem;
  width: 18px;
  text-align: center;
}

.val-passed .val-status-icon { color: var(--color-success); }
.val-failed .val-status-icon { color: var(--color-error); }
.val-pending .val-status-icon { color: var(--color-warning); }

.val-level {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
}

.val-time {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  margin-left: auto;
}

.val-failures {
  margin-top: var(--space-sm);
  padding-left: 26px;
  display: grid;
  gap: var(--space-sm);
}

.val-failure-item {
  font-size: 0.8125rem;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid color-mix(in srgb, var(--color-error) 18%, var(--color-border));
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--color-error) 3%, transparent);
}

.val-failure-title {
  display: block;
  color: var(--color-text-primary);
  font-size: 0.875rem;
  margin-bottom: 4px;
}

.val-fail-detail {
  color: var(--color-text-secondary);
  line-height: 1.6;
  margin: 0;
}

.val-fail-action {
  color: var(--color-text-secondary);
  line-height: 1.55;
  margin: 6px 0 0;
}

.val-fail-action span {
  color: var(--color-error);
  font-weight: 600;
  margin-right: 4px;
}

.val-technical-detail {
  margin-top: 6px;
  color: var(--color-text-tertiary);
  font-size: 0.75rem;
}

.val-technical-detail summary {
  cursor: pointer;
  width: fit-content;
}

.val-technical-detail code {
  display: inline-block;
  margin-top: 6px;
  color: var(--color-error);
  font-size: 0.6875rem;
}

.val-technical-detail p {
  margin: 4px 0 0;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.val-assertions {
  margin-top: var(--space-sm);
  padding-left: 26px;
}

.val-assert-label {
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  margin-bottom: 2px;
}

.val-assert-item {
  font-size: 0.75rem;
  color: var(--color-text-secondary);
  padding: 1px 0;
}
</style>
