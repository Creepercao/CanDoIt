<script setup>
import { ref, nextTick, watch } from 'vue'
import { useChatStore } from '../stores/chat.js'
import { renderMarkdown } from '../utils/markdown.js'

const store = useChatStore()

const topic = ref('')
const style = ref('a')
const streamContainer = ref(null)

const styles = [
  { value: 'a', label: 'Style A — 学霸笔记本', desc: '米黄横线纸 + 螺旋装订 + 单页滚动，适合技术笔记、知识总结' },
  { value: 'b', label: 'Style B — 手账皮革本', desc: '皮革封面 + 金属环装订 + 翻页交互，适合漏洞分析、深度研究' },
]

function scrollToBottom() {
  if (streamContainer.value) streamContainer.value.scrollTop = streamContainer.value.scrollHeight
}
watch(() => store.noteStreamText, () => nextTick(() => scrollToBottom()))
watch(() => store.noteSteps.length, () => nextTick(() => scrollToBottom()))

// Debounced markdown (same pattern as ChatPanel / PPTGenerator)
const streamHtml = ref('')
let debounceTimer = null
watch(() => store.noteStreamText, (val) => {
  if (!store.noteGenerating) { streamHtml.value = renderMarkdown(val); return }
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => { streamHtml.value = renderMarkdown(val || '') }, 30)
})

async function generate() {
  if (!topic.value.trim() || store.noteGenerating) return
  await store.doGenerateNote(topic.value, { style: style.value })
}

function stop() { store.stopNoteGeneration() }

function agentEmoji(type) {
  const map = { research: '🔍', '学霸笔记': '📓' }
  return map[type] || '🔧'
}
</script>

<template>
  <div class="flex flex-col h-full">
    <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700/50 shrink-0">
      <h2 class="font-semibold text-lg text-white">📓 学霸笔记</h2>
    </div>

    <div class="flex-1 overflow-y-auto p-4 space-y-4">
      <!-- Topic -->
      <div>
        <label class="text-xs text-gray-400 mb-1.5 block">What topic should the notes cover?</label>
        <textarea
          v-model="topic"
          placeholder="Describe the subject you want study notes for..."
          rows="3"
          :disabled="store.noteGenerating"
          class="w-full bg-surface-800 border border-gray-600 rounded-xl px-4 py-2.5 text-sm text-white placeholder-gray-500 resize-none disabled:opacity-50 focus:border-primary-500 focus:outline-none"
        />
      </div>

      <!-- Style selector -->
      <div>
        <label class="text-xs text-gray-400 mb-2 block">Note Style</label>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <button
            v-for="s in styles" :key="s.value"
            @click="style = s.value"
            :disabled="store.noteGenerating"
            :class="[
              'text-left p-3 rounded-xl border transition-colors disabled:opacity-50',
              style === s.value
                ? 'bg-primary-600/20 border-primary-500/50 text-white'
                : 'bg-surface-800 border-gray-600 text-gray-400 hover:border-gray-500',
            ]"
          >
            <div class="text-sm font-medium">{{ s.label }}</div>
            <div class="text-[10px] mt-0.5 opacity-70">{{ s.desc }}</div>
          </button>
        </div>
      </div>

      <!-- Action -->
      <div class="flex items-center gap-2">
        <button
          v-if="!store.noteGenerating"
          @click="generate"
          :disabled="!topic.trim()"
          class="flex-1 py-3 bg-amber-600 hover:bg-amber-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm font-medium rounded-xl transition-colors"
        >
          📝 Generate Notes
        </button>
        <button v-else @click="stop"
          class="flex-1 py-3 bg-red-600 hover:bg-red-500 text-white text-sm font-medium rounded-xl transition-colors">
          ⏹ Stop
        </button>
      </div>

      <!-- Live streaming area -->
      <div v-if="store.noteGenerating || (store.noteStreamText && !store.noteResults.length)"
        class="bg-surface-800 rounded-xl border border-gray-700/50 overflow-hidden">
        <div v-if="store.noteSteps.length" class="px-4 py-2 border-b border-gray-700/30 bg-gray-900/30">
          <div class="flex items-center gap-2 flex-wrap">
            <template v-for="(step, i) in store.noteSteps" :key="i">
              <span v-if="step.type === 'phase'" class="text-[10px] text-gray-500">{{ step.message }}</span>
              <span v-else-if="step.type === 'agent_start'"
                :class="['text-[10px] px-1.5 py-0.5 rounded-full border transition-colors',
                  step.done ? 'bg-emerald-900/30 text-emerald-400 border-emerald-700/50'
                           : 'bg-amber-900/30 text-amber-400 border-amber-700/50 animate-pulse']">
                {{ agentEmoji(step.agentType) }} {{ step.label || step.agentType }}
                {{ step.done ? ' ✓' : ' …' }}
              </span>
            </template>
          </div>
        </div>
        <div ref="streamContainer" class="p-4 max-h-[40vh] overflow-y-auto">
          <div v-if="store.noteStreamText"
            class="prose prose-invert prose-sm max-w-none text-sm leading-relaxed"
            v-html="streamHtml" />
          <div v-else class="text-sm text-gray-500 animate-pulse">Researching and writing notes...</div>
        </div>
        <div class="w-full bg-gray-700 h-1">
          <div class="bg-gradient-to-r from-amber-500 to-orange-500 h-full transition-all duration-500"
            :style="{ width: store.noteStreamText ? '85%' : '20%' }" />
        </div>
      </div>

      <!-- Results -->
      <div v-if="store.noteResults.length" class="space-y-3">
        <h3 class="text-sm font-medium text-gray-300 border-t border-gray-700/50 pt-3">Notes Results</h3>
        <div v-for="(item, idx) in store.noteResults.slice().reverse()" :key="idx"
          class="bg-surface-800 rounded-xl border border-gray-700/50 p-4 space-y-2">
          <div class="flex items-center justify-between">
            <p class="text-sm text-white font-medium truncate max-w-[70%]">{{ item.topic }}</p>
            <span class="text-[10px] text-gray-500">{{ item.style === 'b' ? 'Style B' : 'Style A' }}</span>
          </div>
          <div v-if="item.error" class="text-sm text-red-400">{{ item.error }}</div>
          <div v-else class="space-y-2">
            <div v-for="html in (item.htmlResults || [])" :key="html.html_url"
              class="flex items-center justify-between bg-gray-900/50 rounded-lg px-3 py-2">
              <a :href="html.html_url" target="_blank"
                class="text-primary-400 hover:underline text-sm truncate mr-2">
                📄 {{ html.title || 'HTML Preview' }}
              </a>
            </div>
            <details v-if="item.response" class="text-xs">
              <summary class="text-gray-500 cursor-pointer">Response text</summary>
              <div class="mt-1 text-gray-400 max-h-48 overflow-y-auto prose prose-invert prose-xs max-w-none"
                v-html="renderMarkdown(item.response)" />
            </details>
          </div>
          <div class="text-[10px] text-gray-600 text-right">{{ new Date(item.timestamp).toLocaleString() }}</div>
        </div>
      </div>
    </div>
  </div>
</template>
