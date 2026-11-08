---
name: dl-video
description: 按关键词批量下载网络视频到本地。YouTube 走 youtube-dl（dl_ytdl.py，2026-11-04 实测可下，支持按上传日期取最新、同场去重）；B站及其他站点走 yt-dlp（dl_video.py）。当用户给出某个关键词/话题并想要相关视频时使用，例如"下载 lol 总决赛 IG vs TES 相关的视频"、"下载 NBA 勇士最新全场集锦"、"给我下 5 条 IG vs TES 的视频"、"下载这个视频链接"。按关键词搜索取前 N 条，归档去重、可续跑。
---

# dl-video — 按关键词批量下载视频

两个引擎，按站点选：

| 用户要的 | 执行体 | 引擎 |
|---|---|---|
| **YouTube**（关键词/URL/最新集锦） | `dl_ytdl.py` | **youtube-dl**（ytdl-org master） |
| **B站**（关键词/URL） | `dl_video.py` | yt-dlp |
| 其他站点 URL（西瓜/抖音/微博…） | `dl_video.py` | yt-dlp（1755 个 extractor） |

依据：yt-dlp 下 YouTube 撞 PO-Token 反爬墙（下不动）；youtube-dl master 实测可下（见实测状态）。B站则相反，yt-dlp 实测全通。

---

## YouTube 下载（dl_ytdl.py + youtube-dl）

### 用法流程

1. **提取关键词**：英文命中更高（如 `Golden State Warriors Full Game Highlights`）；"最新"类诉求用 `--sort date`（默认）。
2. **定条数/过滤**：没说默认 `--n 5`；有明显内容形态（集锦/短片）时用 `--min-dur/--max-dur` 卡范围（如全场集锦约 5–30 分钟：`--min-dur 300 --max-dur 1800`）。
3. **同场去重**：同一场比赛/事件当天常有多条重复上传——事件类加 `--per-day 1`（每天只取一条）。已在归档的场次会占掉当日名额，重跑自动续选新场次。
4. **不确定就先 `--list`**：会列候选（日期/时长/ID/标题）并标注本次将选哪几条。
5. **跑脚本**（项目根目录）：

```bash
python -X utf8 .claude/skills/dl-video/dl_ytdl.py "Golden State Warriors Full Game Highlights" --n 3 --per-day 1 --min-dur 300 --max-dur 1800
```

6. **回报结果**：把脚本末尾"新下载 N 个 + 文件清单"原样告诉用户，说明落在哪个目录。

### 参数速查

| 参数 | 作用 | 默认 |
|---|---|---|
| 位置参数 | 一个或多个关键词（也可直接给视频 URL） | 必填 |
| `--n` | 每个关键词下载几条 | 5 |
| `--sort` | `date`=按上传日期新→旧（"最新"用它）；`rel`=按相关度 | date |
| `--per-day` | 同一上传日期最多取几条（同场去重，取 1=每场一条） | 0（不限） |
| `--out` | 输出根目录，按关键词建子目录；URL 归入 `url/` | `downloads` |
| `--max-height` | 分辨率上限 | 1080 |
| `--min-dur` / `--max-dur` | 时长过滤（秒），0 不限 | 0 |
| `--list` | 只列候选不下载（✓ 标注将选中哪几条） | 关 |
| `--archive` | 归档文件路径（跨次去重） | `<out>/.dl_archive.txt` |

### 典型命令

```bash
# 先看候选再决定（推荐不确定时先跑）
python -X utf8 .claude/skills/dl-video/dl_ytdl.py "Golden State Warriors Full Game Highlights" --n 3 --per-day 1 --min-dur 300 --max-dur 1800 --list

# NBA 全场集锦实战配方：最新、每场一条、只要真集锦长度（2026-11-04 用它下过勇士最近 3 场）
python -X utf8 .claude/skills/dl-video/dl_ytdl.py "Golden State Warriors Full Game Highlights" --n 3 --per-day 1 --min-dur 300 --max-dur 1800

# 单个 YouTube 链接直下
python -X utf8 .claude/skills/dl-video/dl_ytdl.py "https://www.youtube.com/watch?v=XXXX"

# 相关度排序、不限同场、省空间 720p
python -X utf8 .claude/skills/dl-video/dl_ytdl.py "IG vs TES" --n 5 --sort rel --max-height 720
```

### 安装/更新（重要）

- **PyPI 版 youtube-dl（2021.12.17）已废**：解析 YouTube 报 `The page needs to be reloaded`，别用。
- **必须 GitHub master 版**：`pip install git+https://github.com/ytdl-org/youtube-dl.git`
- 本机已装 master（2025.04.07），源码浅克隆在 `_ytdl_build/youtube-dl/`；更新 = 进该目录 `git pull` 再 `pip install .`。
- 脚本一律 `python -X utf8 -m youtube_dl` 子进程调用，不依赖 PATH 上的 youtube-dl.exe。

### 机制说明

- **归档去重**：已下视频 ID 记进 `<out>/.dl_archive.txt`，精选时跳过且**占掉当日名额**（防止重跑下同场重复上传），续跑自动选下一批新场次。
- **格式**：`bestvideo[height<=H]+bestaudio/best[height<=H]/best` 合并 mp4，旁存 `.info.json` 元数据。
- **候选池**：搜索取 `max(--n × 3, 12)` 条再精选；池里不够 N 条属正常（重跑续选）。
- **限速**：下载间隔 2s + 重试 5 次。

---

## B站等其他站点（dl_video.py + yt-dlp）

用 yt-dlp 按关键词搜索并批量下载视频到本地。

### 用法

1. **提取关键词**：比赛/事件类保留对阵双方、赛事名、年份等实义词。中英文都行。
2. **确定条数**：用户说"5 条/几条"就用它；没说默认 `--n 5`。
3. **确定来源**：默认 B 站（`--source bilibili`）。URL 直传则任何 yt-dlp 支持的站点都能下。
4. **跑脚本**（在项目根目录）：

```bash
python -X utf8 .claude/skills/dl-video/dl_video.py "IG vs TES worlds 2026 finals" --n 5 --out downloads
```

5. **回报结果**：把脚本末尾打印的"新下载 N 个 + 文件清单"原样告诉用户，并说明落在哪个目录。

### 参数速查

| 参数 | 作用 | 默认 |
|---|---|---|
| 位置参数 | 一个或多个关键词（也可直接给视频 URL） | 必填 |
| `--n` | 每个关键词下几条 | 5 |
| `--source` | `bilibili` / `youtube` / `both` | bilibili |
| `--out` | 输出根目录，按关键词建子目录 | `downloads` |
| `--max-height` | 分辨率上限（1080/720/2160…） | 1080 |
| `--min-dur` / `--max-dur` | 时长过滤（秒），0 不限 | 0 |
| `--list` | **只搜不下载**，先列候选给用户看 | 关 |
| `--archive` | 归档文件路径（跨次去重） | `<out>/.dl_archive.txt` |

### 典型命令

```bash
# 想先看候选再决定下哪些（B站搜索）
python -X utf8 .claude/skills/dl-video/dl_video.py "IG TES 总决赛" --n 8 --list

# 下载 B站关键词前 5 条（默认源就是 bilibili，--source 可省）
python -X utf8 .claude/skills/dl-video/dl_video.py "IG TES 总决赛" --n 5

# 直接给任意站点的视频 URL（B站/西瓜/微博/vimeo…… 1755 个 extractor 都认）
python -X utf8 .claude/skills/dl-video/dl_video.py "https://www.bilibili.com/video/BV1Xxx..." --out 素材库/yt

# 只要短片段（<2 分钟）、省空间
python -X utf8 .claude/skills/dl-video/dl_video.py "IG TES" --n 10 --max-dur 120 --max-height 720
```

### 机制说明

- **归档去重**：已下的视频 ID 记进 `<out>/.dl_archive.txt`，重跑自动跳过。
- **格式**：≤1080p 最佳视频+音频合并 mp4，旁存 `.info.json`。
- **限速/重试**：1–5s 间隔 + 5 次重试。
- **单关键词下载完**：按 `--n` 取搜索前 N 条；命中少于 N 条属正常。

---

## 各站实测状态（2026-11-04）

- **YouTube：yt-dlp 弃用；youtube-dl master 实测全通**。yt-dlp 有 PO-Token 反爬墙，无插件时
  只出图片流（用户已决定不再用 yt-dlp 反爬）；**youtube-dl master 实测搜索+下载+合并 mp4 全通，
  无人机验证墙**（单条 19s 测试 + 勇士 3 场集锦 1.5GB 实战都成）。下载走 `dl_ytdl.py`。
- **B站：搜索+下载全通**（dl_video.py 默认源）。无登录态上限 1080P/60 需大会员——用 `--cookies`
  给 B站 cookies.txt 可解，不影响普通画质。搜索偶发 HTTP 412（风控），重试即过。
- **其他站点**：yt-dlp 1755 个 extractor，URL 直传即可试。遇到具体站点失败如实回报错误，不要臆测。

**cookie 注意**：提取浏览器 cookie 属于访问用户登录凭据，**必须由用户明确授权/自行执行**，
不要替用户自动跑 `--cookies-from-browser`。需要时把命令给用户，让 ta 自己导出
cookies.txt（Netscape 格式）再用 `--cookies` 传入。

## 注意

- Windows 下调 Python 一律带 `-X utf8`（项目约定）。
- 下载的东西放 `downloads/` 或 `素材库/yt/`，**不会自动进产线**；要进产线由用户自己决定，
  且须先过 `collect_cc.py` 的来源/授权审查（项目红线：不搬运）。
- 命令失败先看依赖：yt-dlp 路线 `pip install yt-dlp`；youtube-dl 路线须 GitHub master（见上）；
  合并 mp4 都需要 ffmpeg。
