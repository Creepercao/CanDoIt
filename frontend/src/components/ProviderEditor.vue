<script setup>
import { ref, reactive } from 'vue'
import { useChatStore } from '../stores/chat.js'

const store = useChatStore()

const loading = ref(false)
const error = ref('')
const editing = ref(null)              // provider name being edited, or null
const form = reactive({                // add/edit form
  name: '',
  type: 'llm',
  baseUrl: '',
  apiKey: '',
})

const types = [
  { value: 'llm', label: 'LLM (Chat)' },
  { value: 'image', label: 'Image' },
  { value: 'video', label: 'Video' },
  { value: 'search', label: 'Search' },
]

function resetForm() {
  form.name = ''
  form.type = 'llm'
  form.baseUrl = ''
  form.apiKey = ''
  editing.value = null
}

function startEdit(provider) {
  form.name = provider.name
  form.type = provider.type
  form.baseUrl = provider.base_url
  form.apiKey = provider.api_key_masked  // show masked value
  editing.value = provider.name
}

async function save() {
  if (!form.name.trim() || !form.baseUrl.trim()) return
  loading.value = true
  error.value = ''
  try {
    await store.doSaveProvider({
      name: form.name.trim(),
      type: form.type,
      baseUrl: form.baseUrl.trim(),
      apiKey: form.apiKey.trim(),
    }, !editing.value)
    resetForm()
  } catch (e) {
    error.value = e.message || 'Save failed'
  } finally {
    loading.value = false
  }
}

async function remove(name) {
  if (!confirm(`Delete provider "${name}"?`)) return
  loading.value = true
  error.value = ''
  try {
    await store.doDeleteProvider(name)
  } catch (e) {
    error.value = e.message || 'Delete failed'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="flex flex-col h-full">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700/50 shrink-0">
      <h2 class="font-semibold text-lg text-white">⚙️ Providers</h2>
      <button
        @click="store.doRefreshModels()"
        class="text-xs px-3 py-1.5 bg-surface-800 hover:bg-surface-700 border border-gray-600 rounded-lg text-gray-300 transition-colors"
      >
        🔄 Refresh Models
      </button>
    </div>

    <!-- Content -->
    <div class="flex-1 overflow-y-auto p-4 space-y-4">
      <!-- Error banner -->
      <div v-if="error"
        class="bg-red-900/30 border border-red-700/50 rounded-lg px-4 py-2.5 text-sm text-red-300">
        {{ error }}
        <button @click="error = ''" class="float-right opacity-60 hover:opacity-100">&times;</button>
      </div>

      <!-- Add / Edit form -->
      <div class="bg-surface-800 rounded-xl border border-gray-700/50 p-4 space-y-3">
        <h3 class="text-sm font-medium text-gray-300">
          {{ editing ? `Edit: ${editing}` : 'Add Provider' }}
        </h3>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="text-xs text-gray-400 block mb-1">Name</label>
            <input
              v-model="form.name"
              :disabled="!!editing"
              placeholder="e.g. SiliconFlow"
              class="w-full bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 disabled:opacity-50 focus:border-primary-500 focus:outline-none"
            />
          </div>
          <div>
            <label class="text-xs text-gray-400 block mb-1">Type</label>
            <select
              v-model="form.type"
              class="w-full bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white focus:border-primary-500 focus:outline-none"
            >
              <option v-for="t in types" :key="t.value" :value="t.value">{{ t.label }}</option>
            </select>
          </div>
          <div class="sm:col-span-2">
            <label class="text-xs text-gray-400 block mb-1">Base URL</label>
            <input
              v-model="form.baseUrl"
              placeholder="https://api.example.com/v1"
              class="w-full bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-primary-500 focus:outline-none"
            />
          </div>
          <div class="sm:col-span-2">
            <label class="text-xs text-gray-400 block mb-1">
              API Key
              <span class="text-gray-500">({{ editing ? 'leave unchanged or type new' : 'required' }})</span>
            </label>
            <input
              v-model="form.apiKey"
              :placeholder="editing ? '•••••••• (unchanged)' : 'sk-...'"
              class="w-full bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-primary-500 focus:outline-none"
            />
          </div>
        </div>

        <div class="flex items-center gap-2 pt-1">
          <button
            @click="save"
            :disabled="loading || !form.name.trim() || !form.baseUrl.trim()"
            class="px-4 py-2 bg-primary-600 hover:bg-primary-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm rounded-lg font-medium transition-colors"
          >
            {{ loading ? 'Saving...' : (editing ? 'Update' : 'Add') }}
          </button>
          <button
            v-if="editing"
            @click="resetForm"
            class="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 transition-colors"
          >
            Cancel
          </button>
        </div>
      </div>

      <!-- Provider list -->
      <div v-if="store.providers.length === 0 && !loading"
        class="text-center py-8 text-gray-500 text-sm">
        No providers configured. Add one above.
      </div>

      <div class="grid gap-3 sm:grid-cols-1 lg:grid-cols-2">
        <div
          v-for="p in store.providers"
          :key="p.name"
          class="bg-surface-800 rounded-xl border border-gray-700/50 p-4 space-y-2"
        >
          <div class="flex items-center justify-between">
            <span class="font-medium text-white text-sm">{{ p.name }}</span>
            <span
              class="text-[10px] px-2 py-0.5 rounded-full font-medium uppercase"
              :class="{
                'bg-blue-900/50 text-blue-300 border border-blue-700/50': p.type === 'llm',
                'bg-purple-900/50 text-purple-300 border border-purple-700/50': p.type === 'image',
                'bg-amber-900/50 text-amber-300 border border-amber-700/50': p.type === 'video',
                'bg-emerald-900/50 text-emerald-300 border border-emerald-700/50': p.type === 'search',
              }"
            >
              {{ p.type }}
            </span>
          </div>
          <div class="text-xs text-gray-400 truncate">{{ p.base_url }}</div>
          <div class="text-xs text-gray-500 font-mono">{{ p.api_key_masked }}</div>
          <div class="flex items-center gap-2 pt-1">
            <button
              @click="startEdit(p)"
              class="text-xs px-3 py-1 bg-surface-700 hover:bg-surface-600 border border-gray-600 rounded-lg text-gray-300 transition-colors"
            >
              Edit
            </button>
            <button
              @click="remove(p.name)"
              class="text-xs px-3 py-1 bg-red-900/20 hover:bg-red-900/40 border border-red-700/40 rounded-lg text-red-400 transition-colors"
            >
              Delete
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
