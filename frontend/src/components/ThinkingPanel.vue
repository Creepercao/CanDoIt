<script setup>
import { ref, computed, watch } from 'vue'
import { useChatStore } from '../stores/chat.js'

const props = defineProps({
  steps: { type: Array, default: () => [] },   // [{type, label, task, timestamp}]
  isStreaming: { type: Boolean, default: false },
})

const store = useChatStore()

const expanded = ref(true)
const userCollapsed = ref(false)

// Auto-expand when streaming, respect user choice when done
watch(() => props.isStreaming, (val) => {
  if (val) {
    expanded.value = true
    userCollapsed.value = false
  } else if (!userCollapsed.value) {
    // Auto-collapse 1.5s after done
    setTimeout(() => {
      if (!userCollapsed.value) {
        expanded.value = false
      }
    }, 1500)
  }
})

function toggle() {
  expanded.value = !expanded.value
  if (!props.isStreaming) {
    userCollapsed.value = !expanded.value
  }
}

const phaseIcon = (step) => {
  if (step.type === 'phase') return step.phase === 'supervisor' ? '🧠' : '📝'
  if (step.type === 'plan') return '📋'
  if (step.type === 'agent_start') return '▶️'
  if (step.type === 'agent_done') return '✅'
  return '•'
}

const headerSummary = computed(() => {
  if (props.steps.length === 0) return ''
  const agentSteps = props.steps.filter(s => s.type === 'agent_done')
  const planStep = props.steps.find(s => s.type === 'plan')
  if (planStep) {
    const labels = planStep.tasks?.map(t => {
      return store.agentEmojiMap[t.agent] || t.agent
    }).join(' → ') || ''
    return `🧠 分析 → ${labels} → ✅ 完成`
  }
  return `已调用 ${agentSteps.length} 个智能体`
})
</script>

<template>
  <div v-if="steps.length > 0" class="mb-3">
    <!-- Collapsed summary -->
    <div
      v-if="!expanded"
      @click="toggle"
      class="flex items-center gap-2 text-xs text-gray-400 cursor-pointer hover:text-gray-300
             bg-surface-800/50 rounded-lg px-3 py-2 border border-gray-700/30 transition-colors"
    >
      <span>{{ isStreaming ? '⏳' : '✅' }}</span>
      <span>{{ headerSummary }}</span>
      <span class="text-gray-600 ml-auto">展开 ▸</span>
    </div>

    <!-- Expanded panel -->
    <div
      v-else
      class="bg-surface-800/80 border border-gray-700/50 rounded-xl overflow-hidden text-sm"
    >
      <!-- Header bar -->
      <div
        @click="toggle"
        class="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-surface-700/50 transition-colors border-b border-gray-700/30"
      >
        <span v-if="isStreaming" class="w-2 h-2 bg-green-400 rounded-full animate-pulse"></span>
        <span v-else>✅</span>
        <span class="text-xs font-medium text-gray-300">{{ headerSummary }}</span>
        <span class="text-gray-600 ml-auto text-xs">收起 ▾</span>
      </div>

      <!-- Timeline -->
      <div class="px-3 py-2 space-y-1.5 max-h-64 overflow-y-auto">
        <div
          v-for="(step, idx) in steps"
          :key="idx"
          class="flex items-start gap-2 text-xs"
          :class="{
            'text-gray-400': step.type === 'agent_done',
            'text-primary-400': step.type === 'phase' || step.type === 'plan',
            'text-green-400': step.type === 'agent_start',
          }"
        >
          <span class="shrink-0 mt-0.5">{{ phaseIcon(step) }}</span>
          <div class="min-w-0">
            <template v-if="step.type === 'phase'">
              <span>{{ step.message }}</span>
            </template>
            <template v-else-if="step.type === 'plan'">
              <span>创建 {{ step.count }} 个任务</span>
            </template>
            <template v-else-if="step.type === 'agent_start'">
              <span>{{ step.label }}: </span>
              <span class="text-gray-500 truncate">{{ step.task }}</span>
              <span v-if="isStreaming && !steps.some(s => s.type === 'agent_done' && s.agent === step.agent)"
                    class="text-gray-600"> ...</span>
            </template>
            <template v-else-if="step.type === 'agent_done'">
              <span>{{ step.label }} 完成</span>
            </template>
          </div>
          <span class="text-gray-600 shrink-0 ml-auto text-[10px]">{{ step.timestamp }}</span>
        </div>

        <!-- Spinner when streaming -->
        <div v-if="isStreaming" class="flex items-center gap-2 text-xs text-gray-500 pt-1">
          <span class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay:0ms"></span>
          <span class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay:150ms"></span>
          <span class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay:300ms"></span>
        </div>
      </div>
    </div>
  </div>
</template>
