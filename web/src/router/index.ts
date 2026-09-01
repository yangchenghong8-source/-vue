import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'workbench',
    component: () => import('@/views/WorkbenchView.vue'),
  },
  {
    path: '/guide',
    name: 'guide',
    component: () => import('@/views/GuideView.vue'),
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 公开页面：已登录则直接进工作台
  if (to.meta.public) {
    if (auth.token && auth.user) {
      return { name: 'workbench' }
    }
    return true
  }

  // 受保护页面：无 token 直接去登录
  if (!auth.token) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  // 有 token 但还没拉过用户信息（如刷新后），尝试拉取 /me 校验
  if (!auth.user) {
    const ok = await auth.fetchMe()
    if (!ok) {
      return { name: 'login', query: { redirect: to.fullPath } }
    }
  }

  return true
})

export default router
