<script setup>
import { ref, computed, watch } from 'vue'
import { useChatStore } from '../stores/chat.js'

const props = defineProps({
  steps: { type: Array, default: () => [] },
  isStreaming: { type: Boolean, default: false },
})

const store = useChatStore()

const expanded = ref(true)
const userCollapsed = ref(false)
const showFindings = ref({})

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

// ── Safety layer steps ──
const reviewSteps = computed(() => props.steps.filter(s => s.type === 'safety_review'))
const approvalSteps = computed(() => props.steps.filter(s => s.type === 'approval'))
const pendingApprovals = computed(() => approvalSteps.value.filter(s => s.status === 'pending'))
const remediationSteps = computed(() => props.steps.filter(s => s.type === 'remediation'))
const budgetSteps = computed(() => props.steps.filter(s => s.type === 'budget'))
const verification = computed(() => {
  const list = props.steps.filter(s => s.type === 'verification')
  return list.length ? list[list.length - 1].verdict : null
})
const blockedAgents = computed(() => agentSteps.value.filter(s => s.blocked))
const notableReviews = computed(() =>
  reviewSteps.value.filter(s => s.decision !== 'allow' || s.risk !== 'low')
)

// 🆕 Plan Mode — compute step status from agent execution
const planStepStatus = (stepAgent) => {
  const done = doneAgents.value.some(a =>
    (a.agentType || a.agent) === stepAgent || a.agent === stepAgent
  )
  const active = activeAgents.value.some(a =>
    (a.agentType || a.agent) === stepAgent || a.agent === stepAgent
  )
  if (done) return 'done'
  if (active) return 'running'
  return 'pending'
}

const progress = computed(() => {
  const total = planStep.value?.count || agentSteps.value.length || 0
  const done = doneAgents.value.length
  return { total, done, pct: total ? Math.min(100, Math.round((done / total) * 100)) : 0 }
})

const headerSummary = computed(() => {
  if (!props.steps.length) return ''
  if (props.isStreaming) {
    if (pendingApprovals.value.length) return `⏸ 等待人工审批（${pendingApprovals.value.length}）`
    if (activeAgents.value.length) return `${activeAgents.value.length} 个智能体运行中`
    return '正在规划任务'
  }
  const parts = [`已完成 ${progress.value.done}/${progress.value.total || progress.value.done} 个智能体任务`]
  if (verification.value) {
    const map = { complete: '✅ 目标已完成', partial: '⚠️ 目标部分完成', incomplete: '❌ 目标未完成', failed: '❌ 执行失败' }
    parts.push(map[verification.value.status] || verification.value.status)
  }
  if (blockedAgents.value.length) parts.push(`⛔ 拦截 ${blockedAgents.value.length} 步`)
  return parts.join(' · ')
})

function statusClass(step) {
  if (step.blocked) return 'border-red-500/40 bg-red-500/10'
  if (step.done || step.type === 'agent_done') return 'border-green-500/30 bg-green-500/10'
  return 'border-blue-500/30 bg-blue-500/10'
}

// 🆕 Plan step status styles
function planStepStatusClass(status) {
  if (status === 'done') return 'border-green-500/50 bg-green-500/10 text-green-300'
  if (status === 'running') return 'border-blue-400/50 bg-blue-500/15 text-blue-300 ring-1 ring-blue-400/30'
  return 'border-gray-600/30 bg-surface-900 text-gray-400'
}

// ── Safety helpers ──
const RISK_STYLE = {
  low: 'bg-gray-600/30 text-gray-300 border-gray-500/30',
  medium: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  high: 'bg-orange-500/15 text-orange-300 border-orange-500/30',
  critical: 'bg-red-500/15 text-red-300 border-red-500/40',
}
const DECISION_STYLE = {
  allow: 'bg-green-500/15 text-green-300 border-green-500/30',
  notify: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  review: 'bg-orange-500/15 text-orange-300 border-orange-500/30',
  block: 'bg-red-500/15 text-red-300 border-red-500/40',
}
const DECISION_LABEL = {
  allow: '通过', notify: '通过(提示)', review: '待审批', block: '拦截',
}
const GATE_LABEL = {
  plan: '执行前·计划审查',
  action: '执行中·动作门禁',
  output: '执行后·输出审查',
}
const STATUS_LABEL = {
  complete: '✅ 目标已完成',
  partial: '⚠️ 目标部分完成',
  incomplete: '❌ 目标未完成',
  failed: '❌ 执行失败',
}

function riskStyle(risk) {
  return RISK_STYLE[risk] || RISK_STYLE.low
}
function decisionStyle(decision) {
  return DECISION_STYLE[decision] || DECISION_STYLE.allow
}
function decisionLabel(decision) {
  return DECISION_LABEL[decision] || decision
}
function gateLabel(gate) {
  return GATE_LABEL[gate] || gate
}
function statusLabel(status) {
  return STATUS_LABEL[status] || status
}
function criteriaStyle(status) {
  if (status === 'met') return 'text-green-300'
  if (status === 'partial') return 'text-amber-300'
  return 'text-red-300'
}
function criteriaIcon(status) {
  if (status === 'met') return '✓'
  if (status === 'partial') return '◐'
  return '✗'
}
function toggleFindings(key) {
  showFindings.value[key] = !showFindings.value[key]
}
async function approve(item) {
  await store.doDecideApproval(item.id, true)
}
async function reject(item) {
  await store.doDecideApproval(item.id, false, '用户拒绝')
}
</script>

<template>
  <div v-if="steps.length > 0" class="mb-3">
    <button
      v-if="!expanded"
      @click="toggle"
      class="w-full flex items-center gap-2 text-xs text-gray-400 hover:text-gray-200
             bg-surface-800 rounded-lg px-3 py-2 border border-gray-700/40 transition-colors"
    >
      <span v-if="isStreaming" class="w-2 h-2 rounded-full bg-green-400 animate-pulse"></span>
      <span v-else class="w-2 h-2 rounded-full bg-green-500"></span>
      <span class="font-medium">{{ headerSummary }}</span>
      <span class="ml-auto text-gray-500">展开</span>
    </button>

    <div v-else class="bg-surface-800 border border-gray-700/50 rounded-xl overflow-hidden">
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
        <!-- ── Pending approvals (human-in-the-loop) ── -->
        <div
          v-if="pendingApprovals.length"
          class="rounded-lg border border-orange-500/50 bg-orange-500/10 p-2.5 space-y-2"
        >
          <div class="text-xs font-medium text-orange-200">
            ⏸ 执行已暂停，等待人工审批（{{ pendingApprovals.length }}）
          </div>
          <div
            v-for="item in pendingApprovals"
            :key="item.id"
            class="rounded-lg border border-orange-500/30 bg-surface-900 p-2"
          >
            <div class="flex items-center gap-2 mb-1">
              <span class="text-[10px] px-1.5 py-0.5 rounded border" :class="riskStyle(item.risk)">
                {{ item.risk }}
              </span>
              <span class="text-xs text-gray-200 truncate">{{ item.summary }}</span>
            </div>
            <div v-if="item.agent" class="text-[11px] text-gray-400">
              代理: {{ item.agent }}<span v-if="item.stage"> · 阶段: {{ gateLabel(item.stage) }}</span>
            </div>
            <div v-if="item.task" class="text-[11px] text-gray-500 line-clamp-2 mt-0.5">{{ item.task }}</div>
            <ul v-if="item.reasons?.length" class="mt-1 space-y-0.5">
              <li v-for="(r, ri) in item.reasons.slice(0, 4)" :key="ri" class="text-[10px] text-orange-200/80">
                · {{ r }}
              </li>
            </ul>
            <div class="flex gap-2 mt-2">
              <button
                @click="approve(item)"
                class="text-xs px-3 py-1 rounded-lg bg-green-700 hover:bg-green-600 text-white transition-colors"
              >
                批准执行
              </button>
              <button
                @click="reject(item)"
                class="text-xs px-3 py-1 rounded-lg bg-red-700 hover:bg-red-600 text-white transition-colors"
              >
                拒绝
              </button>
            </div>
          </div>
        </div>

        <!-- ── Goal completion confirmation ── -->
        <div
          v-if="verification"
          class="rounded-lg border p-2.5"
          :class="verification.status === 'complete'
            ? 'border-green-500/40 bg-green-500/10'
            : 'border-amber-500/40 bg-amber-500/10'"
        >
          <div class="flex items-center justify-between text-xs mb-1.5">
            <span class="font-medium text-gray-100">
              🎯 目标完成确认 · {{ statusLabel(verification.status) }}
            </span>
            <span class="text-gray-400 tabular-nums">
              得分 {{ Math.round((verification.score || 0) * 100) }}%
            </span>
          </div>
          <div class="text-[11px] text-gray-300">{{ verification.summary }}</div>

          <div v-if="verification.criteria?.length" class="mt-2 space-y-1">
            <div
              v-for="(c, ci) in verification.criteria"
              :key="ci"
              class="flex items-start gap-1.5 text-[11px]"
            >
              <span class="flex-shrink-0" :class="criteriaStyle(c.status)">{{ criteriaIcon(c.status) }}</span>
              <span class="flex-1 min-w-0 text-gray-300">
                {{ c.label }}
                <span class="text-gray-500">— {{ c.evidence }}</span>
              </span>
            </div>
          </div>

          <div v-if="verification.llm" class="mt-2 text-[10px] text-gray-400">
            模型复核：{{ verification.llm.complete ? '确认完成' : '认为未完成' }}
            (置信度 {{ verification.llm.confidence }})
            <span v-if="verification.llm.reason">— {{ verification.llm.reason }}</span>
            <div v-if="verification.llm.missing?.length" class="mt-1 text-amber-300">
              缺失：{{ verification.llm.missing.join('；') }}
            </div>
            <div v-if="verification.llm.unverified_claims?.length" class="mt-1 text-amber-300">
              无法证实：{{ verification.llm.unverified_claims.join('；') }}
            </div>
          </div>
          <div v-else-if="verification.llm_error" class="mt-2 text-[10px] text-gray-500">
            模型复核不可用（{{ verification.llm_error }}），以上为产出物核验结果
          </div>
          <div class="mt-1.5 flex flex-wrap gap-1">
            <span
              v-for="(by, bi) in verification.checked_by || []"
              :key="bi"
              class="text-[10px] px-1.5 py-0.5 rounded bg-surface-900 border border-gray-700/40 text-gray-400"
            >
              {{ by === 'llm' ? '模型复核' : '产出物核验' }}
            </span>
          </div>
        </div>

        <!-- ── Remediation rounds ── -->
        <div
          v-if="remediationSteps.length"
          class="rounded-lg border border-blue-500/40 bg-blue-500/10 p-2.5"
        >
          <div class="text-xs font-medium text-blue-200 mb-1">🔁 自动补救轮次</div>
          <div v-for="(r, ri) in remediationSteps" :key="ri" class="text-[11px] text-gray-300">
            第 {{ r.round }} 轮：{{ (r.tasks || []).map(t => t.agent).join('、') }}
          </div>
        </div>

        <!-- ── Budget guard ── -->
        <div
          v-if="budgetSteps.length"
          class="rounded-lg border border-red-500/40 bg-red-500/10 p-2.5"
        >
          <div class="text-xs font-medium text-red-200 mb-1">🛑 执行预算上限</div>
          <div v-for="(b, bi) in budgetSteps" :key="bi" class="text-[11px] text-gray-300">
            {{ b.reason }}
          </div>
        </div>

        <!-- ── Execution review findings ── -->
        <div
          v-if="notableReviews.length"
          class="rounded-lg border border-gray-700/50 bg-surface-900 p-2.5"
        >
          <div class="flex items-center justify-between text-xs mb-2">
            <span class="font-medium text-gray-200">🛡️ 执行审查</span>
            <span class="text-gray-500">{{ notableReviews.length }} 条</span>
          </div>
          <div class="space-y-1.5">
            <div
              v-for="(r, ri) in notableReviews"
              :key="ri"
              class="rounded-lg border p-2"
              :class="decisionStyle(r.decision)"
            >
              <div class="flex items-center gap-1.5 flex-wrap">
                <span class="text-[10px] px-1.5 py-0.5 rounded border" :class="riskStyle(r.risk)">
                  {{ r.risk }}
                </span>
                <span class="text-[10px] text-gray-300">{{ gateLabel(r.gate) }}</span>
                <span class="text-[10px] font-medium">{{ decisionLabel(r.decision) }}</span>
                <button
                  v-if="r.findings?.length"
                  @click="toggleFindings(`${ri}-${r.gate}`)"
                  class="ml-auto text-[10px] text-gray-400 hover:text-gray-200"
                >
                  {{ showFindings[`${ri}-${r.gate}`] ? '收起' : `依据 ${r.findings.length}` }}
                </button>
              </div>
              <div class="text-[11px] mt-1 opacity-90">{{ r.summary }}</div>
              <ul v-if="r.reasons?.length" class="mt-1 space-y-0.5">
                <li v-for="(reason, xi) in r.reasons.slice(0, showFindings[`${ri}-${r.gate}`] ? 12 : 2)"
                    :key="xi" class="text-[10px] opacity-80">
                  · {{ reason }}
                </li>
              </ul>
            </div>
          </div>
        </div>

        <div v-if="planStep" class="rounded-lg border border-gray-700/50 bg-surface-900 p-2.5">
          <div class="flex items-center justify-between text-xs mb-2">
            <span class="font-medium text-gray-200">📋 执行计划</span>
            <span class="text-gray-500">{{ planStep.count }} 个任务</span>
          </div>

          <div v-if="planStep.dropped?.length" class="mb-2 text-[10px] text-amber-300">
            已丢弃未注册代理：{{ planStep.dropped.map(d => d.agent).join('、') }}
          </div>

          <!-- 🆕 Plan Mode: step-by-step with deps & reasons -->
          <div v-if="planStep.steps && planStep.steps.length > 0" class="space-y-1.5">
            <div
              v-for="(step, idx) in planStep.steps"
              :key="idx"
              class="rounded-lg border p-2 transition-all duration-300"
              :class="planStepStatusClass(planStepStatus(step.agent))"
            >
              <div class="flex items-start gap-2">
                <!-- Step number + status dot -->
                <div class="flex items-center gap-1.5 min-w-[2.5rem]">
                  <span
                    class="w-2 h-2 rounded-full flex-shrink-0"
                    :class="{
                      'bg-green-400': planStepStatus(step.agent) === 'done',
                      'bg-blue-400 animate-pulse': planStepStatus(step.agent) === 'running',
                      'bg-gray-500': planStepStatus(step.agent) === 'pending',
                    }"
                  ></span>
                  <span class="text-xs tabular-nums" :class="{
                    'text-green-400': planStepStatus(step.agent) === 'done',
                    'text-blue-300': planStepStatus(step.agent) === 'running',
                    'text-gray-500': planStepStatus(step.agent) === 'pending',
                  }">{{ step.step || idx + 1 }}</span>
                </div>

                <!-- Content -->
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-1.5">
                    <span class="text-xs font-medium truncate">{{ step.label || step.agent }}</span>
                    <!-- Dependency badges -->
                    <span
                      v-if="step.depends_on && step.depends_on.length > 0"
                      class="text-[10px] text-gray-500"
                    >
                      ← 步骤 {{ step.depends_on.join(', ') }}
                    </span>
                  </div>
                  <div class="text-[11px] line-clamp-1 mt-0.5 opacity-70">{{ step.prompt }}</div>
                  <div v-if="step.reason" class="text-[10px] text-gray-500 mt-0.5 italic">
                    {{ step.reason }}
                  </div>
                </div>

                <!-- Status tag -->
                <span class="text-[10px] flex-shrink-0 px-1.5 py-0.5 rounded" :class="{
                  'bg-green-500/20 text-green-300': planStepStatus(step.agent) === 'done',
                  'bg-blue-500/20 text-blue-300': planStepStatus(step.agent) === 'running',
                  'bg-gray-600/30 text-gray-500': planStepStatus(step.agent) === 'pending',
                }">
                  {{ planStepStatus(step.agent) === 'done' ? '✓' : planStepStatus(step.agent) === 'running' ? '⚡' : '○' }}
                </span>
              </div>
            </div>
          </div>

          <!-- Fallback: legacy task list (no steps) -->
          <div v-else class="space-y-1.5">
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
                :class="step.blocked
                  ? 'bg-red-400'
                  : (step.done || step.type === 'agent_done' ? 'bg-green-400' : 'bg-blue-400 animate-pulse')"
              ></span>
              <span class="text-xs font-medium text-gray-200 truncate">{{ step.label }}</span>
              <span v-if="step.blocked" class="text-[10px] px-1.5 py-0.5 rounded bg-red-500/20 text-red-300">
                已拦截
              </span>
              <span class="ml-auto text-[10px] text-gray-500">{{ step.doneAt || step.timestamp }}</span>
            </div>
            <div v-if="step.task" class="mt-1 text-xs text-gray-400 line-clamp-2">
              {{ step.task }}
            </div>
            <div v-if="step.summary" class="mt-1 text-xs" :class="step.blocked ? 'text-red-300' : 'text-green-300'">
              {{ step.summary }}
            </div>
            <div v-if="step.review" class="mt-1 text-[10px] text-amber-300">
              🛡️ 输出审查：{{ step.review.summary }}
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
