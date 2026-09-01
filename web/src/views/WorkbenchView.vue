<template>
  <div class="workbench">
    <el-container class="layout">
      <el-header class="topbar" height="60px">
        <div class="brand">短视频生成器</div>
        <div class="spacer" />
        <el-button text class="topbar-btn" :icon="Guide" @click="onOpenGuide">
          使用教程
        </el-button>
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
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
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
const auth = useAuthStore()
const store = useWorkbenchStore()

const settingsVisible = ref(false)
const taskManagerRef = ref<InstanceType<typeof TaskManager> | null>(null)

function onOpenGuide() {
  router.push('/guide')
}

function onLogout() {
  auth.clear()
  router.replace('/login')
}

onMounted(() => {
  store.loadResources()
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
