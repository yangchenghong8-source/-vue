<template>
  <div class="auth-page">
    <el-card class="auth-card" shadow="always">
      <div class="brand">
        <h1>注册账号</h1>
        <p>创建你的短视频生成器账号</p>
      </div>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="3-30 位，字母/数字/下划线" :prefix-icon="User" autofocus />
        </el-form-item>
        <el-form-item label="昵称" prop="nickname">
          <el-input v-model="form.nickname" placeholder="可选" :prefix-icon="Avatar" />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input v-model="form.email" placeholder="可选" :prefix-icon="Message" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" placeholder="至少 6 位" :prefix-icon="Lock" show-password />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm_password">
          <el-input
            v-model="form.confirm_password"
            type="password"
            placeholder="再次输入密码"
            :prefix-icon="Lock"
            show-password
            @keyup.enter="onSubmit"
          />
        </el-form-item>

        <el-button type="primary" class="submit" :loading="loading" @click="onSubmit">
          注 册
        </el-button>
      </el-form>

      <div class="footer">
        已有账号？
        <router-link to="/login">去登录</router-link>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Avatar, Message } from '@element-plus/icons-vue'
import { register } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({
  username: '',
  nickname: '',
  email: '',
  password: '',
  confirm_password: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 30, message: '长度 3-30 位', trigger: 'blur' },
    { pattern: /^[a-zA-Z0-9_]+$/, message: '仅字母/数字/下划线', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '至少 6 位', trigger: 'blur' },
  ],
  confirm_password: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value !== form.password) callback(new Error('两次密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
  email: [{ type: 'email', message: '邮箱格式不正确', trigger: 'blur' }],
}

async function onSubmit() {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      const result = await register({
        username: form.username,
        password: form.password,
        confirm_password: form.confirm_password,
        nickname: form.nickname || undefined,
        email: form.email || undefined,
      })
      auth.setToken(result.access_token)
      auth.setUser(result.user)
      ElMessage.success('注册成功，已自动登录')
      router.replace('/')
    } catch {
      // 错误信息已由拦截器统一提示
    } finally {
      loading.value = false
    }
  })
}
</script>

<style scoped>
.auth-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2d3d 0%, #2b3a4a 50%, #1f2d3d 100%);
}
.auth-card {
  width: 400px;
}
.brand {
  text-align: center;
  margin-bottom: 8px;
}
.brand h1 {
  font-size: 22px;
  margin: 0 0 6px;
}
.brand p {
  color: #909399;
  margin: 0 0 16px;
}
.submit {
  width: 100%;
}
.footer {
  margin-top: 16px;
  text-align: center;
  color: #909399;
  font-size: 14px;
}
</style>
