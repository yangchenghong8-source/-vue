import type { TaskItem } from '@/api/tasks'

export function subjectOf(task: TaskItem): string {
  // 优先用后端返回的顶层 subject 字段（后端已做多级 fallback：params.video_subject → checkpoint → script → task_id）
  const subject = task.subject as string | undefined
  if (typeof subject === 'string' && subject.trim()) {
    return subject
  }
  // 兜底：从 params.video_subject 取（历史任务 params 可能是正常对象）
  const p = task.params
  const ps = p?.video_subject
  if (typeof ps === 'string' && ps.trim()) {
    return ps
  }
  return '未填写主题'
}
