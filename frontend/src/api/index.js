import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 300000,
})

export async function fetchProviders() {
  const { data } = await api.get('/providers')
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

export async function sendMessage({
  message,
  chatModelId = '',
  imageModelId = '',
  videoModelId = '',
  stream = false,
  onEvent = null,
  abortSignal = null,
}) {
  if (stream && onEvent) {
    return await streamChat({ message, chatModelId, imageModelId, videoModelId, onEvent, abortSignal })
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

async function streamChat({ message, chatModelId, imageModelId, videoModelId, onEvent, abortSignal }) {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      chat_model_id: chatModelId,
      image_model_id: imageModelId,
      video_model_id: videoModelId,
      stream: true,
    }),
    signal: abortSignal,
  })

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
