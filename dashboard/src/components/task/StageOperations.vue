<template>
  <div class="stage-operations">
    <div v-if="loading" class="stage-loading">{{ $t('taskMonitor.loading') }}</div>
    <template v-else-if="hasStages">
      <div class="stage-strip" aria-label="Modeling stages">
        <div v-for="item in stageCards" :key="item.stage" class="stage-item">
          <span :class="['stage-indicator', `is-${item.status}`]"></span>
          <div>
            <strong>{{ stageLabel(item.stage) }}</strong>
            <span>{{ statusLabel(item.status) }}</span>
          </div>
          <code v-if="item.version">v{{ item.version }}</code>
        </div>
      </div>

      <section v-if="odsTables.length" class="ops-section">
        <div class="section-heading">
          <div>
            <h3>{{ $t('taskMonitor.stages.odsRuns') }}</h3>
            <p>{{ $t('taskMonitor.stages.odsSummary', odsSummary) }}</p>
          </div>
        </div>
        <div class="ops-table-wrap">
          <table class="ops-table">
            <thead><tr>
              <th>{{ $t('taskMonitor.stages.table') }}</th>
              <th>{{ $t('taskMonitor.stages.mode') }}</th>
              <th>{{ $t('taskMonitor.stages.rows') }}</th>
              <th>{{ $t('taskMonitor.stages.attempt') }}</th>
              <th>{{ $t('tasks.status') }}</th>
              <th></th>
            </tr></thead>
            <tbody><tr v-for="run in odsTables" :key="`${run.manifest_version}:${run.table_identity}`">
              <td><code>{{ run.table_identity }}</code><small v-if="run.required">{{ $t('taskMonitor.stages.required') }}</small></td>
              <td>{{ run.sync_mode }}</td>
              <td>{{ rowCount(run) }}</td>
              <td>{{ run.attempt }} / 3</td>
              <td><span :class="['run-status', `is-${run.status}`]">{{ statusLabel(run.status) }}</span></td>
              <td>
                <button v-if="canRetry(run)" class="btn btn-secondary btn-sm" :disabled="retrying === run.table_identity" @click="retry(run)">
                  {{ retrying === run.table_identity ? $t('taskMonitor.stages.retrying') : $t('taskMonitor.stages.retryTable') }}
                </button>
                <span v-else-if="run.status === 'failed'" class="not-retryable">{{ $t('taskMonitor.stages.manualReview') }}</span>
              </td>
            </tr></tbody>
          </table>
        </div>
        <p v-if="actionError" class="action-error">{{ actionError }}</p>
      </section>

      <section v-if="assets.length" class="ops-section">
        <div class="section-heading">
          <div>
            <h3>{{ $t('taskMonitor.stages.publishedAssets') }}</h3>
            <p>{{ $t('taskMonitor.stages.assetSummary', { version: coreVersion, count: assets.length }) }}</p>
          </div>
          <span class="published-badge">{{ $t('taskMonitor.stages.published') }}</span>
        </div>
        <div class="asset-grid">
          <div v-for="asset in assets" :key="asset.id" class="asset-row">
            <span class="asset-layer">{{ asset.layer.toUpperCase() }}</span>
            <code>{{ asset.database_name }}.{{ asset.table_name }}</code>
            <span>{{ assetKind(asset.asset_kind) }}</span>
            <span v-if="asset.workflow_code">DAG {{ asset.workflow_code }}</span>
          </div>
        </div>
      </section>
    </template>
    <div v-else class="empty-state stage-empty">
      <div class="empty-state-title">{{ $t('taskMonitor.stages.notAvailable') }}</div>
      <div class="empty-state-desc">{{ $t('taskMonitor.stages.notAvailableDesc') }}</div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../../utils/api.js'

const props = defineProps({ taskId: { type: String, required: true } })
const { t } = useI18n()
const loading = ref(true)
const stageData = ref({ stages: [], artifacts: [], ods_tables: [] })
const assets = ref([])
const retrying = ref('')
const actionError = ref('')

const hasStages = computed(() => stageData.value.stages.length > 0)
const odsTables = computed(() => stageData.value.ods_tables || [])
const coreVersion = computed(() => Math.max(0, ...assets.value.map(item => item.core_model_version || 0)))
const odsSummary = computed(() => ({
  passed: odsTables.value.filter(item => item.status === 'succeeded').length,
  failed: odsTables.value.filter(item => item.status === 'failed').length,
  total: odsTables.value.length,
}))
const stageCards = computed(() => ['exploration', 'ods', 'core_model'].map(stage => {
  const run = stageData.value.stages.find(item => item.stage === stage) || { stage, status: 'pending' }
  const artifact = stageData.value.artifacts.find(item => item.stage === stage)
  return { ...run, version: artifact?.version || 0 }
}))

function stageLabel(stage) { return t(`taskMonitor.stages.names.${stage}`) }
function statusLabel(status) { return t(`taskMonitor.stages.status.${status}`, status) }
function assetKind(kind) { return t(`taskMonitor.stages.assetKinds.${kind}`, kind) }
function rowCount(run) {
  if (run.source_rows == null || run.target_rows == null) return '-'
  return `${run.source_rows} / ${run.target_rows}`
}
function canRetry(run) { return run.status === 'failed' && run.error_type === 'transient' && run.attempt < 3 }

async function load() {
  loading.value = true
  try {
    const [stages, lineage] = await Promise.all([api.getTaskStages(props.taskId), api.getTaskLineage(props.taskId)])
    stageData.value = stages
    assets.value = lineage.assets || []
  } finally { loading.value = false }
}
async function retry(run) {
  retrying.value = run.table_identity
  actionError.value = ''
  try {
    await api.retryODSTable(props.taskId, run.table_identity)
    await load()
  } catch (error) { actionError.value = error.message }
  finally { retrying.value = '' }
}
onMounted(load)
</script>

<style scoped>
.stage-loading,.stage-empty { padding: var(--space-2xl); }
.stage-strip { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); border:1px solid var(--color-border); border-radius:var(--radius-md); overflow:hidden; }
.stage-item { min-height:72px; display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:10px; padding:14px 16px; border-right:1px solid var(--color-border); }
.stage-item:last-child { border-right:0; }
.stage-item strong,.stage-item span { display:block; }
.stage-item span { color:var(--color-text-secondary); font-size:.75rem; margin-top:2px; }
.stage-indicator { width:9px; height:9px; border-radius:50%; background:var(--color-text-tertiary); }
.stage-indicator.is-succeeded { background:var(--color-success); }
.stage-indicator.is-running { background:var(--color-warning); }
.stage-indicator.is-failed,.stage-indicator.is-blocked { background:var(--color-error); }
.stage-indicator.is-partial_success { background:var(--color-warning); }
.ops-section { margin-top:var(--space-lg); border-top:1px solid var(--color-border); padding-top:var(--space-lg); }
.section-heading { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:12px; }
.section-heading h3 { margin:0; font-size:1rem; }
.section-heading p { margin:3px 0 0; color:var(--color-text-secondary); font-size:.8125rem; }
.ops-table-wrap { overflow-x:auto; }
.ops-table { width:100%; border-collapse:collapse; min-width:700px; }
.ops-table th,.ops-table td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--color-border); font-size:.8125rem; }
.ops-table th { color:var(--color-text-secondary); font-size:.6875rem; text-transform:uppercase; }
.ops-table td:first-child small { display:block; color:var(--color-warning); margin-top:3px; }
.run-status { font-weight:600; }
.run-status.is-succeeded,.published-badge { color:var(--color-success); }
.run-status.is-failed,.action-error { color:var(--color-error); }
.not-retryable { color:var(--color-text-tertiary); font-size:.75rem; }
.published-badge { border:1px solid currentColor; padding:3px 8px; border-radius:var(--radius-sm); font-size:.75rem; }
.asset-grid { display:grid; gap:1px; background:var(--color-border); border:1px solid var(--color-border); }
.asset-row { display:grid; grid-template-columns:48px minmax(220px,1fr) 90px 120px; gap:10px; padding:10px 12px; background:var(--color-bg-elevated); font-size:.8125rem; align-items:center; }
.asset-layer { font-weight:700; color:var(--color-accent); }
@media (max-width:700px) { .stage-strip { grid-template-columns:1fr; } .stage-item { border-right:0; border-bottom:1px solid var(--color-border); } .stage-item:last-child{border-bottom:0;} .asset-row{grid-template-columns:44px 1fr;} }
</style>
