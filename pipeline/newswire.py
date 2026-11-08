"""时事素材采集：每条视频一份「采集工单」——人工取材 + 引用留痕。

时事评论体裁的画面 = 新闻引证 + 自制图解 + 口播，不是通用空镜。
合规基线（红线，不可协商）：
1. 不做平台视频批量爬取/搬运；不做模拟鼠标绕过反爬虫等规避技术措施的工具。
2. 合法取材路径：官方公开发布画面（署名引用）/ 适当引用（人工选段、为评论而引、
   明确署名；法律不设秒数/比例上限）/ 公开数据自制图表 / 自有拍摄画面。
3. 定位是「事件解读」而非「新闻报道」——时政新闻报道需《互联网新闻信息服务
   许可证》资质，个人号只做评论解读，不碰政治敏感域。

用法：newswire.build_collection_plan(script, workdir, topic) → 采集清单.md
取回的素材按清单命名丢进 workdir/采集/，footage 引擎会优先使用并留痕。
"""
import json
from pathlib import Path

from pipeline.llm import extract_json, flash

QUOTE_PROMPT = """你是时事评论视频的资料编辑。根据口播文稿逐句列出事实点与配套画面需求。
只输出严格JSON数组，每项字段：
index(句子序号) / fact(该句涉及的具体事实或引述，≤30字) / entity(核心实体：人/机构/事件)
visual(画面类型，只能选：官方发布会|新闻现场|资料画面|数据图表|地图示意|人物照片|自制字卡|无)
source_hint(建议取材渠道：官方通报原文|新闻联播/官方直播回放|发布会实录|统计局等公开数据|自行拍摄|无)
cite(引用注意事项，如：需署名、只用官方发布版本、用图表转述勿直接引用、无)

句子列表：
{sentences}"""

RULES_MD = """## 引用与取材规范（每次采集前过一遍）

**合规取材（四条合法路径）**
1. 官方公开发布：政府通报、新闻发布会、官方直播回放——使用官方版本、片中与简介署名来源；
2. 适当引用：为介绍/评论某一作品或说明某一问题而引用已发表内容——引用适当部分、
   逐条署名（作者姓名/名称+作品名称）、不影响原作正常使用；法律不设秒数/比例上限；
   评论解说承载内容（平台规则：「内容实际看点依然是原作品」即判搬运）；
3. 公开数据自制图表/地图/时间线：数据来自统计局等公开发布，图自己画——零版权风险且信息量大；
4. 自有拍摄 / 素材库授权素材。

**禁止（红线）**
- 禁止批量爬取/搬运平台视频充当素材（含"只去声音/只取画面"）；
- 禁止用任何技术手段绕过平台限制抓取内容（模拟鼠标、自动化录制等规避技术措施的工具）；
- 禁止删除、遮挡原作者署名与水印；
- 不做新闻报道，不碰政治敏感、灾难猎奇、企业负面本体；涉地图必须用合规底图（自制或审图号）。

**取回素材的放置约定**
把取回的片段/图片按**句子段号**命名放进本目录的 `采集/` 文件夹：
`005_BP环节.mp4`、`007_统计局图表.png` —— 序号 = audio/segments.json 的 index（000 起），
一句一段号、可多个文件同号（引擎按段号取用）；上表事实行只是取材指引，一条事实可拆给多个句段。
流水线会优先使用并在 manifest 中留痕，投稿包自动生成来源声明。
"""


def build_collection_plan(script: dict, workdir: Path, topic: str = "") -> Path:
    """生成 采集清单.md + 采集计划.json；返回清单路径。"""
    workdir.mkdir(parents=True, exist_ok=True)
    sentences = [script.get("hook", "")] + [s.get("text", "") for s in script.get("sections", [])]
    sentences = [s for s in sentences if s]
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences))
    try:
        items = extract_json(flash(QUOTE_PROMPT.format(sentences=numbered), max_tokens=6000))
        if isinstance(items, dict):
            items = items.get("items", [])
    except Exception:
        items = []

    plan = {"topic": topic or (script.get("titles") or [""])[0], "items": items}
    (workdir / "采集计划.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [f"# 采集工单 — {plan['topic']}", "",
          "时事评论画面 = 新闻引证 + 自制图解 + 口播。按下方清单逐项取材。", "",
          "| # | 事实点 | 实体 | 画面类型 | 建议渠道 | 引用注意 |",
          "|---|---|---|---|---|---|"]
    for it in items:
        md.append(f"| {it.get('index', '')} | {it.get('fact', '')} | {it.get('entity', '')} "
                  f"| {it.get('visual', '')} | {it.get('source_hint', '')} | {it.get('cite', '')} |")
    md += ["", RULES_MD]
    out = workdir / "采集清单.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out
