<script setup>
import { ref, computed, watch } from 'vue'

const props = defineProps({
  steps: { type: Array, default: () => [] },
  isStreaming: { type: Boolean, default: false },
})

const expanded = ref(true)
const userCollapsed = ref(false)

watch(() => props.isStreaming, (val) => {
  if (val) {
    expanded.value = true
    userCollapsed.value = false
  } else if (!userCollapsed.value) {
    setTimeout(() => {
      if (!userCollapsed.value) expanded.value = false
    }, 1800)
  }
})

function toggle() {
  expanded.value = !expanded.value
  if (!props.isStreaming) userCollapsed.value = !expanded.value
}

const planStep = computed(() => props.steps.find(s => s.type === 'plan'))
const phaseSteps = computed(() => props.steps.filter(s => s.type === 'phase'))
const agentSteps = computed(() => props.steps.filter(s => s.type === 'agent_start' || s.type === 'agent_done'))
const activeAgents = computed(() => agentSteps.value.filter(s => s.type === 'agent_start' && !s.done))
const doneAgents = computed(() => agentSteps.value.filter(s => s.done || s.type === 'agent_done'))
const progress = computed(() => {
  const total = planStep.value?.count || agentSteps.value.length || 0
  const done = doneAgents.value.length
  return { total, done, pct: total ? Math.min(100, Math.round((done / total) * 100)) : 0 }
})

const headerSummary = computed(() => {
  if (!props.steps.length) return ''
  if (props.isStreaming) {
    if (activeAgents.value.length) return `${activeAgents.value.length} 个智能体运行中`
    return '正在规划任务'
  }
  return `已完成 ${progress.value.done}/${progress.value.total || progress.value.done} 个智能体任务`
})

function statusClass(step) {
  if (step.done || step.type === 'agent_done') return 'border-green-500/30 bg-green-500/10'
  return 'border-blue-500/30 bg-blue-500/10'
}
</script>

<template>
  <div v-if="steps.length > 0" class="mb-3">
    <button
      v-if="!expanded"
      @click="toggle"
      class="w-full flex items-center gap-2 text-xs text-gray-400 hover:text-gray-200
             bg-surface-800/70 rounded-lg px-3 py-2 border border-gray-700/40 transition-colors"
    >
      <span v-if="isStreaming" class="w-2 h-2 rounded-full bg-green-400 animate-pulse"></span>
      <span v-else class="w-2 h-2 rounded-full bg-green-500"></span>
      <span class="font-medium">{{ headerSummary }}</span>
      <span class="ml-auto text-gray-500">展开</span>
    </button>

    <div v-else class="bg-surface-800/85 border border-gray-700/50 rounded-xl overflow-hidden">
      <button
        @click="toggle"
        class="w-full flex items-center gap-3 px-3 py-2.5 hover:bg-surface-700/40 transition-colors border-b border-gray-700/40"
      >
        <span v-if="isStreaming" class="w-2 h-2 rounded-full bg-green-400 animate-pulse"></span>
        <span v-else class="w-2 h-2 rounded-full bg-green-500"></span>
        <div class="min-w-0 flex-1 text-left">
          <div class="text-xs font-medium text-gray-200">{{ headerSummary }}</div>
          <div class="mt-1 h-1.5 rounded-full bg-surface-900 overflow-hidden">
            <div
              class="h-full rounded-full bg-primary-500 transition-all duration-300"
              :style="{ width: `${progress.pct}%` }"
            ></div>
          </div>
        </div>
        <span class="text-xs text-gray-500">收起</span>
      </button>

      <div class="p-3 space-y-3">
        <div v-if="planStep" class="rounded-lg border border-gray-700/50 bg-surface-900/60 p-2.5">
          <div class="flex items-center justify-between text-xs">
            <span class="font-medium text-gray-200">执行计划</span>
            <span class="text-gray-500">{{ planStep.count }} 个任务</span>
          </div>
          <div class="mt-2 space-y-1.5">
            <div
              v-for="(task, idx) in planStep.tasks"
              :key="idx"
              class="grid grid-cols-[1.5rem_7rem_1fr] items-start gap-2 text-xs"
            >
              <span class="text-gray-500 tabular-nums">{{ idx + 1 }}</span>
              <span class="text-primary-300 truncate">{{ task.label || task.agent }}</span>
              <span class="text-gray-400 line-clamp-1">{{ task.prompt }}</span>
            </div>
          </div>
        </div>

        <div class="space-y-2">
          <div
            v-for="(step, idx) in agentSteps"
            :key="`${step.agent}-${idx}`"
            class="rounded-lg border p-2.5 transition-colors"
            :class="statusClass(step)"
          >
            <div class="flex items-center gap-2">
              <span
                class="w-2 h-2 rounded-full"
                :class="step.done || step.type === 'agent_done' ? 'bg-green-400' : 'bg-blue-400 animate-pulse'"
              ></span>
              <span class="text-xs font-medium text-gray-200 truncate">{{ step.label }}</span>
              <span class="ml-auto text-[10px] text-gray-500">{{ step.doneAt || step.timestamp }}</span>
            </div>
            <div v-if="step.task" class="mt-1 text-xs text-gray-400 line-clamp-2">
              {{ step.task }}
            </div>
            <div v-if="step.summary" class="mt-1 text-xs text-green-300">
              {{ step.summary }}
            </div>
          </div>
        </div>

        <div v-if="phaseSteps.length" class="flex flex-wrap gap-1.5">
          <span
            v-for="(step, idx) in phaseSteps"
            :key="idx"
            class="text-[10px] text-gray-400 bg-surface-900 border border-gray-700/40 rounded px-2 py-1"
          >
            {{ step.message }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>
