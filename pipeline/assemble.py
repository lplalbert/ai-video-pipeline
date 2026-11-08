"""FFmpeg 合成：分镜画面 + 配音 + 字幕 + BGM + 封面。"""
import subprocess
from pathlib import Path

from pipeline.tts import FFMPEG

W, H, FPS = 1920, 1080, 30


def _run(args: list) -> None:
    proc = subprocess.run([FFMPEG, "-y", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {proc.stderr[-500:]}")


def _segment_video(src: str, duration: float, out: Path) -> Path:
    """把素材裁到目标时长并统一 1080p30。

    src 分三种：视频素材（短了循环补齐）/ 图片字卡（Ken Burns 缓推动镜）/
    'color' 渐变底（最后兜底）。
    """
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
          f"crop={W}:{H},fps={FPS},setsar=1,format=yuv420p")
    if src == "color":
        _run(["-f", "lavfi", "-i",
              f"gradients=s={W}x{H}:c0=0x101828:c1=0x1f2a44:d={duration:.3f}:speed=0.02",
              "-t", f"{duration:.3f}", "-vf", vf, "-an", str(out)])
    elif str(src).lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        # 静态字卡/图片 → zoompan 缓推，让画面有运动感（一帧输入产出 frames 帧）
        frames = max(2, round(duration * FPS))
        zp = (f"zoompan=z='min(1.0+0.0011*on,1.16)'"
              f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={W}x{H}:fps={FPS}")
        _run(["-i", str(src),
              "-vf", (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
                      f"crop={W * 2}:{H * 2},{zp},setsar=1,format=yuv420p"),
              "-frames:v", str(frames), "-an", str(out)])
    else:
        # 素材不够长则循环补齐，保证不出现黑帧
        _run(["-stream_loop", "-1", "-i", src, "-t", f"{duration:.3f}",
              "-vf", vf, "-an", str(out)])
    return out


def concat_segments(seg_paths: list, out: Path) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in seg_paths),
                   encoding="utf-8")
    _run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    return out


def mux(narration: Path, video: Path, srt: Path = None, bgm: Path = None,
        out: Path = None, burn_subs: bool = True) -> Path:
    """合成成片：配音为主轨，BGM 压低混入，字幕烧录（失败则退回软字幕）。"""
    out = out or video.with_name("成片.mp4")
    has_bgm = bool(bgm and Path(bgm).exists())
    burn = bool(burn_subs and srt and Path(srt).exists())

    inputs = ["-i", str(video), "-i", str(narration)]
    if has_bgm:
        inputs += ["-i", str(bgm)]

    if has_bgm:
        afilters = ("[2:a]volume=0.07[bg];"
                    "[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]")
        audio_map = ["-map", "[aout]"]
    else:
        afilters = ""
        audio_map = ["-map", "1:a"]

    if burn:
        sub = str(srt).replace("\\", "/").replace(":", "\\:")
        vfilter = (f"[0:v]subtitles='{sub}':force_style='"
                   f"FontName=Microsoft YaHei,FontSize=11,PrimaryColour=&H00FFFFFF,"
                   f"OutlineColour=&H00000000,Outline=1,MarginV=36'[vout]")
        video_map = ["-map", "[vout]"]
        v_codec = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        vfilter = ""
        video_map = ["-map", "0:v"]
        v_codec = ["-c:v", "copy"]

    fc = ";".join(f for f in (vfilter, afilters) if f)
    cmd = [*inputs]
    if fc:
        cmd += ["-filter_complex", fc]
    cmd += video_map + audio_map + v_codec + ["-c:a", "aac", "-b:a", "192k", "-ac", "2",
                                              "-shortest", str(out)]
    try:
        _run(cmd)
    except RuntimeError:
        # 字幕烧录失败（缺 libass 等）→ 软字幕
        inputs2 = [*inputs]
        if burn:
            inputs2 += ["-i", str(srt)]
        cmd2 = [*inputs2]
        if afilters:
            cmd2 += ["-filter_complex", afilters]
        cmd2 += ["-map", "0:v"] + audio_map + ["-pix_fmt", "yuv420p", "-c:v", "libx264",
                                               "-preset", "medium", "-crf", "20",
                                               "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        if burn:
            cmd2 += ["-map", "2:s", "-c:s", "mov_text"]
        cmd2 += ["-shortest", str(out)]
        _run(cmd2)
    return out


def make_thumbnail(video: Path, title: str, out: Path) -> Path:
    """取第 3 秒帧 + 大字标题封面（textfile 规避中文转义问题）。"""
    png = out.with_name("封面帧.png")
    _run(["-ss", "3", "-i", str(video), "-frames:v", "1", str(png)])
    if not png.exists():
        return out
    title_file = out.with_suffix(".txt")
    title_file.write_text(title[:24], encoding="utf-8")
    font = "C\\:/Windows/Fonts/msyh.ttc"
    try:
        _run(["-i", str(png),
              "-vf", (f"drawtext=fontfile='{font}':textfile='{title_file.as_posix()}'"
                      f":fontcolor=white:fontsize=72:borderw=4:bordercolor=black:"
                      f"x=(w-text_w)/2:y=h*0.42"),
              str(out)])
    except RuntimeError:
        png.replace(out)  # drawtext 不可用则至少给到截帧
    return out


def assemble_video(segments: list, manifest: list, narration: Path, workdir: Path,
                   srt: Path = None, bgm: Path = None, title: str = "封面") -> Path:
    """整条合成链：分镜→拼接→混音→封面。"""
    seg_paths = []
    n = len(segments)
    for k, seg in enumerate(segments):
        item = manifest[seg["index"]]
        # 画面要铺满句间静音（音频时间轴含 gap，句长 end-start 不含），
        # 否则视频比旁白短 (n-1)*gap 且音画渐进错位。
        if k < n - 1:
            dur = max(0.3, segments[k + 1]["start"] - seg["start"])
        else:
            dur = max(0.3, seg["end"] - seg["start"])
        p = workdir / f"vseg_{seg['index']:03d}.mp4"
        _segment_video(item["clip"], dur, p)
        seg_paths.append(p)
    silent = concat_segments(seg_paths, workdir / "video_nomux.mp4")
    final = mux(narration, silent, srt=srt, bgm=bgm, out=workdir / "成片.mp4")
    # 构建断言：成片时长应≈旁白末点（防音画错位/截尾回归）
    expect = float(segments[-1]["end"])
    try:
        import subprocess
        probe = subprocess.run(
            [str(Path(FFMPEG).with_name(Path(FFMPEG).stem.replace("ffmpeg", "ffprobe") + Path(FFMPEG).suffix)),
             "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
            capture_output=True, text=True)
        got = float(probe.stdout.strip() or 0)
        if abs(got - expect) > 0.5:
            print(f"[warn] 成片时长 {got:.2f}s 与旁白末点 {expect:.2f}s 偏差 >0.5s，请检查分段时长")
    except Exception:
        pass
    make_thumbnail(final, title, workdir / "封面.png")
    return final
