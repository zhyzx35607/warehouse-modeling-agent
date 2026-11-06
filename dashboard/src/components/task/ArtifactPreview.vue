<template>
  <div class="artifact-preview">
    <div class="section-header">
      <h4 class="mb-0">{{ $t('components.artifactPreview.header') }}</h4>
      <router-link v-if="taskId" :to="`/task/${taskId}/design`" class="btn btn-ghost btn-sm">
        {{ $t('components.artifactPreview.openDesign') }}
      </router-link>
    </div>

    <div v-if="!designPlan" class="empty-state" style="padding:var(--space-xl);">
      <div class="empty-state-icon">📐</div>
      <div class="empty-state-title">{{ $t('components.artifactPreview.notGenerated') }}</div>
    </div>

    <div v-else class="design-summary">
      <!-- Layer stats -->
      <div class="layer-stats">
        <div class="layer-stat" v-for="stat in layerStats" :key="stat.label">
          <span class="layer-stat-label">{{ stat.label }}</span>
          <span class="layer-stat-value">{{ stat.count }}</span>
        </div>
      </div>

      <!-- SQL Artifacts preview -->
      <div v-if="sqlArtifacts.length > 0" class="sql-section">
        <h5 class="mb-sm">{{ $t('components.artifactPreview.sqlArtifacts', { count: sqlArtifacts.length }) }}</h5>
        <div v-for="sql in sqlArtifacts" :key="sql.id" class="sql-card">
          <div class="sql-card-header">
            <span class="badge" :class="'badge-' + (sql.layer || 'neutral')">{{ sql.layer }}</span>
            <span class="sql-table">{{ sql.target_table }}</span>
            <span class="sql-type">{{ sql.sql_type }}</span>
            <button class="btn btn-ghost btn-sm sql-expand-btn" @click="toggleSql(sql.id)">
              {{ expandedSql.has(sql.id) ? $t('design.collapseSql') : $t('design.expandSql') }}
            </button>
          </div>
          <!-- Full SQL, never truncated; long statements start collapsed. -->
          <pre class="sql-code" :class="{ 'sql-code-collapsed': !expandedSql.has(sql.id) }">{{ sql.sql }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  designPlan: { type: Object, default: null },
  taskId: { type: String, default: '' },
})

/* SQL blocks show the full statement; long ones start collapsed. */
const expandedSql = ref(new Set())
function toggleSql(sqlId) {
  const next = new Set(expandedSql.value)
  if (next.has(sqlId)) next.delete(sqlId)
  else next.add(sqlId)
  expandedSql.value = next
}

const layerStats = computed(() => {
  const p = props.designPlan
  if (!p) return []
  return [
    { label: 'ODS', count: (p.ods_designs || []).length },
    { label: 'DIM', count: (p.dim_designs || []).length },
    { label: 'DWD', count: (p.dwd_designs || []).length },
    { label: 'DWS', count: (p.dws_designs || []).length },
    { label: 'ADS', count: (p.ads_designs || []).length },
  ]
})

const sqlArtifacts = computed(() => {
  return props.designPlan?.sql_artifacts || []
})
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

.layer-stats {
  display: flex;
  gap: var(--space-sm);
  flex-wrap: wrap;
  margin-bottom: var(--space-lg);
}

.layer-stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  min-width: 64px;
}

.layer-stat-label {
  font-size: 0.6875rem;
  font-weight: 700;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.layer-stat-value {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}

.sql-section { margin-top: var(--space-md); }

.sql-card {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-sm);
  overflow: hidden;
}

.sql-card-header {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-xs) var(--space-sm);
  background: var(--color-bg);
  border-bottom: 1px solid var(--color-border);
}

.sql-table {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--color-text);
  font-weight: 500;
}

.sql-type {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  margin-left: auto;
}

.sql-code {
  margin: 0;
  padding: var(--space-sm);
  font-size: 0.71875rem;
  line-height: 1.45;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.sql-code-collapsed {
  max-height: 110px;
  overflow: hidden;
  mask-image: linear-gradient(to bottom, black 55%, transparent);
  -webkit-mask-image: linear-gradient(to bottom, black 55%, transparent);
}

.sql-expand-btn {
  margin-left: auto;
  flex-shrink: 0;
}

.sql-card-header .sql-type {
  margin-left: 0;
}
</style>
