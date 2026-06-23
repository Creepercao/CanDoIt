<script setup>
import { computed } from 'vue'
import { useChatStore } from '../stores/chat.js'

const store = useChatStore()

const activeAgents = computed(() => {
  return Object.entries(store.agentStatus)
    .filter(([, status]) => status === 'running')
    .map(([name]) => name)
})

const agentLabel = (name) => {
  const map = {
    supervisor: '🧠 主管',
    research_worker: '🔍 研究员',
    image_worker: '🎨 画师',
    video_worker: '🎬 视频师',
    code_worker: '💻 程序员',
  }
  return map[name] || name
}
</script>

<template>
  <div v-if="activeAgents.length > 0" class="flex flex-wrap gap-2 mb-3">
    <div
      v-for="agent in activeAgents"
      :key="agent"
      class="flex items-center gap-2 bg-primary-600/20 border border-primary-500/30 rounded-lg px-3 py-1.5 text-sm"
    >
      <span class="w-2 h-2 bg-green-400 rounded-full animate-pulse"></span>
      <span>{{ agentLabel(agent) }}</span>
      <span class="text-gray-400 text-xs">工作中...</span>
    </div>
  </div>
</template>
