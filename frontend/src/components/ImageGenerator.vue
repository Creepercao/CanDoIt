<script setup>
import { ref } from 'vue'
import { useChatStore } from '../stores/chat.js'
import ModelSelector from './ModelSelector.vue'
import ImageLightbox from './ImageLightbox.vue'

const store = useChatStore()

const prompt = ref('')
const negativePrompt = ref('')
const width = ref(1024)
const height = ref(1024)
const steps = ref(20)
const generating = ref(false)
const error = ref('')
const lightbox = ref({ visible: false, src: '', alt: '' })

async function generate() {
  if (!prompt.value.trim()) return
  generating.value = true
  error.value = ''
  try {
    await store.doGenerateImage(prompt.value, '', {
      negativePrompt: negativePrompt.value,
      width: width.value,
      height: height.value,
      steps: steps.value,
    })
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
      <h2 class="font-semibold text-lg">🎨 文生图</h2>
    </div>

    <div class="flex-1 overflow-y-auto p-4 space-y-4">
      <ModelSelector model-type="image" />

      <div>
        <label class="text-xs text-gray-400 mb-1 block">正向提示词</label>
        <textarea
          v-model="prompt"
          placeholder="描述你想生成的图像..."
          rows="3"
          class="w-full bg-surface-800 border border-gray-600 rounded-xl px-4 py-2.5 text-sm text-white
                 placeholder-gray-500 resize-none focus:border-primary-500 focus:outline-none"
        ></textarea>
      </div>

      <div>
        <label class="text-xs text-gray-400 mb-1 block">负向提示词 (可选)</label>
        <input
          v-model="negativePrompt"
          placeholder="不想出现的内容..."
          class="w-full bg-surface-800 border border-gray-600 rounded-xl px-4 py-2.5 text-sm text-white
                 placeholder-gray-500 focus:border-primary-500 focus:outline-none"
        />
      </div>

      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="text-xs text-gray-400 mb-1 block">宽度</label>
          <select v-model.number="width" class="w-full bg-surface-800 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white">
            <option :value="512">512</option>
            <option :value="768">768</option>
            <option :value="1024">1024</option>
            <option :value="2048">2048</option>
          </select>
        </div>
        <div>
          <label class="text-xs text-gray-400 mb-1 block">高度</label>
          <select v-model.number="height" class="w-full bg-surface-800 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white">
            <option :value="512">512</option>
            <option :value="768">768</option>
            <option :value="1024">1024</option>
            <option :value="2048">2048</option>
          </select>
        </div>
      </div>

      <div>
        <label class="text-xs text-gray-400 mb-1 block">Steps: {{ steps }}</label>
        <input v-model.number="steps" type="range" min="1" max="50" class="w-full accent-primary-500" />
      </div>

      <button
        @click="generate"
        :disabled="generating || !prompt.trim()"
        class="w-full py-3 bg-purple-600 hover:bg-purple-500 disabled:bg-gray-700 disabled:text-gray-500
               text-white rounded-xl font-medium transition-colors"
      >
        {{ generating ? '生成中...' : '🎨 生成图像' }}
      </button>

      <div v-if="error" class="text-red-400 text-sm bg-red-900/30 rounded-lg p-3">{{ error }}</div>

      <!-- Results gallery -->
      <div v-if="store.imageResults.length" class="space-y-4">
        <h3 class="text-sm font-medium text-gray-300">生成结果</h3>
        <div
          v-for="(item, idx) in store.imageResults.slice().reverse()"
          :key="idx"
          class="bg-surface-800 rounded-xl border border-gray-700/50 overflow-hidden"
        >
          <img
            v-if="item.result?.url"
            :src="item.result.url"
            :alt="item.prompt"
            class="w-full max-h-80 object-contain bg-black/50 cursor-zoom-in hover:opacity-90 transition-opacity"
            @click="openLightbox(item.result.url, item.prompt)"
            loading="lazy"
          />
          <div v-else-if="item.result?.error" class="p-4 text-red-400 text-sm">
            {{ item.result.error }}
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
