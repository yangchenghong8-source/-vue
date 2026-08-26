import axios, { type AxiosInstance } from 'axios'
import { ElMessage } from 'element-plus'

// 后端统一响应信封：{ status, message, data }
export interface ApiEnvelope<T = unknown> {
  status: number
  message: string | null
  data: T
}

export const TOKEN_KEY = 'mpt_access_token'

const client: AxiosInstance = axios.create({
  baseURL: '/',
  timeout: 300000,
})

// 请求拦截：附加 JWT
client.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截：统一解包信封，处理鉴权错误
client.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    const data = error.response?.data

    if (status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      const onAuthPage =
        location.pathname.startsWith('/login') || location.pathname.startsWith('/register')
      if (onAuthPage) {
        // 登录/注册页上的 401 是「凭证错误」，必须明确提示，否则用户会以为点了没反应
        const msg =
          data && typeof data === 'object' && 'message' in data && data.message
            ? (data.message as string)
            : '用户名或密码错误'
        ElMessage.error(msg)
      } else {
        ElMessage.error('登录已过期，请重新登录')
        setTimeout(() => {
          location.href = '/login'
        }, 600)
      }
    } else if (data && typeof data === 'object' && 'message' in data && data.message) {
      ElMessage.error(data.message as string)
    } else {
      ElMessage.error(error.message || '请求失败')
    }

    return Promise.reject(error)
  },
)

export async function apiGet<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  const res = await client.get<ApiEnvelope<T>>(url, { params })
  return res.data.data
}

export async function apiPost<T>(url: string, body?: unknown): Promise<T> {
  const res = await client.post<ApiEnvelope<T>>(url, body)
  return res.data.data
}

export async function apiPut<T>(url: string, body?: unknown): Promise<T> {
  const res = await client.put<ApiEnvelope<T>>(url, body)
  return res.data.data
}

export async function apiDelete<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  const res = await client.delete<ApiEnvelope<T>>(url, { params })
  return res.data.data
}

// multipart 上传（背景音乐 / 本地视频素材），字段名默认 `file`
export async function apiUpload<T>(url: string, file: File, field = 'file'): Promise<T> {
  const form = new FormData()
  form.append(field, file)
  const res = await client.post<ApiEnvelope<T>>(url, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return res.data.data
}

export default client
