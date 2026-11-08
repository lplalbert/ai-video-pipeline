# -*- coding: utf-8 -*-
"""autoline.py —— 无人值守产线循环（不依赖 agent 的三层架构）。

三层架构（用户 2026-11-05 设计约束）：
  1) 程序逻辑层：确定性子任务自动执行（token 节省的基础）。
     逐子任务做成本收益评估——出错率极低且失败重跑成本低的**不设检查点**
     （见 STAGE_POLICY：produce 环节 TTS/合成有引擎回退+构建断言，直接跑）。
  2) 监督层=检查点门控器（不是全历史持有者）：程序在关键节点产出结构化状态
     （成功/失败、错误类型、已尝试动作、证据摘要），监督只在**摘要**上决策：
     重试 / 换策略 / 升级给人 / 继续。要判断力，不要记忆力。
     默认规则决策（零 token）；--supervise-llm 时用 flash 只喂摘要+经验，
     该调用计入 supervisor 层台账（coordination 本身有成本，单独计量）。
  3) 经验层独立于监督层：每次失败提取结构化教训（场景/动作/结果/下次做法）
     存 output/lessons.jsonl；同类子任务执行前检索注入程序提示词与监督上下文
     （Experia 0 重复错误率路线）。

token 权衡（用户提醒）：监督调用频率 × 上下文体积 与「程序层省下的 token」同表对比
（`report` 子命令）；监督太粗出错损失大、太细协调吃掉节省——
平衡点按实际子任务出错率 × 失败成本调（STAGE_POLICY 记录当前取舍）。

用法：
  python -X utf8 autoline.py once --mode hot                # 出一条（选题→…→投稿包）
  python -X utf8 autoline.py once --topic "IG vs GEN 前瞻"  # 指定选题
  python -X utf8 autoline.py loop --mode hot --max-runs 3 --interval 21600
  python -X utf8 autoline.py once --workdir output/xxx --resume   # 从检查点续跑
  python -X utf8 autoline.py report                         # token/经验/翻车报告

发布闸门（红线）：默认 dry-run 不碰线上；--publish 只到 --private 上传
（AIGC 勾选必人工两步，坑㉖）；NBA 比赛画面题材**不发 B站**（用户 2026-11-05），
脚本自动识别并转多平台待发清单。
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import MODEL_FLASH, MODEL_PRO, OUTPUT_DIR
from pipeline import assemble, matcher, newswire, publish, sources, subtitles, topics, tts, writer
from pipeline.llm import USAGE, current_run_id, usage_summary
from pipeline.llm import usage_context

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ============================================================
# 检查点成本收益表（2026-11-05 实测出错率定的取舍，跑一段后按 report 复核再调）
#   gate  = 关键节点出结构化状态 → 监督决策（重试/换策略/升级/继续）
#   auto  = 纯程序直跑（出错率极低且重跑便宜，不值得拆检查点——用户成本收益原则）
# ============================================================
STAGE_POLICY = {
    "topic":   ("gate", "选题错=整条白做；LLM 出题廉价可重跑"),
    "script":  ("gate", "文稿/标题历史翻车 4+ 次（坑⑦㉑㉒）；[待核实] 超标=事实底座不扎实"),
    "footage": ("gate", "素材落点历史 7/38 错位（坑㉔）；纯脚本只能给置信度，低置信必过门"),
    "produce": ("auto", "TTS 引擎三级回退+逐句缓存、合成有 yuv420p/立体声构建断言——出错率极低、重跑便宜"),
    "kit":     ("gate", "标题跨平台同质/结构词/极限词历史必犯；lint 必过"),
    "publish": ("gate", "对外不可逆动作；NBA 禁区+AIGC 人工勾选（坑㉖）"),
}

NBA_HINT = re.compile(
    r"NBA|伦德伯格|Lendeborg|勇士|湖人|凯尔特人|雷霆|森林狼|尼克斯|骑士|火箭|独行侠|快船|步行者"
    r"|雄鹿|76人|公牛|老鹰|黄蜂|活塞|魔术|篮网|奇才|猛龙|爵士|掘金|太阳|国王|鹈鹕|灰熊|马刺|开拓者"
    r"|东契奇|约基奇|文班亚马|库里|詹姆斯|杜兰特|爱德华兹|塔图姆|亚历山大", re.I)

LESSONS_PATH = OUTPUT_DIR / "lessons.jsonl"
_EXPERIENCE_ENABLED = contextvars.ContextVar("experience_enabled", default=True)
_EXPERIENCE_PATH = contextvars.ContextVar("experience_path", default=LESSONS_PATH)


@contextlib.contextmanager
def experience_context(enabled: bool, path: Path = None):
    e = _EXPERIENCE_ENABLED.set(enabled)
    p = _EXPERIENCE_PATH.set(path or LESSONS_PATH)
    try:
        yield
    finally:
        _EXPERIENCE_PATH.reset(p)
        _EXPERIENCE_ENABLED.reset(e)
STATE_PATH = OUTPUT_DIR / "autoline_state.json"
DECISION_STATS_PATH = OUTPUT_DIR / "decision_stats.json"  # (stage,error_type,action)→成败，升级顺序学习
_STATE_PATH = contextvars.ContextVar("state_path", default=STATE_PATH)
_STATS_PATH = contextvars.ContextVar("stats_path", default=DECISION_STATS_PATH)
_HINDSIGHT_PATH = contextvars.ContextVar("hindsight_path", default=OUTPUT_DIR / "hindsight_log.jsonl")
_STATS_ENABLED = contextvars.ContextVar("stats_enabled", default=True)


@contextlib.contextmanager
def experiment_io(workdir: Path = None, stats_path: Path = None, stats_enabled: bool = True):
    tokens = [
        (_STATE_PATH, _STATE_PATH.set(workdir / "state.json" if workdir else STATE_PATH)),
        (_STATS_PATH, _STATS_PATH.set(stats_path or DECISION_STATS_PATH)),
        (_HINDSIGHT_PATH, _HINDSIGHT_PATH.set(workdir / "hindsight_log.jsonl" if workdir else OUTPUT_DIR / "hindsight_log.jsonl")),
        (_STATS_ENABLED, _STATS_ENABLED.set(stats_enabled)),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
MAX_ATTEMPTS = 2          # 每个 gate 阶段自动重试上限，超了升级给人
LOW_CONF_MAX_RATIO = 0.15  # footage 低置信句占比超过 15% → 升级给人

# ── 研究落地：省 token 介入谱系（调研 2026-11-06，详见 监督谱系落地报告_20261106.md）──
SELF_HEALING = {"llm_bad_json", "JSONDecodeError", "ConnectionError", "TimeoutError"}
    # L1/ReCoF(2610.36471)：预测可自愈的错误不经监督直接重试（介入 vs 等待的判据）
KNOWN_ERRORS = {"llm_bad_json", "title_lint", "script_gate", "clip_low_conf",
                "publish_blocked", "preflight"}   # 已知→规则高置信；未知/可疑→才留给监督 LLM
SUPERVISOR_LLM_BUDGET = 3   # L3/ECMR(2610.17869)：单次 run 监督 LLM 调用预算，花完退回规则
_sup_budget_left = SUPERVISOR_LLM_BUDGET


# ============================================================
# 0) 结构化状态（检查点）——程序产出，监督只读摘要
# ============================================================

def make_state(stage: str, status: str, error_type: str = "", attempts: int = 1,
               actions_tried: list = None, evidence: dict = None) -> dict:
    return {
        "run_id": current_run_id(),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
        "status": status,           # ok | failed | suspicious
        "error_type": error_type,   # 机器可分型：llm_bad_json / clip_low_conf / title_lint ...
        "attempts": attempts,
        "actions_tried": actions_tried or [],   # 已尝试动作
        "evidence": evidence or {},             # 证据摘要（不带全量历史）
    }


def write_checkpoint(workdir: Path, state: dict) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "checkpoint.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    _update_global_state(state)


# ---------- L0 零成本先筛（StateMirror 2610.32648：执行前确定性校验，宁可拦在执行前） ----------

def preflight(name: str, workdir: Path, topic_info: dict) -> list:
    """阶段执行前的确定性前置校验（零 token）。返回违规清单，空=放行。
    拦的是「注定失败/空转的执行」——比失败后恢复便宜一到两个数量级。"""
    bad = []
    if name == "script" and not (topic_info.get("fact_basis") or "").strip():
        bad.append("事实底座为空——文稿必出编造，先补 选题.json 的 fact_basis")
    if name in ("footage", "produce", "kit", "publish") and not (workdir / "文稿.json").exists():
        bad.append("缺 文稿.json——上游阶段未产出")
    if name == "kit" and not (workdir / "成片.mp4").exists() and not list(workdir.glob("*.mp4")):
        bad.append("缺 成片.mp4——制作阶段未产出")
    if name == "publish":
        kit = workdir / "投稿包.json"
        if not kit.exists():
            bad.append("缺 投稿包.json")
        else:
            try:
                k = json.loads(kit.read_text(encoding="utf-8"))
                if not k.get("platforms", {}).get("bilibili", {}).get("titles"):
                    bad.append("投稿包无 B站标题")
            except Exception as exc:
                bad.append(f"投稿包.json 解析失败：{exc}")
    return bad


def record_outcome(state: dict, decision: dict, final: str) -> None:
    """L3 升级梯的经验数据（Who Plans 2610.24234 / TACIT-Switch 贝叶斯式）+
    L2/HiSentinel(2609.39957) 教师信号：记录 (状态摘要, 决策, 终局) 供事后蒸馏小哨兵。
    final ∈ ok | escalated | gave_up"""
    row = {"ts": datetime.now().isoformat(timespec="seconds"),
           "run_id": state.get("run_id"),
           "stage": state["stage"], "error_type": state.get("error_type", ""),
           "attempts": state.get("attempts", 1),
           "action": decision.get("action", ""), "reason": decision.get("reason", ""),
           "evidence_keys": sorted((state.get("evidence") or {}).keys()),
           "final": final}
    hindsight_path = _HINDSIGHT_PATH.get()
    hindsight_path.parent.mkdir(parents=True, exist_ok=True)
    with open(hindsight_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    if not _STATS_ENABLED.get():
        return
    key = f"{state['stage']}|{state.get('error_type', '') or 'unknown'}|{decision.get('action', '')}"
    stats_path = _STATS_PATH.get()
    try:
        stats = (json.loads(stats_path.read_text(encoding="utf-8"))
                 if stats_path.exists() else {})
        s = stats.setdefault(key, {"ok": 0, "fail": 0})
        s["ok" if final == "ok" else "fail"] += 1
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    except Exception:
        pass


def best_action_by_stats(stage: str, error_type: str, candidates: list) -> str | None:
    """L3：按历史成功率选升级动作（bandit 式经验选择，零 token）。样本 <3 不信。"""
    if not _STATS_ENABLED.get():
        return None
    stats_path = _STATS_PATH.get()
    try:
        stats = (json.loads(stats_path.read_text(encoding="utf-8"))
                 if stats_path.exists() else {})
    except Exception:
        return None
    best, best_rate = None, -1.0
    for a in candidates:
        s = stats.get(f"{stage}|{error_type or 'unknown'}|{a}")
        if not s or (s["ok"] + s["fail"]) < 3:
            continue
        rate = s["ok"] / (s["ok"] + s["fail"])
        if rate > best_rate:
            best, best_rate = a, rate
    return best


def _update_global_state(state: dict) -> None:
    try:
        state_path = _STATE_PATH.get()
        st = {}
        if state_path.exists():
            st = json.loads(state_path.read_text(encoding="utf-8"))
        st.setdefault("runs", []).append(state)
        st["last"] = state
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ============================================================
# 1) 经验层（独立于监督层；失败→教训→下次同类子任务前注入）
# ============================================================

def lessons_all() -> list:
    if not _EXPERIENCE_ENABLED.get():
        return []
    path = _EXPERIENCE_PATH.get()
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def lessons_for(stage: str, k: int = 3) -> list:
    """检索同阶段最近 k 条教训（注入程序提示词/监督上下文）。"""
    hits = [l for l in lessons_all() if l.get("stage") == stage]
    return hits[-k:]


def lesson_add(stage: str, scene: str, action: str, result: str, next_do: str) -> None:
    if not _EXPERIENCE_ENABLED.get():
        return
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "stage": stage,
           "scene": scene, "action": action, "result": result, "next_do": next_do}
    path = _EXPERIENCE_PATH.get()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def lessons_block(stage: str) -> str:
    ls = lessons_for(stage)
    if not ls:
        return ""
    lines = [f"- 场景：{l['scene']}｜做过：{l['action']}｜结果：{l['result']}｜下次：{l['next_do']}"
             for l in ls]
    return "\n经验提醒（历史教训，务必规避）：\n" + "\n".join(lines)


# ============================================================
# 2) 监督层 = 检查点门控器（只看状态摘要 + 相关经验 → 决策）
# ============================================================

def supervise(state: dict, use_llm: bool = False) -> dict:
    """输入：结构化状态摘要（不是全历史）。输出：{action, reason, confidence, mechanism}。

    action ∈ retry | restrategy | continue | escalate
    L1/SMAS(2510.26585)+Watchdogs：LLM-free 规则先行，confidence=high 直接执行（零 token），
    只有可疑/未知错误类型才升级监督 LLM——确定性 monitor 主导，LLM 只补位。
    L3/ECMR(2610.17869)：监督 LLM 有预算上限，花完退回规则（协调开销单独计量见 token_ledger）。
    """
    global _sup_budget_left
    d = _supervise_rules(state)
    if not use_llm or d.get("confidence") == "high":
        d.setdefault("mechanism", "rules")
        return d
    if _sup_budget_left <= 0:
        d["mechanism"] = "rules/budget-exhausted"
        d["reason"] = (d.get("reason", "") + "（监督 LLM 预算耗尽，按规则执行）").strip()
        return d
    _sup_budget_left -= 1
    d2 = _supervise_llm(state)
    d2["mechanism"] = "supervisor-llm"
    return d2


def _supervise_rules(state: dict) -> dict:
    st, et, att = state["stage"], state.get("error_type", ""), state.get("attempts", 1)
    ev = state.get("evidence", {})

    if state["status"] == "ok":
        low = ev.get("low_conf", [])
        if st == "footage" and low:
            ratio = len(low) / max(1, ev.get("total", len(low)))
            if ratio > LOW_CONF_MAX_RATIO:
                return {"action": "continue", "confidence": "high",
                        "reason": f"低置信句占比 {ratio:.0%} > {LOW_CONF_MAX_RATIO:.0%}；"
                                  "低置信候选未入映射，改用字卡并列入人工画面复核"}
            return {"action": "continue", "confidence": "high",
                    "reason": f"{len(low)} 句低置信已列清单，占比可接受，字卡兜底继续"}
        return {"action": "continue", "confidence": "high", "reason": "阶段产出达标"}

    # failed / suspicious
    # L1/ReCoF：可自愈错误=等待而非介入（盲重试，不回灌失败信息——L3/Blind Resampling
    # 2607.26117：看旧失败尝试会锚定，33-68% 重出近似产物；盲重采样更强且省 2.5-5.5× token）
    if et in SELF_HEALING and att < MAX_ATTEMPTS:
        return {"action": "retry", "confidence": "high",
                "reason": f"{et} 属可自愈错误——盲重采样（不喂失败上下文）"}
    if et == "title_lint":
        if att < MAX_ATTEMPTS:
            return {"action": "restrategy", "confidence": "high",
                    "reason": "标题 lint 违规——把违规项回灌提示词重生成（换策略）"}
        return {"action": "escalate", "confidence": "high", "reason": "标题两轮未过 lint，升级给人定标题"}
    if et == "script_gate":
        problems = ev.get("bad", [])
        if att < MAX_ATTEMPTS and problems and all("字数" in item for item in problems):
            return {"action": "restrategy", "confidence": "high",
                    "reason": "文稿长度未达标；下一版按目标字数重写，保留事实边界：" + "；".join(problems)}
        return {"action": "escalate", "confidence": "high", "reason": "文稿闸门不过（句数/字数/[待核实]），升级给人改稿"}
    if et == "clip_low_conf":
        return {"action": "escalate", "confidence": "high", "reason": "素材落点大面积低置信，升级给人取材"}
    if et == "preflight":
        return {"action": "escalate", "confidence": "high", "reason": "前置校验不过（缺输入/缺成片），升级给人补前置"}
    if et == "publish_blocked":
        return {"action": "escalate", "confidence": "high", "reason": "发布闸门拦截（NBA 禁区/无成片），升级给人"}
    if att < MAX_ATTEMPTS:
        # L3 bandit：同 (stage,error_type) 历史哪种动作救回率最高就用哪种（样本≥3 才信）
        hist = best_action_by_stats(st, et, ["retry", "restrategy"])
        if hist:
            return {"action": hist, "confidence": "high",
                    "reason": f"{et or st} 未超上限——历史{hist}救回率最高（决策统计）"}
        return {"action": "retry", "confidence": "high", "reason": f"{et or st} 未超重试上限"}
    if et in KNOWN_ERRORS:
        return {"action": "escalate", "confidence": "high", "reason": f"{et or st} 重试 {att} 次仍失败，升级给人"}
    # L1 触发升级 LLM 的唯一入口：未知/可疑错误类型 且 重试预算已耗尽（2026-11-07 审计 F6：
    # 单次未知故障在 att<MAX 时被上方规则高置信 retry 吞掉，监督 LLM 只见「未知+重复失败」）
    return {"action": "retry", "confidence": "low",
            "reason": f"未知错误类型 {et or st}——规则不敢拍板，留给监督 LLM 判"}


def _supervise_llm(state: dict) -> dict:
    """监督 LLM：只喂结构化摘要+经验（判断力，不喂全历史）。"""
    from pipeline.llm import flash, extract_json, set_layer
    prompt = (
        "你是无人产线的检查点门控器。下面是程序在关键节点产出的结构化状态摘要"
        "（不含全量历史）。只依据摘要与经验做决策。\n"
        f"状态摘要：{json.dumps(state, ensure_ascii=False)}\n"
        f"{lessons_block(state['stage'])}\n"
        "可选决策：retry（重跑本阶段）/ restrategy（换策略，写明换什么）/"
        "continue（带已知小瑕疵继续）/ escalate（升级给人，写明人要做什么）。\n"
        '只输出JSON：{"action":"retry|restrategy|continue|escalate","reason":"≤40字",'
        '"lesson":{"scene":"","action":"","result":"","next_do":""}}'
    )
    try:
        with set_layer("supervisor"):  # 监督调用单独计量（coordination 有成本）
            d = extract_json(flash(prompt, max_tokens=600))
        return {"action": d.get("action", "escalate"),
                "reason": d.get("reason", ""),
                "lesson": d.get("lesson")}
    except Exception as exc:
        return {"action": "escalate", "reason": f"监督 LLM 失败（{exc}），默认升级给人"}


def apply_decision(state: dict, decision: dict, workdir: Path) -> None:
    """决策落地：记教训 + 打检查点。"""
    if decision.get("lesson") and decision["lesson"].get("next_do"):
        l = decision["lesson"]
        lesson_add(state["stage"], l.get("scene", state.get("error_type", "")),
                   l.get("action", ""), l.get("result", ""), l["next_do"])
    state["decision"] = decision
    write_checkpoint(workdir, state)
    print(f"  [监督] {decision['action']}：{decision.get('reason', '')}")


# ============================================================
# 3) 程序逻辑层——各阶段（确定性子任务自动执行）
# ============================================================

def slugify(text: str, n: int = 18) -> str:
    s = re.sub(r"[^\w一-鿿]+", "", text)[:n] or "video"
    return f"{datetime.now().strftime('%Y-%m-%d')}_{s}"


def pick_topic_from_db() -> dict | None:
    conn = topics.db()
    row = conn.execute(
        "SELECT * FROM topics WHERE status='queued' ORDER BY score_total DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM topics WHERE status='candidate' ORDER BY score_total DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    t = dict(row)
    t["keywords"] = json.loads(t.get("keywords") or "[]")
    conn.execute("UPDATE topics SET status='in_production' WHERE id=?", (t["id"],))
    conn.commit()
    return t


def stage_topic(args) -> dict:
    if args.topic:
        return {"topic_title": args.topic}
    if args.ideate:
        topics.run_pipeline(args.mode, args.ideate)
    t = pick_topic_from_db()
    if not t:
        raise RuntimeError("选题库为空（跑 --ideate N 或指定 --topic）")
    return t


def compile_seed(workdir: Path, topic: str) -> Path:
    """程序层事实底座（LLM API 属程序组件，不算 agent）：
    B站搜索摘要 → mimo 提取 angle/hook_line/fact_basis/keywords/source_bvs → 选题.json。
    agent 不在场时循环也能起跑；种子质量由文稿闸门（[待核实] 计数）把关。"""
    from pipeline import llm, sources
    rows = sources.bili_search(topic, n=8) or []
    digest = "\n".join(f"- {r.get('title', '')}（{r.get('author', '')}）bvid={r.get('bvid', '')}"
                       for r in rows)
    out = llm.flash_json(
        f"""你是选题研究员。根据搜索摘要为视频选题整理事实底座。
选题：{topic}
搜索摘要（视频标题/作者/bvid）：
{digest or '（无搜索结果）'}

输出 JSON：
{{"angle": "切入角度一句话", "hook_line": "钩子一句话",
  "fact_basis": "事实边界：只列摘要能支撑的事实，每条注明来源；摘要支撑不了的写[待核实]，不许编造",
  "keywords": ["词"], "source_bvs": ["bvid"]}}
source_bvs 取摘要里与选题最相关、适合引用的 3-6 个 bvid。""")
    p = workdir / "选题.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def title_lint(kit: dict) -> list:
    """标题程序 lint（确定性，零 token）——坑⑦⑳㉑㉒ 的机械化拦截。"""
    bad = []
    plat = kit.get("platforms", {})
    if not isinstance(plat, dict):
        plat = {}
    # F8 同族防御（2026-11-07）：平台值偶发裸 list/str（flash 形状漂移），.get 会炸
    titles = {k: (v.get("titles", []) if isinstance(v, dict)
                  else (v if isinstance(v, list) else ([v] if isinstance(v, str) else [])))
              for k, v in plat.items()}
    struct = re.compile(r"关键看点|事件概述|独到观点|背景回顾|看点[一二三1-3]|核心争议")
    for name, tlist in titles.items():
        for i, t in enumerate(tlist):
            t = t if isinstance(t, str) else str(t)  # F8 同族：候选项偶发非 str，统一兜底
            if struct.search(t):
                bad.append(f"{name}#{i+1} 含结构词：{t}")
            if re.search(r"[A-Z]{2,}\d", t):
                bad.append(f"{name}#{i+1} 含 slug：{t}")
            limit = {"bilibili": 80, "xigua_douyin": 30, "shipinhao": 16}.get(name, 80)
            if len(t) > limit:
                bad.append(f"{name}#{i+1} 超长 {len(t)}>{limit}：{t}")
    # 只拦三平台重复的非选题中文卖点；队名和事件关键词允许一致。
    def grams(t):
        return {t[i:i + 3] for i in range(len(t) - 2)
                if re.fullmatch(r"[\u4e00-\u9fff]{3}", t[i:i + 3])}
    tlists = [t for tl in titles.values() if tl for t in tl[:1]]
    if len(tlists) >= 3:
        common = set.intersection(*[grams(t) for t in tlists]) - grams(kit.get("topic", ""))
        if common:
            bad.append(f"三平台押同词「{sorted(common)[0]}」")
    return bad


def run_stage(name: str, fn, workdir: Path, use_llm_supervisor: bool, resume: bool,
              topic_info: dict = None, policy_override: str = None):
    """带检查点的阶段执行器：gate 阶段产结构化状态→监督决策；auto 阶段直跑重试。
    L0：执行前 preflight 确定性校验；L1：可自愈错误盲重试；L3：决策结果落 hindsight_log。"""
    policy, why = STAGE_POLICY[name]
    if policy_override is not None:
        policy = policy_override
    topic_info = topic_info or {}
    ckpt_file = workdir / f"checkpoint_{name}.json"
    if resume and ckpt_file.exists():
        prev = json.loads(ckpt_file.read_text(encoding="utf-8"))
        if prev.get("status") == "ok":
            print(f"[{name}] 检查点已 ok，跳过（{why}）")
            return prev.get("result")

    attempts = 0
    actions: list = []
    hint = ""  # 监督层 restrategy 时给出的换策略指示，下一轮传给阶段函数
    pending: list = []  # 本链条的所有决策，终局时统一记 hindsight（HiSentinel 教师信号）
    events_path = workdir / "checkpoint_events.jsonl"
    def log_event(state: dict, decision: dict, elapsed: float) -> None:
        row = {"ts": datetime.now().isoformat(timespec="seconds"),
               "run_id": state.get("run_id"), "stage": name,
               "attempt": state.get("attempts"), "status": state.get("status"),
               "error_type": state.get("error_type", ""),
               "action": decision.get("action"), "mechanism": decision.get("mechanism"),
               "elapsed_seconds": round(elapsed, 3)}
        if state.get("status") != "ok":
            row["evidence"] = state.get("evidence", {})
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    while True:
        attempts += 1
        started = time.monotonic()
        # L0 零成本先筛：执行前确定性校验（StateMirror）——前置缺失就别烧执行/LLM
        try:
            pf = preflight(name, workdir, topic_info)
        except Exception as exc:
            pf = [f"preflight 异常：{exc}"]
        if pf:
            state = make_state(name, "failed", error_type="preflight",
                               attempts=attempts, actions_tried=actions[:],
                               evidence={"violations": pf})
            state["attempts"] = attempts
            state["actions_tried"] = actions[:]
            if policy == "auto":
                raise RuntimeError(f"[{name}] 前置校验不过：{pf}")
            with usage_context(stage=name):
                decision = supervise(state, use_llm=use_llm_supervisor)
            apply_decision(state, decision, workdir)
            log_event(state, decision, time.monotonic() - started)
            record_outcome(state, decision, "escalated")
            raise RuntimeError(f"[{name}] 升级给人：前置校验不过 {pf}")
        try:
            with usage_context(stage=name, purpose="rework" if attempts > 1 else "gen"):
                result = fn(attempts, actions, hint)
            state = result if isinstance(result, dict) and "status" in result \
                else make_state(name, "ok", evidence={"result_type": type(result).__name__})
            if "result" not in state:
                state["result"] = result if isinstance(result, (str, int, float, bool, type(None))) else None
        except Exception as exc:
            state = make_state(name, "failed", error_type=type(exc).__name__,
                               attempts=attempts, actions_tried=actions[:],
                               evidence={"error": str(exc)[:300]})

        state["attempts"] = attempts
        state["actions_tried"] = actions[:]
        state.setdefault("run_id", current_run_id())
        state.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        state.setdefault("stage", name)
        if policy == "auto":
            # 不设检查点：出错率极低且重跑便宜——直接重试，仍败才冒出（不叫监督）
            if state["status"] == "ok":
                ckpt_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                log_event(state, {"action": "continue", "mechanism": "auto"}, time.monotonic() - started)
                return state.get("result")
            if attempts < MAX_ATTEMPTS:
                log_event(state, {"action": "retry", "mechanism": "auto"}, time.monotonic() - started)
                actions.append(f"auto-retry#{attempts}")
                continue
            log_event(state, {"action": "gave_up", "mechanism": "auto"}, time.monotonic() - started)
            raise RuntimeError(f"[{name}] 自动重试 {attempts} 次仍失败：{state.get('evidence')}")

        # gate 阶段：结构化状态 → 监督决策
        with usage_context(stage=name):
            decision = supervise(state, use_llm=use_llm_supervisor)
        # §7.3 硬预算（2026-11-07 审计 F7）：规则回退与监督 LLM 都不得越过 MAX_ATTEMPTS——
        # 未知错误在预算耗尽后规则回退是低置信 retry，gate 路径无上限会无限重试烧 token
        if decision.get("action") in ("retry", "restrategy") and attempts >= MAX_ATTEMPTS:
            decision["action"] = "escalate"
            decision["reason"] = (decision.get("reason", "") +
                                  f"（重试预算 {MAX_ATTEMPTS} 已耗尽，原判 retry/restrategy）").strip()
        apply_decision(state, decision, workdir)
        log_event(state, decision, time.monotonic() - started)
        pending.append((state, decision))
        act = decision["action"]
        if act == "continue":
            ckpt_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            for s, d in pending:
                record_outcome(s, d, "ok")
            return state.get("result")
        if act == "retry":
            actions.append(f"retry#{attempts}")
            continue
        if act == "restrategy":
            hint = decision.get("reason", "")
            actions.append(f"restrategy#{attempts}:{hint[:20]}")
            continue
        # escalate → 升级给人，中断本条（loop 模式下转下一条）
        for s, d in pending:
            record_outcome(s, d, "escalated")
        raise RuntimeError(f"[{name}] 升级给人：{decision.get('reason', '')}")


# ---------- 各阶段函数 ----------

def make_stages(args, topic_info: dict, workdir: Path):
    topic = topic_info["topic_title"]

    def st_script(attempts, actions, hint=""):
        info = dict(topic_info)
        lb = lessons_block("script")
        if lb:
            info["fact_basis"] = (info.get("fact_basis") or "") + "\n" + lb
        if hint:
            info["fact_basis"] = ((info.get("fact_basis") or "")
                                  + f"\n[监督换策略指示] 上一版被否决：{hint}。本次必须针对性规避。")
        script = writer.generate_script(topic, fmt=args.format, minutes=args.minutes,
                                        polish=not args.no_polish, topic_info=info)
        sents = writer.flatten_sentences(script)
        wc = writer.word_count(script)
        bad = []
        if not sents:
            bad.append("无句子")
        lo, hi = int(args.minutes * 230), int(args.minutes * 280)
        if not (lo * 0.7 <= wc <= hi * 1.3):
            bad.append(f"字数 {wc} 超出 [{lo},{hi}]±30%")
        n_pending = len(script.get("fact_check_notes") or [])
        if n_pending > max(3, len(sents) // 6):
            bad.append(f"[待核实] {n_pending} 条过多——事实底座不扎实")
        if bad:
            lesson_add("script", f"生成文稿（{topic[:20]}）", "flash+pro 生成",
                       "闸门不过：" + "；".join(bad), "重跑时在事实边界里补来源或压缩断言")
            return make_state("script", "failed", error_type="script_gate",
                              attempts=attempts, actions_tried=actions,
                              evidence={"bad": bad, "words": wc, "sentences": len(sents),
                                        "notes": (script.get("fact_check_notes") or [])[:10]})
        (workdir / "文稿.json").write_text(
            json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[script] {wc} 字 / {len(sents)} 句 / [待核实] {n_pending} 条")
        return make_state("script", "ok",
                          evidence={"words": wc, "sentences": len(sents), "pending": n_pending},
                          )

    def st_footage(attempts, actions, hint=""):
        # 程序层自动取材：按 选题.json 的 source_bvs 补下缺失源视频（确定性，零 token）
        bvs = topic_info.get("source_bvs") or []
        if bvs:
            from pipeline.matcher import _find_local
            missing = [bv for bv in bvs if not _find_local(bv)]
            if missing:
                urls = [f"https://www.bilibili.com/video/{bv}" for bv in missing]
                print(f"[footage] 程序补下源视频 {len(missing)} 条：{missing}")
                cp = subprocess.run([sys.executable, "-X", "utf8",
                                     str(ROOT / ".claude/skills/dl-video/dl_video.py"),
                                     *urls, "--out", str(ROOT / "downloads")],
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
                print(cp.stdout[-600:])
                if cp.returncode != 0:
                    raise RuntimeError(f"源视频下载失败：{cp.stderr[-200:]}")
            else:
                print(f"[footage] {len(bvs)} 条源视频已在 downloads/，跳过下载")
        # 采集计划缓存：LLM 每轮重生成会漂移事实锚点、拆掉人工落点——缺或比文稿旧才重建
        plan_p = workdir / "采集计划.json"
        script_p = workdir / "文稿.json"
        if (not plan_p.exists()
                or script_p.stat().st_mtime > plan_p.stat().st_mtime):
            plan_md = newswire.build_collection_plan(script_json(workdir), workdir, topic=topic)
        else:
            print("  [footage] 采集计划.json 已缓存（文稿未变），跳过重建")
        try:
            plan = json.loads((workdir / "采集计划.json").read_text(encoding="utf-8"))
            sources.discover_for_plan(plan, workdir)
        except Exception as exc:
            print(f"  [footage] 选段辅助降级：{exc}")
        r = matcher.auto_mapping(workdir)
        # 切片（确定性 ffmpeg，产物=采集/NNN_*.mp4 + 来源.json）
        if r["clips"]:
            cp = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "cut_clips.py"), str(workdir)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            print(cp.stdout[-800:])
            if cp.returncode != 0:
                raise RuntimeError(f"cut_clips 失败：{cp.stderr[-300:]}")
        total = r["clips"] + len(r["low_conf"])
        evidence = {"total": total, "clips": r["clips"], "low_conf": r["low_conf"]}
        if not r["clips"]:
            lesson_add("footage", f"素材落点（{topic[:20]}）", "ASR+字幕自动定窗",
                       f"0 条进映射 / {len(r['low_conf'])} 低置信",
                       "检查源视频是否已下载到 downloads/，或补 quotes.json 字幕窗")
            return make_state("footage", "failed", error_type="clip_low_conf",
                              attempts=attempts, actions_tried=actions, evidence=evidence)
        return make_state("footage", "ok", evidence=evidence)

    def st_produce(attempts, actions, hint=""):
        sents = writer.flatten_sentences(script_json(workdir))
        narration = tts.synthesize_narration(sents, workdir / "audio",
                                             engine=tts.get_engine(args.engine))
        segs = narration["segments"]
        srt = subtitles.write_srt(segs, workdir / "字幕.srt")
        manifest = assemble_run(segs, workdir, topic_info, srt)
        total = segs[-1]["end"] or 1.0
        quoted = sum(max(0.0, segs[m["index"]]["end"] - segs[m["index"]]["start"])
                     for m in manifest if str(m.get("source", "")).startswith("collect:"))
        print(f"[produce] 成片完成，引用 {quoted:.1f}s / {total:.1f}s = {quoted/total:.0%}")
        return None

    def st_kit(attempts, actions, hint=""):
        # L4 经验回流执行者（CatalogAgent/ReTTT）：历史教训直接注入生成提示词，下次不靠监督
        kb = lessons_block("kit")
        publish.build_publish_kit(script_json(workdir), workdir, topic=topic,
                                  hint=(kb + "\n" + hint).strip())
        kit = json.loads((workdir / "投稿包.json").read_text(encoding="utf-8"))
        # 人工定稿标题（坑⑦：生成器拦不干净必人肉过）：标题.manual.json =
        # {"bilibili": [...], "xigua_douyin": [...], "shipinhao": [...]}，重跑不丢；
        # 应用后按 v1.6 约定重命名成片=终稿 B站主标题
        mf = workdir / "标题.manual.json"
        if mf.exists():
            try:
                mt = json.loads(mf.read_text(encoding="utf-8"))
                for plat, tl in mt.items():
                    if tl and kit.get("platforms", {}).get(plat):
                        kit["platforms"][plat]["titles"] = list(tl)
                b1 = (kit["platforms"]["bilibili"].get("titles") or [""])[0]
                if b1:
                    video_file = publish.safe_filename(b1) + ".mp4"
                    old = workdir / (kit.get("video_file") or "")
                    if not old.exists():
                        mp4s = sorted(workdir.glob("*.mp4"))
                        old = mp4s[0] if mp4s else None
                    if old and old.exists() and old.name != video_file:
                        target = workdir / video_file
                        if target.exists():  # 旧产物占名（坑㉛同族）——以本批为准，先清后改名
                            target.unlink()
                        old.rename(target)
                        print(f"[kit] 成片按人工标题重命名：{video_file}")
                    kit["video_file"] = video_file
                (workdir / "投稿包.json").write_text(
                    json.dumps(kit, ensure_ascii=False, indent=2), encoding="utf-8")
                print("[kit] 已应用 标题.manual.json（人工定稿标题）")
            except Exception as exc:
                print(f"[kit] 标题.manual.json 应用失败：{exc}")
        bad = title_lint(kit)
        if bad:
            lesson_add("kit", f"投稿包标题（{topic[:20]}）", "flash 出标题+lint",
                       "lint 违规：" + "；".join(bad)[:120],
                       "提示词补禁令后重生成；结构性小节名/slug 永不进标题")
            return make_state("kit", "failed", error_type="title_lint",
                              attempts=attempts, actions_tried=actions, evidence={"bad": bad})
        return make_state("kit", "ok", evidence={"titles_ok": True})

    def st_publish(attempts, actions, hint=""):
        if not args.publish:
            print("[publish] dry-run（默认不碰线上）。发布：加 --publish（--private 上传，AIGC 勾选人工两步）")
            return make_state("publish", "ok", evidence={"mode": "dry-run"})
        if NBA_HINT.search(topic):
            # B站禁区（用户 2026-11-05）：NBA 比赛画面题材不发 B站
            todo = workdir / "多平台待发清单.md"
            todo.write_text(f"# NBA 题材——不发 B站，发西瓜/抖音/视频号\n\n"
                            f"成片与投稿包已就绪（{workdir}），按技术包 §10 三平台手动贴。\n",
                            encoding="utf-8")
            lesson_add("publish", f"NBA 题材（{topic[:20]}）", "发布闸门",
                       "B站禁区拦截，转多平台清单", "NBA 片永不传 B站，直接走西瓜/抖音/视频号")
            return make_state("publish", "ok", evidence={"mode": "nba_multi_platform"})
        cp = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "bili_publish.py"),
                             str(workdir), "--publish", "--private",
                             *(["--extra-fields", json.dumps(topic_info["bili_extra_fields"], ensure_ascii=False)]
                               if topic_info.get("bili_extra_fields") else [])],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(cp.stdout[-800:])
        if cp.returncode != 0:
            return make_state("publish", "failed", error_type="publish_blocked",
                              attempts=attempts, actions_tried=actions,
                              evidence={"stderr": cp.stderr[-200:]})
        print("[publish] 已传仅自己可见——创作中心勾「AI 生成内容」后转公开（坑㉖，人工两步）")
        return make_state("publish", "ok", evidence={"mode": "private_uploaded"})

    return {"topic": lambda a, b, h="": make_state("topic", "ok", evidence={"topic": topic}),
            "script": st_script, "footage": st_footage, "produce": st_produce,
            "kit": st_kit, "publish": st_publish}


def script_json(workdir: Path) -> dict:
    return json.loads((workdir / "文稿.json").read_text(encoding="utf-8"))


def assemble_run(segs, workdir, topic_info, srt):
    """素材（人工采集优先 → Pexels/本地 → 字卡）+ 合成。"""
    manifest = assemble_build_manifest(segs, workdir, topic_info)
    title = (script_json(workdir).get("titles") or [topic_info["topic_title"]])[0]
    assemble.assemble_video(segs, manifest, workdir / "audio/narration.wav", workdir,
                            srt=srt, bgm=None, title=title)
    return manifest


def assemble_build_manifest(segs, workdir, topic_info):
    from pipeline import footage
    manifest = footage.build_manifest(segs, workdir, keyword_pool=topic_info.get("keywords"))
    real = sum(1 for m in manifest if m.get("source", "").startswith(("pexels", "local")))
    cards = sum(1 for m in manifest if m.get("source") == "self-card")
    print(f"[produce] 素材：实拍 {real} / 字卡 {cards} / 其他 {len(manifest)-real-cards}")
    return manifest


# ============================================================
# 入口
# ============================================================

def run_once(args) -> int:
    global _sup_budget_left
    _sup_budget_left = SUPERVISOR_LLM_BUDGET
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if args.arm:
        args.arch = "full-agent" if args.arm == "A1" else "program"
        args.supervise_llm = args.arm == "A4"
    if args.experiment:
        if not args.arm or not args.workdir or not args.topic or args.resume or args.publish:
            print("[experiment] 必须指定 --arm、--workdir 和 --topic，且不得 --resume 或 --publish")
            return 2
        if args.arm == "A4" and not args.experience_store:
            print("[experiment] A4 必须指定隔离的 --experience-store")
            return 2
        exp_dir = Path(args.workdir)
        if not (exp_dir / "选题.json").exists():
            print("[experiment] 缺少冻结输入 选题.json")
            return 2
        if any(exp_dir.glob("checkpoint_*.json")) or (exp_dir / "文稿.json").exists():
            print("[experiment] 工作目录已有运行产物；每个 arm/run 必须使用独立目录")
            return 2
    workdir_arg = Path(args.workdir) if args.workdir else None
    topic_info = None
    # 坑㉘：--resume 必须复用检查点里的题，不重抽选题库——
    # topic_title 与种子串味会撕裂文稿（实测抽错题致 [待核实] 暴涨、闸门连拦）
    if args.resume and workdir_arg and not args.topic:
        ck_file = workdir_arg / "checkpoint_topic.json"
        if ck_file.exists():
            try:
                ck = json.loads(ck_file.read_text(encoding="utf-8"))
                t = (ck.get("evidence") or {}).get("topic")
                if ck.get("status") == "ok" and t:
                    topic_info = {"topic_title": t}
                    print(f"[autoline] resume 复用检查点选题：{t}")
            except Exception:
                pass
    if topic_info is None:
        try:
            topic_info = stage_topic(args)
        except Exception as exc:
            print(f"[autoline] 选题失败：{exc}")
            return 2
    topic = topic_info["topic_title"]
    workdir = workdir_arg or OUTPUT_DIR / slugify(topic)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / f"run_{run_id}.json").write_text(json.dumps({
        "run_id": run_id, "started_at": datetime.now().isoformat(timespec="seconds"),
        "topic": topic, "supervise_llm": args.supervise_llm,
        "resume": args.resume, "experiment": args.experiment,
        "arch": args.arch, "arm": args.arm, "stage_policy": STAGE_POLICY,
        "experience_store": args.experience_store or None,
        "models": {"flash": MODEL_FLASH, "pro": MODEL_PRO},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    def finalize_run(exit_code: int) -> None:
        p = workdir / f"run_{run_id}.json"
        meta = json.loads(p.read_text(encoding="utf-8"))
        meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        meta["exit_code"] = exit_code
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    seed = workdir / "选题.json"  # 种子：{angle, hook_line, fact_basis, keywords, source_bvs}
    if args.experiment:
        frozen = json.loads(seed.read_text(encoding="utf-8"))
        if frozen.get("topic_title") != args.topic or not frozen.get("fact_basis"):
            print("[experiment] 选题.json 必须含与 --topic 一致的 topic_title 和非空 fact_basis")
            return 2
    if seed.exists():
        meta_path = workdir / f"run_{run_id}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["seed_sha256"] = hashlib.sha256(seed.read_bytes()).hexdigest()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if seed.exists():
        try:
            topic_info.update(json.loads(seed.read_text(encoding="utf-8")))
            print("[autoline] 已注入 选题.json（angle/fact_basis/keywords）")
        except Exception as exc:
            print(f"[autoline] 选题.json 读取失败，忽略：{exc}")
    else:
        try:
            with usage_context(run_id=run_id, stage="topic"):
                compile_seed(workdir, topic)
            topic_info.update(json.loads(seed.read_text(encoding="utf-8")))
            print("[autoline] 选题.json 缺失——已程序生成（搜索摘要+mimo 提取）")
        except Exception as exc:
            print(f"[autoline] 事实底座生成失败（可手写 选题.json 后 --resume）：{exc}")
    if args.experiment:
        source_bvs = topic_info.get("source_bvs") or []
        missing_bvs = [bv for bv in source_bvs if not matcher._find_local(bv)]
        if not source_bvs or missing_bvs:
            print(f"[experiment] 冻结素材池不完整，缺本地源：{missing_bvs or 'source_bvs 为空'}")
            return 2
    print(f"[autoline] 选题：{topic}\n[autoline] 工作目录：{workdir}")

    stages = make_stages(args, topic_info, workdir)
    if args.arch == "full-agent":
        from full_agent import make_stages as make_full_agent_stages
        stages = make_full_agent_stages(args, topic_info, workdir, stages)
    order = ["topic", "script", "footage", "produce", "kit", "publish"]
    try:
        with usage_context(run_id=run_id), experiment_io(
                workdir=workdir if args.experiment else None,
                stats_path=Path(args.experience_store).with_name("A4_decision_stats.json")
                if args.experiment and args.experience_store else None,
                stats_enabled=(args.arm == "A4") if args.experiment else True), experience_context(
                enabled=not args.arm or args.arm == "A4",
                path=Path(args.experience_store) if args.experience_store else None):
            for name in order:
                print(f"── 阶段 {name}（{STAGE_POLICY[name][0]}）")
                override = "auto" if args.arm in ("A1", "A2") or (
                    not args.arm and args.arch == "full-agent" and name != "publish") else None
                run_stage(name, stages[name], workdir, args.supervise_llm, args.resume,
                          topic_info, policy_override=override)
    except RuntimeError as exc:
        print(f"[autoline] 停止：{exc}")
        print(f"[autoline] 检查点与经验已落盘；处理完可 --resume 续跑：python -X utf8 autoline.py once --workdir \"{workdir}\" --resume")
        finalize_run(2)
        return 2

    # 落一份 token 小结（程序层 vs 监督层）
    agg = usage_summary([e for e in USAGE if e.get("run_id") == run_id])
    print("[token]", json.dumps(agg, ensure_ascii=False))
    print(f"[autoline] 完成：{workdir}")
    finalize_run(0)
    return 0


def cmd_loop(args) -> int:
    n = 0
    while args.max_runs <= 0 or n < args.max_runs:
        n += 1
        print(f"\n===== 循环第 {n} 条（{datetime.now():%H:%M}）=====")
        rc = run_once(args)
        if rc != 0:
            print(f"[loop] 本条停止（rc={rc}），转下一条")
        if args.max_runs > 0 and n >= args.max_runs:
            break
        print(f"[loop] 休眠 {args.interval}s…")
        time.sleep(args.interval)
    return 0


def cmd_report(_args) -> int:
    print("── token 台账（程序层 vs 监督层，supervisor=coordination 成本）")
    agg = {}
    ledger = OUTPUT_DIR / "token_ledger.jsonl"
    if ledger.exists():
        for ln in ledger.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(ln)
            except Exception:
                continue
            a = agg.setdefault(e["layer"], {"calls": 0, "chars_in": 0, "chars_out": 0,
                                            "tokens_in": 0, "tokens_out": 0})
            for k in ("calls", "chars_in", "chars_out", "tokens_in", "tokens_out"):
                a[k] += e.get(k) or (1 if k == "calls" else 0)
    print(json.dumps(agg or {"note": "尚无台账"}, ensure_ascii=False, indent=2))
    sup = agg.get("supervisor", {}).get("calls", 0)
    pro = agg.get("program", {}).get("calls", 0)
    if pro:
        print(f"监督/程序调用比 = {sup}/{pro}；频率×体积与省下 token 的平衡点按出错率×失败成本调（STAGE_POLICY）")

    print("\n── 经验库（lessons.jsonl）")
    ls = lessons_all()
    for l in ls[-15:]:
        print(f"[{l['ts']}] {l['stage']}：{l['scene']}｜{l['result']}→ 下次 {l['next_do']}")
    print(f"共 {len(ls)} 条")

    print("\n── 检查点状态（autoline_state.json）")
    if STATE_PATH.exists():
        st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        runs = st.get("runs", [])
        by_stage: dict = {}
        for r in runs:
            s = by_stage.setdefault(r["stage"], {"ok": 0, "failed": 0, "suspicious": 0})
            s[r["status"]] = s.get(r["status"], 0) + 1
        print(json.dumps(by_stage, ensure_ascii=False, indent=2))
        for r in runs[-6:]:
            print(f"[{r['ts']}] {r['stage']} {r['status']} {r.get('error_type','')} "
                  f"→ {r.get('decision', {}).get('action', '-')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="无人值守产线循环（三层架构）")
    sub = ap.add_subparsers(dest="cmd")
    p_once = sub.add_parser("once", help="出一条")
    p_loop = sub.add_parser("loop", help="循环出片")
    for p in (p_once, p_loop):
        p.add_argument("--topic", default="", help="指定选题（跳过选题库）")
        p.add_argument("--arch", default="program", choices=["program", "full-agent"],
                       help="实验架构：程序门控或 LLM 直接决策基线")
        p.add_argument("--experiment", action="store_true",
                       help="实验模式：强制冻结选题、独立工作目录、禁投稿")
        p.add_argument("--arm", choices=["A1", "A2", "A3", "A4"],
                       help="E0 实验组；自动设置架构、监督与经验层")
        p.add_argument("--experience-store", default="",
                       help="A4 实验组的独立经验库 JSONL 路径")
        p.add_argument("--ideate", type=int, default=0, help="先生成 N 个选题入库")
        p.add_argument("--mode", default="hot", choices=["hot", "evergreen"])
        p.add_argument("--format", default="热点评论", choices=list(writer.FORMATS))
        p.add_argument("--minutes", type=float, default=3.5)
        p.add_argument("--no-polish", action="store_true")
        p.add_argument("--engine", default="mimo", choices=["mimo", "edge", "sapi"])
        p.add_argument("--workdir", default="")
        p.add_argument("--resume", action="store_true", help="按检查点续跑")
        p.add_argument("--publish", action="store_true",
                       help="真投稿（--private 上传；AIGC 勾选人工两步）。默认 dry-run")
        p.add_argument("--supervise-llm", action="store_true",
                       help="监督决策用 flash（只喂摘要+经验，单独计量）；默认规则决策零 token")
    p_loop.add_argument("--max-runs", type=int, default=0, help="循环条数（0=无限）")
    p_loop.add_argument("--interval", type=int, default=21600, help="条间隔秒（默认 6h）")
    sub.add_parser("report", help="token/经验/翻车报告")

    args = ap.parse_args()
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd in ("once", "loop"):
        return cmd_loop(args) if args.cmd == "loop" else run_once(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
