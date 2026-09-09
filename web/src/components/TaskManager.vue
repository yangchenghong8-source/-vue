<template>
  <el-card class="panel task-card" shadow="never">
    <template #header>
      <div class="panel-header">
        <span>任务管理</span>
        <el-button size="small" :icon="Refresh" circle @click="refresh()" />
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

      <el-table-column label="操作" width="340" fixed="right">
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
          <el-button
            v-if="hasMaterialMatchReport(row)"
            size="small"
            @click="onMaterialReport(row)"
          >
            素材报告
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

    <el-dialog v-model="materialReportVisible" title="分镜素材匹配报告" width="760px">
      <div v-loading="materialReportLoading">
        <template v-if="materialReport">
          <el-alert
            :type="materialReport.unmatched ? 'warning' : 'success'"
            :title="`已命中 ${materialReport.matched}/${materialReport.total} 个分镜`"
            :description="materialReport.unmatched
              ? `${materialReport.unmatched} 个分镜未找到可靠素材，已避免使用无关素材。`
              : `所有素材均来自目录：${materialReport.kb_category}`"
            :closable="false"
            show-icon
          />
          <div class="report-meta">
            检索目录：{{ materialReport.kb_category || '未记录' }}
            · 关键词校验：{{ materialReport.require_keyword_overlap ? '已开启' : '未开启' }}
          </div>
          <div v-if="!materialReport.shots?.length" class="muted legacy-report">
            此历史任务只保存了汇总结果；新生成的任务会展示每个镜头的命中依据。
          </div>
          <div v-for="shot in materialReport.shots" :key="shot.shot" class="match-shot">
            <div class="match-shot-title">镜头 {{ shot.shot }}：{{ shot.text || '未提供镜头文案' }}</div>
            <template v-if="shot.accepted">
              <div class="match-content">
                <video
                  v-if="isVideoMedia(shot.accepted.name)"
                  class="match-preview"
                  :src="materialStreamUrl(shot.accepted.name)"
                  muted
                  preload="metadata"
                />
                <img
                  v-else
                  class="match-preview"
                  :src="materialStreamUrl(shot.accepted.name)"
                  :alt="shot.accepted.name"
                />
                <div>
                  <el-tag type="success" size="small">已命中</el-tag>
                  <div class="match-file">{{ shot.accepted.name }}</div>
                  <div class="muted">命中词：{{ shot.accepted.overlap_terms.join('、') || '语义匹配' }}</div>
                  <div class="muted">匹配分数：{{ formatScore(shot.accepted.score) }}</div>
                </div>
              </div>
            </template>
            <template v-else>
              <el-tag type="warning" size="small">未命中</el-tag>
              <div class="muted report-reason">{{ rejectionSummary(shot.rejections) }}</div>
            </template>
          </div>
        </template>
      </div>
    </el-dialog>
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
  getMaterialMatchReport,
  getTask,
  getTasks,
  retryTask,
  streamUrl,
  type MaterialMatchCandidate,
  type MaterialMatchReport,
  type TaskItem,
} from '@/api/tasks'
import { pauseTask, resumeTask } from '@/api/helper'
import VideoPlayerDialog from './VideoPlayerDialog.vue'
import { subjectOf } from '@/utils/task'

const PAGE_SIZE = 100

const tasks = ref<TaskItem[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const activeTab = ref<'all' | 'processing' | 'complete' | 'failed'>('all')

const playerVisible = ref(false)
const playerUri = ref<string | null>(null)
const materialReportVisible = ref(false)
const materialReportLoading = ref(false)
const materialReport = ref<MaterialMatchReport | null>(null)
const materialReportTaskId = ref('')

let timer: ReturnType<typeof setInterval> | null = null
let refreshing = false

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

function formatTime(task: TaskItem): string {
  const raw = (task.updated_at as string) || (task.created_at as string) || ''
  if (!raw) return '—'
  const s = String(raw)
  return s.replace('T', ' ').slice(0, 19)
}

async function refresh(silent = false) {
  if (refreshing) return
  refreshing = true
  if (!silent) loading.value = true
  try {
    const data = await getTasks(page.value, PAGE_SIZE)
    tasks.value = data.tasks
    total.value = data.total
  } catch {
    // 拦截器已提示
  } finally {
    if (!silent) loading.value = false
    refreshing = false
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
  const videos = task.videos as string[] | undefined
  const combined = task.combined_videos as string[] | undefined
  const list = videos?.length ? videos : combined
  return list?.length ? list[0] : null
}

function hasMaterialMatchReport(task: TaskItem): boolean {
  const params = task.params
  if (typeof params === 'string') {
    return /['"]video_source['"]\s*:\s*['"]knowledge_base['"]/.test(params)
  }
  return Boolean(
    params &&
    params.video_source === 'knowledge_base',
  )
}

function isVideoMedia(name: string): boolean {
  return /\.(mp4|mov|avi|flv|mkv|webm)$/i.test(name)
}

function materialStreamUrl(name: string): string {
  return streamUrl(`${materialReportTaskId.value}/${name}`)
}

function formatScore(score: number): string {
  return Number.isFinite(score) ? score.toFixed(2) : '未提供'
}

function rejectionSummary(rejections: MaterialMatchCandidate[]): string {
  if (rejections.some((item) => item.reason === 'no_keyword_overlap')) {
    return '候选素材与分镜视觉描述没有有效关键词重合。'
  }
  if (rejections.some((item) => item.reason === 'below_semantic_threshold')) {
    return '候选素材的语义匹配分数不足。'
  }
  return '当前目录内没有可用的可靠素材。'
}

async function onMaterialReport(task: TaskItem) {
  materialReportVisible.value = true
  materialReportLoading.value = true
  materialReport.value = null
  materialReportTaskId.value = task.task_id
  try {
    materialReport.value = await getMaterialMatchReport(task.task_id)
  } catch {
    materialReportVisible.value = false
  } finally {
    materialReportLoading.value = false
  }
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
  timer = setInterval(() => refresh(true), 2000)
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
.report-meta {
  margin: 12px 0;
  color: #606266;
  font-size: 13px;
}
.match-shot {
  padding: 12px 0;
  border-bottom: 1px solid #ebeef5;
}
.match-shot:last-child {
  border-bottom: 0;
}
.match-shot-title {
  margin-bottom: 8px;
  font-weight: 600;
  line-height: 1.5;
}
.match-content {
  display: flex;
  gap: 12px;
  align-items: center;
}
.match-preview {
  width: 96px;
  height: 64px;
  flex: 0 0 auto;
  border-radius: 4px;
  background: #f5f7fa;
  object-fit: cover;
}
.match-file {
  margin: 6px 0;
  word-break: break-all;
}
.report-reason {
  margin-top: 8px;
}
.legacy-report {
  margin-bottom: 12px;
}
</style>
