<template>
  <div class="home">
    <!-- Aurora ambient background -->
    <div class="aurora" aria-hidden="true">
      <div class="aurora-blob aurora-blob-1"></div>
      <div class="aurora-blob aurora-blob-2"></div>
      <div class="aurora-blob aurora-blob-3"></div>
    </div>

    <!-- Hero — generous spacing, typographic hierarchy -->
    <section class="hero">
      <h1
        class="hero-title"
        v-motion
        :initial="{ opacity: 0, y: 24 }"
        :enter="{ opacity: 1, y: 0, transition: { duration: 600, ease: 'easeOut' } }"
      >{{ $t('home.title') }}</h1>
      <p
        class="hero-subtitle"
        v-motion
        :initial="{ opacity: 0, y: 16 }"
        :enter="{ opacity: 1, y: 0, transition: { duration: 600, delay: 120, ease: 'easeOut' } }"
      >{{ $t('home.subtitle') }}</p>

      <!-- Layer pipeline visual: ODS → DIM → DWD → DWS → ADS -->
      <div
        class="pipeline"
        v-motion
        :initial="{ opacity: 0, y: 12 }"
        :enter="{ opacity: 1, y: 0, transition: { duration: 600, delay: 260, ease: 'easeOut' } }"
      >
        <template v-for="(layer, i) in pipelineLayers" :key="layer">
          <span class="pipeline-chip" :style="{ animationDelay: i * 0.35 + 's' }">{{ layer }}</span>
          <span v-if="i < pipelineLayers.length - 1" class="pipeline-arrow">
            <span class="pipeline-arrow-line" :style="{ animationDelay: i * 0.35 + 's' }"></span>›
          </span>
        </template>
      </div>
    </section>

    <!-- Create Task Card -->
    <section
      class="card card-elevated create-card"
      v-motion
      :initial="{ opacity: 0, y: 20 }"
      :enter="{ opacity: 1, y: 0, transition: { duration: 550, delay: 360, ease: 'easeOut' } }"
    >
      <div class="create-card-header">
        <h2 class="mb-0">{{ $t('home.newTask') }}</h2>
        <span class="badge badge-info">Step 1</span>
      </div>

      <div class="form-stack">
        <!-- Modeling Goal -->
        <div class="form-group">
          <label class="form-label">{{ $t('home.goal') }}</label>
          <textarea
            v-model="goal"
            class="input goal-textarea"
            :placeholder="$t('home.goalPlaceholder')"
            rows="3"
            @input="goalCharCount = goal.length"
          ></textarea>
          <div class="form-hint">
            <span>{{ goalCharCount }} / 50000</span>
            <span v-if="goalCharCount > 45000" style="color:var(--color-warning);">Approaching limit</span>
          </div>
        </div>

        <!-- Direct task type selection -->
        <div class="form-group">
          <label class="form-label">任务类型</label>
          <div class="segmented">
            <button
              class="seg-btn" :class="{ active: sourceMode === 'ods_migration' }"
              @click="sourceMode = 'ods_migration'"
            >整库迁移</button>
            <button
              class="seg-btn" :class="{ active: sourceMode === 'core_model' }"
              @click="sourceMode = 'core_model'"
            >DIM/DWD 核心建模</button>
            <button
              class="seg-btn" :class="{ active: sourceMode === 'warehouse' }"
              @click="sourceMode = 'warehouse'"
            >数仓探索</button>
          </div>
          <p class="form-hint">
            {{ sourceMode === 'ods_migration' ? '从业务源库迁移全量 ODS 数据'
              : sourceMode === 'core_model' ? '基于 ODS 设计 DIM/DWD 核心资产'
              : '从已有 dim/dwd/dws 组合，仅产出新 DWS/ADS' }}
          </p>
        </div>

        <!-- Business-db mode pane -->
        <div v-if="sourceMode !== 'warehouse'" class="grid-2">
          <div v-if="sourceMode === 'ods_migration'" class="form-group">
            <label class="form-label">{{ $t('home.sourceDB') }}</label>
            <select v-model="selectedDB" class="select" style="width:100%;">
              <option value="">{{ $t('home.selectDB') }}</option>
              <option v-for="db in businessDBs" :key="db.id" :value="db.name">
                {{ db.name }} ({{ db.db_type }}, {{ db.table_count || '?' }} tables)
              </option>
            </select>
            <p class="form-hint">
              <a href="#" @click.prevent="showDBForm = !showDBForm" class="inline-link">
                <span class="link-chevron" :class="{ open: showDBForm }">&#9656;</span>
                {{ showDBForm ? $t('home.cancel') : '+ ' + $t('home.registerDB') }}
              </a>
            </p>
          </div>
          <div class="form-group">
            <label class="form-label">{{ $t('home.targetDW') }}</label>
            <select v-model="targetWarehouseSourceId" class="select" style="width:100%;">
              <option value="">请选择 Doris 数仓源</option>
              <option v-for="db in warehouseDBs" :key="db.id" :value="db.id">{{ db.name }}</option>
            </select>
          </div>
        </div>

        <div v-if="sourceMode !== 'warehouse'" class="form-group">
          <div class="grid-2">
            <div v-for="spec in landingFieldSpecs" :key="spec.layer">
              <label class="form-label">{{ spec.label }}</label>
              <div
                class="landing-combo"
                @focusout="onLandingFocusOut"
              >
                <input
                  class="input"
                  :value="landingValue(spec.layer)"
                  :placeholder="spec.placeholder"
                  autocomplete="off"
                  role="combobox"
                  :aria-expanded="openLandingLayer === spec.layer"
                  aria-autocomplete="list"
                  @mousedown="openLanding(spec.layer)"
                  @focus="openLanding(spec.layer)"
                  @input="onLandingType(spec.layer, $event.target.value)"
                  @keydown.escape.prevent="closeLanding"
                  @keydown.down.prevent="openLanding(spec.layer)"
                />
                <button
                  type="button"
                  class="landing-caret"
                  tabindex="-1"
                  :aria-label="'打开' + spec.label + '列表'"
                  :aria-expanded="openLandingLayer === spec.layer"
                  @mousedown.prevent="toggleLanding(spec.layer)"
                />
                <ul
                  v-if="openLandingLayer === spec.layer && landingSuggestions(spec.layer).length"
                  class="landing-menu"
                  role="listbox"
                  @mousedown.prevent
                >
                  <li
                    v-for="db in landingSuggestions(spec.layer)"
                    :key="db"
                    role="option"
                    :aria-selected="db === landingValue(spec.layer)"
                    :class="{ selected: db === landingValue(spec.layer) }"
                    @mousedown.prevent="pickLanding(spec.layer, db)"
                  >{{ db }}</li>
                </ul>
              </div>
            </div>
          </div>
          <p class="form-hint">可直接输入新库名并自动创建，也可从已有分层库中选择。</p>
        </div>

        <!-- Warehouse-mode pane -->
        <div v-if="sourceMode === 'warehouse'" class="form-stack">
          <div class="form-group">
            <div class="label-row">
              <label class="form-label">数仓源（db_type=doris，可多选）</label>
              <button
                class="btn btn-secondary btn-sm"
                :disabled="refreshingWH"
                @click="refreshWarehouse"
              >
                {{ refreshingWH ? '刷新中…' : '🔄 刷新数仓源' }}
              </button>
            </div>
            <div class="wh-source-list">
              <label v-for="db in warehouseDBs" :key="db.id" class="wh-source-item">
                <input type="checkbox" :value="db.name" v-model="selectedWarehouseDBs" />
                <span class="wh-source-name">{{ db.name }}</span>
                <span class="wh-source-meta">
                  {{ db.host }} · {{ db.table_count || '?' }} tables
                </span>
                <span v-if="liveLayerCounts[db.id]" class="wh-source-layers">
                  dim {{ liveLayerCounts[db.id].dim ?? 0 }} · dwd {{ liveLayerCounts[db.id].dwd ?? 0 }} · dws {{ liveLayerCounts[db.id].dws ?? 0 }}
                </span>
              </label>
            </div>
            <p class="form-hint" v-if="warehouseDBs.length === 0">
              无已注册数仓源 — 请先注册 db_type=doris 的数据源（connection_params.layers 六层映射）
            </p>
            <p class="form-hint" v-if="refreshMsg" :class="{ 'hint-error': refreshFailed }">
              {{ refreshMsg }}
            </p>
            <p class="form-hint">刷新会实时探测各层库表数量（人工新建 dim/dwd/dws 表后点此确认可见）</p>
          </div>

          <!-- Per-selected-source database-level selection (库级选择) -->
          <div class="form-group" v-for="db in selectedWarehouseSources" :key="db.id">
            <div class="label-row">
              <label class="form-label">库级选择 — {{ db.name }}（勾选要探索的库）</label>
              <span class="db-select-actions">
                <a href="#" class="inline-link" @click.prevent="checkAllDbs(db)">全选</a>
                <span class="dim">/</span>
                <a href="#" class="inline-link" @click.prevent="checkNoneDbs(db)">清空</a>
              </span>
            </div>
            <div class="wh-db-list">
              <label v-for="item in warehouseDbItems(db)" :key="item.name" class="wh-db-item">
                <input type="checkbox" :value="item.name" v-model="selectedWarehouseDbs[db.name]" />
                <span class="wh-db-name">{{ item.name }}</span>
                <span class="wh-db-layers">{{ item.layers.join('/') }}</span>
                <span class="wh-db-count">{{ item.count ?? '?' }} tables</span>
              </label>
            </div>
            <p class="form-hint">勾选的库将被探索并作为 DWS/ADS 锚定表；未勾选的库跳过（ods/ads 恒不可探索）</p>
          </div>

          <!-- Task-level landing selection (库级落库选择) -->
          <div class="form-group" v-if="selectedWarehouseSources.length">
            <div class="grid-2">
              <div class="form-group">
                <label class="form-label">DWS 落库</label>
                <select v-model="landingDws" class="select" style="width:100%;">
                  <option v-for="db in landingDbs('dws')" :key="db" :value="db">{{ db }}</option>
                </select>
              </div>
              <div class="form-group">
                <label class="form-label">ADS 落库</label>
                <select v-model="landingAds" class="select" style="width:100%;">
                  <option v-for="db in landingDbs('ads')" :key="db" :value="db">{{ db }}</option>
                </select>
              </div>
            </div>
            <p class="form-hint">新生成的 DWS/ADS 表将落在所选库（默认第一个选中源的分层库）</p>
          </div>
        </div>

        <!-- Source DB Registration (expandable) -->
        <Transition name="list">
          <div v-if="showDBForm" class="db-register-panel">
            <h4 class="mb-md">🔌 {{ $t('home.registerDB') }}</h4>
            <div class="grid-2">
              <input class="input" v-model="newDB.name" :placeholder="$t('home.name')" />
              <select class="select" v-model="newDB.db_type">
                <option>mysql</option><option>postgresql</option><option>oracle</option>
                <option>dm</option><option>kingbase</option><option>mongodb</option>
              </select>
              <input class="input" v-model="newDB.host" :placeholder="$t('home.host')" />
              <input class="input" v-model.number="newDB.port" :placeholder="$t('home.port')" type="number" />
              <input class="input" v-model="newDB.user" :placeholder="$t('home.user')" />
              <input class="input" v-model="newDB.password" :placeholder="$t('home.password')" type="password" />
              <input class="input" v-model="newDB.database" :placeholder="$t('home.database')" />
              <input class="input" v-model="newDB.description" :placeholder="$t('home.description')" />
            </div>
            <label class="auto-toggle mt-md" :title="$t('home.useSslHint')">
              <input type="checkbox" v-model="newDB.use_ssl" />
              <span class="auto-toggle-label">{{ $t('home.useSsl') }}</span>
            </label>
            <div class="flex items-center gap-sm mt-md">
              <button class="btn btn-secondary btn-sm" @click="registerDB" :disabled="!newDB.name || !newDB.host || !newDB.password || registering">
                <span v-if="registering" class="btn-spinner"></span>
                {{ registering ? $t('home.testing') : $t('home.registerDB') }}
              </button>
              <Transition name="fade">
                <span v-if="dbRegistered" class="badge badge-success">✓ {{ $t('home.registered') }}</span>
              </Transition>
            </div>
            <Transition name="fade">
              <div v-if="registerError" class="register-error mt-sm">
                <span class="register-error-icon">⚠</span>
                <span>{{ registerError }}</span>
              </div>
            </Transition>
          </div>
        </Transition>

        <!-- Submit -->
        <div class="create-actions">
          <label class="auto-toggle" :title="$t('home.autoModeHint')">
            <input type="checkbox" v-model="autoMode" />
            <span class="auto-toggle-label">{{ $t('home.autoMode') }}</span>
          </label>
          <Transition name="fade">
            <span v-if="createError" class="badge badge-error">{{ createError }}</span>
          </Transition>
          <button
            class="btn btn-primary btn-lg"
            :class="{ 'btn-loading': creating }"
            @click="createTask"
            :disabled="!goal || creating"
          >
            {{ creating ? $t('home.creating') : $t('home.createTask') }}
          </button>
        </div>
        <p v-if="autoMode" class="auto-mode-hint">⚡ {{ $t('home.autoModeHint') }}</p>
      </div>
    </section>

    <!-- Recent Tasks -->
    <Transition name="fade">
      <section v-if="recentTasks.length" class="card recent-section">
        <div class="recent-header">
          <h3 class="mb-0">{{ $t('home.recentTasks') }}</h3>
          <router-link to="/tasks" class="btn btn-secondary btn-sm">
            {{ $t('home.viewAll') }}
            <span style="font-size:0.75rem;">→</span>
          </router-link>
        </div>

        <div class="recent-list">
          <router-link
            v-for="(task, i) in recentTasks"
            :key="task.task_id"
            :to="`/task/${task.task_id}`"
            class="recent-task-item"
            :style="{ animationDelay: i * 0.05 + 's' }"
          >
            <span class="badge" :class="statusBadge(task.status)">{{ task.status }}</span>
            <span class="recent-task-goal">{{ task.task_id?.slice(0,8) }} — {{ task.modeling_goal?.slice(0,72) }}</span>
            <span class="recent-task-time">{{ timeAgo(task.created_at) }}</span>
          </router-link>
        </div>
      </section>
    </Transition>

    <!-- Empty state if no tasks -->
    <div v-if="!recentTasks.length && !creating" class="empty-state">
      <div class="empty-state-icon">📊</div>
      <div class="empty-state-title">Your first data model awaits</div>
      <div class="empty-state-desc">Describe your modeling goal above and let the AI agent design your warehouse.</div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../utils/api.js'

const router = useRouter()
const pipelineLayers = ['ODS', 'DIM', 'DWD', 'DWS', 'ADS']
const goal = ref('')
const goalCharCount = ref(0)
const selectedDB = ref('')
const autoMode = ref(false)
const creating = ref(false)
const createError = ref('')
const showDBForm = ref(false)
const dbRegistered = ref(false)
const registering = ref(false)
const registerError = ref('')
const sourceDBs = ref([])
const recentTasks = ref([])

// ── Warehouse mode (数仓探索) ─────────────────────────────────
const sourceMode = ref('ods_migration')
const selectedWarehouseDBs = ref([])   // multiple warehouse sources allowed
const selectedWarehouseDbs = ref({})   // source name -> [checked database names] (库级选择)
const refreshingWH = ref(false)
const refreshMsg = ref('')
const refreshFailed = ref(false)
const liveLayerCounts = ref({})   // db.id -> {layer_db: table_count} from last refresh
const targetWarehouseSourceId = ref('')
const landingOds = ref('')
const landingDim = ref('')
const landingDwd = ref('')

const businessDBs = computed(() => sourceDBs.value.filter(d => d.db_type !== 'doris'))
const warehouseDBs = computed(() => sourceDBs.value.filter(d => d.db_type === 'doris'))
const targetWarehouseSource = computed(() =>
  warehouseDBs.value.find(db => String(db.id) === String(targetWarehouseSourceId.value))
)

function targetLayerDatabases(layer) {
  const value = dbLayers(targetWarehouseSource.value)[layer]
  return [...new Set((Array.isArray(value) ? value : [value]).filter(Boolean))]
}

const openLandingLayer = ref('')
const landingFilter = ref('')
const landingFieldSpecs = computed(() => {
  const fields = [{ layer: 'ods', label: 'ODS 落库', placeholder: '输入或选择 ODS 库' }]
  if (sourceMode.value === 'core_model') {
    fields.push(
      { layer: 'dim', label: 'DIM 落库', placeholder: '输入或选择 DIM 库' },
      { layer: 'dwd', label: 'DWD 落库', placeholder: '输入或选择 DWD 库' },
    )
  }
  return fields
})

function landingValue(layer) {
  return { ods: landingOds.value, dim: landingDim.value, dwd: landingDwd.value }[layer] || ''
}

function setLanding(layer, value) {
  if (layer === 'ods') landingOds.value = value
  else if (layer === 'dim') landingDim.value = value
  else if (layer === 'dwd') landingDwd.value = value
}

function closeLanding() {
  openLandingLayer.value = ''
  landingFilter.value = ''
}

function openLanding(layer) {
  openLandingLayer.value = layer
  // Opening must show the full layer list. Filtering is only applied while
  // the user is typing a partial name — not when the field already holds a
  // selected database.
  landingFilter.value = ''
}

function toggleLanding(layer) {
  if (openLandingLayer.value === layer) closeLanding()
  else openLanding(layer)
}

function onLandingType(layer, value) {
  setLanding(layer, value)
  landingFilter.value = value
  openLandingLayer.value = layer
}

function pickLanding(layer, db) {
  setLanding(layer, db)
  closeLanding()
}

function onLandingFocusOut(event) {
  if (event.currentTarget.contains(event.relatedTarget)) return
  closeLanding()
}

function onLandingPointerDown(event) {
  if (!event.target.closest('.landing-combo')) closeLanding()
}

function landingSuggestions(layer) {
  const all = targetLayerDatabases(layer)
  const query = landingFilter.value.trim().toLowerCase()
  if (!query) return all
  return all.filter(db => db.toLowerCase().includes(query))
}

const selectedWarehouseSources = computed(() =>
  warehouseDBs.value.filter(d => selectedWarehouseDBs.value.includes(d.name))
)

function dbLayers(db) {
  const layers = (db?.connection_params || {}).layers
  return (layers && typeof layers === 'object') ? layers : {}
}

function warehouseDbItems(db) {
  // Unique databases across all layers, with layer badges and refreshed counts
  const byName = {}
  for (const [layer, v] of Object.entries(dbLayers(db))) {
    const dbs = Array.isArray(v) ? v : (v ? [v] : [])
    for (const d of dbs) {
      if (!d) continue
      if (!byName[d]) byName[d] = { name: d, layers: [] }
      byName[d].layers.push(layer)
    }
  }
  const counts = liveLayerCounts.value[db.id] || {}
  return Object.values(byName).map(item => ({ ...item, count: counts[item.name] }))
}

function checkAllDbs(db) {
  selectedWarehouseDbs.value[db.name] = warehouseDbItems(db).map(i => i.name)
}

function checkNoneDbs(db) {
  selectedWarehouseDbs.value[db.name] = []
}

// When a source is first checked, default to selecting all its databases
watch(selectedWarehouseDBs, (names) => {
  for (const name of names) {
    if (selectedWarehouseDbs.value[name]) continue
    const db = sourceDBs.value.find(d => d.name === name)
    if (db) selectedWarehouseDbs.value[name] = warehouseDbItems(db).map(i => i.name)
  }
})

// Task-level landing selection (库级落库选择)
const landingDws = ref('')
const landingAds = ref('')

function landingDbs(layer) {
  // union of the layer's databases across selected sources (deduped)
  const out = []
  for (const db of selectedWarehouseSources.value) {
    const v = dbLayers(db)[layer]
    const dbs = Array.isArray(v) ? v : (v ? [v] : [])
    for (const d of dbs) {
      if (d && !out.includes(d)) out.push(d)
    }
  }
  return out
}

// Default landing to the first selected source's dws/ads database
watch(selectedWarehouseSources, (srcs) => {
  if (!srcs.length) return
  if (!landingDws.value) {
    const v = dbLayers(srcs[0]).dws
    landingDws.value = Array.isArray(v) ? (v[0] || '') : (v || '')
  }
  if (!landingAds.value) {
    const v = dbLayers(srcs[0]).ads
    landingAds.value = Array.isArray(v) ? (v[0] || '') : (v || '')
  }
})

async function refreshWarehouse() {
  refreshingWH.value = true
  refreshMsg.value = ''
  refreshFailed.value = false
  try {
    const result = await api.refreshWarehouseSources()
    const counts = {}
    for (const r of result.refreshed || []) counts[r.id] = r.layer_totals || {}
    liveLayerCounts.value = counts
    const failedNames = (result.failed || []).map(f => f.name).join('、')
    const discovered = (result.discovered || []).map(d => `${d.db}→${d.layer}`).join('、')
    const unclassified = (result.unclassified || []).join('、')
    const removed = (result.removed || []).filter(Boolean).join('、')
    refreshMsg.value = `已刷新 ${(result.refreshed || []).length}/${result.total || 0} 个数仓源`
      + (discovered ? `，新发现并加入映射: ${discovered}` : '')
      + (removed ? `，移除失效库: ${removed}` : '')
      + (unclassified ? `，无法自动分层(需手动): ${unclassified}` : '')
      + (failedNames ? `，失败: ${failedNames}` : '')
    if (failedNames || unclassified) refreshFailed.value = true
    await loadSourceDBs()
  } catch (e) {
    refreshFailed.value = true
    refreshMsg.value = (e.message || '').replace(/^HTTP \d+:\s*/, '') || '刷新失败'
  } finally {
    refreshingWH.value = false
  }
}

const newDB = ref({
  name: '', db_type: 'mysql', host: '', port: 3306,
  user: 'root', password: '', database: '', description: '',
  use_ssl: false,  // MySQL 默认不走 SSL
})

onMounted(async () => {
  document.addEventListener('pointerdown', onLandingPointerDown)
  await Promise.all([
    loadSourceDBs(),
    loadRecentTasks(),
  ])
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', onLandingPointerDown)
})

async function loadSourceDBs() {
  try { sourceDBs.value = (await api.listSourceDBs()).items || [] } catch {}
}

async function loadRecentTasks() {
  try {
    const resp = await api.fetch('/api/v1/tasks?limit=5')
    if (resp.ok) {
      const data = await resp.json()
      recentTasks.value = data.tasks || data.items || []
    }
  } catch {}
}

async function registerDB() {
  registering.value = true
  registerError.value = ''
  try {
    const pw = newDB.value.password
    if (!pw) {
      registerError.value = 'Password is required to test connectivity.'
      return
    }
    // The API tests connectivity first — if it fails, nothing is saved.
    const result = await api.registerSourceDB({
      name: newDB.value.name,
      db_type: newDB.value.db_type,
      host: newDB.value.host,
      port: newDB.value.port,
      database_name: newDB.value.database,
      user_name: newDB.value.user,
      password: pw,
      description: newDB.value.description || newDB.value.name,
      tags: [],
      connection_params: { use_ssl: newDB.value.use_ssl },
    })
    const tc = result.connection_test
    dbRegistered.value = true
    await loadSourceDBs()
    setTimeout(() => {
      showDBForm.value = false
      dbRegistered.value = false
    }, 1800)
  } catch (e) {
    // Strip HTTP prefix for cleaner display — the API returns
    // "Connection test failed: <reason>" on connectivity errors.
    let msg = e.message || 'Unknown error'
    msg = msg.replace(/^HTTP \d+:\s*/, '')
    registerError.value = msg
  } finally {
    registering.value = false
  }
}

async function createTask() {
  creating.value = true
  createError.value = ''
  try {
    const dbs = []
    let source_mode = 'business_db'
    if (sourceMode.value !== 'warehouse') {
      if (sourceMode.value === 'ods_migration' && !selectedDB.value) {
        throw new Error('请选择源数据库')
      }
      if (!targetWarehouseSourceId.value) throw new Error('请选择目标 Doris 数仓源')
      if (!landingOds.value) throw new Error('请填写 ODS 落库库名')
      if (sourceMode.value === 'core_model' && (!landingDim.value || !landingDwd.value)) {
        throw new Error('核心建模请填写 DIM、DWD 落库库名')
      }
      const selectedName = sourceMode.value === 'core_model'
        ? targetWarehouseSource.value?.name : selectedDB.value
      if (selectedName) {
        const db = sourceDBs.value.find(d => d.name === selectedName)
        if (db) {
          dbs.push({
            id: db.id, name: db.name, db_type: db.db_type, host: db.host,
            port: db.port, user: '', password: '', database: db.database_name,
            mcp_server_name: `mcp_${db.name}`,
            ...(sourceMode.value === 'core_model'
              ? { connection_params: { ...(db.connection_params || {}), explore_databases: [landingOds.value] }, database: landingOds.value }
              : {})
          })
        }
      }
    } else {
      source_mode = 'warehouse'
      if (!selectedWarehouseDBs.value.length) throw new Error('请至少选择一个数仓源')
      // 多源支持: 每个选中的数仓源各自携带完整 layers 映射;
      // 库级选择 explore_databases 只探索勾选的库 (空列表 = 一个都不探索)
      for (const name of selectedWarehouseDBs.value) {
        const db = sourceDBs.value.find(d => d.name === name)
        if (!db) continue
        const layers = { ...dbLayers(db) }
        const exploreDbs = selectedWarehouseDbs.value[name] || []
        if (!exploreDbs.length) throw new Error(`请为数仓源 ${name} 至少勾选一个库`)
        dbs.push({
          id: db.id, name: db.name, db_type: db.db_type, host: db.host,
          port: db.port, user: '', password: '', database: db.database_name,
          mcp_server_name: `mcp_${db.name}`,
          connection_params: { ...(db.connection_params || {}), layers, explore_databases: exploreDbs },
        })
      }
    }
    const result = await api.createTask({
      modeling_goal: goal.value,
      source_databases: dbs,
      dw_target: { type: 'doris', database: 'wren_dw' },
      mode: autoMode.value ? 'auto' : 'manual',
      source_mode,
      business_task_type: source_mode === 'business_db' ? sourceMode.value : 'full',
      warehouse_source_id: source_mode === 'business_db' ? targetWarehouseSourceId.value : undefined,
      create_missing_databases: source_mode === 'business_db',
      landing: source_mode === 'warehouse'
        ? { dws: landingDws.value, ads: landingAds.value }
        : sourceMode.value === 'ods_migration'
          ? { ods: landingOds.value }
          : { ods: landingOds.value, dim: landingDim.value, dwd: landingDwd.value },
    })
    router.push(`/task/${result.task_id}`)
  } catch (e) {
    createError.value = e.message
  } finally {
    creating.value = false
  }
}

function statusBadge(s) {
  const map = {
    completed: 'badge-success', failed: 'badge-error',
    executing: 'badge-warning', running: 'badge-warning',
    exploring: 'badge-info', designing: 'badge-info',
    pending: 'badge-neutral',
  }
  return map[s] || 'badge-neutral'
}

function timeAgo(d) {
  if (!d) return ''
  const sec = (Date.now() - new Date(d).getTime()) / 1000
  if (sec < 60) return 'just now'
  if (sec < 3600) return `${Math.floor(sec/60)}m ago`
  if (sec < 86400) return `${Math.floor(sec/3600)}h ago`
  return `${Math.floor(sec/86400)}d ago`
}
</script>

<style scoped>
/* ── Warehouse mode (数仓探索) ─────────────────────────────── */
.segmented {
  display: inline-flex;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
  overflow: hidden;
  background: var(--bg-soft, #fff);
}
.seg-btn {
  border: none;
  background: transparent;
  padding: 8px 22px;
  font-size: 14px;
  cursor: pointer;
  color: var(--color-muted, #909399);
}
.seg-btn.active {
  background: var(--color-primary, #3b7cff);
  color: #fff;
}
.layer-matrix {
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 6px;
  overflow: hidden;
  margin-top: 6px;
}
.layer-row {
  display: grid;
  grid-template-columns: 90px 1fr 60px;
  gap: 8px;
  padding: 7px 12px;
  border-bottom: 1px solid var(--border-soft, #f0f2f5);
  align-items: center;
  font-size: 13px;
}
.layer-row:last-child { border-bottom: none; }
.layer-head {
  background: var(--bg-soft, #f8f9fb);
  color: var(--color-muted, #909399);
  font-weight: 600;
}
.layer-key { font-weight: 600; }
.layer-note { color: var(--color-muted, #909399); font-size: 12px; }
.dim { color: var(--color-muted, #b0b3b8); }

/* ── Warehouse sources multi-select ────────────────────────── */
.label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-md, 12px);
  margin-bottom: 6px;
}
.label-row .form-label { margin-bottom: 0; }
.wh-source-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--bg-soft, #fafbfc);
}
.wh-source-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  cursor: pointer;
}
.wh-source-name { font-weight: 600; }
.wh-source-meta { color: var(--color-muted, #909399); }
.wh-source-layers {
  color: var(--color-muted, #909399);
  font-size: 12px;
  background: var(--bg-elevated, #fff);
  border-radius: 6px;
  padding: 2px 8px;
}
.hint-error { color: var(--color-danger, #e5484d); }
.db-select-actions { font-size: 12px; }
.wh-db-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--bg-soft, #fafbfc);
  max-height: 260px;
  overflow-y: auto;
}
.wh-db-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  cursor: pointer;
}
.wh-db-name { font-weight: 600; font-family: var(--font-mono, ui-monospace, monospace); }
.wh-db-layers {
  font-size: 11px;
  color: var(--color-muted, #909399);
  background: var(--bg-elevated, #fff);
  border-radius: 6px;
  padding: 1px 6px;
  font-family: var(--font-mono, ui-monospace, monospace);
}
.wh-db-count { color: var(--color-muted, #909399); font-size: 12px; margin-left: auto; }

/* ── Aurora ambient background ─────────────────────────── */
.home { position: relative; }

.aurora {
  position: fixed;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  z-index: 0;
}

.aurora-blob {
  position: absolute;
  border-radius: 50%;
  filter: blur(90px);
  opacity: 0.35;
  will-change: transform;
}

.aurora-blob-1 {
  width: 46vw; height: 46vw;
  top: -18vw; right: -10vw;
  background: radial-gradient(circle, rgba(0,122,255,0.55), transparent 65%);
  animation: aurora-drift-1 22s ease-in-out infinite;
}

.aurora-blob-2 {
  width: 38vw; height: 38vw;
  bottom: -14vw; left: -8vw;
  background: radial-gradient(circle, rgba(175,82,222,0.42), transparent 65%);
  animation: aurora-drift-2 28s ease-in-out infinite;
}

.aurora-blob-3 {
  width: 30vw; height: 30vw;
  top: 32%; left: 46%;
  background: radial-gradient(circle, rgba(90,200,250,0.36), transparent 65%);
  animation: aurora-drift-3 25s ease-in-out infinite;
}

[data-theme="dark"] .aurora-blob { opacity: 0.5; }

@keyframes aurora-drift-1 {
  0%, 100% { transform: translate(0, 0) scale(1); }
  50% { transform: translate(-6vw, 5vh) scale(1.15); }
}
@keyframes aurora-drift-2 {
  0%, 100% { transform: translate(0, 0) scale(1); }
  50% { transform: translate(7vw, -6vh) scale(1.1); }
}
@keyframes aurora-drift-3 {
  0%, 100% { transform: translate(0, 0) scale(1); }
  50% { transform: translate(-5vw, -4vh) scale(0.9); }
}

/* Keep content above the aurora */
.home > *:not(.aurora) { position: relative; z-index: 1; }

/* ── Hero ────────────────────────────────────────────────── */
.hero {
  text-align: center;
  margin-bottom: var(--space-2xl);
  padding: var(--space-xl) 0 var(--space-lg);
}

.hero-title {
  margin-bottom: var(--space-sm);
  background: linear-gradient(120deg, var(--color-text) 30%, var(--color-accent) 50%, var(--color-text) 70%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  animation: hero-sheen 6s ease-in-out infinite;
}

@keyframes hero-sheen {
  0%, 100% { background-position: 100% 0; }
  50% { background-position: 0% 0; }
}

.hero-subtitle {
  font-size: 1.125rem;
  max-width: 520px;
  margin: 0 auto;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

/* ── Layer pipeline visual ─────────────────────────────── */
.pipeline {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: var(--space-lg);
  flex-wrap: wrap;
}

.pipeline-chip {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--color-accent);
  background: var(--color-accent-glass);
  border: 1px solid var(--color-border-focus);
  border-radius: var(--radius-full);
  padding: 4px 14px;
  animation: pipeline-chip-glow 3s ease-in-out infinite;
}

@keyframes pipeline-chip-glow {
  0%, 100% { box-shadow: 0 0 0 0 rgba(0,122,255,0); }
  50% { box-shadow: 0 0 12px 2px rgba(0,122,255,0.25); }
}

.pipeline-arrow {
  display: inline-flex;
  align-items: center;
  color: var(--color-text-tertiary);
  font-size: 0.875rem;
}

.pipeline-arrow-line {
  display: inline-block;
  width: 18px;
  height: 2px;
  border-radius: 1px;
  background: linear-gradient(90deg, var(--color-accent), transparent);
  background-size: 200% 100%;
  animation: pipeline-flow 1.6s linear infinite;
}

@keyframes pipeline-flow {
  from { background-position: 100% 0; }
  to { background-position: -100% 0; }
}

/* ── Create Card ─────────────────────────────────────────── */
.create-card {
  margin-bottom: var(--space-xl);
  overflow: visible;
}

.create-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-lg);
}

.form-stack {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
}

.form-group {
  display: flex;
  flex-direction: column;
}

.form-label {
  display: block;
  font-size: 0.8125rem;
  font-weight: 600;
  margin-bottom: var(--space-xs);
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.02em;
}

.form-hint {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  margin-top: 4px;
  display: flex;
  justify-content: space-between;
}

.landing-combo {
  position: relative;
}
.landing-combo .input {
  padding-right: 2.25rem;
}
.landing-caret {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 2.25rem;
  border: 0;
  background: transparent;
  cursor: pointer;
  color: var(--color-text-tertiary, #8e8e93);
}
.landing-caret::before {
  content: '';
  display: block;
  margin: 0 auto;
  width: 0;
  height: 0;
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
  border-top: 5px solid currentColor;
}
.landing-menu {
  position: absolute;
  z-index: 40;
  left: 0;
  right: 0;
  top: calc(100% + 4px);
  max-height: 220px;
  overflow-y: auto;
  margin: 0;
  padding: 4px 0;
  list-style: none;
  background: var(--color-bg-elevated, #fff);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm, 8px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
}
.landing-menu li {
  padding: 8px 12px;
  cursor: pointer;
  font-size: 0.8125rem;
  font-family: var(--font-mono, ui-monospace, monospace);
}
.landing-menu li:hover,
.landing-menu li.selected {
  background: var(--color-accent-glass, #e8f1ff);
}

.goal-textarea {
  resize: vertical;
  min-height: 90px;
  line-height: 1.6;
}

.inline-link {
  color: var(--color-accent);
  text-decoration: none;
  font-size: 0.8125rem;
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: color var(--duration-fast) var(--spring-default);
}
.inline-link:hover {
  color: var(--color-accent-hover);
}

.link-chevron {
  display: inline-block;
  font-size: 0.625rem;
  transition: transform var(--duration-fast) var(--spring-default);
}
.link-chevron.open {
  transform: rotate(90deg);
}

/* ── DB Register Panel ───────────────────────────────────── */
.db-register-panel {
  background: var(--color-bg);
  border-radius: var(--radius-md);
  padding: var(--space-lg);
  border: 1px solid var(--color-border);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
  display: inline-block;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ── Register Error ───────────────────────────────────────── */
.register-error {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 14px;
  background: var(--color-error-bg, #fef2f2);
  border: 1px solid var(--color-error, #dc2626);
  border-radius: var(--radius-sm);
  color: var(--color-error, #dc2626);
  font-size: 0.8125rem;
  line-height: 1.5;
  word-break: break-word;
}

.register-error-icon {
  flex-shrink: 0;
  font-size: 1rem;
  line-height: 1.4;
}

/* ── Submit Actions ──────────────────────────────────────── */
.create-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-md);
  padding-top: var(--space-sm);
}

.auto-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-right: auto;
  cursor: pointer;
  user-select: none;
}

.auto-toggle input {
  accent-color: var(--color-accent);
  cursor: pointer;
}

.auto-toggle-label {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--color-text-secondary);
}

.auto-mode-hint {
  font-size: 0.75rem;
  color: var(--color-warning);
  text-align: left;
  margin-top: 4px;
}

/* ── Recent Tasks ────────────────────────────────────────── */
.recent-section {
  animation: task-row-enter 0.5s var(--spring-default) 0.2s both;
}

.recent-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--space-sm);
}

.recent-list {
  display: flex;
  flex-direction: column;
}

.recent-task-item {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: 10px var(--space-sm);
  text-decoration: none;
  color: var(--color-text);
  border-radius: var(--radius-sm);
  transition:
    background var(--duration-fast) var(--spring-default),
    transform var(--duration-fast) var(--spring-default);
  animation: task-row-enter 0.35s var(--spring-default) both;
}

@keyframes task-row-enter {
  from {
    opacity: 0;
    transform: translateX(-8px);
  }
}

.recent-task-item:hover {
  background: var(--color-bg);
}
.recent-task-item:active {
  transform: scale(0.99);
  transition: transform var(--duration-instant) ease-out;
}

.recent-task-goal {
  flex: 1;
  font-size: 0.875rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text);
}

.recent-task-time {
  color: var(--color-text-tertiary);
  font-size: 0.75rem;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
</style>
