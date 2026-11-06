<template>
  <div class="node-timeline">
    <div class="timeline-header">
      <h4 class="mb-0">{{ $t('components.nodeTimeline.header') }}</h4>
      <span class="timeline-stats">
        {{ $t('components.nodeTimeline.completed', { count: completedCount, total: allNodes.length }) }}
      </span>
    </div>

    <div class="timeline-track">
      <div class="progress-bar-bg">
        <div
          class="progress-bar-fill"
          :style="{ width: progressPct + '%' }"
          :class="{ 'progress-bar-complete': allComplete, 'progress-bar-error': hasFailed }"
        ></div>
      </div>

      <div class="node-list">
        <div
          v-for="(node, i) in allNodes"
          :key="node.key"
          :class="[
            'node-item',
            {
              'node-active': node.key === currentNode,
              'node-completed': node.status === 'completed',
              'node-failed': node.status === 'failed',
              'node-running': node.key === currentNode && node.status !== 'completed',
            }
          ]"
          :style="{ animationDelay: Math.min(i * 0.025, 0.7) + 's' }"
          @click="$emit('selectNode', node)"
        >
          <span v-if="node.status === 'completed'" class="node-check">✓</span>
          <span v-else class="node-dot" :class="'dot-' + (node.status || 'pending')"></span>
          <span class="node-label">{{ node.label }}</span>
          <span v-if="node.skipped" class="node-skip-tag">{{ $t('components.nodeTimeline.skipped') }}</span>
          <span v-if="node.duration_ms" class="node-duration">{{ formatDuration(node.duration_ms) }}</span>
          <span v-if="node.status === 'failed'" class="node-error-icon" :title="node.error">⚠</span>
        </div>
      </div>
    </div>

    <!-- Selected node detail -->
    <Transition name="fade">
      <div v-if="selectedNode" class="node-detail card">
        <div class="node-detail-header">
          <span class="badge" :class="'badge-' + (selectedNode.status === 'completed' ? 'success' : selectedNode.status === 'failed' ? 'error' : 'info')">
            {{ selectedNode.status }}
          </span>
          <span class="node-detail-name">{{ selectedNode.label }}</span>
          <span v-if="selectedNode.duration_ms" class="node-detail-time">{{ formatDuration(selectedNode.duration_ms) }}</span>
          <button class="btn btn-ghost btn-sm" @click="selectedNode = null">&times;</button>
        </div>
        <div v-if="selectedNode.error" class="node-detail-error">
          <strong>{{ $t('components.nodeTimeline.error') }}</strong> {{ selectedNode.error }}
        </div>
        <div v-if="selectedNode.outputs" class="node-detail-outputs">
          <div v-for="(val, key) in summarizeOutputs(selectedNode.outputs)" :key="key" class="output-row">
            <span class="output-key">{{ key }}</span>
            <span class="output-val">{{ val }}</span>
          </div>
        </div>
        <div v-if="!selectedNode.error && !selectedNode.outputs" class="empty-state" style="padding:var(--space-md);">
          <span style="color: var(--color-text-tertiary); font-size: 0.8125rem;">{{ $t('components.nodeTimeline.noDetail') }}</span>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { NODE_DEFS } from '../../utils/nodeDefs.js'

const props = defineProps({
  timeline: { type: Array, default: () => [] },
  currentNode: { type: String, default: '' },
})

defineEmits(['selectNode'])

const { t } = useI18n()
const selectedNode = ref(null)

const allNodes = computed(() => {
  return NODE_DEFS.map(def => {
    const match = props.timeline.find(t => t.node === def.key)
    return {
      ...def,
      label: t(`components.nodeTimeline.nodes.${def.key}`),
      status: match?.status || 'pending',
      duration_ms: match?.duration_ms || null,
      error: match?.error || null,
      outputs: match?.outputs || null,
      skipped: match?.skipped || false,
    }
  })
})

const completedCount = computed(() => {
  // Only count known nodes and never exceed the total — duplicated or
  // unknown timeline entries must not push the counter past "29/29".
  const known = new Set(NODE_DEFS.map(d => d.key))
  const done = new Set(
    props.timeline.filter(t => t.status === 'completed' && known.has(t.node)).map(t => t.node)
  )
  return Math.min(done.size, NODE_DEFS.length)
})

const hasFailed = computed(() =>
  props.timeline.some(t => t.status === 'failed')
)

const allComplete = computed(() =>
  props.timeline.length > 0 &&
  NODE_DEFS.every(def => props.timeline.some(t => t.node === def.key && t.status === 'completed'))
)

const progressPct = computed(() => {
  if (allComplete.value) return 100
  const done = completedCount.value
  return Math.min(Math.round((done / NODE_DEFS.length) * 100), 100)
})

function formatDuration(ms) {
  if (!ms) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${(ms / 60000).toFixed(1)}m`
}

function summarizeOutputs(outputs) {
  if (!outputs) return {}
  const summary = {}
  for (const [k, v] of Object.entries(outputs)) {
    if (Array.isArray(v)) summary[k] = `${v.length} items`
    else if (typeof v === 'object' && v !== null) summary[k] = `${Object.keys(v).length} keys`
    else if (v !== null && v !== undefined) summary[k] = String(v).slice(0, 60)
  }
  return summary
}
</script>

<style scoped>
.node-skip-tag {
  font-size: 11px;
  color: var(--color-text-tertiary);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  padding: 0 6px;
  margin-left: 6px;
}
.node-timeline { margin-bottom: var(--space-lg); }

.timeline-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-md);
}

.timeline-stats {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.mb-0 { margin-bottom: 0; }

.progress-bar-bg {
  height: 4px;
  background: var(--color-border);
  border-radius: 2px;
  margin-bottom: var(--space-md);
  overflow: hidden;
}

.progress-bar-fill {
  height: 100%;
  background: var(--color-accent);
  border-radius: 2px;
  transition: width 0.5s var(--spring-default);
}

.progress-bar-complete { background: var(--color-success); }
.progress-bar-error { background: var(--color-error); }

.node-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 4px;
}

.node-item {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  font-size: 0.75rem;
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--spring-default),
    box-shadow var(--duration-base) var(--spring-default);
  border: 1px solid transparent;
  animation: node-item-enter 0.35s var(--spring-default) both;
}

@keyframes node-item-enter {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
}

.node-check {
  width: 8px;
  height: 8px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 700;
  line-height: 1;
  color: #fff;
  background: var(--color-success);
  border-radius: 50%;
  padding: 2px;
  box-sizing: content-box;
  animation: check-pop 0.35s var(--spring-bounce);
}

@keyframes check-pop {
  from {
    transform: scale(0);
  }
  to {
    transform: scale(1);
  }
}

.node-item:hover { background: var(--color-bg); }

.node-active {
  border-color: var(--color-accent);
  background: var(--color-accent-bg, rgba(0,122,255,0.06));
  box-shadow: 0 0 0 1px var(--color-accent), 0 0 16px rgba(0,122,255,0.35);
}

.node-completed { opacity: 0.7; }
.node-failed {
  border-color: var(--color-error);
  background: rgba(255,59,48,0.04);
  box-shadow: 0 0 12px rgba(255,59,48,0.18);
}
.node-running { animation: node-pulse 2s ease-in-out infinite; }

@keyframes node-pulse {
  0%, 100% { border-color: var(--color-accent); }
  50% { border-color: var(--color-accent-hover); }
}

.node-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot-pending { background: var(--color-border); }
.dot-completed { background: var(--color-success); }
.dot-failed { background: var(--color-error); }
.node-running .node-dot { background: var(--color-accent); animation: dot-pulse 1.5s ease-in-out infinite; }

@keyframes dot-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(1.4); }
}

.node-label {
  flex: 1;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-duration {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.node-error-icon { font-size: 0.75rem; flex-shrink: 0; }

/* Node detail card */
.node-detail {
  margin-top: var(--space-md);
  padding: var(--space-md);
}

.node-detail-header {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-bottom: var(--space-sm);
}

.node-detail-name {
  font-weight: 600;
  color: var(--color-text);
}

.node-detail-time {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  margin-left: auto;
  margin-right: var(--space-sm);
}

.node-detail-error {
  padding: var(--space-sm);
  background: rgba(255,59,48,0.06);
  border-radius: var(--radius-sm);
  font-size: 0.8125rem;
  color: var(--color-error);
}

.node-detail-outputs {
  margin-top: var(--space-sm);
}

.output-row {
  display: flex;
  justify-content: space-between;
  padding: 4px 0;
  border-bottom: 1px solid var(--color-border);
  font-size: 0.8125rem;
}

.output-key {
  color: var(--color-text-secondary);
  font-family: var(--font-mono);
  font-size: 0.75rem;
}

.output-val {
  color: var(--color-text-tertiary);
  font-size: 0.75rem;
}
</style>
