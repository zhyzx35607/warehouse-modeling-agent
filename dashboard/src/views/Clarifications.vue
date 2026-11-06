<template>
  <div class="clarifications">
    <div v-if="!inline" class="page-header">
      <router-link :to="`/task/${id}`" class="back-btn">&larr;</router-link>
      <div>
        <h2>{{ $t('clarify.title') }}</h2>
        <p>{{ $t('clarify.subtitle') }}</p>
      </div>
      <div v-if="items.length" class="ml-auto">
        <span class="badge badge-neutral">{{ $t('clarify.questionCount', { count: items.length }) }}</span>
        <span v-if="blockingCount" class="badge badge-error" style="margin-left:4px;">{{ $t('clarify.blockingCount', { count: blockingCount }) }}</span>
      </div>
    </div>
    <div v-else class="inline-header">
      <div>
        <h4 class="mb-0">{{ $t('clarify.title') }}</h4>
        <span v-if="items.length" class="inline-count">{{ $t('clarify.questionCount', { count: items.length }) }}</span>
      </div>
      <router-link :to="`/task/${id}/clarify`" class="btn btn-ghost btn-sm">{{ $t('clarify.openFullPage') }}</router-link>
    </div>

    <!-- Loading skeleton -->
    <div v-if="loading" class="loading-block">
      <div v-for="i in 3" :key="i" class="skeleton" style="height:140px;margin-bottom:var(--space-md);border-radius:var(--radius-lg);"></div>
    </div>

    <!-- Empty -->
    <div v-else-if="items.length === 0" class="card" style="text-align:center;padding:var(--space-3xl);">
      <div class="empty-state">
        <div class="empty-state-icon">✨</div>
        <div class="empty-state-title">{{ $t('clarify.noQuestions') }}</div>
        <div class="empty-state-desc">{{ $t('clarify.noQuestionsDesc') }}</div>
        <router-link :to="`/task/${id}`" class="btn btn-secondary btn-sm mt-md">
          {{ $t('clarify.backToTask') }}
        </router-link>
      </div>
    </div>

    <!-- Question Cards -->
    <TransitionGroup name="list" tag="div" v-else>
      <div
        v-for="(item, index) in items"
        :key="item.id"
        class="card clarify-card"
        :class="{ blocking: item.blocking }"
        :style="{ animationDelay: index * 0.06 + 's' }"
      >
        <!-- Header badges -->
        <div class="clarify-badges">
          <span :class="['badge', item.blocking ? 'badge-error' : 'badge-warning']">
            {{ item.blocking ? $t('clarify.blockingLabel') : $t('clarify.mediumLabel') }}
          </span>
          <span class="badge badge-neutral">{{ item.clarification_type }}</span>
          <span v-if="item.impact_level" class="badge badge-info">{{ item.impact_level }}</span>
        </div>

        <!-- Question -->
        <h3 class="clarify-question">{{ item.question }}</h3>
        <p class="clarify-context">{{ item.context }}</p>

        <!-- Already resolved (answered/skipped) — show the result, hide inputs -->
        <div v-if="isResolved(item)" class="resolved-banner">
          <span class="badge" :class="item.status === 'answered' ? 'badge-success' : 'badge-neutral'">
            {{ item.status === 'answered' ? $t('clarify.answered') : $t('clarify.skipped') }}
          </span>
          <span v-if="item.answer" class="resolved-answer">{{ item.answer }}</span>
        </div>

        <template v-else>
        <!-- Candidate answers -->
        <div v-if="item.candidates?.length" class="candidates-row">
          <button
            v-for="(cand, ci) in item.candidates"
            :key="ci"
            class="btn btn-secondary btn-sm candidate-btn"
            :class="{
              'candidate-selected': item._selected === cand,
              'candidate-recommended': cand === item.recommended,
            }"
            @click="item._selected = (item._selected === cand ? null : cand)"
          >
            <span v-if="cand === item.recommended" class="rec-star">★</span>{{ cand }}
          </button>
        </div>

        <!-- Agent recommendation -->
        <p v-if="item.recommended" class="recommended-hint">
          {{ $t('clarify.agentRecommend', { recommended: item.recommended }) }}
          <span v-if="item.recommended_reason" class="recommended-reason">— {{ item.recommended_reason }}</span>
        </p>

        <!-- Answer input -->
        <div class="answer-row">
          <input
            v-model="item._answer"
            class="input answer-input"
            :disabled="item._submitting"
            :placeholder="item._selected ? $t('clarify.customAnswer', { selected: item._selected }) : $t('clarify.typeHere')"
            @keyup.enter="submit(item)"
          />
          <div class="answer-btns">
            <button class="btn btn-secondary btn-sm" :disabled="item._submitting" @click="skip(item)">{{ $t('clarify.skip') }}</button>
            <button
              class="btn btn-primary btn-sm"
              @click="submit(item)"
              :disabled="item._submitting || (!item._answer && !item._selected)"
            >
              {{ $t('clarify.submit') }}
            </button>
          </div>
        </div>

        <!-- Submitted feedback -->
        <Transition name="fade">
          <div v-if="item._submitted" class="submitted-banner">
            <span class="badge badge-success">{{ $t('clarify.answered') }}</span>
            <span style="font-size:0.8125rem;color:var(--color-text-tertiary);">
              {{ $t('clarify.submitted') }}
            </span>
          </div>
        </Transition>
        </template>
      </div>
    </TransitionGroup>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { storeToRefs } from 'pinia'
import { api } from '../utils/api.js'
import { useTaskStore } from '../stores/task.js'

const { t } = useI18n()

const props = defineProps({
  inline: { type: Boolean, default: false },
})

const route = useRoute()
const id = route.params.id
const items = ref([])
const loading = ref(true)

/* Live updates: the global store owns the task WS connection — reload the
   list (throttled) whenever a clarification/interrupt event arrives, plus
   a slow poll while the tab is visible as a fallback. */
const store = useTaskStore()
const { hitlEventSeq } = storeToRefs(store)

let refreshThrottle = null
watch(hitlEventSeq, () => {
  if (refreshThrottle) return
  refreshThrottle = setTimeout(() => { refreshThrottle = null; load() }, 1000)
})

let pollTimer = null

const blockingCount = computed(() =>
  items.value.filter(i => i.blocking && !isResolved(i)).length
)

function isResolved(item) {
  return item.status === 'answered' || item.status === 'skipped'
}

async function load() {
  try {
    const data = await api.getClarifications(id)
    // Preserve local input state (draft answer / just-submitted flag) so a
    // background refresh never clobbers what the user is doing.
    const prev = new Map(items.value.map(i => [i.id, i]))
    items.value = (data || []).map(item => {
      const old = prev.get(item.id)
      return {
        ...item,
        _answer: old?._answer || '',
        _selected: old?._selected ?? null,
        _submitted: old?._submitted || false,
        _submitting: false,
      }
    })
  } catch (e) {
    console.error('Failed to load clarifications:', e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  store.setContext(id)
  await load()
  pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') load()
  }, 10000)
})

onUnmounted(() => {
  clearInterval(pollTimer)
  pollTimer = null
  clearTimeout(refreshThrottle)
})

async function submit(item) {
  const answer = item._answer || item._selected
  if (!answer || item._submitting) return
  item._submitting = true
  try {
    await api.answerClarification(id, {
      clarification_id: item.id, answer,
    })
    item._submitted = true
    item.status = 'answered'
    item.answer = answer
    // Refresh header badge counts + pull the server truth shortly after
    // (the graph may resume and produce follow-up questions).
    store.refreshCounts()
    setTimeout(load, 800)
  } catch (e) {
    alert(t('common.error') + ': ' + e.message)
  } finally {
    item._submitting = false
  }
}

async function skip(item) {
  if (item._submitting) return
  item._submitting = true
  try {
    await api.answerClarification(id, {
      clarification_id: item.id,
      answer: '',
      skip: true,
    })
    item.status = 'skipped'
    item.answer = null
    store.refreshCounts()
    setTimeout(load, 800)
  } catch (e) {
    alert(t('common.error') + ': ' + e.message)
  } finally {
    item._submitting = false
  }
}
</script>

<style scoped>
/* ── Loading ─────────────────────────────────────────────── */
.loading-block {
  padding: var(--space-md) 0;
}

/* ── Card ────────────────────────────────────────────────── */
.clarify-card {
  margin-bottom: var(--space-md);
  border-left: 4px solid transparent;
  transition:
    border-color var(--duration-base) var(--spring-default),
    box-shadow var(--duration-base) var(--spring-default),
    transform var(--duration-base) var(--spring-default);
  animation: card-enter 0.4s var(--spring-default) both;
}

@keyframes card-enter {
  from {
    opacity: 0;
    transform: translateY(12px);
  }
}

.clarify-card.blocking {
  border-left-color: var(--color-error);
}

.clarify-badges {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  margin-bottom: var(--space-sm);
  flex-wrap: wrap;
}

.clarify-question {
  margin-bottom: var(--space-2xs);
  font-size: 1.125rem;
}

.clarify-context {
  margin-bottom: var(--space-md);
  font-size: 0.875rem;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

/* ── Candidates ──────────────────────────────────────────── */
.candidates-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin-bottom: var(--space-md);
}

.candidate-btn {
  transition:
    all var(--duration-fast) var(--spring-default);
}
.candidate-btn:active {
  transform: scale(0.95);
  transition: transform var(--duration-instant) ease-out;
}
.candidate-selected {
  background: var(--color-accent);
  color: white;
  border-color: var(--color-accent);
}
.candidate-selected:hover {
  background: var(--color-accent-hover);
  color: white;
}
.candidate-recommended {
  border-color: var(--color-accent);
  box-shadow: 0 0 0 1px var(--color-accent);
}

.rec-star {
  margin-right: 4px;
  color: var(--color-warning);
}
.candidate-selected .rec-star {
  color: white;
}

.recommended-hint {
  font-size: 0.8125rem;
  color: var(--color-accent);
  margin-bottom: var(--space-md);
}

.recommended-reason {
  color: var(--color-text-tertiary);
}

/* ── Resolved (answered/skipped) ─────────────────────────── */
.resolved-banner {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.resolved-answer {
  font-size: 0.875rem;
  color: var(--color-text-secondary);
}

/* ── Answer Row ──────────────────────────────────────────── */
.answer-row {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.answer-input {
  flex: 1;
}

.answer-btns {
  display: flex;
  gap: var(--space-sm);
  flex-shrink: 0;
}

/* ── Submitted ───────────────────────────────────────────── */
.submitted-banner {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-top: var(--space-sm);
  padding-top: var(--space-sm);
  border-top: 1px solid var(--color-border);
}

/* ── Utility ─────────────────────────────────────────────── */
.ml-auto { margin-left: auto; }
.mt-md { margin-top: var(--space-md); }
</style>
