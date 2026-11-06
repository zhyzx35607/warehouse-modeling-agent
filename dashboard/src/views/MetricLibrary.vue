<template>
  <div class="metric-library">
    <!-- Page header -->
    <div class="page-header">
      <router-link to="/" class="back-btn" aria-label="back">←</router-link>
      <div class="flex-1">
        <h2>{{ $t('metrics.title') }}</h2>
        <p class="page-subtitle">{{ $t('metrics.subtitle') }}</p>
      </div>
      <div class="header-actions">
        <button class="btn btn-sm" @click="goCatalog">
          🗺️ {{ $t('catalog.title') }}
        </button>
        <template v-if="activeTab === 'metrics'">
          <button class="btn btn-primary btn-sm" @click="openWizard">
            {{ $t('metrics.generateBtn') }}
          </button>
          <button class="btn btn-primary btn-sm" @click="openCreate()">
            {{ $t('metrics.createBtn') }}
          </button>
          <button class="btn btn-sm" @click="loadMetrics" :disabled="loading">
            {{ $t('metrics.refresh') }}
          </button>
        </template>
        <template v-else>
          <button class="btn btn-primary btn-sm" @click="openScanModal">
            {{ $t('dimensions.scanBtn') }}
          </button>
          <button class="btn btn-primary btn-sm" @click="openDimCreate()">
            {{ $t('dimensions.createBtn') }}
          </button>
          <button class="btn btn-sm" @click="loadDimensions" :disabled="dimLoading">
            {{ $t('dimensions.refresh') }}
          </button>
        </template>
      </div>
    </div>

    <!-- Tabs -->
    <div class="library-tabs">
      <button :class="['library-tab', { active: activeTab === 'metrics' }]"
              @click="switchTab('metrics')">{{ $t('metrics.tabMetrics') }}</button>
      <button :class="['library-tab', { active: activeTab === 'dimensions' }]"
              @click="switchTab('dimensions')">{{ $t('dimensions.tabDimensions') }}</button>
    </div>

    <!-- ── Metrics tab ──────────────────────────────────── -->
    <div v-if="activeTab === 'metrics'">
    <!-- Toolbar -->
    <div class="toolbar">
      <input v-model="search" class="input input-sm"
             :placeholder="$t('metrics.search')" @keyup.enter="onSearch" />
      <input v-model="filterSourceTable" class="input input-sm"
             :placeholder="$t('metrics.filterSourceTable')" @keyup.enter="onSearch" />
      <select v-model="filterType" class="select select-sm" @change="onSearch">
        <option value="">{{ $t('metrics.allTypes') }}</option>
        <option value="atomic">{{ $t('metrics.typeAtomic') }}</option>
        <option value="derived">{{ $t('metrics.typeDerived') }}</option>
      </select>
    </div>

    <!-- Table -->
    <div v-if="metrics.length" class="card table-card">
      <div class="table-wrapper">
        <table class="metric-table">
          <thead>
            <tr>
              <th>{{ $t('metrics.columnName') }}</th>
              <th>{{ $t('metrics.columnType') }}</th>
              <th>{{ $t('metrics.columnDefinition') }}</th>
              <th>{{ $t('metrics.columnSourceTable') }}</th>
              <th>{{ $t('metrics.columnGrain') }}</th>
              <th>{{ $t('metrics.columnTags') }}</th>
              <th>{{ $t('metrics.columnCreated') }}</th>
              <th>{{ $t('metrics.columnActions') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in metrics" :key="m.metric_id">
              <td class="metric-name">{{ m.metric_name }}</td>
              <td>
                <span :class="['badge', m.metric_type === 'derived' ? 'badge-warning' : 'badge-success']">
                  {{ m.metric_type === 'derived' ? $t('metrics.typeDerived') : $t('metrics.typeAtomic') }}
                </span>
              </td>
              <td class="metric-def mono">{{ m.definition_sql }}</td>
              <td class="mono">{{ m.source_table }}</td>
              <td class="metric-grain">{{ m.grain }}</td>
              <td class="metric-tags">
                <span v-for="tag in m.tags" :key="tag" class="tag-chip">{{ tag }}</span>
              </td>
              <td class="metric-created">{{ formatDate(m.created_at) }}</td>
              <td class="row-actions">
                <button class="btn btn-ghost btn-xs" @click="openEdit(m)">
                  {{ $t('metrics.edit') }}
                </button>
                <button class="btn btn-danger-ghost btn-xs" @click="confirmDelete(m)">
                  {{ $t('metrics.delete') }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="totalPages > 1" class="pagination">
        <button class="btn btn-sm" :disabled="page <= 1" @click="goPage(page - 1)">
          {{ $t('metrics.pagePrev') }}
        </button>
        <span class="page-info">{{ page }} / {{ totalPages }}</span>
        <button class="btn btn-sm" :disabled="page >= totalPages" @click="goPage(page + 1)">
          {{ $t('metrics.pageNext') }}
        </button>
      </div>
    </div>

    <!-- Empty state -->
    <div v-else-if="!loading" class="card empty-state">
      <div class="empty-state-icon">📊</div>
      <div class="empty-state-title">{{ $t('metrics.noMetrics') }}</div>
      <div class="empty-state-desc">{{ $t('metrics.noMetricsDesc') }}</div>
    </div>

    <!-- Loading -->
    <div v-else class="loading-section">
      <div class="skeleton skeleton-title" style="width:30%;"></div>
      <div v-for="i in 5" :key="i" class="skeleton" style="height:48px;margin-bottom:8px;"></div>
    </div>
    </div><!-- /metrics tab -->

    <!-- ── Dimensions tab ───────────────────────────────── -->
    <div v-if="activeTab === 'dimensions'">
      <!-- Toolbar -->
      <div class="toolbar">
        <input v-model="dimSearch" class="input input-sm"
               :placeholder="$t('dimensions.search')" @keyup.enter="onDimSearch" />
        <input v-model="dimTableFilter" class="input input-sm"
               :placeholder="$t('dimensions.filterTable')" @keyup.enter="onDimSearch" />
      </div>

      <!-- Table -->
      <div v-if="dimensions.length" class="card table-card">
        <div class="table-wrapper">
          <table class="metric-table">
            <thead>
              <tr>
                <th>{{ $t('dimensions.columnName') }}</th>
                <th>{{ $t('dimensions.columnTable') }}</th>
                <th>{{ $t('dimensions.columnDb') }}</th>
                <th>{{ $t('dimensions.columnKeys') }}</th>
                <th>{{ $t('dimensions.columnGrain') }}</th>
                <th>{{ $t('dimensions.columnFields') }}</th>
                <th>{{ $t('dimensions.columnVariant') }}</th>
                <th>{{ $t('dimensions.columnOwner') }}</th>
                <th>{{ $t('dimensions.columnCreated') }}</th>
                <th>{{ $t('dimensions.columnActions') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="d in dimensions" :key="d.dim_id">
                <td class="metric-name">{{ d.dim_name }}</td>
                <td class="mono">{{ d.dim_table }}</td>
                <td class="mono text-muted">{{ d.db_name || '—' }}</td>
                <td class="metric-grain">{{ (d.natural_keys || []).join(', ') }}</td>
                <td class="metric-grain">{{ d.grain }}</td>
                <td class="metric-tags">
                  <span class="tag-chip">{{ (d.fields || []).length }} fields</span>
                </td>
                <td>
                  <span v-if="d.variant_of" class="tag-chip">{{ d.variant_of }}</span>
                  <span v-else class="text-muted">—</span>
                </td>
                <td class="mono text-muted">{{ shortOwner(d.owner_task_id) }}</td>
                <td class="metric-created">{{ formatDate(d.created_at) }}</td>
                <td class="row-actions">
                  <button class="btn btn-ghost btn-xs" @click="openDimEdit(d)">
                    {{ $t('dimensions.edit') }}
                  </button>
                  <button class="btn btn-danger-ghost btn-xs" @click="confirmDimDelete(d)">
                    {{ $t('dimensions.delete') }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="dimTotalPages > 1" class="pagination">
          <button class="btn btn-sm" :disabled="dimPage <= 1" @click="goDimPage(dimPage - 1)">
            {{ $t('dimensions.pagePrev') }}
          </button>
          <span class="page-info">{{ dimPage }} / {{ dimTotalPages }}</span>
          <button class="btn btn-sm" :disabled="dimPage >= dimTotalPages" @click="goDimPage(dimPage + 1)">
            {{ $t('dimensions.pageNext') }}
          </button>
        </div>
      </div>

      <!-- Empty state -->
      <div v-else-if="!dimLoading" class="card empty-state">
        <div class="empty-state-icon">🗂️</div>
        <div class="empty-state-title">{{ $t('dimensions.noDimensions') }}</div>
        <div class="empty-state-desc">{{ $t('dimensions.noDimensionsDesc') }}</div>
      </div>

      <!-- Loading -->
      <div v-else class="loading-section">
        <div class="skeleton skeleton-title" style="width:30%;"></div>
        <div v-for="i in 5" :key="i" class="skeleton" style="height:48px;margin-bottom:8px;"></div>
      </div>
    </div><!-- /dimensions tab -->

    <!-- Wizard modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showWizard" class="modal-overlay" @click.self="closeWizard">
          <div class="modal-card modal-card-wide">
            <div class="modal-header">
              <h3>{{ $t('metrics.wizard.title') }}</h3>
              <button class="modal-close" @click="closeWizard">×</button>
            </div>

            <!-- Step indicator -->
            <div class="wizard-steps">
              <div v-for="(label, i) in stepLabels" :key="i"
                   :class="['wizard-step', { active: wizardStep === i + 1, done: wizardStep > i + 1 }]">
                <span class="wizard-step-dot">{{ wizardStep > i + 1 ? '✓' : i + 1 }}</span>
                <span class="wizard-step-label">{{ label }}</span>
              </div>
            </div>

            <!-- Step 1: sources -->
            <div v-if="wizardStep === 1" class="wizard-body">
              <div v-if="wizardLoading" class="loading-mini">{{ $t('metrics.loading') }}</div>
              <div v-else class="pick-list">
                <button v-for="src in wizardSources" :key="src.id"
                        class="pick-card" @click="wizardSelectSource(src)">
                  <span class="pick-title">{{ src.name }}</span>
                  <span class="pick-sub">
                    {{ src.db_type }} · {{ src.host }}:{{ src.port }}
                    <span v-if="src.has_layers" class="tag-chip">warehouse</span>
                  </span>
                </button>
                <div v-if="!wizardSources.length" class="empty-state-desc">
                  {{ $t('metrics.noMetricsDesc') }}
                </div>
              </div>
            </div>

            <!-- Step 2: databases -->
            <div v-else-if="wizardStep === 2" class="wizard-body">
              <div v-if="wizardLoading" class="loading-mini">{{ $t('metrics.loading') }}</div>
              <div v-else class="pick-list">
                <button v-for="db in wizardDatabases" :key="db.name"
                        class="pick-card" @click="wizardSelectDb(db)">
                  <span class="pick-title">{{ db.name }}</span>
                  <span class="pick-sub">
                    <span v-for="l in db.layers" :key="l" class="tag-chip">{{ l }}</span>
                  </span>
                </button>
              </div>
              <div class="wizard-nav">
                <button class="btn btn-sm" @click="wizardStep = 1">{{ $t('metrics.wizard.back') }}</button>
              </div>
            </div>

            <!-- Step 3: tables -->
            <div v-else-if="wizardStep === 3" class="wizard-body wizard-tables">
              <div class="tables-panel">
                <div class="tables-header">
                  <span class="pick-sub">{{ $t('metrics.wizard.tablesHint') }}</span>
                  <div class="tables-actions">
                    <button class="btn btn-ghost btn-xs" @click="selectAllTables">全选</button>
                    <button class="btn btn-ghost btn-xs" @click="wizardSelectedTables = []">清空</button>
                  </div>
                </div>
                <div v-if="wizardLoading" class="loading-mini">{{ $t('metrics.loading') }}</div>
                <div v-else class="table-list">
                  <label v-for="t in wizardTables" :key="t.table_name"
                         class="table-item" :class="{ checked: wizardSelectedTables.includes(t.table_name) }">
                    <input type="checkbox" :value="t.table_name"
                           v-model="wizardSelectedTables"
                           @change="wizardLoadColumns(t.table_name)" />
                    <span class="table-name mono">{{ t.table_name }}</span>
                    <span class="table-comment">{{ t.table_comment }}</span>
                  </label>
                </div>
              </div>
              <div class="columns-panel">
                <div class="columns-header">{{ $t('metrics.wizard.previewColumns') }}</div>
                <div v-if="!previewTable" class="empty-state-desc">
                  {{ $t('metrics.wizard.noColumns') }}
                </div>
                <div v-else class="column-list">
                  <div v-for="c in previewColumns" :key="c.column_name" class="column-item">
                    <span class="column-name mono">{{ c.column_name }}</span>
                    <span class="column-type mono">{{ c.data_type }}</span>
                    <span class="column-comment">{{ c.column_comment }}</span>
                    <span :class="['badge', c.is_metric_candidate ? 'badge-success' : 'badge-neutral']">
                      {{ c.is_metric_candidate ? $t('metrics.wizard.previewMetric') : $t('metrics.wizard.previewNonMetric') }}
                    </span>
                  </div>
                </div>
              </div>
              <div class="wizard-nav">
                <button class="btn btn-sm" @click="wizardStep = 2">{{ $t('metrics.wizard.back') }}</button>
                <button class="btn btn-primary btn-sm" :disabled="!wizardSelectedTables.length"
                        @click="wizardGenerate">
                  {{ $t('metrics.wizard.startGenerate') }} ({{ wizardSelectedTables.length }})
                </button>
              </div>
            </div>

            <!-- Step 4: generate result -->
            <div v-else class="wizard-body">
              <div v-if="wizardGenerating" class="generating">
                <div class="spinner"></div>
                <div>{{ $t('metrics.wizard.generating') }}</div>
              </div>
              <div v-else-if="wizardResult" class="result-summary">
                <div class="result-line">
                  {{ $t('metrics.msg.generated', { generated: wizardResult.generated, skipped: wizardResult.skipped }) }}
                </div>
                <div v-if="wizardResult.metrics.length" class="result-list">
                  <div v-for="m in wizardResult.metrics" :key="m.metric_name + m.source_table"
                       class="result-item">
                    <span :class="['badge', m.status === 'created' ? 'badge-success' : 'badge-neutral']">
                      {{ m.status === 'created' ? '✓' : 'skip' }}
                    </span>
                    <span class="mono">{{ m.metric_name }}</span>
                    <span class="pick-sub mono">{{ m.source_table }}</span>
                  </div>
                </div>
                <div class="wizard-nav">
                  <button class="btn btn-primary btn-sm" @click="finishWizard">
                    {{ $t('metrics.wizard.finish') }}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Create/Edit modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showFormModal" class="modal-overlay" @click.self="showFormModal = false">
          <div class="modal-card">
            <div class="modal-header">
              <h3>{{ editingMetric ? $t('metrics.form.editTitle') : $t('metrics.form.createTitle') }}</h3>
              <button class="modal-close" @click="showFormModal = false">×</button>
            </div>
            <div class="form-body">
              <label class="field">
                <span>{{ $t('metrics.form.name') }}</span>
                <input v-model="formData.metric_name" class="input"
                       :placeholder="$t('metrics.form.namePlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('metrics.form.type') }}</span>
                <select v-model="formData.metric_type" class="select">
                  <option value="atomic">{{ $t('metrics.typeAtomic') }}</option>
                  <option value="derived">{{ $t('metrics.typeDerived') }}</option>
                </select>
              </label>
              <label class="field">
                <span>{{ $t('metrics.form.definition') }}</span>
                <input v-model="formData.definition_sql" class="input mono"
                       :placeholder="$t('metrics.form.definitionPlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('metrics.form.sourceTable') }}</span>
                <input v-model="formData.source_table" class="input mono"
                       :placeholder="$t('metrics.form.sourceTablePlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('metrics.form.grain') }}</span>
                <input v-model="formData.grain" class="input"
                       :placeholder="$t('metrics.form.grainPlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('metrics.form.parentMetrics') }}</span>
                <input v-model="formParentText" class="input"
                       :placeholder="$t('metrics.form.parentMetrics')" />
              </label>
              <label class="field">
                <span>{{ $t('metrics.form.tags') }}</span>
                <input v-model="formTagsText" class="input"
                       :placeholder="$t('metrics.form.tags')" />
              </label>
            </div>
            <div class="modal-footer">
              <button class="btn btn-sm" @click="showFormModal = false">{{ $t('metrics.wizard.close') }}</button>
              <button class="btn btn-primary btn-sm" :disabled="saving" @click="saveMetric">
                {{ saving ? $t('metrics.form.saving') : $t('metrics.form.save') }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Delete confirm modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="deleteTarget" class="modal-overlay" @click.self="deleteTarget = null">
          <div class="modal-card modal-card-sm">
            <div class="modal-header">
              <h3>{{ $t('metrics.delete') }}</h3>
            </div>
            <div class="form-body">
              {{ $t('metrics.deleteConfirm', { name: deleteTarget.metric_name }) }}
            </div>
            <div class="modal-footer">
              <button class="btn btn-sm" @click="deleteTarget = null">{{ $t('metrics.wizard.close') }}</button>
              <button class="btn btn-danger btn-sm" @click="doDelete">
                {{ $t('metrics.delete') }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Scan history dimensions modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showScanModal" class="modal-overlay" @click.self="closeScanModal">
          <div class="modal-card modal-card-sm">
            <div class="modal-header">
              <h3>{{ $t('dimensions.scanTitle') }}</h3>
              <button class="modal-close" @click="closeScanModal">×</button>
            </div>
            <div class="form-body">
              <p class="pick-sub">{{ $t('dimensions.scanHint') }}</p>
              <label class="field">
                <span>{{ $t('dimensions.scanLimit') }}</span>
                <input v-model.number="scanLimit" type="number" min="1" max="500" class="input" />
              </label>
              <div v-if="scanning" class="generating">
                <div class="spinner"></div>
                <div>{{ $t('dimensions.scanning') }}</div>
              </div>
              <div v-else-if="scanResult" class="result-summary">
                <div class="result-line">
                  {{ $t('dimensions.scanDone', { tasks: scanResult.scanned_tasks, registered: scanResult.registered, skipped: scanResult.skipped }) }}
                </div>
                <div v-if="scanResult.dimensions.length" class="result-list">
                  <div v-for="(d, i) in scanResult.dimensions" :key="i" class="result-item">
                    <span :class="['badge', d.status === 'created' ? 'badge-success' : 'badge-neutral']">
                      {{ d.status === 'created' ? '✓' : 'skip' }}
                    </span>
                    <span class="mono">{{ d.dim_name }}</span>
                    <span class="pick-sub mono">{{ d.dim_table }}</span>
                  </div>
                </div>
              </div>
            </div>
            <div class="modal-footer">
              <button class="btn btn-sm" @click="closeScanModal">{{ $t('dimensions.wizard.close') }}</button>
              <button v-if="!scanning" class="btn btn-primary btn-sm" :disabled="scanning" @click="doScan">
                {{ $t('dimensions.scanBtn') }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Dimension create/edit modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showDimFormModal" class="modal-overlay" @click.self="showDimFormModal = false">
          <div class="modal-card">
            <div class="modal-header">
              <h3>{{ editingDimension ? $t('dimensions.form.editTitle') : $t('dimensions.form.createTitle') }}</h3>
              <button class="modal-close" @click="showDimFormModal = false">×</button>
            </div>
            <div class="form-body">
              <label class="field">
                <span>{{ $t('dimensions.form.name') }}</span>
                <input v-model="dimFormData.dim_name" class="input"
                       :placeholder="$t('dimensions.form.namePlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('dimensions.form.table') }}</span>
                <input v-model="dimFormData.dim_table" class="input mono"
                       :placeholder="$t('dimensions.form.tablePlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('dimensions.form.dbName') }}</span>
                <input v-model="dimFormData.db_name" class="input mono"
                       :placeholder="$t('dimensions.form.dbNamePlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('dimensions.form.keys') }}</span>
                <input v-model="dimKeysText" class="input"
                       :placeholder="$t('dimensions.form.keysPlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('dimensions.form.grain') }}</span>
                <input v-model="dimFormData.grain" class="input"
                       :placeholder="$t('dimensions.form.grainPlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('dimensions.form.variantOf') }}</span>
                <input v-model="dimFormData.variant_of" class="input"
                       :placeholder="$t('dimensions.form.variantOfPlaceholder')" />
              </label>
            </div>
            <div class="modal-footer">
              <button class="btn btn-sm" @click="showDimFormModal = false">{{ $t('dimensions.wizard.close') }}</button>
              <button class="btn btn-primary btn-sm" :disabled="dimSaving" @click="saveDimension">
                {{ dimSaving ? $t('dimensions.form.saving') : $t('dimensions.form.save') }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Dimension delete confirm modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="dimDeleteTarget" class="modal-overlay" @click.self="dimDeleteTarget = null">
          <div class="modal-card modal-card-sm">
            <div class="modal-header">
              <h3>{{ $t('dimensions.delete') }}</h3>
            </div>
            <div class="form-body">
              {{ $t('dimensions.deleteConfirm', { name: dimDeleteTarget.dim_name }) }}
            </div>
            <div class="modal-footer">
              <button class="btn btn-sm" @click="dimDeleteTarget = null">{{ $t('dimensions.wizard.close') }}</button>
              <button class="btn btn-danger btn-sm" @click="doDimDelete">
                {{ $t('dimensions.delete') }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { useRouter } from 'vue-router'
import { api } from '../utils/api'

const { t } = useI18n()
const message = useMessage()
const router = useRouter()

// ── Cross-entry: jump to the asset catalog (数据资产地图) ─────
function goCatalog() {
  router.push('/catalog')
}

// ── Tab state ─────────────────────────────────────────────────
const activeTab = ref('metrics')

function switchTab(tab) {
  activeTab.value = tab
  if (tab === 'dimensions' && !dimensionsLoaded.value) loadDimensions()
}

// ── List state ────────────────────────────────────────────────
const search = ref('')
const filterSourceTable = ref('')
const filterType = ref('')
const page = ref(1)
const pageSize = 20
const total = ref(0)
const metrics = ref([])
const loading = ref(false)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function buildQuery() {
  const params = new URLSearchParams()
  if (search.value) params.set('search', search.value)
  if (filterSourceTable.value) params.set('source_table_like', filterSourceTable.value)
  if (filterType.value) params.set('metric_type', filterType.value)
  params.set('page', String(page.value))
  params.set('page_size', String(pageSize))
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

async function loadMetrics() {
  loading.value = true
  try {
    const data = await api.listMetrics(buildQuery())
    metrics.value = data.items || []
    total.value = data.total || 0
  } catch (e) {
    metrics.value = []
    total.value = 0
    message.error(t('metrics.msg.loadError') + ': ' + (e.message || ''))
  } finally {
    loading.value = false
  }
}

function onSearch() { page.value = 1; loadMetrics() }
function goPage(p) { page.value = p; loadMetrics() }

function formatDate(v) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  return d.toLocaleString()
}

// ── Wizard state ──────────────────────────────────────────────
const showWizard = ref(false)
const wizardStep = ref(1)
const wizardLoading = ref(false)
const wizardSources = ref([])
const wizardDatabases = ref([])
const wizardTables = ref([])
const wizardSelectedTables = ref([])
const wizardTableColumns = ref({})
const previewTable = ref('')
const previewColumns = ref([])
const wizardGenerating = ref(false)
const wizardResult = ref(null)

const stepLabels = computed(() => [
  t('metrics.wizard.stepSource'),
  t('metrics.wizard.stepDatabase'),
  t('metrics.wizard.stepTables'),
  t('metrics.wizard.stepGenerate'),
])

async function openWizard() {
  showWizard.value = true
  wizardStep.value = 1
  wizardResult.value = null
  wizardLoading.value = true
  try {
    const data = await api.getWizardSources()
    wizardSources.value = data.sources || []
  } catch (e) {
    message.error(t('metrics.msg.loadError') + ': ' + (e.message || ''))
  } finally {
    wizardLoading.value = false
  }
}

function closeWizard() {
  if (wizardGenerating.value) return
  showWizard.value = false
}

const wizardSourceId = ref('')
const wizardDbName = ref('')

async function wizardSelectSource(src) {
  wizardSourceId.value = src.id
  wizardLoading.value = true
  try {
    const data = await api.getWizardDatabases(src.id)
    wizardDatabases.value = data.databases || []
    wizardStep.value = 2
  } catch (e) {
    message.error(t('metrics.msg.loadError') + ': ' + (e.message || ''))
  } finally {
    wizardLoading.value = false
  }
}

async function wizardSelectDb(db) {
  wizardDbName.value = db.name
  wizardLoading.value = true
  wizardSelectedTables.value = []
  wizardTableColumns.value = {}
  previewTable.value = ''
  try {
    const data = await api.getWizardTables(wizardSourceId.value, db.name)
    wizardTables.value = data.tables || []
    wizardStep.value = 3
  } catch (e) {
    message.error(t('metrics.msg.loadError') + ': ' + (e.message || ''))
  } finally {
    wizardLoading.value = false
  }
}

async function wizardLoadColumns(tableName) {
  previewTable.value = tableName
  if (!wizardTableColumns.value[tableName]) {
    try {
      const data = await api.getWizardColumns(wizardSourceId.value, wizardDbName.value, tableName)
      wizardTableColumns.value[tableName] = data.columns || []
    } catch (e) {
      wizardTableColumns.value[tableName] = []
    }
  }
  previewColumns.value = wizardTableColumns.value[tableName] || []
}

function selectAllTables() {
  wizardSelectedTables.value = wizardTables.value.map(t => t.table_name)
  if (wizardTables.value.length) wizardLoadColumns(wizardTables.value[0].table_name)
}

async function wizardGenerate() {
  wizardStep.value = 4
  wizardGenerating.value = true
  wizardResult.value = null
  try {
    wizardResult.value = await api.generateMetrics({
      source_id: wizardSourceId.value,
      database: wizardDbName.value,
      tables: wizardSelectedTables.value,
    })
  } catch (e) {
    wizardGenerating.value = false
    wizardStep.value = 3
    message.error(t('metrics.msg.generateError') + ': ' + (e.message || ''))
  } finally {
    wizardGenerating.value = false
  }
}

function finishWizard() {
  showWizard.value = false
  loadMetrics()
}

// ── Create/Edit ───────────────────────────────────────────────
const showFormModal = ref(false)
const editingMetric = ref(null)
const saving = ref(false)
const formData = reactive({
  metric_name: '', metric_type: 'atomic', definition_sql: '',
  source_table: '', grain: '', parent_metrics: [], tags: [],
})
const formParentText = ref('')
const formTagsText = ref('')

function openCreate() {
  editingMetric.value = null
  Object.assign(formData, {
    metric_name: '', metric_type: 'atomic', definition_sql: '',
    source_table: '', grain: '', parent_metrics: [], tags: [],
  })
  formParentText.value = ''
  formTagsText.value = ''
  showFormModal.value = true
}

function openEdit(m) {
  editingMetric.value = m
  Object.assign(formData, {
    metric_name: m.metric_name || '',
    metric_type: m.metric_type || 'atomic',
    definition_sql: m.definition_sql || '',
    source_table: m.source_table || '',
    grain: m.grain || '',
    parent_metrics: m.parent_metrics || [],
    tags: m.tags || [],
  })
  formParentText.value = (formData.parent_metrics || []).join(', ')
  formTagsText.value = (formData.tags || []).join(', ')
  showFormModal.value = true
}

async function saveMetric() {
  saving.value = true
  const payload = {
    metric_name: formData.metric_name.trim(),
    metric_type: formData.metric_type,
    definition_sql: formData.definition_sql.trim(),
    source_table: formData.source_table.trim(),
    grain: formData.grain.trim(),
    parent_metrics: formParentText.value.split(',').map(s => s.trim()).filter(Boolean),
    tags: formTagsText.value.split(',').map(s => s.trim()).filter(Boolean),
  }
  try {
    if (editingMetric.value) {
      await api.updateMetric(editingMetric.value.metric_id, payload)
      message.success(t('metrics.msg.updated', { name: payload.metric_name }))
    } else {
      await api.createMetric(payload)
      message.success(t('metrics.msg.created', { name: payload.metric_name }))
    }
    showFormModal.value = false
    loadMetrics()
  } catch (e) {
    message.error(t('metrics.msg.saveError') + ': ' + (e.message || ''))
  } finally {
    saving.value = false
  }
}

// ── Delete ────────────────────────────────────────────────────
const deleteTarget = ref(null)

function confirmDelete(m) {
  deleteTarget.value = m
}

async function doDelete() {
  const m = deleteTarget.value
  try {
    await api.deleteMetric(m.metric_id)
    message.success(t('metrics.msg.deleted', { name: m.metric_name }))
    deleteTarget.value = null
    loadMetrics()
  } catch (e) {
    message.error(t('metrics.msg.deleteError') + ': ' + (e.message || ''))
  }
}

// ── Dimension list ────────────────────────────────────────────
const dimensions = ref([])
const dimTotal = ref(0)
const dimPage = ref(1)
const dimPageSize = 20
const dimLoading = ref(false)
const dimSearch = ref('')
const dimTableFilter = ref('')
const dimensionsLoaded = ref(false)

const dimTotalPages = computed(() => Math.max(1, Math.ceil(dimTotal.value / dimPageSize)))

function buildDimQuery() {
  const params = new URLSearchParams()
  if (dimSearch.value) params.set('search', dimSearch.value)
  if (dimTableFilter.value) params.set('dim_table_like', dimTableFilter.value)
  params.set('page', String(dimPage.value))
  params.set('page_size', String(dimPageSize))
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

async function loadDimensions() {
  dimLoading.value = true
  try {
    const data = await api.listDimensions(buildDimQuery())
    dimensions.value = data.items || []
    dimTotal.value = data.total || 0
    dimensionsLoaded.value = true
  } catch (e) {
    dimensions.value = []
    dimTotal.value = 0
    message.error(t('dimensions.msg.loadError') + ': ' + (e.message || ''))
  } finally {
    dimLoading.value = false
  }
}

function onDimSearch() { dimPage.value = 1; loadDimensions() }
function goDimPage(p) { dimPage.value = p; loadDimensions() }

function shortOwner(id) {
  if (!id) return '—'
  if (id === 'manual') return t('dimensions.ownerManual')
  return id.length > 8 ? `${id.slice(0, 8)}…` : id
}

// ── Dimension scan ────────────────────────────────────────────
const showScanModal = ref(false)
const scanning = ref(false)
const scanLimit = ref(500)
const scanResult = ref(null)

function openScanModal() {
  scanResult.value = null
  scanLimit.value = 500
  showScanModal.value = true
}

function closeScanModal() {
  if (scanning.value) return
  showScanModal.value = false
}

async function doScan() {
  scanning.value = true
  scanResult.value = null
  try {
    scanResult.value = await api.scanDimensions({ limit: scanLimit.value || null })
  } catch (e) {
    message.error(t('dimensions.msg.scanError') + ': ' + (e.message || ''))
  } finally {
    scanning.value = false
  }
}

// ── Dimension create/edit ─────────────────────────────────────
const showDimFormModal = ref(false)
const editingDimension = ref(null)
const dimSaving = ref(false)
const dimFormData = reactive({
  dim_name: '', dim_table: '', db_name: '', grain: '', variant_of: '',
})
const dimKeysText = ref('')

function openDimCreate() {
  editingDimension.value = null
  Object.assign(dimFormData, { dim_name: '', dim_table: '', db_name: '', grain: '', variant_of: '' })
  dimKeysText.value = ''
  showDimFormModal.value = true
}

function openDimEdit(d) {
  editingDimension.value = d
  Object.assign(dimFormData, {
    dim_name: d.dim_name || '',
    dim_table: d.dim_table || '',
    db_name: d.db_name || '',
    grain: d.grain || '',
    variant_of: d.variant_of || '',
  })
  dimKeysText.value = (d.natural_keys || []).join(', ')
  showDimFormModal.value = true
}

async function saveDimension() {
  dimSaving.value = true
  const payload = {
    dim_name: dimFormData.dim_name.trim(),
    dim_table: dimFormData.dim_table.trim(),
    db_name: dimFormData.db_name.trim(),
    grain: dimFormData.grain.trim(),
    variant_of: dimFormData.variant_of.trim() || null,
    natural_keys: dimKeysText.value.split(',').map(s => s.trim()).filter(Boolean),
  }
  try {
    if (editingDimension.value) {
      await api.updateDimension(editingDimension.value.dim_id, payload)
      message.success(t('dimensions.msg.updated', { name: payload.dim_name }))
    } else {
      await api.createDimension(payload)
      message.success(t('dimensions.msg.created', { name: payload.dim_name }))
    }
    showDimFormModal.value = false
    loadDimensions()
  } catch (e) {
    message.error(t('dimensions.msg.saveError') + ': ' + (e.message || ''))
  } finally {
    dimSaving.value = false
  }
}

// ── Dimension delete ──────────────────────────────────────────
const dimDeleteTarget = ref(null)

function confirmDimDelete(d) {
  dimDeleteTarget.value = d
}

async function doDimDelete() {
  const d = dimDeleteTarget.value
  try {
    await api.deleteDimension(d.dim_id)
    message.success(t('dimensions.msg.deleted', { name: d.dim_name }))
    dimDeleteTarget.value = null
    loadDimensions()
  } catch (e) {
    message.error(t('dimensions.msg.deleteError') + ': ' + (e.message || ''))
  }
}

onMounted(loadMetrics)
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
}

.page-subtitle {
  font-size: 0.8125rem;
  color: var(--color-text-tertiary);
  margin: 2px 0 0;
}

.flex-1 { flex: 1; }
.mono { font-family: var(--font-mono); font-size: 0.71875rem; }

.library-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: var(--space-lg);
  border-bottom: 1px solid var(--color-border);
}
.library-tab {
  padding: var(--space-xs) var(--space-md);
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--color-text-tertiary);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  margin-bottom: -1px;
  transition: color .15s, border-color .15s;
}
.library-tab:hover { color: var(--color-text-secondary); }
.library-tab.active {
  color: var(--color-accent);
  border-bottom-color: var(--color-accent);
}

.text-muted { color: var(--color-text-tertiary); }

.header-actions {
  display: flex;
  gap: var(--space-sm);
}

.toolbar {
  display: flex;
  gap: var(--space-sm);
  margin-bottom: var(--space-md);
}

.input-sm, .select-sm { font-size: 0.8125rem; }

.table-card { overflow: hidden; }
.table-wrapper { overflow-x: auto; }

.metric-table {
  width: 100%;
  font-size: 0.8125rem;
  border-collapse: collapse;
}

.metric-table th {
  text-align: left;
  padding: var(--space-xs) var(--space-sm);
  background: var(--color-bg);
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 1px solid var(--color-border);
  white-space: nowrap;
}

.metric-table td {
  padding: var(--space-xs) var(--space-sm);
  border-bottom: 1px solid var(--color-border);
  vertical-align: middle;
}

.metric-name { font-weight: 600; white-space: nowrap; }
.metric-def { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.metric-grain { max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.metric-created { white-space: nowrap; color: var(--color-text-tertiary); font-size: 0.71875rem; }

.metric-tags { max-width: 160px; }
.tag-chip {
  display: inline-block;
  padding: 1px 8px;
  margin: 1px 2px;
  border-radius: 999px;
  background: var(--color-bg-secondary, rgba(127,127,127,.12));
  font-size: 0.6875rem;
  color: var(--color-text-secondary);
}

.row-actions { white-space: nowrap; }
.btn-ghost, .btn-danger-ghost {
  background: transparent;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
}
.btn-danger-ghost { color: var(--color-danger, #e5484d); }
.btn-xs { padding: 2px 8px; font-size: 0.71875rem; }

.pagination {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
}
.page-info { font-size: 0.75rem; color: var(--color-text-tertiary); }

/* Wizard */
.wizard-steps {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-md) var(--space-lg);
  border-bottom: 1px solid var(--color-border);
}
.wizard-step {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
}
.wizard-step:not(:last-child)::after {
  content: '—';
  margin: 0 6px;
  color: var(--color-border);
}
.wizard-step-dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: 1px solid var(--color-border);
  font-size: 0.6875rem;
}
.wizard-step.active { color: var(--color-accent); }
.wizard-step.active .wizard-step-dot {
  background: var(--color-accent);
  color: #fff;
  border-color: var(--color-accent);
}
.wizard-step.done .wizard-step-dot { background: rgba(46,160,67,.15); }

.wizard-body { padding: var(--space-lg); min-height: 260px; }
.loading-mini { padding: var(--space-lg); text-align: center; color: var(--color-text-tertiary); }

.pick-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--space-sm);
  max-height: 320px;
  overflow-y: auto;
}
.pick-card {
  text-align: left;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg);
  cursor: pointer;
  transition: border-color .15s;
}
.pick-card:hover { border-color: var(--color-accent); }
.pick-title { display: block; font-weight: 600; font-size: 0.8125rem; }
.pick-sub { display: block; font-size: 0.6875rem; color: var(--color-text-tertiary); margin-top: 2px; }

.wizard-nav {
  display: flex;
  justify-content: space-between;
  margin-top: var(--space-lg);
}

.wizard-tables {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-md);
}
.tables-panel, .columns-panel {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
}
.tables-header, .columns-header {
  padding: var(--space-xs) var(--space-sm);
  background: var(--color-bg);
  border-bottom: 1px solid var(--color-border);
  font-size: 0.75rem;
  font-weight: 600;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.table-list, .column-list {
  max-height: 300px;
  overflow-y: auto;
}
.table-item {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 6px var(--space-sm);
  border-bottom: 1px solid var(--color-border);
  font-size: 0.75rem;
  cursor: pointer;
}
.table-item.checked { background: rgba(64,158,255,.06); }
.table-comment {
  color: var(--color-text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.column-item {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 5px var(--space-sm);
  border-bottom: 1px solid var(--color-border);
  font-size: 0.71875rem;
}
.column-name { min-width: 130px; }
.column-type { min-width: 90px; color: var(--color-text-tertiary); }
.column-comment { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.generating {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-md);
  padding: var(--space-xl) 0;
  color: var(--color-text-secondary);
}
.spinner {
  width: 28px; height: 28px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.result-summary { padding: var(--space-md) 0; }
.result-line { font-weight: 600; margin-bottom: var(--space-md); }
.result-list { max-height: 260px; overflow-y: auto; }
.result-item {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 5px var(--space-sm);
  border-bottom: 1px solid var(--color-border);
  font-size: 0.75rem;
}

/* Form */
.form-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  padding: var(--space-lg);
}
.field { display: flex; flex-direction: column; gap: 4px; font-size: 0.75rem; }
.field span { color: var(--color-text-secondary); }
.field .input, .field .select { width: 100%; }

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-sm);
  padding: var(--space-md) var(--space-lg);
  border-top: 1px solid var(--color-border);
}

.modal-card-wide { width: min(860px, 92vw); }
.modal-card-sm { width: min(420px, 92vw); }

.empty-state { text-align: center; padding: var(--space-xl); }
.empty-state-icon { font-size: 2rem; margin-bottom: var(--space-sm); }

/* ── Modal (Teleported to body — styles must live here, scoped to this
      component; TasksOverview's modal CSS does NOT apply across components) ── */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(4px);
}

.modal-card {
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-elevated);
  width: min(560px, 92vw);
  max-height: 88vh;
  overflow-y: auto;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-md) var(--space-lg);
  border-bottom: 1px solid var(--color-border);
}
.modal-header h3 {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
}
.modal-close {
  background: none;
  border: none;
  font-size: 1.25rem;
  line-height: 1;
  color: var(--color-text-tertiary);
  cursor: pointer;
  padding: 0 4px;
}
.modal-close:hover { color: var(--color-text); }

/* ── Modal Transitions ───────────────────────────────────── */
.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.2s ease;
}
.modal-enter-active .modal-card,
.modal-leave-active .modal-card {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
.modal-enter-from .modal-card {
  transform: scale(0.95);
  opacity: 0;
}
.modal-leave-to .modal-card {
  transform: scale(0.95);
  opacity: 0;
}
</style>
