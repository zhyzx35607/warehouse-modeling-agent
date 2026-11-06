<template>
  <div class="design-plan">
    <div v-if="!inline" class="page-header">
      <router-link :to="`/task/${id}`" class="back-btn">&larr;</router-link>
      <div>
        <h2>{{ $t('design.title') }}</h2>
        <p>{{ $t('design.subtitle') }}</p>
      </div>
    </div>
    <div v-else class="inline-header">
      <div>
        <h4 class="mb-0">{{ $t('design.title') }}</h4>
      </div>
      <router-link :to="`/task/${id}/design`" class="btn btn-ghost btn-sm">{{ $t('design.openFullPage') }}</router-link>
    </div>

    <!-- Layer Cards -->
    <div class="grid-2">
      <div
        v-for="layer in layers"
        :key="layer.name"
        class="card layer-card"
        :style="{ '--layer-color': layer.color }"
      >
        <div class="layer-card-header">
          <div class="layer-dot" :style="{ background: layer.color }"></div>
          <h3 class="mb-0">{{ layer.name }}</h3>
          <span class="badge badge-neutral">{{ $t('design.tables', { count: layer.count }) }}</span>
        </div>

        <div class="layer-items">
          <div v-for="item in layer.items" :key="item.table || item" class="layer-item">
            <span class="layer-item-table" :title="item.table">
              {{ item.table || item }}
            </span>
            <div class="layer-item-meta">
              <span v-if="item.source" class="layer-item-source">
                ← {{ item.source }}
              </span>
              <span v-if="item.sync" class="layer-item-sync">
                {{ item.sync }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- SQL Artifacts -->
    <div class="card mt-lg" v-if="plan?.sql_artifacts?.length">
      <h3 class="mb-md">{{ $t('design.sqlArtifacts') }}</h3>
      <div v-for="sql in plan.sql_artifacts" :key="sql.id" class="sql-block">
        <div class="sql-block-header">
          <span class="badge badge-info">{{ sql.layer }}</span>
          <span class="badge badge-neutral">{{ sql.sql_type }}</span>
          <span class="sql-table-name">{{ sql.target_table }}</span>
          <button
            class="btn btn-ghost btn-sm sql-expand-btn"
            @click="toggleSql(sql.id)"
          >
            {{ expandedSql.has(sql.id) ? $t('design.collapseSql') : $t('design.expandSql') }}
          </button>
        </div>
        <!-- Full SQL, never truncated; long statements start collapsed. -->
        <pre class="sql-code" :class="{ 'sql-code-collapsed': !expandedSql.has(sql.id) }">{{ sql.sql }}</pre>
      </div>
    </div>

    <!-- Empty state -->
    <div v-if="!layers.length && !plan" class="card" style="text-align:center;padding:var(--space-3xl);">
      <div class="empty-state">
        <div class="empty-state-icon">📐</div>
        <div class="empty-state-title">{{ $t('design.noDesign') }}</div>
        <div class="empty-state-desc">{{ $t('design.noDesignDesc') }}</div>
        <router-link :to="`/task/${id}`" class="btn btn-secondary btn-sm mt-md">
          {{ $t('design.backToTask') }}
        </router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../utils/api.js'

defineProps({
  inline: { type: Boolean, default: false },
})

const route = useRoute()
const id = route.params.id
const plan = ref(null)
const layers = ref([])

/* SQL blocks show the full statement; long ones start collapsed and the
   user expands per block. */
const expandedSql = ref(new Set())
function toggleSql(sqlId) {
  const next = new Set(expandedSql.value)
  if (next.has(sqlId)) next.delete(sqlId)
  else next.add(sqlId)
  expandedSql.value = next
}

const layerColorMap = {
  ODS: '#007AFF',
  DIM: '#34C759',
  DWD: '#FF9500',
  DWS: '#FF3B30',
  ADS: '#AF52DE',
}

async function load() {
  try {
    plan.value = await api.getDesignPlan(id)
    if (plan.value) {
      layers.value = [
        { name: 'ODS', color: layerColorMap.ODS, count: plan.value.ods_designs?.length || 0,
          items: (plan.value.ods_designs || []).map(d => ({ table: d.ods_table, source: d.source_table, sync: d.sync_method })) },
        { name: 'DIM', color: layerColorMap.DIM, count: plan.value.dim_designs?.length || 0,
          items: (plan.value.dim_designs || []).map(d => ({ table: d.dim_table, source: d.source })) },
        { name: 'DWD', color: layerColorMap.DWD, count: plan.value.dwd_designs?.length || 0,
          items: (plan.value.dwd_designs || []).map(d => ({ table: d.fact_table, source: `grain=${d.grain?.join(',')}` })) },
        { name: 'DWS', color: layerColorMap.DWS, count: plan.value.dws_designs?.length || 0,
          items: (plan.value.dws_designs || []).map(d => ({ table: d.summary_table })) },
        { name: 'ADS', color: layerColorMap.ADS, count: plan.value.ads_designs?.length || 0,
          items: (plan.value.ads_designs || []).map(d => ({ table: d.ads_table })) },
      ]
    }
  } catch (e) {
    console.error(e)
  }
}

/* The design only exists after the design phase — poll while it is
   missing so the page fills in by itself instead of needing F5. */
let pollTimer = null

onMounted(async () => {
  await load()
  pollTimer = setInterval(() => {
    if (plan.value || document.visibilityState !== 'visible') return
    load()
  }, 5000)
})

onUnmounted(() => {
  clearInterval(pollTimer)
  pollTimer = null
})
</script>

<style scoped>
/* ── Layer Cards ─────────────────────────────────────────── */
.layer-card {
  transition:
    box-shadow var(--duration-base) var(--spring-default),
    transform var(--duration-base) var(--spring-default);
}
.layer-card:hover {
  box-shadow: var(--shadow-elevated);
}

.layer-card-header {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-bottom: var(--space-md);
}

.layer-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

/* ── Layer Items ─────────────────────────────────────────── */
.layer-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-xs) 0;
  font-size: 0.8125rem;
  gap: var(--space-sm);
}
.layer-item + .layer-item {
  border-top: 1px solid var(--color-border);
}

.layer-item-table {
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.layer-item-meta {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  flex-shrink: 0;
}

.layer-item-source {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
}

.layer-item-sync {
  font-size: 0.6875rem;
  color: var(--color-accent);
  background: var(--color-accent-glass);
  padding: 1px 6px;
  border-radius: var(--radius-full);
}

/* ── SQL Blocks ──────────────────────────────────────────── */
.sql-block {
  margin-bottom: var(--space-md);
  background: var(--color-bg);
  border-radius: var(--radius-md);
  padding: var(--space-md);
  border: 1px solid var(--color-border);
}

.sql-block-header {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-bottom: var(--space-sm);
}

.sql-table-name {
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  color: var(--color-text);
}

.sql-code {
  background: var(--color-bg-elevated);
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-sm);
  font-size: 0.75rem;
  font-family: var(--font-mono);
  overflow-x: auto;
  line-height: 1.6;
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  white-space: pre-wrap;
  word-break: break-all;
}

.sql-code-collapsed {
  max-height: 120px;
  overflow: hidden;
  mask-image: linear-gradient(to bottom, black 60%, transparent);
  -webkit-mask-image: linear-gradient(to bottom, black 60%, transparent);
}

.sql-expand-btn {
  margin-left: auto;
  flex-shrink: 0;
}

/* ── Utility ─────────────────────────────────────────────── */
.mt-lg { margin-top: var(--space-lg); }
.mt-md { margin-top: var(--space-md); }
.mb-0 { margin-bottom: 0; }
.mb-md { margin-bottom: var(--space-md); }
</style>
