import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiPost } from '@/api/client'
import { generateScript, generateTerms, generateSocialMetadata } from '@/api/llm'

vi.mock('@/api/client', () => ({
  apiPost: vi.fn(),
}))

const apiPostMock = vi.mocked(apiPost)

describe('llm.ts 路由（BUG-1/2：去掉 /llm 前缀）', () => {
  beforeEach(() => {
    apiPostMock.mockReset()
  })

  it('generateScript 请求 POST /api/v1/scripts', async () => {
    apiPostMock.mockResolvedValue({ video_script: '脚本' })
    const body = { video_subject: '主题' }
    await generateScript(body)
    expect(apiPostMock).toHaveBeenCalledWith('/api/v1/scripts', body)
    expect(apiPostMock.mock.calls[0][0]).not.toContain('/llm')
  })

  it('generateTerms 请求 POST /api/v1/terms', async () => {
    apiPostMock.mockResolvedValue({ video_terms: ['a', 'b'] })
    const body = { video_subject: '主题' }
    await generateTerms(body)
    expect(apiPostMock).toHaveBeenCalledWith('/api/v1/terms', body)
    expect(apiPostMock.mock.calls[0][0]).not.toContain('/llm')
  })

  it('generateSocialMetadata 请求 POST /api/v1/social-metadata', async () => {
    apiPostMock.mockResolvedValue({ title: 't', caption: 'c', hashtags: ['#x'] })
    const body = { video_subject: '主题' }
    await generateSocialMetadata(body)
    expect(apiPostMock).toHaveBeenCalledWith('/api/v1/social-metadata', body)
    expect(apiPostMock.mock.calls[0][0]).not.toContain('/llm')
  })
})
