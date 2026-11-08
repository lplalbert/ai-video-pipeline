"""Offline regression check for publish-kit filename changes on retry."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline.publish import build_publish_kit


def main():
    script = {"hook": "测试文稿", "sections": [], "tags": []}
    platforms = ("bilibili", "xigua_douyin", "shipinhao")
    with TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        (workdir / "成片.mp4").write_bytes(b"test")
        build_publish_kit(script, workdir, topic="测试",
                          titles_override={key: ["旧标题"] for key in platforms})
        build_publish_kit(script, workdir, topic="测试",
                          titles_override={key: ["新标题"] for key in platforms})
        kit = json.loads((workdir / "投稿包.json").read_text(encoding="utf-8"))
        assert (workdir / kit["video_file"]).is_file()
        assert not (workdir / "旧标题.mp4").exists()
    print("publish retry rename ok")


if __name__ == "__main__":
    main()
