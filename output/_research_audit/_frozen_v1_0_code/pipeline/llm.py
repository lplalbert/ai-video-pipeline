"""mimo 大模型调用层（Anthropic 兼容接口，UTF-8 请求体）。"""
import contextlib
import contextvars
import json
import re
import time

import requests

from config import API_BASE, API_TOKEN, MODEL_FLASH, MODEL_PRO, OUTPUT_DIR

_HEADERS = {
    "content-type": "application/json",
    "authorization": f"Bearer {API_TOKEN}",
    "x-api-key": API_TOKEN,
    "anthropic-version": "2023-06-01",
}

# ---- Token 台账（autoline 三层架构：监督调用与程序调用分开计量）----
# 用户 2026-11-05：监督 Agent 的 token 开销需单独计量——coordination 本身有成本，
# 监督粒度按「出错率×失败成本 vs 协调开销」平衡。
USAGE: list = []  # 每次成功调用追加一条台账
_LAYER = ["program"]
_RUN_ID = contextvars.ContextVar("run_id", default=None)
_STAGE = contextvars.ContextVar("stage", default=None)
_PURPOSE = contextvars.ContextVar("purpose", default="gen")


def current_run_id():
    return _RUN_ID.get()


@contextlib.contextmanager
def usage_context(*, run_id=None, stage=None, purpose=None):
    """Attach experiment attribution to all LLM calls made in this scope."""
    changes = []
    for var, value in ((_RUN_ID, run_id), (_STAGE, stage), (_PURPOSE, purpose)):
        if value is not None:
            changes.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(changes):
            var.reset(token)


@contextlib.contextmanager
def set_layer(name: str):
    """标记后续 LLM 调用归属层：program（程序逻辑）| supervisor（监督门控）。"""
    prev = _LAYER[0]
    _LAYER[0] = name
    try:
        yield
    finally:
        _LAYER[0] = prev


def _usage_record(model: str, prompt: str, system: str, text: str, usage: dict,
                  *, temperature: float = None, response_model: str = None) -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "layer": _LAYER[0],
        "run_id": _RUN_ID.get(),
        "stage": _STAGE.get(),
        "purpose": "supervise" if _LAYER[0] == "supervisor" else _PURPOSE.get(),
        "model": model,
        "response_model": response_model,
        "temperature": temperature,
        "chars_in": len(prompt) + len(system or ""),
        "chars_out": len(text),
        "tokens_in": usage.get("input_tokens"),
        "tokens_out": usage.get("output_tokens"),
    }
    USAGE.append(entry)
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_DIR / "token_ledger.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def usage_summary(usage_list: list = None) -> dict:
    """按层汇总 token/字符开销（report 用）。"""
    agg: dict = {}
    for e in (usage_list if usage_list is not None else USAGE):
        a = agg.setdefault(e["layer"], {"calls": 0, "chars_in": 0, "chars_out": 0,
                                       "tokens_in": 0, "tokens_out": 0})
        a["calls"] += 1
        a["chars_in"] += e["chars_in"]
        a["chars_out"] += e["chars_out"]
        a["tokens_in"] += e.get("tokens_in") or 0
        a["tokens_out"] += e.get("tokens_out") or 0
    return agg


def chat(model: str, prompt: str, system: str = "", max_tokens: int = 4096,
         temperature: float = 0.7) -> str:
    """调用 messages 接口，返回文本（自动跳过 thinking 块）。

    该网关默认先产出 thinking 块，可能吃光 max_tokens 导致 text 为空，
    因此显式传 thinking=disabled 直出正文（已实测有效）；
    若网关不认该参数则自动去掉重试。空文本+stop=max_tokens 时加倍 max_tokens 兜底。
    """
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    last_err = ""
    for attempt in range(4):
        try:
            resp = requests.post(
                f"{API_BASE}/v1/messages",
                headers=_HEADERS,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                timeout=300,
            )
            if resp.status_code in (429, 500, 502, 503):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 400 and "thinking" in body:
                body.pop("thinking", None)  # 网关不认 thinking 参数 → 去掉重试
                last_err = "网关拒绝 thinking 参数，已移除后重试"
                continue
            resp.raise_for_status()
            data = resp.json()
            parts = [b.get("text") or "" for b in data.get("content", []) if b.get("type") == "text"]
            text = "".join(parts).strip()
            if text:
                _usage_record(model, prompt, system, text, data.get("usage") or {},
                              temperature=temperature, response_model=data.get("model"))
                return text
            if data.get("usage"):
                _usage_record(model, prompt, system, text, data["usage"],
                              temperature=temperature, response_model=data.get("model"))
            stop = data.get("stop_reason", "")
            if stop == "max_tokens" and body["max_tokens"] < 16000:
                body["max_tokens"] = min(body["max_tokens"] * 2, 16000)
                last_err = f"输出被截断，max_tokens 加倍至 {body['max_tokens']} 重试"
                continue
            last_err = f"模型未返回文本（stop_reason={stop}）"
        except requests.RequestException as exc:  # 网络/HTTP 错误重试
            last_err = str(exc)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"LLM 调用失败（{model}）: {last_err}")


def flash(prompt: str, **kw) -> str:
    """走量任务用：选题、初稿、关键词、标题。"""
    return chat(MODEL_FLASH, prompt, **kw)


def pro(prompt: str, **kw) -> str:
    """精修任务用：文稿润色、复杂策划。"""
    return chat(MODEL_PRO, prompt, **kw)


def flash_json(prompt: str, tries: int = 3, **kw):
    """flash + JSON 解析；模型偶发输出坏 JSON 时自动重新生成重试，失败存原始输出备查。"""
    last, raw = None, ""
    for _ in range(max(1, tries)):
        raw = flash(prompt, **kw)
        try:
            return extract_json(raw)
        except ValueError as exc:  # JSONDecodeError 也继承 ValueError
            last = exc
            time.sleep(1)
    dbg = OUTPUT_DIR / f"bad_json_{time.strftime('%H%M%S')}.txt"
    dbg.write_text(raw, encoding="utf-8")
    raise RuntimeError(f"JSON 解析失败（flash 已重试 {tries} 次）: {last}；原始输出已存 {dbg}")


def _fix_trailing_commas(s: str) -> str:
    """去掉 } ] 前的多余逗号。"""
    return re.sub(r",\s*([}\]])", r"\1", s)


def _fix_unescaped_quotes(s: str) -> str:
    """字符串值内未转义的直引号 " → \\"（闭合判据：其后非空白字符是 , : } ] " 或结尾）。"""
    out, in_str, esc, n = [], False, False, len(s)
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                k = i + 1
                while k < n and s[k] in " \t\r\n":
                    k += 1
                if k < n and s[k] not in ',:}]"':  # 后面不像 JSON 结构 → 是值里的引号
                    out.append('\\"')
                    continue
                in_str = False
        elif ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def _fix_missing_commas(s: str) -> str:
    """相邻两个 JSON 值之间漏掉的逗号补上（只在结构层，不动字符串内部）。

    判据：值结尾（" } ] 或字母数字）后紧跟值开头（" { [ 或字母数字 -）；
    字母数字续写（如 123、1.5e-3）靠"中间无空白"排除， `,:[{` 后不算值结尾。
    """
    out, in_str, esc, prev, ws = [], False, False, "", False
    for i, ch in enumerate(s):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                prev, ws = '"', False
            continue
        if ch in " \t\r\n":
            ws = True
            out.append(ch)
            continue
        prev_end = bool(prev) and (prev in '"}]' or prev.isalnum())
        start_struct = ch in '"{['
        start_word = ch.isalnum() or ch == "-"
        if prev_end and ((start_struct) or (start_word and ws)):
            out.append(", ")
        out.append(ch)
        if ch == '"':
            in_str = True
        prev, ws = ch, False
    return "".join(out)


def _loads_repair(s: str):
    """json.loads + 常见畸形修复阶梯（尾逗号 / 未转义引号 / 漏逗号）。"""
    cands = [s, _fix_trailing_commas(s), _fix_unescaped_quotes(s),
             _fix_missing_commas(s),
             _fix_missing_commas(_fix_unescaped_quotes(s)),
             _fix_missing_commas(_fix_trailing_commas(_fix_unescaped_quotes(s)))]
    last = None
    for c in cands:
        try:
            return json.loads(c)
        except ValueError as exc:
            last = exc
    raise ValueError(f"JSON 修复后仍无法解析: {last}")


def extract_json(text: str):
    """从模型输出提取 JSON（容忍 ```json 围栏与前后杂文，畸形自动修复）。"""
    t = text.strip()
    if "```" in t:
        for chunk in t.split("```"):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("{") or c.startswith("["):
                t = c
                break
    # 逐个候选起点尝试（取最长可解析片段），防"前面杂文里也有括号"截错
    best, last = None, None
    for start, ch in enumerate(t):
        if ch not in "{[":
            continue
        closer = "}" if ch == "{" else "]"
        end = t.rfind(closer)
        if end <= start:
            continue
        try:
            val = _loads_repair(t[start:end + 1])
        except ValueError as exc:
            last = exc
            continue
        if best is None or end - start > best[0]:
            best = (end - start, val)
    if best is not None:
        return best[1]
    raise ValueError(f"输出中未找到可用 JSON: {text[:200]}") from last
