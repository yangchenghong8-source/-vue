<template>
  <el-card class="panel" shadow="never">
    <template #header>
      <div class="panel-header">
        <span>脚本</span>
        <span class="panel-hint">主题 → 生成脚本 → 生成关键词</span>
      </div>
    </template>

    <el-form label-position="top" size="default">
      <el-form-item label="视频主题">
        <el-input
          v-model="store.params.video_subject"
          type="textarea"
          :rows="3"
          placeholder="例如：春天的花海，一场说走就走的旅行"
        />
      </el-form-item>

      <el-form-item label="视频语言">
        <el-select v-model="store.params.video_language" placeholder="自动" clearable style="width: 100%">
          <el-option v-for="l in LOCALES" :key="l" :label="l" :value="l" />
        </el-select>
      </el-form-item>

      <el-collapse class="advanced">
        <el-collapse-item title="高级脚本文案设置" name="advanced">
          <el-form-item label="脚本时长（秒）">
            <el-input-number
              v-model="store.params.video_script_duration"
              :min="0"
              :max="300"
              :step="5"
              controls-position="right"
              style="width: 100%"
            />
            <div class="tip">0 = 不限制；非 0 时按约 4.2 字/秒换算目标字数</div>
          </el-form-item>

          <el-form-item label="自定义文案要求">
            <el-input
              v-model="store.params.video_script_prompt"
              type="textarea"
              :rows="3"
              placeholder="对脚本风格、结构、口吻的额外要求"
            />
          </el-form-item>

          <el-form-item label="自定义 System Prompt">
            <el-input
              v-model="store.params.custom_system_prompt"
              type="textarea"
              :rows="5"
              placeholder="留空使用系统默认"
            />
          </el-form-item>

          <el-form-item>
            <el-switch v-model="store.params.use_knowledge" active-text="使用知识库生成脚本" />
          </el-form-item>

          <el-form-item v-if="store.params.use_knowledge" label="知识库文档（留空自动检索）">
            <el-select
              v-model="kbDocFilenames"
              multiple
              filterable
              clearable
              placeholder="选择文档"
              style="width: 100%"
            >
              <el-option
                v-for="d in kbDocs"
                :key="d.filename"
                :label="d.name || d.filename"
                :value="d.filename"
              />
            </el-select>
          </el-form-item>
        </el-collapse-item>
      </el-collapse>

      <el-form-item>
        <el-button
          type="primary"
          :loading="scriptLoading"
          :disabled="!store.params.video_subject.trim()"
          @click="onGenerateScript"
        >
          生成脚本
        </el-button>
      </el-form-item>

      <el-form-item label="视频脚本">
        <el-input
          v-model="store.params.video_script"
          type="textarea"
          :rows="8"
          placeholder="可直接粘贴脚本，或点击上方「生成脚本」由 AI 生成"
        />
      </el-form-item>

      <el-form-item>
        <el-button
          type="primary"
          plain
          :loading="termsLoading"
          :disabled="!store.params.video_script.trim()"
          @click="onGenerateTerms"
        >
          生成关键词
        </el-button>
      </el-form-item>

      <el-form-item label="视频关键词（逗号分隔）">
        <el-input
          v-model="store.params.video_terms"
          type="textarea"
          :rows="3"
          placeholder="用于搜索素材的关键词，逗号分隔"
        />
      </el-form-item>
    </el-form>
  </el-card>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useWorkbenchStore } from '@/stores/workbench'
import { getKbDocuments } from '@/api/helper'
import type { KbDoc } from '@/api/types'

const store = useWorkbenchStore()

// 与后端 support_locales 对齐
const LOCALES = [
  'zh-CN',
  'zh-HK',
  'zh-TW',
  'de-DE',
  'en-US',
  'es-ES',
  'fr-FR',
  'ru-RU',
  'vi-VN',
  'th-TH',
  'tr-TR',
]

const scriptLoading = ref(false)
const termsLoading = ref(false)

// 知识库文档列表（勾选「使用知识库」后加载）
const kbDocs = ref<KbDoc[]>([])

// kb_doc_filenames 为 null 时表示「留空自动检索」，多选组件需要数组，故用 getter/setter 包裹
const kbDocFilenames = computed<string[]>({
  get: () => store.params.kb_doc_filenames ?? [],
  set: (v: string[]) => {
    store.params.kb_doc_filenames = v.length ? v : null
  },
})

watch(
  () => store.params.use_knowledge,
  async (on) => {
    if (!on) return
    try {
      kbDocs.value = await getKbDocuments()
    } catch {
      // 拦截器已提示
    }
  },
)

async function onGenerateScript() {
  if (!store.params.video_subject.trim()) {
    ElMessage.warning('请先填写视频主题')
    return
  }
  scriptLoading.value = true
  try {
    await store.doGenerateScript()
    ElMessage.success('脚本已生成')
  } catch {
    // 拦截器已提示
  } finally {
    scriptLoading.value = false
  }
}

async function onGenerateTerms() {
  if (!store.params.video_script.trim()) {
    ElMessage.warning('请先生成或填写视频脚本')
    return
  }
  termsLoading.value = true
  try {
    await store.doGenerateTerms()
    ElMessage.success('关键词已生成')
  } catch {
    // 拦截器已提示
  } finally {
    termsLoading.value = false
  }
}
</script>

<style scoped>
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}
.panel-hint {
  font-weight: 400;
  font-size: 12px;
  color: #909399;
}
.advanced {
  margin-bottom: 16px;
}
.tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}
</style>
