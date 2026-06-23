<script setup>
import { ref, nextTick, watch } from 'vue'
import { useChatStore } from '../stores/chat.js'
import { renderMarkdown } from '../utils/markdown.js'
import ThinkingPanel from './ThinkingPanel.vue'
import ImageLightbox from './ImageLightbox.vue'

const store = useChatStore()
const input = ref('')
const chatContainer = ref(null)
const lightbox = ref({ visible: false, src: '', alt: '' })

async function send() {
  const text = input.value
  if (!text.trim()) return
  input.value = ''
  await store.sendChatMessage(text)
  await nextTick()
  scrollToBottom()
}

function handleKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

function scrollToBottom() {
  if (chatContainer.value) {
    chatContainer.value.scrollTop = chatContainer.value.scrollHeight
  }
}

watch(() => store.messages.length, () => nextTick(() => scrollToBottom()))
watch(() => store.currentResponse, () => nextTick(() => scrollToBottom()))

function formatTime(ts) {
  return new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function openLightbox(src, alt) {
  lightbox.value = { visible: true, src, alt }
}

function renderContent(text) {
  if (!text) return ''
  return renderMarkdown(text)
}

// Debounced streaming markdown – re-render at most every 80ms
const streamingHtml = ref('')
let debounceTimer = null
watch(() => store.currentResponse, (val) => {
  if (!store.isLoading) {
    // Final render: no debounce needed
    streamingHtml.value = renderMarkdown(val)
    return
  }
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    streamingHtml.value = renderMarkdown(val || '')
  }, 80)
})
</script>

<template>
  <div class="flex flex-col h-full">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700/50 shrink-0">
      <h2 class="font-semibold text-lg">💬 多智能体对话</h2>
      <button
        @click="store.clearChat()"
        class="text-xs text-gray-400 hover:text-white px-3 py-1 rounded-lg hover:bg-surface-700 transition-colors"
      >
        清空对话
      </button>
    </div>

    <!-- Messages -->
    <div ref="chatContainer" class="flex-1 overflow-y-auto px-4 py-4 space-y-4">
      <div v-if="store.messages.length === 0 && !store.isLoading" class="text-center text-gray-500 mt-20">
        <div class="text-4xl mb-4">🤖</div>
        <p class="text-lg mb-2">多智能体协作平台</p>
        <p class="text-sm">智能体会自动分工：搜索、绘画、视频、编程，并行工作</p>
        <p class="text-xs text-gray-600 mt-2">Shift+Enter 换行，Enter 发送</p>
      </div>

      <!-- Completed messages -->
      <div
        v-for="(msg, idx) in store.messages"
        :key="idx"
        :class="['flex gap-3', msg.role === 'user' ? 'justify-end' : 'justify-start']"
      >
        <!-- Avatar -->
        <div v-if="msg.role === 'assistant'" class="w-8 h-8 rounded-full bg-primary-600 flex items-center justify-center text-sm shrink-0">
          🤖
        </div>

        <div
          :class="[
            'max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed',
            'overflow-hidden',
            msg.role === 'user'
              ? 'bg-primary-600 text-white rounded-br-md'
              : msg.isError
                ? 'bg-red-900/50 border border-red-500/30 text-red-200 rounded-bl-md'
                : 'bg-surface-800 border border-gray-700/50 rounded-bl-md',
          ]"
        >
          <!-- Past think steps (collapsed by default) -->
          <ThinkingPanel
            v-if="msg.thinkSteps?.length"
            :steps="msg.thinkSteps"
            :is-streaming="false"
          />

          <!-- Rendered markdown content -->
          <div
            class="prose prose-invert prose-sm max-w-none [&_a]:text-blue-400 [&_a]:underline [&_a]:break-all [&_pre]:bg-surface-900 [&_code]:text-green-300 [&_blockquote]:border-l-primary-500 [&_table]:text-xs"
            v-html="renderContent(msg.content)"
          ></div>

          <!-- Image results -->
          <div v-if="msg.image_results?.length" class="mt-3 space-y-3">
            <div
              v-for="(ir, ii) in msg.image_results"
              :key="'img-'+ii"
              class="rounded-lg overflow-hidden border border-gray-600 bg-surface-900"
            >
              <img
                v-if="ir.result?.url"
                :src="ir.result.url"
                :alt="ir.task"
                class="w-full max-h-80 object-contain bg-black/50 cursor-zoom-in hover:opacity-90 transition-opacity"
                @click="openLightbox(ir.result.url, ir.task)"
                loading="lazy"
              />
              <div v-else-if="ir.result?.error" class="p-3 text-red-400 text-xs">{{ ir.result.error }}</div>
              <p class="text-xs text-gray-400 p-2 truncate">{{ ir.task }}</p>
            </div>
          </div>

          <!-- Chart results -->
          <div v-if="msg.chart_results?.length" class="mt-3 space-y-3">
            <div
              v-for="(cr, ci) in msg.chart_results"
              :key="'chart-'+ci"
              class="rounded-lg overflow-hidden border border-gray-600 bg-white"
            >
              <img
                v-if="cr.result?.url && !cr.result?.error"
                :src="cr.result.url"
                :alt="cr.result.title || cr.task"
                class="w-full max-h-80 object-contain cursor-zoom-in hover:opacity-90 transition-opacity"
                @click="openLightbox(cr.result.url, cr.result.title || cr.task)"
                loading="lazy"
              />
              <div v-else-if="cr.result?.error" class="p-3 text-red-400 text-xs">{{ cr.result.error }}</div>
              <div class="p-2 bg-surface-900">
                <p class="text-xs font-medium text-gray-300 truncate">
                  {{ cr.result?.title || cr.chart_spec?.title || cr.task }}
                </p>
                <p class="text-[10px] text-gray-500">{{ cr.chart_spec?.chart_type || 'chart' }}</p>
              </div>
            </div>
          </div>

          <div class="text-xs text-gray-500 mt-2">{{ formatTime(msg.timestamp) }}</div>
        </div>

        <div v-if="msg.role === 'user'" class="w-8 h-8 rounded-full bg-gray-600 flex items-center justify-center text-sm shrink-0">
          👤
        </div>
      </div>

      <!-- Streaming: live thinking + response -->
      <div v-if="store.isLoading" class="flex gap-3 justify-start">
        <div class="w-8 h-8 rounded-full bg-primary-600 flex items-center justify-center text-sm shrink-0">🤖</div>
        <div class="max-w-[80%] rounded-2xl rounded-bl-md px-4 py-3 bg-surface-800 border border-gray-700/50 text-sm">
          <!-- Live thinking panel -->
          <ThinkingPanel
            :steps="store.thinkSteps"
            :is-streaming="true"
          />

          <!-- Streaming response preview (debounced markdown for performance) -->
          <div
            v-if="store.currentResponse"
            class="prose prose-invert prose-sm max-w-none [&_a]:text-blue-400 [&_a]:underline [&_a]:break-all [&_pre]:bg-surface-900 [&_code]:text-green-300"
            v-html="streamingHtml"
          ></div>

          <!-- Initial loading dots if no response yet -->
          <div v-if="!store.currentResponse && store.thinkSteps.length === 0" class="flex gap-1">
            <span class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay:0ms"></span>
            <span class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay:150ms"></span>
            <span class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay:300ms"></span>
          </div>
        </div>
      </div>
    </div>

    <!-- Input -->
    <div class="px-4 py-2 border-t border-gray-700/50 shrink-0">
      <div class="flex gap-2">
        <div class="flex-1 flex flex-col">
          <textarea
            v-model="input"
            @keydown="handleKeydown"
            placeholder="输入任务，多智能体并行处理... | Shift+Enter 换行，Enter 发送"
            rows="2"
            class="flex-1 bg-surface-800 border border-gray-600 rounded-xl px-4 py-2.5 text-sm text-white
                   placeholder-gray-500 resize-none focus:border-primary-500 focus:outline-none"
            :disabled="store.isLoading"
          ></textarea>
          <div class="text-[10px] text-gray-500 mt-1 flex items-center gap-1">
            <span class="w-1.5 h-1.5 rounded-full bg-green-500"></span>
            联网搜索已启用
          </div>
        </div>
        <button
          v-if="store.isLoading"
          @click="store.stopGeneration()"
          class="px-5 py-2.5 bg-red-600 hover:bg-red-500 text-white rounded-xl font-medium transition-colors shrink-0 flex items-center gap-2"
        >
          <span class="w-2 h-2 bg-white rounded-full"></span>
          停止
        </button>
        <button
          v-else
          @click="send"
          :disabled="!input.trim()"
          class="px-5 py-2.5 bg-primary-600 hover:bg-primary-500 disabled:bg-gray-700 disabled:text-gray-500
                 text-white rounded-xl font-medium transition-colors shrink-0"
        >
          发送
        </button>
      </div>
    </div>

    <!-- Lightbox -->
    <ImageLightbox
      :visible="lightbox.visible"
      :src="lightbox.src"
      :alt="lightbox.alt"
      @close="lightbox.visible = false"
    />
  </div>
</template>
