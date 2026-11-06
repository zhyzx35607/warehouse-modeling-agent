/**
 * Global task store — single WebSocket connection + shared task state.
 *
 * One connection per "current task context" (set from the route), shared by
 * every page (monitor, approvals, header badge, interrupt notice) so counts
 * and banners stay live no matter which page the user is on.
 *
 * The server has no event replay: on reconnect we refetch full-state +
 * counts via REST (resync).
 */

import { defineStore } from 'pinia'
import { api } from '../utils/api.js'

const ACTIVE_STATUSES = [
  'pending', 'running', 'exploring', 'designing', 'validating',
  'deploying', 'executing', 'grilling', 'awaiting_approval',
]
const COUNT_EVENTS = new Set([
  'interrupt', 'approval_added', 'approval_resolved',
  'clarification_added', 'clarification_answered', 'task_status_change',
])
const MAX_RECONNECT_DELAY = 30000
const HEARTBEAT_INTERVAL = 30000

// Module-level connection state (singleton — the store itself is one too)
let ws = null
let reconnectTimer = null
let heartbeatTimer = null
let reconnectAttempts = 0
let hasConnectedOnce = false
let countsThrottle = null
let fullStateThrottle = null

export const useTaskStore = defineStore('task', {
  state: () => ({
    taskId: '',
    task: null,
    fullState: null,
    pendingClarifications: 0,
    pendingApprovals: 0,
    countsLoaded: false,
    wsConnected: false,
    currentStatus: '',
    currentPhase: '',
    currentNode: '',
    wsTimeline: [],
    wsThoughts: [],
    wsEvents: [],
    wsError: null,
    interruptType: '',
    lastInterrupt: null,
    /** Increments on every interrupt event — watch trigger for big-screen notices. */
    interruptSeq: 0,
    /** Increments on approval/clarification events — pages watch to reload lists. */
    hitlEventSeq: 0,
  }),

  getters: {
    /** Persisted timeline merged with live WS entries (latest wins per node). */
    allTimeline(state) {
      const persisted = state.fullState?.node_timeline || []
      // Dedupe the persisted array itself too — pre-fix crash recoveries
      // persisted duplicated entries (e.g. "78/29 completed"), and without
      // this the dirty rows are rendered as-is.
      const merged = []
      for (const t of [...persisted, ...state.wsTimeline]) {
        const idx = merged.findIndex(e => e.node === t.node)
        if (idx >= 0) merged[idx] = t
        else merged.push(t)
      }
      // Warehouse mode: mark short-circuited nodes so the timeline shows a
      // "skipped" tag instead of implying real work happened (T-09).
      const isWarehouse = state.fullState?.task?.source_mode === 'warehouse'
      if (isWarehouse) {
        const skipSet = new Set([
          'design_ods', 'design_dim', 'design_dwd',
          'detect_schema_drifts', 'generate_drift_fixes', 'confirm_sensitive',
        ])
        for (const t of merged) {
          if (skipSet.has(t.node)) t.skipped = true
        }
      }
      return merged
    },
    isAwaitingHuman(state) {
      return state.currentStatus === 'grilling' || state.currentStatus === 'awaiting_approval'
    },
    displayClarificationCount(state) {
      const total = (state.fullState?.clarifications || []).length
      return state.countsLoaded ? state.pendingClarifications : total
    },
    displayApprovalCount(state) {
      const total = (state.fullState?.approval_items || []).length
      return state.countsLoaded ? state.pendingApprovals : total
    },
  },

  actions: {
    /** Bind the store to a task (from the route). Idempotent. */
    setContext(taskId) {
      if (!taskId || this.taskId === taskId) return
      this._disconnect()
      this.$reset()
      this.taskId = taskId
      hasConnectedOnce = false
      this.refreshAll()
    },

    clearContext() {
      this._disconnect()
      this.$reset()
    },

    /** Full refresh: task basics + full state + pending counts. */
    async refreshAll() {
      if (!this.taskId) return
      await Promise.all([this.loadTask(), this.loadFullState(), this.refreshCounts()])
      const status = this.task?.status || this.fullState?.task?.status || ''
      if (ACTIVE_STATUSES.includes(status)) this.connect()
    },

    async loadTask() {
      try {
        this.task = await api.getTask(this.taskId)
        if (this.task?.status) this.currentStatus = this.task.status
        if (this.task?.phase) this.currentPhase = this.task.phase
      } catch (e) {
        console.warn('Failed to load task:', e)
      }
    },

    async loadFullState() {
      try {
        const data = await api.getTaskFullState(this.taskId)
        this.fullState = data
        // Full-state is the freshest persisted snapshot; live WS events
        // arriving afterwards will overwrite these again.
        if (data.task?.status) this.currentStatus = data.task.status
        if (data.task?.phase) this.currentPhase = data.task.phase
      } catch (e) {
        console.error('Failed to load task full state:', e)
      }
    },

    async refreshCounts() {
      if (!this.taskId) return
      try {
        const clarifications = await api.getClarifications(this.taskId)
        this.pendingClarifications = (clarifications || []).filter(c => c.status === 'pending').length
      } catch (e) {
        console.warn('Failed to refresh clarification count:', e)
      }
      try {
        const approvals = await api.getApprovals(this.taskId)
        this.pendingApprovals = (approvals || []).filter(a => a.status === 'pending').length
      } catch (e) {
        console.warn('Failed to refresh approval count:', e)
      }
      this.countsLoaded = true
    },

    /* ── WebSocket ─────────────────────────────────────── */

    connect() {
      if (!this.taskId) return
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${protocol}//${location.host}/ws/tasks/${this.taskId}`)

      ws.onopen = () => {
        this._startHeartbeat()
      }

      ws.onmessage = (event) => {
        let msg
        try {
          msg = JSON.parse(event.data)
        } catch (e) {
          console.error('WS message parse error:', e)
          return
        }
        if (msg.type === 'pong') return
        this._handleEvent(msg)
      }

      ws.onclose = () => {
        this.wsConnected = false
        this._stopHeartbeat()
        if (!this.taskId) return  // context cleared — do not reconnect
        const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), MAX_RECONNECT_DELAY)
        reconnectAttempts++
        reconnectTimer = setTimeout(() => this.connect(), delay)
      }

      ws.onerror = () => {
        ws?.close()
      }
    },

    _disconnect() {
      clearTimeout(reconnectTimer)
      this._stopHeartbeat()
      if (ws) {
        ws.onclose = null
        ws.close()
        ws = null
      }
      this.wsConnected = false
    },

    _startHeartbeat() {
      this._stopHeartbeat()
      heartbeatTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, HEARTBEAT_INTERVAL)
    },

    _stopHeartbeat() {
      if (heartbeatTimer) {
        clearInterval(heartbeatTimer)
        heartbeatTimer = null
      }
    },

    _handleEvent(msg) {
      this.wsEvents.push(msg)
      if (this.wsEvents.length > 500) this.wsEvents = this.wsEvents.slice(-300)

      switch (msg.type) {
        case 'connected':
          this.wsConnected = true
          reconnectAttempts = 0
          // NOTE: the WS handshake is NOT a task phase — don't touch
          // currentPhase here (the stat card briefly showed "Connected").
          if (hasConnectedOnce) {
            // Reconnected after a drop — compensate missed events via REST.
            this.loadFullState()
            this.refreshCounts()
          }
          hasConnectedOnce = true
          break
        case 'task_status_change':
          this.currentStatus = msg.data?.new
          if (this.task && msg.data?.new) {
            this.task = { ...this.task, status: msg.data.new }
          }
          break
        case 'phase_change':
          this.currentPhase = msg.data?.phase
          this.currentNode = msg.data?.phase || ''
          break
        case 'node_timeline':
          this.currentNode = msg.data?.node || ''
          if (msg.data) this.wsTimeline = [...this.wsTimeline, msg.data]
          break
        case 'thought_stream':
          if (msg.data) this.wsThoughts = [...this.wsThoughts.slice(-99), msg.data]
          break
        case 'interrupt':
          this.interruptType = msg.data?.interrupt_type || msg.data?.type || ''
          this.currentStatus = msg.data?.status || this.currentStatus
          this.lastInterrupt = msg
          this.interruptSeq++
          break
        case 'error':
          this.wsError = msg.data?.message
          break
      }

      // Keep counts/full-state live without spamming REST: HITL events
      // refresh immediately, high-frequency events are throttled.
      if (COUNT_EVENTS.has(msg.type)) {
        this.hitlEventSeq++
        clearTimeout(countsThrottle)
        countsThrottle = null
        this.refreshCounts()
        this._scheduleFullStateReload()
      } else if (['phase_change', 'node_timeline', 'validation_result'].includes(msg.type)) {
        if (!countsThrottle) {
          countsThrottle = setTimeout(() => { countsThrottle = null; this.refreshCounts() }, 2000)
        }
        this._scheduleFullStateReload()
      }
    },

    _scheduleFullStateReload() {
      if (!fullStateThrottle) {
        fullStateThrottle = setTimeout(() => {
          fullStateThrottle = null
          this.loadFullState()
        }, 2000)
      }
    },
  },
})
