#!/usr/bin/env python3
"""P1 全双工语音引擎(英文):边听边说、可打断、按话轮接话、流式回复。

音频:通话声音从「外置麦克风」进来,AI 声音从「外置耳机」送回通话。
链路:Silero VAD → 判断说完(静音 + Smart Turn + 文本完整度)→ Parakeet 识别
     → 大模型流式输出 → 按句切分 → Kokoro 合成 → 可中断播放
打断:AI 说话或思考时一直在听,对方开口超过 BARGE_MS 就立刻停止播放、取消生成。
大模型:OpenAI 兼容接口(默认 3090 上的 qwen3.8-27b,关闭思考模式)。

用法:
  venv312/bin/python duplex.py             等 iPhone 13 来电(callctl 事件),接通后开始对话
  venv312/bin/python duplex.py --now       不等来电,立刻开始一段对话(用于已接通的通话)
"""
import json, os, queue, re, subprocess, sys, threading, time, urllib.request

import numpy as np
import onnxruntime as ort
import sounddevice as sd

# ---------------- 配置 ----------------
HERE = os.path.dirname(os.path.abspath(__file__))
IN_DEV = os.environ.get("AUDIO_IN", "外置麦克风")
OUT_DEV = os.environ.get("AUDIO_OUT", "外置耳机")
CTL = os.environ.get("CALLCTL_URL", "http://127.0.0.1:8766")
EVENTS = os.path.expanduser("~/tools/iphone-call/events-13.jsonl")
CALLS_DIR = os.path.expanduser("~/tools/voice/calls")
LLM_URL = os.environ.get("LLM_URL", "http://192.168.88.30:1280/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b")
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
AUTO_ANSWER = os.environ.get("AUTO_ANSWER", "1") == "1"

SR = 16000
VAD_FRAME = 512                 # Silero 16k 下每帧 512 点 = 32ms
FRAME_MS = 32
VAD_ON = 0.5                    # 语音概率阈值
VAD_ON_SPEAKING = 0.85          # AI 说话时提高阈值,减少回声误触发
MIN_SPEECH_MS = 200             # 至少说这么久才算一句
EARLY_CHECK_MS = 260            # 静音这么久先做一次"说完了吗"判断(严格:语调+文本都完整,或以问号结尾)  [调优A: 350→260]
SECOND_CHECK_MS = 560           # 再等到这么久做第二次判断(宽松:文本完整即可)  [调优A: 800→560]
DIGIT_SILENCE_MS = 1150         # 对方在报号码时,静音这么久才算说完  [调优A: 1600→1150]
MAX_SILENCE_MS = 950            # 静音超过这么久无论如何算说完  [调优A: 1400→950]
BARGE_MIN_DB = -36             # AI 说话时,插话的平均音量至少要这么大(过滤轻微杂音)
BARGE_MS = 300                  # AI 说话时对方连续说话超过这么久 → 打断
PREROLL_MS = 300
MAX_UTT_S = 20

GREETING = "Hi, this is Lei's AI assistant. This call may be recorded. Who's calling, and what is it regarding?"

SYSTEM = """You are Lei's AI phone assistant, answering a live phone call on his behalf. Everything you write is spoken aloud by text-to-speech.

How to talk:
- Speak like a friendly, efficient human receptionist. One or two short sentences per turn, about 20 words at most.
- Plain spoken sentences only: no lists, markdown, emojis, URLs or symbols. Write numbers the way they are said.
- Reply in English unless you are told otherwise for this call.
- The caller's words come from speech recognition and may contain errors. If something is unclear, ask them to repeat it.
- If the caller interrupts you, your last reply is cut off where they started talking; continue naturally from what they said.
- Names are often misheard. If a name sounds unusual or unclear, ask the caller to spell it.
- When speaking Chinese, refer to the owner as 张雷 (or 张先生), never just 雷, and call yourself 张雷的助理.
- The owner's name is Lei (pronounced like "Lay"). Speech recognition often writes it as "lay", "Leigh", "Lee", "Lane" or "Ray"; when a caller seems to say one of these, they mean Lei. For example "I'm calling to lay for an appointment" means "I'm calling Lei for an appointment". His full name is Lei Zhang (Zhang Lei), which may come through as "Gang Lee", "John Lee" or similar; still never confirm that a named person is Lei.

Your job:
- Find out who is calling and what it is about. As soon as the purpose is clear, call set_category once.
- You cannot see Lei's calendar or check his schedule, and you cannot transfer the call. Lei will call back.
- Never confirm or share Lei's personal information (address, birthday, accounts, whether he is the person they named). Never make commitments or payments on his behalf.

How to handle each kind of call:
- sales (insurance, loans, warranties, solar, surveys, any offer): politely decline, ask them to add this number to their do-not-call list, say goodbye, and call end_call in the same reply.
- scam (urgent threats, account suspended, IRS or Social Security, gift cards, crypto or wire payments, verification codes, "is this Lei Zhang?" with no clear reason): never say yes, never confirm any detail, never give any code or number. Say you will verify through official channels, say goodbye, and call end_call in the same reply.
- customer, personal or other: collect their name, a callback number (the caller ID is given at the start; ask if that is the best number), the reason, and a good time to call back. Then call take_message, tell them Lei will get back to them, ask if there is anything else, and when they are done say goodbye and call end_call.
- Mark urgency "high" only for real emergencies or time-critical matters.
- If the caller ID is unknown, ask for their number directly instead of asking whether the calling number is fine.
- Ask for each detail at most twice. If the caller says goodbye, declines, or gives an odd answer, do not push: save the message with what you have, say a short goodbye, and call end_call.

Tools:
- Always say something out loud in the same reply as a tool call, except set_category which can be silent.
- Call end_call only together with your final goodbye sentence."""

TOOLS = [
    {"type": "function", "function": {
        "name": "set_category", "description": "Record what kind of call this is, as soon as it is clear.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "enum": ["sales", "scam", "customer", "personal", "other"]},
            "reason": {"type": "string", "description": "short evidence for the category"}},
            "required": ["category", "reason"]}}},
    {"type": "function", "function": {
        "name": "take_message", "description": "Save a message for Lei once you have the caller's details.",
        "parameters": {"type": "object", "properties": {
            "caller_name": {"type": "string"},
            "company": {"type": "string", "description": "company or organization, empty if none"},
            "callback_number": {"type": "string"},
            "reason": {"type": "string"},
            "best_time": {"type": "string", "description": "when to call back, empty if not given"},
            "urgency": {"type": "string", "enum": ["low", "normal", "high"]}},
            "required": ["caller_name", "callback_number", "reason", "urgency"]}}},
    {"type": "function", "function": {
        "name": "end_call", "description": "Hang up after your goodbye sentence has been spoken.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "enum": ["sales", "scam", "customer", "personal", "other"]},
            "summary": {"type": "string", "description": "one or two sentence summary of the call for Lei"}},
            "required": ["category", "summary"]}}},
]

REPORT_TOOL = {"type": "function", "function": {
    "name": "report_result", "description": "Report the outcome of this outbound call to Lei, once, before saying goodbye.",
    "parameters": {"type": "object", "properties": {
        "status": {"type": "string", "enum": ["success", "failed", "partial", "needs_lei"]},
        "summary_zh": {"type": "string", "description": "给张雷看的中文结果摘要"},
        "details": {"type": "string", "description": "key facts: date, time, names, confirmation number, price, etc."},
        "follow_up": {"type": "string", "description": "what Lei still needs to do, empty if nothing"}},
        "required": ["status", "summary_zh"]}}}
ALL_TOOLS = {t["function"]["name"]: t for t in TOOLS + [REPORT_TOOL]}

SOULS = f"{HERE}/souls"


def load_soul(name):
    """读取 souls/<name>.md:返回 (front matter 字典, 正文)"""
    import yaml
    txt = open(f"{SOULS}/{name}.md").read()
    m = re.match(r"---\n(.*?)\n---\n(.*)$", txt, re.S)
    return (yaml.safe_load(m.group(1)) or {}, m.group(2).strip()) if m else ({}, txt.strip())


def task_card_text(task):
    keys = [("goal", "Goal"), ("to_name", "Calling"), ("to", "Number"), ("details", "Details to give"),
            ("may_share", "You may share"), ("may_decide", "You may decide on your own"),
            ("must_not", "Do not"), ("language", "Language to use"), ("notes", "Notes")]
    lines = []
    for k, label in keys:
        v = task.get(k)
        if v:
            lines.append(f"- {label}: {', '.join(v) if isinstance(v, list) else v}")
    return "\n".join(lines)


def build_persona(role, task=None):
    """组合 系统提示 = soul 正文 + 机主信息 + 任务卡;每通电话现读文件,改了 soul 下一通就生效"""
    meta, body = load_soul(role)
    _, owner = load_soul("owner")
    system = body + "\n\nOwner facts:\n" + owner
    if task:
        system += "\n\nTask card for this call:\n" + task_card_text(task)
    tools = [ALL_TOOLS[n] for n in meta.get("tools", []) if n in ALL_TOOLS]
    return meta, system, tools


CONTACTS_CACHE = f"{HERE}/contacts_cache.json"


def _norm_phone(raw):
    s = str(raw or "").strip()
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return ""
    if s.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def match_contact(info):
    """来电匹配:先按姓名(iPhone 常显示联系人名),再按归一化号码,查本地缓存。"""
    try:
        cache = json.load(open(CONTACTS_CACHE))
    except Exception:
        return None
    raw = str(info.get("number_raw") or info.get("number") or "").strip()
    if not raw:
        return None
    c = cache.get("by_name", {}).get(raw)
    if not c:
        ph = _norm_phone(raw)
        if ph:
            c = cache.get("by_phone", {}).get(ph)
    if not c:
        return None
    c = dict(c)
    c["_souls"] = cache.get("souls", {})
    return c


def contact_profile_text(c):
    lines = []
    nm = (f"{c.get('title','')} {c.get('name','')}").strip()
    if nm:
        lines.append(f"- Name: {nm}")
    for k, label in (("relationship", "Relationship to owner"), ("org", "Organization"),
                     ("birthday", "Birthday"), ("preferences", "Preferences"), ("notes", "Notes")):
        if c.get(k):
            lines.append(f"- {label}: {c[k]}")
    mems = c.get("memories") or []
    if mems:
        lines.append("- Past calls (most recent first):")
        for m in mems[:5]:
            lines.append(f"    · {m}")
    return "\n".join(lines)


NOTIFY_IMESSAGE = os.environ.get("NOTIFY_IMESSAGE", "+16692964129")   # 机主 iAir;设为空字符串则只做本机通知
NOTIFY_LOG = os.path.expanduser("~/tools/voice/notify.log")


def notify(title, body, image=None):
    """通知机主:写日志 + 本机通知中心;设置了 NOTIFY_IMESSAGE 时再发一条 iMessage"""
    with open(NOTIFY_LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {title}\n{body}\n\n")
    esc = lambda x: x.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"'],
                   capture_output=True)
    if NOTIFY_IMESSAGE:
        script = ('tell application "Messages" to send "%s" to participant "%s" of '
                  '(1st account whose service type = iMessage)') % (esc(title + "\n" + body), esc(NOTIFY_IMESSAGE))
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if r.returncode != 0:
            log("iMessage 发送失败:", r.stderr.strip())
        if image and os.path.exists(image):
            # Messages 只能从 ~/Pictures 等位置发文件,文件名用英文
            import shutil
            dst = os.path.expanduser(f"~/Pictures/voice_samples/ring_{time.strftime('%H%M%S')}.png")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(image, dst)
            time.sleep(2)
            subprocess.run(["osascript", "-e", ('tell application "Messages" to send (POSIX file "%s") to participant "%s" of '
                                                '(1st account whose service type = iMessage)') % (dst, esc(NOTIFY_IMESSAGE))],
                           capture_output=True)
    log(f"  🔔 通知: {title} | {body.replace(chr(10), ' / ')}")


EXTRACT_PROMPT = """Below is the transcript of a phone call answered by Lei's AI assistant (speech recognition may contain errors; "lay", "Lane", "Leigh" usually mean Lei).
Return only a JSON object with these keys:
category: one of sales, scam, customer, personal, other
caller_name: string or ""
company: string or ""
callback_number: string or "" (use the caller ID if the caller agreed it is fine)
reason: short string, why they called
best_time: string or ""
urgency: one of low, normal, high
needs_callback: true or false
summary: one or two sentences for Lei, in Chinese"""


def extract_call(rec):
    """通话结束后,把整段记录交给模型抽取结构化信息(比通话中靠模型自觉调工具可靠)"""
    lines = [f"Caller ID: {rec.get('number_raw') or 'unknown'} ({rec.get('location') or ''})"]
    for t in rec["turns"]:
        if t.get("text") and t["text"] != "—":
            lines.append(("Caller: " if t["role"] == "caller" else "Assistant: ") + t["text"])
    body = {"model": LLM_MODEL, "max_tokens": 600, "reasoning_effort": "none",
            "messages": [{"role": "system", "content": EXTRACT_PROMPT},
                         {"role": "user", "content": "\n".join(lines)}]}
    req = urllib.request.Request(LLM_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    txt = json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else {}


CHANNEL_HINTS = [("WeChat", "wechat", "微信"), ("WhatsApp", "whatsapp", "WhatsApp"), ("Telegram", "telegram", "Telegram"),
                 ("Signal", "signal", "Signal"), ("Messenger", "messenger", "Messenger"), ("FaceTime", "facetime", "FaceTime"),
                 ("Skype", "skype", "Skype"), ("Discord", "discord", "Discord"), ("Zoom", "zoom", "Zoom"), ("X ", "x", "X")]


def _load_channels():
    try:
        return {c["id"]: c for c in json.load(open(f"{HERE}/channels.json"))["channels"]}
    except Exception:
        return {}


CHANNEL_CFG = _load_channels()


def parse_caller(info):
    """从系统来电界面的文字里识别来源应用和对方名字。
    应用来电时,标签处显示 'WeChat Audio…' 之类,名字处显示 '<名字> invites you to a voice call'"""
    raw = info.get("number_raw") or ""
    label = info.get("location") or ""
    for key, cid, zh in CHANNEL_HINTS:
        if key.lower() in label.lower() or key.lower() in raw.lower():
            name = re.sub(r"\s*(invites you to a (voice|video) call|is calling).*$", "", raw, flags=re.I).strip()
            return {"channel": cid, "channel_zh": zh, "name": name or raw}
    return {"channel": "cellular", "channel_zh": "手机来电", "name": raw}


CATEGORY_ZH = {"sales": "推销", "scam": "疑似诈骗", "customer": "客户", "personal": "私人", "other": "其他"}

def looks_garbled(text):
    """识别结果里同一个短语反复出现(Parakeet 遇到中文等不支持的语言时的典型表现)"""
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) < 12:
        return False
    grams = [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
    top = max(grams.count(g) for g in set(grams))
    return top >= 4 and top / len(grams) > 0.1


ONLY_FILLER = re.compile(r"^\W*((um+|uh+|er+|mm+|hmm+|mhm+|ah+|oh+|嗯+|啊+|呃+|额+|哦+|噢+|唔+|아+|어+|음+|응+|あ+|え+ー*|うん|ん+)\W*)+$", re.I)
FILLERS_END = re.compile(
    r"\b(um+|uh+|er+|and|but|or|so|because|the|a|an|to|of|for|with|that|my|your|his|her|our|their|is|are|was|i|i'm|we|if|when|which|like)\W*$",
    re.I)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------- 模型 ----------------
class VAD:
    def __init__(self):
        self.s = ort.InferenceSession(f"{HERE}/models/silero_vad.onnx")
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.ctx = np.zeros(64, dtype=np.float32)

    def __call__(self, frame):
        x = np.concatenate([self.ctx, frame]).reshape(1, -1).astype(np.float32)
        out, self.state = self.s.run(None, {"input": x, "state": self.state, "sr": np.array(SR, dtype=np.int64)})
        self.ctx = frame[-64:]
        return float(out.reshape(-1)[0])


class TurnDetector:
    """Smart Turn v3:根据语音(语调、节奏)判断一句话是否说完"""

    def __init__(self):
        from transformers import WhisperFeatureExtractor
        self.fe = WhisperFeatureExtractor(chunk_length=8)
        self.s = ort.InferenceSession(f"{HERE}/models/smart-turn-v3.2-cpu.onnx")

    def __call__(self, audio):
        a = audio[-8 * SR:]
        a = np.pad(a, (8 * SR - len(a), 0))
        x = self.fe(a, sampling_rate=SR, return_tensors="np", padding="max_length", max_length=8 * SR,
                    truncation=True, do_normalize=True).input_features.astype(np.float32)
        y = float(self.s.run(None, {"input_features": x})[0].reshape(-1)[0])
        return y if 0 <= y <= 1 else float(1 / (1 + np.exp(-y)))


class ASR:
    def __init__(self):
        from parakeet_mlx import from_pretrained
        import mlx.core as mx
        self.mx = mx
        self.m = from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")

    def __call__(self, audio):
        import tempfile, soundfile as sf
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            sf.write(f.name, audio, SR)
            return self.m.transcribe(f.name).text.strip()


class WhisperASR:
    """中英文自动识别(微信等以中文来电为主的渠道用)。繁体转简体,过滤 Whisper 的幻觉句"""
    MODEL = "mlx-community/whisper-large-v3-turbo"
    BAD = ("字幕", "订阅", "点赞", "转发", "打赏", "明镜", "谢谢观看", "感谢观看", "请不吝", "Thank you for watching")

    def __init__(self):
        import mlx_whisper
        from opencc import OpenCC
        self.w = mlx_whisper
        self.t2s = OpenCC("t2s")

    def __call__(self, audio):
        peak = float(np.max(np.abs(audio)) + 1e-9)
        a = (audio / peak * 0.8).astype(np.float32)
        r = self.w.transcribe(a, path_or_hf_repo=self.MODEL, language=None,
                              condition_on_previous_text=False)
        segs = [x for x in r["segments"] if x.get("no_speech_prob", 0) < 0.6]
        text = self.t2s.convert("".join(x["text"] for x in segs)).strip()
        if any(b in text for b in self.BAD):
            return ""
        return text


class SenseVoiceASR:
    """SenseVoice(sherpa-onnx,CPU):中英粤日韩自动识别,短句约 0.05–0.15s,比 Whisper 快 10–25 倍"""
    M = f"{HERE}/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

    def __init__(self):
        import sherpa_onnx
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=f"{self.M}/model.int8.onnx", tokens=f"{self.M}/tokens.txt",
            num_threads=4, use_itn=True, language="auto")
        self.lock = threading.Lock()

    def __call__(self, audio):
        peak = float(np.max(np.abs(audio)) + 1e-9)
        a = (audio / peak * 0.8).astype(np.float32)
        with self.lock:
            st = self.rec.create_stream()
            st.accept_waveform(SR, a)
            self.rec.decode_stream(st)
            self.last_lang = (st.result.lang or "").strip("<|>")
            return st.result.text.strip()


class TTS:
    SR = 24000

    def __init__(self):
        from mlx_audio.tts.utils import load_model
        self.m = load_model("mlx-community/Kokoro-82M-bf16")

    def __call__(self, text):
        chunks = [np.array(r.audio, dtype=np.float32).reshape(-1)
                  for r in self.m.generate(text=text, voice=TTS_VOICE, speed=1.0, lang_code="a")]
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


class CloneTTS:
    """用户克隆音色(Qwen3-TTS 1.7B,样本 D 配置),流式输出:首段约 0.5s,生成速度约 1.5 倍实时"""
    SR = 24000

    def __init__(self):
        from mlx_audio.tts.utils import load_model
        from mlx_audio.utils import load_audio
        cfg = json.load(open(f"{HERE}/clone/voice_config.json"))
        self.m = load_model(cfg["model"])
        self.kw = dict(ref_audio=load_audio(f"{HERE}/{cfg['ref_audio']}", sample_rate=self.m.sample_rate),
                       ref_text=cfg["ref_text"], lang_code=cfg["lang_code"], temperature=cfg["temperature"])
        self.gain = 1.0
        warm = self("Thanks for calling. How can I help you today?")
        rms = float(np.sqrt(np.mean(warm ** 2)) + 1e-9)
        self.gain = min(4.0, 0.08 / rms)   # 统一到和 Kokoro 接近的响度(约 -22 dBFS)

    def stream(self, text, cancel=None):
        text = re.sub(r"[*_`#~]+", "", text).strip()   # 去掉模型偶尔输出的 Markdown 符号(如 **night**),免得念出来或停顿
        if not text:
            return
        kw = dict(self.kw)
        kw["lang_code"] = "chinese" if re.search(r"[\u4e00-\u9fff]", text) else "english"
        for r in self.m.generate(text=text, stream=True, streaming_interval=0.4, **kw):
            if cancel is not None and cancel.is_set():
                return
            yield np.clip(np.array(r.audio, dtype=np.float32).reshape(-1) * self.gain, -1, 1)

    def __call__(self, text):
        chunks = list(self.stream(text))
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


VOICE_MODE = os.environ.get("VOICE_MODE", "clone")   # clone = 用户克隆音色;kokoro = 通用音色


# ---------------- 音频输入输出 ----------------
class Player:
    """可随时清空的播放器(打断时立即静音)"""

    def __init__(self):
        self.buf = np.zeros(0, dtype=np.float32)
        self.lock = threading.Lock()
        self.played = 0
        self.stream = sd.OutputStream(device=OUT_DEV, samplerate=TTS.SR, channels=1, dtype="float32",
                                      callback=self._cb, blocksize=480)
        self.stream.start()

    def _cb(self, out, frames, t, status):
        with self.lock:
            n = min(frames, len(self.buf))
            out[:n, 0] = self.buf[:n]
            out[n:, 0] = 0
            self.buf = self.buf[n:]
            self.played += n

    def play(self, audio):
        with self.lock:
            self.buf = np.concatenate([self.buf, audio])

    def stop(self):
        with self.lock:
            self.buf = np.zeros(0, dtype=np.float32)

    def busy(self):
        return len(self.buf) > 0


class Mic:
    def __init__(self):
        dev = sd.query_devices(IN_DEV, "input")
        self.dev_sr = int(dev["default_samplerate"])
        self.step = self.dev_sr // SR
        self.q = queue.Queue()
        self.rest = np.zeros(0, dtype=np.float32)
        self.stream = sd.InputStream(device=IN_DEV, channels=1, samplerate=self.dev_sr, dtype="float32",
                                     blocksize=VAD_FRAME * self.step, callback=self._cb)
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        x = indata[:, 0]
        x = x[: len(x) // self.step * self.step].reshape(-1, self.step).mean(axis=1)
        x = np.concatenate([self.rest, x])
        while len(x) >= VAD_FRAME:
            self.q.put(x[:VAD_FRAME].copy())
            x = x[VAD_FRAME:]
        self.rest = x

    def drain(self):
        with self.q.mutex:
            self.q.queue.clear()


# ---------------- 大模型(流式) ----------------
def llm_stream(messages, cancel, calls=None, system=None, tools=None):
    """流式输出文本;工具调用累积到 calls(dict: index → {id, name, args})"""
    body = {"model": LLM_MODEL, "stream": True, "max_tokens": 400,
            "reasoning_effort": "none",   # 实测只有这个能关掉 Qwen 思考(enable_thinking=False 无效),首字 5s→0.5s
            "tools": tools if tools is not None else TOOLS,
            "messages": [{"role": "system", "content": system or SYSTEM}] + messages}
    req = urllib.request.Request(LLM_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        for line in r:
            if cancel.is_set():
                return
            line = line.decode().strip()
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            d = json.loads(line[5:])
            if d.get("choices"):
                delta = d["choices"][0].get("delta", {})
                for tc in delta.get("tool_calls") or []:
                    if calls is not None:
                        c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        c["id"] = tc.get("id") or c["id"]
                        fn = tc.get("function") or {}
                        c["name"] += fn.get("name") or ""
                        c["args"] += fn.get("arguments") or ""
                c = delta.get("content")
                if c:
                    yield c


SENT_END = re.compile(r"([.!?])(\s+|$)|([。！？；])")


def sentences(token_iter):
    """把流式 token 切成句子:遇到句号/问号/感叹号就吐出一句"""
    buf = ""
    for tok in token_iter:
        buf += tok
        while True:
            m = SENT_END.search(buf)
            if not m or len(buf[:m.end()].strip()) < 2:
                break
            yield buf[:m.end()].strip()
            buf = buf[m.end():]
    if buf.strip():
        yield buf.strip()


# ---------------- 对话会话 ----------------
class Session:
    def __init__(self, eng, info, mode="inbound", task=None):
        self.e = eng
        self.info = info
        self.mode = mode                      # inbound = 接听(receptionist) / outbound = 外呼(outbound)
        self.task = task
        self.meta, self.system, self.tools = build_persona("outbound" if mode == "outbound" else "receptionist", task)
        self.messages = []
        self.caller = parse_caller(info)
        # 来电匹配通讯录:命中则(可选)换用联系人指定的 soul,并注入其档案(姓名/关系/喜好/生日/历次记忆)
        self.contact = None
        if mode == "inbound":
            c = match_contact(info)
            if c:
                self.contact = c
                sid, souls = c.get("soul_id"), c.get("_souls", {})
                if sid and sid in souls:
                    s = souls[sid]
                    _, owner = load_soul("owner")
                    self.system = s["body"] + "\n\nOwner facts:\n" + owner
                    mtools = (s.get("meta") or {}).get("tools")
                    if mtools:
                        self.tools = [ALL_TOOLS[n] for n in mtools if n in ALL_TOOLS]
                self.system += ("\n\nYou already know this caller — greet them by name and be warm and personal. "
                                "Caller profile:\n" + contact_profile_text(c))
                log(f"  👤 识别来电联系人: {c.get('name')} (关系 {c.get('relationship') or '—'}"
                    + (f", soul {souls.get(sid,{}).get('name')}" if sid and sid in souls else "") + ")")
        # 渠道配置里 asr=whisper 的渠道(如微信)用中英自动识别,其他用更快的英文 Parakeet
        self.asr_engine = "auto" if mode == "outbound" else CHANNEL_CFG.get(self.caller["channel"], {}).get("asr", "parakeet")
        if mode == "outbound" and self.meta.get("reply_language", "follow") == "follow":
            self.system += ("\n\nLanguage: reply in the language the other person is using (English by default; "
                            "natural spoken Simplified Chinese if they speak Chinese).")
        self.rec = {"number": info.get("number"), "number_raw": info.get("number_raw"),
                    "channel": self.caller["channel"], "caller_name": self.caller["name"],
                    "ring_screenshot": info.get("ring_screenshot"),
                    "location": info.get("location"), "start": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "turns": [], "category": None, "messages": [], "summary": None, "end_reason": None,
                    "mode": mode, "task": task, "result": None}
        self.stop_evt = None
        self.reply_cancel = threading.Event()
        self.reply_thread = None
        self.speaking_text = []       # 当前回复已送去播放的句子

    # --- AI 说话 ---
    def say(self, text, cancel=None):
        for ch in self.e.tts_stream(text, cancel or threading.Event()):
            self.e.player.play(ch)

    def respond(self, user_text, t_end):
        """后台线程:流式生成 → 按句合成 → 播放 → 执行工具;可被 reply_cancel 中断"""
        self.reply_cancel = cancel = threading.Event()
        # 上一轮回复还没出声就被对方接着说打断了:把上一段话和这一段合成一句,避免模型看到碎片
        if (len(self.messages) >= 2 and self.messages[-1].get("role") == "assistant"
                and self.messages[-1].get("content") == "—" and not self.messages[-1].get("tool_calls")
                and self.messages[-2].get("role") == "user"):
            self.messages.pop()
            prev = self.messages.pop()["content"]
            user_text = prev + " " + user_text
            log(f"  (合并碎片) {user_text}")
        if self.mode == "inbound" and not self.messages and not user_text.startswith("[Caller ID"):
            if self.caller["channel"] == "cellular":
                cid = f"[Caller ID: {self.info.get('number_raw') or 'unknown'}, {self.info.get('location') or 'unknown location'}]"
            else:
                cid = (f"[Caller ID: {self.caller['name']} via {self.caller['channel_zh'] if self.caller['channel'] != 'wechat' else 'WeChat'} "
                       f"voice call. This is an app call: there is NO phone number, do not ask for or mention a caller ID number; "
                       f"Lei will call back in the same app, so you only need their name, reason and a good time]")
            rl = CHANNEL_CFG.get(self.caller["channel"], {}).get("reply_language", "en")
            if rl == "follow":
                cid += ("\n[Reply in the same language the caller is using. If the caller speaks Chinese, reply in natural, "
                        "colloquial spoken Simplified Chinese (short sentences, no English mixed in except names); if they speak English, reply in English. "
                        "If the caller asks you to switch language, switch.]")
            elif rl == "zh":
                cid += "\n[Always reply in natural, colloquial spoken Simplified Chinese, even if the caller speaks English.]"
            elif self.asr_engine in ("whisper", "sensevoice", "auto"):
                cid += "\n[The caller may speak Chinese or mix Chinese and English. You understand Chinese, but always reply in English.]"
            user_text = f"{cid}\n{user_text}"
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(3):   # 只有工具调用、没说话时,把工具结果交回模型再说一次
            calls = {}
            spoken = self._speak_reply(cancel, calls, t_end)
            if cancel.is_set():
                break
            if not calls:
                break
            ended = self._run_tools(calls, spoken)
            if ended or spoken:
                break
            t_end = time.time()

    def _speak_reply(self, cancel, calls, t_end):
        self.speaking_text = spoken = []
        first_audio = None
        t_llm = time.time()
        first_tok = None
        try:
            def toks():
                nonlocal first_tok
                for t in llm_stream(self.messages, cancel, calls, self.system, self.tools):
                    if first_tok is None:
                        first_tok = time.time() - t_llm
                    yield t
            for sent in sentences(toks()):
                if cancel.is_set():
                    break
                for ch in self.e.tts_stream(sent, cancel):
                    if cancel.is_set():
                        break
                    self.e.player.play(ch)
                    if first_audio is None:
                        first_audio = time.time() - t_end
                        log(f"  ⏱ 说完→AI开口 {first_audio:.2f}s(大模型首字 {first_tok or 0:.2f}s)")
                if cancel.is_set():
                    break
                spoken.append(sent)
        except Exception as ex:
            log("大模型出错:", repr(ex))
            if not cancel.is_set():
                self.say("Sorry, could you say that again?")
                spoken.append("Sorry, could you say that again?")
        if not spoken and not cancel.is_set() and not calls:
            fb = "Sorry, could you say that again?"
            self.say(fb)
            spoken.append(fb)
            log("  (大模型回复为空,用兜底句)")
        text = " ".join(spoken)
        if cancel.is_set():
            text = (text + " —").strip() if text else "—"
            calls.clear()   # 被打断的回复里的工具调用不执行
        msg = {"role": "assistant", "content": text or ("" if calls else "—")}
        if calls:
            msg["tool_calls"] = [{"id": c["id"] or f"call_{i}", "type": "function",
                                  "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                 for i, c in sorted(calls.items())]
        self.messages.append(msg)
        self.rec["turns"].append({"role": "ai", "text": text, "first_audio_s": first_audio,
                                  "llm_first_token_s": first_tok, "interrupted": cancel.is_set(),
                                  "tools": [c["name"] for c in calls.values()]})
        log(f"[AI{'(被打断)' if cancel.is_set() else ''}] {text}" + (f"  🛠 {[c['name'] for c in calls.values()]}" if calls else ""))
        return spoken

    def _run_tools(self, calls, spoken):
        """执行工具,把结果追加到对话历史。返回是否已结束通话"""
        ended = False
        names = [c["name"] for c in calls.values()]
        if self.mode == "outbound" and "report_result" in names and "end_call" not in names:
            # 外呼:模型报告了结果但忘了挂断(实测会发生),报告即视为结束
            calls[max(calls) + 1] = {"id": "auto_end", "name": "end_call",
                                     "args": json.dumps({"category": "other", "summary": "outbound task finished"})}
        for i, c in sorted(calls.items()):
            try:
                args = json.loads(c["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            name = c["name"]
            result = "ok"
            if name == "set_category":
                self.rec["category"] = args.get("category")
                log(f"  🏷 分类: {CATEGORY_ZH.get(args.get('category'), args.get('category'))}({args.get('reason', '')})")
            elif name == "take_message":
                self.rec["messages"].append(args)
                urgent = args.get("urgency") == "high"
                notify(("🚨 紧急留言" if urgent else "📞 来电留言") + f"({self.caller['channel_zh']}) {self.caller['name'] or ''}",
                       f"{args.get('caller_name', '?')}" + (f"({args['company']})" if args.get("company") else "")
                       + f"\n事由: {args.get('reason', '')}\n回电: {args.get('callback_number', '')}"
                       + (f",{args['best_time']}" if args.get("best_time") else ""),
                       image=self.info.get("ring_screenshot"))
                result = "Message saved. Lei will be notified."
            elif name == "report_result":
                self.rec["result"] = args
                st = {"success": "✅ 成功", "failed": "❌ 未办成", "partial": "⚠️ 部分完成", "needs_lei": "🙋 需要你处理"}.get(args.get("status"), args.get("status"))
                body = f"{args.get('summary_zh', '')}"
                if args.get("details"):
                    body += f"\n细节: {args['details']}"
                if args.get("follow_up"):
                    body += f"\n待办: {args['follow_up']}"
                notify(f"📤 外呼结果 {st}({(self.task or {}).get('to_name') or self.info.get('number_raw') or ''})", body)
                result = "Result recorded and sent to Lei."
            elif name == "end_call":
                if not spoken:   # 模型只调了挂断没说再见:补一句告别语(外呼是主叫,别说"谢谢来电")
                    self.say("Thanks for your help. Goodbye!" if self.mode == "outbound"
                             else "Thanks for calling. Goodbye!")
                    log("  (补了一句告别语)")
                self.rec["category"] = args.get("category") or self.rec["category"]
                self.rec["summary"] = args.get("summary")
                self.rec["end_reason"] = "ai_end_call"
                ended = True
                # 等告别语播完再挂
                while self.e.player.busy():
                    time.sleep(0.1)
                time.sleep(0.6)
                log("  📴 AI 挂断:", ctl("hangup"))
                if self.stop_evt:
                    self.stop_evt.set()
            else:
                result = f"unknown tool {name}"
            self.messages.append({"role": "tool", "tool_call_id": c["id"] or f"call_{i}", "content": result})
        return ended

    DIGITS = re.compile(r"\b(zero|oh|o|one|two|three|four|five|six|seven|eight|nine|\d+)\W*$", re.I)

    def expecting_digits(self, text=None):
        """AI 刚问了号码,或对方这句话以数字结尾 → 对方很可能在报号码,要多等"""
        if text is not None:
            self._last_partial = text
        last_ai = next((m.get("content") or "" for m in reversed(self.messages) if m.get("role") == "assistant"), "")
        asked = bool(re.search(r"\b(number|digits?|phone)\b", last_ai, re.I)) and self.caller["channel"] == "cellular"
        partial = getattr(self, "_last_partial", "") or ""
        return asked or bool(self.DIGITS.search(partial))

    def ai_active(self):
        return (self.reply_thread is not None and self.reply_thread.is_alive()) or self.e.player.busy()

    def barge_in(self):
        self.reply_cancel.set()
        self.e.player.stop()
        log("  ✋ 对方插话,AI 停止")

    # --- 主循环:听 ---
    def run(self, stop_evt):
        self.stop_evt = stop_evt
        e = self.e
        log("== 对话开始" + ("(外呼)" if self.mode == "outbound" else ""), self.info.get("number_raw") or "")
        if self.mode == "inbound":
            greeting = self.meta.get("greeting") or GREETING
            self.rec["turns"].append({"role": "ai", "text": greeting})
            self.say(greeting)
        t_open = time.time()
        opened = self.mode == "inbound"
        wait_s = float(self.meta.get("wait_for_callee_s", 2.5))
        e.mic.drain()
        e.vad.reset()
        pre = []
        seg = []
        in_speech = False
        speech_ms = silence_ms = 0
        checked = False
        cached_text = None
        while not stop_evt.is_set():
            # 外呼:接通后对方一直没开口,由 AI 先说
            if not opened and not in_speech and not self.messages and time.time() - t_open > wait_s:
                opened = True
                note = "[The call was just answered and the other person has not spoken yet. Start the conversation.]"
                self.reply_thread = threading.Thread(target=self.respond, args=(note, time.time()), daemon=True)
                self.reply_thread.start()
            try:
                fr = e.mic.q.get(timeout=0.2)
            except queue.Empty:
                continue
            active = self.ai_active()
            p = e.vad(fr)
            voiced = p > (VAD_ON_SPEAKING if active else VAD_ON)
            if not in_speech:
                pre.append(fr)
                pre = pre[-(PREROLL_MS // FRAME_MS):]
                if voiced:
                    speech_ms += FRAME_MS
                    if speech_ms >= (BARGE_MS if active else 64):
                        db = 20 * np.log10(np.sqrt(np.mean(np.concatenate(pre[-(BARGE_MS // FRAME_MS):]) ** 2)) + 1e-9)
                        if active and db < BARGE_MIN_DB:
                            speech_ms = 0          # 太轻,当作杂音,不打断 AI
                            continue
                        if active:
                            log(f"  (插话判定:语音概率 {p:.2f},音量 {db:.1f} dB)")
                            self.barge_in()
                        in_speech = True
                        seg = list(pre)
                        silence_ms = 0
                        checked = False
                else:
                    speech_ms = 0
                continue
            seg.append(fr)
            if voiced:
                speech_ms += FRAME_MS
                silence_ms = 0
                checked = False
                cached_text = None
                continue
            silence_ms += FRAME_MS
            done = False
            if silence_ms >= EARLY_CHECK_MS and checked is False:
                checked = 1
                audio = np.concatenate(seg)
                prob = e.turn(audio)
                text = e.asr(audio, self.asr_engine)
                cached_text = text            # 静音判断时已识别过;之后对方没再出声就直接复用
                complete_text = bool(text) and not FILLERS_END.search(text) and not ONLY_FILLER.match(text)
                log(f"  · 静音 {silence_ms}ms,Smart Turn {prob:.2f},文本{'完整' if complete_text else '未完'}: {text}")
                if complete_text and (prob > 0.45 or text.rstrip().endswith("?")) and not self.expecting_digits(text):   # 调优A: 0.5→0.45 让第一道闸更常放行
                    done = True
                last_check = (prob, complete_text)
            elif silence_ms >= SECOND_CHECK_MS and checked == 1:
                checked = 2
                prob, complete_text = last_check
                if self.expecting_digits():
                    log("  · 对方可能在报号码,多等一会儿")
                elif complete_text and prob > 0.1:
                    log(f"  · 静音 {silence_ms}ms,文本完整 → 判定说完")
                    done = True
            limit = DIGIT_SILENCE_MS if self.expecting_digits() else MAX_SILENCE_MS
            if silence_ms >= limit or len(seg) * FRAME_MS > MAX_UTT_S * 1000:
                done = True
            if done:
                t_end = time.time() - silence_ms / 1000
                audio = np.concatenate(seg)
                self._last_partial = ""
                in_speech = False
                speech_ms = silence_ms = 0
                pre = []
                if len(seg) * FRAME_MS - silence_ms < MIN_SPEECH_MS:
                    continue
                if checked is not False and cached_text is not None:
                    text = cached_text        # 判断说完时识别过且之后没有新的语音:不再重复识别(Whisper 一次约 1.6s)
                else:
                    text = e.asr(audio, self.asr_engine)
                cached_text = None
                if not text:
                    continue
                if not re.search(r"\w", text):
                    continue   # 只有标点(如 ".")
                if looks_garbled(text):
                    log(f"  (识别结果像乱码/重复,当作没听清: {text[:60]}…)")
                    text = "[unclear audio — speech recognition could not understand the caller]"
                if ONLY_FILLER.match(text):
                    log(f"  (只有语气词,不回复: {text})")
                    continue
                opened = True
                log(f"[对方] {text}")
                self.rec["turns"].append({"role": "caller", "text": text})
                if self.ai_active():
                    self.barge_in()
                    if self.reply_thread:
                        self.reply_thread.join(timeout=2)
                self.reply_thread = threading.Thread(target=self.respond, args=(text, t_end), daemon=True)
                self.reply_thread.start()
        self.reply_cancel.set()
        e.player.stop()
        os.makedirs(CALLS_DIR, exist_ok=True)
        fn = f"{CALLS_DIR}/{time.strftime('%Y%m%d_%H%M%S')}_{(self.info.get('number') or 'unknown').strip('+')}.json"
        self.rec["end"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.rec["end_reason"] = self.rec["end_reason"] or "caller_hangup"
        if self.mode == "outbound":
            json.dump(self.rec, open(fn, "w"), ensure_ascii=False, indent=2)
            log(f"== 外呼结束({self.rec['end_reason']}),记录:", fn)
            if not self.rec["result"]:
                said = " / ".join(f"{'对方' if t['role'] == 'caller' else 'AI'}: {t['text']}" for t in self.rec["turns"] if t.get("text"))[-600:]
                notify(f"📤 外呼结束,但没有报告结果({(self.task or {}).get('to_name') or ''})",
                       f"任务: {(self.task or {}).get('goal', '')}\n对话: {said}")
            self.outcome = self.rec["result"] or {"status": "unknown"}
            return
        try:
            ex = extract_call(self.rec)
            self.rec["extracted"] = ex
            self.rec["category"] = ex.get("category") or self.rec["category"]
            if ex.get("summary"):
                self.rec["summary_zh"] = ex["summary"]
        except Exception as err:
            ex = {}
            log("通话信息抽取失败:", repr(err))
        json.dump(self.rec, open(fn, "w"), ensure_ascii=False, indent=2)
        cat = self.rec["category"]
        log(f"== 对话结束({CATEGORY_ZH.get(cat, cat or '未分类')},{self.rec['end_reason']}),记录:", fn)
        if self.rec["summary"]:
            log("   摘要:", self.rec["summary"])
        # 推销/诈骗只记录不打扰;其他类型若通话中没调 take_message,用通话后抽取的信息补发通知
        if cat not in ("sales", "scam") and not self.rec["messages"]:
            urgent = ex.get("urgency") == "high"
            who = (ex.get("caller_name") or "未知来电者") + (f"({ex['company']})" if ex.get("company") else "")
            body = f"{who}\n事由: {ex.get('reason') or '未说明'}"
            if ex.get("needs_callback"):
                body += f"\n回电: {ex.get('callback_number') or self.info.get('number_raw') or ''}" + (f",{ex['best_time']}" if ex.get("best_time") else "")
            if ex.get("summary"):
                body += f"\n摘要: {ex['summary']}"
            notify(("🚨 紧急来电 " if urgent else "📞 来电 ") + f"({self.caller['channel_zh']}) {self.caller['name']}({CATEGORY_ZH.get(cat, cat or '未分类')})",
                   body, image=self.info.get("ring_screenshot"))


class MLXWorker:
    """MLX 的计算流是线程私有的:模型在哪个线程加载就只能在哪个线程用。
    所以识别和合成都放进这一个专用线程,其他线程提交任务、等结果(也避免两个模型同时抢 GPU)。"""

    def __init__(self):
        from concurrent.futures import ThreadPoolExecutor
        self.ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")
        self.ex.submit(self._load).result()

    def _load(self):
        self._asr = ASR()
        self._whisper = WhisperASR()
        self._whisper(np.zeros(SR, dtype=np.float32) + 1e-4)   # 预热
        self._tts = CloneTTS() if VOICE_MODE == "clone" else TTS()
        if VOICE_MODE != "clone":
            self._tts("Warm up.")
        self._asr(np.zeros(SR, dtype=np.float32))

    def asr(self, audio, engine="parakeet"):
        fn = self._whisper if engine == "whisper" else self._asr
        return self.ex.submit(fn, audio).result()

    def tts(self, text):
        return self.ex.submit(self._tts, text).result()

    def tts_stream(self, text, cancel):
        """在 MLX 线程里流式合成,音频块通过队列交给调用方;None 表示结束"""
        q = queue.Queue()

        def job():
            try:
                if hasattr(self._tts, "stream"):
                    for ch in self._tts.stream(text, cancel):
                        q.put(ch)
                else:
                    q.put(self._tts(text))
            finally:
                q.put(None)
        self.ex.submit(job)
        while True:
            ch = q.get()
            if ch is None:
                return
            yield ch


class Engine:
    def __init__(self):
        t = time.time()
        self.vad = VAD()
        self.turn = TurnDetector()
        self.mlx = MLXWorker()
        self.sensevoice = SenseVoiceASR()
        self.sensevoice(np.zeros(SR, dtype=np.float32) + 1e-4)
        self.tts = self.mlx.tts
        self.tts_stream = self.mlx.tts_stream
        self.player = Player()
        self.mic = Mic()
        log(f"引擎就绪({time.time()-t:.1f}s):输入「{IN_DEV}」 输出「{OUT_DEV}」 模型 {LLM_MODEL}")
        self.mic_selfcheck()

    def mic_selfcheck(self):
        """启动自检:没有麦克风权限时 macOS 给的是纯数字静音(约 -91 dB),正常线路底噪约 -55~-70 dB"""
        self.mic.drain()
        self.mic.muted = False
        time.sleep(1.5)
        fr = []
        while not self.mic.q.empty():
            fr.append(self.mic.q.get())
        self.mic.muted = True
        if not fr:
            log("⚠️ 麦克风自检:没有收到任何音频帧")
            return
        x = np.concatenate(fr)
        db = 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)
        if db < -85:
            log(f"⚠️ 麦克风自检:{db:.1f} dB,是纯静音,多半没有麦克风权限(系统设置 → 隐私与安全性 → 麦克风)")
        else:
            log(f"麦克风自检正常:{db:.1f} dB")

    def asr(self, audio, engine="parakeet"):
        """parakeet=英文(MLX) / sensevoice=中英自动(CPU) / auto=SenseVoice 判断语言,英文改用 Parakeet / whisper=中英自动(MLX,较慢,备用)"""
        if engine == "sensevoice":
            return self.sensevoice(audio)
        if engine == "auto":
            # 先用 SenseVoice 识别并判断语言(约 0.1s);英文再交给英文更准的 Parakeet(多约 0.2s)
            text = self.sensevoice(audio)
            if self.sensevoice.last_lang == "en":
                return self.mlx.asr(audio, "parakeet")
            return text
        return self.mlx.asr(audio, engine)


def ring_screenshot(info):
    """来电响铃时截一张屏(免会话接口,不影响 callctl 的 WDA 会话),存进通话记录目录"""
    import base64
    try:
        wda = open(os.path.expanduser("~/.wda_url_13")).read().strip()
        png = base64.b64decode(json.load(urllib.request.urlopen(wda + "/screenshot", timeout=20))["value"])
        os.makedirs(CALLS_DIR, exist_ok=True)
        fn = f"{CALLS_DIR}/{time.strftime('%Y%m%d_%H%M%S')}_ring.png"
        open(fn, "wb").write(png)
        info["ring_screenshot"] = fn
        log("  📸 来电截屏:", fn)
    except Exception as ex:
        log("来电截屏失败:", repr(ex))


def ctl(action, body=None):
    try:
        data = json.dumps(body).encode() if body else b""
        r = urllib.request.Request(f"{CTL}/{action}", data=data, method="POST",
                                   headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(r, timeout=30))
    except Exception as ex:
        return {"ok": False, "error": repr(ex)}


TASKS = f"{HERE}/tasks"


def next_task():
    """取 tasks/pending/ 里最早的任务(json),挪到 running/"""
    try:
        files = sorted(os.listdir(f"{TASKS}/pending"))
    except FileNotFoundError:
        return None
    for fn in files:
        if not fn.endswith(".json"):
            continue
        src, dst = f"{TASKS}/pending/{fn}", f"{TASKS}/running/{fn}"
        os.rename(src, dst)
        t = json.load(open(dst))
        t.setdefault("id", fn[:-5])
        t["_file"] = dst
        return t
    return None


def finish_task(task, outcome):
    task["outcome"] = outcome
    task["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    dst = f"{TASKS}/done/{os.path.basename(task['_file'])}"
    json.dump({k: v for k, v in task.items() if k != "_file"}, open(dst, "w"), ensure_ascii=False, indent=2)
    try:
        os.remove(task["_file"])
    except OSError:
        pass
    log(f"外呼任务 {task['id']} 结束: {outcome.get('status')}")


def warmup(eng):
    """外呼拨号后趁振铃预热 LLM/TTS/ASR,接通后首次应答更快。都不出声。"""
    t0 = time.time()
    try:
        for _ in llm_stream([{"role": "user", "content": "hi"}], threading.Event(),
                            system="Reply with just: ok", tools=[]):
            pass
    except Exception as e:
        log("  预热 LLM 跳过:", e)
    try:
        eng.tts("Hello, warming up.")          # 合成不播放,热 TTS 管线
    except Exception as e:
        log("  预热 TTS 跳过:", e)
    try:
        eng.asr(np.zeros(8000, dtype=np.float32), "auto")   # 热 ASR
    except Exception:
        pass
    log(f"  🔥 外呼预热完成({time.time() - t0:.1f}s)")


def main():
    eng = Engine()
    if "--now" in sys.argv:
        stop = threading.Event()
        try:
            Session(eng, {}).run(stop)
        except KeyboardInterrupt:
            stop.set()
        return
    f = open(EVENTS)
    f.seek(0, 2)
    stop = threading.Event()
    th = None
    info = {}
    task = None            # 正在执行的外呼任务
    task_sess = None
    task_t0 = 0.0
    log("等待来电…(外呼任务放进 tasks/pending/ 即自动执行)")
    while True:
        line = f.readline()
        if not line:
            # 空闲时取下一个外呼任务
            idle = (th is None or not th.is_alive())
            if task and task_sess and idle:
                finish_task(task, task_sess.outcome if hasattr(task_sess, "outcome") else {"status": "unknown"})
                task, task_sess = None, None
            elif task and not task_sess and time.time() - task_t0 > 90:
                finish_task(task, {"status": "no_answer"})
                notify(f"📤 外呼未接通({task.get('to_name') or task.get('to')})", f"任务: {task.get('goal', '')}")
                task = None
            elif not task and idle:
                task = next_task()
                if task:
                    log(f"开始外呼任务 {task['id']}: {task.get('to_name') or ''} {task['to']} | {task.get('goal', '')}")
                    r = ctl("dial", {"number": task["to"]})
                    log("拨号:", r)
                    task_t0 = time.time()
                    if r.get("ok"):
                        threading.Thread(target=warmup, args=(eng,), daemon=True).start()
                    if not r.get("ok"):
                        finish_task(task, {"status": "dial_failed", "error": r.get("error")})
                        notify("📤 外呼拨号失败", f"{task.get('to')}: {r.get('error')}")
                        task = None
            time.sleep(0.1)
            continue
        ev = json.loads(line)
        kind = ev.get("event")
        if kind == "answered" and task and not task_sess:
            stop = threading.Event()
            task_sess = Session(eng, {"number": task["to"], "number_raw": task["to"], "location": None},
                                mode="outbound", task=task)
            th = threading.Thread(target=task_sess.run, args=(stop,), daemon=True)
            th.start()
            continue
        if kind == "incoming":
            info = ev
            c = parse_caller(ev)
            log(f"来电({c['channel_zh']})", c["name"], ev.get("location") or "")
            ring_screenshot(info)
            if AUTO_ANSWER:
                log("接听:", ctl("answer"))
        elif kind == "connected" and (th is None or not th.is_alive()):
            stop = threading.Event()
            th = threading.Thread(target=Session(eng, dict(info)).run, args=(stop,), daemon=True)
            th.start()
        elif kind in ("ended", "missed", "declined"):
            stop.set()
            if task and not task_sess and kind == "ended":
                finish_task(task, {"status": "no_answer"})
                notify(f"📤 外呼未接通({task.get('to_name') or task.get('to')})", f"任务: {task.get('goal', '')}")
                task = None


if __name__ == "__main__":
    main()
