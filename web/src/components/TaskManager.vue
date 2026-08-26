<template>
  <el-card class="panel task-card" shadow="never">
    <template #header>
      <div class="panel-header">
        <span>任务管理</span>
        <el-button size="small" :icon="Refresh" circle @click="refresh" />
      </div>
    </template>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="全部" name="all" />
      <el-tab-pane label="处理中" name="processing" />
      <el-tab-pane label="已完成" name="complete" />
      <el-tab-pane label="失败" name="failed" />
    </el-tabs>

    <el-table :data="filteredTasks" v-loading="loading" empty-text="暂无任务" size="small">
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="stateInfo(row.state).type" size="small">
            {{ stateInfo(row.state).text }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="主题" min-width="180">
        <template #default="{ row }">
          <span class="subject">{{ subjectOf(row) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="进度" width="160">
        <template #default="{ row }">
          <el-progress
            v-if="row.state === TASK_STATE.PROCESSING || row.state === TASK_STATE.PENDING"
            :percentage="Number(row.progress ?? 0)"
            :stroke-width="10"
          />
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>

      <el-table-column label="更新时间" width="160">
        <template #default="{ row }">
          <span class="muted">{{ formatTime(row) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="240" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.state === TASK_STATE.PROCESSING || row.state === TASK_STATE.PENDING"
            size="small"
            @click="onPause(row)"
          >
            暂停
          </el-button>
          <el-button v-if="row.state === TASK_STATE.PAUSED" size="small" @click="onResume(row)">
            恢复
          </el-button>
          <el-button v-if="row.state === TASK_STATE.FAILED" size="small" @click="onRetry(row)">
            重试
          </el-button>
          <el-button
            v-if="row.state === TASK_STATE.COMPLETE"
            size="small"
            type="primary"
            @click="onPlay(row)"
          >
            播放
          </el-button>
          <el-button
            v-if="row.state === TASK_STATE.COMPLETE"
            size="small"
            @click="onDownload(row)"
          >
            下载
          </el-button>
          <el-button size="small" type="danger" plain @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-if="total > PAGE_SIZE"
      v-model:current-page="page"
      :page-size="PAGE_SIZE"
      :total="total"
      layout="prev, pager, next"
      class="pagination"
      @current-change="refresh"
    />

    <VideoPlayerDialog v-model="playerVisible" :uri="playerUri" />
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  TASK_STATE,
  deleteTask,
  downloadUrl,
  getTask,
  getTasks,
  retryTask,
  type TaskItem,
} from '@/api/tasks'
import { pauseTask, resumeTask } from '@/api/helper'
import VideoPlayerDialog from './VideoPlayerDialog.vue'

const PAGE_SIZE = 100

const tasks = ref<TaskItem[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const activeTab = ref<'all' | 'processing' | 'complete' | 'failed'>('all')

const playerVisible = ref(false)
const playerUri = ref<string | null>(null)

let timer: ReturnType<typeof setInterval> | null = null

const filteredTasks = computed(() => {
  switch (activeTab.value) {
    case 'processing':
      return tasks.value.filter(
        (t) => t.state === TASK_STATE.PROCESSING || t.state === TASK_STATE.PENDING,
      )
    case 'complete':
      return tasks.value.filter((t) => t.state === TASK_STATE.COMPLETE)
    case 'failed':
      return tasks.value.filter((t) => t.state === TASK_STATE.FAILED)
    default:
      return tasks.value
  }
})

function stateInfo(state: number): { text: string; type: 'danger' | 'success' | 'warning' | 'primary' | 'info' } {
  if (state === TASK_STATE.FAILED) return { text: '失败', type: 'danger' }
  if (state === TASK_STATE.COMPLETE) return { text: '已完成', type: 'success' }
  if (state === TASK_STATE.PAUSED) return { text: '已暂停', type: 'warning' }
  if (state === TASK_STATE.PROCESSING || state === TASK_STATE.PENDING) {
    return { text: '处理中', type: 'primary' }
  }
  return { text: '历史', type: 'info' }
}

function subjectOf(task: TaskItem): string {
  const p = task.params as Record<string, unknown> | undefined
  return (p?.video_subject as string) || task.task_id
}

function formatTime(task: TaskItem): string {
  const raw = (task.updated_at as string) || (task.created_at as string) || ''
  if (!raw) return '—'
  const s = String(raw)
  return s.replace('T', ' ').slice(0, 19)
}

async function refresh() {
  if (loading.value) return
  loading.value = true
  try {
    const data = await getTasks(page.value, PAGE_SIZE)
    tasks.value = data.tasks
    total.value = data.total
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

async function onPause(task: TaskItem) {
  try {
    await pauseTask(task.task_id)
    ElMessage.success('已暂停')
    refresh()
  } catch {
    /* ignore */
  }
}

async function onResume(task: TaskItem) {
  try {
    await resumeTask(task.task_id)
    ElMessage.success('已恢复')
    refresh()
  } catch {
    /* ignore */
  }
}

async function onRetry(task: TaskItem) {
  try {
    await retryTask(task.task_id)
    ElMessage.success('已重新提交')
    refresh()
  } catch {
    /* ignore */
  }
}

async function onDelete(task: TaskItem) {
  try {
    await ElMessageBox.confirm('确定删除该任务及其生成文件吗？', '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await deleteTask(task.task_id)
    ElMessage.success('已删除')
    refresh()
  } catch {
    /* ignore */
  }
}

function firstVideoUri(task: TaskItem): string | null {
  const combined = task.combined_videos as string[] | undefined
  const videos = task.videos as string[] | undefined
  const list = combined?.length ? combined : videos
  return list?.length ? list[0] : null
}

async function onPlay(task: TaskItem) {
  let uri = firstVideoUri(task)
  if (!uri) {
    try {
      const full = await getTask(task.task_id)
      uri = firstVideoUri(full)
    } catch {
      /* ignore */
    }
  }
  if (!uri) {
    ElMessage.warning('未找到可播放的视频文件')
    return
  }
  playerUri.value = uri
  playerVisible.value = true
}

async function onDownload(task: TaskItem) {
  let uri = firstVideoUri(task)
  if (!uri) {
    try {
      const full = await getTask(task.task_id)
      uri = firstVideoUri(full)
    } catch {
      /* ignore */
    }
  }
  if (!uri) {
    ElMessage.warning('未找到可下载的视频文件')
    return
  }
  window.open(downloadUrl(uri), '_blank')
}

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 2000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

defineExpose({ refresh })
</script>

<style scoped>
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}
.subject {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.muted {
  color: #909399;
  font-size: 12px;
}
.pagination {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
