import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createVideo } from '@/api/tasks'
import { uploadLogo } from '@/api/helper'
import { useWorkbenchStore } from '@/stores/workbench'

vi.mock('element-plus', () => ({
  ElMessage: { error: vi.fn(), success: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))
vi.mock('@/api/tasks', () => ({
  createVideo: vi.fn(),
  uploadMusic: vi.fn(),
  uploadVideoMaterial: vi.fn(),
}))
vi.mock('@/api/helper', () => ({
  getConfig: vi.fn(),
  getFonts: vi.fn(),
  getLlmProviders: vi.fn(),
  getVoices: vi.fn(),
  uploadCustomAudio: vi.fn(),
  uploadLogo: vi.fn(),
}))
vi.mock('@/api/llm', () => ({
  generateScript: vi.fn(),
  generateTerms: vi.fn(),
}))

const createVideoMock = vi.mocked(createVideo)
const uploadLogoMock = vi.mocked(uploadLogo)

function makeDeferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function setupStore() {
  const store = useWorkbenchStore()
  store.params.video_subject = '测试主题'
  store.params.video_source = 'pexels'
  store.config = {
    app: { pexels_api_keys: 'test-key' },
    azure: {},
    siliconflow: {},
    elevenlabs: {},
    chatterbox: {},
    ui: {},
  }
  return store
}

describe('submitGeneration 防重复（BUG-3）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    createVideoMock.mockReset()
    uploadLogoMock.mockReset()
  })

  it('单次点击只产生 1 个任务', async () => {
    const store = setupStore()
    createVideoMock.mockResolvedValue({ task_id: 't-1' })
    const result = await store.submitGeneration()
    expect(createVideoMock).toHaveBeenCalledTimes(1)
    expect(result).toEqual({ task_id: 't-1' })
    expect(store.generating).toBe(false)
  })

  it('3 次快速点击只产生 1 个任务', async () => {
    const store = setupStore()
    const d = makeDeferred<{ task_id: string }>()
    createVideoMock.mockReturnValue(d.promise)

    const p1 = store.submitGeneration()
    const p2 = store.submitGeneration()
    const p3 = store.submitGeneration()

    expect(createVideoMock).toHaveBeenCalledTimes(1)
    expect(store.generating).toBe(true)

    d.resolve({ task_id: 't-1' })
    await p1
    expect(await p2).toBeNull()
    expect(await p3).toBeNull()
    expect(store.generating).toBe(false)
  })

  it('请求失败后 generating 复位为 false', async () => {
    const store = setupStore()
    createVideoMock.mockRejectedValue(new Error('boom'))
    await expect(store.submitGeneration()).rejects.toThrow('boom')
    expect(store.generating).toBe(false)
  })

  it('校验失败不会卡在 loading 状态', async () => {
    const store = setupStore()
    store.params.video_subject = ''
    await store.submitGeneration()
    expect(createVideoMock).not.toHaveBeenCalled()
    expect(store.generating).toBe(false)
  })

  it('知识库模式未选择目录时不提交任务', async () => {
    const store = setupStore()
    store.params.video_source = 'knowledge_base'
    store.params.kb_category = ''

    await store.submitGeneration()

    expect(createVideoMock).not.toHaveBeenCalled()
    expect(store.generating).toBe(false)
  })

  it('知识库模式选择具体目录后允许提交任务', async () => {
    const store = setupStore()
    store.params.video_source = 'knowledge_base'
    store.params.kb_category = '科尔顿-医用-血液透析-AI机器人系列'
    createVideoMock.mockResolvedValue({ task_id: 'kb-1' })

    await store.submitGeneration()

    expect(createVideoMock).toHaveBeenCalledWith(
      expect.objectContaining({
        video_source: 'knowledge_base',
        kb_category: '科尔顿-医用-血液透析-AI机器人系列',
      }),
    )
  })

  it('启用 Logo 时会先上传图片，再将返回路径提交给视频任务', async () => {
    const store = setupStore()
    store.params.logo_enabled = true
    store.logoFile = new File(['logo'], 'brand.png', { type: 'image/png' })
    uploadLogoMock.mockResolvedValue({ file: 'storage/logos/1/brand.png' })
    createVideoMock.mockResolvedValue({ task_id: 't-logo' })

    await store.submitGeneration()

    expect(uploadLogoMock).toHaveBeenCalledWith(store.logoFile)
    expect(createVideoMock).toHaveBeenCalledWith(
      expect.objectContaining({
        logo_enabled: true,
        logo_file: 'storage/logos/1/brand.png',
      }),
    )
  })
})
