import { apiDelete, apiGet, apiPost, apiPut, apiUpload } from './client'
import type {
  CacheStats,
  ConfigSections,
  KbCategory,
  KbDoc,
  LlmProvidersData,
  VoiceMap,
} from './types'

// ── 声音 / 字体 / LLM Provider 注册表（只读）──────────────────────────

export function getVoices(): Promise<VoiceMap> {
  return apiGet<VoiceMap>('/api/v1/voices')
}

export function getFonts(): Promise<string[]> {
  return apiGet<{ fonts: string[] }>('/api/v1/fonts').then((r) => r.fonts)
}

export function getLlmProviders(): Promise<LlmProvidersData> {
  return apiGet<LlmProvidersData>('/api/v1/llm/providers')
}

// ── 配置读写 ──────────────────────────────────────────────────────────

export function getConfig(): Promise<ConfigSections> {
  return apiGet<ConfigSections>('/api/v1/config')
}

export function updateConfig(body: Partial<ConfigSections>): Promise<{ saved: boolean }> {
  return apiPut<{ saved: boolean }>('/api/v1/config', body)
}

// ── 任务暂停 / 恢复 ───────────────────────────────────────────────────

export function pauseTask(taskId: string): Promise<{ task_id: string; paused: boolean }> {
  return apiPost(`/api/v1/tasks/${taskId}/pause`)
}

export function resumeTask(taskId: string): Promise<{ task_id: string; resumed: boolean }> {
  return apiPost(`/api/v1/tasks/${taskId}/resume`)
}

// ── 缓存管理（admin）──────────────────────────────────────────────────

export function getCacheStats(maxAgeDays?: number): Promise<CacheStats> {
  return apiGet<CacheStats>(
    '/api/v1/cache',
    maxAgeDays != null ? { max_age_days: maxAgeDays } : undefined,
  )
}

export function cleanCache(
  maxAgeDays?: number,
): Promise<{ deleted_count: number; deleted_size: number; failed_count: number }> {
  return apiDelete('/api/v1/cache', maxAgeDays != null ? { max_age_days: maxAgeDays } : undefined)
}

// ── 知识库（只读）─────────────────────────────────────────────────────

export function getKbHealth(): Promise<{ healthy: boolean }> {
  return apiGet<{ healthy: boolean }>('/api/v1/kb/health')
}

export function getKbDocuments(search = '', category = ''): Promise<KbDoc[]> {
  return apiGet<{ documents: KbDoc[] }>('/api/v1/kb/documents', { search, category }).then(
    (r) => r.documents,
  )
}

export function getKbCategories(): Promise<KbCategory[]> {
  return apiGet<{ categories: KbCategory[] }>('/api/v1/kb/categories').then((r) => r.categories)
}

export function getKbMediaCategories(fileType = 'all'): Promise<KbCategory[]> {
  return apiGet<{ tree: KbCategory[] }>('/api/v1/kb/media/categories', {
    file_type: fileType,
  }).then((r) => r.tree)
}

// ── LLM 连通性测试（admin）────────────────────────────────────────────

export function testLlmConnection(): Promise<{ success: boolean; message: string; elapsed: number }> {
  return apiPost('/api/v1/llm/test')
}

// ── 自定义配音上传 ────────────────────────────────────────────────────
// 与 /musics、/video_materials 一致：服务端校验音频流并落盘到
// storage/uploaded_audio，返回项目相对路径，供 createVideo 的
// custom_audio_file 字段引用（resolve_custom_audio_file 会在 root_dir 下解析）。
export function uploadCustomAudio(file: File): Promise<{ file: string }> {
  return apiUpload<{ file: string }>('/api/v1/custom-audio', file)
}
