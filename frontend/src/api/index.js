import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 300000,
})

export async function fetchProviders() {
  const { data } = await api.get('/providers')
  return data
}

export async function createProvider({ name, type, baseUrl, apiKey }) {
  const { data } = await api.post('/providers', {
    name, type, base_url: baseUrl, api_key: apiKey,
  })
  return data
}

export async function updateProvider(name, { type, baseUrl, apiKey }) {
  const { data } = await api.put(`/providers/${encodeURIComponent(name)}`, {
    type, base_url: baseUrl, api_key: apiKey,
  })
  return data
}

export async function deleteProvider(name) {
  const { data } = await api.delete(`/providers/${encodeURIComponent(name)}`)
  return data
}

export async function fetchModels(type = '') {
  const { data } = await api.get('/models', { params: { type } })
  return data
}

export async function fetchChatModels() {
  const { data } = await api.get('/models/chat')
  return data
}

export async function fetchImageModels() {
  const { data } = await api.get('/models/image')
  return data
}

export async function fetchVideoModels() {
  const { data } = await api.get('/models/video')
  return data
}

export async function refreshModels() {
  const { data } = await api.post('/refresh-models')
  return data
}

// ── Shared SSE stream reader ──────────────────────────────────────────

async function readSSEStream(response, onEvent) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let currentEvent = 'message'

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7).trim()
      } else if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6))
          onEvent(currentEvent, data)
        } catch {}
        currentEvent = 'message'
      }
    }
  }
}


// ── Chat ──────────────────────────────────────────────────────────────

export async function sendMessage({
  message,
  chatModelId = '',
  imageModelId = '',
  videoModelId = '',
  sessionId = '',
  history = [],
  stream = false,
  onEvent = null,
  abortSignal = null,
}) {
  if (stream && onEvent) {
    return await streamChat({ message, chatModelId, imageModelId, videoModelId, sessionId, history, onEvent, abortSignal })
  }
  const { data } = await api.post('/chat/sync', {
    message,
    chat_model_id: chatModelId,
    image_model_id: imageModelId,
    video_model_id: videoModelId,
    stream: false,
  })
  return data
}

async function streamChat({ message, chatModelId, imageModelId, videoModelId, sessionId, history, onEvent, abortSignal }) {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      chat_model_id: chatModelId,
      image_model_id: imageModelId,
      video_model_id: videoModelId,
      session_id: sessionId || '',
      history: history || [],
      stream: true,
    }),
    signal: abortSignal,
  })
  return await readSSEStream(response, onEvent)
}


// ── PPT ───────────────────────────────────────────────────────────────

export async function streamPPT({ topic, theme, slideCount, purpose, chatModelId, onEvent, abortSignal }) {
  const response = await fetch('/api/generate-ppt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      topic,
      theme,
      slide_count: slideCount,
      purpose,
      chat_model_id: chatModelId,
      stream: true,
    }),
    signal: abortSignal,
  })
  return await readSSEStream(response, onEvent)
}


// ── Scholar Notes ─────────────────────────────────────────────────────

export async function streamNote({ topic, style, chatModelId, onEvent, abortSignal }) {
  const response = await fetch('/api/generate-note', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      topic,
      style,
      chat_model_id: chatModelId,
      stream: true,
    }),
    signal: abortSignal,
  })
  return await readSSEStream(response, onEvent)
}

export async function generateImage({ prompt, modelId = '', negativePrompt = '', width = 1024, height = 1024, steps = 20 }) {
  const { data } = await api.post('/generate-image', {
    prompt, model_id: modelId, negative_prompt: negativePrompt,
    width, height, steps,
  })
  return data
}

export async function generateVideo({ prompt, modelId = '', duration = 5 }) {
  const { data } = await api.post('/generate-video', { prompt, model_id: modelId, duration })
  return data
}

export async function fetchSkills() {
  const { data } = await api.get('/skills')
  return data
}

export async function toggleSkill(name, enabled) {
  const { data } = await api.post(`/skills/${encodeURIComponent(name)}/toggle`, null, {
    params: { enabled },
  })
  return data
}

export async function installSkillPackage(file) {
  const formData = new FormData()
  formData.append('file', file)
  const { data } = await api.post('/skills/packages/install', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return data
}

export async function uninstallSkillPackage(name) {
  const { data } = await api.delete(`/skills/packages/${encodeURIComponent(name)}`)
  return data
}

export async function fetchSkillPackages() {
  const { data } = await api.get('/skills/packages')
  return data
}

export async function fetchSkillReadme(name) {
  const { data } = await api.get(`/skills/${encodeURIComponent(name)}/readme`)
  return data
}

export async function exportPptx({
  htmlContent = '',
  htmlUrl = '',
  title = '',
  mode = 'final',
  framesPerSlide = 3,
}) {
  const { data } = await api.post('/skills/ppt-animation/export-pptx', {
    html_content: htmlContent,
    html_url: htmlUrl,
    title,
    mode,
    frames_per_slide: framesPerSlide,
  })
  return data
}

// ---- Session API ----

export async function createSession() {
  const { data } = await api.post('/sessions')
  return data
}

export async function fetchSessions() {
  const { data } = await api.get('/sessions')
  return data
}

export async function fetchSession(id) {
  const { data } = await api.get(`/sessions/${encodeURIComponent(id)}`)
  return data
}

export async function saveSession(id, messages) {
  const { data } = await api.put(`/sessions/${encodeURIComponent(id)}`, { messages })
  return data
}

export async function deleteSessionApi(id) {
  const { data } = await api.delete(`/sessions/${encodeURIComponent(id)}`)
  return data
}
