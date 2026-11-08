# AI视频量产流水线

一条命令跑通 **选题 → 文稿 → 配音 → 素材 → 成片 → 投稿包** 五个环节的自动化生产工具。
面向"AI 文案 + AI 配音 + 可商用素材拼接"的量产型内容业务（中视频/知识科普/盘点号）。

## 环境要求

- Windows / Python 3.10+
- FFmpeg（已内置路径探测 `C:\ffmpeg-2025-11-02-git-f5eb11a71d-essentials_build\bin`，其他位置请加入 PATH）
- 依赖安装：`pip install -r requirements.txt`（requests、edge-tts、pillow）

## 配置（.env）

```ini
MIMO_API_BASE=https://token-plan-cn.xiaomimimo.com/anthropic
MIMO_API_TOKEN=tp-xxxx
MODEL_FLASH=mimo-v2.6-flash      # 走量：选题/初稿/关键词/标题
MODEL_PRO=mimo-v2.6-pro          # 精修：文稿润色
MODEL_TTS=mimo-v2.5-tts          # 配音（官方 /v1/chat/completions 音频模态，限时免费）
MIMO_TTS_VOICE=冰糖              # 预置音色：冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean
MIMO_TTS_STYLE=沉稳清晰的科普解说腔调，语速适中，语气自然平实，吐字清楚。
PEXELS_API_KEY=                  # 可选：免费可商用素材 https://www.pexels.com/api/
```

## 快速开始

```bash
# 1) 批量出 10 个选题 → 入 SQLite 选题库（output/topics.db），自动打分排序
python -X utf8 run_pipeline.py --ideate 10

# 2) 从选题库取最高分选题，完整产出一条视频
python -X utf8 run_pipeline.py --minutes 4

# 只出文稿+配音+投稿包，不合成视频（快速试听）
python -X utf8 run_pipeline.py --minutes 2 --no-assemble

# 指定选题、跳过素材检索（纯色背景出样片）
python -X utf8 run_pipeline.py --topic "夏天古人怎么降温" --skip-footage

# 人工取材闭环：按 选段工单.md 录屏放进 工作目录/采集/ 后，只重跑 素材→成片→投稿包
python -X utf8 run_pipeline.py --workdir output/2026-11-04_xxx --resume
```

每条视频产出在 `output/{日期}_{选题slug}/`：

| 文件 | 说明 |
|---|---|
| `文稿.json` / 文稿各字段 | 钩子→分节→CTA，含 tags、fact_check_notes |
| `audio/narration.wav` + `segments.json` | 旁白与逐句时间轴 |
| `字幕.srt` | 按逐句时长精确对齐（不用 ASR） |
| `manifest.json` | 逐句素材映射 + 授权留痕 |
| `成片.mp4` + `封面.png` | 1080p30，字幕烧录，可选 BGM |
| `投稿包.md` / `投稿包.json` | B站/西瓜抖音/视频号 多平台标题、简介、标签 |
| `发布自查清单.md` | 发布前 10 项逐条打勾 |

## 五环节单独使用

### 1. 选题引擎（pipeline/topics.py）

```bash
python -m pipeline.topics collect                # 抓 B站/微博/知乎热榜存热点池
python -m pipeline.topics ideate --mode evergreen --n 20   # 常青批量出题
python -m pipeline.topics ideate --mode hot --n 10         # 热点出题
python -m pipeline.topics angles T2026...        # 给选题做 10 个差异化切入角度
python -m pipeline.topics review                 # 周复盘（爆款/扑街归因）
python -m pipeline.topics list                   # 查看选题库状态
```

打分五维：热度 .25 / 竞争 .20 / 素材可得性 .20 / 合规 .20 / 账号匹配 .15，总分 ≥3.5 进 queued。
选题自动带 `angle`（切入角度）、`hook_line`（钩子）、`fact_basis`（事实边界，文稿据此写、标记[待核实]）、`keywords`（英文素材检索词）。

### 2. 文稿生成（pipeline/writer.py）

体裁：科普解说 / 盘点 / 热点评论。flash 出初稿 → pro 精修（`--no-polish` 可省）。
结构强制：钩子→3-5 小节→CTA；230-280 字/分钟；自动规避极限词、投资/医疗建议；存疑事实标 `[待核实]` 并汇入 fact_check_notes。

### 3. 配音（pipeline/tts.py）

逐句合成 → 拿到每句精确时长 → 无缝拼接。**不用 ASR 就能出精确字幕和分镜时间**。
引擎优先级：`--engine mimo`（默认，mimo-v2.5-tts，官方 /v1/chat/completions 音频模态，限时免费）
→ `edge`（微软晓晓）→ `sapi`（Windows 慧慧，离线），失败自动逐级回退。
风格可调：.env 的 `MIMO_TTS_STYLE` 一句话指令控制语速/情绪/腔调（官方支持自然语言风格控制、
标签控制、唱歌模式；音色设计 voicedesign 与声音克隆 voiceclone 也可用同样方式接入）。

### 4. 素材（pipeline/footage.py + cardgen.py）

逐句英文关键词（flash 生成）→ Pexels 横屏高清检索 → 本地素材库（素材库/ 目录，放自有实拍）
→ **自制字卡兜底**（cardgen.py 渲染大字关键词卡 + Ken Burns 缓推动镜，零版权风险）。
每个 clip 的来源与授权写入 `manifest.json` 留痕。短素材自动 `-stream_loop` 循环对齐句长。

**时事热点方向（每视频采集工单）**：时事评论画面 = 新闻引证 + 自制图解 + 口播。
流水线自动生成 `采集清单.md`（逐句事实点 → 画面类型 → 建议取材渠道 → 引用注意），
人工按单取材（官方通报/发布会回放/公开数据图表/自拍）丢进 `workdir/采集/`，
按**句子段号**命名（`005_BP环节.mp4`，段号=segments.json 的 index）即被优先使用并在 manifest 留痕，投稿包自动生成来源声明
（引用规范：著作权法 24 条适当引用——为评论/介绍而引、逐条署名，不设时长/比例上限）。
**选段辅助（pipeline/sources.py）**：批量发现候选视频（B站公开检索）＋公开字幕定位
引用时间窗 → 选段工单.md（候选/时间窗/引用文本/署名格式/勾选框）。分工：
机器找＋定位，人拍板＋取材（录屏批准的时间窗丢进 `采集/`）。B站字幕现需登录——
`.env` 填 `BILI_SESSDATA` 后时间窗全自动；不填则降级为人工定位，检索照常。
**红线**：不批量爬取平台视频、不做模拟鼠标绕反爬虫等规避技术措施的工具、不删水印署名；
定位「事件解读」而非「新闻报道」（时政报道需资质，个人号不碰）。

**图表动画（pipeline/charts.py）**：公开数据 → 自制动态图表（柱状对比/折线趋势/时间线），
1080p 动画输出，图上强制带数据来源标注，零版权。用法：
`python -m pipeline.charts spec.json out.mp4`（spec 格式见模块注释），
生成后按 `003_图表.mp4` 命名丢进 `采集/` 即被引用。
**引用统计**：出片时自动统计引用素材时长占比写入 `引用预算.txt`（仅统计，不设阈值）。
适当性按著作权法 24 条实质判断：为评论/介绍而引用、引用适当部分、逐条署名、不影响原作正常使用。

**素材来源与成本**：

| 来源 | 费用 | 授权 |
|---|---|---|
| Pexels API | 免费（200 次/时） | 可商用、无需署名 |
| Pixabay API | 免费 | 可商用（不可原样转售） |
| 自制字卡（cardgen） | 0 | 完全自有 |
| 自有实拍（手机空镜攒素材库/） | 0 | 完全自有 |
| AI 生成图 + 运镜（后续可接即梦/SD） | 按量 | 依平台条款 |

起步阶段全部用免费来源即可，无需买付费素材库（Envato/Artgrid 等）。

### 5. 成片 + 投稿（pipeline/assemble.py、publish.py）

FFmpeg：逐段 1080p30 缩放裁剪 → concat → 旁白混 BGM（自动压低）→ 字幕烧录（libass）→ 封面图。
投稿包给三平台标题（B站≤80 / 西瓜抖音≤30 / 视频号≤16）、简介、标签、AIGC 标注文案、平台规则提示。

## 合规红线（不可协商）

1. **文稿原创**：严禁洗稿、扒稿、搬运他人文案。AI 辅助创作 + 人工审核，不是"AI 去重"。
2. **素材授权**：只用 Pexels 等可商用授权素材或自有素材；严禁影视剪辑搬运。manifest 留痕备查。
3. **AIGC 标注**：依《人工智能生成合成内容标识办法》，简介带标注文案，发布时勾选平台"AI 生成内容"声明（投稿包已内置模板）。
4. **不做规避检测**：不提供"去重过原创/防搬运检测/混淆过审"类技术，这类属于黑产，不在本工具范围。
5. **资质红线**：医疗、财经、法律类需资质的内容不发；标题不用极限词、不标题党。

## 常见问题

- **控制台中文乱码/崩溃**：一律用 `python -X utf8` 运行；输出文件均 UTF-8。
- **mimo TTS 怎么调**：目标文本必须放 `role=assistant` 消息；`user` 消息放风格指令；请求发到
  根路径 `/v1/chat/completions`（非 /anthropic 前缀）带 `audio={"format":"wav","voice":"冰糖"}`，
  音频在响应 `choices[0].message.audio.data`（base64）。已封装进 tts.MimoTTS。
- **mimo LLM 返回空文本**：旧版为 thinking 吃光 max_tokens 所致，已在 llm.py 用
  `thinking:disabled` 根治并留加倍重试兜底。
- **edge-tts 失败**：需能连微软服务（可设 `HTTPS_PROXY=http://127.0.0.1:7892`）；连不上自动回退 SAPI。
- **Pexels 无 key**：自动退化为本地素材库/自制字卡，成片仍可出；申请免费 key 后自动用上实拍素材。
