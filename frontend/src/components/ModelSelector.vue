<script setup>
import { computed } from 'vue'
import { useChatStore } from '../stores/chat.js'

const store = useChatStore()

const props = defineProps({
  modelType: { type: String, required: true }, // chat | image | video
})

const models = computed(() => {
  if (props.modelType === 'chat') return store.chatModels
  if (props.modelType === 'image') return store.imageModels
  return store.videoModels
})

const selected = computed({
  get() {
    if (props.modelType === 'chat') return store.selectedChatModel
    if (props.modelType === 'image') return store.selectedImageModel
    return store.selectedVideoModel
  },
  set(val) {
    if (props.modelType === 'chat') store.selectedChatModel = val
    else if (props.modelType === 'image') store.selectedImageModel = val
    else store.selectedVideoModel = val
  },
})

const label = computed(() => {
  if (props.modelType === 'chat') return '💬 对话模型'
  if (props.modelType === 'image') return '🎨 图像模型'
  return '🎬 视频模型'
})

const icon = computed(() => {
  if (props.modelType === 'chat') return '💬'
  if (props.modelType === 'image') return '🎨'
  return '🎬'
})
</script>

<template>
  <div class="flex items-center gap-2">
    <label class="text-xs text-gray-400 whitespace-nowrap">{{ icon }}</label>
    <select
      v-model="selected"
      class="bg-surface-800 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-white
             focus:border-primary-500 focus:outline-none cursor-pointer min-w-0 flex-1"
    >
      <option v-if="models.length === 0" value="" disabled>
        无可用模型
      </option>
      <option
        v-for="m in models"
        :key="m.id"
        :value="m.id"
        class="truncate"
      >
        {{ m.id }}
      </option>
    </select>
    <button
      @click="store.doRefreshModels()"
      class="text-gray-500 hover:text-white text-xs px-2 py-1 rounded hover:bg-surface-700 transition-colors"
      title="刷新模型列表"
    >
      🔄
    </button>
  </div>
</template>
