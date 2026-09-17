<script setup>
import { ref, computed, onMounted } from 'vue'
import { useChatStore } from '../stores/chat.js'
import {
  fetchRuns, fetchRun, fetchRunStats, fetchRunPricing, deleteRun, fetchRunChain,
} from '../api/index.js'

const store = useChatStore()

const runs = ref([])
const stats = ref(null)
const pricing = ref(null)
const selected = ref(null)          // full run record
const loading = ref(false)
const onlyCurrentSession = ref(false)
const expanded = ref({})            // run_id -> bool (inline detail)
const details = ref({})             // run_id -> full record
const chains = ref({})              // run_id -> { text, summary }
const copied = ref('')              // run_id just copied

const STATUS_STYLE = {
  complete: 'bg-green-500/15 text-green-300 border-green-500/30',
  partial: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  incomplete: 'bg-orange-500/15 text-orange-300 border-orange-500/30',
  failed: 'bg-red-500/15 text-red-300 border-red-500/30',
}
const STATUS_LABEL = {
  complete: '✅ 已完成',
  partial: '⚠️ 部分完成',
  incomplete: '❌ 未完成',
  failed: '❌ 失败',
}

function statusStyle(status) {
  return STATUS_STYLE[status] || 'bg-gray-600/20 text-gray-400 border-gray-500/30'
}
function statusLabel(status) {
  return STATUS_LABEL[status] || status || '未知'
}
function fmtTime(value) {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleString('zh-CN', { hour12: false })
  } catch { return value }
}
function fmtNumber(value) {
  return (value ?? 0).toLocaleString('zh-CN')
}
function fmtDuration(seconds) {
  const total = Math.round(seconds || 0)
  if (total < 60) return `${total}s`
  return `${Math.floor(total / 60)}m${String(total % 60).padStart(2, '0')}s`
}
function fmtCost(value) {
  if (value === null || value === undefined) return '—'
  return `$${Number(value).toFixed(4)}`
}

async function refresh() {
  loading.value = true
  try {
    const [list, aggregate, priceInfo] = await Promise.all([
      fetchRuns({ limit: 100, sessionId: onlyCurrentSession.value ? store.currentSessionId : '' }),
      fetchRunStats(),
      fetchRunPricing(),
    ])
    runs.value = list.runs || []
    stats.value = aggregate
    pricing.value = priceInfo
  } catch (e) {
    console.error('Failed to load runs:', e)
  } finally {
    loading.value = false
  }
}

async function toggle(run) {
  const id = run.run_id
  if (expanded.value[id]) {
    expanded.value[id] = false
    return
  }
  expanded.value[id] = true
  const jobs = []
  if (!details.value[id]) {
    jobs.push(fetchRun(id).then(r => { details.value[id] = r })
      .catch(e => console.error('Failed to load run detail:', e)))
  }
  if (!chains.value[id]) {
    jobs.push(fetchRunChain(id, 'text').then(c => { chains.value[id] = c })
      .catch(() => { chains.value[id] = { text: '(本次运行没有链路记录)' } }))
  }
  await Promise.all(jobs)
}

async function copyChain(runId) {
  const text = chains.value[runId]?.text || ''
  try {
    await navigator.clipboard.writeText(text)
    copied.value = runId
    setTimeout(() => { copied.value = '' }, 1500)
  } catch (e) {
    console.error('Clipboard unavailable:', e)
  }
}

async function loadDetail(runId) {
  try {
    selected.value = await fetchRun(runId)
  } catch (e) {
    console.error('Failed to load run:', e)
  }
}

async function remove(runId) {
  if (!window.confirm(`删除运行记录 ${runId}？`)) return
  await deleteRun(runId)
  if (selected.value?.run_id === runId) selected.value = null
  delete details.value[runId]
  await refresh()
}

function agentBreakdown(record) {
  const byAgent = record?.usage?.by_agent || {}
  return Object.entries(byAgent)
    .map(([agent, info]) => ({ agent, ...info }))
    .sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0))
}

onMounted(refresh)
</script>

<template>
  <div class="flex flex-col h-full">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700/50 shrink-0">
      <div class="flex items-center gap-3 min-w-0">
        <h2 class="font-semibold text-lg shrink-0">📈 运行记录</h2>
        <span class="text-[10px] text-gray-500">{{ runs.length }} 条</span>
        <label class="flex items-center gap-1 text-[11px] text-gray-500 cursor-pointer">
          <input type="checkbox" v-model="onlyCurrentSession" @change="refresh" class="w-3 h-3 accent-primary-500" />
          只看当前会话
        </label>
      </div>
      <button
        @click="refresh"
        :disabled="loading"
        class="text-xs text-gray-400 hover:text-slate-900 px-3 py-1 rounded-lg hover:bg-surface-700 transition-colors disabled:opacity-50"
      >
        {{ loading ? '加载中…' : '刷新' }}
      </button>
    </div>

    <div class="flex-1 overflow-y-auto px-4 py-4 space-y-4">
      <!-- Aggregate -->
      <div v-if="stats" class="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
          <div class="text-[10px] text-gray-500 uppercase tracking-wider">运行次数</div>
          <div class="text-xl font-semibold text-gray-100">{{ stats.runs }}</div>
        </div>
        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
          <div class="text-[10px] text-gray-500 uppercase tracking-wider">模型调用</div>
          <div class="text-xl font-semibold text-gray-100">{{ fmtNumber(stats.model_calls) }}</div>
        </div>
        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
          <div class="text-[10px] text-gray-500 uppercase tracking-wider">总 token</div>
          <div class="text-xl font-semibold text-gray-100">{{ fmtNumber(stats.total_tokens) }}</div>
        </div>
        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
          <div class="text-[10px] text-gray-500 uppercase tracking-wider">平均耗时</div>
          <div class="text-xl font-semibold text-gray-100">{{ fmtDuration(stats.avg_duration) }}</div>
        </div>
        <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
          <div class="text-[10px] text-gray-500 uppercase tracking-wider">失败 / 重试任务</div>
          <div class="text-xl font-semibold text-gray-100">
            {{ stats.failed_tasks }} / {{ stats.retried_tasks }}
          </div>
        </div>
      </div>

      <div v-if="pricing && !pricing.configured"
           class="rounded-xl border border-gray-700/50 bg-surface-900 p-3 text-[11px] text-gray-500">
        未配置模型价格，因此只统计 token 不估算成本。{{ pricing.hint }}
      </div>
      <div v-else-if="stats?.total_cost_usd !== null && stats?.total_cost_usd !== undefined"
           class="rounded-xl border border-gray-700/50 bg-surface-900 p-3 text-[11px] text-gray-500">
        最近 {{ stats.runs }} 次运行累计估算成本：<span class="text-gray-300 font-medium">{{ fmtCost(stats.total_cost_usd) }}</span>
        （按 AGENT_MODEL_PRICING 估算）
      </div>

      <!-- Run list -->
      <div v-if="!runs.length" class="text-sm text-gray-500">
        还没有运行记录。发起一次对话后，这里会显示每次运行的耗时、token 用量、失败与重试情况。
      </div>

      <div class="space-y-2">
        <div v-for="run in runs" :key="run.run_id"
             class="rounded-xl border border-gray-700/50 bg-surface-800 overflow-hidden">
          <button @click="toggle(run)"
                  class="w-full text-left px-3 py-2.5 hover:bg-surface-900 transition-colors">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-[10px] px-1.5 py-0.5 rounded border" :class="statusStyle(run.status)">
                {{ statusLabel(run.status) }}
              </span>
              <span class="text-xs text-gray-300 truncate flex-1 min-w-0">
                {{ run.request || '(无请求内容)' }}
              </span>
              <span class="text-[10px] text-gray-500 shrink-0">{{ fmtTime(run.started_at) }}</span>
              <span class="text-[10px] text-gray-500 shrink-0">▾</span>
            </div>
            <div class="flex items-center gap-3 mt-1 text-[10px] text-gray-500 flex-wrap">
              <span>⏱ {{ fmtDuration(run.duration_seconds) }}</span>
              <span>🔢 {{ fmtNumber(run.model_calls) }} 次调用</span>
              <span>🎫 {{ fmtNumber(run.total_tokens) }} tokens</span>
              <span v-if="run.cost_usd !== null && run.cost_usd !== undefined">💰 {{ fmtCost(run.cost_usd) }}</span>
              <span :class="run.failed_tasks ? 'text-red-300' : ''">❌ {{ run.failed_tasks }} 失败</span>
              <span :class="run.retried_tasks ? 'text-amber-300' : ''">🔁 {{ run.retried_tasks }} 重试</span>
              <span class="font-mono text-gray-600">{{ run.run_id }}</span>
            </div>
          </button>

          <!-- Inline detail -->
          <div v-if="expanded[run.run_id]" class="border-t border-gray-700/40 px-3 py-3 space-y-3 bg-surface-900">
            <div v-if="!details[run.run_id]" class="text-xs text-gray-500">加载中…</div>
            <template v-else>
              <div v-if="details[run.run_id].verification?.summary" class="text-[11px] text-gray-400">
                {{ details[run.run_id].verification.summary }}
              </div>

              <!-- Tasks -->
              <div>
                <div class="text-xs font-medium text-gray-300 mb-1">任务</div>
                <table class="w-full text-[11px]">
                  <thead class="text-gray-500">
                    <tr class="text-left">
                      <th class="py-0.5 pr-3">代理</th>
                      <th class="py-0.5 pr-3">尝试</th>
                      <th class="py-0.5 pr-3">耗时</th>
                      <th class="py-0.5 pr-3">状态</th>
                      <th class="py-0.5">产出</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(task, ti) in details[run.run_id].tasks || []" :key="ti"
                        class="border-t border-gray-700/30">
                      <td class="py-0.5 pr-3 text-gray-300">{{ task.agent }}</td>
                      <td class="py-0.5 pr-3" :class="task.attempts > 1 ? 'text-amber-300' : 'text-gray-500'">
                        {{ task.attempts }}
                      </td>
                      <td class="py-0.5 pr-3 text-gray-500">{{ task.duration }}s</td>
                      <td class="py-0.5 pr-3" :class="task.status === 'done' ? 'text-green-300' : 'text-red-300'">
                        {{ task.status }}
                      </td>
                      <td class="py-0.5 text-gray-500">{{ task.count }} 条</td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <!-- Token breakdown -->
              <div>
                <div class="text-xs font-medium text-gray-300 mb-1">token 归属</div>
                <div class="space-y-0.5">
                  <div v-for="item in agentBreakdown(details[run.run_id])" :key="item.agent"
                       class="flex items-center gap-2 text-[11px] text-gray-400">
                    <span class="w-28 truncate text-gray-300">{{ item.agent }}</span>
                    <span>{{ fmtNumber(item.total_tokens) }} tokens</span>
                    <span class="text-gray-600">（入 {{ fmtNumber(item.input_tokens) }} / 出 {{ fmtNumber(item.output_tokens) }}，{{ item.calls }} 次）</span>
                  </div>
                </div>
              </div>

              <!-- Execution chain -->
              <div>
                <div class="flex items-center justify-between mb-1">
                  <span class="text-xs font-medium text-gray-300">
                    🧩 执行链路
                    <span v-if="chains[run.run_id]?.summary" class="text-[10px] font-normal text-gray-500">
                      （{{ chains[run.run_id].summary.spans }} 个环节
                      <span v-if="chains[run.run_id].summary.failed">
                        · {{ chains[run.run_id].summary.failed }} 失败</span>
                      <span v-if="chains[run.run_id].summary.blocked">
                        · {{ chains[run.run_id].summary.blocked }} 拦截</span>）
                    </span>
                  </span>
                  <button @click="copyChain(run.run_id)"
                          class="text-[10px] text-gray-400 hover:text-slate-900 px-2 py-0.5 rounded hover:bg-surface-700 transition-colors">
                    {{ copied === run.run_id ? '已复制' : '复制' }}
                  </button>
                </div>
                <pre class="text-[11px] leading-relaxed font-mono whitespace-pre-wrap break-all rounded-lg border border-gray-700/40 bg-surface-800 p-2 max-h-72 overflow-y-auto text-gray-300">{{ chains[run.run_id]?.text || '加载中…' }}</pre>
              </div>

              <!-- Safety -->
              <div v-if="details[run.run_id].safety?.policy" class="text-[11px] text-gray-500">
                审查 {{ details[run.run_id].safety.policy.checkpoints }} 个检查点 ·
                拦截 {{ details[run.run_id].safety.policy.blocked }} ·
                脱敏 {{ details[run.run_id].safety.redactions }}
                <span v-if="details[run.run_id].stopped_reason" class="text-amber-300">
                  · {{ details[run.run_id].stopped_reason }}
                </span>
              </div>

              <button @click="remove(run.run_id)"
                      class="text-[11px] text-red-400 hover:text-red-300">
                删除这条记录
              </button>
            </template>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
