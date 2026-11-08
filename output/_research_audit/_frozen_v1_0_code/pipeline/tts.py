"""配音引擎：逐句合成拿精确时长 → 拼接旁白 → 供字幕与分镜使用。

引擎优先级：mimo-v2.5-tts（官方 /v1/chat/completions 音频模态，限时免费）
→ edge-tts（微软晓晓）→ Windows SAPI（离线兜底）。
mimo 调用约定（官方文档）：目标文本放 role=assistant；user 角色放风格指令；
响应 choices[0].message.audio.data 为 base64 音频。
"""
import asyncio
import base64
import json
import subprocess
from pathlib import Path

from config import (API_ROOT, API_TOKEN, MODEL_TTS, MODEL_TTS_VOICESIGN,
                    TTS_ATEMPO, TTS_STYLE, TTS_VOICE, TTS_VOICE_DESIGN)

import shutil

_FFMPEG_DIR = r"C:\ffmpeg-2025-11-02-git-f5eb11a71d-essentials_build\bin"
FFMPEG = shutil.which("ffmpeg") or str(Path(_FFMPEG_DIR) / "ffmpeg.exe")
FFPROBE = shutil.which("ffprobe") or str(Path(_FFMPEG_DIR) / "ffprobe.exe")

NORM_AR = 44100  # 统一重采样参数，保证 concat 无缝


def audio_duration(path: Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


class EdgeTTS:
    voice = "zh-CN-XiaoxiaoNeural"
    rate = "+0%"

    def synth(self, text: str, out: Path) -> None:
        import edge_tts

        async def _run():
            await edge_tts.Communicate(text, self.voice, rate=self.rate).save(str(out))

        asyncio.run(_run())


class SapiTTS:
    """Windows 自带语音（Microsoft Huihui），无需联网。"""

    voice = "Microsoft Huihui Desktop"

    def synth(self, text: str, out: Path) -> None:
        wav = out.with_suffix(".wav")
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$s.SelectVoice('{self.voice}');"
            f"$s.SetOutputToWaveFile('{wav.as_posix()}');"
            "$s.Speak([Console]::In.ReadToEnd());$s.Dispose()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            input=text.encode("utf-8"), capture_output=True,
        )
        if not wav.exists() or wav.stat().st_size < 128:
            raise RuntimeError("SAPI 合成失败")
        if wav != out:
            wav.replace(out)


class MimoTTS:
    """mimo-v2.5-tts / mimo-v2.5-tts-voicedesign（限时免费）。

    走根路径 /v1/chat/completions 音频模态：
    - assistant 角色 = 待朗读文本（官方强制约定）
    - user 角色 = 自然语言指令；预置音色模式=风格指令，voicedesign 模式=声线描述+风格指令
    - 预置音色：audio={"format":"wav","voice":...}
    - 定制音色（voicedesign）：audio 不带 voice 字段（带了会 400），描述放 user 消息
    预置音色：mimo_default/冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean。
    """

    def __init__(self, voice: str = "", style: str = "", design: str = ""):
        self.voice = voice or TTS_VOICE
        self.style = style or TTS_STYLE
        self.design = design or TTS_VOICE_DESIGN
        self.model = MODEL_TTS_VOICESIGN if self.design else MODEL_TTS

    def synth(self, text: str, out: Path) -> None:
        import requests

        user_content = f"{self.design}。{self.style}" if self.design else self.style
        audio = {"format": "wav"} if self.design else {"format": "wav", "voice": self.voice}
        body = {
            "model": self.model, "max_tokens": 8000,
            "messages": [
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": text},
            ],
            "audio": audio,
        }
        resp = requests.post(
            f"{API_ROOT}/v1/chat/completions",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {API_TOKEN}",
                "x-api-key": API_TOKEN,
            },
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=180,
        )
        resp.raise_for_status()
        msg = (resp.json().get("choices") or [{}])[0].get("message", {})
        data = (msg.get("audio") or {}).get("data")
        if not data:
            raise RuntimeError(f"mimo-v2.5-tts 未返回音频: {str(msg)[:160]}")
        out.write_bytes(base64.b64decode(data))


def get_engine(name: str = "mimo"):
    """引擎选择：mimo（默认，限时免费）/ edge / sapi。"""
    if name == "edge":
        try:
            import edge_tts  # noqa: F401
            return EdgeTTS()
        except ImportError:
            return SapiTTS()
    if name == "sapi":
        return SapiTTS()
    return MimoTTS()


def _normalize(src: Path, dst: Path) -> None:
    subprocess.run(
        [FFMPEG, "-y", "-i", str(src), "-ar", str(NORM_AR), "-ac", "1",
         "-af", f"atempo={TTS_ATEMPO}", "-c:a", "pcm_s16le", str(dst)],
        capture_output=True,
    )
    if not dst.exists() or dst.stat().st_size < 256:
        raise RuntimeError(f"音频归一化失败: {src}")


def synth_sig(engine) -> str:
    """合成签名：引擎/音色/定制声线/风格/提速任一变化都应重配。"""
    return (f"{type(engine).__name__}|{getattr(engine, 'voice', '?')}"
            f"|{getattr(engine, 'design', '')}|{getattr(engine, 'style', '')}|{TTS_ATEMPO}")


def cache_fresh(audio_dir: Path, engine=None) -> bool:
    """texts.json 缓存的合成签名与当前配置一致且句子齐全。"""
    try:
        cached = json.loads((audio_dir / "texts.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    if not cached:
        return False
    sig = synth_sig(engine or get_engine())
    return all(isinstance(v, dict) and v.get("sig") == sig for v in cached.values())


def synthesize_narration(sentences: list, workdir: Path, engine=None, gap: float = 0.2) -> dict:
    """逐句合成 → 归一化 WAV → 拼接 → 返回 {audio, segments}。

    segments: [{index, text, start, end}]，start/end 为拼接后的时间轴，
    直接用于字幕与画面分镜对齐。
    """
    engine = engine or get_engine()
    workdir.mkdir(parents=True, exist_ok=True)

    # 引擎失败时逐级回退：mimo → edge → SAPI
    chain = [engine]
    for alt in (EdgeTTS(), SapiTTS()):
        if not any(type(e) is type(alt) for e in chain):
            chain.append(alt)
    ci = 0

    wavs: list = []
    # 文本感知缓存：句子改过或音色/风格/提速变了 → 作废旧音频重配
    sig = synth_sig(engine)
    cache_file = workdir / "texts.json"
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        cached = {}
    for i, sent in enumerate(sentences):
        raw_mp3 = workdir / f"sent_{i:03d}.mp3"
        norm_wav = workdir / f"norm_{i:03d}.wav"
        if cached.get(str(i)) != {"text": sent, "sig": sig}:
            raw_mp3.unlink(missing_ok=True)
            norm_wav.unlink(missing_ok=True)
        if not raw_mp3.exists() or raw_mp3.stat().st_size < 200:
            try:
                chain[ci].synth(sent, raw_mp3)
            except Exception as exc:  # 当前引擎失败 → 逐级兜底
                if ci >= len(chain) - 1:
                    raise
                ci += 1
                print(f"[tts] 引擎失败（{exc}），回退 {type(chain[ci]).__name__}")
                chain[ci].synth(sent, raw_mp3)
        if not norm_wav.exists() or norm_wav.stat().st_size < 256:
            _normalize(raw_mp3, norm_wav)
        wavs.append((sent, norm_wav))
    cache_file.write_text(
        json.dumps({str(i): {"text": s, "sig": sig} for i, s in enumerate(sentences)},
                   ensure_ascii=False),
        encoding="utf-8")

    sil = workdir / "sil.wav"
    if not sil.exists():
        subprocess.run(
            [FFMPEG, "-y", "-f", "lavfi", "-i", f"anullsrc=r={NORM_AR}:cl=mono",
             "-t", str(gap), str(sil)],
            capture_output=True,
        )

    lines, segments, t = [], [], 0.0
    for i, (sent, wav) in enumerate(wavs):
        dur = audio_duration(wav)
        segments.append({"index": i, "text": sent, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur
        lines.append(f"file '{wav.resolve().as_posix()}'")  # 绝对路径：concat 按清单所在目录解析
        if i < len(wavs) - 1:
            lines.append(f"file '{sil.resolve().as_posix()}'")
            t += gap

    concat_list = workdir / "concat.txt"
    concat_list.write_text("\n".join(lines), encoding="utf-8")
    narration = workdir / "narration.wav"
    subprocess.run(
        [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c:a", "pcm_s16le", str(narration)],
        capture_output=True,
    )
    if not narration.exists() or narration.stat().st_size < 1024:
        raise RuntimeError("旁白拼接失败")

    (workdir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"audio": narration, "segments": segments}
