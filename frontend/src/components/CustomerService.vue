<script setup>
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref } from 'vue'
import { customerServiceRequest } from '../api/index.js'
import SupportOperations from './SupportOperations.vue'

const storageKey = 'candoit-support-demo-session'
const token = ref('')
const session = ref(null)
const product = ref('cream')
const channel = ref('web')
const draft = ref('')
const messages = ref([])
const ticketDraft = ref(null)
const afterSaleDraft = ref(null)
const workbench = ref(false)
let pollTimer
const pending = ref(false)
let mutationCount = 0
const error = ref('')
const timeline = ref(null)
const summaryField = ref(null)
const statusNames = { pending: '待受理', in_progress: '处理中', resolved: '已结单' }
const saleStates = { submitted: '申请已提交', reviewing: '人工审核中', completed: '处理完成' }
const examples = computed(() => [
  '现在满多少送赠品？',
  '什么时候发货？',
  ...(session.value?.orders.slice(0, 1).flatMap(order => [
    `${order.id} 的物流到哪了？`, `${order.id} 下单时有什么赠品规则？`,
    `${order.id} 那盒旅行装怎么没跟包裹一起来？`,
  ]) || []),
  '使用商品后感觉不适，想联系人工。',
])

async function perform(action) {
  if (pending.value) return
  mutationCount += 1
  pending.value = true
  error.value = ''
  try {
    await action()
  } catch (problem) {
    const detail = problem.response?.data?.detail
    error.value = typeof detail === 'string' ? detail : '操作未完成，请检查服务是否已启动后重试。'
  } finally {
    pending.value = false
  }
}

async function openSession(buyer) {
  await perform(async () => {
    const buyerKey = `${storageKey}-${buyer}`
    const stored = sessionStorage.getItem(buyerKey)
    let restored
    if (stored) {
      try {
        restored = await customerServiceRequest('/session', { token: stored })
      } catch (problem) {
        if (problem.response?.status !== 401) throw problem
        sessionStorage.removeItem(buyerKey)
      }
    }
    const data = restored || await customerServiceRequest('/sessions', { method: 'post', data: { buyer } })
    token.value = restored ? stored : data.token
    sessionStorage.setItem(buyerKey, token.value)
    sessionStorage.setItem(storageKey, data.buyer)
    session.value = data
    messages.value = data.messages
    draft.value = ''
    ticketDraft.value = null
    afterSaleDraft.value = null
  })
}

onMounted(() => openSession(sessionStorage.getItem(storageKey) === 'b' ? 'b' : 'a'))
onActivated(() => {
  pollTimer = setInterval(() => {
    if (session.value && !workbench.value && !document.hidden && !pending.value) {
      refreshTickets(true).catch(() => {})
    }
  }, 3000)
})
onDeactivated(() => clearInterval(pollTimer))
onUnmounted(() => clearInterval(pollTimer))

async function ask(message) {
  const query = message.trim()
  if (!query || !session.value) return
  await perform(async () => {
    const answer = await customerServiceRequest('/messages', {
      method: 'post', token: token.value, data: { message: query, product: product.value, channel: channel.value },
    })
    if (!messages.value.some(item => item.id === answer.message_id)) {
      messages.value.push({ id: answer.message_id, query, answer })
    }
    if (draft.value.trim() === query) draft.value = ''
    await nextTick()
    if (timeline.value) timeline.value.scrollTop = timeline.value.scrollHeight
  })
}

async function prepareTicket(message = '', orderId = '', messageId = null) {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  ticketDraft.value = { request_id: hex, order_id: orderId || '', summary: message, message_id: messageId }
  await nextTick()
  summaryField.value?.focus()
}

function rememberTicket(ticket) {
  const index = session.value.tickets.findIndex(item => item.id === ticket.id)
  if (index === -1) session.value.tickets.unshift(ticket)
  else session.value.tickets[index] = ticket
}

async function submitTicket() {
  await perform(async () => {
    const data = { ...ticketDraft.value, order_id: ticketDraft.value.order_id || null }
    const ticket = await customerServiceRequest('/tickets', { method: 'post', token: token.value, data })
    rememberTicket(ticket)
    ticketDraft.value = null
  })
}

async function applyAfterSale() {
  await perform(async () => {
    const item = await customerServiceRequest('/after-sales', { method: 'post', token: token.value, data: afterSaleDraft.value })
    session.value.after_sales = [item, ...session.value.after_sales.filter(sale => sale.id !== item.id)]
    afterSaleDraft.value = null
  })
}

let refreshRunning = false
async function refreshTickets(quiet = false) {
  if (!session.value || refreshRunning) return
  const capturedToken = token.value
  const capturedMutation = mutationCount
  const sync = async () => {
    refreshRunning = true
    try {
      const data = await customerServiceRequest('/session', { token: capturedToken })
      if (token.value !== capturedToken || (quiet && (pending.value || capturedMutation !== mutationCount))) return
      const hasNewMessage = data.messages.at(-1)?.id > (messages.value.at(-1)?.id || 0)
      session.value = data
      messages.value = data.messages
      if (hasNewMessage) {
        await nextTick()
        if (timeline.value) timeline.value.scrollTop = timeline.value.scrollHeight
      }
    } finally {
      refreshRunning = false
    }
  }
  if (quiet) await sync()
  else await perform(sync)
}

async function showCustomer() {
  workbench.value = false
  await refreshTickets()
}
</script>

<template>
  <div class="desk">
    <header class="desk-heading">
      <div>
        <h1>电商客服工作台</h1>
        <p>模拟商店 · 规则日期 {{ session?.demo_date || '—' }}</p>
      </div>
      <div class="account-controls" role="group" aria-label="体验身份">
        <button v-if="workbench" type="button" @click="showCustomer">返回顾客端</button>
        <template v-else><button type="button" v-for="buyer in ['a', 'b']" :key="buyer"
          :aria-pressed="session?.buyer === buyer" :disabled="pending || session?.buyer === buyer"
          @click="openSession(buyer)">顾客 {{ buyer.toUpperCase() }}</button>
          <button type="button" :disabled="pending" @click="workbench = true">进入客服工作台</button>
        </template>
      </div>
    </header>

    <SupportOperations v-if="workbench" />
    <template v-else>
    <p v-if="error" class="error" role="alert">{{ error }}
      <button v-if="!session || error.includes('重新开启')" type="button" @click="openSession(session?.buyer || 'a')">重试连接</button>
    </p>
    <p v-else-if="!session" class="loading" role="status">正在开启体验会话…</p>

    <div v-if="session" class="desk-grid">
      <section class="conversation" aria-labelledby="conversation-heading">
        <div class="section-heading">
          <h2 id="conversation-heading">顾客对话</h2>
          <div class="filters">
            <label>商品<select v-model="product" :disabled="pending">
              <option v-for="(name, id) in session.products" :key="id" :value="id">{{ name }}</option>
            </select></label>
            <label>渠道<select v-model="channel" :disabled="pending">
              <option v-for="(name, id) in session.channels" :key="id" :value="id">{{ name }}</option>
            </select></label>
          </div>
        </div>
        <div ref="timeline" class="timeline" role="log" aria-label="客服对话记录">
          <p v-if="!messages.length" class="greeting">你好，可以从下方选择一个问题，也可以输入你想咨询的内容。</p>
          <template v-for="{ query, answer, id } in messages" :key="id">
            <p v-if="query" class="customer-message">{{ query }}</p>
            <article class="answer" :class="{ 'human-answer': answer.kind === 'human' }">
              <p v-if="answer.kind === 'human'" class="field-hint">人工客服<span v-if="answer.resolved"> · 结单说明</span></p>
              <p>{{ answer.text }}</p>
              <details v-if="answer.citation" class="receipt">
                <summary>{{ answer.citation.title }} · 查看规则依据</summary>
                <blockquote>{{ answer.citation.excerpt }}</blockquote>
                <div>核对日期 {{ answer.citation.effective_date }}</div>
                <div>生效 {{ answer.citation.start }}<span v-if="answer.citation.end">，{{ answer.citation.end }} 起失效</span></div>
                <code>{{ answer.citation.rule_id }} · {{ answer.citation.chunk_id }}</code>
              </details>
              <dl v-if="answer.order" class="order-facts">
                <dt>商品</dt><dd>{{ answer.order.product }}</dd>
                <dt>下单日期</dt><dd>{{ answer.order.purchased }}</dd>
                <dt>渠道</dt><dd>{{ answer.order.channel }}</dd>
              </dl>
              <button v-if="answer.kind === 'handoff'" type="button" class="action mt-2" :disabled="pending"
                @click="prepareTicket(query, answer.order_id)">填写工单</button>
              <button v-if="answer.kind === 'gift_review'" type="button" class="action mt-2" :disabled="pending"
                @click="prepareTicket(answer.issue, answer.order_id, id)">请人工核实赠品</button>
            </article>
          </template>
        </div>
        <div class="prompt-list" aria-label="示例问题">
          <button v-for="example in examples" :key="example" type="button" :disabled="pending" @click="ask(example)">{{ example }}</button>
        </div>
        <form class="composer" @submit.prevent="ask(draft)">
          <label class="sr-only" for="support-message">输入咨询内容</label>
          <input id="support-message" v-model="draft" maxlength="1000" placeholder="咨询商品、活动或订单情况" :disabled="pending" />
          <button class="action primary" :disabled="pending || !draft.trim()">发送</button>
        </form>
      </section>

      <aside class="casework" aria-labelledby="ticket-heading">
        <div class="section-heading">
          <h2 id="ticket-heading">人工工单</h2>
          <button class="action compact" type="button" :disabled="pending" @click="refreshTickets()">刷新</button>
        </div>
        <p class="field-hint">客服接手后，回复和结单说明会出现在对话中。请使用虚构信息。</p>
        <form v-if="ticketDraft" class="ticket" @submit.prevent="submitTicket">
          <h3>提交给人工客服</h3>
          <label>关联订单<select v-model="ticketDraft.order_id" :disabled="pending || !!ticketDraft.message_id">
            <option value="">不关联订单</option>
            <option v-for="order in session.orders" :key="order.id" :value="order.id">{{ order.id }} · {{ order.product }}</option>
          </select></label>
          <label>问题说明<textarea ref="summaryField" v-model="ticketDraft.summary" rows="3" maxlength="1000" :disabled="pending" required /></label>
          <div class="ticket-actions">
            <button class="action primary" :disabled="pending || !ticketDraft.summary.trim()">提交工单</button>
            <button class="action" type="button" :disabled="pending" @click="ticketDraft = null">取消</button>
          </div>
        </form>
        <button v-else type="button" class="action new-ticket" :disabled="pending" @click="prepareTicket()">新建工单</button>
        <p v-if="!session.tickets.length" class="empty">本会话暂无工单。</p>
        <article v-for="ticket in session.tickets" :key="ticket.id" class="ticket">
          <header><code>#{{ ticket.id.slice(0, 8) }}</code><span :data-status="ticket.status">{{ statusNames[ticket.status] }}</span></header>
          <p v-if="ticket.order_id" class="field-hint">{{ ticket.order_id }}</p>
          <p class="ticket-summary">{{ ticket.summary }}</p>
          <p v-if="ticket.resolution" class="resolution">处理说明 · {{ ticket.resolution }}</p>
        </article>
        <div class="section-heading sale-heading"><h2>模拟售后</h2>
          <button v-if="!afterSaleDraft" class="action" type="button" :disabled="pending || !session.orders.length"
            @click="afterSaleDraft = { order_id: session.orders[0].id, summary: '' }">申请售后</button>
        </div>
        <form v-if="afterSaleDraft" class="ticket" @submit.prevent="applyAfterSale">
          <label>订单<select v-model="afterSaleDraft.order_id" :disabled="pending">
            <option v-for="order in session.orders" :key="order.id" :value="order.id">{{ order.id }} · {{ order.product }}</option>
          </select></label>
          <label>申请原因<textarea v-model="afterSaleDraft.summary" rows="2" maxlength="1000" :disabled="pending" required /></label>
          <div class="ticket-actions">
            <button class="action primary" :disabled="pending || !afterSaleDraft.summary.trim()">提交售后申请</button>
            <button class="action" type="button" :disabled="pending" @click="afterSaleDraft = null">取消</button>
          </div>
        </form>
        <p v-if="!session.after_sales.length" class="empty">本会话暂无售后记录。</p>
        <article v-for="sale in session.after_sales" :key="sale.id" class="ticket">
          <header><code>{{ sale.order_id }}</code><span>{{ saleStates[sale.status] }}</span></header>
          <p class="ticket-summary">{{ sale.summary }}</p>
          <button type="button" class="action" :disabled="pending" @click="ask(`${sale.order_id} 售后进度`)">查询售后进度</button>
        </article>
      </aside>
    </div>
    </template>
  </div>
</template>

<style scoped>
.desk { --ink: #1d3134; --muted: #52676b; --line: #d2dedc; --paper: #f0f5f3; --teal: #175c58; --amber: #9a561e;
  height: 100%; overflow-y: auto; color: var(--ink); background: var(--paper); padding: clamp(.75rem, 3vw, 2.5rem); }
.desk-heading, .section-heading, .account-controls, .filters, .ticket-actions { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
.desk-heading { justify-content: space-between; margin: 0 auto 1.4rem; max-width: 88rem; }
h1, h2 { font-family: 'Noto Serif CJK SC', 'Songti SC', SimSun, serif; font-weight: 700; }
h1 { font-size: clamp(1.45rem, 4vw, 2.5rem); line-height: 1.2; margin-bottom: .35rem; }
h2 { font-size: 1.2rem; }
.desk-heading p, .field-hint, .empty, .loading { color: var(--muted); font-size: .85rem; }
.desk-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(15rem, 1fr); margin: auto; max-width: 88rem; gap: 1rem; align-items: start; }
.conversation, .casework { padding: clamp(.7rem, 2vw, 1.2rem); background: white; border: 1px solid var(--line); border-radius: .65rem; min-width: 0; }
.section-heading { justify-content: space-between; padding-bottom: .75rem; border-bottom: 1px solid var(--line); }
.filters label, .ticket label { font-size: .78rem; color: var(--muted); display: grid; gap: .2rem; }
select, input, textarea { min-width: 0; padding: .45rem .6rem; background: white; color: var(--ink); border: 1px solid var(--line); border-radius: .35rem; }
.timeline { min-height: 12rem; height: min(49vh, 32rem); overflow: auto; display: flex; flex-direction: column; gap: .75rem; padding: .9rem .15rem; }
.greeting, .answer { align-self: start; background: var(--paper); border-radius: .45rem; padding: .75rem; max-width: 94%; overflow-wrap: anywhere; }
.customer-message, .ticket-summary, .resolution { overflow-wrap: anywhere; white-space: pre-wrap; }
.customer-message { align-self: end; padding: .6rem .75rem; border: 1px solid var(--line); border-radius: .45rem; max-width: 90%; }
.receipt { border-left: .2rem solid var(--teal); padding: .4rem .65rem; margin-top: .65rem; background: white; font-size: .8rem; }
.receipt summary { font-weight: 600; cursor: pointer; color: var(--teal); }
.receipt blockquote { margin: .35rem 0; }
.human-answer { border-left: .2rem solid var(--teal); }
.sale-heading { margin-top: 1rem; }
.receipt code { color: var(--muted); font-size: .7rem; }
.order-facts { display: grid; grid-template-columns: max-content 1fr; gap: .2rem .7rem; font-size: .8rem; margin-top: .55rem; }
.order-facts dt { color: var(--muted); }
.prompt-list { display: flex; flex-wrap: wrap; gap: .35rem; padding: .65rem 0; border-top: 1px solid var(--line); }
.prompt-list button, .action, .account-controls button, .error button { border-radius: .35rem; border: 1px solid var(--line); padding: .4rem .6rem; font-size: .8rem; color: var(--teal); }
.prompt-list button:hover, .action:hover, .account-controls button:hover { background: var(--paper); }
.account-controls [aria-pressed='true'] { border-color: var(--teal); color: var(--ink); font-weight: 650; }
.composer { display: flex; gap: .45rem; }
.composer input { flex: 1; }
.primary, .primary:hover { background: var(--teal); color: white; border-color: var(--teal); }
.casework > p, .new-ticket { margin-top: .65rem; }
.ticket { padding: .75rem; border: 1px solid var(--line); border-radius: .4rem; margin-top: .7rem; display: grid; gap: .45rem; }
.ticket header { display: flex; justify-content: space-between; flex-wrap: wrap; gap: .2rem; font-size: .8rem; }
.ticket header span { color: var(--teal); font-weight: 650; }
.ticket header span[data-status='pending'] { color: var(--amber); }
.ticket > .action { justify-self: start; }
.ticket form .action { margin-top: .5rem; }
.error { margin: 0 auto .75rem; max-width: 88rem; color: #842d25; padding: .6rem; border: 1px solid #dba8a4; background: white; }
button:disabled { cursor: default; }
button:disabled:not([aria-pressed='true']) { opacity: .6; }
:is(button, input, select, textarea, summary):focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
@media (max-width: 820px) { .desk-grid { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 400px) { .filters { width: 100%; } .filters label { flex: 1; } select { width: 100%; } }
</style>
