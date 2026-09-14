<script setup>
import { ref, computed, onMounted } from 'vue'
import { useChatStore } from '../stores/chat.js'

const store = useChatStore()

const activeSection = ref('overview')   // overview | approvals | audit | rules | probe
const probeText = ref('')
const probeStage = ref('action')
const probeAgent = ref('code')
const chainResult = ref(null)
const busy = ref(false)
const message = ref('')

const settings = computed(() => store.safetySettings || {})
const status = computed(() => store.safetyStatus || {})
const approvals = computed(() => store.pendingApprovals || [])
const records = computed(() => store.auditRecords || [])
const policyData = computed(() => store.safetyPolicy || {})
const probe = computed(() => store.reviewProbe)

const MODE_OPTIONS = [
  { value: 'enforce', label: '强制 (enforce)', hint: '高风险动作会停下等待审批，严重风险直接拦截' },
  { value: 'monitor', label: '观察 (monitor)', hint: '只记录不拦截，用于灰度上线' },
  { value: 'off', label: '关闭 (off)', hint: '完全关闭安全层，不记录也不干预' },
]
const NONSTREAM_OPTIONS = [
  { value: 'allow', label: '允许', hint: '非流式（无界面）运行时放行需审批的动作' },
  { value: 'deny', label: '拒绝', hint: '非流式运行时一律拒绝需审批的动作（最严格）' },
  { value: 'review', label: '同 allow', hint: '兼容值' },
]
const EVENT_OPTIONS = [
  '', 'review.plan', 'review.action', 'review.output', 'verification.goal',
  'approval.requested', 'approval.resolved', 'approval.decided',
  'guard.output_redacted', 'guard.untrusted_content', 'guard.budget_exceeded',
  'verification.remediation', 'run.start', 'run.finish', 'run.delivered',
  'settings.updated', 'system.startup',
]
const SEVERITY_OPTIONS = ['', 'info', 'warning', 'critical']

const SEVERITY_STYLE = {
  info: 'bg-gray-600/30 text-gray-300',
  warning: 'bg-amber-500/20 text-amber-300',
  critical: 'bg-red-500/20 text-red-300',
}
const DECISION_STYLE = {
  allow: 'bg-green-500/20 text-green-300',
  notify: 'bg-amber-500/20 text-amber-300',
  review: 'bg-orange-500/20 text-orange-300',
  block: 'bg-red-500/20 text-red-300',
}
const RISK_STYLE = {
  low: 'bg-gray-600/30 text-gray-300',
  medium: 'bg-amber-500/20 text-amber-300',
  high: 'bg-orange-500/20 text-orange-300',
  critical: 'bg-red-500/20 text-red-300',
}

function fmtTime(ts) {
  if (!ts) return '-'
  try {
    return new Date(ts).toLocaleString('zh-CN', { hour12: false })
  } catch { return ts }
}

async function setSetting(key, value) {
  busy.value = true
  message.value = ''
  try {
    await store.doUpdateSafetySettings({ [key]: value })
    message.value = '已保存'
  } catch (e) {
    message.value = `保存失败: ${e.message}`
  } finally {
    busy.value = false
    setTimeout(() => { message.value = '' }, 2500)
  }
}

async function refreshAll() {
  busy.value = true
  try {
    await Promise.all([
      store.loadSafetyStatus(),
      store.loadAudit(),
      store.loadApprovals(),
      store.loadSafetyPolicy(),
    ])
  } finally {
    busy.value = false
  }
}

async function loadCurrentRun() {
  if (!store.currentRunId) {
    message.value = '当前会话还没有运行记录'
    setTimeout(() => { message.value = '' }, 2500)
    return
  }
  await store.loadRunAudit(store.currentRunId)
  activeSection.value = 'audit'
}

async function runChainVerify() {
  busy.value = true
  try {
    chainResult.value = await store.doVerifyAudit(store.auditFilter.day || '')
  } finally {
    busy.value = false
  }
}

async function runProbe() {
  if (!probeText.value.trim()) return
  await store.doReviewText(probeText.value, probeStage.value, probeAgent.value)
}

async function decide(item, approved) {
  busy.value = true
  try {
    await store.doDecideApproval(item.id, approved, approved ? '' : '用户拒绝')
    await store.loadSafetyStatus()
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await refreshAll()
})
</script>

<template>
  <div class="flex flex-col h-full">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700/50 shrink-0">
      <div class="flex items-center gap-3 min-w-0">
        <h2 class="font-semibold text-lg shrink-0">🔒 安全与执行审查</h2>
        <span
          class="text-[10px] px-2 py-0.5 rounded border"
          :class="{
            'border-green-500/40 bg-green-500/10 text-green-300': settings.mode === 'enforce',
            'border-amber-500/40 bg-amber-500/10 text-amber-300': settings.mode === 'monitor',
            'border-gray-600/40 bg-gray-600/10 text-gray-400': settings.mode === 'off',
          }"
        >
          {{ settings.mode === 'enforce' ? '强制执行审查' : settings.mode === 'monitor' ? '仅观察' : '已关闭' }}
        </span>
        <span v-if="approvals.length" class="text-[10px] px-2 py-0.5 rounded bg-orange-500/20 text-orange-300">
          {{ approvals.length }} 项待审批
        </span>
      </div>
      <div class="flex items-center gap-2">
        <span v-if="message" class="text-xs text-gray-400">{{ message }}</span>
        <button
          @click="refreshAll"
          :disabled="busy"
          class="text-xs text-gray-400 hover:text-slate-900 px-3 py-1 rounded-lg hover:bg-surface-700 transition-colors disabled:opacity-50"
        >
          刷新
        </button>
      </div>
    </div>

    <!-- Section tabs -->
    <div class="flex gap-1 px-4 py-2 border-b border-gray-700/50 shrink-0 overflow-x-auto">
      <button
        v-for="tab in [
          { key: 'overview', label: '概览与开关' },
          { key: 'approvals', label: `审批 (${approvals.length})` },
          { key: 'audit', label: '审计日志' },
          { key: 'rules', label: '策略与规则' },
          { key: 'probe', label: '文本审查' },
        ]"
        :key="tab.key"
        @click="activeSection = tab.key"
        class="text-xs px-3 py-1.5 rounded-lg transition-colors whitespace-nowrap"
        :class="activeSection === tab.key
          ? 'bg-primary-600/20 text-primary-400 border border-primary-500/30'
          : 'text-gray-400 hover:text-slate-900 hover:bg-surface-800'"
      >
        {{ tab.label }}
      </button>
    </div>

    <div class="flex-1 overflow-y-auto px-4 py-4 space-y-4">
      <!-- ── Overview ── -->
      <template v-if="activeSection === 'overview'">
        <!-- Stats -->
        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
            <div class="text-[10px] text-gray-500 uppercase tracking-wider">审计记录（内存）</div>
            <div class="text-xl font-semibold text-gray-100">{{ status.audit?.buffered ?? 0 }}</div>
            <div class="text-[10px] text-gray-500">磁盘可写：{{ status.audit?.writable ? '是' : '否' }}</div>
          </div>
          <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
            <div class="text-[10px] text-gray-500 uppercase tracking-wider">待审批 / 已决</div>
            <div class="text-xl font-semibold text-gray-100">
              {{ status.approvals?.pending ?? 0 }} / {{ status.approvals?.decided ?? 0 }}
            </div>
            <div class="text-[10px] text-gray-500">
              通过 {{ status.approvals?.approved ?? 0 }} · 拒绝 {{ status.approvals?.rejected ?? 0 }}
            </div>
          </div>
          <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
            <div class="text-[10px] text-gray-500 uppercase tracking-wider">已注册代理</div>
            <div class="text-xl font-semibold text-gray-100">{{ status.agents?.total ?? 0 }}</div>
            <div class="text-[10px] text-gray-500">
              第三方扩展 {{ status.agents?.package ?? 0 }} · 可触及主机 {{ status.agents?.executing ?? 0 }}
            </div>
          </div>
          <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
            <div class="text-[10px] text-gray-500 uppercase tracking-wider">规则总数</div>
            <div class="text-xl font-semibold text-gray-100">
              {{ (status.rules?.policy ?? 0) + (status.rules?.static ?? 0) }}
            </div>
            <div class="text-[10px] text-gray-500">
              策略 {{ status.rules?.policy ?? 0 }} · 注入检测 {{ status.rules?.static ?? 0 }}
            </div>
          </div>
        </div>

        <!-- Switches -->
        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-4 space-y-4">
          <div class="text-sm font-medium text-gray-200">审查策略</div>

          <div class="grid md:grid-cols-2 gap-4">
            <label class="block">
              <span class="text-xs text-gray-400">审查模式</span>
              <select
                :value="settings.mode"
                @change="setSetting('mode', $event.target.value)"
                class="mt-1 w-full bg-surface-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-primary-500"
              >
                <option v-for="opt in MODE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
              <span class="text-[10px] text-gray-500">
                {{ MODE_OPTIONS.find(o => o.value === settings.mode)?.hint }}
              </span>
            </label>

            <label class="block">
              <span class="text-xs text-gray-400">非流式运行遇到需审批动作时</span>
              <select
                :value="settings.nonstream_review"
                @change="setSetting('nonstream_review', $event.target.value)"
                class="mt-1 w-full bg-surface-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-primary-500"
              >
                <option v-for="opt in NONSTREAM_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
              <span class="text-[10px] text-gray-500">
                {{ NONSTREAM_OPTIONS.find(o => o.value === settings.nonstream_review)?.hint }}
              </span>
            </label>

            <label class="block">
              <span class="text-xs text-gray-400">审批超时（秒，超时按拒绝处理）</span>
              <input
                type="number"
                min="5"
                :value="settings.approval_timeout"
                @change="setSetting('approval_timeout', Number($event.target.value))"
                class="mt-1 w-full bg-surface-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-primary-500"
              />
            </label>
          </div>

          <div class="grid md:grid-cols-2 gap-2 pt-2 border-t border-gray-700/40">
            <label
              v-for="sw in [
                { key: 'auto_approve', label: '自动批准（跳过人工审批）', hint: '所有需审批动作直接放行，仅记录审计' },
                { key: 'llm_review', label: 'LLM 计划评审', hint: '静态扫描发现风险时再调用模型复核计划' },
                { key: 'goal_verification', label: '目标完成确认', hint: '执行结束后核对产出物是否真的满足请求' },
                { key: 'auto_remediate', label: '自动补救', hint: '未完成时自动追加补救步骤（受预算限制）' },
                { key: 'redact_outputs', label: '输出脱敏', hint: 'worker 结果中的凭据在入库前替换为占位符' },
              ]"
              :key="sw.key"
              class="flex items-start gap-2 p-2 rounded-lg hover:bg-surface-900 transition-colors cursor-pointer"
            >
              <input
                type="checkbox"
                :checked="!!settings[sw.key]"
                @change="setSetting(sw.key, $event.target.checked)"
                class="mt-0.5 w-4 h-4 accent-primary-500"
              />
              <span class="min-w-0">
                <span class="block text-xs text-gray-200">{{ sw.label }}</span>
                <span class="block text-[10px] text-gray-500">{{ sw.hint }}</span>
              </span>
            </label>
          </div>

          <div class="text-[10px] text-gray-500">
            运行预算：最多 {{ status.budget?.max_steps }} 步 / {{ status.budget?.max_waves }} 波 /
            {{ status.budget?.max_replans }} 轮补救 / {{ status.budget?.max_wall_seconds }} 秒
            （通过 AGENT_MAX_STEPS、AGENT_MAX_WAVES、AGENT_MAX_REPLANS、AGENT_MAX_WALL_SECONDS 配置）
          </div>
        </div>

        <!-- severity breakdown -->
        <div v-if="status.audit?.by_severity" class="rounded-xl border border-gray-700/50 bg-surface-800 p-4">
          <div class="text-sm font-medium text-gray-200 mb-2">审计严重度分布</div>
          <div class="flex flex-wrap gap-2">
            <span
              v-for="(count, sev) in status.audit.by_severity"
              :key="sev"
              class="text-[11px] px-2 py-1 rounded border border-gray-700/40"
              :class="SEVERITY_STYLE[sev] || SEVERITY_STYLE.info"
            >
              {{ sev }}: {{ count }}
            </span>
          </div>
        </div>
      </template>

      <!-- ── Approvals ── -->
      <template v-else-if="activeSection === 'approvals'">
        <div v-if="!approvals.length" class="text-sm text-gray-500">
          当前没有待审批的动作。当计划或某个步骤被判定为高风险时，执行会暂停在这里等待你的决定。
        </div>
        <div
          v-for="item in approvals"
          :key="item.id"
          class="rounded-xl border border-orange-500/40 bg-orange-500/10 p-3"
        >
          <div class="flex items-center gap-2 flex-wrap">
            <span class="text-[10px] px-1.5 py-0.5 rounded" :class="RISK_STYLE[item.risk]">{{ item.risk }}</span>
            <span class="text-xs font-medium text-gray-100">{{ item.stage === 'plan' ? '计划审查' : '动作门禁' }}</span>
            <span class="text-[10px] text-gray-400">run {{ item.run_id }}</span>
            <span class="ml-auto text-[10px] text-gray-500">{{ Math.round(item.age_seconds) }}s 前</span>
          </div>
          <div class="text-xs text-gray-200 mt-1">{{ item.summary }}</div>
          <div v-if="item.agent" class="text-[11px] text-gray-400">代理：{{ item.agent }}</div>
          <div v-if="item.task" class="text-[11px] text-gray-400 line-clamp-2 mt-0.5">{{ item.task }}</div>
          <ul class="mt-1 space-y-0.5">
            <li v-for="(r, ri) in (item.reasons || []).slice(0, 6)" :key="ri" class="text-[10px] text-orange-200/80">
              · {{ r }}
            </li>
          </ul>
          <div class="flex gap-2 mt-2">
            <button
              @click="decide(item, true)"
              :disabled="busy"
              class="text-xs px-3 py-1.5 rounded-lg bg-green-700 hover:bg-green-600 text-white transition-colors disabled:opacity-50"
            >
              批准
            </button>
            <button
              @click="decide(item, false)"
              :disabled="busy"
              class="text-xs px-3 py-1.5 rounded-lg bg-red-700 hover:bg-red-600 text-white transition-colors disabled:opacity-50"
            >
              拒绝
            </button>
          </div>
        </div>

        <div v-if="store.approvalHistory.length" class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
          <div class="text-sm font-medium text-gray-200 mb-2">最近决定</div>
          <div class="space-y-1">
            <div
              v-for="h in store.approvalHistory"
              :key="h.id"
              class="flex items-center gap-2 text-[11px] text-gray-400"
            >
              <span class="px-1.5 py-0.5 rounded" :class="h.status === 'approved' ? 'bg-green-500/20 text-green-300' : 'bg-red-500/20 text-red-300'">
                {{ h.status }}
              </span>
              <span class="truncate flex-1">{{ h.summary }}</span>
              <span class="text-gray-500">{{ h.decided_by }}</span>
            </div>
          </div>
        </div>
      </template>

      <!-- ── Audit ── -->
      <template v-else-if="activeSection === 'audit'">
        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3 space-y-3">
          <div class="grid md:grid-cols-4 gap-2">
            <input
              v-model="store.auditFilter.run_id"
              placeholder="run_id"
              class="bg-surface-900 border border-gray-600 rounded-lg px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-primary-500"
            />
            <select
              v-model="store.auditFilter.event"
              class="bg-surface-900 border border-gray-600 rounded-lg px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-primary-500"
            >
              <option v-for="ev in EVENT_OPTIONS" :key="ev" :value="ev">{{ ev || '全部事件' }}</option>
            </select>
            <select
              v-model="store.auditFilter.severity"
              class="bg-surface-900 border border-gray-600 rounded-lg px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-primary-500"
            >
              <option v-for="sv in SEVERITY_OPTIONS" :key="sv" :value="sv">{{ sv || '全部严重度' }}</option>
            </select>
            <select
              v-model="store.auditFilter.day"
              class="bg-surface-900 border border-gray-600 rounded-lg px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-primary-500"
            >
              <option value="">全部日期</option>
              <option v-for="d in store.auditDays" :key="d" :value="d">{{ d }}</option>
            </select>
          </div>
          <div class="flex flex-wrap gap-2">
            <button
              @click="store.loadAudit()"
              :disabled="busy"
              class="text-xs px-3 py-1.5 rounded-lg bg-primary-600 hover:bg-primary-500 text-white transition-colors disabled:opacity-50"
            >
              查询
            </button>
            <button
              @click="loadCurrentRun"
              :disabled="busy"
              class="text-xs px-3 py-1.5 rounded-lg bg-surface-700 hover:bg-surface-600 text-gray-100 transition-colors disabled:opacity-50"
            >
              本会话运行记录
            </button>
            <button
              @click="runChainVerify"
              :disabled="busy"
              class="text-xs px-3 py-1.5 rounded-lg bg-surface-700 hover:bg-surface-600 text-gray-100 transition-colors disabled:opacity-50"
            >
              校验哈希链
            </button>
            <span v-if="chainResult" class="text-xs self-center" :class="chainResult.ok ? 'text-green-400' : 'text-red-400'">
              {{ chainResult.ok ? '✅ 审计链完整' : '❌ 审计链被篡改' }}
              （{{ chainResult.days?.[0]?.records ?? 0 }} 条）
            </span>
          </div>
        </div>

        <div class="rounded-xl border border-gray-700/50 bg-surface-800 divide-y divide-gray-700/40">
          <div v-if="!records.length" class="p-4 text-sm text-gray-500">没有匹配的审计记录。</div>
          <div v-for="rec in records" :key="rec.id" class="p-3">
            <div class="flex items-center gap-2 flex-wrap text-[11px]">
              <span class="px-1.5 py-0.5 rounded" :class="SEVERITY_STYLE[rec.severity] || SEVERITY_STYLE.info">
                {{ rec.severity }}
              </span>
              <span class="text-gray-200 font-medium">{{ rec.event }}</span>
              <span v-if="rec.decision" class="px-1.5 py-0.5 rounded" :class="DECISION_STYLE[rec.decision]">
                {{ rec.decision }}
              </span>
              <span v-if="rec.risk && rec.risk !== 'low'" class="px-1.5 py-0.5 rounded" :class="RISK_STYLE[rec.risk]">
                {{ rec.risk }}
              </span>
              <span v-if="rec.stage" class="text-gray-500">{{ rec.stage }}</span>
              <span class="ml-auto text-gray-500">{{ fmtTime(rec.ts) }}</span>
            </div>
            <div class="text-xs text-gray-300 mt-1">{{ rec.message }}</div>
            <div v-if="rec.run_id" class="text-[10px] text-gray-500 mt-0.5">
              run {{ rec.run_id }}<span v-if="rec.target"> · target {{ rec.target }}</span>
              · hash {{ (rec.hash || '').slice(0, 10) }}
            </div>
          </div>
        </div>
      </template>

      <!-- ── Rules ── -->
      <template v-else-if="activeSection === 'rules'">
        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-4">
          <div class="text-sm font-medium text-gray-200 mb-2">风险 → 决策映射</div>
          <div class="flex flex-wrap gap-2">
            <span
              v-for="(decision, risk) in (policyData.risk_matrix || {})"
              :key="risk"
              class="text-[11px] px-2 py-1 rounded border border-gray-700/40"
            >
              <span :class="RISK_STYLE[risk]" class="px-1.5 py-0.5 rounded mr-1">{{ risk }}</span>
              → <span :class="DECISION_STYLE[decision]" class="px-1.5 py-0.5 rounded ml-1">{{ decision }}</span>
            </span>
          </div>
        </div>

        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-4">
          <div class="text-sm font-medium text-gray-200 mb-2">代理风险分级</div>
          <div class="overflow-x-auto">
            <table class="w-full text-[11px]">
              <thead class="text-gray-500">
                <tr class="text-left">
                  <th class="py-1 pr-3">代理</th>
                  <th class="py-1 pr-3">风险</th>
                  <th class="py-1 pr-3">类型</th>
                  <th class="py-1 pr-3">触及主机</th>
                  <th class="py-1">说明</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="agent in policyData.agents || []" :key="agent.name" class="border-t border-gray-700/30">
                  <td class="py-1 pr-3 text-gray-200">{{ agent.name }}</td>
                  <td class="py-1 pr-3">
                    <span class="px-1.5 py-0.5 rounded" :class="RISK_STYLE[agent.risk]">{{ agent.risk }}</span>
                  </td>
                  <td class="py-1 pr-3 text-gray-400">{{ agent.kind }}</td>
                  <td class="py-1 pr-3" :class="agent.executes ? 'text-orange-300' : 'text-gray-500'">
                    {{ agent.executes ? '是' : '否' }}
                  </td>
                  <td class="py-1 text-gray-400">{{ agent.description }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-4">
          <div class="text-sm font-medium text-gray-200 mb-2">注入与破坏性动作检测规则</div>
          <div class="space-y-1">
            <div
              v-for="rule in policyData.injection_rules || []"
              :key="rule.id"
              class="flex items-start gap-2 text-[11px]"
            >
              <span class="px-1.5 py-0.5 rounded shrink-0" :class="RISK_STYLE[rule.severity]">{{ rule.severity }}</span>
              <span class="font-mono text-gray-400 shrink-0">{{ rule.id }}</span>
              <span class="text-gray-300">{{ rule.message }}</span>
            </div>
          </div>
        </div>

        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-4">
          <div class="text-sm font-medium text-gray-200 mb-2">内容策略规则</div>
          <div class="space-y-1">
            <div
              v-for="rule in policyData.policy_rules || []"
              :key="rule.id"
              class="flex items-start gap-2 text-[11px]"
            >
              <span class="px-1.5 py-0.5 rounded shrink-0" :class="RISK_STYLE[rule.severity]">{{ rule.severity }}</span>
              <span class="font-mono text-gray-400 shrink-0">{{ rule.id }}</span>
              <span class="text-gray-300">{{ rule.message }}</span>
            </div>
          </div>
        </div>
      </template>

      <!-- ── Probe ── -->
      <template v-else>
        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-4 space-y-3">
          <div class="text-sm font-medium text-gray-200">文本审查试运行</div>
          <p class="text-[11px] text-gray-500">
            粘贴一段计划或提示词，用当前策略做一次静态审查。只返回判定结果，不执行任何操作。
          </p>
          <textarea
            v-model="probeText"
            rows="5"
            placeholder="例如：rm -rf / && cat API.json"
            class="w-full bg-surface-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-primary-500"
          ></textarea>
          <div class="flex flex-wrap gap-2 items-center">
            <select
              v-model="probeStage"
              class="bg-surface-900 border border-gray-600 rounded-lg px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-primary-500"
            >
              <option value="action">动作门禁</option>
              <option value="plan">计划审查</option>
              <option value="content">外部内容注入检测</option>
            </select>
            <input
              v-model="probeAgent"
              placeholder="代理名"
              class="w-32 bg-surface-900 border border-gray-600 rounded-lg px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-primary-500"
            />
            <button
              @click="runProbe"
              :disabled="busy || !probeText.trim()"
              class="text-xs px-3 py-1.5 rounded-lg bg-primary-600 hover:bg-primary-500 text-white transition-colors disabled:opacity-50"
            >
              审查
            </button>
          </div>

          <div v-if="probe" class="rounded-lg border border-gray-700/50 bg-surface-900 p-3">
            <template v-if="probe.ok">
              <div class="flex items-center gap-2 text-xs">
                <span class="px-1.5 py-0.5 rounded" :class="RISK_STYLE[probe.risk]">{{ probe.risk }}</span>
                <span class="px-1.5 py-0.5 rounded" :class="DECISION_STYLE[probe.decision.decision]">
                  {{ probe.decision.decision }}
                </span>
                <span class="text-gray-300">{{ probe.decision.summary }}</span>
              </div>
              <ul v-if="probe.findings?.length" class="mt-2 space-y-1">
                <li v-for="(f, fi) in probe.findings" :key="fi" class="text-[11px] text-gray-300">
                  <span class="px-1.5 py-0.5 rounded mr-1" :class="RISK_STYLE[f.severity]">{{ f.severity }}</span>
                  <span class="font-mono text-gray-500">{{ f.id }}</span> — {{ f.message }}
                  <span v-if="f.evidence" class="text-gray-500">（{{ f.evidence }}）</span>
                </li>
              </ul>
              <div v-else class="mt-1 text-[11px] text-green-400">未发现风险</div>
            </template>
            <div v-else class="text-xs text-red-400">审查失败：{{ probe.error }}</div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>
