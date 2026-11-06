<template>
  <div class="lifecycle-feed">
    <div class="section-header">
      <h4 class="mb-0">{{ $t('components.lifecycle.header') }}</h4>
      <span class="section-count">{{ $t('components.lifecycle.events', { count: feed.length }) }}</span>
    </div>

    <div v-if="feed.length === 0" class="empty-state" style="padding:var(--space-xl);">
      <div class="empty-state-icon">📜</div>
      <div class="empty-state-title">{{ $t('components.lifecycle.noEvents') }}</div>
    </div>

    <div v-else class="feed-list" ref="feedRef">
      <div v-for="(item, i) in feed" :key="i" class="feed-item" :class="'feed-' + item.level">
        <span class="feed-time">{{ item.timestamp ? formatTime(item.timestamp) : '' }}</span>
        <span class="feed-dot" :class="'dot-' + item.level"></span>
        <div class="feed-content">
          <span class="feed-phase">{{ item.phase || item.node || item.type }}</span>
          <span class="feed-action">{{ item.action || item.message || '' }}</span>
          <span v-if="item.duration_ms" class="feed-duration">{{ formatDuration(item.duration_ms) }}</span>
          <span v-if="item.error" class="feed-error-msg">{{ item.error }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, watch, ref, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  auditLog: { type: Array, default: () => [] },
  timeline: { type: Array, default: () => [] },
  thoughts: { type: Array, default: () => [] },
  wsEvents: { type: Array, default: () => [] },
})

const { t: i18nT } = useI18n()
const feedRef = ref(null)

const feed = computed(() => {
  const items = []

  // Node timeline events
  for (const t of props.timeline) {
    items.push({
      timestamp: t.timestamp,
      level: t.status === 'failed' ? 'error' : 'info',
      phase: t.node,
      action: t.status === 'completed' ? i18nT('task.status.completed') : i18nT('task.status.failed'),
      duration_ms: t.duration_ms,
      error: t.error,
      type: 'node',
    })
  }

  // Audit log entries
  for (const a of props.auditLog || []) {
    items.push({
      timestamp: a.timestamp || a.created_at,
      level: 'info',
      phase: a.phase,
      action: a.action || JSON.stringify(a).slice(0, 80),
      type: 'audit',
    })
  }

  // LLM thoughts — persisted traces carry created_at; live WS thoughts
  // fall back to now (they really did just happen).
  for (const th of props.thoughts) {
    items.push({
      timestamp: th.created_at || new Date().toISOString(),
      level: 'info',
      phase: th.node,
      action: `LLM call (${th.duration_ms ? Math.round(th.duration_ms) + 'ms' : ''})`,
      duration_ms: th.duration_ms,
      type: 'thought',
    })
  }

  // WebSocket events
  for (const e of props.wsEvents || []) {
    const level = e.type === 'error' ? 'error' : e.type === 'warning' ? 'warning' : 'info'
    items.push({
      timestamp: e.timestamp,
      level,
      phase: e.type,
      action: typeof e.data === 'string' ? e.data : JSON.stringify(e.data || '').slice(0, 80),
      message: typeof e.data === 'string' ? e.data : '',
      type: 'ws',
    })
  }

  // Sort by timestamp descending
  items.sort((a, b) => new Date(b.timestamp || 0) - new Date(a.timestamp || 0))
  return items.slice(0, 200)
})

watch(feed, async () => {
  await nextTick()
  if (feedRef.value) {
    feedRef.value.scrollTop = 0
  }
})

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return d.toLocaleTimeString('en-GB', { hour12: false })
}

function formatDuration(ms) {
  if (!ms) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
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

.feed-list {
  max-height: 400px;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-sm);
}

.feed-item {
  display: flex;
  align-items: flex-start;
  gap: var(--space-sm);
  padding: 4px 0;
  font-size: 0.75rem;
  animation: feed-item-enter 0.3s var(--spring-default);
}

@keyframes feed-item-enter {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
}

.feed-item + .feed-item {
  border-top: 1px solid var(--color-border);
}

.feed-time {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
  min-width: 70px;
}

.feed-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-top: 5px;
  flex-shrink: 0;
}

.dot-info { background: var(--color-accent); }
.dot-error { background: var(--color-error); }
.dot-warning { background: var(--color-warning); }

.feed-content {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
  align-items: baseline;
}

.feed-phase {
  font-weight: 500;
  color: var(--color-text);
  font-size: 0.6875rem;
  font-family: var(--font-mono);
}

.feed-action {
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 300px;
}

.feed-duration {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.feed-error-msg {
  color: var(--color-error);
  font-size: 0.6875rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 200px;
}

.feed-error { background: rgba(255,59,48,0.03); }
.feed-error .feed-dot { background: var(--color-error); }
.feed-error .feed-phase { color: var(--color-error); }
</style>
