import { apiDelete, apiGet, apiPost, apiUpload } from './client'
import type { BgmFileItem, MaterialFileItem } from './types'

// ── 任务状态（与后端 const.py 对齐）──
// FAILED=-1 / COMPLETE=1 / PAUSED=3 / PROCESSING=4；pending=0
export const TASK_STATE = {
  FAILED: -1,
  PENDING: 0,
  COMPLETE: 1,
  PAUSED: 3,
  PROCESSING: 4,
} as const

export interface TaskItem {
  task_id: string
  request_id?: string
  state: number
  progress: number
  params?: Record<string, unknown>
  user_id?: string | number
  videos?: string[]
  combined_videos?: string[]
  failed_stage?: string | null
  error?: string | null
  cross_post_state?: string | null
  cross_post_results?: unknown[] | null
  cross_post_error?: string | null
  [key: string]: unknown
}

export interface TaskListData {
  tasks: TaskItem[]
  total: number
  page: number
  page_size: number
}

// ── 生成请求体（与后端 VideoParams schema 对应）──
export interface TaskVideoRequest {
  video_subject?: string
  video_script?: string
  video_terms?: string | string[]
  video_aspect?: string | null
  video_concat_mode?: string | null
  video_transition_mode?: string | null
  video_clip_duration?: number | null
  video_clip_speed?: number | null
  match_materials_to_script?: boolean
  video_count?: number | null
  video_source?: string | null
  video_materials?: unknown[] | null
  use_knowledge?: boolean
  kb_doc_filenames?: string[] | null
  kb_category?: string | null
  jimeng_storyboard?: string | null
  material_driven_mode?: boolean
  selected_category?: string | null
  custom_audio_file?: string | null
  video_language?: string | null
  voice_name?: string | null
  voice_volume?: number | null
  voice_rate?: number | null
  bgm_type?: string | null
  bgm_file?: string | null
  bgm_volume?: number | null
  video_music_prompt?: string
  sonilo_bgm_prompt?: string
  subtitle_enabled?: boolean | null
  subtitle_position?: string | null
  custom_position?: number
  font_name?: string | null
  text_fore_color?: string | null
  text_background_color?: boolean | string
  rounded_subtitle_background?: boolean
  font_size?: number
  stroke_color?: string | null
  stroke_width?: number
  n_threads?: number | null
  paragraph_number?: number
  video_script_prompt?: string
  custom_system_prompt?: string
  video_script_duration?: number
  [key: string]: unknown
}

export interface SubtitleRequest {
  video_script: string
  video_language?: string | null
  voice_name?: string | null
  voice_volume?: number | null
  voice_rate?: number | null
  bgm_type?: string | null
  bgm_file?: string | null
  bgm_volume?: number | null
  subtitle_position?: string | null
  font_name?: string | null
  text_fore_color?: string | null
  text_background_color?: boolean | string
  rounded_subtitle_background?: boolean
  font_size?: number
  stroke_color?: string | null
  stroke_width?: number
  video_source?: string | null
  subtitle_enabled?: string | null
}

export interface AudioRequest {
  video_script: string
  video_language?: string | null
  voice_name?: string | null
  voice_volume?: number | null
  voice_rate?: number | null
  bgm_type?: string | null
  bgm_file?: string | null
  bgm_volume?: number | null
  video_source?: string | null
}

// ── 任务接口 ──────────────────────────────────────────────────────────

export async function getTasks(page: number, pageSize: number): Promise<TaskListData> {
  return apiGet<TaskListData>('/api/v1/tasks', { page, page_size: pageSize })
}

export async function getTask(taskId: string): Promise<TaskItem> {
  return apiGet<TaskItem>(`/api/v1/tasks/${taskId}`)
}

export async function deleteTask(taskId: string): Promise<unknown> {
  return apiDelete(`/api/v1/tasks/${taskId}`)
}

export async function createVideo(body: TaskVideoRequest): Promise<{ task_id: string }> {
  return apiPost<{ task_id: string }>('/api/v1/videos', body)
}

export async function createSubtitle(body: SubtitleRequest): Promise<{ task_id: string }> {
  return apiPost<{ task_id: string }>('/api/v1/subtitle', body)
}

export async function createAudio(body: AudioRequest): Promise<{ task_id: string }> {
  return apiPost<{ task_id: string }>('/api/v1/audio', body)
}

export async function retryTask(taskId: string): Promise<{ task_id: string }> {
  return apiPost<{ task_id: string }>(`/api/v1/videos/${taskId}/retry`)
}

export interface BatchResult {
  batch_total: number
  created: number
  failed: number
  tasks: Array<{ row: number; status: string; task_id: string | null; subject?: string }>
}

// 直接以 JSON 数组作为 body 提交批量任务（后端同时支持 raw JSON array body）
export async function createBatch(rows: Record<string, unknown>[]): Promise<BatchResult> {
  return apiPost<BatchResult>('/api/v1/videos/batch', rows)
}

// ── 背景音乐 / 本地视频素材 ───────────────────────────────────────────

export async function getMusics(): Promise<BgmFileItem[]> {
  return apiGet<{ files: BgmFileItem[] }>('/api/v1/musics').then((r) => r.files)
}

export async function uploadMusic(file: File): Promise<{ file: string }> {
  return apiUpload<{ file: string }>('/api/v1/musics', file)
}

export async function getVideoMaterials(): Promise<MaterialFileItem[]> {
  return apiGet<{ files: MaterialFileItem[] }>('/api/v1/video_materials').then((r) => r.files)
}

export async function uploadVideoMaterial(file: File): Promise<{ file: string }> {
  return apiUpload<{ file: string }>('/api/v1/video_materials', file)
}

// ── 视频流 / 下载地址 ─────────────────────────────────────────────────

/**
 * 把后端返回的视频 URI（形如 `/tasks/{task_id}/final-1.mp4`，或带 endpoint
 * 前缀的完整 URL）转换为 stream/download 接口需要的相对路径
 * `{task_id}/final-1.mp4`（相对于 tasks 根目录）。FastAPI 的 `:path` 路由
 * 需要保留斜杠，不能整体 encodeURIComponent。
 */
export function taskFileRelativePath(uri: string): string {
  let path = uri
  const schemeIdx = path.indexOf('://')
  if (schemeIdx >= 0) {
    const afterScheme = path.slice(schemeIdx + 3)
    const slashIdx = afterScheme.indexOf('/')
    path = slashIdx >= 0 ? afterScheme.slice(slashIdx) : ''
  }
  path = path.replace(/^\/+/, '')
  if (path.startsWith('tasks/')) {
    path = path.slice('tasks/'.length)
  }
  return path
}

export function streamUrl(uri: string): string {
  return `/api/v1/stream/${taskFileRelativePath(uri)}`
}

export function downloadUrl(uri: string): string {
  return `/api/v1/download/${taskFileRelativePath(uri)}`
}
