import type { TaskItem } from '@/api/tasks'

export function subjectOf(task: TaskItem): string {
  const p = task.params
  const subject = p?.video_subject
  if (typeof subject === 'string' && subject.trim()) {
    return subject
  }
  return '未填写主题'
}
