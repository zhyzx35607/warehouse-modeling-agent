/** REST API client for Wren Agent. */

const BASE = '/api/v1'

function requestHeaders(headers = {}) {
  return {
    'Content-Type': 'application/json',
    ...headers,
  }
}

function rawFetch(url, options = {}) {
  return fetch(url, {
    ...options,
    headers: requestHeaders(options.headers),
  })
}

async function request(url, options = {}) {
  const res = await rawFetch(`${BASE}${url}`, options)
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`HTTP ${res.status}: ${body.slice(0, 200)}`)
  }
  return res.json()
}

export const api = {
  // Raw fetch for custom paths
  fetch: rawFetch,

  // Tasks
  listTasks: (status = '') => request(`/tasks${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  createTask: (data) => request('/tasks', { method: 'POST', body: JSON.stringify(data) }),
  getTask: (id)       => request(`/tasks/${id}`),
  getTaskFullState: (id) => request(`/tasks/${id}/full-state`),
  getTaskStages: (id) => request(`/tasks/${id}/stages`),
  getTaskLineage: (id) => request(`/tasks/${id}/lineage`),
  retryODSTable: (id, tableIdentity) =>
    request(`/tasks/${id}/stages/ods/retry?table_identity=${encodeURIComponent(tableIdentity)}`, { method: 'POST' }),
  cancelTask: (id)    => request(`/tasks/${id}/cancel`, { method: 'POST' }),
  recoverTask: (id)   => request(`/tasks/${id}/recover`, { method: 'POST' }),
  cleanupTasks: (taskId = '') =>
    request(`/tasks/cleanup${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''}`, { method: 'DELETE' }),

  // Clarifications
  getClarifications: (id) => request(`/tasks/${id}/clarifications`),
  answerClarification: (id, data) =>
    request(`/tasks/${id}/clarifications`, { method: 'POST', body: JSON.stringify(data) }),

  // Approvals
  getApprovals: (id) => request(`/tasks/${id}/approvals`),
  respondApproval: (id, data) =>
    request(`/tasks/${id}/approvals`, { method: 'POST', body: JSON.stringify(data) }),
  bulkApprove: (id, data = {}) =>
    request(`/tasks/${id}/approvals/bulk`, { method: 'POST', body: JSON.stringify(data) }),
  getApprovalNarratives: (taskId, approvalId) =>
    request(`/tasks/${taskId}/approvals/${approvalId}/narratives`),

  // Design & Report
  getDesignPlan: (id) => request(`/tasks/${id}/design-plan`),
  getReport: (id) => request(`/tasks/${id}/report`),
  getLogs: (id, params = '') => request(`/tasks/${id}/logs${params}`),
  getLLMTraces: (id, params = '') => request(`/tasks/${id}/llm-traces${params}`),

  // Source DBs
  listSourceDBs: (params = '') => request(`/source-dbs${params}`),
  getSourceDB: (id) => request(`/source-dbs/${id}`),
  registerSourceDB: (data) => request('/source-dbs', { method: 'POST', body: JSON.stringify(data) }),
  testSourceDB: (id, body) => request(`/source-dbs/${id}/test`, { method: 'POST', body: JSON.stringify(body) }),
  deleteSourceDB: (id) => request(`/source-dbs/${id}/delete`, { method: 'POST' }),
  refreshWarehouseSources: () => request('/source-dbs/refresh-warehouse', { method: 'POST' }),

  // Metrics (Metric Library)
  listMetrics: (params = '') => request(`/metrics${params}`),
  createMetric: (data) => request('/metrics', { method: 'POST', body: JSON.stringify(data) }),
  updateMetric: (id, data) => request(`/metrics/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteMetric: (id) => request(`/metrics/${id}`, { method: 'DELETE' }),

  // Metric generation wizard
  getWizardSources: () => request('/metrics/wizard/sources'),
  getWizardDatabases: (sourceId) => request(`/metrics/wizard/databases?source_id=${encodeURIComponent(sourceId)}`),
  getWizardTables: (sourceId, database) => request(`/metrics/wizard/tables?source_id=${encodeURIComponent(sourceId)}&database=${encodeURIComponent(database)}`),
  getWizardColumns: (sourceId, database, table) => request(`/metrics/wizard/columns?source_id=${encodeURIComponent(sourceId)}&database=${encodeURIComponent(database)}&table=${encodeURIComponent(table)}`),
  generateMetrics: (data) => request('/metrics/generate', { method: 'POST', body: JSON.stringify(data) }),

  // Dimensions (Dimension Library)
  listDimensions: (params = '') => request(`/dimensions${params}`),
  createDimension: (data) => request('/dimensions', { method: 'POST', body: JSON.stringify(data) }),
  updateDimension: (id, data) => request(`/dimensions/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteDimension: (id) => request(`/dimensions/${id}`, { method: 'DELETE' }),
  scanDimensions: (data) => request('/dimensions/scan', { method: 'POST', body: JSON.stringify(data || {}) }),

  // Health
  health: () => rawFetch('/health').then(r => r.json()),

  // Asset Catalog (数据资产地图)
  getCatalogSources: () => request('/catalog/sources'),
  getCatalogDatabases: (sourceId) => request(`/catalog/databases?source_id=${encodeURIComponent(sourceId)}`),
  getCatalogTables: (sourceId, database) => request(`/catalog/tables?source_id=${encodeURIComponent(sourceId)}&database=${encodeURIComponent(database)}`),
  getCatalogColumns: (sourceId, database, table) => request(`/catalog/columns?source_id=${encodeURIComponent(sourceId)}&database=${encodeURIComponent(database)}&table=${encodeURIComponent(table)}`),
  getCatalogRows: (sourceId, database, table, page = 1, pageSize = 50) =>
    request(`/catalog/rows?source_id=${encodeURIComponent(sourceId)}&database=${encodeURIComponent(database)}&table=${encodeURIComponent(table)}&page=${page}&page_size=${pageSize}`),
}
