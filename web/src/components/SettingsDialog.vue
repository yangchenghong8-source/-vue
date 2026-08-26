<template>
  <el-dialog
    v-model="visible"
    title="设置"
    width="640px"
    :before-close="onClose"
    destroy-on-close
  >
    <el-tabs v-model="activeTab">
      <el-tab-pane label="界面" name="interface">
        <el-form label-position="top" size="small">
          <div class="row">
            <el-form-item label="默认字体">
              <el-select v-model="localUi.font_name" filterable style="width: 100%">
                <el-option v-for="f in fonts" :key="f" :label="f" :value="f" />
              </el-select>
            </el-form-item>
            <el-form-item label="默认字号">
              <el-input-number v-model="localUi.font_size" :min="20" :max="200" style="width: 100%" />
            </el-form-item>
          </div>

          <el-form-item label="字幕位置">
            <el-select v-model="localUi.subtitle_position" style="width: 100%">
              <el-option label="顶部" value="top" />
              <el-option label="居中" value="center" />
              <el-option label="底部" value="bottom" />
              <el-option label="自定义" value="custom" />
            </el-select>
          </el-form-item>

          <el-form-item label="文字颜色">
            <el-color-picker v-model="localUi.text_fore_color" />
          </el-form-item>

          <el-form-item label="默认语言">
            <el-select v-model="localUi.language" clearable placeholder="自动" style="width: 100%">
              <el-option v-for="l in LOCALES" :key="l" :label="l" :value="l" />
            </el-select>
          </el-form-item>

          <div class="row">
            <el-form-item label="配音模式">
              <el-select v-model="localUi.voice_mode" style="width: 100%">
                <el-option label="AI 配音" value="tts" />
                <el-option label="上传配音" value="upload" />
                <el-option label="无配音" value="none" />
              </el-select>
            </el-form-item>
            <el-form-item label="TTS 服务器">
              <el-select v-model="localUi.tts_server" style="width: 100%">
                <el-option v-for="t in TTS_SERVERS" :key="t.value" :label="t.label" :value="t.value" />
              </el-select>
            </el-form-item>
          </div>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="缓存" name="cache">
        <div v-loading="cacheLoading" class="cache-pane">
          <el-descriptions v-if="cacheStats" :column="2" border size="small">
            <el-descriptions-item label="文件数">{{ cacheStats.file_count }}</el-descriptions-item>
            <el-descriptions-item label="总大小">{{ formatBytes(cacheStats.total_size) }}</el-descriptions-item>
            <el-descriptions-item label="目录" :span="2">{{ cacheStats.dir }}</el-descriptions-item>
          </el-descriptions>
          <el-empty v-else-if="!cacheLoading" description="无权限或暂无缓存信息" :image-size="60" />
          <el-button type="danger" class="clean-btn" @click="onCleanCache">清理缓存</el-button>
        </div>
      </el-tab-pane>

      <el-tab-pane label="LLM" name="llm">
        <el-form label-position="top" size="small">
          <el-form-item label="LLM 提供商">
            <el-select v-model="localApp.llm_provider" style="width: 100%">
              <el-option
                v-for="p in providerOptions"
                :key="p.id"
                :label="p.label"
                :value="p.id"
              />
            </el-select>
          </el-form-item>

          <el-form-item v-if="currentProvider" label="API Key">
            <el-input
              v-model="localApp[`${localApp.llm_provider}_api_key`]"
              type="password"
              show-password
              :placeholder="currentProvider.api_key_url ? `获取地址：${currentProvider.api_key_url}` : '请输入 API Key'"
            />
          </el-form-item>

          <el-form-item v-if="currentProvider?.requires_model_name" label="模型名">
            <el-input v-model="localApp[`${localApp.llm_provider}_model_name`]" />
          </el-form-item>

          <el-form-item v-if="currentProvider?.requires_base_url" label="Base URL">
            <el-input v-model="localApp[`${localApp.llm_provider}_base_url`]" />
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="素材 API" name="material">
        <el-form label-position="top" size="small">
          <el-form-item label="Pexels API Keys（逗号分隔多个）">
            <el-input v-model="localApp.pexels_api_keys" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="Pixabay API Keys（逗号分隔多个）">
            <el-input v-model="localApp.pixabay_api_keys" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="Coverr API Keys（逗号分隔多个）">
            <el-input v-model="localApp.coverr_api_keys" type="textarea" :rows="2" />
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>

    <template #footer>
      <el-button @click="onClose">取消</el-button>
      <el-button type="primary" :loading="saving" :disabled="!isAdmin" @click="onSave">
        保存
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useWorkbenchStore, TTS_SERVERS } from '@/stores/workbench'
import { useAuthStore } from '@/stores/auth'
import { cleanCache, getCacheStats, updateConfig } from '@/api/helper'
import type { CacheStats } from '@/api/types'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const store = useWorkbenchStore()
const auth = useAuthStore()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const isAdmin = computed(() => auth.isAdmin)
const activeTab = ref('interface')
const saving = ref(false)

const localUi = reactive<Record<string, unknown>>({})
const localApp = reactive<Record<string, unknown>>({})

const fonts = computed(() => store.fonts)
const providerOptions = computed(() => store.llmProviders?.providers ?? [])
const currentProvider = computed(() =>
  providerOptions.value.find((p) => p.id === localApp.llm_provider),
)

const LOCALES = [
  'zh-CN', 'zh-HK', 'zh-TW', 'de-DE', 'en-US', 'es-ES', 'fr-FR',
  'ru-RU', 'vi-VN', 'th-TH', 'tr-TR',
]

// 打开时从 store.config 初始化本地副本
watch(
  visible,
  (open) => {
    if (!open) return
    Object.keys(localUi).forEach((k) => delete localUi[k])
    Object.keys(localApp).forEach((k) => delete localApp[k])
    Object.assign(localUi, store.config?.ui ?? {})
    Object.assign(localApp, store.config?.app ?? {})
    if (!localUi.voice_mode) localUi.voice_mode = 'tts'
    if (!localUi.tts_server) localUi.tts_server = 'azure-tts-v1'
    if (!localApp.llm_provider) localApp.llm_provider = store.llmProviders?.current ?? 'moonshot'
  },
)

function onClose() {
  emit('update:modelValue', false)
}

// 被掩码的密钥值（如 "***"）不回传，避免覆盖真实值
function isMasked(v: unknown): boolean {
  return typeof v === 'string' && (v.startsWith('***') || v === '')
}

async function onSave() {
  saving.value = true
  try {
    const uiPayload: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(localUi)) {
      if (!isMasked(v)) uiPayload[k] = v
    }

    const appPayload: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(localApp)) {
      if (typeof v === 'string' && v.startsWith('***')) continue
      appPayload[k] = v
    }

    await updateConfig({ ui: uiPayload, app: appPayload })
    ElMessage.success('设置已保存')
    await store.loadResources()
    onClose()
  } catch {
    // 拦截器已提示
  } finally {
    saving.value = false
  }
}

// ── 缓存管理 ──
const cacheLoading = ref(false)
const cacheStats = ref<CacheStats | null>(null)

watch(
  [visible, activeTab],
  ([open, tab]) => {
    if (open && tab === 'cache') loadCache()
  },
)

async function loadCache() {
  cacheLoading.value = true
  try {
    cacheStats.value = await getCacheStats()
  } catch {
    cacheStats.value = null
  } finally {
    cacheLoading.value = false
  }
}

async function onCleanCache() {
  try {
    await ElMessageBox.confirm('确定清理缓存吗？该操作不可撤销。', '清理确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await cleanCache()
    ElMessage.success('缓存已清理')
    loadCache()
  } catch {
    /* ignore */
  }
}

function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let n = bytes
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(1)} ${units[i]}`
}
</script>

<style scoped>
.row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.cache-pane {
  min-height: 120px;
}
.clean-btn {
  margin-top: 12px;
}
</style>
