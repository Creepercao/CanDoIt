<script setup>
import { computed, onMounted, ref } from 'vue'
import { customerServiceRequest } from '../api/index.js'

const staffKey = 'candoit-support-staff'
const staff = ref(sessionStorage.getItem(staffKey) || '')
const tab = ref('tickets')
const busy = ref(false)
const error = ref('')
const queue = ref([])
const active = ref(null)
const reply = ref('')
const catalog = ref(null)
const editor = ref(null)
const preview = ref(null)
const runs = ref([])
const report = ref(null)
const failedOnly = ref(false)
const statuses = { pending: '待受理', in_progress: '处理中', resolved: '已结单' }
const categories = { gift: '活动', returns: '售后', product: '商品' }
const channels = { web: '品牌商城', live: '直播间' }
const products = { cream: '舒缓面霜', cleanser: '氨基酸洁面' }
const checkNames = { kind: '处理分支', rule_id: '规则版本', date: '规则核对日期', text: '回复内容', excerpt: '引用原文', order_id: '订单号', status: '售后状态', first_kind: '第一轮处理', private_data: '订单隔离' }
const cases = computed(() => report.value?.cases.filter(item => !failedOnly.value || !item.passed) || [])

function request(path, method = 'get', data) {
  return customerServiceRequest(`/staff${path}`, { staff: staff.value, method, data })
}

async function perform(operation) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await operation()
  } catch (problem) {
    if (problem.response?.status === 401) {
      sessionStorage.removeItem(staffKey)
      staff.value = ''
    }
    const detail = problem.response?.data?.detail
    error.value = typeof detail === 'string' ? detail : '操作失败，请检查输入或稍后再试。'
  } finally {
    busy.value = false
  }
}

async function reloadQueue() {
  queue.value = await request('/tickets')
  if (active.value && queue.value.some(ticket => ticket.id === active.value.id)) {
    active.value = await request(`/tickets/${active.value.id}`)
  } else {
    active.value = null
  }
}

async function enter() {
  await perform(async () => {
    const data = await request('/sessions', 'post')
    staff.value = data.token
    sessionStorage.setItem(staffKey, data.token)
    await reloadQueue()
  })
}

onMounted(() => { if (staff.value) perform(reloadQueue) })

async function chooseTab(name) {
  tab.value = name
  await perform(async () => {
    if (name === 'tickets') await reloadQueue()
    if (name === 'rules') catalog.value = await request('/rules')
    if (name === 'quality') {
      runs.value = await request('/quality')
      report.value = runs.value[0] || null
    }
  })
}

async function showTicket(ticket) {
  await perform(async () => {
    active.value = await request(`/tickets/${ticket.id}`)
    reply.value = ''
  })
}

async function ticketAction(action) {
  await perform(async () => {
    const updated = await request(`/tickets/${active.value.id}/actions`, 'post', {
      action, text: ['reply', 'resolve'].includes(action) ? reply.value : null,
    })
    active.value = action === 'release' ? null : updated
    reply.value = ''
    queue.value = await request('/tickets')
  })
}

function startDraft(rule) {
  preview.value = null
  editor.value = {
    base_rule_id: rule.id, title: rule.title, body: rule.body,
    starts_at: catalog.value.demo_date,
    ends_at: rule.ends_at > catalog.value.demo_date ? rule.ends_at : '',
  }
}

async function saveDraft() {
  await perform(async () => {
    const { id, base_rule_id, starts_at, ends_at, body } = editor.value
    preview.value = await request(id ? `/rules/drafts/${id}` : '/rules/drafts', id ? 'patch' : 'post', {
      base_rule_id, starts_at, ends_at: ends_at || null, body,
    })
    editor.value = null
    catalog.value = await request('/rules')
  })
}

async function loadDraft(id) {
  await perform(async () => {
    preview.value = await request(`/rules/drafts/${id}`)
    editor.value = null
  })
}

async function publishRule() {
  await perform(async () => {
    preview.value = await request(`/rules/drafts/${preview.value.draft.id}/publish`, 'post', {
      draft_sha256: preview.value.draft_sha256,
    })
    catalog.value = await request('/rules')
  })
}

function editPreview() {
  editor.value = { ...preview.value.draft, title: preview.value.previous.title, ends_at: preview.value.draft.ends_at || '' }
}

async function runQuality() {
  await perform(async () => {
    report.value = await request('/quality', 'post')
    runs.value = await request('/quality')
  })
}

function timeLabel(value) {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' }).format(new Date(value))
}

function ruleState(rule) {
  if (rule.starts_at > catalog.value.demo_date) return '待生效'
  if (rule.ends_at && rule.ends_at <= catalog.value.demo_date) return '历史版本'
  return '生效中'
}
</script>

<template>
  <div class="operations">
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <section v-if="!staff" class="panel entry">
      <h2>人工客服工作台</h2>
      <p>使用模拟客服身份受理工单、管理规则和运行质检。</p>
      <button class="primary" :disabled="busy" @click="enter">以模拟客服进入</button>
    </section>
    <template v-else>
      <nav class="ops-nav" aria-label="客服工作台功能">
        <button v-for="[key, label] in [['tickets', '人工接待'], ['rules', '规则知识库'], ['quality', '客服质检']]"
          :key="key" :aria-current="tab === key ? 'page' : undefined" :disabled="busy" @click="chooseTab(key)">{{ label }}</button>
      </nav>

      <div v-if="tab === 'tickets'" class="split">
        <section class="panel">
          <header><h2>工单队列</h2><button :disabled="busy" @click="perform(reloadQueue)">刷新工单</button></header>
          <p v-if="!queue.length" class="hint">暂无待处理工单。</p>
          <button v-for="ticket in queue" :key="ticket.id" class="queue-row" :aria-pressed="active?.id === ticket.id"
            :disabled="busy" @click="showTicket(ticket)">
            <span class="queue-meta">顾客 {{ ticket.buyer.toUpperCase() }}<span>{{ statuses[ticket.status] }}</span></span>
            <strong>{{ ticket.summary }}</strong>
            <small v-if="ticket.order_id">{{ ticket.order_id }}<span v-if="ticket.after_sale_id"> · 售后申请</span></small>
          </button>
        </section>
        <section class="panel">
          <header><h2>交接详情</h2><span v-if="active">{{ statuses[active.status] }}</span></header>
          <p v-if="!active" class="hint">从队列选择一张工单。</p>
          <template v-else>
            <div class="evidence"><strong>顾客诉求</strong><p>{{ active.summary }}</p>
              <p v-if="active.order">{{ active.order.id }} · {{ active.order.product }} · {{ active.order.status }}</p>
              <details v-if="active.citation"><summary>最近查阅的规则 · {{ active.citation.title }}</summary>
                <p>{{ active.citation.excerpt }}</p><code>{{ active.citation.rule_id }}</code>
              </details>
            </div>
            <ol v-if="active.messages.length" class="transcript" aria-label="会话记录">
              <li v-for="item in active.messages" :key="item.id">
                <p v-if="item.query"><strong>顾客</strong> {{ item.query }}</p>
                <p><strong>{{ item.answer.kind === 'human' ? '人工客服' : '系统' }}</strong> {{ item.answer.text }}</p>
              </li>
            </ol>
            <button v-if="active.status !== 'resolved' && !active.assigned" class="primary" :disabled="busy" @click="ticketAction('claim')">受理工单</button>
            <form v-else-if="active.status === 'in_progress'" @submit.prevent="ticketAction('reply')">
              <label>回复或处理说明<textarea v-model="reply" rows="3" maxlength="1000" required /></label>
              <div class="actions">
                <button class="primary" :disabled="busy || !reply.trim()">发送给顾客</button>
                <button type="button" :disabled="busy || !reply.trim()" @click="ticketAction('resolve')">发送说明并结单</button>
                <button type="button" :disabled="busy || Boolean(reply.trim())" @click="ticketAction('release')">退回待受理</button>
              </div>
            </form>
            <p v-else class="hint">结单说明 · {{ active.resolution }}</p>
          </template>
        </section>
      </div>

      <div v-else-if="tab === 'rules' && catalog" class="split">
        <section class="panel">
          <header><h2>发布记录</h2><span class="hint">{{ catalog.demo_date }}</span></header>
          <details v-for="rule in catalog.versions" :key="rule.id" class="record">
            <summary>{{ rule.title }} <span class="label">{{ ruleState(rule) }}</span></summary>
            <small>{{ categories[rule.topic] }} · {{ channels[rule.channel] || '全渠道' }} · {{ products[rule.product] || '全商品' }}</small>
            <p>{{ rule.starts_at }} 生效<span v-if="rule.ends_at">，{{ rule.ends_at }} 起失效</span></p>
            <p>{{ rule.body }}</p><code>{{ rule.id }}</code>
            <button v-if="rule.replaceable" class="block-button" :disabled="busy" @click="startDraft(rule)">新建版本草稿</button>
          </details>
        </section>
        <section class="panel">
          <header><h2>政策草稿</h2></header>
          <form v-if="editor" class="draft" @submit.prevent="saveDraft">
            <h3>{{ editor.title }}</h3>
            <label>生效日期<input v-model="editor.starts_at" type="date" :min="catalog.demo_date" required /></label>
            <label>失效日期（选填）<input v-model="editor.ends_at" type="date" /></label>
            <label>规则正文<textarea v-model="editor.body" rows="5" maxlength="1000" required /></label>
            <div class="actions"><button class="primary" :disabled="busy || !editor.body.trim()">保存并预览</button>
              <button type="button" :disabled="busy" @click="editor = null">取消</button></div>
          </form>
          <div v-else-if="preview" class="draft">
            <h3>{{ preview.previous.title }} · {{ preview.published ? '已发布' : '发布预览' }}</h3>
            <div class="evidence"><strong>上一版</strong><p>{{ preview.previous.body }}</p></div>
            <div class="evidence"><strong>新版本</strong><p>{{ preview.draft.body }}</p>
              <small>{{ preview.draft.starts_at }} 生效<span v-if="preview.draft.ends_at">，{{ preview.draft.ends_at }} 起失效</span></small>
            </div>
            <div v-if="!preview.published" class="actions">
              <button :disabled="busy" @click="editPreview">继续编辑</button>
              <button class="primary" :disabled="busy" @click="publishRule">发布规则版本</button>
            </div>
            <p v-else class="hint" role="status">新版本已发布。</p>
          </div>
          <p v-else class="hint">展开发布记录中的规则，可为最新版本建立草稿。</p>
          <button v-for="item in catalog.drafts" :key="item.id" class="queue-row" :disabled="busy" @click="loadDraft(item.id)">
            <span class="queue-meta">{{ catalog.versions.find(rule => rule.id === item.base_rule_id)?.title || item.base_rule_id }}
              <span>{{ item.published_id ? '已发布' : '待发布' }}</span></span>
            <small>{{ item.starts_at }} 生效</small>
          </button>
        </section>
      </div>

      <section v-else-if="tab === 'quality'" class="panel quality">
        <header><h2>客服质检</h2><button class="primary" :disabled="busy" @click="runQuality">运行回归评估</button></header>
        <p v-if="!report" class="hint">用模拟用例检验当前发布的规则和客服流程。</p>
        <template v-else>
          <div class="quality-controls">
            <label>评估记录<select :value="report.created_at" @change="report = runs.find(run => run.created_at === $event.target.value)">
              <option v-for="run in runs" :key="run.created_at" :value="run.created_at">{{ timeLabel(run.created_at) }} · {{ run.passed }}/{{ run.total }}</option>
            </select></label>
            <label class="checkbox"><input v-model="failedOnly" type="checkbox" />仅看失败</label>
          </div>
          <p class="score" role="status"><strong>{{ report.passed }} / {{ report.total }} 通过</strong>
            <span>规则引用命中率 {{ Math.round(report.policy_recall_at_1 * 100) }}%<template v-if="report.policy_citation_queries != null">（{{ report.policy_citation_queries }} 题）</template></span>
            <span>较上次新增失败 {{ report.regressed.length }} 项</span></p>
          <details v-for="item in cases" :key="item.id" class="record">
            <summary>{{ item.category }} · {{ item.queries[0] }} <span class="label" :class="{ fail: !item.passed }">{{ item.passed ? '通过' : '失败' }}</span></summary>
            <dl class="checks"><dt>预期</dt><dd><span v-for="(value, key) in item.expected" :key="key">{{ checkNames[key] }} {{ value }} </span></dd></dl>
            <ol class="transcript"><li v-for="(answer, index) in item.replies" :key="index">
              <p><strong>顾客</strong> {{ item.queries[index] }}</p><p><strong>实际回复</strong> {{ answer.text }}</p>
              <p v-if="answer.citation" class="hint">{{ answer.citation.rule_id }} · {{ answer.citation.effective_date }}</p>
              <p v-if="answer.citation && answer.citation.excerpt !== answer.text"><strong>引用原文</strong> {{ answer.citation.excerpt }}</p>
            </li></ol>
            <p v-if="!item.passed" class="fail">差异 · {{ item.differences.map(key => checkNames[key]).join('、') }}</p>
          </details>
          <p v-if="!cases.length" class="hint">没有失败用例。</p>
        </template>
      </section>
      <p v-else-if="busy" role="status">正在加载…</p>
    </template>
  </div>
</template>

<style scoped>
.operations { margin: auto; max-width: 88rem; }
.ops-nav, header, .queue-meta, .actions, .quality-controls, .score { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem; }
.ops-nav { margin-bottom: .8rem; }
.ops-nav [aria-current], .queue-row[aria-pressed='true'] { background: #e3efeb; border-color: #81aaa1; }
.split { display: grid; grid-template-columns: minmax(14rem, 1fr) minmax(0, 1.7fr); gap: 1rem; align-items: start; }
.panel { min-width: 0; background: white; border: 1px solid var(--line); padding: 1rem; border-radius: .65rem; }
header { justify-content: space-between; border-bottom: 1px solid var(--line); padding-bottom: .65rem; margin-bottom: .6rem; }
h2 { font-family: 'Noto Serif CJK SC', 'Songti SC', SimSun, serif; font-size: 1.2rem; font-weight: 700; }
h3 { font-weight: 700; }.entry { max-width: 36rem; }.entry p { margin: .4rem 0 .85rem; }
button, textarea, input, select { border: 1px solid var(--line); border-radius: .35rem; padding: .42rem .6rem; min-width: 0; }
button { color: var(--teal); } button:hover { background: var(--paper); }
button:disabled { opacity: .55; cursor: default; }
.primary, .primary:hover { background: var(--teal); border-color: var(--teal); color: white; }
.queue-row { display: grid; width: 100%; text-align: left; margin: .45rem 0; color: var(--ink); padding: .6rem; overflow-wrap: anywhere; }
.queue-meta { justify-content: space-between; font-size: .8rem; color: var(--teal); }
.hint, small, code { color: var(--muted); font-size: .82rem; overflow-wrap: anywhere; }
.evidence, .score { padding: .65rem .8rem; background: var(--paper); border-left: .2rem solid var(--teal); overflow-wrap: anywhere; }
.evidence p, .record p { white-space: pre-wrap; overflow-wrap: anywhere; margin-top: .3rem; }
.transcript { max-height: 23rem; overflow: auto; margin: .6rem 0; }
.transcript li { border-bottom: 1px solid var(--line); padding: .4rem .1rem; overflow-wrap: anywhere; white-space: pre-wrap; }
.transcript strong { color: var(--teal); font-size: .8rem; }
label { display: grid; gap: .2rem; color: var(--muted); font-size: .85rem; }
textarea, select, input { background: white; color: var(--ink); }
.actions, .block-button, .quality-controls { margin-top: .65rem; }
.draft { display: grid; gap: .65rem; margin-bottom: .75rem; }
.draft > button { justify-self: start; }
.record { padding: .65rem .1rem; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
summary { cursor: pointer; } .label { color: var(--teal); margin-left: .35rem; font-size: .8rem; }
.fail, .error { color: #922c23; } .error { margin-bottom: .6rem; }
.checkbox { display: flex; align-items: center; }
.score { margin: .75rem 0; } .score span { color: var(--muted); font-size: .85rem; }
.checks { margin-top: .5rem; font-size: .85rem; }.checks dt { font-weight: 650; }
.checks dd { display: flex; flex-wrap: wrap; gap: .4rem; }
.checks dd span { background: var(--paper); padding: .1rem .3rem; }
:is(button, input, select, textarea, summary):focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
@media (max-width: 820px) { .split { grid-template-columns: minmax(0, 1fr); } }
</style>
