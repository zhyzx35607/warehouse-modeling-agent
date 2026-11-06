<template>
  <div class="approval-detail">
    <div class="page-header">
      <router-link :to="`/task/${id}`" class="back-btn">&larr;</router-link>
      <div>
        <h2>{{ t('approvalDetail.title') }}</h2>
        <p class="detail-subtitle">
          {{ t('approvalDetail.subtitle') }}
          <span v-if="hasHistoryNote" class="hint-note">{{ t('approvalDetail.latestPlanNote') }}</span>
        </p>
      </div>
      <span
        v-if="approvalStatus"
        class="badge approval-status-badge"
        :class="approvalStatus === 'approved' ? 'badge-success' : approvalStatus === 'rejected' ? 'badge-error' : 'badge-warning'"
      >
        {{ approvalStatus }}
      </span>
    </div>

    <!-- Narrative status banner -->
    <div v-if="banner" :class="['narr-banner', banner.kind]">
      <span v-if="banner.kind === 'pending'" class="spinner"></span>
      {{ banner.text }}
    </div>

    <!-- Sticky anchor nav -->
    <nav class="sticky-nav">
      <a v-for="s in sections" :key="s.id" :href="`#${s.id}`">{{ s.label }}</a>
    </nav>

    <!-- Loading -->
    <div v-if="loading" class="loading-block">
      <div v-for="i in 3" :key="i" class="skeleton" style="height:140px;margin-bottom:var(--space-md);border-radius:var(--radius-lg);"></div>
    </div>

    <!-- Empty -->
    <div v-else-if="!plan" class="card" style="text-align:center;padding:var(--space-3xl);">
      <div class="empty-state">
        <div class="empty-state-icon">📐</div>
        <div class="empty-state-title">{{ t('design.noDesign') }}</div>
        <router-link :to="`/task/${id}/approve`" class="btn btn-secondary btn-sm mt-md">
          {{ t('approve.backToTask') }}
        </router-link>
      </div>
    </div>

    <template v-else>
      <!-- ① 目标解读 -->
      <section id="g" class="card">
        <h3 class="section-title">
          <span class="sec-num">1</span>{{ t('approvalDetail.goal') }}
          <span class="badge" :class="goalBadge.cls">{{ goalBadge.text }}</span>
        </h3>
        <div v-if="goalBlock?.status === 'ready'" class="narr-grid">
          <div class="block">
            <h4>🗣 {{ t('approvalDetail.businessView') }}</h4>
            <p>{{ goalContent?.business }}</p>
          </div>
          <div class="block">
            <h4>⚙ {{ t('approvalDetail.technicalView') }}</h4>
            <p>{{ goalContent?.technical }}</p>
          </div>
        </div>
        <div v-else-if="goalBlock?.status === 'failed'" class="degraded-note">
          {{ t('approvalDetail.blockFailed', { block: t('approvalDetail.goal') }) }}
        </div>
        <div v-else class="narr-grid">
          <div class="block"><div class="sk sk-w80"></div><div class="sk"></div><div class="sk sk-w60"></div></div>
          <div class="block"><div class="sk sk-w60"></div><div class="sk"></div><div class="sk sk-w40"></div></div>
        </div>
      </section>

      <!-- ② 表清单 + DDL -->
      <section id="t" class="card">
        <h3 class="section-title">
          <span class="sec-num">2</span>{{ t('approvalDetail.tables') }}
          <span class="badge badge-neutral">{{ t('approvalDetail.tableCount', { count: totalTables }) }}</span>
        </h3>
        <div v-for="layer in layers" :key="layer.name" class="layer-group" :class="{ open: openLayers.has(layer.name) }" :style="{ borderLeftColor: layer.color }">
          <div class="layer-head" @click="toggleLayer(layer.name)">
            <span class="dot" :style="{ background: layer.color }"></span>
            <b>{{ layer.name }}</b>
            <span class="count">{{ t('approvalDetail.tableCount', { count: layer.tables.length }) }}</span>
            <span class="arrow">▶</span>
          </div>
          <div class="layer-body">
            <div class="table-row head">
              <span>{{ t('design.table') }}</span><span>{{ t('approvalDetail.source') }}</span>
              <span>{{ t('approvalDetail.sync') }}</span><span>{{ t('approvalDetail.fields') }}</span><span>{{ t('approvalDetail.grain') }}</span>
            </div>
            <div v-for="tbl in layer.tables" :key="tbl.table" class="table-row" :class="{ open: openRows.has(tbl.table) }" @click="toggleRow(tbl.table)">
              <span class="name">{{ tbl.table }}</span>
              <span class="src">{{ tbl.source }}</span>
              <span class="sync">{{ tbl.sync || '—' }}</span>
              <span>{{ tbl.fields }}</span>
              <span>{{ tbl.grain || '—' }}</span>
              <span v-if="tbl.ddl" class="ddl-block">
                <div class="ddl-head">
                  <span class="ddl-type">{{ tbl.ddl.sql_type }}</span>
                  <button class="copy-btn" @click.stop="copyDdl(tbl)">{{ copiedTable === tbl.table ? '✓' : t('approvalDetail.copy') }}</button>
                </div>
                <pre>{{ tbl.ddl.sql }}</pre>
              </span>
            </div>
            <div v-if="!layer.tables.length" class="hint" style="padding:10px 14px;">—</div>
          </div>
        </div>
      </section>

      <!-- ③ 指标分析 -->
      <section id="m" class="card">
        <h3 class="section-title">
          <span class="sec-num">3</span>{{ t('approvalDetail.metrics') }}
          <span class="badge" :class="metricBlock?.status === 'ready' ? 'badge-info' : (metricBlock?.status === 'failed' ? 'badge-error' : 'badge-neutral')">
            {{ metricBadgeText }}
          </span>
        </h3>
        <div v-if="metrics.length" class="metric-cards">
          <div v-for="m in metrics" :key="m.name + m.layer" class="metric-card">
            <div class="metric-head">
              <span class="metric-name">{{ m.name }}</span>
              <span class="badge badge-neutral">{{ m.layer }}</span>
              <span class="badge" :class="m.registered ? 'badge-success' : 'badge-warning'">
                {{ m.registered ? t('approvalDetail.registered') : t('approvalDetail.unregistered') }}
              </span>
            </div>
            <code class="expr">{{ m.expression || '—' }}</code>
            <div class="metric-meta">
              {{ t('approvalDetail.metricGrain') }}: {{ m.grain || '—' }} ·
              {{ t('approvalDetail.metricSource') }}: {{ m.source || '—' }}
            </div>
            <p v-if="metricBlock?.status !== 'failed'" class="metric-interp" :class="{ empty: !m.interpretation }">
              <span v-if="m.interpretation">{{ m.interpretation }}</span>
              <span v-else-if="metricBlock?.status === 'pending'">⏳ {{ t('approvalDetail.generating') }}</span>
              <span v-else>—</span>
            </p>
          </div>
        </div>
        <div v-else class="hint">{{ t('approvalDetail.noMetrics') }}</div>
      </section>

      <!-- ④ 预计效果 -->
      <section id="i" class="card">
        <h3 class="section-title">
          <span class="sec-num">4</span>{{ t('approvalDetail.impact') }}
          <span class="badge" :class="impactBadge.cls">{{ impactBadge.text }}</span>
        </h3>
        <template v-if="impactBlock?.status === 'ready'">
          <p class="impact-narr">{{ impactContent?.narrative }}</p>
          <div v-if="impactContent?.quantified" class="impact-stats">
            <div v-for="(val, key) in impactContent.quantified" :key="key" class="stat">
              <div class="num">{{ displayStat(val) }}</div>
              <div class="lab">{{ t(`approvalDetail.quantified.${key}`) }}</div>
            </div>
          </div>
        </template>
        <div v-else-if="impactBlock?.status === 'failed'" class="degraded-note">
          {{ t('approvalDetail.blockFailed', { block: t('approvalDetail.impact') }) }}
        </div>
        <div v-else class="narr-grid">
          <div class="block"><div class="sk sk-w80"></div><div class="sk"></div><div class="sk sk-w60"></div></div>
          <div class="block"><div class="sk sk-w60"></div><div class="sk"></div><div class="sk sk-w40"></div></div>
        </div>
      </section>

      <!-- ⑤ 校验与风险 -->
      <section id="r" class="card">
        <h3 class="section-title">
          <span class="sec-num">5</span>{{ t('approvalDetail.risk') }}
          <span class="badge badge-neutral">{{ t('approvalDetail.validation') }}</span>
        </h3>
        <div v-if="riskContent">
          <div v-if="riskContent.validation_failures?.length" class="failures">
            <div v-for="(f, i) in riskContent.validation_failures" :key="i" class="fail-item">
              <div class="t">✗ {{ f.level }} · {{ f.layer }}</div>
              <div v-for="(chk, j) in f.checks" :key="j" class="fail-detail">· {{ chk }}</div>
              <div v-for="(fl, k) in f.failures" :key="k" class="fail-detail">· {{ fl }}</div>
            </div>
          </div>
          <div class="risk-label">{{ t('approvalDetail.riskList') }}</div>
          <div class="risk-list">
            <div v-for="(r, i) in riskContent.risks" :key="i" class="risk-item">⚠ {{ r }}</div>
            <div v-if="!riskContent.risks?.length" class="risk-item ok">✓ {{ t('approvalDetail.noRisks') }}</div>
          </div>
        </div>
        <div v-else class="hint">{{ t('approvalDetail.riskPending') }}</div>
      </section>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../utils/api.js'

const { t } = useI18n()
const route = useRoute()
const id = route.params.id
const approvalId = route.params.approvalId

const plan = ref(null)
const narr = ref(null)
const loading = ref(true)
let pollTimer = null

const layerColorMap = {
  ODS: '#007AFF', DIM: '#34C759', DWD: '#FF9500', DWS: '#FF3B30', ADS: '#AF52DE',
}

// Explicit expand state — mutating objects returned by a computed does not
// trigger re-renders, so layer/row open state lives in reactive Sets.
const openLayers = ref(new Set())
const openRows = ref(new Set())
const copiedTable = ref('')

function toggleLayer(name) {
  const s = new Set(openLayers.value)
  s.has(name) ? s.delete(name) : s.add(name)
  openLayers.value = s
}

function toggleRow(table) {
  const s = new Set(openRows.value)
  s.has(table) ? s.delete(table) : s.add(table)
  openRows.value = s
}

const sections = computed(() => [
  { id: 'g', label: `① ${t('approvalDetail.goal')}` },
  { id: 't', label: `② ${t('approvalDetail.tables')}` },
  { id: 'm', label: `③ ${t('approvalDetail.metrics')}` },
  { id: 'i', label: `④ ${t('approvalDetail.impact')}` },
  { id: 'r', label: `⑤ ${t('approvalDetail.risk')}` },
])

function blockOf(name) {
  return narr.value?.blocks?.find(b => b.block === name) || null
}
const goalBlock = computed(() => blockOf('goal_interpretation'))
const metricBlock = computed(() => blockOf('metric_interpretation'))
const impactBlock = computed(() => blockOf('expected_impact'))
const riskBlock = computed(() => blockOf('risk'))

const goalContent = computed(() => goalBlock.value?.status === 'ready' ? goalBlock.value.content : null)
const impactContent = computed(() => impactBlock.value?.status === 'ready' ? impactBlock.value.content : null)
const riskContent = computed(() => riskBlock.value?.content || null)

const hasHistoryNote = computed(() => !pendingNarratives.value.length && !loading.value)

const pendingNarratives = computed(() =>
  ['goal_interpretation', 'metric_interpretation', 'expected_impact']
    .filter(b => blockOf(b)?.status === 'pending'))

const banner = computed(() => {
  const failed = ['goal_interpretation', 'metric_interpretation', 'expected_impact']
    .map(b => blockOf(b)).filter(b => b?.status === 'failed')
  if (pendingNarratives.value.length) {
    return {
      kind: 'pending',
      text: t('approvalDetail.bannerPending', { count: pendingNarratives.value.length }),
    }
  }
  if (failed.length) {
    return {
      kind: 'fail',
      text: t('approvalDetail.bannerFailed', { count: failed.length }),
    }
  }
  return null
})

function badgeFor(block, readyText, pendingText, failedText) {
  const cls = block?.status === 'ready' ? 'badge-success'
    : block?.status === 'failed' ? 'badge-error' : 'badge-neutral'
  const text = block?.status === 'ready' ? readyText
    : block?.status === 'failed' ? failedText : pendingText
  return { cls, text }
}
const goalBadge = computed(() => badgeFor(goalBlock.value, t('approvalDetail.ready'), t('approvalDetail.generating'), t('approvalDetail.failed')))
const impactBadge = computed(() => badgeFor(impactBlock.value, t('approvalDetail.ready'), t('approvalDetail.generating'), t('approvalDetail.failed')))
const metricBadgeText = computed(() => {
  if (metricBlock.value?.status === 'ready') return t('approvalDetail.metricReady')
  if (metricBlock.value?.status === 'failed') return t('approvalDetail.metricFailed')
  return t('approvalDetail.metricGenerating')
})

// ── Layer tables with DDL from sql_artifacts ──
const totalTables = computed(() => layers.value.reduce((n, l) => n + l.tables.length, 0))

const layers = computed(() => {
  if (!plan.value) return []
  const planData = plan.value
  const sqlByTable = {}
  for (const s of planData.sql_artifacts || []) {
    sqlByTable[s.target_table] = sqlByTable[s.target_table] || s
  }
  const build = (defs, tableKey, sourceKey, syncKey, grainKey) =>
    (defs || []).map(d => ({
      table: d[tableKey] || '',
      source: Array.isArray(d[sourceKey])
        ? (d[sourceKey] || []).join(', ')
        : (d[sourceKey] || ''),
      sync: syncKey ? d[syncKey] || '' : '',
      fields: (d.fields || []).length,
      grain: d[grainKey] ? (Array.isArray(d[grainKey]) ? d[grainKey].join('、') : d[grainKey]) : '',
      ddl: sqlByTable[d[tableKey]] || null,
    }))
  return [
    { name: 'ODS', color: layerColorMap.ODS, tables: build(planData.ods_designs, 'ods_table', 'source_table', 'sync_method', null) },
    { name: 'DIM', color: layerColorMap.DIM, tables: build(planData.dim_designs, 'dim_table', 'source', null, null) },
    { name: 'DWD', color: layerColorMap.DWD, tables: build(planData.dwd_designs, 'fact_table', 'source_ods_tables', null, 'grain') },
    { name: 'DWS', color: layerColorMap.DWS, tables: build(planData.dws_designs, 'summary_table', 'source_tables', null, 'grain') },
    { name: 'ADS', color: layerColorMap.ADS, tables: build(planData.ads_designs, 'ads_table', 'source_dws_tables', null, 'grain') },
  ]
})

// ── Metrics with registry status + LLM interpretations ──
const metrics = computed(() => {
  if (!plan.value) return []
  const regMap = {}
  for (const ms of narr.value?.metric_status || []) regMap[ms.name] = ms
  const interpMap = {}
  if (metricBlock.value?.status === 'ready') {
    for (const i of metricBlock.value.content?.interpretations || []) interpMap[i.metric_name] = i.interpretation
  }
  const out = []
  const seen = new Set()
  for (const d of plan.value.dws_designs || []) {
    for (const m of d.measures || []) {
      if (!m.name || seen.has(m.name)) continue
      seen.add(m.name)
      out.push({
        name: m.name, layer: 'DWS', expression: m.expression,
        grain: m.grain || (d.grain || []).join('、'),
        source: d.summary_table, registered: !!regMap[m.name]?.registered,
        interpretation: interpMap[m.name] || '',
      })
    }
  }
  for (const d of plan.value.ads_designs || []) {
    for (const m of d.metrics || []) {
      if (!m.name || seen.has(m.name)) continue
      seen.add(m.name)
      out.push({
        name: m.name, layer: 'ADS', expression: m.expression,
        grain: (d.grain || []).join('、'),
        source: d.ads_table, registered: !!regMap[m.name]?.registered,
        interpretation: interpMap[m.name] || '',
      })
    }
  }
  return out
})

function displayStat(val) {
  if (Array.isArray(val)) return val.length ? val.join('；') : '—'
  if (typeof val === 'number' && val >= 1000) return val.toLocaleString()
  if (val === 0 || val === null || val === undefined) return '—'
  return String(val)
}

async function copyDdl(tbl) {
  try {
    await navigator.clipboard.writeText(tbl.ddl.sql)
    copiedTable.value = tbl.table
    setTimeout(() => (copiedTable.value = ''), 1200)
  } catch (e) {
    /* clipboard unavailable — ignore */
  }
}

async function loadNarratives() {
  try {
    narr.value = await api.getApprovalNarratives(id, approvalId)
  } catch (e) {
    console.error('Failed to load narratives:', e)
  }
}

// Approval status itself also needs refreshing — narratives may finish
// generating long before a human resolves the approval.
const approvalStatus = ref('')

async function loadApprovalStatus() {
  try {
    const list = await api.getApprovals(id)
    approvalStatus.value = (list || []).find(a => a.id === approvalId)?.status || ''
  } catch (e) {
    console.warn('Failed to load approval status:', e)
  }
}

async function load() {
  try {
    plan.value = await api.getDesignPlan(id)
  } catch (e) {
    plan.value = null
  }
  await Promise.all([loadNarratives(), loadApprovalStatus()])
}

onMounted(async () => {
  await load()
  loading.value = false
  pollTimer = setInterval(async () => {
    if (pendingNarratives.value.length) await loadNarratives()
    if (approvalStatus.value === 'pending') await loadApprovalStatus()
  }, 3000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})

watch(pendingNarratives, (pending) => {
  if (!pending.length && pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.page-header { display: flex; align-items: center; gap: var(--space-md); margin-bottom: var(--space-md); }
.approval-status-badge { margin-left: auto; text-transform: capitalize; }
.back-btn { text-decoration: none; color: var(--color-text-secondary); font-size: 18px; }
.detail-subtitle { color: var(--color-text-tertiary); font-size: 13px; margin-top: 2px; }
.hint-note { color: var(--color-text-quaternary); font-size: 12px; margin-left: 8px; }

/* ── Banner ─────────────────────────────────────────────── */
.narr-banner {
  display: flex; align-items: center; gap: 10px;
  border-radius: var(--radius-md); padding: 10px 14px;
  font-size: 13px; margin-bottom: var(--space-md);
  background: var(--color-accent-glass); color: var(--color-accent);
  border: 1px solid rgba(0,122,255,0.2);
}
.narr-banner.fail { background: var(--color-error-bg); color: var(--color-error); border-color: rgba(255,59,48,0.25); }
.spinner {
  width: 14px; height: 14px; flex: none;
  border: 2px solid rgba(0,122,255,0.25); border-top-color: var(--color-accent);
  border-radius: 50%; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Sticky nav ─────────────────────────────────────────── */
.sticky-nav {
  position: sticky; top: 56px; z-index: 5;
  display: flex; gap: 4px; flex-wrap: wrap;
  background: var(--color-bg-glass-heavy);
  backdrop-filter: blur(12px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 8px 12px; margin-bottom: var(--space-md);
}
.sticky-nav a { font-size: 12px; color: var(--color-text-secondary); text-decoration: none; padding: 4px 10px; border-radius: 6px; }
.sticky-nav a:hover { background: var(--color-accent-glass); color: var(--color-accent); }

/* ── Sections ───────────────────────────────────────────── */
.section-title { display: flex; align-items: center; gap: 8px; }
.sec-num {
  width: 18px; height: 18px; border-radius: 50%;
  background: var(--color-accent); color: #fff;
  font-size: 11px; display: inline-flex; align-items: center; justify-content: center;
}
.badge-success { background: var(--color-success-bg); color: var(--color-success); }
.badge-error { background: var(--color-error-bg); color: var(--color-error); }
.badge-warning { background: var(--color-warning-bg); color: var(--color-warning); }

/* ── Narratives ─────────────────────────────────────────── */
.narr-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.block { border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: 12px 14px; font-size: 13px; line-height: 1.7; }
.block h4 { font-size: 13px; color: var(--color-text-secondary); margin-bottom: 6px; }
.degraded-note {
  background: var(--color-warning-bg); color: var(--color-warning);
  border: 1px solid rgba(255,149,0,0.25); border-radius: var(--radius-md);
  padding: 10px 14px; font-size: 13px;
}
.sk { height: 12px; border-radius: 4px; margin-bottom: 8px; background: linear-gradient(90deg, var(--color-skeleton-base) 25%, var(--color-skeleton-shine) 50%, var(--color-skeleton-base) 75%); background-size: 200% 100%; animation: sk 1.2s infinite; }
.sk-w80 { width: 80%; } .sk-w60 { width: 60%; } .sk-w40 { width: 40%; }
@keyframes sk { to { background-position: -200% 0; } }

/* ── Layer groups + DDL ─────────────────────────────────── */
.layer-group { border: 1px solid var(--color-border); border-left-width: 3px; border-radius: var(--radius-md); overflow: hidden; margin-bottom: 10px; }
.layer-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px; cursor: pointer; font-size: 13px; }
.layer-head:hover { background: var(--color-bg); }
.dot { width: 8px; height: 8px; border-radius: 50%; }
.count { color: var(--color-text-tertiary); font-size: 12px; }
.arrow { margin-left: auto; color: var(--color-text-tertiary); transition: transform 0.15s; }
.layer-group.open .arrow { transform: rotate(90deg); }
.layer-body { display: none; border-top: 1px solid var(--color-border); }
.layer-group.open .layer-body { display: block; }
.table-row {
  display: grid; grid-template-columns: 1.4fr 1.2fr 0.9fr 60px 90px; gap: 10px;
  padding: 8px 14px; border-bottom: 1px solid var(--color-border); font-size: 13px; align-items: center; cursor: pointer;
}
.table-row.head { background: var(--color-bg); color: var(--color-text-tertiary); font-size: 12px; cursor: default; }
.table-row:last-child { border-bottom: none; }
.table-row .name { font-weight: 600; }
.table-row .src, .table-row .sync { font-size: 12px; color: var(--color-text-secondary); }
.ddl-block { display: none; grid-column: 1 / -1; background: #1f2d3d; color: #dce6f2; border-radius: var(--radius-md); padding: 10px 12px; margin: 6px 0 10px; }
.table-row.open .ddl-block { display: block; }
.ddl-head { display: flex; align-items: center; margin-bottom: 6px; }
.ddl-type { color: #8fb4e8; font-size: 12px; }
.copy-btn { margin-left: auto; background: transparent; border: 1px solid #46566b; color: #dce6f2; border-radius: 4px; padding: 2px 8px; font-size: 11px; cursor: pointer; }
.ddl-block pre { white-space: pre-wrap; word-break: break-all; font-size: 12px; line-height: 1.6; }

/* ── Metrics (stacked cards) ──────────────────────────────
   Card per metric: no horizontal squeeze, long interpretations get the
   full width. A table with fixed layout caused column content overlap. */
.metric-cards { display: flex; flex-direction: column; gap: 10px; }
.metric-card { border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: 12px 14px; }
.metric-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.metric-name { font-weight: 600; font-size: 14px; }
.expr { display: block; font-family: var(--font-mono, Consolas, monospace); font-size: 12px; color: var(--color-text-secondary); background: var(--color-bg); padding: 6px 8px; border-radius: 4px; word-break: break-all; }
.metric-meta { font-size: 12px; color: var(--color-text-secondary); margin: 6px 0; }
.metric-interp { font-size: 13px; line-height: 1.7; margin-top: 6px; }
.metric-interp.empty { color: var(--color-text-quaternary); font-style: italic; }

/* ── Impact ─────────────────────────────────────────────── */
.impact-narr { font-size: 13px; line-height: 1.8; margin-bottom: 14px; }
.impact-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat { border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: 12px; text-align: center; }
.stat .num { font-size: 22px; font-weight: 700; color: var(--color-accent); }
.stat .lab { font-size: 12px; color: var(--color-text-tertiary); margin-top: 4px; }

/* ── Risk ───────────────────────────────────────────────── */
.fail-item { border: 1px solid rgba(255,59,48,0.25); background: var(--color-error-bg); border-radius: var(--radius-md); padding: 10px 14px; margin-bottom: 8px; font-size: 13px; }
.fail-item .t { font-weight: 600; color: var(--color-error); margin-bottom: 4px; }
.fail-detail { color: var(--color-text-secondary); font-size: 12px; }
.risk-label { font-size: 13px; font-weight: 600; margin: 14px 0 8px; }
.risk-list { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; font-size: 13px; }
.risk-item { display: flex; gap: 8px; padding: 8px 10px; border: 1px solid rgba(255,149,0,0.3); background: var(--color-warning-bg); border-radius: var(--radius-md); color: var(--color-warning); }
.risk-item.ok { border-color: rgba(52,199,89,0.3); background: var(--color-success-bg); color: var(--color-success); }

@media (max-width: 768px) {
  .narr-grid { grid-template-columns: 1fr; }
  .impact-stats { grid-template-columns: repeat(2, 1fr); }
  .table-row { grid-template-columns: 1fr 1fr; }
}
</style>
