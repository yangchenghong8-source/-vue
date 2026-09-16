<template>
  <el-card class="panel" shadow="never">
    <template #header>
      <div class="panel-header">
        <span>配音</span>
      </div>
    </template>

    <el-form label-position="top" size="default">
      <el-form-item id="guide-voice-mode" label="配音模式">
        <el-radio-group v-model="store.voiceMode">
          <el-radio-button value="tts">AI 配音</el-radio-button>
          <el-radio-button value="upload">上传配音</el-radio-button>
          <el-radio-button value="none">无配音</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <template v-if="store.voiceMode === 'tts'">
        <el-form-item label="TTS 服务器">
          <el-select v-model="store.ttsServer" style="width: 100%">
            <el-option v-for="t in TTS_SERVERS" :key="t.value" :label="t.label" :value="t.value" />
          </el-select>
        </el-form-item>

        <el-form-item id="guide-voice-name" label="音色">
          <el-select v-model="store.params.voice_name" style="width: 100%">
            <el-option
              v-for="o in store.voiceOptions"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            />
          </el-select>
        <el-button type="primary" link :loading="previewLoading" :disabled="!store.params.voice_name" @click="previewVoice(store.params.voice_name)">试听当前音色</el-button>
        <audio v-if="previewUrl" :src="previewUrl" controls autoplay class="voice-preview" />
        </el-form-item>
      </template>

      <el-form-item v-if="store.voiceMode === 'upload'" label="配音音频文件">
        <el-upload
          :auto-upload="false"
          :limit="1"
          :on-change="onAudioChange"
          :on-remove="onAudioRemove"
          accept="audio/*"
        >
          <el-button>选择音频文件</el-button>
        </el-upload>
      </el-form-item>

      <template v-if="store.voiceMode !== 'none'">
        <el-form-item label="音量">
          <el-slider v-model="store.params.voice_volume" :min="0" :max="2" :step="0.05" show-input />
        </el-form-item>
        <el-form-item label="语速">
          <el-slider v-model="store.params.voice_rate" :min="0.5" :max="2" :step="0.05" show-input />
        </el-form-item>
      </template>

      <el-divider content-position="left">背景音乐</el-divider>

      <el-form-item id="guide-bgm" label="背景音乐类型">
        <el-radio-group v-model="store.params.bgm_type">
          <el-radio-button value="random">随机</el-radio-button>
          <el-radio-button value="none">无</el-radio-button>
          <el-radio-button value="custom">自定义</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <el-form-item v-if="store.params.bgm_type === 'custom'" label="背景音乐文件">
        <el-upload
          :auto-upload="false"
          :limit="1"
          :on-change="onBgmChange"
          :on-remove="onBgmRemove"
          accept="audio/*"
        >
          <el-button>选择音频文件</el-button>
        </el-upload>
      </el-form-item>

      <el-form-item v-if="store.params.bgm_type !== 'none'" label="背景音乐音量">
        <el-slider v-model="store.params.bgm_volume" :min="0" :max="1" :step="0.05" show-input />
      </el-form-item>
    </el-form>
  </el-card>
</template>

<script setup lang="ts">
import { ref } from "vue"
import { ElMessage } from "element-plus"
import { previewVoice as requestVoicePreview } from "@/api/helper"
import type { UploadFile } from 'element-plus'
import { useWorkbenchStore, TTS_SERVERS } from '@/stores/workbench'

const store = useWorkbenchStore()
const previewUrl = ref("")
const previewLoading = ref(false)

async function previewVoice(voiceName: string) {
  if (previewLoading.value) return
  previewLoading.value = true
  try {
    const text = voiceName.toLowerCase().startsWith("en-") ? "Hello, this is a preview of the selected voice." : "你好，这是当前音色的试听。"
    const blob = await requestVoicePreview({ voice_name: voiceName, text, voice_rate: store.params.voice_rate, voice_volume: store.params.voice_volume })
    if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
    previewUrl.value = URL.createObjectURL(blob)
  } catch {
    ElMessage.error("试听生成失败，请检查对应 TTS 配置")
  } finally {
    previewLoading.value = false
  }
}

function onAudioChange(file: UploadFile) {
  if (file.raw) store.customAudioFile = file.raw
}

function onAudioRemove() {
  store.customAudioFile = null
}

function onBgmChange(file: UploadFile) {
  if (file.raw) store.uploadedBgmFile = file.raw
}

function onBgmRemove() {
  store.uploadedBgmFile = null
}
</script>

<style scoped>
.voice-preview { width: 100%; margin: 8px 0 12px; }
.panel-header {
  font-weight: 600;
}
</style>
