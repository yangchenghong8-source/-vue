import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createVideo } from '@/api/tasks'
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
}))
vi.mock('@/api/llm', () => ({
  generateScript: vi.fn(),
  generateTerms: vi.fn(),
}))

const createVideoMock = vi.mocked(createVideo)

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
})
