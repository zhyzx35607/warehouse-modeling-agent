<template>
  <div class="catalog">
    <!-- Fullscreen header -->
    <header class="catalog-header">
      <button class="back-btn" aria-label="back" @click="goBack">←</button>
      <div class="flex-1">
        <h2>{{ $t('catalog.title') }}</h2>
        <p class="page-subtitle">{{ $t('catalog.subtitle') }}</p>
      </div>
      <button class="btn btn-sm" @click="goMetrics">{{ $t('catalog.viewMetrics') }}</button>
      <button class="btn btn-sm" @click="loadAll" :disabled="loadingSources">
        {{ $t('catalog.refresh') }}
      </button>
    </header>

    <div class="catalog-body">
      <!-- ── Column 1: repositories (sources + databases) ── -->
      <section class="panel">
        <div class="panel-header">{{ $t('catalog.repositories') }}</div>
        <div class="panel-scroll">
          <div v-if="loadingSources" class="loading-mini">{{ $t('catalog.loading') }}</div>
          <div v-else-if="!sources.length" class="panel-empty">{{ $t('catalog.noSources') }}</div>
          <div v-else class="source-list">
            <div v-for="src in sources" :key="src.id" class="source-card">
              <button class="source-btn" @click="toggleSource(src)">
                <span class="source-title">{{ src.name }}</span>
                <span class="source-sub">
                  {{ src.db_type }} · {{ src.host }}:{{ src.port }}
                  <template v-if="src.table_count != null"> · {{ src.table_count }} 表</template>
                </span>
              </button>

              <div v-if="isExpanded(src.id)" class="db-list">
                <div v-if="dbsLoading[src.id]" class="loading-mini">{{ $t('catalog.loading') }}</div>
                <template v-else>
                  <button v-for="db in sourceDbs[src.id] || []" :key="db.name"
                          :class="['db-item', { selected: selectedSourceId === src.id && selectedDb === db.name }]"
                          @click="selectDb(src, db)">
                    <span class="db-name mono">{{ db.name }}</span>
                    <span v-for="l in db.layers" :key="l" class="tag-chip">{{ l }}</span>
                  </button>
                  <div v-if="!(sourceDbs[src.id] || []).length" class="panel-empty">
                    {{ $t('catalog.noDatabases') }}
                  </div>
                </template>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- ── Column 2: tables + comments ── -->
      <section class="panel">
        <div class="panel-header">{{ $t('catalog.tables') }}</div>
        <div class="panel-scroll">
          <div v-if="loadingTables" class="loading-mini">{{ $t('catalog.loading') }}</div>
          <div v-else-if="!selectedSourceId" class="panel-empty">{{ $t('catalog.noTables') }}</div>
          <div v-else-if="!tables.length" class="panel-empty">{{ $t('catalog.noTables') }}</div>
          <div v-else class="table-list">
            <button v-for="t in tables" :key="t.table_name"
                    :class="['table-item', { selected: selectedTable === t.table_name }]"
                    @click="selectTable(t)">
              <span class="table-name mono">{{ t.table_name }}</span>
              <span class="table-comment">{{ t.table_comment || '—' }}</span>
            </button>
          </div>
        </div>
      </section>

      <!-- ── Column 3: wide data table (two-row header + paged rows) ── -->
      <section class="panel">
        <div class="panel-header panel-header-meta">
          <span>{{ $t('catalog.columns') }}</span>
          <span v-if="selectedTable && total != null" class="header-meta">
            {{ $t('catalog.totalRows', { total }) }}
            <span v-if="redactedFields.length" class="tag-chip redacted-chip">
              {{ $t('catalog.redactedHint', { count: redactedFields.length }) }}
            </span>
          </span>
        </div>
        <div class="panel-scroll">
          <div v-if="loadingRows" class="loading-mini">{{ $t('catalog.loading') }}</div>
          <div v-else-if="!selectedTable" class="panel-empty">{{ $t('catalog.noColumns') }}</div>
          <template v-else>
            <div class="rows-scroll">
              <table class="rows-table">
                <thead>
                  <tr>
                    <th v-for="c in columns" :key="c.column_name">
                      <span class="th-name mono">{{ c.column_name }}</span>
                      <span class="th-type mono">{{ c.data_type }}</span>
                    </th>
                  </tr>
                  <tr>
                    <th v-for="c in columns" :key="c.column_name" class="th-comment">
                      {{ c.column_comment || '—' }}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(r, ri) in rows" :key="ri">
                    <td v-for="c in columns" :key="c.column_name" class="mono">
                      <span v-if="isRedacted(c.column_name)" class="redacted">[REDACTED]</span>
                      <template v-else>{{ formatCell(r[c.column_name]) }}</template>
                    </td>
                  </tr>
                  <tr v-if="!rows.length">
                    <td :colspan="Math.max(1, columns.length)" class="row-empty">
                      {{ $t('catalog.noRows') }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="pagination">
              <span class="page-info">{{ $t('catalog.pageInfo', { page, totalPages }) }}</span>
              <button class="btn btn-sm" :disabled="page <= 1" @click="goPage(page - 1)">
                {{ $t('catalog.prevPage') }}
              </button>
              <button class="btn btn-sm" :disabled="page >= totalPages" @click="goPage(page + 1)">
                {{ $t('catalog.nextPage') }}
              </button>
            </div>
          </template>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { useRouter } from 'vue-router'
import { api } from '../utils/api'

const { t } = useI18n()
const message = useMessage()
const router = useRouter()

const PAGE_SIZE = 50

// ── Column 1: sources + databases ─────────────────────────────
const sources = ref([])
const loadingSources = ref(false)
const sourceDbs = reactive({})        // sourceId -> [db, ...] (cached)
const dbsLoading = reactive({})       // sourceId -> bool
const expandedIds = ref([])

const selectedSourceId = ref('')
const selectedDb = ref('')

// ── Column 2: tables ──────────────────────────────────────────
const tables = ref([])
const loadingTables = ref(false)
const selectedTable = ref('')

// ── Column 3: wide data table ─────────────────────────────────
const columns = ref([])               // [{column_name, data_type, column_comment}]
const rows = ref([])
const total = ref(0)
const page = ref(1)
const redactedFields = ref([])
const loadingRows = ref(false)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/metrics')
}

function goMetrics() {
  router.push('/metrics')
}

async function loadAll() {
  loadingSources.value = true
  // reset selection + cache on refresh
  selectedSourceId.value = ''
  selectedDb.value = ''
  selectedTable.value = ''
  tables.value = []
  columns.value = []
  rows.value = []
  total.value = 0
  page.value = 1
  redactedFields.value = []
  expandedIds.value = []
  Object.keys(sourceDbs).forEach((k) => delete sourceDbs[k])
  Object.keys(dbsLoading).forEach((k) => delete dbsLoading[k])
  try {
    const data = await api.getCatalogSources()
    sources.value = data.sources || []
  } catch (e) {
    sources.value = []
    message.error(t('catalog.subtitle') + ': ' + (e.message || ''))
  } finally {
    loadingSources.value = false
  }
}

function isExpanded(id) {
  return expandedIds.value.includes(id)
}

async function toggleSource(src) {
  const idx = expandedIds.value.indexOf(src.id)
  if (idx >= 0) {
    expandedIds.value.splice(idx, 1)
    return
  }
  expandedIds.value.push(src.id)
  if (!sourceDbs[src.id]) await loadSourceDbs(src)
}

async function loadSourceDbs(src) {
  dbsLoading[src.id] = true
  try {
    const data = await api.getCatalogDatabases(src.id)
    sourceDbs[src.id] = data.databases || []
  } catch (e) {
    sourceDbs[src.id] = []
    message.error(t('catalog.subtitle') + ': ' + (e.message || ''))
  } finally {
    dbsLoading[src.id] = false
  }
}

async function selectDb(src, db) {
  selectedSourceId.value = src.id
  selectedDb.value = db.name
  selectedTable.value = ''
  columns.value = []
  rows.value = []
  total.value = 0
  redactedFields.value = []
  loadingTables.value = true
  try {
    const data = await api.getCatalogTables(src.id, db.name)
    tables.value = data.tables || []
  } catch (e) {
    tables.value = []
    message.error(t('catalog.subtitle') + ': ' + (e.message || ''))
  } finally {
    loadingTables.value = false
  }
}

async function selectTable(t) {
  selectedTable.value = t.table_name
  await loadRows(1)
}

async function goPage(p) {
  if (p < 1 || p > totalPages.value) return
  await loadRows(p)
}

async function loadRows(p) {
  loadingRows.value = true
  try {
    const data = await api.getCatalogRows(
      selectedSourceId.value, selectedDb.value, selectedTable.value, p, PAGE_SIZE,
    )
    columns.value = data.columns || []
    rows.value = data.rows || []
    total.value = data.total || 0
    page.value = data.page || p
    redactedFields.value = data.redacted_fields || []
  } catch (e) {
    columns.value = []
    rows.value = []
    total.value = 0
    redactedFields.value = []
    message.error(t('catalog.subtitle') + ': ' + (e.message || ''))
  } finally {
    loadingRows.value = false
  }
}

function isRedacted(name) {
  return redactedFields.value.includes(name)
}

function formatCell(v) {
  if (v === null || v === undefined || v === '') return '—'
  return String(v)
}

loadAll()
</script>

<style scoped>
/* ── Fullscreen overlay (covers the app, below modals) ─────── */
.catalog {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--color-bg);
}

.catalog-header {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  padding: 0 var(--space-lg);
  height: var(--header-height);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-glass);
  backdrop-filter: saturate(180%) blur(20px);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
}

.page-subtitle {
  font-size: 0.8125rem;
  color: var(--color-text-tertiary);
  margin: 2px 0 0;
}

.flex-1 { flex: 1; }
.mono { font-family: var(--font-mono); font-size: 0.71875rem; }

/* ── Three-column layout fills the viewport ────────────────── */
.catalog-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(170px, 0.55fr) minmax(200px, 0.8fr) minmax(400px, 2fr);
  gap: var(--space-md);
  padding: var(--space-md) var(--space-lg);
}

.panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--color-bg);
}

.panel-header {
  padding: var(--space-xs) var(--space-sm);
  background: var(--color-bg);
  border-bottom: 1px solid var(--color-border);
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  flex: none;
}

.panel-header-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-sm);
}

.header-meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
}

.panel-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.loading-mini {
  padding: var(--space-lg);
  text-align: center;
  color: var(--color-text-tertiary);
  font-size: 0.75rem;
}

.panel-empty {
  padding: var(--space-lg);
  text-align: center;
  color: var(--color-text-tertiary);
  font-size: 0.75rem;
}

/* ── Column 1: sources + databases ─────────────────────────── */
.source-list, .table-list {
  padding-bottom: 4px;
}

.source-card {
  border-bottom: 1px solid var(--color-border);
}
.source-card:last-child { border-bottom: none; }

.source-btn {
  width: 100%;
  text-align: left;
  padding: var(--space-sm) var(--space-sm);
  background: none;
  border: none;
  cursor: pointer;
  transition: background .15s;
}
.source-btn:hover { background: var(--color-bg-secondary, rgba(127,127,127,.08)); }

.source-title { display: block; font-weight: 600; font-size: 0.8125rem; }
.source-sub {
  display: block;
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  margin-top: 2px;
}

.db-list {
  padding: 2px 0 6px;
}

.db-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: calc(100% - var(--space-md));
  margin: 0 var(--space-sm);
  padding: 5px 8px;
  border: none;
  border-radius: var(--radius-xs);
  background: none;
  text-align: left;
  cursor: pointer;
  font-size: 0.75rem;
  transition: background .15s;
}
.db-item:hover { background: var(--color-bg-secondary, rgba(127,127,127,.08)); }
.db-item.selected {
  background: rgba(64,158,255,.12);
  color: var(--color-accent);
}
.db-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.tag-chip {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--color-bg-secondary, rgba(127,127,127,.12));
  font-size: 0.6875rem;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.redacted-chip { color: var(--color-warning, #ff9500); }

/* ── Column 2: tables ──────────────────────────────────────── */
.table-item {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  width: 100%;
  padding: 7px var(--space-sm);
  border: none;
  border-bottom: 1px solid var(--color-border);
  background: none;
  text-align: left;
  font-size: 0.75rem;
  cursor: pointer;
  transition: background .15s;
}
.table-item:last-child { border-bottom: none; }
.table-item:hover { background: var(--color-bg-secondary, rgba(127,127,127,.08)); }
.table-item.selected {
  background: rgba(64,158,255,.12);
  color: var(--color-accent);
}
.table-name { font-weight: 500; white-space: nowrap; }
.table-comment {
  flex: 1;
  color: var(--color-text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── Column 3: wide data table ─────────────────────────────── */
.rows-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.rows-table {
  width: max-content;
  min-width: 100%;
  font-size: 0.75rem;
  border-collapse: separate;
  border-spacing: 0;
}

.rows-table thead {
  position: sticky;
  top: 0;
  z-index: 2;
}

.rows-table th {
  text-align: left;
  padding: 4px var(--space-sm);
  background: var(--color-bg);
  font-weight: 600;
  color: var(--color-text-secondary);
  border-bottom: 1px solid var(--color-border);
  border-right: 1px solid var(--color-border);
  white-space: nowrap;
}
.rows-table th:last-child { border-right: none; }

.th-name { display: block; font-weight: 600; font-size: 0.71875rem; }
.th-type {
  display: block;
  font-size: 0.625rem;
  font-weight: 400;
  color: var(--color-text-tertiary);
  margin-top: 1px;
}
.th-comment {
  font-weight: 500;
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  border-top: none;
}

.rows-table td {
  padding: 4px var(--space-sm);
  border-bottom: 1px solid var(--color-border);
  border-right: 1px solid var(--color-border);
  white-space: nowrap;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--color-text);
}
.rows-table td:last-child { border-right: none; }

.rows-table tr:hover td { background: var(--color-bg-secondary, rgba(127,127,127,.04)); }

.redacted { color: var(--color-warning, #ff9500); }
.row-empty {
  text-align: center;
  color: var(--color-text-tertiary);
  padding: var(--space-lg);
  white-space: normal;
}

.pagination {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  border-top: 1px solid var(--color-border);
  flex: none;
}
.page-info { font-size: 0.75rem; color: var(--color-text-tertiary); }

/* ── Responsive ────────────────────────────────────────────── */
@media (max-width: 1200px) {
  .catalog-body {
    grid-template-columns: minmax(150px, 0.5fr) minmax(170px, 0.7fr) minmax(340px, 1.8fr);
  }
}

@media (max-width: 900px) {
  .catalog-body {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }
  .panel-scroll { overflow-y: visible; }
  .rows-scroll { max-height: 60vh; }
}
</style>
