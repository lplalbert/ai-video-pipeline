# -*- coding: utf-8 -*-
"""topics.py —— 选题引擎（产线环节 1/5）

职责：垂类选择依据 → 热点/常青选题采集与生成 → 素材可得性预检 → 合规门禁
→ 五维评分 → 查重/差异化角度 → 选题库（SQLite）管理。

用法（在项目根目录执行）：
    python -m pipeline.topics collect                 # 拉取热榜 → output/热点_日期.json
    python -m pipeline.topics ideate --mode evergreen --n 20
    python -m pipeline.topics ideate --mode hot --n 5
    python -m pipeline.topics angles <topic_id>       # 为某选题出 5 个差异化角度
    python -m pipeline.topics review                  # 周复盘（pro 模型）
    python -m pipeline.topics list [--status queued]  # 导出选题库（JSON+CSV）

红线（硬性）：
  1) 只产出"选题+角度"，不产出文稿内容，不复述任何现有视频的观点组合（严禁洗稿）；
  2) 选题层禁止具体数字/功效断言，只标注事实来源类型（fact_basis）；
  3) 合规黑名单命中即丢弃，不做任何"去重过原创/防搬运"设计；
  4) 全部投稿在下游环节强制 AIGC 标注（本环节在输出里携带 ai_generated=true 标记）。
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import requests

from config import LIB_DIR, OUTPUT_DIR, PEXELS_KEY
from pipeline.llm import flash, flash_json, pro

DB_PATH = OUTPUT_DIR / "topics.db"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ============================================================
# 1) 账号定位与垂类决策（fit 评分的输入）
# ============================================================
ACCOUNT_PROFILE = (
    "B站+西瓜+抖音+视频号 四平台分发的 AI 解说/科普矩阵主账号。"
    "人设：冷静克制的'资料型讲述者'，3-8 分钟横屏中视频，"
    "信息密度高、有出处、不标题党。目标观众 20-40 岁泛知识人群。"
    "主垂类：古代史×世界奇观×科技史；副垂类：自然地理冷知识、生活中的科学。"
)

# 垂类取舍决策矩阵（5 分制：素材可得性 / 热度 / 竞争低度 / 合规安全 / 广告价值）
# 结论：主推「历史×科普」交叉，避开健康/财经的高合规风险，避开影视解说红海。
VERTICAL_MATRIX = {
    "历史(古代/世界史)": {"material": 5, "heat": 4, "competition": 3, "compliance": 4, "ad_value": 4},
    "科普(天文/地理/生物)": {"material": 5, "heat": 3, "competition": 3, "compliance": 5, "ad_value": 3},
    "军事(装备史/战史)": {"material": 3, "heat": 4, "competition": 3, "compliance": 2, "ad_value": 3},
    "情感/心理": {"material": 2, "heat": 4, "competition": 2, "compliance": 3, "ad_value": 3},
    "育儿": {"material": 3, "heat": 3, "competition": 3, "compliance": 3, "ad_value": 5},
    "健康": {"material": 3, "heat": 5, "competition": 2, "compliance": 1, "ad_value": 5},
    "财经": {"material": 3, "heat": 5, "competition": 2, "compliance": 1, "ad_value": 5},
}
VERTICAL_WEIGHTS = {"material": 0.20, "heat": 0.20, "competition": 0.20, "compliance": 0.25, "ad_value": 0.15}
ACTIVE_VERTICALS = ["古代史", "世界奇观", "科技史", "自然地理", "生活科学"]  # 账号实际启用的垂类标签

# ============================================================
# 2) 选题评分模型（可编码）
#    总分 = Σ w_i * s_i（0-5 分），权重见 SCORE_WEIGHTS
#    门禁：合规 pass 且 ≥3；素材 ≥3（≥15 条命中）；总分 ≥3.5；任何单维 <2 丢弃
# ============================================================
SCORE_WEIGHTS = {"heat": 0.25, "competition": 0.20, "material": 0.20, "compliance": 0.20, "fit": 0.15}
GATE_TOTAL = 3.5
GATE_MIN_DIM = 2
GATE_MATERIAL_MIN_HITS = 15          # 一条 3 分钟视频约需 20-30 段 B-roll
DEDUP_SIM_MAX = 0.60                 # 标题 3-gram Jaccard 查重阈值

# ============================================================
# 3) 合规黑名单（题材层门禁；命中 → risk=blocked 直接丢弃）
# ============================================================
COMPLIANCE_BLACK = [
    r"选举|总统|领导人|政治局|外交|制裁|台海|南海|港独|疆独",
    r"荐股|荐基|内幕|稳赚|保本|收益率|躺赚|割韭菜|炒股秘籍",
    r"诊断|治疗|处方|偏方|药到病除|治愈|抗癌|疫苗|疫情|病毒来源",
    r"灵异|鬼魂|附体|算命|风水|迷信|未解之谜(?!科学)",
    r"未成年|儿童色情|校园暴力",
    r"地震|空难|矿难|事故.*(盘点|猎奇|合集)|灾难.*(消费|排行)",
    r"战争.*(实时|最新|今日)|战场画面",
    r"色情|擦边|约炮|赌博|毒品",
]
_COMPILED_BLACK = [re.compile(p) for p in COMPLIANCE_BLACK]
GRAY_HINT = r"减肥|增高|补肾|养生|理财|投资|股票|基金|军事演习|国际关系"

# ============================================================
# 4) 差异化角度模板（防同质化；生成选题/角度时强制轮换）
# ============================================================
ANGLE_TEMPLATES = [
    "反常识辟谣", "极端对比(古今/中外/成败)", "倒推解构(从结果问为什么)",
    "假设推演(如果……会怎样)", "量化盘点(排名/规模/速度)", "失败与代价",
    "第一视角叙事", "冷知识串联", "跨界迁移(A领域逻辑用于B)", "时间线编年",
]

# ============================================================
# 5) Prompt 模板（全文；{占位符} 用 render() 替换）
# ============================================================
P1_HOT_TO_TOPICS = """你是B站中视频（3-8分钟横屏解说）的资深选题编辑，为一个AI科普解说矩阵账号做【热点选题转化】。

账号定位：{account_profile}
垂类范围：{verticals}
今日热点素材（标题+来源+热度指标）：
{hot_items}

任务：从上面热点中挑选 0-5 个与账号垂类相关、值得做的选题，输出 JSON 数组，每个元素包含：
- topic_title: 选题标题（20字内，含具体对象，禁止"震惊""细思极恐"类空洞标题）
- hot_source: 引用的热点条目标题
- why_now: 一句话说明"为什么现在做"（时效窗口）
- angle: 切入角度（从 angle_templates 选一种并注明模板名，必须区别于平铺直叙介绍）
- hook_line: 前15秒钩子文案（一句话，40字内）
- keywords: 5-8个英文素材检索词（用于Pexels/Pixabay，必须是具体可视化的名词/动作，如 "ancient roman ruins drone"）
- fact_basis: 该选题涉及的可公开查证事实的来源类型（如 维基百科条目/官方统计/学术报道），不写任何未经查证的具体数字
- evergreen: true/false（是否长期可发）

angle_templates：{angle_templates}

硬性红线（违反即丢弃该选题）：
- 不做时政敏感、灾难事故猎奇、医疗诊断/治疗建议、荐股荐基、封建迷信、未成年人话题
- 不得沿用任何现有视频的标题或观点组合（只使用热点事实本身）
- 不得编造具体数据与功效断言，数字留给文稿环节核查

只输出 JSON 数组，不要其他文字。"""

P2_EVERGREEN_BATCH = """你是B站中视频选题编辑，为账号做【常青选题储备】。

账号定位：{account_profile}
垂类：{verticals}
今天是 {date}，未来30天可绑定的日历事件/纪念日/节气：{calendar_events}
近期已做过与已储备的选题（禁止重复或换皮）：{recent_topics}

请批量生成 {n} 个常青选题，要求：
- 覆盖至少 5 种不同的 angle_templates（{angle_templates}），同一模板不超过 3 次
- 其中不少于 {seasonal_n} 个与日历事件绑定（填 seasonal_window 字段：最佳发布日期区间）
- 每个选题输出字段：topic_title / angle(注明模板名) / hook_line / keywords(5-8个英文B-roll检索词) / fact_basis / evergreen=true / seasonal_window(可空) / why_now
- 标题必须具体：有对象、有数字或对比，20字内
- keywords 先想象"这条视频需要哪些 B-roll 画面"（空镜/地图/复原/细节特写/人群/自然现象），再反推英文检索词
- fact_basis 只写事实来源类型，禁止在选题里写具体数字断言

只输出 JSON 数组，不要其他文字。"""

P3_SCORE = """你是选题评分器。对下面的选题按 5 个维度打 0-5 的整数分，并给理由（每条理由≤30字）。

选题：{topic_json}
账号定位：{account_profile}
辅助指标（客观数据，评分必须参考）：近7日B站同类视频新增数={new_videos_7d}；热榜出现次数={hot_rank_count}；素材检索命中数={material_count}

维度定义与打分锚点：
- heat 热度（现在做有多少人看）：出现在≥2个热榜=5；1个热榜=4；无热榜但近7日同类新增20-200条=4；>200条=3（过热）；<20条且无热榜=2
- competition 竞争度（分越高=竞争越小）：同类标题近30天爆款(>10万播放)≥10条=1；3-10条=3；<3条=5
- material 素材可得性：以素材检索命中数为准，≥40=5；25-40=4；15-25=3；8-15=2；<8=1
- compliance 合规安全：健康/财经/时政敏感邻域=1；含功效或收益断言=3；一般话题且无断言=5
- fit 账号契合度：与账号定位（人设/垂类/时长）的匹配度，0-5

只输出 JSON 对象：
{"heat":{"score":n,"reason":"…"},"competition":{"score":n,"reason":"…"},"material":{"score":n,"reason":"…"},"compliance":{"score":n,"reason":"…"},"fit":{"score":n,"reason":"…"}}"""

P4_DIFFERENTIATE = """你是选题差异化设计师。同一题材在B站常有几十个同类视频，你的任务是为下面的题材设计 5 个互不相同的切入角度，避免同质化。

题材：{topic_title}
题材事实边界（只允许围绕这些公开可查证的事实展开）：{fact_basis}
同平台近30天该题材已有视频标题（仅用于避让，不得模仿其角度或观点组合，不得复述其内容）：{existing_titles}

要求：
- 5 个角度分别来自不同的 angle_template（{angle_templates}），每个输出：angle_template / 一句话角度说明 / 标题（B站风格：具体对象+数字或对比+悬念钩子，≤30字）/ hook_line（前15秒钩子，≤40字）/ outline（3-5个段落主题）
- 5 个角度的标题两两不得共享同一"卖点词+对象"组合
- 禁止复述任何已有视频的结构或观点组合（这是洗稿红线）
- 不得引入 fact_basis 之外的具体数字断言

只输出 JSON 数组，不要其他文字。"""

P5_COMPLIANCE = """你是内容合规审查员。判断下面的选题是否触及红线。
红线清单：{blacklist}
选题：{topic_json}

只输出 JSON：
{"pass": true 或 false, "risk_level": "safe|gray|blocked", "hit_keywords": ["…"], "advice": "若为gray，给出改造建议≤40字；否则留空"}"""

P6_WEEKLY_REVIEW = """你是选题策略负责人。这是过去 7 天已发布选题的表现数据（B站口径）：
{performance_rows}
账号定位：{account_profile}
当前评分权重：{weights}

请输出 JSON（字段如下）：
{
 "good_patterns": ["表现好的选题共性，≤3条，每条≤30字"],
 "bad_patterns": ["表现差的共性，≤3条"],
 "next_week_mix": {"evergreen_ratio": 0-1的小数, "hot_ratio": 0-1的小数, "verticals": {"垂类": 占比}, "angles": {"模板名": 占比}},
 "weight_tuning": {"heat": 值, "competition": 值, "material": 值, "compliance": 值, "fit": 值},
 "notes": "≤60字"
}
约束：weight_tuning 五项之和必须=1，每项相对现值变化不超过 ±0.05；合规权重只增不减。"""

SYSTEM_GUARD = "你是AI视频量产产线的选题子系统。严格输出要求的JSON。遵守内容红线：不洗稿、不搬运、不碰时政/医疗建议/荐股/迷信/灾难消费。"


# ============================================================
# 工具函数
# ============================================================
def render(tpl: str, **kw) -> str:
    out = tpl
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _http_json(url: str, **kw) -> dict | list | None:
    """GET JSON，失败返回 None（所有外网采集都必须能降级，不影响主流程）。"""
    try:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        headers.update(kw.pop("headers", {}))
        resp = requests.get(url, headers=headers, timeout=15, **kw)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 —— 采集失败一律降级
        print(f"[warn] 采集失败 {url}: {exc}")
        return None


def today() -> str:
    return date.today().isoformat()


# ============================================================
# 6) 热点/常青选题来源采集（自动 vs 手动 见文档注释）
# ============================================================
def fetch_bilibili_hot() -> list[dict]:
    """B站热门+排行榜（自动）。条目：{title, source:'bilibili_hot', metric}"""
    items: list[dict] = []
    data = _http_json("https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1")
    for v in (data or {}).get("data", {}).get("list", []) or []:
        items.append({"title": v.get("title", ""), "source": "bilibili_hot",
                      "metric": f"play={v.get('stat', {}).get('view', 0)}"})
    data = _http_json("https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all")
    for v in (data or {}).get("data", {}).get("list", []) or []:
        items.append({"title": v.get("title", ""), "source": "bilibili_rank",
                      "metric": f"play={v.get('stat', {}).get('view', 0)}"})
    return [i for i in items if i["title"]]


def fetch_weibo_hot() -> list[dict]:
    """微博热搜（自动，实时热点时效信号）。"""
    data = _http_json("https://weibo.com/ajax/side/hotSearch")
    items = []
    for v in (data or {}).get("data", {}).get("realtime", []) or []:
        if v.get("word"):
            items.append({"title": v["word"], "source": "weibo_hot",
                          "metric": f"num={v.get('num', v.get('raw_hot', 0))}"})
    return items


def fetch_zhihu_hot() -> list[dict]:
    """知乎热榜（自动，深度话题信号）。"""
    data = _http_json("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50")
    items = []
    for v in (data or {}).get("data", []) or []:
        t = (v.get("target", {}) or {}).get("title", "")
        if t:
            items.append({"title": t, "source": "zhihu_hot",
                          "metric": f"heat={v.get('detail_text', '')}"})
    return items


def fetch_onthisday() -> list[dict]:
    """历史上的今天（Wikimedia，自动）——常青纪念日来源。"""
    mm, dd = date.today().strftime("%m"), date.today().strftime("%d")
    data = _http_json(f"https://api.wikimedia.org/feed/v1/wikipedia/zh/onthisday/events/{mm}/{dd}")
    items = []
    for e in (data or {}).get("events", [])[:15]:
        text = e.get("text", "")
        year = e.get("year", "")
        if text:
            items.append({"title": f"{year}年·{text[:40]}", "source": "onthisday", "metric": ""})
    return items


# 手动/半自动来源（自动化受限，留人工入口）：
#   百度指数 index.baidu.com —— 需登录 cookie，半自动（每周手动跑 1 次趋势词）
#   新榜/蝉妈妈/抖音热点宝 —— 付费或需登录，手动（周会 15 分钟看榜单）
#   西瓜/抖音创作热点 —— App 内榜单，手动
MANUAL_SOURCES_NOTE = (
    "手动来源：百度指数(需cookie,周1次)、新榜/抖音热点宝(付费,可选)、"
    "抖音创作热点App榜单(日1次,5分钟)。手动条目用 'manual:' 前缀写入 topics.py 的 MANUAL_HOT 缓存文件。"
)

SEASONAL_EVENTS = {  # 月-日 → 可绑定的纪念日/节气（扩充用，仅作选题绑定窗口）
    "01-01": "新年/元旦", "02-14": "情人节", "03-08": "妇女节", "04-22": "世界地球日",
    "04-23": "世界读书日", "05-01": "劳动节", "05-18": "国际博物馆日", "06-01": "儿童节",
    "06-05": "世界环境日", "07-01": "建党节前后(避开时政)", "08-01": "建军节(只做装备史)",
    "09-03": "抗战胜利纪念日(只做史实科普)", "10-01": "国庆(避开时政,做山河地理)",
    "10-24": "联合国日", "11-11": "双十一(消费史角度)", "12-13": "国家公祭日(避开)",
    "12-25": "圣诞节(文化史角度)",
}


def upcoming_calendar_events(days: int = 30) -> list[str]:
    out, d0 = [], date.today()
    for i in range(days + 1):
        d = d0 + timedelta(days=i)
        key = d.strftime("%m-%d")
        if key in SEASONAL_EVENTS:
            out.append(f"{d.isoformat()} {SEASONAL_EVENTS[key]}")
    for it in fetch_onthisday()[:8]:
        out.append(f"历史上的今天: {it['title']}")
    return out


def collect_hot() -> list[dict]:
    """聚合自动来源 → output/热点_YYYYMMDD.json"""
    items: list[dict] = []
    for fn in (fetch_bilibili_hot, fetch_weibo_hot, fetch_zhihu_hot):
        got = fn()
        print(f"[collect] {fn.__name__}: {len(got)}")
        items.extend(got)
        time.sleep(2)  # 频控，避免触发平台风控
    path = OUTPUT_DIR / f"热点_{today().replace('-', '')}.json"
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[collect] 共 {len(items)} 条 → {path}")
    return items


# ============================================================
# 7) 素材可得性预检（Pexels/Pixabay，免费可商用）
# ============================================================
def pexels_hits(query: str) -> int:
    if not PEXELS_KEY:
        return -1
    data = _http_json(
        "https://api.pexels.com/videos/search",
        params={"query": query, "per_page": 80, "orientation": "landscape"},
        headers={"Authorization": PEXELS_KEY},
    )
    return len((data or {}).get("videos", [])) if data is not None else 0


def pixabay_hits(query: str) -> int:
    import os
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        return -1
    data = _http_json(
        "https://pixabay.com/api/videos/",
        params={"key": key, "q": query, "per_page": 50, "safesearch": "true"},
    )
    return len((data or {}).get("hits", [])) if data is not None else 0


def material_count(keywords: list[str]) -> int:
    """多检索词命中去重计数（近似：取每词命中数之和×0.7 折损）。"""
    total, ok = 0, False
    for kw in keywords[:8]:
        h = pexels_hits(kw)
        if h >= 0:
            total += h
            ok = True
        h2 = pixabay_hits(kw)
        if h2 >= 0:
            total += h2
            ok = True
        time.sleep(1)
    return int(total * 0.7) if ok else -1  # -1 = 未配置素材API，由人工在素材库确认


def material_score(count: int) -> int:
    if count < 0:
        return 3  # 未配置API：默认中性分，要求人工确认素材库
    if count >= 40: return 5
    if count >= 25: return 4
    if count >= 15: return 3
    if count >= 8:  return 2
    return 1


# ============================================================
# 8) 合规门禁 + 查重
# ============================================================
def compliance_keyword_check(text: str) -> dict:
    hits = [p.pattern for p in _COMPILED_BLACK if p.search(text)]
    gray = bool(re.search(GRAY_HINT, text))
    if hits:
        return {"pass": False, "risk_level": "blocked", "hit_keywords": hits, "advice": ""}
    return {"pass": True, "risk_level": "gray" if gray else "safe",
            "hit_keywords": [], "advice": "补免责声明并避免功效断言" if gray else ""}


def compliance_llm_check(topic: dict) -> dict:
    kw = compliance_keyword_check(json.dumps(topic, ensure_ascii=False))
    if not kw["pass"]:
        return kw
    try:
        out = flash_json(render(
            P5_COMPLIANCE,
            blacklist="；".join(COMPLIANCE_BLACK),
            topic_json=json.dumps(topic, ensure_ascii=False),
        ), system=SYSTEM_GUARD, temperature=0.0, max_tokens=300)
        return {
            "pass": bool(out.get("pass", True)),
            "risk_level": out.get("risk_level", "safe"),
            "hit_keywords": out.get("hit_keywords", []),
            "advice": out.get("advice", ""),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] LLM合规检查失败，回退关键词规则: {exc}")
        return kw


def _ngrams(s: str, n: int = 3) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + n] for i in range(max(1, len(s) - n + 1))}


def title_similarity(a: str, b: str) -> float:
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def is_duplicate(title: str, others: list[str]) -> tuple[bool, str]:
    for o in others:
        sim = title_similarity(title, o)
        if sim >= DEDUP_SIM_MAX:
            return True, o
    return False, ""


# ============================================================
# 9) 五维评分
# ============================================================
def score_topic(topic: dict, metrics: dict | None = None) -> dict:
    """返回 {dims:{heat:{score,reason},...}, total, pass, gates}"""
    metrics = metrics or {}
    mat_count = metrics.get("material_count")
    if mat_count is None:
        mat_count = material_count(topic.get("keywords", []))
    metrics["material_count"] = mat_count

    try:
        raw = flash_json(render(
            P3_SCORE,
            topic_json=json.dumps({k: topic.get(k) for k in
                                   ("topic_title", "angle", "hook_line", "keywords", "fact_basis")},
                                  ensure_ascii=False),
            account_profile=ACCOUNT_PROFILE,
            new_videos_7d=metrics.get("new_videos_7d", "未知"),
            hot_rank_count=metrics.get("hot_rank_count", 0),
            material_count=mat_count,
        ), system=SYSTEM_GUARD, temperature=0.2, max_tokens=700)
        dims = {k: {"score": int(raw.get(k, {}).get("score", 3)),
                    "reason": raw.get(k, {}).get("reason", "")}
                for k in SCORE_WEIGHTS}
    except Exception as exc:  # noqa: BLE001 —— LLM 失败走启发式
        print(f"[warn] LLM评分失败，走启发式: {exc}")
        dims = {
            "heat": {"score": 3, "reason": "启发式默认"},
            "competition": {"score": 3, "reason": "启发式默认"},
            "material": {"score": material_score(mat_count), "reason": f"素材命中{mat_count}"},
            "compliance": {"score": 3, "reason": "启发式默认"},
            "fit": {"score": 3, "reason": "启发式默认"},
        }
    # 素材分以客观命中数为准（覆盖 LLM）
    dims["material"] = {"score": material_score(mat_count), "reason": f"素材命中{mat_count}"}

    total = round(sum(SCORE_WEIGHTS[k] * dims[k]["score"] for k in SCORE_WEIGHTS), 2)
    compl = compliance_llm_check(topic)
    gates = {
        "compliance_pass": bool(compl["pass"]) and dims["compliance"]["score"] >= GATE_MIN_DIM,
        "material_ok": mat_count < 0 or mat_count >= GATE_MATERIAL_MIN_HITS,
        "total_ok": total >= GATE_TOTAL,
        "no_dim_below": all(dims[k]["score"] >= GATE_MIN_DIM for k in dims),
    }
    return {"dims": dims, "total": total, "compliance": compl,
            "pass": all(gates.values()), "gates": gates}


# ============================================================
# 10) 选题库（SQLite）
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS topics(
  id TEXT PRIMARY KEY,
  topic_title TEXT NOT NULL,
  angle TEXT, hook_line TEXT, keywords TEXT, fact_basis TEXT,
  vertical TEXT, source TEXT, source_url TEXT, why_now TEXT,
  seasonal_window TEXT, evergreen INTEGER DEFAULT 1,
  score_heat REAL, score_comp REAL, score_mat REAL, score_compl REAL, score_fit REAL,
  score_total REAL, compliance_level TEXT, status TEXT DEFAULT 'candidate',
  ai_generated INTEGER DEFAULT 1,
  created_at TEXT, publish_date TEXT, views_24h INTEGER, views_7d INTEGER, notes TEXT
);
"""


def db() -> sqlite3.Connection:
    OUTPUT_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_topic(conn: sqlite3.Connection, t: dict, scored: dict | None = None) -> str:
    tid = t.get("id") or f"T{datetime.now().strftime('%Y%m%d%H%M%S')}{random.randint(10, 99)}"
    dims = (scored or {}).get("dims", {})
    conn.execute(
        """INSERT OR REPLACE INTO topics
        (id, topic_title, angle, hook_line, keywords, fact_basis, vertical, source, source_url,
         why_now, seasonal_window, evergreen, score_heat, score_comp, score_mat, score_compl,
         score_fit, score_total, compliance_level, status, ai_generated, created_at, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, t.get("topic_title", ""), t.get("angle", ""), t.get("hook_line", ""),
         json.dumps(t.get("keywords", []), ensure_ascii=False), t.get("fact_basis", ""),
         t.get("vertical", ""), t.get("source", "llm"), t.get("source_url", ""),
         t.get("why_now", ""), t.get("seasonal_window", ""), int(t.get("evergreen", True)),
         dims.get("heat", {}).get("score"), dims.get("competition", {}).get("score"),
         dims.get("material", {}).get("score"), dims.get("compliance", {}).get("score"),
         dims.get("fit", {}).get("score"), (scored or {}).get("total"),
         (scored or {}).get("compliance", {}).get("risk_level", ""),
         "queued" if (scored or {}).get("pass") else "candidate",
         1, datetime.now().isoformat(timespec="seconds"),
         json.dumps({"gates": (scored or {}).get("gates", {})}, ensure_ascii=False)),
    )
    conn.commit()
    return tid


# ============================================================
# 11) 生成入口（flash 走量）
# ============================================================
def ideate_evergreen(n: int = 20) -> list[dict]:
    recent = [r["topic_title"] for r in db().execute(
        "SELECT topic_title FROM topics ORDER BY created_at DESC LIMIT 30")]
    prompt = render(
        P2_EVERGREEN_BATCH,
        account_profile=ACCOUNT_PROFILE, verticals="、".join(ACTIVE_VERTICALS),
        date=today(), calendar_events="；".join(upcoming_calendar_events()) or "无",
        recent_topics="；".join(recent) or "（空）", n=n,
        seasonal_n=max(3, n // 5), angle_templates="、".join(ANGLE_TEMPLATES),
    )
    return _normalize_topics(flash_json(prompt, system=SYSTEM_GUARD, temperature=0.9,
                                        max_tokens=6000), source="evergreen")


def ideate_hot(hot_items: list[dict], n: int = 5) -> list[dict]:
    blob = "\n".join(f"- [{i['source']}/{i.get('metric','')}] {i['title']}" for i in hot_items[:60])
    prompt = render(
        P1_HOT_TO_TOPICS,
        account_profile=ACCOUNT_PROFILE, verticals="、".join(ACTIVE_VERTICALS),
        hot_items=blob, angle_templates="、".join(ANGLE_TEMPLATES),
    )
    items = flash_json(prompt, system=SYSTEM_GUARD, temperature=0.8, max_tokens=4000)
    if isinstance(items, dict):
        items = items.get("topics") or items.get("items") or [items]
    return _normalize_topics(items[:n], source="hot")


def _normalize_topics(items, source: str) -> list[dict]:
    out = []
    for it in (items if isinstance(items, list) else []):
        if not isinstance(it, dict) or not it.get("topic_title"):
            continue
        it.setdefault("keywords", [])
        it.setdefault("evergreen", source == "evergreen")
        it["source"] = source
        out.append(it)
    return out


def run_pipeline(mode: str, n: int) -> None:
    """生成 → 查重 → 合规 → 素材预检 → 评分 → 入库。"""
    topics = ideate_evergreen(n) if mode == "evergreen" else ideate_hot(collect_hot(), n)
    conn = db()
    existing = [r["topic_title"] for r in conn.execute("SELECT topic_title FROM topics")]
    kept = 0
    for t in topics:
        dup, who = is_duplicate(t["topic_title"], existing)
        if dup:
            print(f"[dedup] 丢弃（撞题 {who}）: {t['topic_title']}")
            continue
        kw = compliance_keyword_check(t["topic_title"] + t.get("angle", "") + t.get("hook_line", ""))
        if not kw["pass"]:
            print(f"[compliance] 丢弃（{kw['hit_keywords']}）: {t['topic_title']}")
            continue
        scored = score_topic(t)
        tid = save_topic(conn, t, scored)
        existing.append(t["topic_title"])
        mark = "queued-OK" if scored["pass"] else "candidate"  # ASCII：GBK 控制台兼容
        print(f"[{mark}] {tid} 总分={scored['total']} :: {t['topic_title']}")
        kept += 1
    print(f"[done] 处理 {kept} 条 → {DB_PATH}")


def run_angles(topic_id: str) -> None:
    conn = db()
    row = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if not row:
        print(f"未找到选题 {topic_id}")
        return
    others = [r["topic_title"] for r in conn.execute(
        "SELECT topic_title FROM topics WHERE topic_title<>? LIMIT 10", (row["topic_title"],))]
    prompt = render(
        P4_DIFFERENTIATE,
        topic_title=row["topic_title"], fact_basis=row["fact_basis"] or "维基百科/公开报道",
        existing_titles="；".join(others) or "（无）",
        angle_templates="、".join(ANGLE_TEMPLATES),
    )
    angles = flash_json(prompt, system=SYSTEM_GUARD, temperature=0.85, max_tokens=3500)
    out_path = OUTPUT_DIR / f"angles_{topic_id}.json"
    out_path.write_text(json.dumps(angles, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] 5 个差异化角度 → {out_path}")


def run_review() -> None:
    rows = [dict(r) for r in db().execute(
        "SELECT topic_title, vertical, score_total, views_24h, views_7d, status "
        "FROM topics WHERE status='published' ORDER BY publish_date DESC LIMIT 50")]
    if not rows:
        print("尚无已发布选题数据，跳过周复盘。")
        return
    out = pro(render(
        P6_WEEKLY_REVIEW,
        performance_rows=json.dumps(rows, ensure_ascii=False),
        account_profile=ACCOUNT_PROFILE,
        weights=json.dumps(SCORE_WEIGHTS),
    ), system=SYSTEM_GUARD, temperature=0.3, max_tokens=1200)
    path = OUTPUT_DIR / f"review_{today().replace('-', '')}.json"
    path.write_text(out, encoding="utf-8")
    print(f"[done] 周复盘 → {path}")


def list_topics(status: str = "") -> None:
    conn = db()
    sql = "SELECT * FROM topics" + (" WHERE status=?" if status else "") + " ORDER BY score_total DESC"
    rows = [dict(r) for r in conn.execute(sql, (status,) if status else ())]
    json_path = OUTPUT_DIR / "topics_export.json"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if rows:
        csv_path = OUTPUT_DIR / "topics_export.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"[done] {len(rows)} 条 → {json_path} / {csv_path}")
    else:
        print("选题库为空。")


# ============================================================
# CLI
# ============================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="AI视频量产产线·选题引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect", help="拉取热榜")
    p_ide = sub.add_parser("ideate", help="生成+评分+入库")
    p_ide.add_argument("--mode", choices=["evergreen", "hot"], default="evergreen")
    p_ide.add_argument("--n", type=int, default=20)
    p_ang = sub.add_parser("angles", help="差异化角度设计")
    p_ang.add_argument("topic_id")
    sub.add_parser("review", help="周复盘")
    p_ls = sub.add_parser("list", help="导出选题库")
    p_ls.add_argument("--status", default="")
    args = ap.parse_args()

    if args.cmd == "collect":
        collect_hot()
    elif args.cmd == "ideate":
        run_pipeline(args.mode, args.n)
    elif args.cmd == "angles":
        run_angles(args.topic_id)
    elif args.cmd == "review":
        run_review()
    elif args.cmd == "list":
        list_topics(args.status)


if __name__ == "__main__":
    main()
