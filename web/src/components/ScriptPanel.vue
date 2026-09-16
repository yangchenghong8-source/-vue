<template>
  <el-card class="panel" shadow="never">
    <template #header>
      <div class="panel-header">
        <span>脚本</span>
        <span class="panel-hint">主题 → 生成脚本 → 生成关键词</span>
      </div>
    </template>

    <el-form label-position="top" size="default">
      <el-form-item id="guide-subject" label="视频主题">
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

      <div class="advanced">
          <el-form-item id="guide-source" label="素材来源">
            <el-select v-model="store.params.video_source" style="width: 100%">
              <el-option v-for="s in VIDEO_SOURCES" :key="s.value" :label="s.label" :value="s.value" />
            </el-select>
          </el-form-item>

          <el-form-item v-if="store.params.video_source === 'knowledge_base'" id="guide-kb-directory" label="知识库素材目录" required><el-cascader v-model="kbCategoryPath" :options="cascaderOptions" :props="{ checkStrictly: true }" clearable filterable style="width: 100%" placeholder="选择末级目录或上级覆盖范围" @change="onKbCategoryChange" /><div v-if="kbTreeEmpty" class="tip">知识库暂无可用目录，请先上传并完成素材分类</div><div v-else class="tip">选择末级目录时仅检索该目录；选择上级目录时会覆盖其下级目录。</div></el-form-item>
          <el-form-item v-if="store.params.video_source === 'knowledge_base'" id="guide-match">
            <el-switch v-model="store.params.match_materials_to_script" active-text="素材匹配脚本" />
            <div v-if="isKbSource" class="tip match-tip">
              开启后按每个分镜的视觉描述、实体和场景，在所选目录中匹配素材；无可靠命中时会标记为未命中，不会用无关素材替代。
            </div>
          </el-form-item>
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

          <div v-show="false">
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
          </div>
      </div>

      <el-form-item>
        <el-button
          id="guide-script-generate"
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

      <el-form-item v-show="false" label="视频关键词（逗号分隔）">
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
import { useWorkbenchStore, VIDEO_SOURCES } from '@/stores/workbench'
import { getKbDocuments, getKbMediaCategories } from '@/api/helper'
import type { KbCategory, KbDoc } from '@/api/types'

const store = useWorkbenchStore()

const isKbSource = computed(
  () => store.params.video_source === "knowledge_base" || store.params.video_source === "jimeng",
)

interface CascaderOption { value: string; label: string; children?: CascaderOption[] }; const kbTree = ref<KbCategory[]>([]); const kbCategoryPath = ref<string[]>([]); const kbTreeEmpty = computed(() => kbTree.value.length === 0); function toCascaderOptions(nodes: KbCategory[]): CascaderOption[] { return nodes.map((n) => { const full = n.full || n.name || ""; return { value: full, label: (n.name || full) + "（" + (n.count ?? 0) + " 个素材）", children: n.children?.length ? toCascaderOptions(n.children) : undefined } }) }; const cascaderOptions = computed<CascaderOption[]>(() => toCascaderOptions(kbTree.value)); function findNodeByFull(nodes: KbCategory[], full: string): KbCategory | null { for (const node of nodes) { if ((node.full || node.name) === full) return node; const hit = node.children?.length ? findNodeByFull(node.children, full) : null; if (hit) return hit }; return null }; async function loadKbTree() { if (!isKbSource.value) { kbTree.value = []; kbCategoryPath.value = []; store.params.kb_category = ""; return }; try { kbTree.value = await getKbMediaCategories(store.params.video_source === "jimeng" ? "image" : "all") } catch { kbTree.value = [] } }; function onKbCategoryChange(value: string | number | (string | number)[]) { if (Array.isArray(value)) { const full = String(value[value.length - 1] ?? ""); const node = full ? findNodeByFull(kbTree.value, full) : null; store.params.kb_category = node?.prefixes?.join(",") || "" } else { store.params.kb_category = "" } }; watch(() => store.params.video_source, loadKbTree, { immediate: true });
// 与后端 support_locales 对齐
const LOCALES = ['zh-CN', 'en-US']

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
