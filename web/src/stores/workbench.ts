import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getConfig, getFonts, getLlmProviders, getVoices, uploadCustomAudio } from '@/api/helper'
import { generateScript, generateTerms } from '@/api/llm'
import { createVideo, uploadMusic, uploadVideoMaterial } from '@/api/tasks'
import type { TaskVideoRequest } from '@/api/tasks'
import type { ConfigSections, LlmProvidersData, VoiceMap } from '@/api/types'

// ── 与后端 VideoParams schema 对齐的表单模型 ──────────────────────────

export interface MaterialInfo {
  provider: string
  url: string
  duration: number
}

export interface WorkbenchParams {
  video_subject: string
  video_script: string
  video_terms: string
  video_aspect: string
  video_concat_mode: string
  video_transition_mode: string | null
  video_clip_duration: number
  video_clip_speed: number
  match_materials_to_script: boolean
  video_count: number
  video_source: string
  video_materials: MaterialInfo[] | null
  use_knowledge: boolean
  kb_doc_filenames: string[] | null
  kb_category: string
  jimeng_storyboard: string
  material_driven_mode: boolean
  selected_category: string | null
  custom_audio_file: string | null
  video_language: string
  voice_name: string
  voice_volume: number
  voice_rate: number
  bgm_type: string
  bgm_file: string
  bgm_volume: number
  video_music_prompt: string
  sonilo_bgm_prompt: string
  subtitle_enabled: boolean
  subtitle_position: string
  custom_position: number
  font_name: string
  text_fore_color: string
  text_background_color: boolean | string
  rounded_subtitle_background: boolean
  font_size: number
  stroke_color: string
  stroke_width: number
  n_threads: number
  paragraph_number: number
  video_script_prompt: string
  custom_system_prompt: string
  video_script_duration: number
}

export type VoiceMode = 'tts' | 'upload' | 'none'

const DEFAULT_PARAMS: WorkbenchParams = {
  video_subject: '',
  video_script: '',
  video_terms: '',
  video_aspect: '9:16',
  video_concat_mode: 'random',
  video_transition_mode: null,
  video_clip_duration: 5,
  video_clip_speed: 1.0,
  match_materials_to_script: true,
  video_count: 1,
  video_source: 'pexels',
  video_materials: null,
  use_knowledge: false,
  kb_doc_filenames: null,
  kb_category: '',
  jimeng_storyboard: '',
  material_driven_mode: false,
  selected_category: null,
  custom_audio_file: null,
  video_language: '',
  voice_name: '',
  voice_volume: 1.0,
  voice_rate: 1.0,
  bgm_type: 'random',
  bgm_file: '',
  bgm_volume: 0.2,
  video_music_prompt: '',
  sonilo_bgm_prompt: '',
  subtitle_enabled: true,
  subtitle_position: 'bottom',
  custom_position: 70.0,
  font_name: 'STHeitiMedium.ttc',
  text_fore_color: '#FFFFFF',
  text_background_color: false,
  rounded_subtitle_background: false,
  font_size: 60,
  stroke_color: '#000000',
  stroke_width: 1.5,
  n_threads: 16,
  paragraph_number: 1,
  video_script_prompt: '',
  custom_system_prompt: '',
  video_script_duration: 0,
}

export const TTS_SERVERS = [
  { value: 'azure-tts-v1', label: 'Azure TTS V1' },
  { value: 'azure-tts-v2', label: 'Azure TTS V2' },
  { value: 'siliconflow', label: 'SiliconFlow TTS' },
  { value: 'gemini-tts', label: 'Google Gemini TTS' },
  { value: 'mimo-tts', label: 'Xiaomi MiMo TTS' },
  { value: 'elevenlabs', label: 'ElevenLabs TTS' },
  { value: 'chatterbox', label: 'Chatterbox TTS' },
] as const

export const VIDEO_SOURCES = [
  { value: 'pexels', label: 'Pexels' },
  { value: 'pixabay', label: 'Pixabay' },
  { value: 'coverr', label: 'Coverr' },
  { value: 'local', label: '本地素材' },
  { value: 'knowledge_base', label: '知识库' },
  { value: 'jimeng', label: '即梦 AI' },
] as const

// 音色友好展示名（与 WebUI _friendly 对齐，中文标签）
export function friendlyVoice(v: string): string {
  if (!v) return ''
  if (v === 'no-voice' || v === 'none') return '无配音'
  if (v.startsWith('elevenlabs:')) {
    const parts = v.split(':')
    return parts.length >= 3 ? parts[2] : v
  }
  if (v.startsWith('chatterbox:')) {
    const name = v.includes(':') ? v.slice(v.indexOf(':') + 1) : v
    return name.replace('-Female', '').replace('-Male', '')
  }
  return v.replace('Female', '女声').replace('Male', '男声').replace('Neural', '')
}

export const useWorkbenchStore = defineStore('workbench', () => {
  // ── 表单参数 ──
  const params = reactive<WorkbenchParams>({ ...DEFAULT_PARAMS })

  // ── 配音模式（UI 级状态，持久化到 config.ui）──
  const voiceMode = ref<VoiceMode>('tts')
  const ttsServer = ref('azure-tts-v1')

  // ── 浏览器端上传（提交时通过 API 持久化到服务端）──
  const localMaterials = ref<File[]>([])
  const customAudioFile = ref<File | null>(null)
  const uploadedBgmFile = ref<File | null>(null)
  // 已持久化到 storage/local_videos 的素材（重新生成时复用）
  const persistedLocalMaterials = ref<MaterialInfo[]>([])

  // ── 资源（并行加载）──
  const voices = ref<VoiceMap | null>(null)
  const fonts = ref<string[]>([])
  const config = ref<ConfigSections | null>(null)
  const llmProviders = ref<LlmProvidersData | null>(null)
  const loadingResources = ref(false)

  // ── 动作状态 ──
  const generating = ref(false)

  // ── 派生 ──
  const configApp = computed<Record<string, unknown>>(() => config.value?.app ?? {})
  const configUi = computed<Record<string, unknown>>(() => config.value?.ui ?? {})

  // 当前 TTS 服务对应的音色列表
  const currentVoices = computed<string[]>(() => {
    if (!voices.value) return []
    const azure = voices.value.azure ?? []
    if (ttsServer.value === 'azure-tts-v2') return azure.filter((v) => v.includes('V2'))
    if (ttsServer.value === 'azure-tts-v1') return azure.filter((v) => !v.includes('V2'))
    const map: Record<string, keyof VoiceMap> = {
      siliconflow: 'siliconflow',
      'gemini-tts': 'gemini',
      'mimo-tts': 'mimo',
      elevenlabs: 'elevenlabs',
      chatterbox: 'chatterbox',
    }
    const key = map[ttsServer.value]
    return key ? voices.value[key] ?? [] : []
  })

  const voiceOptions = computed(() =>
    currentVoices.value.map((v) => ({ value: v, label: friendlyVoice(v) })),
  )

  // ── 资源加载 ──
  // 首次加载成功后才用 config 回填表单默认值，避免组件重挂载时二次覆盖用户编辑。
  let hydrated = false
  // 复用在途 Promise，并发调用共享同一次加载，而不是早退返回 undefined。
  let resourcesPromise: Promise<void> | null = null

  async function loadResources(): Promise<void> {
    if (resourcesPromise) return resourcesPromise
    resourcesPromise = (async () => {
      loadingResources.value = true
      try {
        const [v, f, c, p] = await Promise.all([getVoices(), getFonts(), getConfig(), getLlmProviders()])
        voices.value = v
        fonts.value = f
        config.value = c
        llmProviders.value = p
        if (!hydrated) {
          applyConfigDefaults()
          hydrated = true
        }
      } catch {
        // 拦截器已弹错误提示
      } finally {
        loadingResources.value = false
        resourcesPromise = null
      }
    })()
    return resourcesPromise
  }

  // 把 config 里的持久化偏好回填到表单与配音状态
  function applyConfigDefaults(): void {
    const ui = configUi.value
    const app = configApp.value

    params.font_name = (ui.font_name as string) || params.font_name
    params.subtitle_position = (ui.subtitle_position as string) || params.subtitle_position
    params.custom_position = Number(ui.custom_position ?? params.custom_position)
    params.text_fore_color = (ui.text_fore_color as string) || params.text_fore_color
    params.font_size = Number(ui.font_size ?? params.font_size)
    params.stroke_color = (ui.stroke_color as string) || params.stroke_color
    params.stroke_width = Number(ui.stroke_width ?? params.stroke_width)

    const savedVoiceMode = ui.voice_mode as VoiceMode | undefined
    const savedTtsServer = (ui.tts_server as string) || 'azure-tts-v1'
    if (savedVoiceMode === 'tts' || savedVoiceMode === 'upload' || savedVoiceMode === 'none') {
      voiceMode.value = savedVoiceMode
    } else {
      voiceMode.value = savedTtsServer === 'no-voice' ? 'none' : 'tts'
    }
    ttsServer.value = savedTtsServer === 'no-voice' ? 'azure-tts-v1' : savedTtsServer

    const savedVoiceName = (ui.voice_name as string) || ''
    if (savedVoiceName && currentVoices.value.includes(savedVoiceName)) {
      params.voice_name = savedVoiceName
    } else if (voiceMode.value === 'tts' && currentVoices.value.length) {
      const zh = currentVoices.value.find((v) => v.toLowerCase().startsWith('zh-cn'))
      params.voice_name = zh ?? currentVoices.value[0]
    }

    params.video_source = (app.video_source as string) || params.video_source
    params.match_materials_to_script =
      (app.match_materials_to_script as boolean) ?? params.match_materials_to_script
  }

  // ── LLM 脚本 / 关键词 ──
  async function doGenerateScript(): Promise<string> {
    const result = await generateScript({
      video_subject: params.video_subject,
      video_language: params.video_language,
      paragraph_number: params.paragraph_number,
      video_script_prompt: params.video_script_prompt,
      custom_system_prompt: params.custom_system_prompt,
      use_knowledge: params.use_knowledge,
      kb_doc_filenames: params.kb_doc_filenames,
      video_script_duration: params.video_script_duration,
    })
    params.video_script = result.video_script
    return result.video_script
  }

  async function doGenerateTerms(): Promise<string[]> {
    const result = await generateTerms({
      video_subject: params.video_subject,
      video_script: params.video_script,
      amount: 5,
      match_materials_to_script: params.match_materials_to_script,
      video_source: params.video_source,
    })
    params.video_terms = result.video_terms.join(', ')
    return result.video_terms
  }

  // ── 提交生成（含校验 + 上传本地素材/背景音乐/自定义配音）──
  async function submitGeneration(): Promise<{ task_id: string } | null> {
    // 1. 主题与脚本不能同时为空（后端 /videos 额外要求 video_subject 非空）
    if (!params.video_subject.trim()) {
      ElMessage.error('请先填写视频主题（video_subject）')
      return null
    }
    if (!VIDEO_SOURCES.some((s) => s.value === params.video_source)) {
      ElMessage.error('请选择有效的视频素材来源')
      return null
    }
    const app = configApp.value
    if (params.video_source === 'pexels' && !app.pexels_api_keys) {
      ElMessage.error('请先在设置中填写 Pexels API Key')
      return null
    }
    if (params.video_source === 'pixabay' && !app.pixabay_api_keys) {
      ElMessage.error('请先在设置中填写 Pixabay API Key')
      return null
    }
    if (params.video_source === 'coverr' && !app.coverr_api_keys) {
      ElMessage.error('请先在设置中填写 Coverr API Key')
      return null
    }
    if (params.video_source === 'local' && !localMaterials.value.length && !persistedLocalMaterials.value.length) {
      ElMessage.error('请先上传本地素材')
      return null
    }
    if (voiceMode.value === 'upload' && !customAudioFile.value) {
      ElMessage.error('请先上传配音文件')
      return null
    }

    generating.value = true
    try {
      const body: TaskVideoRequest = { ...params }

      // 上传本地素材 → 组装 video_materials
      if (localMaterials.value.length) {
        const materials: MaterialInfo[] = []
        for (const f of localMaterials.value) {
          const res = await uploadVideoMaterial(f)
          materials.push({ provider: 'local', url: res.file, duration: 0 })
        }
        body.video_materials = materials
        persistedLocalMaterials.value = materials
      } else if (params.video_source === 'local' && persistedLocalMaterials.value.length) {
        body.video_materials = persistedLocalMaterials.value
      }

      // 背景音乐上传
      if (uploadedBgmFile.value && params.bgm_type !== 'random' && params.bgm_volume > 0) {
        const res = await uploadMusic(uploadedBgmFile.value)
        body.bgm_file = res.file
      }

      // 自定义配音上传
      if (voiceMode.value === 'upload' && customAudioFile.value) {
        const res = await uploadCustomAudio(customAudioFile.value)
        body.custom_audio_file = res.file
      }

      // 配音模式 → voice_name
      if (voiceMode.value === 'none') {
        body.voice_name = 'no-voice'
      } else if (voiceMode.value === 'upload') {
        body.voice_name = 'no-voice'
      }

      const result = await createVideo(body)
      return result
    } finally {
      generating.value = false
    }
  }

  return {
    params,
    voiceMode,
    ttsServer,
    localMaterials,
    customAudioFile,
    uploadedBgmFile,
    persistedLocalMaterials,
    voices,
    fonts,
    config,
    llmProviders,
    loadingResources,
    generating,
    configApp,
    configUi,
    currentVoices,
    voiceOptions,
    loadResources,
    applyConfigDefaults,
    doGenerateScript,
    doGenerateTerms,
    submitGeneration,
  }
})
