// 共享类型：各面板与设置页使用的后端数据结构

export interface VoiceMap {
  azure: string[]
  siliconflow: string[]
  gemini: string[]
  mimo: string[]
  elevenlabs: string[]
  chatterbox: string[]
}

export interface LlmProviderSpec {
  id: string
  label: string
  default_model: string
  default_base_url: string
  requires_api_key: boolean
  requires_model_name: boolean
  requires_base_url: boolean
  show_api_key: boolean
  show_base_url: boolean
  api_key_url: string
}

export interface LlmProvidersData {
  current: string
  providers: LlmProviderSpec[]
}

export interface BgmFileItem {
  name: string
  size: number
  file: string
}

export interface MaterialFileItem {
  name: string
  size: number
  file: string
}

export interface CacheStats {
  file_count: number
  total_size: number
  oldest_mtime: number | null
  newest_mtime: number | null
  dir: string
}

export interface KbDoc {
  filename?: string
  name?: string
  category?: string
  [key: string]: unknown
}

export interface KbCategory {
  name?: string
  full?: string
  count?: number
  prefixes?: string[]
  children?: KbCategory[]
  [key: string]: unknown
}

export interface ConfigSections {
  app: Record<string, unknown>
  azure: Record<string, unknown>
  siliconflow: Record<string, unknown>
  elevenlabs: Record<string, unknown>
  chatterbox: Record<string, unknown>
  ui: Record<string, unknown>
}
