/** WebSocket connection manager for task event streams. */

import { ref, onUnmounted } from 'vue'

/**
 * Subscribe to /ws/tasks/:taskId with auto-reconnect, heartbeat and
 * reconnect-resync hooks.
 *
 * Options:
 * - onEvent(msg): called for EVERY parsed event (after internal state
 *   updates), so pages can react to events the composable does not track
 *   (approval_added / approval_resolved / clarification_added /
 *   validation_result / interrupt ...).
 * - onResync(): called after a successful RE-connect (not the first
 *   connect) — the server has no event replay, so missed events must be
 *   compensated via REST refetch.
 */
export function useTaskWebSocket(taskId, { onEvent, onResync } = {}) {
  const connected = ref(false)
  const events = ref([])
  const currentPhase = ref('')
  const currentStatus = ref('')
  const interruptType = ref('')
  const error = ref(null)
  const nodeTimeline = ref([])
  const thoughts = ref([])
  const currentNode = ref('')
  /** Latest interrupt event payload (items, counts) for big-screen prompts. */
  const lastInterrupt = ref(null)

  let ws = null
  let reconnectTimer = null
  let heartbeatTimer = null
  let reconnectAttempts = 0
  let hasConnectedOnce = false
  const maxReconnectDelay = 30000
  const heartbeatInterval = 30000

  function connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${location.host}/ws/tasks/${taskId}`
    ws = new WebSocket(url)

    ws.onopen = () => {
      startHeartbeat()
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.type === 'pong') return
        events.value.push(msg)

        switch (msg.type) {
          case 'connected':
            connected.value = true
            reconnectAttempts = 0
            // Handshake is not a task phase — don't overwrite currentPhase.
            if (hasConnectedOnce) {
              // Reconnected after a drop — events were missed, let the
              // page refetch full state + counts via REST.
              try { onResync?.() } catch (e) { console.warn('WS onResync error:', e) }
            }
            hasConnectedOnce = true
            break
          case 'task_status_change':
            currentStatus.value = msg.data?.new
            break
          case 'phase_change':
            currentPhase.value = msg.data?.phase
            currentNode.value = msg.data?.phase || ''
            break
          case 'node_timeline':
            currentNode.value = msg.data?.node || ''
            if (msg.data) {
              nodeTimeline.value = [...nodeTimeline.value, msg.data]
            }
            break
          case 'thought_stream':
            if (msg.data) {
              thoughts.value = [...thoughts.value.slice(-99), msg.data]
            }
            break
          case 'interrupt':
            interruptType.value = msg.data?.interrupt_type || msg.data?.type || ''
            currentStatus.value = msg.data?.status || currentStatus.value
            lastInterrupt.value = msg
            break
          case 'error':
            error.value = msg.data?.message
            break
        }

        try { onEvent?.(msg) } catch (e) { console.warn('WS onEvent error:', e) }

        // Trim events to last 500
        if (events.value.length > 500) {
          events.value = events.value.slice(-300)
        }
      } catch (e) {
        console.error('WS message parse error:', e)
      }
    }

    ws.onclose = () => {
      connected.value = false
      stopHeartbeat()
      scheduleReconnect()
    }

    ws.onerror = () => {
      ws?.close()
    }
  }

  function scheduleReconnect() {
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), maxReconnectDelay)
    reconnectAttempts++
    reconnectTimer = setTimeout(connect, delay)
  }

  function startHeartbeat() {
    stopHeartbeat()
    heartbeatTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }))
      }
    }, heartbeatInterval)
  }

  function stopHeartbeat() {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer)
      heartbeatTimer = null
    }
  }

  function disconnect() {
    clearTimeout(reconnectTimer)
    stopHeartbeat()
    if (ws) {
      ws.onclose = null
      ws.close()
      ws = null
    }
    connected.value = false
  }

  onUnmounted(disconnect)

  return {
    connected, events, currentPhase, currentStatus, interruptType, error,
    nodeTimeline, thoughts, currentNode, lastInterrupt, connect, disconnect
  }
}
