<template>
  <div class="workbench">
    <el-container class="layout">
      <el-header class="topbar" height="60px">
        <div class="brand">短视频生成器</div>
        <div class="spacer" />
        <el-button text class="topbar-btn" :icon="Guide" @click="onOpenGuide">
          使用教程
        </el-button>
        <el-button text class="topbar-btn" @click="startTour">新手引导</el-button>
        <el-button text class="topbar-btn" :icon="Setting" @click="settingsVisible = true">
          设置
        </el-button>
        <span class="user">{{ auth.user?.nickname || auth.user?.username }}</span>
        <el-button text class="topbar-btn" @click="onLogout">退出登录</el-button>
      </el-header>

      <el-main class="main" v-loading="store.loadingResources">
        <div class="columns">
          <ScriptPanel />
          <VideoPanel />
          <AudioPanel />
          <SubtitlePanel />
        </div>

        <GenerationBar class="gen-bar" @generated="taskManagerRef?.refresh()" />

        <TaskManager ref="taskManagerRef" />
      </el-main>
    </el-container>

    <SettingsDialog v-model="settingsVisible" />
    <el-tour v-model="tourVisible" :mask="true" show-arrow>
      <el-tour-step target="#guide-subject" title="第 1 步：填写视频主题" description="在这里填写视频主题，例如：秋日城市漫步。" />
      <el-tour-step target="#guide-source" title="第 2 步：选择素材来源" description="选择本次视频的画面来源；首次使用可选择“知识库”。" />
      <el-tour-step v-if="isKnowledgeBase" target="#guide-kb-directory" title="第 3 步：选择知识库素材目录" description="仅使用知识库时需要选择本次视频使用的素材目录。" />
      <el-tour-step v-if="isKnowledgeBase" target="#guide-match" title="第 4 步：素材匹配脚本" description="可开启此开关，让每段画面尽量匹配对应的脚本文案。" />
      <el-tour-step target="#guide-script-generate" title="第 5 步：生成脚本" description="点击此按钮，让 AI 根据主题自动生成视频文案。" />
      <el-tour-step target="#guide-voice-name" title="第 6 步：选择音色" description="选择用于视频配音的音色，默认推荐音色可直接使用。" />
      <el-tour-step target="#guide-font" title="第 7 步：选择字幕字体" description="确认字幕字体即可，其他字幕样式可按需要再调整。" />
      <el-tour-step target="#guide-generate-button" title="第 8 步：开始生成视频" description="确认前面的设置后，点击这里提交任务；完成后可在任务管理中播放或下载。" />
    </el-tour>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Setting, Guide } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useWorkbenchStore } from '@/stores/workbench'
import ScriptPanel from '@/components/ScriptPanel.vue'
import VideoPanel from '@/components/VideoPanel.vue'
import AudioPanel from '@/components/AudioPanel.vue'
import SubtitlePanel from '@/components/SubtitlePanel.vue'
import GenerationBar from '@/components/GenerationBar.vue'
import TaskManager from '@/components/TaskManager.vue'
import SettingsDialog from '@/components/SettingsDialog.vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const store = useWorkbenchStore()
const isKnowledgeBase = computed(() => store.params.video_source === 'knowledge_base')

const settingsVisible = ref(false)
const tourVisible = ref(false)
const taskManagerRef = ref<InstanceType<typeof TaskManager> | null>(null)

function onOpenGuide() {
  router.push('/guide')
}

function startTour() {
  tourVisible.value = true
}

function onLogout() {
  auth.clear()
  router.replace('/login')
}

onMounted(() => {
  store.loadResources()
  if (route.query.tour === '1') {
    nextTick(() => { tourVisible.value = true })
  }
})
</script>

<style scoped>
.workbench,
.layout {
  height: 100%;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #1f2d3d;
  color: #fff;
}
.brand {
  font-size: 18px;
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.topbar-btn {
  color: #fff;
}
.user {
  font-size: 14px;
  opacity: 0.9;
}
.main {
  background: #f5f7fa;
}
.columns {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.gen-bar {
  margin-bottom: 16px;
}

@media (max-width: 1400px) {
  .columns {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 720px) {
  .columns {
    grid-template-columns: 1fr;
  }
}
</style>
