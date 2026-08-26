<template>
  <el-dialog
    v-model="visible"
    title="视频预览"
    width="720px"
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
.player {
  width: 100%;
  max-height: 480px;
  background: #000;
  border-radius: 4px;
}
</style>
