import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import {
  fetchChatModels, fetchImageModels, fetchVideoModels,
  refreshModels, sendMessage, generateImage, generateVideo,
  fetchSkills, toggleSkill,
  installSkillPackage, uninstallSkillPackage,
  createSession, fetchSessions, fetchSession,
  saveSession, deleteSessionApi,
  fetchProviders, createProvider, updateProvider, deleteProvider,
  streamPPT, streamNote,
  fetchSafetyStatus, fetchSafetyPolicy, updateSafetySettings,
  fetchAuditTrail, fetchRunAudit, verifyAuditChain,
  fetchApprovals, decideApproval, reviewText,
} from '../api/index.js'

// localStorage helpers
function loadSetting(key, fallback = '') {
  try { return localStorage.getItem('multiagent_' + key) || fallback } catch { return fallback }
}
function saveSetting(key, val) {
  try { localStorage.setItem('multiagent_' + key, val) } catch {}
}

function loadLocalMessages() {
  try {
    const raw = localStorage.getItem('multiagent_msgs')
    return raw ? JSON.parse(raw) : []
  } catch { return [] }
}

function saveLocalMessages(msgs) {
  try { localStorage.setItem('multiagent_msgs', JSON.stringify(msgs)) } catch {}
}

function loadLocalSessionId() {
  try { return localStorage.getItem('multiagent_sid') || '' } catch { return '' }
}

function saveLocalSessionId(sid) {
  try { localStorage.setItem('multiagent_sid', sid) } catch {}
}

export const useChatStore = defineStore('chat', () => {
  const messages = ref([])
  const isLoading = ref(false)
  const thinkSteps = ref([])        // Thinking timeline steps
  const currentResponse = ref('')   // Streaming response accumulator
  let abortController = null        // For stopping generation

  const chatModels = ref([])
  const imageModels = ref([])
  const videoModels = ref([])

  const selectedChatModel = ref(loadSetting('chatModel'))
  const selectedImageModel = ref(loadSetting('imageModel'))
  const selectedVideoModel = ref(loadSetting('videoModel'))

  const activeTab = ref('chat')
  const imageResults = ref([])
  const videoResults = ref([])

  // Sessions
  const sessions = ref([])
  const currentSessionId = ref('')

  // Skills
  const skills = ref([])
  const enabledSkills = ref([])

  // Providers
  const providers = ref([])

  // PPT
  const pptResults = ref([])
  const pptGenerating = ref(false)
  const pptStreamText = ref('')     // live token-by-token text
  const pptSteps = ref([])          // live agent progress timeline
  let pptAbortController = null

  // Scholar Notes
  const noteResults = ref([])
  const noteGenerating = ref(false)
  const noteStreamText = ref('')
  const noteSteps = ref([])
  let noteAbortController = null

  // ── Safety / execution review ──────────────────────────────────
  const safetyStatus = ref(null)        // /safety/status snapshot
  const safetyPolicy = ref(null)        // agent risk catalog + rules
  const safetySettings = ref({})        // live switches
  const auditRecords = ref([])          // newest-first audit trail
  const auditDays = ref([])
  const auditFilter = ref({ run_id: '', event: '', severity: '', decision: '', day: '' })
  const auditChain = ref(null)          // hash-chain verification result
  const pendingApprovals = ref([])      // approvals waiting for a human
  const approvalHistory = ref([])
  const currentRunId = ref('')          // run id of the latest request
  const currentVerification = ref(null) // goal-confirmation verdict
  const safetyBusy = ref(false)
  const reviewProbe = ref(null)         // ad-hoc /safety/review result

  // Dynamic agent emoji map (built-in + skills) for ThinkingPanel
  const agentEmojiMap = computed(() => {
    const map = {
      research: '🔍', analyst: '🔢', chart: '📊',
      image_gen: '🎨', video_gen: '🎬', code: '💻',
      reviewer: '🛡️', verifier: '✅',
    }
    for (const skill of skills.value) {
      if (skill.enabled) {
        map[skill.name] = skill.emoji || '🔧'
      }
    }
    return map
  })

  const allModels = computed(() => ({
    chat: chatModels.value, image: imageModels.value, video: videoModels.value,
  }))

  const modelsLoading = ref(false)

  async function loadModels() {
    try {
      const [chat, img, vid] = await Promise.all([
        fetchChatModels(), fetchImageModels(), fetchVideoModels(),
      ])
      chatModels.value = chat; imageModels.value = img; videoModels.value = vid
      // Only default if nothing saved; fallback if saved model disappeared
      if (chat.length && !selectedChatModel.value) selectedChatModel.value = chat[0].id
      else if (chat.length && !chat.find(m => m.id === selectedChatModel.value)) selectedChatModel.value = chat[0].id
      if (img.length && !selectedImageModel.value) selectedImageModel.value = img[0].id
      if (vid.length && !selectedVideoModel.value) selectedVideoModel.value = vid[0].id
    } catch (e) {
      console.error('Failed to load models:', e)
    }

    // Persist selections
    watch(selectedChatModel, v => saveSetting('chatModel', v))
    watch(selectedImageModel, v => saveSetting('imageModel', v))
    watch(selectedVideoModel, v => saveSetting('videoModel', v))
  }

  async function doRefreshModels() {
    modelsLoading.value = true
    try {
      const result = await refreshModels()
      await loadModels()
      return result
    } finally {
      modelsLoading.value = false
    }
  }

  async function loadSkills() {
    try {
      const result = await fetchSkills()
      skills.value = result.skills || []
      enabledSkills.value = result.skills?.filter(s => s.enabled) || []
    } catch (e) {
      console.error('Failed to load skills:', e)
    }
  }

  async function doToggleSkill(name, enabled) {
    try {
      await toggleSkill(name, enabled)
      await loadSkills()
    } catch (e) {
      console.error('Failed to toggle skill:', e)
    }
  }

  async function doInstallPackage(file) {
    const result = await installSkillPackage(file)
    await loadSkills()
    return result
  }

  async function doUninstallPackage(name) {
    const result = await uninstallSkillPackage(name)
    await loadSkills()
    return result
  }

  // ── Provider actions ────────────────────────────────────────────

  async function loadProviders() {
    try {
      providers.value = await fetchProviders()
    } catch (e) {
      console.error('Failed to load providers:', e)
    }
  }

  async function doSaveProvider(providerData, isNew) {
    if (isNew) {
      await createProvider(providerData)
    } else {
      await updateProvider(providerData.name, providerData)
    }
    await loadProviders()
    await doRefreshModels()
  }

  async function doDeleteProvider(name) {
    await deleteProvider(name)
    await loadProviders()
    await doRefreshModels()
  }

  // ── PPT actions ─────────────────────────────────────────────────

  async function doGeneratePPT(topic, options = {}) {
    if (!topic.trim() || pptGenerating.value) return

    const {
      theme = 'dark-tech',
      slideCount = 6,
      purpose = '展示',
      chatModelId = selectedChatModel.value,
    } = options

    pptGenerating.value = true
    pptStreamText.value = ''
    pptSteps.value = []
    pptAbortController = new AbortController()

    let finalHtmlResults = []
    let currentResponse = ''

    try {
      await streamPPT({
        topic,
        theme,
        slideCount,
        purpose,
        chatModelId,
        abortSignal: pptAbortController.signal,
        onEvent: (eventType, data) => {
          switch (eventType) {
            case 'phase':
              pptSteps.value.push({
                type: 'phase',
                phase: data.phase,
                message: data.message,
              })
              break
            case 'plan':
              pptSteps.value.push({
                type: 'plan',
                tasks: data.tasks,
                count: data.count,
              })
              break
            case 'agent_start':
              pptSteps.value.push({
                type: 'agent_start',
                agent: data.agent,
                agentType: data.agent_type,
                label: data.label,
                task: data.task,
                slideIndex: data.slide_index,
                done: false,
              })
              break
            case 'agent_done':
              // Mark matching agent_start as done
              for (let i = pptSteps.value.length - 1; i >= 0; i--) {
                const s = pptSteps.value[i]
                if (s.type === 'agent_start' && s.agent === data.agent && !s.done) {
                  pptSteps.value[i] = { ...s, done: true, summary: data.summary }
                  break
                }
              }
              break
            case 'token':
              currentResponse += data.text || ''
              pptStreamText.value = currentResponse
              break
            case 'final':
              if (!currentResponse || currentResponse.trim().length === 0) {
                currentResponse = data.response || ''
                pptStreamText.value = currentResponse
              }
              finalHtmlResults = data.html_results || []
              break
            case 'error':
              currentResponse = `Error: ${data.error}`
              pptStreamText.value = currentResponse
              break
          }
        },
      })

      pptResults.value.push({
        topic,
        theme,
        slideCount,
        htmlResults: finalHtmlResults,
        response: currentResponse,
        steps: [...pptSteps.value],
        timestamp: Date.now(),
      })
    } catch (e) {
      if (e.name !== 'AbortError') {
        pptResults.value.push({
          topic,
          error: e.message,
          timestamp: Date.now(),
        })
      }
    } finally {
      pptGenerating.value = false
      pptAbortController = null
      // Keep pptStreamText + pptSteps visible for a moment after completion,
      // then clear when next generation starts
    }
  }

  function stopPPTGeneration() {
    if (pptAbortController) pptAbortController.abort()
  }

  // ── Scholar Note actions ───────────────────────────────────────

  async function doGenerateNote(topic, options = {}) {
    if (!topic.trim() || noteGenerating.value) return

    const {
      style = 'a',
      chatModelId = selectedChatModel.value,
    } = options

    noteGenerating.value = true
    noteStreamText.value = ''
    noteSteps.value = []
    noteAbortController = new AbortController()

    let finalHtmlResults = []
    let currentResponse = ''

    try {
      await streamNote({
        topic,
        style,
        chatModelId,
        abortSignal: noteAbortController.signal,
        onEvent: (eventType, data) => {
          switch (eventType) {
            case 'phase':
              noteSteps.value.push({ type: 'phase', phase: data.phase, message: data.message })
              break
            case 'plan':
              noteSteps.value.push({ type: 'plan', tasks: data.tasks, count: data.count })
              break
            case 'agent_start':
              noteSteps.value.push({
                type: 'agent_start', agent: data.agent, agentType: data.agent_type,
                label: data.label, task: data.task, done: false,
              })
              break
            case 'agent_done':
              for (let i = noteSteps.value.length - 1; i >= 0; i--) {
                const s = noteSteps.value[i]
                if (s.type === 'agent_start' && s.agent === data.agent && !s.done) {
                  noteSteps.value[i] = { ...s, done: true, summary: data.summary }
                  break
                }
              }
              break
            case 'token':
              currentResponse += data.text || ''
              noteStreamText.value = currentResponse
              break
            case 'final':
              if (!currentResponse || currentResponse.trim().length === 0) {
                currentResponse = data.response || ''
                noteStreamText.value = currentResponse
              }
              finalHtmlResults = data.html_results || []
              break
            case 'error':
              currentResponse = `Error: ${data.error}`
              noteStreamText.value = currentResponse
              break
          }
        },
      })

      noteResults.value.push({
        topic, style,
        htmlResults: finalHtmlResults,
        response: currentResponse,
        steps: [...noteSteps.value],
        timestamp: Date.now(),
      })
    } catch (e) {
      if (e.name !== 'AbortError') {
        noteResults.value.push({ topic, error: e.message, timestamp: Date.now() })
      }
    } finally {
      noteGenerating.value = false
      noteAbortController = null
    }
  }

  function stopNoteGeneration() {
    if (noteAbortController) noteAbortController.abort()
  }

  function addThinkStep(step) {
    thinkSteps.value.push({ ...step, timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) })
  }

  function finishThinkStep(data) {
    const idx = [...thinkSteps.value].reverse().findIndex(
      s => s.type === 'agent_start' && s.agent === data.agent && !s.done
    )
    if (idx >= 0) {
      const realIdx = thinkSteps.value.length - 1 - idx
      thinkSteps.value[realIdx] = {
        ...thinkSteps.value[realIdx],
        done: true,
        status: data.status || 'done',
        blocked: data.status === 'blocked',
        review: data.review || null,
        attempts: data.attempts || 1,
        summary: data.summary || (data.status === 'blocked' ? '已被安全审查拦截' : '完成'),
        resultKey: data.result_key || '',
        resultCount: data.count || 0,
        doneAt: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
      }
    } else {
      addThinkStep({ type: 'agent_done', ...data })
    }
  }

  // ── Safety actions ──────────────────────────────────────────────

  async function loadSafetyStatus() {
    try {
      const status = await fetchSafetyStatus()
      safetyStatus.value = status
      safetySettings.value = status.settings || {}
    } catch (e) {
      console.error('Failed to load safety status:', e)
    }
  }

  async function loadSafetyPolicy() {
    try {
      safetyPolicy.value = await fetchSafetyPolicy()
    } catch (e) {
      console.error('Failed to load safety policy:', e)
    }
  }

  async function doUpdateSafetySettings(changes) {
    safetyBusy.value = true
    try {
      safetySettings.value = await updateSafetySettings(changes)
      await loadSafetyStatus()
      return safetySettings.value
    } finally {
      safetyBusy.value = false
    }
  }

  async function loadAudit(params = null) {
    safetyBusy.value = true
    try {
      const query = { ...auditFilter.value, limit: 200, ...(params || {}) }
      Object.keys(query).forEach(k => { if (!query[k]) delete query[k] })
      const result = await fetchAuditTrail(query)
      auditRecords.value = result.records || []
      auditDays.value = result.days || []
      return result
    } catch (e) {
      console.error('Failed to load audit trail:', e)
      return { records: [] }
    } finally {
      safetyBusy.value = false
    }
  }

  async function loadRunAudit(runId) {
    if (!runId) return { records: [] }
    safetyBusy.value = true
    try {
      const result = await fetchRunAudit(runId)
      auditRecords.value = result.records || []
      return result
    } catch (e) {
      console.error('Failed to load run audit:', e)
      return { records: [] }
    } finally {
      safetyBusy.value = false
    }
  }

  async function doVerifyAudit(day = '') {
    safetyBusy.value = true
    try {
      auditChain.value = await verifyAuditChain(day)
      return auditChain.value
    } catch (e) {
      console.error('Failed to verify audit chain:', e)
      return null
    } finally {
      safetyBusy.value = false
    }
  }

  async function loadApprovals() {
    try {
      const result = await fetchApprovals()
      pendingApprovals.value = result.pending || []
      approvalHistory.value = result.history || []
    } catch (e) {
      console.error('Failed to load approvals:', e)
    }
  }

  async function doDecideApproval(approvalId, approved, note = '') {
    // Resolve locally first so the timeline reacts immediately; the run also
    // gets an approval_resolved event when the wait completes.
    for (let i = pendingApprovals.value.length - 1; i >= 0; i--) {
      if (pendingApprovals.value[i].id === approvalId) {
        const step = {
          type: 'approval',
          id: approvalId,
          status: approved ? 'approved' : 'rejected',
          risk: pendingApprovals.value[i].risk,
          summary: pendingApprovals.value[i].summary,
          reasons: pendingApprovals.value[i].reasons || [],
          decidedBy: 'user',
        }
        const existing = thinkSteps.value.find(
          s => s.type === 'approval' && s.id === approvalId && s.status === 'pending'
        )
        if (existing) Object.assign(existing, step)
        else thinkSteps.value.push(step)
        pendingApprovals.value.splice(i, 1)
        break
      }
    }
    const result = await decideApproval(approvalId, approved, note)
    await loadApprovals()
    return result
  }

  async function doReviewText(text, stage = 'action', agent = '') {
    safetyBusy.value = true
    try {
      reviewProbe.value = await reviewText({ text, stage, agent })
      return reviewProbe.value
    } catch (e) {
      reviewProbe.value = { ok: false, error: e.message }
      return reviewProbe.value
    } finally {
      safetyBusy.value = false
    }
  }

  // ---- Session management ----

  async function loadSessions() {
    try {
      const result = await fetchSessions()
      sessions.value = result.sessions || []
      // Restore last session if available
      const lastId = loadLocalSessionId()
      if (lastId && sessions.value.find(s => s.id === lastId)) {
        currentSessionId.value = lastId
        // Load messages from localStorage (fast, works offline)
        const localMsgs = loadLocalMessages()
        if (localMsgs.length > 0) {
          messages.value = localMsgs
        } else {
          // Fallback: load from backend
          const session = await fetchSession(lastId)
          if (session && !session.error) {
            messages.value = session.messages || []
          }
        }
      }
    } catch (e) {
      console.error('Failed to load sessions:', e)
      // Fallback: load from localStorage only
      messages.value = loadLocalMessages()
      currentSessionId.value = loadLocalSessionId()
    }
  }

  async function newSession() {
    currentSessionId.value = ''
    messages.value = []
    thinkSteps.value = []
    currentResponse.value = ''
    saveLocalMessages([])
    saveLocalSessionId('')
    // Backend session will be created on first message
  }

  async function switchSession(id) {
    if (id === currentSessionId.value) return
    currentSessionId.value = id
    saveLocalSessionId(id)
    try {
      const session = await fetchSession(id)
      if (session && !session.error) {
        messages.value = session.messages || []
        saveLocalMessages(messages.value)
      }
    } catch (e) {
      console.error('Failed to load session:', e)
    }
  }

  async function deleteSession(id) {
    if (!id) return
    try {
      await deleteSessionApi(id)
    } catch (e) {
      console.error('Failed to delete session:', e)
    }
    sessions.value = sessions.value.filter(s => s.id !== id)
    if (currentSessionId.value === id) {
      await newSession()
    }
  }

  async function deleteCurrentSession() {
    await deleteSession(currentSessionId.value)
  }

  async function _ensureSession() {
    // Auto-create backend session if needed (await so session_id is ready before message sent)
    if (!currentSessionId.value) {
      try {
        const res = await createSession()
        if (res && res.session_id) {
          currentSessionId.value = res.session_id
          saveLocalSessionId(res.session_id)
        }
      } catch (e) {
        console.error('Failed to create session:', e)
      }
    }
  }

  function _persistMessages() {
    // Save to localStorage always
    saveLocalMessages(messages.value)
    // Save to backend if we have a session
    if (currentSessionId.value && messages.value.length > 0) {
      saveSession(currentSessionId.value, messages.value).catch(() => {})
      // Refresh session list (title may have been updated)
      if (messages.value.length <= 2) {
        loadSessions()
      }
    }
  }

  async function sendChatMessage(text) {
    if (!text.trim() || isLoading.value) return

    messages.value.push({ role: 'user', content: text, timestamp: Date.now() })

    isLoading.value = true
    thinkSteps.value = []
    currentResponse.value = ''
    currentVerification.value = null
    pendingApprovals.value = []

    // Capture chart/image/html results from final SSE event
    let finalChartResults = []
    let finalImageResults = []
    let finalHtmlResults = []
    let finalSafety = null
    let finalVerification = null
    let finalRunId = ''
    let finalUsage = null

    // Create abort controller for stop button
    abortController = new AbortController()

    try {
      await _ensureSession()
      await sendMessage({
        message: text,
        chatModelId: selectedChatModel.value,
        imageModelId: selectedImageModel.value,
        videoModelId: selectedVideoModel.value,
        sessionId: currentSessionId.value,
        history: messages.value.slice(-20),
        stream: true,
        abortSignal: abortController.signal,
        onEvent: (eventType, data) => {
          switch (eventType) {
            case 'phase':
              addThinkStep({ type: 'phase', phase: data.phase, message: data.message })
              break
            case 'plan':
              if (data.run_id) currentRunId.value = data.run_id
              addThinkStep({
                type: 'plan',
                tasks: data.tasks,
                count: data.count,
                steps: data.steps || [],  // 🆕 Plan Mode steps with deps & reasons
                dropped: data.dropped || [],
              })
              break
            case 'agent_start':
              addThinkStep({
                type: 'agent_start',
                agent: data.agent,
                agentType: data.agent_type,
                label: data.label,
                task: data.task,
                taskCount: data.task_count || 0,
                done: false,
              })
              break
            case 'agent_done':
              finishThinkStep(data)
              break
            case 'agent_retry':
              addThinkStep({
                type: 'agent_retry',
                agent: data.agent,
                agentType: data.agent_type,
                label: data.label,
                taskId: data.task_id,
                attempt: data.attempt,
                maxAttempts: data.max_attempts,
                kind: data.kind,
                reason: data.reason,
                backoffSeconds: data.backoff_seconds || 0,
              })
              break
            case 'token':
              // Incremental token streaming from synthesizer
              currentResponse.value += data.text || ''
              break
            // ── Safety layer ──
            case 'safety_review':
              addThinkStep({
                type: 'safety_review',
                gate: data.gate,
                stage: data.stage,
                decision: data.decision,
                intended: data.intended_decision,
                risk: data.risk,
                summary: data.summary,
                reasons: data.reasons || [],
                findings: data.findings || [],
              })
              break
            case 'approval_required': {
              pendingApprovals.value.push(data)
              addThinkStep({
                type: 'approval',
                id: data.id,
                status: 'pending',
                risk: data.risk,
                stage: data.stage,
                summary: data.summary,
                reasons: data.reasons || [],
                task: data.task,
                agent: data.agent,
              })
              break
            }
            case 'approval_resolved': {
              const idx = pendingApprovals.value.findIndex(a => a.id === data.id)
              if (idx >= 0) pendingApprovals.value.splice(idx, 1)
              const existing = thinkSteps.value.find(
                s => s.type === 'approval' && s.id === data.id
              )
              const patch = {
                status: data.status,
                decidedBy: data.decided_by,
                note: data.note,
              }
              if (existing) Object.assign(existing, patch)
              else addThinkStep({ type: 'approval', id: data.id, risk: data.risk, ...patch })
              break
            }
            case 'verification':
              currentVerification.value = data
              addThinkStep({ type: 'verification', verdict: data })
              break
            case 'remediation':
              addThinkStep({
                type: 'remediation',
                round: data.round,
                tasks: data.tasks || [],
              })
              break
            case 'budget_exceeded':
              addThinkStep({
                type: 'budget',
                reason: data.reason,
                budget: data.budget,
              })
              break
            case 'final':
              // Accumulate tokens already handled via 'token' events;
              // only use final.response as fallback when no tokens arrived.
              if (!currentResponse.value || currentResponse.value.trim().length === 0) {
                currentResponse.value = data.response || ''
              }
              finalChartResults = data.chart_results || []
              finalImageResults = data.image_results || []
              finalHtmlResults = data.html_results || []
              finalSafety = data.safety || null
              finalVerification = data.verification || currentVerification.value
              finalRunId = data.run_id || currentRunId.value
              finalUsage = data.usage || null
              break
            case 'error':
              currentResponse.value = `**Error:** ${data.error}`
              break
          }
        },
      })

      // Push completed message with chart/image results for rendering
      const finalContent = currentResponse.value || '(no response)'
      messages.value.push({
        role: 'assistant',
        content: finalContent,
        timestamp: Date.now(),
        thinkSteps: [...thinkSteps.value],
        chart_results: finalChartResults,
        image_results: finalImageResults,
        html_results: finalHtmlResults,
        run_id: finalRunId,
        safety: finalSafety,
        verification: finalVerification,
        usage: finalUsage,
      })
    } catch (e) {
      if (e.name === 'AbortError') {
        // User stopped — save whatever we got
        const partial = currentResponse.value || '(已停止)'
        messages.value.push({
          role: 'assistant',
          content: partial,
          timestamp: Date.now(),
          thinkSteps: [...thinkSteps.value],
          stopped: true,
        })
      } else {
        messages.value.push({
          role: 'assistant',
          content: `**Error:** ${e.message}`,
          timestamp: Date.now(),
          isError: true,
        })
      }
    } finally {
      isLoading.value = false
      abortController = null
      _persistMessages()
    }
  }

  function stopGeneration() {
    if (abortController) {
      abortController.abort()
    }
  }

  async function doGenerateImage(prompt, modelId = '', opts = {}) {
    isLoading.value = true
    try {
      const result = await generateImage({
        prompt, modelId: modelId || selectedImageModel.value,
        negativePrompt: opts.negativePrompt || '',
        width: opts.width || 1024, height: opts.height || 1024, steps: opts.steps || 20,
      })
      imageResults.value.push({ prompt, result })
      return result
    } finally {
      isLoading.value = false
    }
  }

  async function doGenerateVideo(prompt, modelId = '', duration = 5) {
    isLoading.value = true
    try {
      const result = await generateVideo({ prompt, modelId: modelId || selectedVideoModel.value, duration })
      videoResults.value.push({ prompt, result })
      return result
    } finally {
      isLoading.value = false
    }
  }

  function clearChat() {
    messages.value = []
    thinkSteps.value = []
    currentResponse.value = ''
  }

  return {
    messages, isLoading, thinkSteps, currentResponse,
    chatModels, imageModels, videoModels,
    selectedChatModel, selectedImageModel, selectedVideoModel,
    activeTab, imageResults, videoResults, allModels,
    skills, enabledSkills, agentEmojiMap,
    sessions, currentSessionId,
    providers, pptResults, pptGenerating, pptStreamText, pptSteps,
    noteResults, noteGenerating, noteStreamText, noteSteps,
    modelsLoading,
    // Safety / execution review
    safetyStatus, safetyPolicy, safetySettings, auditRecords, auditDays,
    auditFilter, auditChain, pendingApprovals, approvalHistory,
    currentRunId, currentVerification, safetyBusy, reviewProbe,
    loadModels, doRefreshModels, sendChatMessage, stopGeneration,
    doGenerateImage, doGenerateVideo, clearChat,
    loadSkills, doToggleSkill, doInstallPackage, doUninstallPackage,
    loadSessions, newSession, switchSession, deleteSession, deleteCurrentSession,
    loadProviders, doSaveProvider, doDeleteProvider,
    doGeneratePPT, stopPPTGeneration,
    doGenerateNote, stopNoteGeneration,
    loadSafetyStatus, loadSafetyPolicy, doUpdateSafetySettings,
    loadAudit, loadRunAudit, doVerifyAudit,
    loadApprovals, doDecideApproval, doReviewText,
  }
})
