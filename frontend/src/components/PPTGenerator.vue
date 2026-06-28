<script setup>
import { ref, nextTick, watch } from 'vue'
import { useChatStore } from '../stores/chat.js'
import { renderMarkdown } from '../utils/markdown.js'
import { exportPptx } from '../api/index.js'

const store = useChatStore()

const topic = ref('')
const theme = ref('dark-tech')
const slideCount = ref(6)
const exporting = ref({})
const streamContainer = ref(null)

const themes = [
  { value: 'dark-tech', label: 'Dark Tech — 暗色炫酷科技风' },
  { value: 'warm-paper', label: 'Warm Paper — 暖色报纸风' },
  { value: 'clean-white', label: 'Clean White — 简约白色风' },
]

function scrollToBottom() {
  if (streamContainer.value) {
    streamContainer.value.scrollTop = streamContainer.value.scrollHeight
  }
}

watch(() => store.pptStreamText, () => nextTick(() => scrollToBottom()))
watch(() => store.pptSteps.length, () => nextTick(() => scrollToBottom()))

// Debounced markdown rendering (same pattern as ChatPanel)
const streamHtml = ref('')
let debounceTimer = null
watch(() => store.pptStreamText, (val) => {
  if (!store.pptGenerating) {
    streamHtml.value = renderMarkdown(val)
    return
  }
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    streamHtml.value = renderMarkdown(val || '')
  }, 30)
})

async function generate() {
  if (!topic.value.trim() || store.pptGenerating) return
  await store.doGeneratePPT(topic.value, {
    theme: theme.value,
    slideCount: slideCount.value,
  })
}

function stop() {
  store.stopPPTGeneration()
}

async function downloadPPTX(htmlUrl, title) {
  exporting.value[htmlUrl] = true
  try {
    const result = await exportPptx({
      htmlUrl,
      title: title || 'PPT',
      mode: 'final',
    })
    if (result.file_url) {
      window.open(result.file_url, '_blank')
    } else if (result.error) {
      alert('PPTX export failed: ' + result.error)
    }
  } catch (e) {
    console.error('PPTX export error:', e)
    alert('PPTX export failed: ' + (e.message || 'unknown error'))
  } finally {
    exporting.value[htmlUrl] = false
  }
}

function agentEmoji(agentType) {
  const map = {
    research: '🔍', analyst: '🔢', chart: '📊',
    image_gen: '🎨', video_gen: '🎬', code: '💻',
    ppt_planner: '🧭', ppt_slide: '🧩', ppt_assembler: '🎞️',
  }
  return map[agentType] || '🔧'
}

function formatTime(ts) {
  return new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
</script>

<template>
  <div class="flex flex-col h-full">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700/50 shrink-0">
      <h2 class="font-semibold text-lg text-white">📊 PPT Presentation</h2>
    </div>

    <!-- Content -->
    <div class="flex-1 overflow-y-auto p-4 space-y-4">
      <!-- Topic -->
      <div>
        <label class="text-xs text-gray-400 mb-1.5 block">What topic should the PPT cover?</label>
        <textarea
          v-model="topic"
          placeholder="Describe the subject for your slide deck..."
          rows="3"
          :disabled="store.pptGenerating"
          class="w-full bg-surface-800 border border-gray-600 rounded-xl px-4 py-2.5 text-sm text-white placeholder-gray-500 resize-none disabled:opacity-50 focus:border-primary-500 focus:outline-none"
        />
      </div>

      <!-- Theme + slide count -->
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label class="text-xs text-gray-400 mb-1 block">Theme</label>
          <select
            v-model="theme"
            :disabled="store.pptGenerating"
            class="w-full bg-surface-800 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white disabled:opacity-50 focus:border-primary-500 focus:outline-none"
          >
            <option v-for="t in themes" :key="t.value" :value="t.value">{{ t.label }}</option>
          </select>
        </div>
        <div>
          <label class="text-xs text-gray-400 mb-1 block">Slides: {{ slideCount }}</label>
          <input
            v-model.number="slideCount"
            type="range" min="3" max="15"
            :disabled="store.pptGenerating"
            class="w-full accent-primary-500 disabled:opacity-50"
          />
          <div class="flex justify-between text-[10px] text-gray-500 mt-0.5">
            <span>3</span><span>15</span>
          </div>
        </div>
      </div>

      <!-- Action buttons -->
      <div class="flex items-center gap-2">
        <button
          v-if="!store.pptGenerating"
          @click="generate"
          :disabled="!topic.trim()"
          class="flex-1 py-3 bg-primary-600 hover:bg-primary-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm font-medium rounded-xl transition-colors"
        >
          🚀 Generate PPT
        </button>
        <button
          v-else
          @click="stop"
          class="flex-1 py-3 bg-red-600 hover:bg-red-500 text-white text-sm font-medium rounded-xl transition-colors"
        >
          ⏹ Stop Generation
        </button>
      </div>

      <!-- ── Live streaming area (visible during generation) ── -->
      <div v-if="store.pptGenerating || (store.pptStreamText && !store.pptResults.length)"
        class="bg-surface-800 rounded-xl border border-gray-700/50 overflow-hidden">
        <!-- Agent progress steps (mini timeline) -->
        <div v-if="store.pptSteps.length" class="px-4 py-2 border-b border-gray-700/30 bg-gray-900/30">
          <div class="flex items-center gap-2 flex-wrap">
            <template v-for="(step, i) in store.pptSteps" :key="i">
              <span v-if="step.type === 'phase'" class="text-[10px] text-gray-500">
                {{ step.message }}
              </span>
              <span
                v-else-if="step.type === 'agent_start'"
                :class="[
                  'text-[10px] px-1.5 py-0.5 rounded-full border transition-colors',
                  step.done
                    ? 'bg-emerald-900/30 text-emerald-400 border-emerald-700/50'
                    : 'bg-blue-900/30 text-blue-400 border-blue-700/50 animate-pulse',
                ]"
              >
                {{ agentEmoji(step.agentType) }}
                {{ step.label || step.agentType }}
                <template v-if="step.slideIndex"> #{{ step.slideIndex }}</template>
                {{ step.done ? ' ✓' : ' …' }}
              </span>
            </template>
          </div>
        </div>

        <!-- Streaming text (token-by-token, markdown rendered) -->
        <div
          ref="streamContainer"
          class="p-4 max-h-[40vh] overflow-y-auto"
        >
          <div v-if="store.pptStreamText"
            class="prose prose-invert prose-sm max-w-none text-sm leading-relaxed"
            v-html="streamHtml"
          />
          <div v-else class="text-sm text-gray-500 animate-pulse">
            Planning slides...
          </div>
        </div>

        <!-- Progress bar -->
        <div class="w-full bg-gray-700 h-1">
          <div
            class="bg-gradient-to-r from-primary-500 to-orange-500 h-full transition-all duration-500"
            :style="{ width: store.pptStreamText ? '85%' : '20%' }"
          />
        </div>
      </div>

      <!-- ── Results (after generation completes) ── -->
      <div v-if="store.pptResults.length" class="space-y-3">
        <h3 class="text-sm font-medium text-gray-300 border-t border-gray-700/50 pt-3">
          Generation Results
        </h3>
        <div
          v-for="(item, idx) in store.pptResults.slice().reverse()"
          :key="idx"
          class="bg-surface-800 rounded-xl border border-gray-700/50 p-4 space-y-2"
        >
          <div class="flex items-center justify-between">
            <p class="text-sm text-white font-medium truncate max-w-[70%]">{{ item.topic }}</p>
            <span class="text-[10px] text-gray-500">
              {{ item.theme }} · {{ item.slideCount }} slides
            </span>
          </div>

          <!-- Error -->
          <div v-if="item.error" class="text-sm text-red-400">{{ item.error }}</div>

          <!-- HTML results -->
          <div v-else class="space-y-2">
            <div
              v-for="html in (item.htmlResults || [])"
              :key="html.html_url"
              class="flex items-center justify-between bg-gray-900/50 rounded-lg px-3 py-2"
            >
              <a
                :href="html.html_url"
                target="_blank"
                class="text-primary-400 hover:underline text-sm truncate mr-2"
              >
                📄 {{ html.title || 'HTML Preview' }}
              </a>
              <button
                @click="downloadPPTX(html.html_url, html.title || 'PPT')"
                :disabled="exporting[html.html_url]"
                class="shrink-0 text-xs px-3 py-1.5 bg-purple-600 hover:bg-purple-500 disabled:bg-gray-700 disabled:text-gray-500 text-white rounded-lg transition-colors"
              >
                {{ exporting[html.html_url] ? '⏳' : '📥 PPTX' }}
              </button>
            </div>

            <!-- Response text (collapsed) -->
            <details v-if="item.response" class="text-xs">
              <summary class="text-gray-500 cursor-pointer">Response text</summary>
              <div
                class="mt-1 text-gray-400 max-h-48 overflow-y-auto prose prose-invert prose-xs max-w-none"
                v-html="renderMarkdown(item.response)"
              />
            </details>

            <!-- Agent steps summary (collapsed) -->
            <details v-if="item.steps?.length" class="text-xs">
              <summary class="text-gray-500 cursor-pointer">
                Agent timeline ({{ item.steps.length }} events)
              </summary>
              <div class="mt-1 space-y-0.5 max-h-32 overflow-y-auto">
                <div v-for="(step, si) in item.steps" :key="si" class="flex items-center gap-2 text-[10px]">
                  <span class="text-gray-600 w-12 shrink-0">{{ formatTime(item.timestamp) }}</span>
                  <span v-if="step.type === 'phase'" class="text-gray-500">{{ step.message }}</span>
                  <span v-else-if="step.type === 'agent_start'" class="text-gray-400">
                    {{ agentEmoji(step.agentType) }} {{ step.label }}
                    <template v-if="step.slideIndex"> #{{ step.slideIndex }}</template>
                    — {{ step.task?.slice(0, 60) || '' }}
                  </span>
                </div>
              </div>
            </details>
          </div>

          <div class="text-[10px] text-gray-600 text-right">
            {{ new Date(item.timestamp).toLocaleString() }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
