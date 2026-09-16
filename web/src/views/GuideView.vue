<template>
  <div class="guide-page">
    <el-header class="guide-topbar" height="60px">
      <div class="brand">短视频生成器 · 新手指南</div>
      <div class="spacer" />
      <el-button text class="back-btn" @click="goBack">返回工作台</el-button>
    </el-header>

    <el-main class="guide-main">
      <section class="hero">
        <span class="eyebrow">第一次使用？从这里开始</span>
        <h1>四步生成第一条短视频</h1>
        <p>无需逐项研究全部设置。先按推荐配置完成一次生成，之后再按需要微调。</p>
        <el-button type="primary" size="large" @click="startTour">开始界面引导</el-button>
      </section>

      <el-card class="guide-card" shadow="never">
        <el-steps :active="4" align-center class="quick-steps">
          <el-step title="填写主题" description="告诉 AI 要做什么视频" />
          <el-step title="选择素材" description="选择知识库和素材目录" />
          <el-step title="确认声音字幕" description="保留默认配置即可" />
          <el-step title="生成并查看" description="任务完成后播放或下载" />
        </el-steps>
        <div class="step-grid">
          <article class="step-card"><span class="step-number">1</span><div><h2>填写主题并生成脚本</h2><p>在“视频主题”输入想表达的内容，例如“秋日城市漫步”。点击“生成脚本”，也可以直接粘贴已有文案。</p></div></article>
          <article class="step-card"><span class="step-number">2</span><div><h2>选择知识库素材</h2><p>在“素材来源”选择“知识库”，再选择要使用的素材目录。打开“素材匹配脚本”可让画面更贴近文案。</p></div></article>
          <article class="step-card"><span class="step-number">3</span><div><h2>使用推荐的声音、字幕和 BGM</h2><p>保持 AI 配音、字幕开启和随机背景音乐即可生成完整成片；确认效果后再调整音色、字号或音乐音量。</p></div></article>
          <article class="step-card"><span class="step-number">4</span><div><h2>开始生成并查看结果</h2><p>点击“开始生成视频”。任务管理会显示进度；完成后可直接播放预览或下载视频。</p></div></article>
        </div>
      </el-card>

      <el-card class="guide-card" shadow="never">
        <h2 class="section-title">新手常见问题</h2>
        <el-collapse>
          <el-collapse-item title="第一次生成时，哪些设置必须填写？" name="required"><p>只需填写视频主题，并在使用知识库时选择一个素材目录。其余选项均可先使用默认值。</p></el-collapse-item>
          <el-collapse-item title="为什么建议先使用知识库？" name="knowledge"><p>知识库会从您已整理的素材中选取画面。选择目录后，系统会在该目录及其子目录中匹配素材。</p></el-collapse-item>
          <el-collapse-item title="生成后没有字幕、配音或背景音乐怎么办？" name="media"><p>生成前确认“字幕”开关已开启、配音模式不是“无配音”，背景音乐类型不是“无”。</p></el-collapse-item>
          <el-collapse-item title="在哪里查看生成的视频？" name="result"><p>回到工作台下方的“任务管理”，等待进度完成后点击播放或下载。</p></el-collapse-item>
        </el-collapse>
      </el-card>
    </el-main>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
const router = useRouter()
const goBack = () => router.replace('/')
const startTour = () => router.replace({ path: '/', query: { tour: '1' } })
</script>

<style scoped>
.guide-page { min-height: 100%; background: #f5f7fa; }
.guide-topbar { display: flex; align-items: center; gap: 12px; background: #1f2d3d; color: #fff; }
.brand { font-size: 18px; font-weight: 600; }.spacer { flex: 1; }.back-btn { color: #fff; }
.guide-main { max-width: 1040px; margin: 0 auto; padding: 28px 20px 48px; }
.hero { padding: 26px 30px; margin-bottom: 20px; border-radius: 12px; color: #fff; background: linear-gradient(135deg, #2b73c7, #4aa4f4); }
.eyebrow { font-size: 13px; opacity: .88; }.hero h1 { margin: 6px 0; font-size: 28px; }.hero p { margin: 0 0 18px; opacity: .95; }
.guide-card { margin-bottom: 20px; border-radius: 10px; }.quick-steps { margin: 4px 0 28px; }
.step-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.step-card { display: flex; gap: 14px; padding: 18px; border: 1px solid #e8eef7; border-radius: 8px; background: #fbfdff; }
.step-number { display: grid; place-items: center; flex: 0 0 28px; height: 28px; border-radius: 50%; background: #409eff; color: #fff; font-weight: 700; }
.step-card h2 { margin: 0 0 6px; font-size: 16px; color: #1f2d3d; }.step-card p { margin: 0; color: #606266; line-height: 1.7; font-size: 14px; }
.section-title { margin: 0 0 12px; font-size: 19px; color: #1f2d3d; }
@media (max-width: 720px) { .guide-main { padding: 16px 12px 32px; }.hero { padding: 22px 20px; }.hero h1 { font-size: 23px; }.step-grid { grid-template-columns: 1fr; }.quick-steps { display: none; } }
</style>
