<template>
  <div class="generation-bar">
    <div class="count">
      <span class="label">生成数量</span>
      <el-input-number
        v-model="store.params.video_count"
        :min="1"
        :max="10"
        controls-position="right"
      />
    </div>

    <el-button
      type="primary"
      size="large"
      :loading="store.generating"
      :disabled="!store.params.video_subject.trim()"
      @click="onSubmit"
    >
      {{ store.generating ? '生成中…' : '开始生成视频' }}
    </el-button>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { useWorkbenchStore } from '@/stores/workbench'

const emit = defineEmits<{ generated: [] }>()

const store = useWorkbenchStore()

async function onSubmit() {
  if (!store.params.video_subject.trim()) {
    ElMessage.warning('请先填写视频主题')
    return
  }
  try {
    const result = await store.submitGeneration()
    if (result) {
      ElMessage.success('任务已提交')
      emit('generated')
    }
  } catch {
    // 拦截器已提示
  }
}
</script>

<style scoped>
.generation-bar {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 16px 20px;
  background: #fff;
  border-radius: 6px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
}
.count {
  display: flex;
  align-items: center;
  gap: 12px;
}
.label {
  color: #606266;
  font-size: 14px;
}
</style>
