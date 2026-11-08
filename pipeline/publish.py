"""投稿包：多平台标题/简介/标签 + AIGC 标注 + 发布自查清单。

合规基线：《人工智能生成合成内容标识办法》要求 AI 生成/合成内容主动标注，
各平台发布时还需勾选平台自带的「AI 生成内容」声明。
"""
import json
from datetime import date
from pathlib import Path

from pipeline.llm import extract_json, flash

AIGC_NOTE = ("【AIGC 标注】本视频文案由 AI 辅助创作并经人工审核，配音为 AI 语音合成，"
             "画面素材来自可商用素材库。")

PLATFORMS = {
    "bilibili": {
        "name": "B站",
        "title_limit": 80,
        "rules": "横屏≥1分钟可投【创作激励计划】；投稿时勾选「AI 生成内容」声明；可归入合集",
    },
    "xigua_douyin": {
        "name": "西瓜视频/抖音（中视频伙伴计划）",
        "title_limit": 30,
        "rules": "横屏≥1分钟、时长≥60秒享分成；抖音端同步需勾选 AI 声明",
    },
    "shipinhao": {
        "name": "微信视频号",
        "title_limit": 16,
        "rules": "横屏可发；声明 AI 生成；适合私域沉淀",
    },
}

TITLE_PROMPT = """你是多平台短视频运营。根据以下文稿信息为每个平台生成标题。
平台与字数上限：{platforms}
要求：核心关键词前置、有钩子感但不标题党、不使用极限词、每平台给 2 个候选。
标题中提到的看点/要点名称必须逐字使用小节标题原文，禁止改写、换词、增减要点数量；
标题最多点到 2-3 个看点，禁止把小节清单整段搬进标题；
「事件概述」「独到观点」这类结构性小节名不是看点，禁止出现在标题里；「逆袭之路」「争议与个性」这类叙事性小节名同样禁止进标题，标题卖点用具体的数字/事件/判词表达，且三平台标题不得押同一个词。
小节标题原文（看点名称白名单）：{headings}
只输出严格JSON：{{"bilibili":["",""],"xigua_douyin":["",""],"shipinhao":["",""]}}

选题：{topic}
标签：{tags}
文稿摘要（前300字）：{summary}"""


def safe_filename(title: str) -> str:
    """标题 → Windows 合法文件名（非法 ASCII 换全角，去尾点空格，截 100 字）。"""
    for ch, rep in zip('\\/:*?"<>|', '＼／：＊？＂＜＞｜'):
        title = title.replace(ch, rep)
    return title.strip().rstrip(". ")[:100]


def build_publish_kit(script: dict, workdir: Path, topic: str = "", hint: str = "",
                      titles_override: dict = None) -> Path:
    """生成 投稿包.json + 投稿包.md + 发布自查清单.md。"""
    topic = topic or (script.get("titles") or [""])[0]
    tags = script.get("tags", [])
    sentences = [script.get("hook", "")] + [s.get("text", "") for s in script.get("sections", [])]
    summary = "\n".join(sentences)[:300]
    headings = "、".join(s.get("heading", "") for s in script.get("sections", []))
    prompt = TITLE_PROMPT.format(
        platforms="；".join(f"{p['name']}≤{p['title_limit']}字" for p in PLATFORMS.values()),
        headings=headings, topic=topic, tags="、".join(tags), summary=summary)
    if hint:
        # 监督层 restrategy 指示（autoline 换策略）：上一轮标题被否决，针对性规避
        prompt += f"\n注意：上一轮标题被审核否决，原因：{hint}。本轮必须规避该问题。\n"
    if titles_override is not None:
        titles = titles_override
    else:
        try:
            titles = extract_json(flash(prompt, max_tokens=1500))
        except Exception:
            titles = None
        if not isinstance(titles, dict):
            # F8（2026-11-07 同版四臂 A4 实测）：flash 偶发回 list 形 JSON，extract_json
            # 只修语法不修语义形状，titles.get 会炸 'list' object has no attribute 'get'——
            # 形状不符一律回落兜底标题（不冒充成功、不烧二次生成）
            titles = None
        if titles is None:
            fallback_title = (script.get("titles") or [topic])[0]
            titles = {k: [fallback_title] for k in PLATFORMS}

    kit = {"date": date.today().isoformat(), "topic": topic, "tags": tags,
           "aic_note": AIGC_NOTE, "platforms": {}}
    file_title = ""
    for key, p in PLATFORMS.items():
        raw = titles.get(key, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []
        cands = [str(t)[: p["title_limit"]] for t in raw if t]
        # hook 整句直出（曾 [:80] 硬切把简介断在半句，坑㉑）
        desc = f"{script.get('hook', '')}\n\n{'#'.join(tags)}\n\n{AIGC_NOTE}"
        kit["platforms"][key] = {"name": p["name"], "titles": cands or [topic[: p["title_limit"]]],
                                 "description": desc, "rules": p["rules"]}
        if key == "bilibili":
            file_title = (cands or [topic])[0]

    # 成片文件名 = B站主标题（用户 2026-11-05 约定）；标题人肉改定后同步重命名（§6）
    video_file = safe_filename(file_title or topic) + ".mp4"
    old_video = workdir / "成片.mp4"
    if not old_video.exists():
        previous_kit = workdir / "投稿包.json"
        if previous_kit.exists():
            previous_name = json.loads(previous_kit.read_text(encoding="utf-8")).get("video_file")
            if previous_name:
                old_video = workdir / previous_name
    if old_video.exists() and old_video.name != video_file:
        target = workdir / video_file
        if target.exists():
            raise FileExistsError(f"target video already exists: {target}")
        old_video.rename(target)
    kit["video_file"] = video_file

    (workdir / "投稿包.json").write_text(json.dumps(kit, ensure_ascii=False, indent=2),
                                         encoding="utf-8")

    md = [f"# 投稿包 — {topic}", "", f"生成日期：{kit['date']}", ""]
    for key, p in kit["platforms"].items():
        md += [f"## {p['name']}", "", "标题候选：" + " / ".join(p["titles"]),
               "", f"简介：\n\n{p['description']}", "", f"平台规则：{p['rules']}", ""]
    md += ["## 通用注意事项", "", f"- {AIGC_NOTE}", "- 发布时勾选平台自带的 AI 生成内容声明",
           "- 标签：" + "、".join(tags),
           "- 如引用 Riot 赛事画面（在其 Legal Jibber Jabber 授权框架内），简介附声明："
           "[标题] was created under Riot Games' \"Legal Jibber Jabber\" policy "
           "using assets owned by Riot Games. Riot Games does not endorse or sponsor this project."
           "（另：封面/视频不得使用 Riot 商标与官方 logo）", ""]

    # 素材来源声明：引用素材必须署名，缺信息显式留「待补」，不允许无声明发布
    # 逐条署名优先从 采集/来源.json 取（{文件名: {author, work, url}}）
    smap_path = workdir / "采集" / "来源.json"
    smap = {}
    if smap_path.exists():
        try:
            smap = {k: v for k, v in json.loads(smap_path.read_text(encoding="utf-8")).items()
                    if not k.startswith("_")}
        except Exception:
            smap = {}
    mpath = workdir / "manifest.json"
    if mpath.exists():
        entries = [m for m in json.loads(mpath.read_text(encoding="utf-8"))
                   if str(m.get("source", "")).startswith(("collect:", "local:", "pexels:"))]
        if entries:
            md += ["## 素材来源声明（复制进简介或片尾鸣谢）", ""]
            for m in entries:
                src = m.get("source", "")
                if src.startswith("collect:"):
                    info = smap.get(src[8:], {})
                    if info:
                        md.append(f"- 句{m.get('index')}：{src[8:]} —— 画面来源：{info.get('author', '')}"
                                  f"《{info.get('work', '')}》（{info.get('url', '')}）"
                                  f"（引用用途：评论/介绍，适当引用）")
                    else:
                        md.append(f"- 句{m.get('index')}：{src[8:]} —— 来源链接：【待补】 署名：【待补】"
                                  f"（引用用途：评论/介绍，适当引用）")
                else:
                    md.append(f"- 句{m.get('index')}：{src}（{m.get('license', '')}）")
            md.append("")

    (workdir / "投稿包.md").write_text("\n".join(md), encoding="utf-8")

    fact_notes = "；".join(script.get("fact_check_notes", [])) or "（无）"
    checklist = f"""# 发布自查清单（每条视频发布前逐项打勾）
- [ ] 事实核查完成（无[待核实]残留）：{fact_notes}（核查→改 文稿.json→--resume 自动重配重出→再勾选）
- [ ] 文稿为原创内容，未洗稿/未搬运他人文案
- [ ] 全部画面素材可商用授权（manifest 已留痕）
- [ ] 引用素材为评论/介绍而引用、逐条署名（作者+作品名称）、不影响原作正常使用（见 引用预算.txt）
- [ ] 已在简介/声明中标注 AIGC 内容
- [ ] 平台发布页已勾选「AI 生成内容」声明
- [ ] 标题无极限词、无标题党、无虚假信息
- [ ] 成片文件名=终稿 B站主标题（改标题后同步重命名视频文件，用户 2026-11-05 约定）
- [ ] 医疗/财经类内容已核对是否需要资质（需要则不发）
- [ ] 封面与标题一致、清晰可读
- [ ] 音画同步、字幕无错位（成片已抽检）
- [ ] 已记录发布时间与链接，便于数据复盘
"""
    (workdir / "发布自查清单.md").write_text(checklist, encoding="utf-8")
    return workdir / "投稿包.md"
