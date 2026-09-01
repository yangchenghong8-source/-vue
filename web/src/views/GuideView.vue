<template>
  <div class="guide-page">
    <el-header class="guide-topbar" height="56px">
      <div class="brand">短视频生成器 · 使用教程</div>
      <div class="spacer" />
      <el-button text class="back-btn" @click="goBack">← 返回工作台</el-button>
    </el-header>

    <el-main class="guide-main">
      <el-card class="guide-card" shadow="never">
        <article class="guide-content">
          <p class="lead">
            一款基于 AI 的短视频自动生成工具。只需输入一个视频主题，即可自动完成「脚本撰写 → 素材匹配 → 配音 → 字幕合成」全流程，快速产出一条完整短视频。
          </p>

          <h2>一、简介</h2>
          <p>本系统是 MoneyPrinterTurbo 的多用户 Web 版本，支持账号注册登录、多租户隔离与任务管理。</p>
          <p class="sub-title">核心能力</p>
          <ul>
            <li>AI 脚本生成：支持自定义文案要求与知识库检索</li>
            <li>多素材来源：Pexels / Pixabay / Coverr / 本地素材 / 知识库 / 即梦 AI</li>
            <li>多 AI 配音：Azure、SiliconFlow、Gemini、ElevenLabs、小米 MiMo、Chatterbox 等</li>
            <li>字幕样式自定义：字体、字号、颜色、描边、位置、背景</li>
            <li>任务管理：实时进度、暂停/恢复、重试、播放、下载</li>
          </ul>

          <h2>二、快速开始</h2>
          <ol>
            <li><strong>注册 / 登录</strong>：首次使用请先点击「立即注册」创建账号，再用账号密码登录。</li>
            <li><strong>填写主题</strong>：进入工作台，在左侧「脚本」面板输入视频主题（必填），例如「春天的花海，一场说走就走的旅行」。</li>
            <li><strong>生成脚本</strong>：点击「生成脚本」，AI 自动撰写视频文案。</li>
            <li><strong>生成关键词</strong>：点击「生成关键词」，AI 从脚本中提取用于搜索素材的关键词。</li>
            <li><strong>开始生成</strong>：按需设置画面、配音、字幕后，点击底部「开始生成视频」。</li>
            <li><strong>查看结果</strong>：在「任务管理」中查看进度，完成后可「播放」预览或「下载」成片。</li>
          </ol>

          <h2>三、脚本面板</h2>
          <table>
            <thead><tr><th>配置项</th><th>说明</th></tr></thead>
            <tbody>
              <tr><td>视频主题</td><td>视频核心内容，<strong>必填</strong></td></tr>
              <tr><td>视频语言</td><td>脚本语言，支持简/繁中文、英、德、西、法、俄、越、泰、土耳其语等</td></tr>
              <tr><td>脚本时长（秒）</td><td><code>0</code> 表示不限；非 0 时按约 4.2 字/秒换算目标字数</td></tr>
              <tr><td>自定义文案要求</td><td>对脚本风格、结构、口吻的额外要求</td></tr>
              <tr><td>自定义 System Prompt</td><td>高级选项，留空使用系统默认</td></tr>
              <tr><td>使用知识库</td><td>开启后可从知识库文档检索相关内容辅助生成脚本</td></tr>
              <tr><td>视频脚本</td><td>可直接粘贴自写脚本，或点「生成脚本」由 AI 生成</td></tr>
              <tr><td>视频关键词</td><td>用于搜索素材的关键词，逗号分隔，可点「生成关键词」自动提取</td></tr>
            </tbody>
          </table>

          <h2>四、视频画面面板</h2>
          <table>
            <thead><tr><th>配置项</th><th>说明</th></tr></thead>
            <tbody>
              <tr><td>视频比例</td><td>横屏 16:9 / 竖屏 9:16 / 方形 1:1</td></tr>
              <tr><td>拼接模式</td><td>随机（素材随机拼接）/ 顺序（按序拼接）</td></tr>
              <tr><td>素材来源</td><td>Pexels / Pixabay / Coverr（在线素材库，需配置 API Key）、本地素材（上传自备视频/图片）、知识库（取用知识库素材）、即梦 AI（AI 生成图片）</td></tr>
              <tr><td>知识库层级</td><td>选择「知识库」或「即梦 AI」时，可指定分类层级（可选中中间层级聚合）</td></tr>
              <tr><td>本地素材</td><td>选择「本地素材」时，上传视频或图片文件</td></tr>
              <tr><td>每片段时长</td><td>每个镜头时长，1–60 秒</td></tr>
              <tr><td>剪辑速度</td><td>素材播放速度，0.1–3 倍</td></tr>
              <tr><td>素材匹配脚本</td><td>开启后尝试让素材内容与脚本语义匹配</td></tr>
            </tbody>
          </table>

          <h2>五、配音面板</h2>
          <table>
            <thead><tr><th>配置项</th><th>说明</th></tr></thead>
            <tbody>
              <tr><td>配音模式</td><td>AI 配音（文字转语音）/ 上传配音（自备音频）/ 无配音</td></tr>
              <tr><td>TTS 服务器</td><td>Azure TTS V1/V2、SiliconFlow、Google Gemini、小米 MiMo、ElevenLabs、Chatterbox</td></tr>
              <tr><td>音色</td><td>选择具体配音音色（男声/女声等）</td></tr>
              <tr><td>音量 / 语速</td><td>调节配音音量与播放速度</td></tr>
              <tr><td>背景音乐</td><td>随机（内置曲库随机）/ 无 / 自定义（上传音乐），可调音量</td></tr>
            </tbody>
          </table>

          <h2>六、字幕面板</h2>
          <table>
            <thead><tr><th>配置项</th><th>说明</th></tr></thead>
            <tbody>
              <tr><td>字幕开关</td><td>控制是否显示字幕</td></tr>
              <tr><td>字体 / 字号</td><td>字幕字体与大小</td></tr>
              <tr><td>文字颜色 / 描边颜色 / 描边宽度</td><td>字幕外观</td></tr>
              <tr><td>字幕位置</td><td>顶部 / 居中 / 底部 / 自定义</td></tr>
              <tr><td>字幕背景</td><td>可启用背景色块与圆角效果</td></tr>
            </tbody>
          </table>

          <h2>七、任务管理</h2>
          <ul>
            <li><strong>状态</strong>：处理中、已完成、失败、已暂停、历史等。</li>
            <li><strong>进度</strong>：处理中的任务实时显示进度条。</li>
            <li><strong>操作</strong>：
              <ul>
                <li>暂停 / 恢复：控制任务执行</li>
                <li>重试：失败任务重新提交</li>
                <li>播放：在线预览成片</li>
                <li>下载：下载视频文件</li>
                <li>删除：删除任务及其生成文件</li>
              </ul>
            </li>
            <li>列表每 2 秒自动刷新，也可点击表头右上角刷新按钮手动刷新。</li>
          </ul>

          <h2>八、设置</h2>
          <p>点击右上角「设置」按钮打开设置对话框：</p>
          <ul>
            <li><strong>界面</strong>：默认字体、字号、字幕位置、文字颜色、默认语言、配音模式、TTS 服务器等默认值。</li>
            <li><strong>缓存</strong>：查看缓存文件数与占用大小，可一键清理。</li>
            <li><strong>LLM</strong>：配置 LLM 提供商、API Key、模型名、Base URL。</li>
            <li><strong>素材 API</strong>：配置 Pexels、Pixabay、Coverr 的 API Key（使用在线素材库需要）。</li>
          </ul>
          <p class="note">部分设置（LLM、素材 API 等）需要管理员权限才能保存。</p>

          <h2>九、常见问题（FAQ）</h2>
          <ol>
            <li><strong>无法生成视频？</strong> 确认已填写「视频主题」，且所选素材来源对应的 API Key 已配置。</li>
            <li><strong>任务一直处理中？</strong> 观察进度条；若长时间卡住，可尝试「暂停→恢复」，或删除后重试。</li>
            <li><strong>视频没有声音？</strong> 检查配音面板是否误设为「无配音」，或音量是否调为 0。</li>
            <li><strong>字幕不显示？</strong> 确认字幕开关已打开，字号与位置设置合理。</li>
            <li><strong>在线素材搜不到？</strong> 请确认已在「设置 → 素材 API」中配置对应平台的 API Key。</li>
          </ol>
        </article>
      </el-card>
    </el-main>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()

function goBack() {
  router.replace('/')
}
</script>

<style scoped>
.guide-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}
.guide-topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #1f2d3d;
  color: #fff;
  flex-shrink: 0;
}
.brand {
  font-size: 17px;
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.back-btn {
  color: #fff;
}
.guide-main {
  flex: 1;
  overflow: auto;
  padding: 20px;
}
.guide-card {
  max-width: 880px;
  margin: 0 auto;
}
.guide-content {
  line-height: 1.7;
  color: #303133;
  font-size: 14px;
}
.guide-content .lead {
  font-size: 15px;
  color: #606266;
  background: #f0f9ff;
  border-left: 4px solid #409eff;
  padding: 12px 16px;
  border-radius: 0 6px 6px 0;
  margin: 0 0 20px;
}
.guide-content h2 {
  font-size: 18px;
  margin: 28px 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #ebeef5;
  color: #1f2d3d;
}
.guide-content .sub-title {
  font-weight: 600;
  margin: 16px 0 6px;
}
.guide-content ul,
.guide-content ol {
  margin: 8px 0;
  padding-left: 24px;
}
.guide-content li {
  margin: 4px 0;
}
.guide-content table {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0 16px;
  font-size: 13px;
}
.guide-content th,
.guide-content td {
  border: 1px solid #ebeef5;
  padding: 8px 12px;
  text-align: left;
  vertical-align: top;
}
.guide-content th {
  background: #f5f7fa;
  font-weight: 600;
  white-space: nowrap;
}
.guide-content code {
  background: #f0f2f5;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 12px;
  color: #c7254e;
}
.guide-content .note {
  color: #909399;
  font-size: 13px;
  background: #fdf6ec;
  border-left: 3px solid #e6a23c;
  padding: 8px 12px;
  border-radius: 0 4px 4px 0;
}
</style>
