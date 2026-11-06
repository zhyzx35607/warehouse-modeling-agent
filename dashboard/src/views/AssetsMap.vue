<template>
  <div class="assets-map">
    <div v-if="!inline" class="page-header">
      <router-link to="/" class="back-btn">&larr;</router-link>
      <div class="flex-1">
        <h2>{{ $t('assets.title') }}</h2>
        <p>{{ $t('assets.subtitle') }}</p>
      </div>
      <div>
        <select v-model="selectedTask" class="select" @change="loadLineage">
          <option value="">{{ $t('assets.selectTask') }}</option>
          <option v-for="t in tasks" :key="t" :value="t">{{ t }}</option>
        </select>
      </div>
    </div>
    <div v-else class="inline-header">
      <div>
        <h4 class="mb-0">{{ $t('assets.title') }}</h4>
      </div>
      <router-link :to="`/task/${id || selectedTask}/assets`" class="btn btn-ghost btn-sm">{{ $t('assets.openFullPage') }}</router-link>
    </div>

    <!-- Asset Map Grid -->
    <div class="card mb-lg">
      <h3 class="mb-md">{{ $t('assets.assetMap') }}</h3>
      <div class="asset-grid" v-if="assetMap">
        <div
          v-for="(layer, key) in assetMap.layers"
          :key="key"
          class="asset-layer-card"
          :style="{ '--layer-color': layerColors[key] || '#8E8E93' }"
        >
          <div class="layer-header">
            <span class="badge" :style="{ background: (layerColors[key] || '#8E8E93') + '18', color: layerColors[key] }">
              {{ key.toUpperCase() }}
            </span>
            <span class="layer-count">{{ $t('assets.tables', { count: layer.count }) }}</span>
          </div>
          <div class="layer-items">
            <div v-for="item in layer.items" :key="item.name" class="layer-item-row">
              <span class="layer-dot-color" :style="{ color: layerColors[key] }">&#9679;</span>
              <span class="layer-item-name">{{ item.name }}</span>
              <span v-if="item.key" class="layer-item-key">PK: {{ item.key?.join(',') }}</span>
              <span v-if="item.grain" class="layer-item-key">grain: {{ item.grain?.join(',') }}</span>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="empty-state" style="padding:var(--space-2xl);">
        <div class="empty-state-icon">🗺️</div>
        <div class="empty-state-title">{{ $t('assets.selectTaskHint') }}</div>
      </div>
    </div>

    <!-- Lineage Visualization -->
    <div class="card mb-lg">
      <div class="lineage-header">
        <h3 class="mb-0">{{ $t('assets.lineage') }}</h3>
        <span v-if="lineageData" class="lineage-stats">
          {{ $t('assets.lineageStats', { nodes: lineageData.summary?.total_nodes || 0, edges: lineageData.summary?.total_edges || 0 }) }}
        </span>
      </div>
      <div ref="lineageContainer" class="lineage-container" :style="{ minHeight: lineageHeight + 'px' }">
        <svg ref="lineageSvg" width="100%" :height="lineageHeight" v-if="lineageData" class="lineage-svg">
          <!-- Edges -->
          <g v-for="(edge, ei) in lineageData.edges" :key="'e'+ei">
            <line
              :x1="nodePos[edge.source]?.x || 0"
              :y1="nodePos[edge.source]?.y || 0"
              :x2="nodePos[edge.target]?.x || 0"
              :y2="nodePos[edge.target]?.y || 0"
              stroke="#D1D1D6" stroke-width="1.5"
              stroke-linecap="round"
              :marker-end="'url(#arrowhead)'"
            />
            <text
              :x="((nodePos[edge.source]?.x||0) + (nodePos[edge.target]?.x||0)) / 2"
              :y="((nodePos[edge.source]?.y||0) + (nodePos[edge.target]?.y||0)) / 2 - 6"
              font-size="9" fill="#AEAEB2" text-anchor="middle"
              font-family="system-ui, sans-serif"
            >{{ edge.label }}</text>
          </g>
          <!-- Nodes -->
          <g v-for="(node, ni) in lineageData.nodes" :key="'n'+ni" class="lineage-node-group">
            <rect
              :x="(nodePos[node.id]?.x || 0) - (nodePos[node.id]?.w || 150) / 2"
              :y="(nodePos[node.id]?.y || 0) - 16"
              :width="nodePos[node.id]?.w || 150" height="32"
              :rx="7"
              :fill="(layerColors[node.label] || '#8E8E93') + '12'"
              :stroke="layerColors[node.label] || '#8E8E93'"
              stroke-width="1.5"
            />
            <text
              :x="nodePos[node.id]?.x || 0"
              :y="(nodePos[node.id]?.y || 0) + 4"
              font-size="11"
              :fill="layerColors[node.label] || '#8E8E93'"
              text-anchor="middle"
              font-family="ui-monospace, SFMono-Regular, Menlo, monospace"
              font-weight="500"
            >{{ node.name }}<title>{{ node.name }} ({{ node.label }})</title></text>
          </g>
          <defs>
            <marker id="arrowhead" markerWidth="6" markerHeight="4" refX="5" refY="2" orient="auto">
              <polygon points="0 0, 6 2, 0 4" fill="#D1D1D6" />
            </marker>
          </defs>
        </svg>
        <div v-else class="empty-state" style="padding:var(--space-2xl);">
          <div class="empty-state-icon">🔗</div>
          <div class="empty-state-title">{{ $t('assets.lineageHint') }}</div>
        </div>
      </div>
    </div>

    <!-- Impact Analysis -->
    <div class="card">
      <h3 class="mb-md">{{ $t('assets.impact') }}</h3>
      <div class="impact-form">
        <input
          v-model="impactDim"
          class="input"
          :placeholder="$t('assets.dimPlaceholder')"
          @keyup.enter="loadImpact"
        />
        <button class="btn btn-primary btn-sm" @click="loadImpact" :disabled="!impactDim || analyzing">
          <span v-if="analyzing" class="btn-spinner-sm"></span>
          {{ $t('assets.analyze') }}
        </button>
      </div>
      <Transition name="fade">
        <div v-if="impactResult" class="impact-result">
          <div class="impact-summary">
            <span class="badge badge-info">{{ $t('assets.affected', { count: impactResult.affected_count }) }}</span>
          </div>
          <div v-for="a in (impactResult.affected_artifacts || [])" :key="a.uuid" class="impact-item">
            <span class="badge badge-neutral">{{ a.type }}</span>
            <span class="impact-name">{{ a.name }}</span>
            <span class="impact-distance">{{ $t('assets.distance', { distance: a.distance }) }}</span>
          </div>
        </div>
      </Transition>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { api } from '../utils/api.js'

const props = defineProps({
  inline: { type: Boolean, default: false },
  taskId: { type: String, default: '' },
})

const tasks = ref([])
const selectedTask = ref('')
const assetMap = ref(null)
const lineageData = ref(null)
const lineageHeight = ref(400)
const impactDim = ref('')
const impactResult = ref(null)
const analyzing = ref(false)

const nodePos = reactive({})

const layerColors = {
  source: '#8E8E93',
  ods: '#007AFF',
  dim: '#34C759',
  dwd: '#FF9500',
  dws: '#FF3B30',
  ads: '#AF52DE',
  metrics: '#5AC8FA',
}

async function loadTasks() {
  try {
    const r = await api.listTasks()
    tasks.value = (r.tasks || []).map(t => t.task_id)
    // Auto-select: prefer prop, fallback to most recent task
    if (props.taskId && tasks.value.includes(props.taskId)) {
      selectedTask.value = props.taskId
    } else if (tasks.value.length > 0) {
      selectedTask.value = tasks.value[0]
    }
    if (selectedTask.value) {
      await loadLineage()
    }
  } catch (e) {
    console.error('Failed to load tasks:', e)
  }
}

async function loadLineage() {
  if (!selectedTask.value) return
  try {
    const [lin, assets] = await Promise.all([
      api.fetch(`/api/v1/lineage/${selectedTask.value}?depth=5`).then(r => r.json()),
      api.fetch(`/api/v1/asset-map/${selectedTask.value}`).then(r => r.json()),
    ])
    // Drop edges whose endpoints are not in the node list (e.g. BELONGS_TO
    // edges to the task node, which the API does not return) — otherwise
    // they render as lines flying into the top-left origin corner.
    const knownIds = new Set((lin.nodes || []).map(n => n.id))
    lin.edges = (lin.edges || []).filter(e => knownIds.has(e.source) && knownIds.has(e.target))
    lineageData.value = lin
    assetMap.value = assets

    // Layout: one column per layer, left → right in data-flow order.
    if (lin.nodes?.length) {
      const layerGroups = {}
      for (const n of lin.nodes) {
        const layer = n.label
        if (!layerGroups[layer]) layerGroups[layer] = []
        layerGroups[layer].push(n)
      }
      const layerOrder = ['source', 'ods', 'dim', 'dwd', 'dws', 'ads', 'metrics']
      const layerKeys = Object.keys(layerGroups).sort((a, b) => layerOrder.indexOf(a) - layerOrder.indexOf(b))
      const xStep = 320
      const yStep = 64

      let x = 170
      let maxY = 0
      // Clear previous positions
      Object.keys(nodePos).forEach(k => delete nodePos[k])
      for (const layer of layerKeys) {
        const nodes = layerGroups[layer]
        let y = 60
        for (const n of nodes) {
          // Node width follows the (untruncated) name — long table names
          // used to be clipped to 18 chars and became indistinguishable.
          const w = Math.min(300, Math.max(150, (n.name || '').length * 7.4 + 28))
          nodePos[n.id] = { x, y, w }
          y += yStep
        }
        maxY = Math.max(maxY, y)
        x += xStep
      }
      lineageHeight.value = Math.max(400, maxY + 20)
    }
  } catch (e) {
    console.error('Lineage load failed:', e)
  }
}

onMounted(loadTasks)

async function loadImpact() {
  if (!impactDim.value) return
  analyzing.value = true
  try {
    const r = await api.fetch(`/api/v1/impact/${impactDim.value}`).then(r => r.json())
    impactResult.value = r
  } catch (e) {
    console.error(e)
  } finally {
    analyzing.value = false
  }
}
</script>

<style scoped>
/* ── Asset Grid ──────────────────────────────────────────── */
.asset-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--space-md);
}

.asset-layer-card {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
  transition:
    box-shadow var(--duration-base) var(--spring-default),
    transform var(--duration-base) var(--spring-default);
}
.asset-layer-card:hover {
  box-shadow: var(--shadow-elevated);
}

.layer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-sm) var(--space-md);
  background: var(--color-bg);
  border-bottom: 1px solid var(--color-border);
}

.layer-count {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  font-weight: 500;
}

.layer-items {
  padding: var(--space-sm) var(--space-md);
}

.layer-item-row {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: 3px 0;
  font-size: 0.8125rem;
}

.layer-dot-color {
  flex-shrink: 0;
  font-size: 0.5rem;
}

.layer-item-name {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.layer-item-key {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── Lineage ─────────────────────────────────────────────── */
.lineage-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-md);
}

.lineage-stats {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.lineage-container {
  overflow: auto;
  background: var(--color-bg);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
}

.lineage-svg {
  display: block;
}

.lineage-node-group {
  cursor: default;
  transition: opacity var(--duration-fast) var(--spring-default);
}
.lineage-node-group:hover {
  opacity: 0.85;
}

/* ── Impact Analysis ─────────────────────────────────────── */
.impact-form {
  display: flex;
  gap: var(--space-md);
  align-items: center;
}

.impact-result {
  margin-top: var(--space-md);
}

.impact-summary {
  margin-bottom: var(--space-sm);
}

.impact-item {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: 6px 0;
}
.impact-item + .impact-item {
  border-top: 1px solid var(--color-border);
}

.impact-name {
  font-family: var(--font-mono);
  font-size: 0.8125rem;
}

.impact-distance {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  margin-left: auto;
}

/* ── Spinner ─────────────────────────────────────────────── */
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

/* ── Utility ─────────────────────────────────────────────── */
.mb-0 { margin-bottom: 0; }
.mb-md { margin-bottom: var(--space-md); }
.mb-lg { margin-bottom: var(--space-lg); }
</style>
