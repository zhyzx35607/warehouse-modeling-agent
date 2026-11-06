<template>
  <div class="kg-snapshot">
    <div class="section-header">
      <h4 class="mb-0">{{ $t('components.kgSnapshot.header') }}</h4>
      <router-link v-if="taskId" :to="`/task/${taskId}/assets`" class="btn btn-ghost btn-sm">
        {{ $t('components.kgSnapshot.viewAssets') }}
      </router-link>
    </div>

    <div v-if="!hasData" class="empty-state" style="padding:var(--space-xl);">
      <div class="empty-state-icon">🔗</div>
      <div class="empty-state-title">{{ $t('components.kgSnapshot.notExported') }}</div>
      <div class="empty-state-desc">{{ $t('components.kgSnapshot.notExportedDesc') }}</div>
    </div>

    <div v-else class="kg-content">
      <!-- Node type counts -->
      <div class="kg-stats">
        <div v-for="(count, type) in nodeTypes" :key="type" class="kg-stat">
          <span class="kg-stat-label">{{ type }}</span>
          <span class="kg-stat-value">{{ count }}</span>
        </div>
      </div>

      <!-- Layer breakdown -->
      <div v-if="layers" class="kg-layers">
        <h5 class="mb-sm">{{ $t('components.kgSnapshot.tableLineage') }}</h5>
        <div v-for="(tables, layer) in layers" :key="layer" class="kg-layer-group">
          <span class="badge kg-layer-badge">{{ layer }}</span>
          <div class="kg-layer-items">
            <span v-for="t in tables" :key="t.table || t.name" class="kg-table-chip">
              {{ t.table || t.name }}
            </span>
          </div>
        </div>
      </div>

      <div v-if="kgExportId" class="kg-export-id">
        <span class="kg-export-label">{{ $t('components.kgSnapshot.exportId') }}</span>
        <code>{{ kgExportId }}</code>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  assetMap: { type: [Object, String], default: null },
  kgExportId: { type: String, default: '' },
  taskId: { type: String, default: '' },
})

const assetData = computed(() => {
  if (!props.assetMap) return null
  if (typeof props.assetMap === 'string') {
    try { return JSON.parse(props.assetMap) } catch { return null }
  }
  return props.assetMap
})

const hasData = computed(() => !!assetData.value || !!props.kgExportId)

const nodeTypes = computed(() => {
  if (!assetData.value) return {}
  return assetData.value.nodes || assetData.value.node_types || {}
})

const layers = computed(() => {
  if (!assetData.value) return null
  return assetData.value.layers || null
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

.kg-stats {
  display: flex;
  gap: var(--space-sm);
  flex-wrap: wrap;
  margin-bottom: var(--space-lg);
}

.kg-stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  min-width: 70px;
}

.kg-stat-label {
  font-size: 0.625rem;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.kg-stat-value {
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}

.kg-layers {
  margin-bottom: var(--space-md);
}

.kg-layer-group {
  margin-bottom: var(--space-sm);
}

.kg-layer-badge {
  margin-bottom: 4px;
  display: inline-block;
}

.kg-layer-items {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}

.kg-table-chip {
  font-family: var(--font-mono);
  font-size: 0.6875rem;
  padding: 2px 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xs);
  color: var(--color-text-secondary);
}

.kg-export-id {
  margin-top: var(--space-md);
  padding: var(--space-sm);
  background: var(--color-bg);
  border-radius: var(--radius-sm);
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.kg-export-label {
  color: var(--color-text-tertiary);
  font-size: 0.75rem;
  margin-right: var(--space-sm);
}

.kg-export-id code {
  font-family: var(--font-mono);
  font-size: 0.71875rem;
  color: var(--color-accent);
}
</style>
