# 111 (MoneyPrinterTurbo) 分镜素材匹配优化 — 改动报告

> 日期：2026-07-31
> 改动范围：`app/services/material.py` + `app/services/task.py`
> 目标：知识库素材搜索实现文案、音频、画面一一对应

---

## 一、改动概览

| 文件 | 改动类型 | 行数变化 | 说明 |
|------|---------|---------|------|
| [material.py](app/services/material.py) | 新增函数 | 712 → 865 (+153行) | `download_videos_by_storyboard()` 分镜级素材下载 |
| [task.py](app/services/task.py) | 修改 4 处 | 1724 → 1741 (+17行) | KB 分镜关键词保留 + 关联逻辑对齐 |

---

## 二、material.py 改动详情

### 新增：`download_videos_by_storyboard()` (155 行)

**位置**: 第 709-863 行，`if __name__ == "__main__"` 之前

**功能**: 按分镜逐个下载素材，替代原来的平铺式 `_download_files_from_kb()`。

**输入参数**:

```python
def download_videos_by_storyboard(
    task_id: str,           # 任务 ID
    storyboard: list,       # LLM 生成的分镜表 [{index, text, keywords_cn, keywords_en}, ...]
    video_subject: str,     # 视频主题（用于宽泛搜索降级）
    audio_duration: float,  # 音频总时长
    max_clip_duration: int, # 单个素材最大时长
    clip_durations: list,   # 每个场景的实际时长（来自字幕解析）
    kb_category: str,       # KB 分类过滤
) -> list:                  # 返回与 storyboard 等长的素材路径列表
```

**3 层降级搜索链**:

```
每个 storyboard shot 独立搜索：
┌─ Layer 1: KB 精准匹配 ─────────────────────────────────┐
│ 使用 shot.keywords_cn 逐个调用 _search_kb_with_fallback │
│ top_k=3, 匹配到 → 下载 → 结束                          │
├─ Layer 2: KB 宽泛匹配 ─────────────────────────────────┤
│ 使用 video_subject + jieba 分词扩展                     │
│ top_k=5, 匹配到 → 下载 → 结束                          │
├─ Layer 3: 邻居复用 ────────────────────────────────────┤
│ 向外逐层搜索 (i-1, i+1, i-2, i+2...)                    │
│ 找到最近的非空场景 → 复用其素材                          │
└────────────────────────────────────────────────────────┘
所有场景均失败 → 返回 []
```

**边界处理**:

| 边界情况 | 行为 |
|---------|------|
| `storyboard` 为空 | 返回 `[]` |
| `kb_client.is_healthy() == False` | 返回 `[]` |
| `clip_durations` 短于 storyboard | 自动用 `max_clip_duration` 补齐 |
| `clip_durations` 长于 storyboard | 自动截断 |
| 全局唯一素材追踪 | `seen_names` 集合防止同一素材被多个场景下载 |
| 邻居复用后仍全空 | 返回 `[]`（非部分空列表） |

**关键代码片段**:

```python
# Layer 1: KB precise (shot keywords_cn)
if keywords_cn:
    for kw in keywords_cn:
        results = _search_kb_with_fallback(kw, top_k=3, category=kb_category)
        for item in results:
            name = item.get("name", "")
            if name in seen_names:
                continue
            seen_names.add(name)
            local = kb_client.download_media(name, material_directory)
            if local:
                scene_materials[i] = local
                break
        if scene_materials[i]:
            break

# Layer 3: Neighbor reuse (outward search)
for offset in range(1, n_shots):
    src = i - offset
    if src >= 0 and scene_materials[src]:
        scene_materials[i] = scene_materials[src]
        break
    src = i + offset
    if src < n_shots and scene_materials[src]:
        scene_materials[i] = scene_materials[src]
        break
```

---

## 三、task.py 改动详情

### 改动 1: KB 模式保留分镜关键词（第 1458-1464 行）

**Before** — jieba 切 video_subject 覆盖分镜关键词：
```python
# KB 模式：jieba 主动拆分视频主题为多个搜索词，扩大搜索覆盖面
if params.video_source == "knowledge_base":
    _jieba_words = [w.strip() for w in jieba.cut(params.video_subject)
                    if len(w.strip()) >= 2]
    video_terms = [params.video_subject] + _jieba_words
    _seen = set()
    video_terms = [t for t in video_terms
                   if not (t in _seen or _seen.add(t))]
    logger.info(
        f"KB mode: jieba-decomposed {len(video_terms)} search terms: {video_terms}"
    )
```

**After** — 保留分镜关键词，交由 `download_videos_by_storyboard` 逐场景搜索：
```python
# KB 模式：保留分镜关键词用于逐场景素材匹配
# per-shot keywords_cn 保留在 _storyboard 中，
# 素材下载阶段按场景逐个搜索，实现文案-画面一一对应
if params.video_source == "knowledge_base":
    logger.info(
        f"KB mode: using per-shot storyboard keywords for "
        f"scene-by-scene material matching ({len(_storyboard)} shots)"
    )
```

**影响**: KB 模式下，不再用 `video_subject` 的 jieba 分词替代分镜关键词。每个 shot 的 `keywords_cn` 被保留在 `_storyboard` 中，传递给素材下载函数，实现精确匹配。

---

### 改动 2: `_clip_durations` 回退逻辑（第 1546-1554 行）

**Before** — 回退时使用 `video_terms` 数量（KB 模式为 topic_terms，非 KB 为 script_terms）：
```python
# 单段落回退：关键词数均分音频时长
if video_terms and isinstance(video_terms, list) and len(video_terms) > 0:
    _per_shot = audio_duration / len(video_terms)
    _clip_durations = [_per_shot] * len(video_terms)
```

**After** — 回退时优先使用 storyboard 场景数：
```python
# 单段落回退：按场景数均分音频时长
_ref_count = len(_storyboard) if _storyboard else (
    len(video_terms) if isinstance(video_terms, list) else 0
)
if _ref_count > 0:
    _per_shot = audio_duration / _ref_count
    _clip_durations = [_per_shot] * _ref_count
```

**影响**: 当字幕解析失败需要回退时，`_clip_durations` 的数量与 `storyboard` 场景数对齐，而不是与搜索词数量对齐。确保素材数量与场景数量一致。

---

### 改动 3: 素材下载路径分流（第 1553-1582 行）

**Before** — 所有路径统一调用 `get_video_materials()`：
```python
# 5. Get video materials
downloaded_videos = get_video_materials(
    task_id, params, video_terms, audio_duration,
    kb_fallback_to_pexels=False,
    kb_category=getattr(params, "kb_category", "") or "",
)
```

**After** — KB+storyboard 走新路径，其他不变：
```python
# 5. Get video materials
if _storyboard and params.video_source == "knowledge_base":
    # 分镜模式 KB：按场景逐个下载素材，实现文案-画面一一对应
    # 每个 shot 独立执行 3 层降级搜索（精准→宽泛→邻居复用）
    downloaded_videos = material.download_videos_by_storyboard(
        task_id=task_id,
        storyboard=_storyboard,
        video_subject=params.video_subject,
        audio_duration=audio_duration,
        max_clip_duration=params.video_clip_duration,
        clip_durations=_clip_durations,
        kb_category=getattr(params, "kb_category", "") or "",
    )
    # download_videos_by_storyboard 内部已处理降级和邻居复用，
    # 返回 [] 表示所有场景均失败，由下方空素材处理逻辑接管
else:
    downloaded_videos = get_video_materials(
        task_id, params, video_terms, audio_duration,
        kb_fallback_to_pexels=False,
        kb_category=getattr(params, "kb_category", "") or "",
    )
```

**影响**: 当 `match_materials_to_script=True` 且视频源为 KB 且分镜生成成功时，素材下载走逐场景匹配的新路径。所有其他情况保持原有逻辑不变。

---

### 改动 4: 时长重分配 `_idle` 引用修正（第 1575-1585 行）

**Before** — 使用 `video_terms` 数量作为预期场景数：
```python
if _clip_durations and downloaded_videos:
    MIN_SHOT_DURATION = 2.0
    _n = len(downloaded_videos)
    _idle = len(video_terms) if isinstance(video_terms, list) else 0
    if _n < _idle or _n < len(_clip_durations):
```

**After** — 优先使用 `_storyboard` 场景数：
```python
if _clip_durations and downloaded_videos:
    MIN_SHOT_DURATION = 2.0
    _n = len(downloaded_videos)
    _idle = len(_storyboard) if _storyboard else (
        len(video_terms) if isinstance(video_terms, list) else 0
    )
    if _n < _idle or _n < len(_clip_durations):
```

**影响**: 分镜模式下，时长重分配使用真实的场景数判断是否需要调整，而非依赖 `video_terms`（KB 模式下为 `topic_terms`，与场景数无关）。

---

## 四、兼容性说明

### 向下兼容

| 场景 | 行为 |
|------|------|
| 非 KB 源（Pexels/Pixabay） | 完全不受影响，走原有 `download_videos()` → `_download_videos_by_script_order()` |
| KB 源 + 未开启分镜匹配 | 走原有 `download_videos()` → `_download_files_from_kb()` |
| KB 源 + 分镜匹配但 storyboard 生成失败 | `_storyboard=None`，fallback 到原有路径 |
| KB 源 + 分镜匹配 + storyboard 成功 | **走新路径** `download_videos_by_storyboard()` |
| `video.py` shot-by-shot 模式 | 无需修改，已有的 `_shot_mode` 逻辑完全兼容新素材结构 |

### video.py 兼容性

`video.py` 的 shot-by-shot 模式 (第 615-680 行) 已原生支持：
- `len(clip_durations)` 与 `len(video_paths)` 自动对齐（pad/truncate）
- 图片自动转视频（ffmpeg loop）
- 视频按 `clip_durations` 精确裁剪
- 非 shot 模式不受影响

---

## 五、测试结果

### 端到端测试（新增 8 个）

| 测试 | 场景 | 结果 |
|------|------|:--:|
| `test_all_shots_precise_match` | 所有场景 KB 精准匹配 | ✅ |
| `test_fallback_to_broad_search` | 部分场景降级到宽泛搜索 | ✅ |
| `test_neighbor_reuse_when_all_layers_fail` | 某场景所有层失败，邻居复用 | ✅ |
| `test_total_failure_returns_empty` | 所有场景均失败，返回 `[]` | ✅ |
| `test_empty_storyboard_returns_empty` | 空分镜表边界处理 | ✅ |
| `test_kb_unreachable_returns_empty` | KB 不可达边界处理 | ✅ |
| `test_clip_durations_alignment` | clip_durations 补齐/截断 | ✅ |
| `test_no_duplicate_materials_across_scenes` | 跨场景素材去重 | ✅ |

### 回归测试

| 测试集 | 改动前 | 改动后 | 变化 |
|--------|:------:|:------:|:----:|
| material 测试 | 12/15 通过 | 12/15 通过 | 无变化 |
| task 测试 | 35/50 通过 | 35/50 通过 | 无变化 |
| 语法检查 | ✅ | ✅ | 无变化 |

---

## 六、数据流对比

### Before（旧流程）
```
video_subject ─→ jieba 切词 ─→ [词A, 词B, 词C] 平铺搜索词
                                         │
            ┌────────────────────────────┘
            ▼
_download_files_from_kb()  遍历搜索词，下载素材凑够总时长
            │
            ▼
["/path/a.mp4", "/path/b.jpg", "/path/c.mp4"]  无序平铺列表
            │
            ▼
generate_video()  素材列表 + clip_durations 尝试对齐
  问题：素材不绑定场景，出现 "第3段文案配了第1段的图"
```

### After（新流程）
```
storyboard.shot1.keywords_cn ─→ KB搜索(L1→L2→L3) ─→ 素材1 ─┐
storyboard.shot2.keywords_cn ─→ KB搜索(L1→L2→L3) ─→ 素材2 ─┤
storyboard.shot3.keywords_cn ─→ KB搜索(L1→L2→L3) ─→ 素材3 ─┘
                                                              │
TTS音频 ─→ 字幕SRT ─→ _parse_paragraph_durations() ─→ [2.3s, 4.1s, 3.7s]
                                                              │
                                                              ▼
                                              generate_video() shot-by-shot
                                              文案[N] ↔ 音频[N] ↔ 画面[N]
```
