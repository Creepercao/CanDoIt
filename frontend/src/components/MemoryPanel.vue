<script setup>
import { ref, computed, onMounted } from 'vue'
import { useChatStore } from '../stores/chat.js'
import { fetchMemories, fetchMemoryStats, confirmMemory, rejectMemory, deleteMemory, createMemory } from '../api/index.js'

const store = useChatStore()

const memories = ref([])
const stats = ref(null)
const busy = ref(false)
const filter = ref({ status: '', kind: '' })
const newMemory = ref({ text: '', kind: 'fact' })

const KIND_STYLE = {
  preference: 'bg-indigo-500/15 text-indigo-300 border-indigo-500/30',
  fact: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  conclusion: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
}
const KIND_LABEL = { preference: '偏好', fact: '事实', conclusion: '结论' }
const STATUS_LABEL = { active: '已生效', pending: '待确认', rejected: '已否决' }
const STATUS_STYLE = {
  active: 'bg-green-500/15 text-green-300',
  pending: 'bg-amber-500/15 text-amber-300',
  rejected: 'bg-gray-600/25 text-gray-400',
}

const pending = computed(() => memories.value.filter(m => m.status === 'pending'))

function kindLabel(kind) { return KIND_LABEL[kind] || kind }
function kindStyle(kind) { return KIND_STYLE[kind] || KIND_STYLE.fact }
function statusLabel(status) { return STATUS_LABEL[status] || status }
function statusStyle(status) { return STATUS_STYLE[status] || STATUS_STYLE.rejected }
function fmtTime(ts) {
  if (!ts) return '-'
  try { return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) } catch { return '-' }
}

async function refresh() {
  busy.value = true
  try {
    const params = {}
    if (filter.value.status) params.status = filter.value.status
    if (filter.value.kind) params.kind = filter.value.kind
    const [list, aggregate] = await Promise.all([fetchMemories(params), fetchMemoryStats()])
    memories.value = list.memories || []
    stats.value = aggregate
  } catch (e) {
    console.error('Failed to load memories:', e)
  } finally {
    busy.value = false
  }
}

async function confirm(item) {
  await confirmMemory(item.id)
  await refresh()
}
async function reject(item) {
  await rejectMemory(item.id)
  await refresh()
}
async function remove(item) {
  if (!window.confirm('删除这条记忆？')) return
  await deleteMemory(item.id)
  await refresh()
}

async function add() {
  if (!newMemory.value.text.trim()) return
  await createMemory({ text: newMemory.value.text.trim(), kind: newMemory.value.kind })
  newMemory.value = { text: '', kind: 'fact' }
  await refresh()
}

onMounted(refresh)
</script>

<template>
  <div class="flex flex-col h-full">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700/50 shrink-0">
      <div class="flex items-center gap-3 min-w-0">
        <h2 class="font-semibold text-lg shrink-0">🧠 长期记忆</h2>
        <span v-if="stats" class="text-[10px] text-gray-500">
          共 {{ stats.total }} 条 · 生效 {{ stats.by_status?.active || 0 }} · 待确认 {{ stats.pending || 0 }}
        </span>
        <span v-if="!stats?.enabled" class="text-[10px] px-2 py-0.5 rounded bg-gray-600/25 text-gray-400">
          已关闭（AGENT_MEMORY=0）
        </span>
      </div>
      <div class="flex items-center gap-2">
        <select v-model="filter.status" @change="refresh"
                class="bg-surface-900 border border-gray-600 rounded-lg px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-primary-500">
          <option value="">全部状态</option>
          <option value="active">已生效</option>
          <option value="pending">待确认</option>
          <option value="rejected">已否决</option>
        </select>
        <select v-model="filter.kind" @change="refresh"
                class="bg-surface-900 border border-gray-600 rounded-lg px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-primary-500">
          <option value="">全部类型</option>
          <option value="preference">偏好</option>
          <option value="fact">事实</option>
          <option value="conclusion">结论</option>
        </select>
        <button @click="refresh" :disabled="busy"
                class="text-xs text-gray-400 hover:text-slate-900 px-3 py-1 rounded-lg hover:bg-surface-700 transition-colors disabled:opacity-50">
          刷新
        </button>
      </div>
    </div>

    <div class="flex-1 overflow-y-auto px-4 py-4 space-y-4">
      <!-- Pending confirmations -->
      <div v-if="pending.length" class="rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 space-y-2">
        <div class="text-xs font-medium text-amber-200">
          ⏸ {{ pending.length }} 条结论等待确认
        </div>
        <p class="text-[11px] text-gray-400">
          研究结论会被后续会话直接引用，因此先暂存。确认后才参与召回。
        </p>
        <div v-for="item in pending" :key="item.id"
             class="rounded-lg border border-amber-500/30 bg-surface-800 p-2.5">
          <div class="flex items-start gap-2">
            <span class="text-[10px] px-1.5 py-0.5 rounded border shrink-0" :class="kindStyle(item.kind)">
              {{ kindLabel(item.kind) }}
            </span>
            <span class="text-xs text-gray-200 flex-1 min-w-0">{{ item.text }}</span>
          </div>
          <div class="flex items-center gap-2 mt-2">
            <button @click="confirm(item)"
                    class="text-xs px-3 py-1 rounded-lg bg-green-700 hover:bg-green-600 text-white transition-colors">
              确认并生效
            </button>
            <button @click="reject(item)"
                    class="text-xs px-3 py-1 rounded-lg bg-surface-700 hover:bg-surface-600 text-gray-100 transition-colors">
              否决
            </button>
            <span class="text-[10px] text-gray-500 ml-auto">
              置信度 {{ item.confidence }} · {{ fmtTime(item.created_at) }}
            </span>
          </div>
        </div>
      </div>

      <!-- Manual add -->
      <div class="rounded-xl border border-gray-700/50 bg-surface-800 p-3 space-y-2">
        <div class="text-sm font-medium text-gray-200">手动添加一条记忆</div>
        <div class="flex flex-wrap gap-2">
          <select v-model="newMemory.kind"
                  class="bg-surface-900 border border-gray-600 rounded-lg px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-primary-500">
            <option value="fact">事实</option>
            <option value="preference">偏好</option>
            <option value="conclusion">结论</option>
          </select>
          <input v-model="newMemory.text" @keydown.enter="add"
                 placeholder="例如：报告一律用中文"
                 class="flex-1 min-w-[200px] bg-surface-900 border border-gray-600 rounded-lg px-3 py-1.5 text-xs text-gray-100 placeholder-gray-500 focus:outline-none focus:border-primary-500" />
          <button @click="add" :disabled="!newMemory.text.trim()"
                  class="text-xs px-3 py-1.5 rounded-lg bg-primary-600 hover:bg-primary-500 text-white transition-colors disabled:opacity-50">
            添加
          </button>
        </div>
        <p class="text-[10px] text-gray-500">
          手动添加的记忆视为人工确认，直接生效；含密钥/密码的内容会被拒绝写入。
        </p>
      </div>

      <!-- Memory list -->
      <div v-if="!memories.length" class="text-sm text-gray-500">
        还没有记忆。对话结束后，系统会自动沉淀长期偏好与事实；研究结论会先进入上方待确认区。
      </div>

      <div class="space-y-2">
        <div v-for="item in memories" :key="item.id"
             class="rounded-xl border border-gray-700/50 bg-surface-800 p-3">
          <div class="flex items-start gap-2">
            <span class="text-[10px] px-1.5 py-0.5 rounded border shrink-0" :class="kindStyle(item.kind)">
              {{ kindLabel(item.kind) }}
            </span>
            <span class="text-xs text-gray-200 flex-1 min-w-0">{{ item.text }}</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded shrink-0" :class="statusStyle(item.status)">
              {{ statusLabel(item.status) }}
            </span>
          </div>
          <div class="flex items-center gap-2 mt-2 text-[10px] text-gray-500">
            <span>{{ fmtTime(item.created_at) }}</span>
            <span v-if="item.run_id" class="font-mono">· {{ item.run_id }}</span>
            <span v-if="item.tags?.length">· {{ item.tags.join(', ') }}</span>
            <button v-if="item.status !== 'active'" @click="confirm(item)"
                    class="ml-auto text-green-400 hover:text-green-300">生效</button>
            <button v-if="item.status !== 'rejected'" @click="reject(item)"
                    :class="item.status !== 'active' ? '' : 'ml-auto'"
                    class="text-gray-400 hover:text-gray-200">否决</button>
            <button @click="remove(item)" class="text-red-400 hover:text-red-300">删除</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
