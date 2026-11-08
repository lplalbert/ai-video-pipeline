"""文稿生产线：flash 起草 → pro 精修 → 结构化口播稿。"""
import re

from pipeline.llm import extract_json, flash, pro

FORMATS = {
    "科普解说": "知识科普解说：把一个概念/现象讲透，比喻+案例，逻辑层层递进",
    "盘点": "盘点体：N个案例/事件并列展开，每个案例有钩子和转折，开头总述结尾升华",
    "热点评论": "热点评论体：一句话定性（带立场）→3-5个看点/对位拆解（每个带判词）→独到观点（争议性收束）→开放讨论",
}

SCRIPT_PROMPT = """你是B站中视频资深编剧。请为选题《{topic}》写一篇{fmt_desc}的口播文稿。

硬性要求：
1. 目标时长约{minutes}分钟，全文{lo}~{hi}个汉字（口播语速约250字/分钟）；
2. 结构：开头钩子（前5秒）→ 主体3-5个小节层层递进 → 结尾总结+互动引导；
3. 钩子三选一，必须在第一句就给立场/悬念/承诺，禁止「先交代背景/先说战况」式铺垫开场：
   ①暴论先行（先抛争议结论，让观众等着看你怎么论证）②反差/自曝框架（「要不是…我早就…」式立场张力）
   ③数据炸弹（20秒内一个反直觉数据）。结尾用争议性或前瞻性判词收束，不要四平八稳的空泛总结；
4. 观点密度：每个小节至少一个明确判词（点名到人/局/决策，是「这意味着谁的锅、谁被高估」，不是复述发生了什么）；
   每30~60秒用反问链或第一人称立场标记（「我说白了」「我的判断」「我看不到…」）钩一次观众；
   善用二选一框架（「是X还是Y」）；对大众常见叙事敢于拆解反驳，评论区才会活；
   深度靠三段链：数据→解读→影响，或版本/战术归因，或对位/细节拆解，禁止事件流水账；
5. 口播化文风：像真人在直播间跟观众聊天，短句为主、口语自然、有节奏停顿，避免书面语长难句；数字优先用汉字写法便于朗读；具体细节（比分/数据/画面）优先于空泛评论；禁止书面腔套话（「藏着值得玩味的细节」「与其说不如说」「尤为」「看不见摸不着却」这类），禁止连续排比定义句和「不是A，是B」句式连用；语气要有情绪起伏，像赛事解说不像播音员；
6. 原创与事实：观点、结构必须原创；只写确凿事实，不确定的写[待核实]，严禁编造数据和事件；观点可以狠，事实不许编。fact_check_notes 只登记正文中实际带 [待核实] 标记的点——事实边界里已给来源、可直接陈述的判词不算待核实、不进清单；正文没有待核实点就给空数组 []；
7. 严禁任何投资建议、医疗诊断建议和极限词（最好/第一/绝对）。
{topic_info}
只输出严格JSON：
{{"titles":["候选标题1","候选标题2","候选标题3"],
  "hook":"开头钩子全文",
  "sections":[{{"heading":"小节名","text":"小节口播全文"}}],
  "cta":"结尾互动引导全文",
  "tags":["标签1","标签2","标签3","标签4","标签5"],
  "fact_check_notes":["仅正文里实际带[待核实]标记的点；没有则给空数组[]"]}}"""


# v1.1（R3 预注册，2026-11-08）：长度窗分体裁 + 按实测 TTS 语速校准。
# 依据：pilot 120 run 产物完整片体裁分布（热点评论中位 1072 / 盘点 972 / 科普 1099 字 @3.5min，
# 实测白桦 TTS ≈5.3 字/s），旧 [230,280] 字/分全局窗对科普/高密度题系统性误杀（F2×11+F4×4）。
# E0 前冻结；闸=精确窗，不再叠 ±30%（带宽已含自然散布）。fmt 缺省回落 (230, 375)。
WORD_RATE = {
    "热点评论": (250, 375),
    "科普解说": (250, 385),
    "盘点": (230, 375),
}


class LlmShapeError(ValueError):
    """LLM 回包形状不符约定（坑㉞）：typed 失败，不冒充成功、不裸 AttributeError。"""


def _word_range(minutes: float, fmt: str = None) -> tuple:
    lo_r, hi_r = WORD_RATE.get(fmt, (230, 375))
    return int(minutes * lo_r), int(minutes * hi_r)


def generate_script(topic: str, fmt: str = "科普解说", minutes: float = 5.0,
                    polish: bool = True, topic_info: dict = None) -> dict:
    """生成结构化口播稿。polish=True 时用 pro 精修一遍（失败则保留 flash 初稿）。

    topic_info（可选，来自选题库）：{angle, hook_line, fact_basis, keywords}
    —— angle/hook_line 用于对齐差异化角度，fact_basis 作为事实边界约束。
    """
    lo, hi = _word_range(minutes, fmt)
    info_lines = []
    if topic_info:
        if topic_info.get("angle"):
            info_lines.append(f"切入角度（必须贴合）：{topic_info['angle']}")
        if topic_info.get("hook_line"):
            info_lines.append(f"钩子思路（可改写，勿照抄）：{topic_info['hook_line']}")
        if topic_info.get("fact_basis"):
            info_lines.append(f"事实边界（只允许写这些来源可查证的事实）：{topic_info['fact_basis']}")
    topic_info_block = ("\n选题附加信息：\n" + "\n".join(info_lines)) if info_lines else ""
    raw = flash(
        SCRIPT_PROMPT.format(
            topic=topic, fmt_desc=FORMATS.get(fmt, fmt),
            minutes=minutes, lo=lo, hi=hi, topic_info=topic_info_block,
        ),
        max_tokens=6000,
    )
    script = extract_json(raw)
    # v1.1（R4）：消费前 isinstance 防御——形状不符 typed 失败（盲重试可解），
    # 不带病进下游 flatten/word_count（F7 'list' object 崩溃族）
    if not isinstance(script, dict):
        raise LlmShapeError(f"flash script shape {type(script).__name__}, expect dict")
    if polish:
        try:
            refined = pro(
                "你是资深编剧。请精修以下口播文稿：改掉不通顺、压缩冗余、强化口语节奏，"
                "消灭AI腔书面套话，让每句话都像真人说话；"
                "保持JSON结构和字段完全不变，总字数±10%。只输出严格JSON。\n\n" + raw,
                max_tokens=6000,
            )
            refined_script = extract_json(refined)
            if isinstance(refined_script, dict):
                script = refined_script
            # 形状不符回落 flash 初稿（坑㉞：不冒充成功、不为形状烧二次生成）
        except Exception:
            pass  # 保留 flash 初稿
    return script


def flatten_sentences(script: dict) -> list:
    """把整稿摊平成句子列表（供逐句 TTS / 字幕 / 分镜使用）。

    [待核实：…] 等内部核查标记只留在 文稿.json 与 fact_check_notes，
    不进入生产面（TTS 旁白 / 字幕 / 字卡画面），避免泄漏给观众。
    """
    parts = [script.get("hook", "")]
    for sec in script.get("sections", []):
        parts.append(sec.get("text", ""))
    parts.append(script.get("cta", ""))
    text = "\n".join(p for p in parts if p)
    text = re.sub(r"\[[^\]]*待核实[^\]]*\]", "", text)
    return [s.strip() for s in re.split(r"(?<=[。！？；!?;])", text) if s.strip()]


def word_count(script: dict) -> int:
    return len(re.sub(r"\s", "", "\n".join(flatten_sentences(script))))
