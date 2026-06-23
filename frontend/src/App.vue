<script setup>
import { onMounted, watch } from 'vue'
import { useChatStore } from './stores/chat.js'
import ChatPanel from './components/ChatPanel.vue'
import ImageGenerator from './components/ImageGenerator.vue'
import VideoGenerator from './components/VideoGenerator.vue'
import ModelSelector from './components/ModelSelector.vue'

const store = useChatStore()

onMounted(async () => {
  await Promise.all([store.loadModels(), store.loadSkills()])
})

const tabs = [
  { key: 'chat', label: '💬 对话', component: ChatPanel },
  { key: 'image', label: '🎨 文生图', component: ImageGenerator },
  { key: 'video', label: '🎬 文生视频', component: VideoGenerator },
]
</script>

<template>
  <div class="flex h-screen bg-gray-950 overflow-hidden">
    <!-- Sidebar -->
    <aside class="w-16 lg:w-64 bg-surface-900 border-r border-gray-700/50 flex flex-col shrink-0">
      <!-- Logo -->
      <div class="px-4 py-4 border-b border-gray-700/50">
        <div class="flex items-center gap-3">
          <span class="text-2xl">🤖</span>
          <span class="hidden lg:block font-bold text-lg">MultiAgent</span>
        </div>
        <p class="hidden lg:block text-xs text-gray-500 mt-1">多智能体协作平台</p>
      </div>

      <!-- Tabs -->
      <nav class="flex-1 py-4 space-y-1 px-2">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          @click="store.activeTab = tab.key"
          :class="[
            'flex items-center gap-3 w-full px-3 py-2.5 rounded-xl text-sm font-medium transition-colors',
            store.activeTab === tab.key
              ? 'bg-primary-600/20 text-primary-400 border border-primary-500/30'
              : 'text-gray-400 hover:text-white hover:bg-surface-800',
          ]"
        >
          <span class="text-lg">{{ tab.label.slice(0, 2) }}</span>
          <span class="hidden lg:block">{{ tab.label.slice(3) }}</span>
        </button>
      </nav>

      <!-- Model info footer -->
      <div class="hidden lg:block px-4 py-3 border-t border-gray-700/50 space-y-2 text-xs text-gray-500">
        <div class="flex items-center justify-between">
          <span>对话</span>
          <span class="text-gray-400 truncate ml-2 max-w-[120px]">{{ store.selectedChatModel || '未选择' }}</span>
        </div>
        <div class="flex items-center justify-between">
          <span>图像</span>
          <span class="text-gray-400 truncate ml-2 max-w-[120px]">{{ store.selectedImageModel || '未选择' }}</span>
        </div>
        <div class="flex items-center justify-between">
          <span>视频</span>
          <span class="text-gray-400 truncate ml-2 max-w-[120px]">{{ store.selectedVideoModel || '未选择' }}</span>
        </div>
      </div>
    </aside>

    <!-- Main content -->
    <main class="flex-1 flex flex-col min-w-0">
      <!-- Top bar with model selectors -->
      <header class="px-4 py-2 border-b border-gray-700/50 bg-surface-900/50 flex items-center gap-4 shrink-0 overflow-x-auto">
        <div class="flex items-center gap-4 flex-1 min-w-0">
          <ModelSelector model-type="chat" class="min-w-[180px] max-w-[300px]" />
          <div class="hidden md:flex items-center gap-1 text-gray-600 text-xs">
            <span class="w-1 h-1 rounded-full bg-green-500"></span>
            <span v-if="store.chatModels.length">{{ store.chatModels.length }} 对话模型</span>
            <span v-else class="text-yellow-500">加载中...</span>
          </div>
          <div class="hidden md:flex items-center gap-1 text-gray-600 text-xs">
            <span class="w-1 h-1 rounded-full bg-purple-500"></span>
            <span>{{ store.imageModels.length }} 图像模型</span>
          </div>
          <div class="hidden md:flex items-center gap-1 text-gray-600 text-xs">
            <span class="w-1 h-1 rounded-full bg-pink-500"></span>
            <span>{{ store.videoModels.length }} 视频模型</span>
          </div>
        </div>
        <button
          @click="store.doRefreshModels()"
          class="text-xs text-gray-500 hover:text-white px-3 py-1 rounded-lg hover:bg-surface-700 transition-colors shrink-0"
          title="刷新模型"
        >
          🔄 刷新
        </button>
      </header>

      <!-- Tab content -->
      <div class="flex-1 min-h-0">
        <KeepAlive>
          <ChatPanel v-if="store.activeTab === 'chat'" key="chat" />
          <ImageGenerator v-else-if="store.activeTab === 'image'" key="image" />
          <VideoGenerator v-else-if="store.activeTab === 'video'" key="video" />
        </KeepAlive>
      </div>
    </main>
  </div>
</template>
