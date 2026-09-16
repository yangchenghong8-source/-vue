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


      <el-form-item v-if="store.params.video_source === 'local'" label="本地素材">
        <el-upload
          multiple
          :auto-upload="false"
          :file-list="localFileList"
          :on-change="onLocalChange"
          :on-remove="onLocalRemove"
          accept="video/*,image/jpeg,image/png"
        >
          <el-button>选择视频/图片文件</el-button>
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

      <el-divider content-position="left">右上角 Logo</el-divider>
      <el-form-item>
        <el-switch v-model="store.params.logo_enabled" active-text="添加右上角 Logo" />
      </el-form-item>
      <el-form-item v-if="store.params.logo_enabled" label="选择已有 Logo">
  <el-select
    v-model="store.params.logo_file"
    placeholder="点击选择 Logo"
    clearable
    style="width: 100%"
    @change="onLibraryLogoChange"
  >
    <el-option v-for="logo in libraryLogos" :key="logo.file" :label="logo.name" :value="logo.file" />
  </el-select>
  <div class="tip">服务器 logo 文件夹中的 Logo：{{ libraryLogos.length ? '点击名称即可选择' : '暂无可用图片' }}</div>
</el-form-item>
<el-form-item v-if="store.params.logo_enabled" label="选择 Logo 图片">
        <el-upload
          :auto-upload="false"
          :limit="1"
          accept="image/png,image/jpeg,image/webp"
          :on-change="onLogoChange"
          :on-remove="onLogoRemove"
        >
          <el-button>选择 Logo</el-button>
          <template #tip>
            <div class="tip">支持 PNG、JPG、WebP；透明 PNG 会保留透明背景。</div>
          </template>
        </el-upload>
      </el-form-item>
    </el-form>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import type { UploadFile, UploadUserFile } from 'element-plus'
import { useWorkbenchStore } from '@/stores/workbench'
import { getLibraryLogos } from '@/api/helper'
import type { LibraryLogo } from '@/api/helper'

const store = useWorkbenchStore()
const libraryLogos = ref<LibraryLogo[]>([])

async function loadLibraryLogos() {
  try {
    libraryLogos.value = await getLibraryLogos()
  } catch {
    libraryLogos.value = []
  }
}

function onLibraryLogoChange() {
  store.logoFile = null
}

onMounted(loadLibraryLogos)

const ASPECTS = [
  { value: '16:9', label: '横屏 16:9' },
  { value: '9:16', label: '竖屏 9:16' },
  { value: '1:1', label: '方形 1:1' },
]


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

function onLogoChange(file: UploadFile) {
  if (file.raw) store.logoFile = file.raw
}

function onLogoRemove() {
  store.logoFile = null
  store.params.logo_file = null
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
