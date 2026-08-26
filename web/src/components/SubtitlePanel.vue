<template>
  <el-card class="panel" shadow="never">
    <template #header>
      <div class="panel-header">
        <span>字幕</span>
        <el-switch v-model="store.params.subtitle_enabled" />
      </div>
    </template>

    <el-form label-position="top" size="default" :disabled="!store.params.subtitle_enabled">
      <el-form-item label="字体">
        <el-select v-model="store.params.font_name" style="width: 100%" filterable>
          <el-option v-for="f in store.fonts" :key="f" :label="f" :value="f" />
        </el-select>
      </el-form-item>

      <el-form-item label="字号">
        <el-input-number
          v-model="store.params.font_size"
          :min="20"
          :max="200"
          controls-position="right"
          style="width: 100%"
        />
      </el-form-item>

      <div class="row">
        <el-form-item label="文字颜色">
          <el-color-picker v-model="store.params.text_fore_color" />
        </el-form-item>
        <el-form-item label="描边颜色">
          <el-color-picker v-model="store.params.stroke_color" />
        </el-form-item>
      </div>

      <el-form-item label="描边宽度">
        <el-input-number
          v-model="store.params.stroke_width"
          :min="0"
          :max="10"
          :step="0.5"
          controls-position="right"
          style="width: 100%"
        />
      </el-form-item>

      <el-form-item label="字幕位置">
        <el-select v-model="store.params.subtitle_position" style="width: 100%">
          <el-option label="顶部" value="top" />
          <el-option label="居中" value="center" />
          <el-option label="底部" value="bottom" />
          <el-option label="自定义" value="custom" />
        </el-select>
      </el-form-item>

      <el-form-item v-if="store.params.subtitle_position === 'custom'" label="自定义位置（%）">
        <el-input-number
          v-model="store.params.custom_position"
          :min="0"
          :max="100"
          controls-position="right"
          style="width: 100%"
        />
      </el-form-item>

      <el-divider content-position="left">字幕背景</el-divider>

      <el-form-item label="启用背景">
        <el-switch v-model="backgroundEnabled" />
      </el-form-item>

      <template v-if="backgroundEnabled">
        <el-form-item label="背景颜色">
          <el-color-picker v-model="bgColor" />
        </el-form-item>
        <el-form-item label="圆角背景">
          <el-switch v-model="rounded" />
        </el-form-item>
      </template>
    </el-form>
  </el-card>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useWorkbenchStore } from '@/stores/workbench'

const store = useWorkbenchStore()

const backgroundEnabled = ref(false)
const bgColor = ref('#1e1e1e')
const rounded = ref(false)

// 与 WebUI _render_subtitle_settings 对齐：
// text_background_color = 选中颜色（开启时）否则 False；rounded_subtitle_background 同理
watch(
  [backgroundEnabled, bgColor, rounded],
  () => {
    store.params.text_background_color = backgroundEnabled.value ? bgColor.value : false
    store.params.rounded_subtitle_background = backgroundEnabled.value ? rounded.value : false
  },
  { immediate: true },
)

// 配置加载后回填背景偏好
watch(
  () => store.config,
  (cfg) => {
    const ui = cfg?.ui
    if (!ui) return
    backgroundEnabled.value = Boolean(ui.subtitle_background_enabled)
    if (ui.subtitle_background_color) bgColor.value = String(ui.subtitle_background_color)
    rounded.value = Boolean(ui.rounded_subtitle_background)
  },
  { immediate: true },
)
</script>

<style scoped>
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}
.row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
</style>
