"""Offline contract check for the agent footage stage; no model or media calls."""
import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import full_agent


def main():
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        (workdir / "文稿.json").write_text(json.dumps({
            "hook": "BLG赢了。", "sections": [], "cta": "你怎么看？",
            "titles": ["标题"], "tags": [], "fact_check_notes": []}, ensure_ascii=False), encoding="utf-8")
        options = argparse.Namespace(minutes=3.5, format="热点评论")
        topic_info = {"topic_title": "BLG", "fact_basis": "BLG赢了", "source_bvs": ["BVfake"]}
        with patch.object(full_agent.matcher, "_find_local", return_value=Path("fake.mp4")), \
             patch.object(full_agent.matcher, "_probe_dur", return_value=90.0), \
             patch.object(full_agent.matcher, "transcribe_cached", return_value=[
                 {"from": 0.0, "to": 4.0, "content": "BLG赢了"}]), \
             patch.object(full_agent, "pro", side_effect=[
                 json.dumps({"clips": [{"index": 0, "src": "BVfake", "start": 0.0,
                                         "desc": "胜利画面"}]}),
                 json.dumps({"clips": [{"index": 0, "src": "BVfake", "start": 2.0}]})]), \
             patch.object(full_agent.subprocess, "run", return_value=argparse.Namespace(returncode=0)):
            stages = full_agent.make_stages(options, topic_info, workdir, {})
            state = stages["footage"](1, [], "")
        mapping = json.loads((workdir / "采集映射.json").read_text(encoding="utf-8"))
        assert state["status"] == "ok"
        assert mapping["clips"][0]["index"] == 0
        assert mapping["clips"][0]["src"] == "BVfake"
        assert 0 <= mapping["clips"][0]["frac"] <= 1
        assert mapping["sources"]["BVfake"]["url"].endswith("BVfake")
    print("full-agent footage contract passed")


if __name__ == "__main__":
    main()
