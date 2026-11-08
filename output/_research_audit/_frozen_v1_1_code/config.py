"""全局配置：API、模型、路径。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

API_BASE = os.environ.get("MIMO_API_BASE", "https://token-plan-cn.xiaomimimo.com/anthropic").rstrip("/")
API_TOKEN = os.environ.get("MIMO_API_TOKEN", "")
MODEL_FLASH = os.environ.get("MODEL_FLASH", "mimo-v2.6-flash")
MODEL_PRO = os.environ.get("MODEL_PRO", "mimo-v2.6-pro")
MODEL_TTS = os.environ.get("MODEL_TTS", "mimo-v2.5-tts")
MODEL_TTS_VOICESIGN = os.environ.get("MODEL_TTS_VOICESIGN", "mimo-v2.5-tts-voicedesign")

# TTS 走 OpenAI 式 /v1/chat/completions（去掉 /anthropic 前缀即为根）
API_ROOT = API_BASE.replace("/anthropic", "")
# 预置音色：mimo_default/冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean（限时免费）
# 用户认可：白桦（产线默认）；定制解说声线不稳定已弃用（见下）
TTS_VOICE = os.environ.get("MIMO_TTS_VOICE", "白桦")
# 定制音色（voicedesign 模型，音色描述放 user 消息、audio 不带 voice 字段）：
# 填自然语言声线描述即启用，优先于预置音色；置空退回 TTS_VOICE 预置音色。
# ⚠ 默认置空：voicedesign 每句独立采样声线，41 句=41 次重新生成，音色/语速会漂
# （实测语速 CV 22.6%、音色明暗 CV 23.9%），产线要用固定声线（白桦）保稳定。
# 想试验定制声线再往 .env 填 MIMO_TTS_VOICE_DESIGN，例如原解说声线描述：
#   电竞赛事解说员男声，二十七八岁，声线低沉有磁性而不闷，咬字清晰有力，
#   爆发力强，情绪饱满有激情，抑扬顿挫起伏明显，重音落在关键数字和人名上，
#   像体育解说员一样有感染力
TTS_VOICE_DESIGN = os.environ.get("MIMO_TTS_VOICE_DESIGN", "")
# 自然语言风格指令（放 user 角色，控制语速/情绪/腔调）
TTS_STYLE = os.environ.get("MIMO_TTS_STYLE",
                           "电竞赛事解说风格，语速快，利落不拖沓，语速均匀稳定、每句节奏一致，"
                           "句间停顿短促，情绪饱满有激情，"
                           "抑扬顿挫起伏明显，重音落在关键数字和人名上，像体育解说员一样有感染力，不要播音腔。")
# 机械提速倍率（ffmpeg atempo）；风格指令管情绪，这里保底管语速
TTS_ATEMPO = os.environ.get("MIMO_TTS_ATEMPO", "1.3")

OUTPUT_DIR = ROOT / "output"
LIB_DIR = ROOT / "素材库"
PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "").strip()
# Pixabay 免费素材 API key（可选，和 Pexels 一起喂本地素材库）
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
# 素材库积攒默认限速（Pixabay 条款禁「系统性批量扒库」——增量攒池 OK，爬全库不行）
MATLIB_PER_KW_CAP = 10          # 单关键词最多存多少条
MATLIB_TOTAL_CAP = 120          # 单次运行累计上限
MATLIB_SLEEP_S = 1.2            # 每次素材请求之间的间隔（秒）
# 可选：B站登录 Cookie（SESSDATA），填了才能取字幕做引用定位（B站 need_login_subtitle）
BILI_SESSDATA = os.environ.get("BILI_SESSDATA", "").strip()

OUTPUT_DIR.mkdir(exist_ok=True)
LIB_DIR.mkdir(exist_ok=True)
