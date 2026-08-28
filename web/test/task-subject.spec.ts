import { describe, expect, it } from 'vitest'
import { subjectOf } from '@/utils/task'
import type { TaskItem } from '@/api/tasks'

describe('subjectOf（BUG-4：任务主题列）', () => {
  it('优先返回 params.video_subject，而非 task_id', () => {
    const task = { task_id: 'uuid-123', params: { video_subject: '我的主题' } } as TaskItem
    expect(subjectOf(task)).toBe('我的主题')
  })

  it('params.video_subject 为空白时返回「未填写主题」', () => {
    const task = { task_id: 'uuid-123', params: { video_subject: '   ' } } as TaskItem
    expect(subjectOf(task)).toBe('未填写主题')
  })

  it('缺少 params 时返回「未填写主题」', () => {
    const task = { task_id: 'uuid-123' } as TaskItem
    expect(subjectOf(task)).toBe('未填写主题')
  })

  it('不会把 task_id 当作主题回填', () => {
    const task = { task_id: 'uuid-123' } as TaskItem
    expect(subjectOf(task)).not.toBe('uuid-123')
  })
})
