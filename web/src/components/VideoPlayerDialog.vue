<template>
  <el-dialog
    v-model="visible"
    title="视频预览"
    width="600px"
    top="5vh"
    :before-close="onClose"
    destroy-on-close
  >
    <video v-if="uri" :src="streamUrl(uri)" controls autoplay class="player" />
    <template #footer>
      <el-button @click="onClose">关闭</el-button>
      <el-button v-if="uri" type="primary" @click="onDownload">下载</el-button>
      <el-button v-if="uri" @click="onOpenNewTab">新窗口打开</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { streamUrl, downloadUrl } from '@/api/tasks'

const props = defineProps<{ modelValue: boolean; uri: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

function onClose() {
  emit('update:modelValue', false)
}

function onDownload() {
  if (props.uri) window.open(downloadUrl(props.uri), '_blank')
}

function onOpenNewTab() {
  if (props.uri) window.open(streamUrl(props.uri), '_blank')
}
</script>

<style scoped>
/* 不写死 width/height：由 max-* 双向约束，让浏览器按视频固有比例缩放。
   旧样式 `width:100%; max-height:480px` 会把 9:16 竖屏压成 270x480 —— 竖屏
   受高度约束，宽度只剩 270px，烧录时 60px 的字幕被缩到约 15px、1.5px 描边
   缩到亚像素，于是"字幕看不见"。同时保留 max-width 以兼容 16:9 横屏素材，
   否则横屏视频会撑破弹窗。 */
.player {
  display: block;
  margin: 0 auto;
  max-width: 100%;
  max-height: 75vh;
  background: #000;
  border-radius: 4px;
}
</style>
