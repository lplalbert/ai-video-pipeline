# v1.1 R1-R6 规则回归冒烟（2026-11-08 落盘时实测全过；改 autoline/writer 规则后必跑）
import json
import tempfile
from pathlib import Path

import autoline
from pipeline import writer

ok = []

# ── R3：分体裁窗 ──
assert writer._word_range(3.5, "热点评论") == (875, 1312), writer._word_range(3.5, "热点评论")
assert writer._word_range(3.5, "科普解说") == (875, 1347), writer._word_range(3.5, "科普解说")
assert writer._word_range(3.5, "盘点") == (805, 1312), writer._word_range(3.5, "盘点")
assert writer._word_range(3.5, None) == (805, 1312), writer._word_range(3.5, None)  # 缺省回落
assert writer._word_range(3.5) == (805, 1312)  # 旧签名不带 fmt 也兼容
ok.append("R3 word_range")

# ── R2：3 字碎片不再触发，4 字连续串触发 ──
def mk(firsts):
    return {"topic": "中国八大超级工程盘点：从白鹤滩到都江堰",
            "platforms": {name: {"titles": [t]} for name, t in zip(
                ("bilibili", "xigua_douyin", "shipinhao"), firsts)}}

# pilot 误杀案例：三平台只共享 ≤3 字碎片（三峡前/圳的悖/则才是 类）→ 应放行
k = mk(["白鹤滩到底有多强：三峡前的奇迹", "都江堰为何两千年不倒：圳的悖论",
        "八大工程最年轻的一个：则才是巅峰"])
bad = autoline.title_lint(k)
assert not any("押同词" in b for b in bad), f"3字碎片仍触发: {bad}"
ok.append("R2 3-char fragment exempt")

# 4 字连续串共享 → 应触发
k = mk(["超级工程全线碾压榜单", "中国工程全线碾压世界", "盘点全线碾压时刻"])
bad = autoline.title_lint(k)
assert any("押同词" in b for b in bad), f"4字同词未触发: {bad}"
ok.append("R2 4-char gram fires")

# 主题/标签锚词（工程名等，R2 豁免）→ 放行；同结构无锚词卖点仍触发见上例
k = mk(["白鹤滩水电站有多猛", "白鹤滩水电站如何冠绝全球", "白鹤滩水电站的含金量"])
k["tags"] = ["白鹤滩", "水电站", "超级工程"]
bad = autoline.title_lint(k)
assert not any("押同词" in b for b in bad), f"主题锚词仍触发: {bad}"
ok.append("R2 topic/tag-anchor exempt")

# 有锚词但卖点仍共享 → 剥词后残余同词照拦（豁免不得放水）
k = mk(["白鹤滩全线碾压时刻", "白鹤滩全线碾压世界", "白鹤滩全线碾压天花板"])
k["tags"] = ["白鹤滩", "水电站"]
bad = autoline.title_lint(k)
assert any("押同词" in b for b in bad), f"剥词后卖点同词未触发: {bad}"
ok.append("R2 hook-gram fires after strip")

# ── R1：候选轮换零 token 过闸 ──
k = {"topic": "超级工程盘点",
     "platforms": {
         "bilibili": {"titles": ["全线碾压的超级工程", "白鹤滩到底多强"]},
         "xigua_douyin": {"titles": ["全线碾压的中国工程", "都江堰两千年之谜"]},
         "shipinhao": {"titles": ["全线碾压时刻", "八大工程盘点"]}}}
assert autoline.title_lint(k), "前置：应有押同词违规"
before = {p: list(v["titles"]) for p, v in k["platforms"].items()}
assert autoline.title_lint_fix(k) is True, "轮换应找到无违规组合"
assert not autoline.title_lint(k), "轮换后应干净"
for p, v in k["platforms"].items():  # 只重排不增删候选
    assert sorted(v["titles"]) == sorted(before[p]), (p, v["titles"], before[p])
assert any(v["titles"][0] != before[p][0] for p, v in k["platforms"].items()), "应至少换一位首标题"
assert not autoline.title_lint_fix({"topic": "t", "platforms": {}}), "空 kit 应 False"
ok.append("R1 title_lint_fix rotation")

# ── R4：LlmShapeError 类型 ──
assert issubclass(writer.LlmShapeError, ValueError)
assert "LlmShapeError" in autoline.SELF_HEALING and "LlmShapeError" in autoline.KNOWN_ERRORS
ok.append("R4 LlmShapeError typed")

# ── R1 兜底 + R6 infra：run_stage 合成测试 ──
import autoline as A
with tempfile.TemporaryDirectory() as td:
    workdir = Path(td)
    import pipeline.llm as llm
    orig_out = llm.OUTPUT_DIR
    llm.OUTPUT_DIR = workdir
    try:
        # preflight 过关前置件：kit 需要 文稿.json + mp4，produce 需要 文稿.json
        (workdir / "文稿.json").write_text("{}", encoding="utf-8")
        (workdir / "成片.mp4").write_bytes(b"\x00")
        # R1：title_lint 永败 → 应走 1 次 fallback#1（attempts=3 后升级给人照记失败）
        calls = []

        def lint_fail(attempt, actions, hint):
            calls.append((attempt, tuple(actions), hint))
            return A.make_state("kit", "failed", error_type="title_lint",
                                evidence={"bad": ["三平台押同词「测试词」"]})

        raised = None
        try:
            A.run_stage("kit", lint_fail, workdir, use_llm_supervisor=False, resume=False)
        except RuntimeError as exc:
            raised = str(exc)
        assert raised and "升级给人" in raised, raised
        assert len(calls) == 3, f"应 3 次尝试（2 轮 restrategy + 1 兜底），实际 {len(calls)}"
        assert any("fallback#1" in a for _, a, _ in calls[1:]), calls
        assert any("自动兜底改写" in h for _, _, h in calls[1:]), calls
        # 事件流应有 fallback 记录
        events = [json.loads(x) for x in
                  (workdir / "checkpoint_events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert any(e.get("action") == "fallback" for e in events), events
        ok.append("R1 fallback one-shot then fail")

        # R1 --attended：直接升级不兜底
        A.ATTENDED = True
        calls.clear()
        raised = None
        try:
            A.run_stage("kit", lint_fail, workdir, use_llm_supervisor=False, resume=False)
        except RuntimeError as exc:
            raised = str(exc)
        A.ATTENDED = False
        assert raised and "升级给人" in raised
        assert len(calls) == 2, f"attended 应 2 次尝试即升级，实际 {len(calls)}"
        ok.append("R1 attended = v1.0 behavior")

        # R6：ConnectionResetError → error_class=infra 进事件流
        def net_fail(attempt, actions, hint):
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")

        raised = None
        try:
            A.run_stage("produce", net_fail, workdir, use_llm_supervisor=False, resume=False)
        except RuntimeError as exc:
            raised = str(exc)
        events = [json.loads(x) for x in
                  (workdir / "checkpoint_events.jsonl").read_text(encoding="utf-8").splitlines()]
        infra = [e for e in events if e.get("error_class") == "infra"]
        assert infra, events
        assert infra[-1]["error_type"] == "ConnectionResetError", infra
        ok.append("R6 infra error_class in events")

        # R3：st_script 闸不再用 ±30%（经 make_stages 间接过重，这里直接验窗函数口径）
        lo, hi = writer._word_range(3.5, "科普解说")
        assert lo == 875 and hi == 1347 and not (lo * 0.7 <= 1400 <= hi * 1.3 and lo <= 1400 <= hi)
        ok.append("R3 exact band semantics")
    finally:
        llm.OUTPUT_DIR = orig_out

print("SMOKE PASS:", *ok, sep="\n  - ")
