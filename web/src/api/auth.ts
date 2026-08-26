import client from './client'
import type { ApiEnvelope } from './client'
import type { AuthUser } from '@/stores/auth'

export interface LoginResult {
  access_token: string
  token_type: string
  user: AuthUser
}

export interface RegisterPayload {
  username: string
  password: string
  confirm_password: string
  nickname?: string
  email?: string
}

// 登录：后端用 OAuth2PasswordRequestForm，需表单编码
export async function login(username: string, password: string): Promise<LoginResult> {
  const form = new URLSearchParams()
  form.append('username', username)
  form.append('password', password)
  const res = await client.post<ApiEnvelope<LoginResult>>('/api/v1/auth/login', form, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  })
  return res.data.data
}

export async function register(payload: RegisterPayload): Promise<LoginResult> {
  const res = await client.post<ApiEnvelope<LoginResult>>('/api/v1/auth/register', payload)
  return res.data.data
}
