<script setup>
import { computed, ref, onMounted, onUnmounted, nextTick } from 'vue'
import { useChatStore } from '../stores/chat.js'

const store = useChatStore()

const props = defineProps({
  modelType: { type: String, required: true },
})

const open = ref(false)
const triggerRef = ref(null)
const panelRef = ref(null)
const collapsedProviders = ref({})

// Dynamic positioning — anchored to the trigger button
const panelStyle = ref({ top: '0px', left: '0px' })

function updatePosition() {
  if (!triggerRef.value) return
  const rect = triggerRef.value.getBoundingClientRect()
  panelStyle.value = {
    top: (rect.bottom + 4) + 'px',
    left: Math.min(rect.left, window.innerWidth - 380) + 'px',
  }
}

function toggleDropdown() {
  open.value = !open.value
  if (open.value) {
    // Collapse all providers by default for cleaner overview
    collapsedProviders.value = {}
    nextTick(() => updatePosition())
  }
}

function closeDropdown() {
  open.value = false
}

function selectModel(id) {
  selected.value = id
  closeDropdown()
}

function toggleProvider(provider) {
  collapsedProviders.value[provider] = !collapsedProviders.value[provider]
  nextTick(() => updatePosition())
}

function handleClickOutside(e) {
  if (!open.value) return
  if (triggerRef.value?.contains(e.target)) return
  if (panelRef.value?.contains(e.target)) return
  closeDropdown()
}

function handleKeydown(e) {
  if (e.key === 'Escape') closeDropdown()
}

onMounted(() => {
  document.addEventListener('click', handleClickOutside, true)
  document.addEventListener('keydown', handleKeydown)
  window.addEventListener('resize', updatePosition)
  window.addEventListener('scroll', updatePosition, true)
})
onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside, true)
  document.removeEventListener('keydown', handleKeydown)
  window.removeEventListener('resize', updatePosition)
  window.removeEventListener('scroll', updatePosition, true)
})

// ── Computed ──────────────────────────────────────────────────────

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
  if (props.modelType === 'chat') return '💬 对话'
  if (props.modelType === 'image') return '🎨 图像'
  return '🎬 视频'
})

const providerGroups = computed(() => {
  const map = {}
  for (const m of models.value) {
    const p = m.provider || 'Unknown'
    if (!map[p]) map[p] = []
    map[p].push(m)
  }
  return Object.keys(map).sort().map(name => ({ name, models: map[name] }))
})

const selectedShortId = computed(() => {
  if (!selected.value) return '未选择'
  const parts = selected.value.split('/')
  return parts[parts.length - 1] || selected.value
})
</script>

<template>
  <div class="relative">
    <!-- Trigger button -->
    <button
      ref="triggerRef"
      @click="toggleDropdown"
      class="flex items-center gap-1.5 bg-surface-800 border border-gray-600 rounded-lg px-2.5 py-1.5 text-sm text-white
             hover:border-gray-500 focus:border-primary-500 focus:outline-none transition-colors min-w-0"
    >
      <span class="text-xs">{{ label }}</span>
      <span class="text-xs text-gray-400 truncate max-w-[140px]">{{ selectedShortId }}</span>
      <span class="text-[10px] text-gray-500 ml-0.5">▾</span>
    </button>

    <!-- Dropdown panel — teleported to body so no parent overflow clips it -->
    <Teleport to="body">
      <div
        v-if="open"
        ref="panelRef"
        class="fixed bg-surface-900 border border-gray-600 rounded-xl shadow-2xl z-[9999] max-h-[70vh] overflow-hidden flex flex-col"
        :style="{ top: panelStyle.top, left: panelStyle.left, width: '380px', maxWidth: '92vw' }"
      >
        <!-- Header -->
        <div class="flex items-center justify-between px-3 py-2 border-b border-gray-700/50 shrink-0">
          <span class="text-xs text-gray-400">
            {{ label }} — {{ models.length }} 个 / {{ providerGroups.length }} 供应商
          </span>
          <button
            @click="store.doRefreshModels()"
            :disabled="store.modelsLoading"
            class="text-xs text-gray-500 hover:text-white px-2 py-0.5 rounded hover:bg-surface-700 transition-colors disabled:opacity-50"
            title="刷新模型列表"
          >
            <span v-if="store.modelsLoading" class="inline-block w-3 h-3 border-2 border-gray-400 border-t-transparent rounded-full animate-spin mr-1 align-middle"></span>
            {{ store.modelsLoading ? '刷新中' : '🔄' }}
          </button>
        </div>

        <!-- Provider groups -->
        <div class="overflow-y-auto flex-1">
          <div v-if="providerGroups.length === 0" class="px-4 py-8 text-center text-sm text-gray-500">
            无可用模型 — 请先在 ⚙️ Providers 中配置
          </div>

          <div v-for="group in providerGroups" :key="group.name">
            <button
              @click="toggleProvider(group.name)"
              class="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-surface-800 transition-colors border-b border-gray-700/20"
            >
              <span class="text-[10px] text-gray-500 transition-transform" :class="{ 'rotate-90': !collapsedProviders[group.name] }">▶</span>
              <span class="text-xs text-gray-300 font-medium truncate flex-1">{{ group.name }}</span>
              <span class="text-[10px] text-gray-600">{{ group.models.length }}</span>
            </button>

            <div v-if="!collapsedProviders[group.name]" class="border-b border-gray-700/10">
              <button
                v-for="m in group.models"
                :key="m.id"
                @click="selectModel(m.id)"
                :class="[
                  'w-full text-left px-6 py-1.5 text-sm transition-colors truncate block',
                  selected === m.id
                    ? 'bg-primary-600/20 text-primary-400 border-l-2 border-primary-500'
                    : 'text-gray-400 hover:text-white hover:bg-surface-800 border-l-2 border-transparent',
                ]"
              >
                {{ m.id }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
