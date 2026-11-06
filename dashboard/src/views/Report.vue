<template>
  <div class="report">
    <div v-if="!inline" class="page-header">
      <router-link :to="`/task/${id}`" class="back-btn">&larr;</router-link>
      <div>
        <h2>{{ $t('report.title') }}</h2>
        <p>{{ $t('report.subtitle') }}</p>
      </div>
      <button v-if="report" class="btn btn-secondary btn-sm download-btn" @click="downloadMd">
        ⬇ {{ $t('report.download') }}
      </button>
    </div>
    <div v-else class="inline-header">
      <div>
        <h4 class="mb-0">{{ $t('report.title') }}</h4>
      </div>
      <router-link :to="`/task/${id}/report`" class="btn btn-ghost btn-sm">{{ $t('report.openFullPage') }}</router-link>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="loading-block">
      <div class="skeleton skeleton-title" style="width:40%;margin-bottom:var(--space-lg);"></div>
      <div v-for="i in 5" :key="i" class="skeleton skeleton-text" style="margin-bottom:12px;" :style="{ width: (90 - i * 10) + '%' }"></div>
    </div>

    <!-- Report Content -->
    <template v-else-if="report">
      <div class="report-switch" role="tablist" :aria-label="$t('report.versionLabel')">
        <button
          type="button"
          role="tab"
          :aria-selected="activeVersion === 'plain'"
          :class="['report-switch-btn', { active: activeVersion === 'plain' }]"
          @click="activeVersion = 'plain'"
        >
          <span>📖</span>
          <span>
            <strong>{{ $t('report.plainTab') }}</strong>
            <small>{{ $t('report.plainTabDesc') }}</small>
          </span>
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="activeVersion === 'technical'"
          :class="['report-switch-btn', { active: activeVersion === 'technical' }]"
          @click="activeVersion = 'technical'"
        >
          <span>🛠️</span>
          <span>
            <strong>{{ $t('report.technicalTab') }}</strong>
            <small>{{ $t('report.technicalTabDesc') }}</small>
          </span>
        </button>
      </div>
      <div v-if="outcome" class="outcome-banner" :class="outcomeClass">
        <span class="outcome-icon">{{ outcomeIcon }}</span>
        <span class="outcome-text">{{ outcome }}</span>
      </div>
      <div class="card markdown-body report-content" v-html="rendered"></div>
    </template>

    <!-- Empty -->
    <div v-else class="card" style="text-align:center;padding:var(--space-3xl);">
      <div class="empty-state">
        <div class="empty-state-icon">📋</div>
        <div class="empty-state-title">{{ $t('report.notReady') }}</div>
        <div class="empty-state-desc">{{ $t('report.hint') }}</div>
        <router-link :to="`/task/${id}`" class="btn btn-secondary btn-sm mt-md">
          {{ $t('report.backToTask') }}
        </router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../utils/api.js'
import MarkdownIt from 'markdown-it'

defineProps({
  inline: { type: Boolean, default: false },
})

const route = useRoute()
const id = route.params.id
const technicalReport = ref('')
const plainReport = ref('')
const activeVersion = ref('plain')
const loading = ref(true)

const report = computed(() =>
  activeVersion.value === 'plain'
    ? (plainReport.value || technicalReport.value)
    : technicalReport.value
)

const md = new MarkdownIt({
  html: true,
  linkify: true,
  breaks: true,
})

const rendered = computed(() => {
  if (!report.value) return ''
  return md.render(report.value)
})

/* Outcome banner — surface the report's own 状态 line so the result is
   obvious without reading the whole document. */
const outcome = computed(() => {
  const m = report.value.match(/\*\*(?:状态|当前结论)\*\*[:：]\s*(.+)/)
  return m ? m[1].trim() : ''
})
const outcomeClass = computed(() => {
  if (outcome.value === '成功' || outcome.value === '已完成') return 'outcome-success'
  if (/错误|未通过|失败|未完成|需要处理|待确认/.test(outcome.value)) return 'outcome-warn'
  return 'outcome-neutral'
})
const outcomeIcon = computed(() => {
  if (outcomeClass.value === 'outcome-success') return '✅'
  if (outcomeClass.value === 'outcome-warn') return '⚠️'
  return 'ℹ️'
})

function downloadMd() {
  try {
    const blob = new Blob([report.value], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const suffix = activeVersion.value === 'plain' ? 'plain' : 'technical'
    a.download = `wren-report-${suffix}-${String(id || '').slice(0, 8)}.md`
    // Some browsers ignore .click() on anchors not attached to the DOM.
    a.style.display = 'none'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) {
    console.error('report download failed', e)
  }
}

async function loadReport() {
  try {
    const data = await api.getReport(id)
    if (data && typeof data === 'object') {
      technicalReport.value = data.report || data.construction_report || ''
      plainReport.value = data.plain_report || ''
      if (!technicalReport.value && !plainReport.value) {
        technicalReport.value = JSON.stringify(data, null, 2)
      }
    } else {
      technicalReport.value = String(data || '')
    }
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

/* The report only exists after the task finishes — poll while it is
   missing so the page fills in by itself instead of needing F5. */
let pollTimer = null

onMounted(async () => {
  await loadReport()
  pollTimer = setInterval(() => {
    if (report.value || document.visibilityState !== 'visible') return
    loadReport()
  }, 5000)
})

onUnmounted(() => {
  clearInterval(pollTimer)
  pollTimer = null
})
</script>

<style scoped>
/* ── Loading ─────────────────────────────────────────────── */
.loading-block {
  padding: var(--space-xl) 0;
}

/* ── Report Content ──────────────────────────────────────── */
.report-content {
  animation: content-in 0.5s var(--spring-default);
  overflow-x: hidden;
}

/* Long table names / SQL / unbroken strings must wrap inside the card
   instead of overflowing it. */
.report-content :deep(table) {
  display: block;
  width: 100%;
  overflow-x: auto;
  table-layout: auto;
  border-collapse: collapse;
}
.report-content :deep(th),
.report-content :deep(td) {
  word-break: break-word;
  overflow-wrap: anywhere;
  white-space: normal;
  vertical-align: top;
}
.report-content :deep(code) {
  word-break: break-all;
  overflow-wrap: anywhere;
  white-space: normal;
}
.report-content :deep(pre) {
  white-space: pre-wrap;
  word-break: break-all;
  overflow-x: auto;
}
.report-content :deep(p),
.report-content :deep(li) {
  overflow-wrap: anywhere;
}

.report-switch {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-sm);
  margin-bottom: var(--space-md);
  padding: 4px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-bg);
}

.report-switch-btn {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  padding: var(--space-sm) var(--space-md);
  background: transparent;
  color: var(--color-text-secondary);
  text-align: left;
  cursor: pointer;
  transition: background 160ms ease, border-color 160ms ease, color 160ms ease;
}

.report-switch-btn:hover {
  background: var(--color-surface);
}

.report-switch-btn.active {
  background: var(--color-surface);
  border-color: var(--color-accent);
  color: var(--color-text-primary);
  box-shadow: var(--shadow-sm);
}

.report-switch-btn span:last-child {
  display: grid;
  gap: 2px;
}

.report-switch-btn strong {
  font-size: 0.875rem;
}

.report-switch-btn small {
  color: var(--color-text-tertiary);
  font-size: 0.75rem;
}

@media (max-width: 640px) {
  .report-switch { grid-template-columns: 1fr; }
}

@keyframes content-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
}

/* Enhance markdown rendering */
.report-content :deep(table) {
  border-radius: var(--radius-md);
  overflow: hidden;
  box-shadow: var(--shadow-card);
}

.report-content :deep(th) {
  background: var(--color-bg);
}

.report-content :deep(tbody tr:nth-child(even)) {
  background: var(--color-bg);
}

.report-content :deep(h1),
.report-content :deep(h2) {
  letter-spacing: -0.02em;
}

.report-content :deep(h2) {
  padding-bottom: var(--space-xs);
  border-bottom: 1px solid var(--color-border);
}

.report-content :deep(pre) {
  background: #1f2d3d;
  color: #dce6f2;
  border-radius: var(--radius-md);
  padding: var(--space-md);
  overflow-x: auto;
}

.report-content :deep(code) {
  font-family: var(--font-mono);
  font-size: 0.85em;
}

.report-content :deep(:not(pre) > code) {
  background: var(--color-accent-glass);
  color: var(--color-accent);
  border-radius: var(--radius-xs);
  padding: 1px 5px;
}

.report-content :deep(img) {
  max-width: 100%;
  border-radius: var(--radius-md);
}

.report-content :deep(a) {
  color: var(--color-accent);
}

.report-content :deep(hr) {
  border: none;
  height: 1px;
  background: linear-gradient(
    to right,
    transparent 0%,
    var(--color-border) 20%,
    var(--color-border) 80%,
    transparent 100%
  );
  margin: var(--space-lg) 0;
}

/* ── Outcome banner ────────────────────────────────────── */
.download-btn { margin-left: auto; }

.outcome-banner {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-md) var(--space-lg);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
  margin-bottom: var(--space-md);
  font-weight: 600;
  animation: content-in 0.4s var(--spring-default);
}
.outcome-success {
  background: var(--color-success-bg);
  border-color: var(--color-success);
  color: var(--color-success);
}
.outcome-warn {
  background: var(--color-warning-bg);
  border-color: var(--color-warning);
  color: var(--color-warning);
}
.outcome-neutral {
  background: var(--color-accent-glass);
  border-color: var(--color-accent);
  color: var(--color-accent);
}
.outcome-icon { font-size: 1.25rem; }

/* ── Utility ─────────────────────────────────────────────── */
.mt-md { margin-top: var(--space-md); }
</style>
