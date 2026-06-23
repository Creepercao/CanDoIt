import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import {
  fetchChatModels, fetchImageModels, fetchVideoModels,
  refreshModels, sendMessage, generateImage, generateVideo,
  fetchSkills, toggleSkill,
} from '../api/index.js'

// localStorage helpers
function loadSetting(key, fallback = '') {
  try { return localStorage.getItem('multiagent_' + key) || fallback } catch { return fallback }
}
function saveSetting(key, val) {
  try { localStorage.setItem('multiagent_' + key, val) } catch {}
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

  // Skills
  const skills = ref([])
  const enabledSkills = ref([])

  // Dynamic agent emoji map (built-in + skills) for ThinkingPanel
  const agentEmojiMap = computed(() => {
    const map = {
      research: '🔍', analyst: '🔢', chart: '📊',
      image_gen: '🎨', video_gen: '🎬', code: '💻',
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
    await refreshModels()
    await loadModels()
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

  function addThinkStep(step) {
    thinkSteps.value.push({ ...step, timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) })
  }

  async function sendChatMessage(text) {
    if (!text.trim() || isLoading.value) return

    messages.value.push({ role: 'user', content: text, timestamp: Date.now() })

    isLoading.value = true
    thinkSteps.value = []
    currentResponse.value = ''

    // Capture chart/image results from final SSE event
    let finalChartResults = []
    let finalImageResults = []

    // Create abort controller for stop button
    abortController = new AbortController()

    try {
      await sendMessage({
        message: text,
        chatModelId: selectedChatModel.value,
        imageModelId: selectedImageModel.value,
        videoModelId: selectedVideoModel.value,
        stream: true,
        abortSignal: abortController.signal,
        onEvent: (eventType, data) => {
          switch (eventType) {
            case 'phase':
              addThinkStep({ type: 'phase', phase: data.phase, message: data.message })
              break
            case 'plan':
              addThinkStep({ type: 'plan', tasks: data.tasks, count: data.count })
              break
            case 'agent_start':
              addThinkStep({ type: 'agent_start', agent: data.agent, label: data.label, task: data.task, index: data.index })
              break
            case 'agent_done':
              addThinkStep({ type: 'agent_done', agent: data.agent, label: data.label })
              break
            case 'token':
              // Incremental token streaming from synthesizer
              currentResponse.value += data.text || ''
              break
            case 'final':
              // Accumulate tokens already handled via 'token' events;
              // only use final.response as fallback when no tokens arrived.
              if (!currentResponse.value || currentResponse.value.trim().length === 0) {
                currentResponse.value = data.response || ''
              }
              finalChartResults = data.chart_results || []
              finalImageResults = data.image_results || []
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
    loadModels, doRefreshModels, sendChatMessage, stopGeneration,
    doGenerateImage, doGenerateVideo, clearChat,
    loadSkills, doToggleSkill,
  }
})
