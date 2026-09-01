import { apiPost } from './client'

export interface ScriptResult {
  video_script: string
  kb_info?: Record<string, unknown>
}

export interface ScriptRequestBody {
  video_subject?: string
  video_language?: string
  paragraph_number?: number
  video_script_prompt?: string
  custom_system_prompt?: string
  use_knowledge?: boolean
  kb_doc_filenames?: string[] | null
  video_script_duration?: number
  match_materials_to_script?: boolean
  video_source?: string
  kb_category?: string
}

export interface TermsRequestBody {
  video_subject?: string
  video_script?: string
  amount?: number
  match_materials_to_script?: boolean
  video_source?: string
}

export interface SocialMetadata {
  title?: string
  caption?: string
  hashtags?: string[]
}

export interface SocialMetadataRequestBody {
  video_subject?: string
  video_script?: string
  language?: string
  platform?: string
}

export function generateScript(body: ScriptRequestBody): Promise<ScriptResult> {
  return apiPost<ScriptResult>('/api/v1/scripts', body)
}

export function generateTerms(body: TermsRequestBody): Promise<{ video_terms: string[] }> {
  return apiPost<{ video_terms: string[] }>('/api/v1/terms', body)
}

export function generateSocialMetadata(body: SocialMetadataRequestBody): Promise<SocialMetadata> {
  return apiPost<SocialMetadata>('/api/v1/social-metadata', body)
}
