<script setup>
import { ref, computed, onMounted } from 'vue'
import { useChatStore } from '../stores/chat.js'
import { fetchSkillReadme } from '../api/index.js'

const store = useChatStore()

const loading = ref(false)
const error = ref('')
const toggling = ref(new Set())
const uploading = ref(false)
const uploadResult = ref(null)
const expandedReadme = ref({})    // { skillName: { loading, content } }
const uninstalling = ref(new Set())

const totalCount = computed(() => store.skills.length)
const enabledCount = computed(() => store.skills.filter(s => s.enabled).length)

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    await store.loadSkills()
  } catch (e) {
    error.value = e.message || 'Failed to load skills'
  } finally {
    loading.value = false
  }
}

async function toggle(skill) {
  const newEnabled = !skill.enabled
  toggling.value.add(skill.name)
  try {
    await store.doToggleSkill(skill.name, newEnabled)
  } catch (e) {
    error.value = `切换 ${skill.display_name} 失败: ${e.message}`
  } finally {
    toggling.value.delete(skill.name)
  }
}

async function uploadFile(file) {
  uploading.value = true
  uploadResult.value = null
  error.value = ''
  try {
    uploadResult.value = await store.doInstallPackage(file)
  } catch (e) {
    uploadResult.value = { errors: [e.message] }
  } finally {
    uploading.value = false
  }
}

function onFileChange(e) {
  const file = e.target.files?.[0]
  if (file) {
    uploadFile(file)
    e.target.value = ''  // reset so same file can be picked again
  }
}

async function toggleReadme(skill) {
  const name = skill.name
  if (expandedReadme.value[name]) {
    // Collapse
    delete expandedReadme.value[name]
    return
  }
  // Expand + fetch
  expandedReadme.value[name] = { loading: true, content: '' }
  try {
    const result = await fetchSkillReadme(name)
    expandedReadme.value[name] = {
      loading: false,
      content: result.content || result.error || 'No content',
    }
  } catch (e) {
    expandedReadme.value[name] = {
      loading: false,
      content: `Error: ${e.message}`,
    }
  }
}

async function confirmUninstall(skill) {
  if (!confirm(`确定卸载 "${skill.display_name}" 吗？\n\n此操作会删除技能目录且不可撤销。`)) return
  uninstalling.value.add(skill.name)
  try {
    await store.doUninstallPackage(skill.name)
  } catch (e) {
    error.value = `卸载 ${skill.display_name} 失败: ${e.message}`
  } finally {
    uninstalling.value.delete(skill.name)
  }
}

onMounted(() => {
  if (store.skills.length === 0) {
    refresh()
  }
})
</script>

<template>
  <div class="flex flex-col h-full">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700/50 shrink-0">
      <h2 class="font-semibold text-lg text-white">🔧 技能管理</h2>
      <div class="flex items-center gap-2">
        <!-- Upload button -->
        <label
          :class="[
            'text-xs text-gray-400 hover:text-white px-3 py-1.5 rounded-lg hover:bg-surface-700 transition-colors cursor-pointer',
            uploading ? 'opacity-50 cursor-wait' : '',
          ]"
        >
          {{ uploading ? '⏳ 安装中...' : '📦 安装' }}
          <input
            type="file"
            accept=".zip"
            class="hidden"
            @change="onFileChange"
            :disabled="uploading"
          />
        </label>
        <button
          @click="refresh"
          :disabled="loading"
          class="text-xs text-gray-400 hover:text-white px-3 py-1.5 rounded-lg hover:bg-surface-700 transition-colors disabled:opacity-50"
        >
          {{ loading ? '加载中...' : '🔄 刷新' }}
        </button>
      </div>
    </div>

    <!-- Install result banner -->
    <div
      v-if="uploadResult"
      :class="[
        'px-4 py-2.5 border-b shrink-0 text-sm',
        uploadResult.errors && uploadResult.errors.length > 0 && uploadResult.installed?.length === 0
          ? 'bg-red-900/30 border-red-500/20 text-red-400'
          : uploadResult.errors?.length
            ? 'bg-amber-900/20 border-amber-500/20 text-amber-400'
            : 'bg-green-900/20 border-green-500/20 text-green-400',
      ]"
    >
      <div class="flex items-center justify-between">
        <span>
          <template v-if="uploadResult.installed?.length">
            ✅ 已安装: <strong>{{ uploadResult.installed.map(i => i.display_name).join(', ') }}</strong>
            <template v-if="uploadResult.errors?.length">
              | ⚠️ {{ uploadResult.errors.join('; ') }}
            </template>
          </template>
          <template v-else>
            ❌ {{ uploadResult.errors?.join('; ') || '未知错误' }}
          </template>
        </span>
        <button @click="uploadResult = null" class="text-gray-500 hover:text-white ml-2">✕</button>
      </div>
    </div>

    <!-- Summary bar -->
    <div
      v-if="totalCount > 0"
      class="px-4 py-2.5 bg-surface-800/50 border-b border-gray-700/30 shrink-0"
    >
      <div class="flex items-center gap-4 text-sm">
        <span class="text-gray-400">
          共 <span class="text-white font-medium">{{ totalCount }}</span> 个技能
        </span>
        <span class="text-gray-600">|</span>
        <span class="text-green-400">
          <span class="text-white font-medium">{{ enabledCount }}</span> 已启用
        </span>
        <span class="text-gray-600">|</span>
        <span class="text-gray-500">
          {{ totalCount - enabledCount }} 已禁用
        </span>
      </div>
    </div>

    <!-- Scrollable body -->
    <div class="flex-1 overflow-y-auto p-4">
      <!-- Loading skeleton -->
      <div v-if="loading && totalCount === 0" class="grid gap-3 sm:grid-cols-1 lg:grid-cols-2">
        <div
          v-for="i in 3" :key="i"
          class="bg-surface-800 rounded-xl border border-gray-700/50 p-4 animate-pulse"
        >
          <div class="flex items-center gap-3">
            <div class="w-8 h-8 bg-surface-700 rounded-lg" />
            <div class="flex-1 space-y-2">
              <div class="h-4 bg-surface-700 rounded w-24" />
              <div class="h-3 bg-surface-700 rounded w-16" />
            </div>
            <div class="w-11 h-6 bg-surface-700 rounded-full" />
          </div>
          <div class="mt-3 space-y-2">
            <div class="h-3 bg-surface-700 rounded w-full" />
            <div class="h-3 bg-surface-700 rounded w-3/4" />
          </div>
          <div class="flex gap-2 mt-3">
            <div class="h-5 bg-surface-700 rounded-full w-12" />
            <div class="h-5 bg-surface-700 rounded-full w-16" />
          </div>
        </div>
      </div>

      <!-- Error -->
      <div v-else-if="error" class="text-red-400 text-sm bg-red-900/30 rounded-lg p-4">
        <p>{{ error }}</p>
        <button @click="refresh" class="mt-2 underline hover:text-red-300 text-xs">重试</button>
      </div>

      <!-- Empty -->
      <div
        v-else-if="totalCount === 0 && !loading"
        class="flex flex-col items-center justify-center py-20 text-center"
      >
        <span class="text-5xl mb-4">🔧</span>
        <p class="text-gray-400 text-sm mb-1">没有发现技能模块</p>
        <p class="text-gray-600 text-xs mb-4">上传标准 Skill 包或创建 Python 技能模块后刷新</p>
        <div class="flex items-center gap-3">
          <label
            class="text-sm text-primary-400 hover:text-primary-300 px-3 py-1.5 rounded-lg hover:bg-surface-800 transition-colors cursor-pointer"
          >
            📦 安装 Skill 包
            <input type="file" accept=".zip" class="hidden" @change="onFileChange" />
          </label>
          <button
            @click="refresh"
            class="text-sm text-gray-400 hover:text-white px-3 py-1.5 rounded-lg hover:bg-surface-800 transition-colors"
          >
            🔄 刷新
          </button>
        </div>
      </div>

      <!-- Skill cards -->
      <div v-else class="grid gap-3 sm:grid-cols-1 lg:grid-cols-2">
        <div
          v-for="skill in store.skills"
          :key="skill.name"
          :class="[
            'bg-surface-800 rounded-xl border transition-all p-4',
            skill.enabled
              ? skill.is_package ? 'border-purple-500/30' : 'border-green-500/30'
              : 'border-gray-700/50 opacity-70',
          ]"
        >
          <!-- Top row: emoji + info + toggle -->
          <div class="flex items-start justify-between gap-3">
            <div class="flex items-center gap-3 min-w-0">
              <span class="text-2xl shrink-0">{{ skill.emoji || '🔧' }}</span>
              <div class="min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                  <h3 class="font-medium text-white truncate">{{ skill.display_name }}</h3>
                  <span
                    v-if="skill.is_package"
                    class="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400 border border-purple-500/30 shrink-0"
                  >
                    v{{ skill.package_version || '?' }}
                  </span>
                  <span
                    v-if="skill.is_package"
                    class="text-[10px] px-1.5 py-0.5 rounded bg-cyan-500/15 text-cyan-400 border border-cyan-500/30 shrink-0"
                  >
                    📦 包
                  </span>
                </div>
                <p class="text-xs text-gray-500 font-mono">{{ skill.name }}</p>
              </div>
            </div>

            <!-- Toggle switch -->
            <button
              @click="toggle(skill)"
              :disabled="toggling.has(skill.name)"
              :class="[
                'relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors',
                skill.enabled ? 'bg-green-500' : 'bg-gray-600',
                toggling.has(skill.name) ? 'opacity-50 cursor-wait' : 'cursor-pointer',
              ]"
              :title="skill.enabled ? '点击禁用' : '点击启用'"
            >
              <span
                :class="[
                  'inline-block h-4 w-4 transform rounded-full bg-white transition-transform',
                  skill.enabled ? 'translate-x-6' : 'translate-x-1',
                ]"
              />
            </button>
          </div>

          <!-- Description -->
          <p class="text-sm text-gray-400 mt-3 line-clamp-2">
            {{ skill.description || '暂无描述' }}
          </p>

          <!-- Metadata tags -->
          <div class="flex flex-wrap items-center gap-2 mt-3">
            <span
              :class="[
                'text-xs px-2 py-0.5 rounded-full border',
                skill.is_independent
                  ? 'bg-blue-500/20 text-blue-400 border-blue-500/30'
                  : 'bg-amber-500/20 text-amber-400 border-amber-500/30',
              ]"
            >
              {{ skill.is_independent ? '⚡ 独立' : '🔗 依赖' }}
            </span>

            <span
              v-if="skill.depends_on && skill.depends_on.length > 0"
              class="text-xs px-2 py-0.5 rounded-full bg-surface-700 text-gray-400 border border-gray-600/50"
            >
              依赖: {{ skill.depends_on.join(', ') }}
            </span>

            <span
              class="text-xs px-2 py-0.5 rounded-full bg-surface-700 text-gray-400 border border-gray-600/50"
            >
              🔧 {{ skill.tool_count ?? 0 }} 工具
            </span>

            <span
              v-if="skill.has_worker"
              class="text-xs px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-400 border border-purple-500/30"
            >
              🏃 Worker
            </span>
          </div>

          <!-- Action buttons for package skills -->
          <div v-if="skill.is_package" class="flex items-center gap-2 mt-3 pt-3 border-t border-gray-700/30">
            <button
              @click="toggleReadme(skill)"
              class="text-xs text-blue-400 hover:text-blue-300 px-2 py-1 rounded hover:bg-surface-700 transition-colors"
            >
              {{ expandedReadme[skill.name] ? '📖 收起说明' : '📖 查看说明' }}
            </button>
            <button
              @click="confirmUninstall(skill)"
              :disabled="uninstalling.has(skill.name)"
              class="text-xs text-red-400 hover:text-red-300 px-2 py-1 rounded hover:bg-surface-700 transition-colors disabled:opacity-50"
            >
              {{ uninstalling.has(skill.name) ? '⏳ ...' : '🗑️ 卸载' }}
            </button>
          </div>

          <!-- Expanded readme -->
          <div
            v-if="expandedReadme[skill.name]"
            class="mt-3 pt-3 border-t border-gray-700/30 max-h-96 overflow-y-auto"
          >
            <div v-if="expandedReadme[skill.name].loading" class="text-xs text-gray-500 py-2">
              ⏳ 加载说明文档...
            </div>
            <pre
              v-else
              class="text-xs text-gray-300 whitespace-pre-wrap font-mono leading-relaxed"
            >{{ expandedReadme[skill.name].content }}</pre>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
