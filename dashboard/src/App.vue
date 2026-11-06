<template>
  <n-config-provider :theme="naiveTheme" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-notification-provider>
        <n-dialog-provider>
  <div class="app" ref="appRef">
    <!-- Scroll progress indicator -->
    <div class="scroll-progress" :style="{ width: scrollProgress + '%' }"></div>

    <header class="glass-header" :class="{ scrolled: isScrolled }">
      <div class="header-inner">
        <router-link to="/" class="logo">
          <span class="logo-icon">W</span>
          <span class="logo-text">Warehouse Lab</span>
        </router-link>

        <nav class="header-nav">
          <router-link to="/demo" class="nav-link" active-class="nav-link-active">离线演示</router-link>
          <router-link v-if="health?.mode !== 'offline-demo'" to="/live" class="nav-link" active-class="nav-link-active">Live</router-link>
          <router-link v-if="health?.mode !== 'offline-demo'" to="/tasks" class="nav-link" active-class="nav-link-active">
            {{ $t('nav.tasks') }}
          </router-link>
          <router-link v-if="health?.mode !== 'offline-demo'" to="/catalog" class="nav-link" active-class="nav-link-active">
            {{ $t('nav.catalog') }}
          </router-link>
          <router-link v-if="health?.mode !== 'offline-demo'" to="/metrics" class="nav-link" active-class="nav-link-active">
            {{ $t('nav.metrics') }}
          </router-link>

          <!-- Global pending-approval badge (live via shared task store) -->
          <router-link
            v-if="displayApprovalCount > 0 && taskId"
            :to="`/task/${taskId}/approve`"
            class="pending-badge"
            :title="$t('nav.pendingApprovals', { count: displayApprovalCount })"
          >
            <span class="pending-badge-dot"></span>
            ✋ {{ displayApprovalCount }}
          </router-link>
          <router-link
            v-if="displayClarificationCount > 0 && taskId"
            :to="`/task/${taskId}/clarify`"
            class="pending-badge pending-badge-info"
            :title="$t('nav.pendingClarifications', { count: displayClarificationCount })"
          >
            <span class="pending-badge-dot"></span>
            💬 {{ displayClarificationCount }}
          </router-link>

          <button
            class="theme-toggle"
            @click="toggleTheme"
            :title="isDark ? $t('nav.themeToLight') : $t('nav.themeToDark')"
            :aria-label="isDark ? $t('nav.themeToLight') : $t('nav.themeToDark')"
          >
            <span class="theme-toggle-icon" :class="{ dark: isDark }">{{ isDark ? '☀' : '☾' }}</span>
          </button>

          <select class="lang-switch" v-model="$i18n.locale" aria-label="Language">
            <option value="zh-CN">中</option>
            <option value="en">EN</option>
          </select>

          <div class="connection-status" v-if="health" :data-tooltip="health.status === 'ok' ? 'System healthy' : 'System degraded'">
            <span :class="['connected-dot', health.status === 'ok' ? 'online' : 'offline']"></span>
            <span class="connection-label">
              {{ health.summary || (health.status === 'ok' ? 'Healthy' : 'Degraded') }}
            </span>
          </div>

          <span class="version-label">v0.2.0</span>
        </nav>
      </div>
    </header>

    <main class="app-shell">
      <router-view v-slot="{ Component, route: r }">
        <transition name="slide" mode="out-in" @before-enter="onPageEnter">
          <component :is="Component" :key="r.path" />
        </transition>
      </router-view>
    </main>

    <!-- Big-screen notice when the agent pauses for human input -->
    <GlobalInterruptNotice />
  </div>
        </n-dialog-provider>
      </n-notification-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch, watchEffect } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { darkTheme, NConfigProvider, NMessageProvider, NNotificationProvider, NDialogProvider } from 'naive-ui'
import { api } from './utils/api.js'
import { useTaskStore } from './stores/task.js'
import GlobalInterruptNotice from './components/GlobalInterruptNotice.vue'

const router = useRouter()
const route = useRoute()
const appRef = ref(null)
const health = ref(null)
const isScrolled = ref(false)
const scrollProgress = ref(0)

/* ── Light / dark theme ─────────────────────────────────── */
const THEME_KEY = 'wren-theme'
const savedTheme = localStorage.getItem(THEME_KEY)
const isDark = ref(
  savedTheme ? savedTheme === 'dark'
             : window.matchMedia('(prefers-color-scheme: dark)').matches
)
const naiveTheme = computed(() => (isDark.value ? darkTheme : null))
const themeOverrides = {
  common: {
    primaryColor: '#007AFF',
    primaryColorHover: '#409CFF',
    primaryColorPressed: '#0056CC',
    primaryColorSuppl: '#409CFF',
    borderRadius: '8px',
    fontFamily: `-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'SF Pro Text', 'Helvetica Neue', 'Inter', system-ui, sans-serif`,
  },
}

watchEffect(() => {
  if (isDark.value) document.documentElement.setAttribute('data-theme', 'dark')
  else document.documentElement.removeAttribute('data-theme')
})

function toggleTheme() {
  isDark.value = !isDark.value
  localStorage.setItem(THEME_KEY, isDark.value ? 'dark' : 'light')
}

/* ── Global task context: bind the shared store to the route's task ── */
const store = useTaskStore()
const { taskId, displayApprovalCount, displayClarificationCount } = storeToRefs(store)

watch(() => route.params.id, (id) => {
  if (id) store.setContext(id)
  else store.clearContext()
}, { immediate: true })

/* ── Scroll tracking for glass header + progress bar ────── */
let scrollTicking = false

function onScroll() {
  if (!scrollTicking) {
    requestAnimationFrame(() => {
      const st = window.scrollY
      isScrolled.value = st > 8

      const docH = document.documentElement.scrollHeight - window.innerHeight
      scrollProgress.value = docH > 0 ? Math.min(100, (st / docH) * 100) : 0

      scrollTicking = false
    })
    scrollTicking = true
  }
}

/* ── Page enter — scroll to top ─────────────────────────── */
function onPageEnter() {
  window.scrollTo({ top: 0, behavior: 'instant' })
}

/* ── Health check ────────────────────────────────────────── */
onMounted(async () => {
  window.addEventListener('scroll', onScroll, { passive: true })
  try { health.value = await api.health() } catch {}

  // Watch route changes to close mobile nav etc.
  router.afterEach(() => {
    window.scrollTo({ top: 0, behavior: 'smooth' })
  })
})

onUnmounted(() => {
  window.removeEventListener('scroll', onScroll)
})
</script>

<style scoped>
.app {
  min-height: 100vh;
}

/* ── Scroll Progress Bar ─────────────────────────────────── */
.scroll-progress {
  position: fixed;
  top: 0;
  left: 0;
  height: 2px;
  background: var(--color-accent);
  z-index: 200;
  transition: width 0.1s linear;
  border-radius: 0 1px 1px 0;
}

/* ── Header Layout ───────────────────────────────────────── */
.header-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 100%;
}

/* ── Logo ────────────────────────────────────────────────── */
.logo {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
  color: var(--color-text);
  -webkit-tap-highlight-color: transparent;
}
.logo-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  background: var(--color-accent);
  color: white;
  border-radius: 7px;
  font-weight: 700;
  font-size: 15px;
  transition:
    transform var(--duration-fast) var(--spring-default),
    box-shadow var(--duration-fast) var(--spring-default);
}
.logo:hover .logo-icon {
  transform: scale(1.05);
  box-shadow: 0 2px 8px rgba(0,122,255,0.3);
}
.logo:active .logo-icon {
  transform: scale(0.95);
  transition: transform var(--duration-instant) ease-out;
}
.logo-text {
  font-size: 1.0625rem;
  font-weight: 600;
  letter-spacing: -0.01em;
}

/* ── Navigation ──────────────────────────────────────────── */
.header-nav {
  display: flex;
  align-items: center;
  gap: 6px;
}

.nav-link {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--color-text-secondary);
  text-decoration: none;
  padding: 6px 12px;
  border-radius: var(--radius-xs);
  transition:
    color var(--duration-fast) var(--spring-default),
    background var(--duration-fast) var(--spring-default);
}
.nav-link:hover {
  color: var(--color-accent);
  background: var(--color-accent-glass);
}
.nav-link:active {
  transform: scale(0.96);
}
.nav-link-active {
  color: var(--color-accent);
}

.lang-switch {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  padding: 3px 10px;
  font-size: 0.75rem;
  font-weight: 500;
  background: var(--color-bg-elevated);
  cursor: pointer;
  outline: none;
  color: var(--color-text-secondary);
  font-family: var(--font-sans);
  transition:
    border-color var(--duration-fast) var(--spring-default),
    box-shadow var(--duration-fast) var(--spring-default);
}
.lang-switch:hover {
  border-color: var(--color-border-hover);
}
.lang-switch:focus-visible {
  box-shadow: var(--shadow-glow-accent);
}

.connection-status {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border-radius: var(--radius-full);
  transition: background var(--duration-fast) var(--spring-default);
}
.connection-status:hover {
  background: var(--color-bg);
}

.connection-label {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.version-label {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
  padding-left: 4px;
}

/* ── Theme toggle ────────────────────────────────────────── */
.theme-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  background: var(--color-bg-elevated);
  cursor: pointer;
  font-size: 0.875rem;
  color: var(--color-text-secondary);
  transition:
    border-color var(--duration-fast) var(--spring-default),
    transform var(--duration-fast) var(--spring-bounce),
    box-shadow var(--duration-fast) var(--spring-default);
}
.theme-toggle:hover {
  border-color: var(--color-border-hover);
  transform: scale(1.1) rotate(15deg);
  box-shadow: var(--shadow-card);
}
.theme-toggle:active { transform: scale(0.92); }
.theme-toggle-icon {
  display: inline-block;
  transition: transform var(--duration-spring) var(--spring-bounce);
}
.theme-toggle-icon.dark { transform: rotate(180deg); }

/* ── Pending HITL badges ───────────────────────────────── */
.pending-badge {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-warning, #ff9500);
  text-decoration: none;
  padding: 4px 10px;
  border-radius: var(--radius-full);
  background: var(--color-warning-bg, rgba(255,149,0,0.12));
  animation: pending-badge-pulse 2s ease-in-out infinite;
  transition: transform var(--duration-fast) var(--spring-default);
}
.pending-badge:hover { transform: scale(1.05); }
.pending-badge-info {
  color: var(--color-accent);
  background: var(--color-accent-glass);
}
.pending-badge-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
@keyframes pending-badge-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.65; }
}

/* ── Responsive ──────────────────────────────────────────── */
@media (max-width: 640px) {
  .connection-label {
    display: none;
  }
  .version-label {
    display: none;
  }
  .logo-text {
    display: none;
  }
  .glass-header {
    padding: 0 var(--space-md);
  }
  .header-inner {
    gap: var(--space-sm);
  }
}
</style>
