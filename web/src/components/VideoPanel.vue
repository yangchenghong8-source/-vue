<template>
  <el-card class="panel" shadow="never">
    <template #header>
      <div class="panel-header">
        <span>视频画面</span>
      </div>
    </template>

    <el-form label-position="top" size="default">
      <div class="row">
        <el-form-item label="视频比例">
          <el-select v-model="store.params.video_aspect" style="width: 100%">
            <el-option v-for="a in ASPECTS" :key="a.value" :label="a.label" :value="a.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="拼接模式">
          <el-select v-model="store.params.video_concat_mode" style="width: 100%">
            <el-option label="随机" value="random" />
            <el-option label="顺序" value="sequential" />
          </el-select>
        </el-form-item>
      </div>

      <el-form-item label="素材来源">
        <el-select v-model="store.params.video_source" style="width: 100%">
          <el-option v-for="s in VIDEO_SOURCES" :key="s.value" :label="s.label" :value="s.value" />
        </el-select>
      </el-form-item>

      <el-form-item v-if="isKbSource" label="知识库层级">
        <el-cascader
          v-model="kbCategoryPath"
          :options="cascaderOptions"
          :props="{ checkStrictly: true }"
          clearable
          filterable
          style="width: 100%"
          placeholder="选择分类（可选中中间层级聚合）"
          @change="onKbCategoryChange"
        />
        <div v-if="kbTreeEmpty" class="tip">知识库暂无分类素材，请先上传素材</div>
      </el-form-item>

      <el-form-item v-if="store.params.video_source === 'local'" label="本地素材">
        <el-upload
          multiple
          :auto-upload="false"
          :file-list="localFileList"
          :on-change="onLocalChange"
          :on-remove="onLocalRemove"
          accept="video/*"
        >
          <el-button>选择视频文件</el-button>
          <template #tip>
            <div class="tip">上传后会暂存到服务端 local_videos 目录</div>
          </template>
        </el-upload>
      </el-form-item>

      <div class="row">
        <el-form-item label="每片段时长（秒）">
          <el-input-number
            v-model="store.params.video_clip_duration"
            :min="1"
            :max="60"
            controls-position="right"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="剪辑速度">
          <el-input-number
            v-model="store.params.video_clip_speed"
            :min="0.1"
            :max="3"
            :step="0.1"
            controls-position="right"
            style="width: 100%"
          />
        </el-form-item>
      </div>

      <el-form-item>
        <el-switch v-model="store.params.match_materials_to_script" active-text="素材匹配脚本" />
      </el-form-item>
    </el-form>
  </el-card>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { UploadFile, UploadUserFile } from 'element-plus'
import { useWorkbenchStore, VIDEO_SOURCES } from '@/stores/workbench'
import { getKbMediaCategories } from '@/api/helper'
import type { KbCategory } from '@/api/types'

const store = useWorkbenchStore()

const ASPECTS = [
  { value: '16:9', label: '横屏 16:9' },
  { value: '9:16', label: '竖屏 9:16' },
  { value: '1:1', label: '方形 1:1' },
]

interface CascaderOption {
  value: string
  label: string
  children?: CascaderOption[]
}

// 知识库层级（分类级联），仅 knowledge_base / jimeng 来源显示
const kbTree = ref<KbCategory[]>([])
const kbCategoryPath = ref<string[]>([])

const isKbSource = computed(
  () => store.params.video_source === 'knowledge_base' || store.params.video_source === 'jimeng',
)
const kbTreeEmpty = computed(() => kbTree.value.length === 0)

function toCascaderOptions(nodes: KbCategory[]): CascaderOption[] {
  return nodes.map((n) => {
    const full = n.full || n.name || ''
    return {
      value: full,
      label: `${n.name || full}（${n.count ?? 0} 个素材）`,
      children: n.children?.length ? toCascaderOptions(n.children) : undefined,
    }
  })
}

const cascaderOptions = computed<CascaderOption[]>(() => toCascaderOptions(kbTree.value))

function findNodeByFull(nodes: KbCategory[], full: string): KbCategory | null {
  for (const n of nodes) {
    if ((n.full || n.name) === full) return n
    if (n.children?.length) {
      const hit = findNodeByFull(n.children, full)
      if (hit) return hit
    }
  }
  return null
}

async function loadKbTree() {
  if (!isKbSource.value) {
    kbTree.value = []
    kbCategoryPath.value = []
    store.params.kb_category = ''
    return
  }
  const fileType = store.params.video_source === 'jimeng' ? 'image' : 'all'
  try {
    kbTree.value = await getKbMediaCategories(fileType)
  } catch {
    kbTree.value = []
  }
}

function onKbCategoryChange(value: string | number | (string | number)[]) {
  if (Array.isArray(value)) {
    const full = String(value[value.length - 1] ?? '')
    const node = full ? findNodeByFull(kbTree.value, full) : null
    store.params.kb_category = node && node.prefixes ? node.prefixes.join(',') : ''
  } else {
    store.params.kb_category = ''
  }
}

watch(() => store.params.video_source, loadKbTree, { immediate: true })

const localFileList = computed<UploadUserFile[]>(() =>
  store.localMaterials.map((f) => ({ name: f.name, size: f.size }) as UploadUserFile),
)

function onLocalChange(file: UploadFile) {
  if (file.raw) store.localMaterials.push(file.raw)
}

function onLocalRemove(file: UploadFile) {
  const idx = store.localMaterials.findIndex((f) => f.name === file.name)
  if (idx >= 0) store.localMaterials.splice(idx, 1)
}
</script>

<style scoped>
.panel-header {
  font-weight: 600;
}
.row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}
</style>
