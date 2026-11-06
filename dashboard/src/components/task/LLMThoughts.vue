<template>
  <div class="llm-thoughts">
    <div class="section-header">
      <h4 class="mb-0">{{ $t('components.llmThoughts.header') }}</h4>
      <span class="section-count">{{ $t('components.llmThoughts.calls', { count: thoughts.length }) }}</span>
    </div>

    <div v-if="thoughts.length === 0" class="empty-state" style="padding:var(--space-xl);">
      <div class="empty-state-icon">🧠</div>
      <div class="empty-state-title">{{ $t('components.llmThoughts.noThoughts') }}</div>
      <div class="empty-state-desc">{{ $t('components.llmThoughts.noThoughtsDesc') }}</div>
    </div>

    <div v-else class="thought-list">
      <div
        v-for="(t, i) in thoughts"
        :key="i"
        class="thought-card"
        :class="{ 'thought-expanded': expandedIndex === i }"
      >
        <div class="thought-header" @click="expandedIndex = expandedIndex === i ? -1 : i">
          <span class="thought-node-badge">{{ t.node || $t('common.unknown') }}</span>
          <span class="thought-preview">{{ t.prompt_summary?.slice(0, 80) || '...' }}</span>
          <span class="thought-meta">
            <span v-if="t.duration_ms" class="thought-time">{{ formatDuration(t.duration_ms) }}</span>
            <span class="thought-chevron">{{ expandedIndex === i ? '▴' : '▾' }}</span>
          </span>
        </div>
        <Transition name="fade">
          <div v-if="expandedIndex === i" class="thought-body">
            <div class="thought-section">
              <div class="thought-label">{{ $t('components.llmThoughts.prompt') }}</div>
              <pre class="thought-pre">{{ t.prompt_summary || $t('components.llmThoughts.noPrompt') }}</pre>
            </div>
            <div class="thought-section">
              <div class="thought-label">{{ $t('components.llmThoughts.response') }}</div>
              <pre class="thought-pre">{{ t.response_summary || $t('components.llmThoughts.noResponse') }}</pre>
            </div>
            <div v-if="t.duration_ms" class="thought-footer">
              {{ $t('components.llmThoughts.duration', { duration: formatDuration(t.duration_ms) }) }} &nbsp;·&nbsp;
              {{ $t('components.llmThoughts.node', { node: t.node || $t('common.unknown') }) }}
            </div>
          </div>
        </Transition>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

defineProps({
  thoughts: { type: Array, default: () => [] },
})

const expandedIndex = ref(-1)

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

.thought-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.thought-card {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
  transition: border-color var(--duration-fast);
  animation: thought-card-enter 0.35s var(--spring-default);
}

@keyframes thought-card-enter {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
}

.thought-card:hover { border-color: var(--color-accent-hover); }

.thought-expanded { border-color: var(--color-accent); }

.thought-header {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  cursor: pointer;
  user-select: none;
}

.thought-node-badge {
  font-size: 0.6875rem;
  padding: 1px 6px;
  border-radius: var(--radius-xs);
  background: var(--color-accent-bg, rgba(0,122,255,0.08));
  color: var(--color-accent);
  flex-shrink: 0;
  font-family: var(--font-mono);
}

.thought-preview {
  flex: 1;
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.thought-meta {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-shrink: 0;
}

.thought-time {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.thought-chevron {
  font-size: 0.625rem;
  color: var(--color-text-tertiary);
}

.thought-body {
  padding: var(--space-sm) var(--space-md) var(--space-md);
  border-top: 1px solid var(--color-border);
}

.thought-section {
  margin-bottom: var(--space-sm);
}

.thought-label {
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 4px;
}

.thought-pre {
  margin: 0;
  padding: var(--space-sm);
  background: var(--color-bg);
  border-radius: var(--radius-sm);
  font-size: 0.75rem;
  line-height: 1.5;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}

.thought-footer {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  margin-top: var(--space-sm);
}
</style>
