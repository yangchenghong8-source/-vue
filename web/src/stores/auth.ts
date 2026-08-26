import { defineStore } from 'pinia'
import { TOKEN_KEY, apiGet } from '@/api/client'

export interface AuthUser {
  id: number
  username: string
  nickname: string
  email: string
  role: string
  status: number
  create_time?: string
  update_time?: string
}

interface AuthState {
  token: string
  user: AuthUser | null
}

export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: null,
  }),
  getters: {
    isAuthenticated: (state) => !!state.token,
    isAdmin: (state) => state.user?.role === 'admin',
  },
  actions: {
    setToken(token: string) {
      this.token = token
      localStorage.setItem(TOKEN_KEY, token)
    },
    setUser(user: AuthUser) {
      this.user = user
    },
    clear() {
      this.token = ''
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
    },
    async fetchMe(): Promise<boolean> {
      try {
        const user = await apiGet<AuthUser>('/api/v1/auth/me')
        this.user = user
        return true
      } catch {
        this.clear()
        return false
      }
    },
  },
})
