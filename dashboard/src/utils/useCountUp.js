/** Animated number counter (count-up on value change). */

import { ref, watch, onUnmounted } from 'vue'

/**
 * @param source ref/computed getter returning the target number
 * @param duration animation duration in ms
 * @returns ref with the tweened display value (integer)
 */
export function useCountUp(source, duration = 600) {
  const display = ref(0)
  let raf = null
  let from = 0

  function animateTo(target) {
    cancelAnimationFrame(raf)
    from = display.value
    if (from === target) return
    const start = performance.now()

    const tick = (now) => {
      const p = Math.min(1, (now - start) / duration)
      // easeOutCubic
      const eased = 1 - Math.pow(1 - p, 3)
      display.value = Math.round(from + (target - from) * eased)
      if (p < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
  }

  watch(source, (val) => animateTo(Number(val) || 0), { immediate: true })

  onUnmounted(() => cancelAnimationFrame(raf))

  return display
}
