<script setup>
import { ref } from 'vue'
import { useChatStore } from '../stores/chat.js'
import ModelSelector from './ModelSelector.vue'
import ImageLightbox from './ImageLightbox.vue'

const store = useChatStore()

const prompt = ref('')
const duration = ref(5)
const generating = ref(false)
const error = ref('')
const lightbox = ref({ visible: false, src: '', alt: '' })

async function generate() {
  if (!prompt.value.trim()) return
  generating.value = true
  error.value = ''
  try {
    await store.doGenerateVideo(prompt.value, '', duration.value)
  } catch (e) {
    error.value = e.message
  } finally {
    generating.value = false
  }
}

function openLightbox(src, alt) {
  lightbox.value = { visible: true, src, alt }
}
</script>

<template>
  <div class="flex flex-col h-full">
    <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700/50 shrink-0">
      <h2 class="font-semibold text-lg">🎬 文生视频</h2>
    </div>

    <div class="flex-1 overflow-y-auto p-4 space-y-4">
      <ModelSelector model-type="video" />

      <div>
        <label class="text-xs text-gray-400 mb-1 block">视频描述</label>
        <textarea
          v-model="prompt"
          placeholder="描述你想生成的视频场景..."
          rows="4"
          class="w-full bg-surface-800 border border-gray-600 rounded-xl px-4 py-2.5 text-sm text-white
                 placeholder-gray-500 resize-none focus:border-primary-500 focus:outline-none"
        ></textarea>
      </div>

      <div>
        <label class="text-xs text-gray-400 mb-1 block">时长: {{ duration }}秒</label>
        <input v-model.number="duration" type="range" min="2" max="15" step="1" class="w-full accent-primary-500" />
      </div>

      <button
        @click="generate"
        :disabled="generating || !prompt.trim()"
        class="w-full py-3 bg-pink-600 hover:bg-pink-500 disabled:bg-gray-700 disabled:text-gray-500
               text-white rounded-xl font-medium transition-colors"
      >
        {{ generating ? '生成中...' : '🎬 生成视频' }}
      </button>

      <div v-if="error" class="text-red-400 text-sm bg-red-900/30 rounded-lg p-3">{{ error }}</div>

      <div v-if="store.videoResults.length" class="space-y-4">
        <h3 class="text-sm font-medium text-gray-300">生成结果</h3>
        <div
          v-for="(item, idx) in store.videoResults.slice().reverse()"
          :key="idx"
          class="bg-surface-800 rounded-xl border border-gray-700/50 overflow-hidden"
        >
          <video
            v-if="item.result?.url"
            :src="item.result.url"
            controls
            class="w-full max-h-80 bg-black"
          ></video>
          <div v-else-if="item.result?.error" class="p-4 text-red-400 text-sm">
            {{ item.result.error }}
          </div>
          <div v-else class="p-4">
            <p class="text-yellow-400 text-sm">
              {{ item.result?.note || '视频生成中，或提供者不支持视频。已生成关键帧作为后备。' }}
            </p>
            <img
              v-if="item.result?.fallback_image?.url"
              :src="item.result.fallback_image.url"
              class="mt-2 rounded-lg max-w-full max-h-48 object-contain bg-black/50 cursor-zoom-in hover:opacity-90"
              @click="openLightbox(item.result.fallback_image.url, item.prompt)"
              loading="lazy"
            />
          </div>
          <div class="p-3">
            <p class="text-xs text-gray-400 truncate">{{ item.prompt }}</p>
          </div>
        </div>
      </div>
    </div>

    <ImageLightbox
      :visible="lightbox.visible"
      :src="lightbox.src"
      :alt="lightbox.alt"
      @close="lightbox.visible = false"
    />
  </div>
</template>
