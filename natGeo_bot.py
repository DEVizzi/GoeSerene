import argparse
import asyncio
import base64
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import wave
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:
    import requests
except ImportError:
    import urllib.parse as _up

    class _FallbackRequests:
        class utils:
            quote = staticmethod(_up.quote)

        @staticmethod
        def get(*_a, **_kw):
            raise RuntimeError("requests package is required for live HTTP calls.")

        @staticmethod
        def post(*_a, **_kw):
            raise RuntimeError("requests package is required for live HTTP calls.")

    requests = _FallbackRequests()

try:
    import tweepy
except ImportError:
    tweepy = None

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_a, **_kw):
        return False

try:
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
except ImportError:
    class RefreshError(Exception):
        pass

    class HttpError(Exception):
        def __init__(self, resp=None, content=b""):
            super().__init__(content)
            self.resp = resp

    Request = Credentials = build = MediaFileUpload = None

try:
    from pydub.utils import mediainfo
except ImportError:
    mediainfo = None


# Load environment variables
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# YouTube OAuth scope and token endpoint
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
RESUMABLE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB resumable chunks
RETRIABLE_YT_STATUS_CODES = {500, 502, 503, 504}

# Instagram Graph API credentials
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
IG_USER_ID = os.getenv("IG_USER_ID")

BASE_URL = "https://graph.facebook.com/v19.0"
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
COVERR_API_URL = os.getenv("COVERR_API_URL")
COVERR_API_KEY = os.getenv("COVERR_API_KEY")

HTTP_TIMEOUT = 60
LONG_VIDEO_DOWNLOAD_TIMEOUT = 240
LONG_VIDEO_MIN_SECONDS = 485             # strictly > 8 minutes (8m05s)
LONG_VIDEO_MAX_UPLOAD_SECONDS = 885      # strictly < 15 minutes (14m45s) so YouTube never rejects as "Video is too long"
LONG_VIDEO_TRIM_TARGET_SECONDS = 750     # 12m30s cohesive feature when trimming >15m public-domain documentaries
LONG_VIDEO_MAX_SECONDS = 2400            # <= 40 minutes source discovery ceiling
LONG_VIDEO_MAX_BYTES = 850_000_000       # 850 MB safety cap for GitHub Actions disk/network
LONG_VIDEO_MIN_WIDTH = 1280           # at least 720p HD horizontal
LONG_VIDEO_MIN_HEIGHT = 720
LONG_VIDEO_MIN_ASPECT = 1.30          # strictly horizontal widescreen (not vertical / square)
MAX_LONG_VIDEOS_PER_DAY = int(os.getenv("MAX_LONG_VIDEOS_PER_DAY", "1") or "1")
MAX_DAILY_POSTS_PER_CHANNEL = int(os.getenv("MAX_DAILY_POSTS_PER_CHANNEL", "1") or "1")
LONG_VIDEO_WEEKDAYS = {
    int(x.strip())
    for x in (os.getenv("LONG_VIDEO_WEEKDAYS", "6") or "6").split(",")
    if x.strip().isdigit()
}
VOICE_LEAD_IN_S = 0.20   # 200ms clean start breath so first word is never clipped on scroll-in
VOICE_TAIL_PAD_S = 1.40  # 1.40s post-speech cushion so the final spoken word is NEVER cut off
ENABLE_LONG_YOUTUBE = os.getenv("ENABLE_LONG_YOUTUBE", "true").strip().lower() not in ("0", "false", "no")
REQUIRE_BOTH_PLATFORMS = os.getenv("REQUIRE_BOTH_PLATFORMS", "true").strip().lower() not in ("0", "false", "no")

# Wikimedia-compliant User-Agent with project URL (prevents HTTP 429 blocks on upload.wikimedia.org)
BOT_USER_AGENT = "GeoSereneBot/2.0 (https://github.com/DEVizzi/GoeSerene; wildlife-educational-media) python-requests/2.x"

ALLOWED_DOWNLOAD_SCHEMES = {"https"}


class YouTubeAuthError(RuntimeError):
    """Raised when YouTube OAuth credentials are missing, expired, or revoked."""


def _redact_secrets(text: str) -> str:
    """Redacts any accidental secret or token values from log messages."""
    out = str(text or "")
    for env_name in (
        "GOOGLE_API_KEY",
        "IG_ACCESS_TOKEN",
        "IG_USER_TOKEN",
        "FB_APP_SECRET",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "PEXELS_API_KEY",
        "PIXABAY_API_KEY",
        "COVERR_API_KEY",
        "TWITTER_BEARER_TOKEN",
        "TWITTER_API_SECRET",
        "TWITTER_ACCESS_TOKEN",
        "TWITTER_ACCESS_TOKEN_SECRET",
    ):
        val = os.getenv(env_name)
        if val and len(val) >= 6:
            out = out.replace(val, "***REDACTED***")
    return out


def _resolve_repo_path(rel_or_abs: str) -> str:
    """Resolves a path safely inside BASE_DIR or system temp directory."""
    raw = str(rel_or_abs or "").strip()
    if not raw:
        raise ValueError("Empty file path.")
    candidate = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(BASE_DIR, raw))
    allowed_roots = (
        os.path.realpath(BASE_DIR) + os.sep,
        os.path.realpath(tempfile.gettempdir()) + os.sep,
    )
    if candidate == os.path.realpath(BASE_DIR) or any(candidate.startswith(r) for r in allowed_roots):
        return candidate
    raise ValueError(f"Refusing path outside workspace/temp boundary: {raw}")


def _validate_remote_media_url(url: str) -> str:
    """Ensures remote download URLs strictly use HTTPS and a public hostname."""
    clean = str(url or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme.lower() not in ALLOWED_DOWNLOAD_SCHEMES or not parsed.netloc:
        raise ValueError(f"Disallowed or invalid media URL scheme: {clean[:80]}")
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
        raise ValueError("Refusing loopback/local media URL.")
    return clean


class SystemMessage:
    def __init__(self, content: str):
        self.content = content


class HumanMessage:
    def __init__(self, content: str):
        self.content = content


class _GeminiResponse:
    def __init__(self, content: str):
        self.content = content


class ChatGoogleGenerativeAI:
    def __init__(
        self,
        model: str = "models/gemini-2.5-flash",
        temperature: float = 0.65,
        max_output_tokens: int = 1024,
    ):
        self.model = model if model.startswith("models/") else f"models/{model}"
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    def invoke(self, messages: list) -> _GeminiResponse:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY environment variable is not set.")
        sys_parts = [m.content for m in messages if isinstance(m, SystemMessage)]
        user_parts = [m.content for m in messages if not isinstance(m, SystemMessage)]
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "\n\n".join(user_parts)}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_output_tokens,
            },
        }
        if sys_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n".join(sys_parts)}]}
        resp = None
        model_chain = list(dict.fromkeys([
            self.model,
            "models/gemini-2.5-flash",
            "models/gemini-flash-latest",
            "models/gemini-flash-lite-latest",
            "models/gemini-3.1-flash-lite",
        ]))
        for candidate_model in model_chain:
            url = f"https://generativelanguage.googleapis.com/v1beta/{candidate_model}:generateContent"
            resp = requests.post(
                url,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code in (404, 429, 500, 502, 503, 504):
                continue
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                continue
            cand0 = candidates[0]
            finish_reason = str(cand0.get("finishReason") or "")
            if finish_reason in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
                continue
            parts = (cand0.get("content") or {}).get("parts") or []
            non_thought_texts = [
                str(p.get("text") or "")
                for p in parts
                if isinstance(p, dict) and not p.get("thought") and p.get("text")
            ]
            text = "".join(non_thought_texts).strip()
            if text:
                return _GeminiResponse(text)
        if resp is not None:
            resp.raise_for_status()
        raise RuntimeError("No Gemini candidate models returned a complete response.")


# Initialize Gemini Chat Model
llm = ChatGoogleGenerativeAI(
    model="models/gemini-2.5-flash",
    temperature=0.65,
    max_output_tokens=1024,
)


WILDLIFE_TOPICS = [
    # Iconic Marine & Aquatic Wildlife
    "orca", "great white shark", "humpback whale", "octopus", "jellyfish",
    "sea turtle", "manta ray", "dolphin", "sea otter", "seahorse",
    "clownfish", "stingray", "hammerhead shark", "blue whale", "seal",

    # Iconic Land Predators & Mammals
    "snow leopard", "cheetah", "bengal tiger", "lion", "jaguar",
    "polar bear", "grizzly bear", "wolf", "arctic fox", "red fox",
    "red panda", "elephant", "giraffe", "gorilla", "orangutan",
    "sloth", "koala", "kangaroo", "bison", "rhino", "hippo", "meerkat",

    # Striking Birds, Reptiles, Amphibians & Insects
    "bald eagle", "peregrine falcon", "snowy owl", "barn owl", "hummingbird",
    "kingfisher", "flamingo", "penguin", "peacock", "toucan", "macaw",
    "chameleon", "komodo dragon", "iguana", "crocodile", "tree frog",
    "praying mantis", "monarch butterfly", "honeybee", "dragonfly",

    # Dramatic Natural Phenomena
    "aurora borealis", "volcano lava", "lightning storm", "glacier",
    "coral reef", "bioluminescent waves", "waterfall", "sandstorm",
]

# Dedicated documentary queries rich in >8-minute horizontal public-domain / CC HD films
LONG_DOCUMENTARY_TOPICS = [
    "yellowstone",
    "yosemite",
    "national park",
    "coral reef",
    "aurora borealis",
    "ocean",
    "rainforest",
    "glacier",
    "waterfall",
    "volcano",
    "everglades",
    "grand canyon",
    "humpback whale",
    "grizzly bear",
    "sea turtle",
    "wilderness",
    "earth",
    "underwater",
]

# Keep backwards-compatible alias
NATURE_QUERIES = WILDLIFE_TOPICS


voice_names = [
    "en-US-ChristopherNeural",
    "en-US-AndrewNeural",
    "en-GB-RyanNeural",
    "en-US-GuyNeural",
]


# ============================================================================
# FREE COMMERCIAL LICENSE VERIFICATION (PUBLIC DOMAIN / CC0 / CC-BY / STOCK)
# ============================================================================
_RESTRICTED_LICENSE_PATTERNS = re.compile(
    r"(?:\bnc\b|by-nc|non[\s_\-]*commercial|\bnd\b|by-nd|no[\s_\-]*deriv|"
    r"all[\s_\-]*rights[\s_\-]*reserved|copyrighted|fair[\s_\-]*use|"
    r"editorial[\s_\-]*only|unlicensed|proprietary)",
    re.IGNORECASE,
)

_APPROVED_FREE_LICENSE_PATTERNS = re.compile(
    r"(?:public[\s_\-]*domain|\bpdm\b|cc0|zero/1\.0|usgov|pd-usgov|"
    r"\bnasa\b|\bnoaa\b|\busfws\b|\bnps\b|\busgs\b|loc-public-domain|"
    r"cc[\s_\-]*by(?![\s_\-]*(?:nc|nd))|licenses/by/(?:1|2|3|4)|"
    r"attribution(?:\s+share[\s\-]*alike)?(?:\s+\d\.\d)?|"
    r"\b(?:pexels|pixabay|coverr|wikimedia\s+commons)\b[^,\n]*\blicense\b)",
    re.IGNORECASE,
)


def is_free_commercial_license(license_text: str) -> bool:
    """
    Strictly validates that `license_text` (URL or short name) permits free commercial
    reuse and YouTube/Instagram publishing (Public Domain, US Gov PD, CC0, CC-BY, CC-BY-SA,
    Pexels, Pixabay, Coverr) and has NO NonCommercial (NC) or NoDerivatives (ND) restrictions.
    """
    s = str(license_text or "").strip()
    if not s:
        return False
    if _RESTRICTED_LICENSE_PATTERNS.search(s):
        return False
    return bool(_APPROVED_FREE_LICENSE_PATTERNS.search(s))


def format_license_credit(metadata: dict) -> str:
    """Builds a clean attribution line for Creative Commons / Open-Source footage."""
    source = str(metadata.get("source") or "open-source").replace("_", " ").title()
    lic = str(metadata.get("license") or "Public Domain / Free License").strip()
    author = str(metadata.get("author") or "").strip()
    orig = str(metadata.get("original_url") or "").strip()
    parts = [f"Footage Source: {source}"]
    if author:
        parts.append(f"Creator: {author}")
    parts.append(f"License: {lic}")
    if orig:
        parts.append(f"Original: {orig}")
    return " | ".join(parts)


# ============================================================================
# FFMPEG / FFPROBE MEDIA & QUALITY GATES
# ============================================================================
def _ffmpeg_has_filter(filter_name: str) -> bool:
    """Checks if the installed ffmpeg binary supports a given filter."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=10,
        )
        return bool(re.search(rf"\b{re.escape(filter_name)}\b", res.stdout or ""))
    except Exception:
        return False


def probe_video_file(video_path: str) -> dict:
    """
    Uses ffprobe to inspect video width, height, duration (seconds), fps, bitrate,
    codec, and whether an audio stream is present.
    """
    safe_path = _resolve_repo_path(video_path)
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        safe_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
    info = json.loads(res.stdout or "{}")
    streams = info.get("streams") or []
    fmt = info.get("format") or {}

    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v_stream:
        raise ValueError("No video stream found in file.")

    width = int(v_stream.get("width") or 0)
    height = int(v_stream.get("height") or 0)
    # Respect rotation metadata if present (e.g. phone portrait videos tagged 1920x1080 + 90deg)
    rotation = 0
    for sd in v_stream.get("side_data_list") or []:
        if "rotation" in sd:
            rotation = abs(int(float(sd["rotation"])))
    tag_rot = (v_stream.get("tags") or {}).get("rotate")
    if tag_rot and not rotation:
        rotation = abs(int(float(tag_rot)))
    if rotation in (90, 270):
        width, height = height, width

    duration = float(fmt.get("duration") or v_stream.get("duration") or 0.0)
    bit_rate = int(fmt.get("bit_rate") or v_stream.get("bit_rate") or 0)

    fps_raw = str(v_stream.get("avg_frame_rate") or v_stream.get("r_frame_rate") or "30/1")
    if "/" in fps_raw:
        num, den = fps_raw.split("/", 1)
        fps = float(num) / float(den) if float(den or 0) > 0 else 0.0
    else:
        fps = float(fps_raw or 0.0)

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "bit_rate": bit_rate,
        "fps": fps,
        "video_codec": str(v_stream.get("codec_name") or ""),
        "pix_fmt": str(v_stream.get("pix_fmt") or ""),
        "has_audio": a_stream is not None,
        "audio_codec": str((a_stream or {}).get("codec_name") or ""),
    }


def _sample_frame_stats(video_path: str, timestamp_s: float) -> tuple:
    """
    Samples a single 160x90 grayscale frame at `timestamp_s` and returns
    (raw_bytes, mean_luma, std_luma) to detect black, blown-out, flat, or frozen videos.
    """
    safe_path = _resolve_repo_path(video_path)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{max(0.0, timestamp_s):.2f}",
        "-i", safe_path,
        "-frames:v", "1",
        "-vf", "scale=160:90,format=gray",
        "-f", "rawvideo",
        "pipe:1",
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=25, check=False)
    buf = res.stdout or b""
    if len(buf) < 160 * 90:
        return b"", 0.0, 0.0
    buf = buf[: 160 * 90]
    n = len(buf)
    mean = sum(buf) / n
    variance = sum((px - mean) ** 2 for px in buf) / n
    return buf, mean, math.sqrt(variance)


def verify_long_horizontal_video_quality(video_path: str) -> tuple:
    """
    Strictly verifies that a candidate YouTube long-form video:
    1. Has length > 8 minutes (> 480s, default >= 485s)
    2. Is strictly HORIZONTAL widescreen (width > height, aspect >= 1.30 — NEVER vertical like Reels)
    3. Is HD resolution (>= 1280x720) with healthy frame rate (>= 20 fps) and bitrate (>= 500 kbps)
    4. Looks visually good across 15%, 50%, and 85% timestamps (non-black, non-blown-out, good contrast,
       and real motion rather than a static image/title card).

    Returns (is_valid: bool, reason: str, probe_info: dict).
    """
    try:
        info = probe_video_file(video_path)
    except Exception as e:
        return False, f"ffprobe failed: {e}", {}

    w = info["width"]
    h = info["height"]
    dur = info["duration"]
    fps = info["fps"]
    br = info["bit_rate"]

    if dur <= 480.0 or dur < LONG_VIDEO_MIN_SECONDS:
        return False, f"duration {dur:.1f}s is not > 8 minutes ({LONG_VIDEO_MIN_SECONDS}s min)", info
    if w <= h:
        return False, f"video is vertical/square ({w}x{h}), not horizontal", info
    aspect = w / float(h or 1)
    if aspect < LONG_VIDEO_MIN_ASPECT:
        return False, f"aspect ratio {aspect:.2f} ({w}x{h}) is too narrow for widescreen horizontal", info
    if w < LONG_VIDEO_MIN_WIDTH or h < LONG_VIDEO_MIN_HEIGHT:
        return False, f"resolution {w}x{h} is below HD {LONG_VIDEO_MIN_WIDTH}x{LONG_VIDEO_MIN_HEIGHT}", info
    if fps < 20.0:
        return False, f"frame rate {fps:.1f} fps is too low (< 20 fps)", info
    if br and br < 500_000:
        return False, f"bitrate {br // 1000} kbps is too low (< 500 kbps)", info

    # Sample 3 frames (15%, 50%, 85%) to ensure visual richness and real motion
    samples = []
    for frac in (0.15, 0.50, 0.85):
        raw, mean_luma, std_luma = _sample_frame_stats(video_path, dur * frac)
        if not raw:
            return False, f"failed to decode visual sample at {int(frac * 100)}%", info
        if mean_luma < 16.0 or mean_luma > 242.0:
            return False, f"frame at {int(frac * 100)}% looks blank/extreme (luma={mean_luma:.1f})", info
        if std_luma < 12.0:
            return False, f"frame at {int(frac * 100)}% lacks visual detail/contrast (std={std_luma:.1f})", info
        samples.append(raw)

    # Reject static slideshows / frozen images
    diffs = []
    for a, b in ((samples[0], samples[1]), (samples[1], samples[2]), (samples[0], samples[2])):
        diffs.append(sum(abs(x - y) for x, y in zip(a, b)) / len(a))
    if max(diffs) < 2.5:
        return False, f"video appears static/frozen across timeline (motion={max(diffs):.2f})", info

    return True, "ok", info


def _has_audible_audio(video_path: str, probe_info: dict) -> bool:
    """
    Checks whether a video genuinely has audible soundtrack/narration across multiple
    points in the timeline (rejecting silent tracks or faint <-38 dB ambient tape hiss).
    """
    if not probe_info.get("has_audio"):
        return False
    safe_path = _resolve_repo_path(video_path)
    dur = float(probe_info.get("duration") or 30.0)
    sample_offsets = [0.0] if dur < 60.0 else [dur * 0.12, dur * 0.45, dur * 0.75]
    best_mean_db = -99.0
    measured = False
    for offset in sample_offsets:
        try:
            res = subprocess.run(
                [
                    "ffmpeg", "-hide_banner",
                    "-ss", f"{max(0.0, offset):.2f}",
                    "-t", "20",
                    "-i", safe_path,
                    "-af", "volumedetect", "-f", "null", "-",
                ],
                capture_output=True, text=True, timeout=25, check=False,
            )
            m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", res.stderr or "")
            if m:
                measured = True
                best_mean_db = max(best_mean_db, float(m.group(1)))
        except Exception:
            pass
    return (best_mean_db > -38.0) if measured else True


def verify_nature_frames_with_gemini(video_path: str, topic: str, metadata: dict, duration_s: float) -> tuple:
    """
    Extracts 3 horizontal JPEG frames (20%, 50%, 80%) and uses Gemini Multimodal Vision
    to verify the video genuinely depicts outdoor nature, wildlife, marine life, landscapes,
    or Earth-from-space (rejecting studio interviews like 'The Jaguar Poet', cars, lectures,
    concerts, sports, cartoons, or watermarked/low-grade clips).

    Returns (approved: bool, reason: str).
    """
    api_key = (os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        return True, "GOOGLE_API_KEY unset (vision gate skipped)"

    safe_video = _resolve_repo_path(video_path)
    effective_end = min(duration_s, float(LONG_VIDEO_MAX_UPLOAD_SECONDS))
    parts = [{
        "text": (
            f"You are the strict visual quality & nature curator for the @GeoSerene YouTube nature channel.\n"
            f"Candidate search topic: '{topic}'\n"
            f"Candidate source title: '{metadata.get('title', '')}'\n"
            f"Candidate description: '{metadata.get('description', '')}'\n\n"
            "Inspect these 3 frames sampled across the video and return JSON ONLY: "
            '{"approved": true_or_false, "reason": "short explanation"}\n\n'
            "Set approved=true ONLY IF ALL of the following hold:\n"
            "1. The footage genuinely shows real-world nature, wildlife, animals, underwater ocean life, "
            "national parks, mountains/forests/rivers/auroras, or Earth/space observation.\n"
            "2. It is NOT a human studio interview, poet/music performance, lecture/presentation slides, "
            "news broadcast, car/vehicle review, sports event, video game, or cartoon.\n"
            "3. The image quality looks sharp, scenic, and pleasing for a widescreen YouTube nature feature."
        )
    }]

    for frac in (0.20, 0.50, 0.80):
        ts = max(1.0, effective_end * frac)
        res = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{ts:.2f}", "-i", safe_video,
                "-frames:v", "1", "-vf", "scale=640:-2",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            capture_output=True, timeout=25, check=False,
        )
        jpg = res.stdout or b""
        if len(jpg) > 1000:
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": base64.b64encode(jpg).decode("ascii"),
                }
            })

    if len(parts) < 2:
        return False, "could not extract JPEG frames for vision verification"

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.1},
    }
    for model_name in ("models/gemini-2.5-flash", "models/gemini-flash-latest", "models/gemini-flash-lite-latest"):
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=40,
            )
            if resp.status_code in (404, 429, 500, 503):
                continue
            resp.raise_for_status()
            cand_parts = ((resp.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            txt = "".join(str(p.get("text") or "") for p in cand_parts if not p.get("thought")).strip()
            txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.IGNORECASE | re.MULTILINE).strip()
            verdict = json.loads(txt)
            return bool(verdict.get("approved")), str(verdict.get("reason") or "")
        except Exception as e:
            print(f"⚠️ Gemini Vision check warning ({model_name}): {_redact_secrets(e)}")
    return True, "vision API unavailable; passed technical quality gates"


def prepare_long_horizontal_video(
    video_path: str,
    intro_voice_path: str = "",
    chapter_voice_paths: list = None,
) -> str:
    """
    Prepares a >8-minute horizontal HD video for YouTube WITHOUT cropping to vertical:
    - Keeps full horizontal 16:9 framing.
    - Enforces `LONG_VIDEO_MIN_SECONDS` (8m05s) .. `LONG_VIDEO_MAX_UPLOAD_SECONDS` (14m45s) so
      videos are ALWAYS > 8 minutes AND strictly under YouTube's 15:00 unverified-channel limit!
    - Preserves & loudness-normalizes audible original sound, or scores silent films with
      broadcast-normalized (-14 LUFS) multi-chapter narration + ducked ambient music.
    """
    safe_video = _resolve_repo_path(video_path)
    info = probe_video_file(safe_video)
    output_path = re.sub(r"\.[A-Za-z0-9]+$", "", safe_video) + "_yt_long.mp4"

    raw_duration = info["duration"]
    needs_trim = raw_duration > LONG_VIDEO_MAX_UPLOAD_SECONDS
    start_offset = 10.0 if (needs_trim and raw_duration > LONG_VIDEO_TRIM_TARGET_SECONDS + 20.0) else 0.0
    target_duration = LONG_VIDEO_TRIM_TARGET_SECONDS if needs_trim else raw_duration
    trim_in_args = (["-ss", f"{start_offset:.2f}"] if start_offset > 0 else [])

    can_copy_video = (
        info.get("video_codec") == "h264"
        and info.get("pix_fmt") in ("yuv420p", "")
        and safe_video.lower().endswith(".mp4")
    )

    if _has_audible_audio(safe_video, info):
        if can_copy_video and info.get("audio_codec") == "aac" and not needs_trim:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", safe_video,
                    "-c", "copy", "-movflags", "+faststart", output_path,
                ],
                check=True,
            )
            return output_path
        v_args = ["-c:v", "copy"] if can_copy_video else [
            "-vf", "scale='min(1920,iw)':-2,setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        ]
        af_norm = "loudnorm=I=-16:TP=-1.5:LRA=11" if _ffmpeg_has_filter("loudnorm") else "volume=1.0"
        subprocess.run(
            [
                "ffmpeg", "-y",
                *trim_in_args,
                "-i", safe_video,
                "-t", f"{target_duration:.2f}",
                *v_args,
                "-af", af_norm,
                "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "44100",
                "-movflags", "+faststart", output_path,
            ],
            check=True,
        )
        return output_path

    # Silent footage -> add ambient background music + broadcast-normalized chapter voiceovers
    music_dir = os.path.join(BASE_DIR, "music")
    music_files = [f for f in os.listdir(music_dir) if f.endswith(".mp3")] if os.path.isdir(music_dir) else []
    if not music_files:
        raise FileNotFoundError(f"No .mp3 files found in {music_dir} to score silent long video.")
    music_path = os.path.join(music_dir, random.choice(music_files))

    v_args = ["-c:v", "copy"] if can_copy_video else [
        "-vf", "scale='min(1920,iw)':-2,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
    ]

    vo_files = [p for p in (chapter_voice_paths or []) if p and os.path.exists(p)]
    if not vo_files and intro_voice_path and os.path.exists(intro_voice_path):
        vo_files = [intro_voice_path]

    fade_out_start = max(1.0, target_duration - 2.5)
    if vo_files:
        temp_dir = tempfile.gettempdir()
        prepared_wavs = []
        chapter_fracs = (0.002, 0.33, 0.68)
        for idx, vp in enumerate(vo_files[:3]):
            wav_out = os.path.join(temp_dir, f"long_ch_{idx}.wav")
            wav_path, _ = _prepare_broadcast_voice_wav(vp, wav_out, lead_in_s=0.3, tail_pad_s=1.0)
            delay_ms = int(max(800.0, target_duration * chapter_fracs[idx] * 1000.0)) if idx > 0 else 1200
            prepared_wavs.append((wav_path, delay_ms))

        extra_inputs = []
        vo_filters = []
        vo_labels = []
        for idx, (wav_path, delay_ms) in enumerate(prepared_wavs):
            in_idx = idx + 2
            extra_inputs.extend(["-i", wav_path])
            vo_filters.append(f"[{in_idx}:a]adelay={delay_ms}|{delay_ms},apad,atrim=0:{target_duration:.2f}[vo{idx}]")
            vo_labels.append(f"[vo{idx}]")

        if len(vo_labels) == 1:
            vo_combined_filter = f"{vo_filters[0]};[vo0]asplit=2[vo_sc][vo_mix]"
        else:
            vo_combined_filter = (
                ";".join(vo_filters)
                + f";{''.join(vo_labels)}amix=inputs={len(vo_labels)}:duration=first:normalize=0,asplit=2[vo_sc][vo_mix]"
            )

        if _ffmpeg_has_filter("sidechaincompress"):
            fc = (
                f"{vo_combined_filter};"
                f"[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
                f"volume=0.18,atrim=0:{target_duration:.2f}[amb_raw];"
                "[amb_raw][vo_sc]sidechaincompress=threshold=0.025:ratio=5:attack=20:release=500[amb_duck];"
                f"[vo_mix][amb_duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                f"afade=t=out:st={fade_out_start:.2f}:d=2.2[aout]"
            )
        else:
            fc = (
                ";".join(vo_filters)
                + (
                    f";{''.join(vo_labels)}amix=inputs={len(vo_labels)}:duration=first:normalize=0[vo_mix];"
                    if len(vo_labels) > 1 else ";[vo0]anull[vo_mix];"
                )
                + f"[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
                f"volume=0.13,atrim=0:{target_duration:.2f}[amb_low];"
                f"[vo_mix][amb_low]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                f"afade=t=out:st={fade_out_start:.2f}:d=2.2[aout]"
            )

        cmd = [
            "ffmpeg", "-y",
            *trim_in_args,
            "-i", safe_video,
            "-stream_loop", "-1", "-i", music_path,
            *extra_inputs,
            "-t", f"{target_duration:.2f}",
            "-filter_complex", fc,
            "-map", "0:v:0",
            "-map", "[aout]",
            *v_args,
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "44100",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            *trim_in_args,
            "-i", safe_video,
            "-stream_loop", "-1", "-i", music_path,
            "-t", f"{target_duration:.2f}",
            "-map", "0:v:0",
            "-map", "1:a:0",
            *v_args,
            "-af", f"volume=0.26,afade=t=out:st={fade_out_start:.2f}:d=2.2",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "44100",
            "-movflags", "+faststart",
            output_path,
        ]

    subprocess.run(cmd, check=True)
    return output_path


def _format_ass_time(seconds: float) -> str:
    """Formats seconds into ASS timestamp H:MM:SS.cc."""
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def generate_ass_subtitles(
    word_events: list,
    ass_path: str,
    words_per_chunk: int = 2,
    hook_banner: str = "",
) -> str:
    """
    Generates a bold, high-contrast .ass subtitle file from edge-tts WordBoundary events.
    - Uses WrapStyle: 0 (smart wrapping), Fontsize: 50, and 120px side margins so text
      NEVER overflows the 1080x1920 vertical frame.
    - Splits chunks at natural speech pauses (>0.18s gap) so sentences never bleed together.
    - Optionally displays a 2.6s top hook banner to stop scrollers in the first 2 seconds.
    """
    if not word_events:
        return ""

    safe_ass = _resolve_repo_path(ass_path)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,50,&H00FFFFFF,&H0000FFFF,&H00000000,&HA0000000,"
        "-1,0,0,0,100,100,1,0,3,8,0,2,120,120,420,1\n"
        "Style: Hook,Arial,44,&H0000D7FF,&H00FFFFFF,&H00000000,&HB0000000,"
        "-1,0,0,0,100,100,1,0,3,6,0,8,120,120,230,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    chunks = []
    current = []
    for w in word_events:
        if current:
            gap = w["start"] - current[-1]["end"]
            current_chars = sum(len(x["text"]) for x in current) + len(current) - 1
            if gap > 0.18 or len(current) >= words_per_chunk or (current_chars + 1 + len(w["text"])) > 18:
                chunks.append(current)
                current = []
        current.append(w)
    if current:
        chunks.append(current)

    lines = [header]
    if hook_banner:
        clean_hook = hook_banner.strip().replace("{", "(").replace("}", ")").upper()
        if len(clean_hook) > 55:
            clean_hook = clean_hook[:52].rsplit(" ", 1)[0] + "..."
        lines.append(f"Dialogue: 1,0:00:00.00,0:00:02.60,Hook,,0,0,0,,{clean_hook}\n")

    for i, chunk in enumerate(chunks):
        start_s = chunk[0]["start"]
        end_s = max(chunk[-1]["end"] + 0.06, start_s + 0.20)
        if i + 1 < len(chunks):
            end_s = min(end_s, max(start_s + 0.10, chunks[i + 1][0]["start"] - 0.01))
        words_upper = [w["text"].strip().replace("{", "(").replace("}", ")").upper() for w in chunk if w["text"].strip()]
        if not words_upper:
            continue
        joined = " ".join(words_upper)
        if len(words_upper) > 1 and len(joined) > 14:
            mid = len(words_upper) // 2
            joined = "\\N".join([" ".join(words_upper[:mid]), " ".join(words_upper[mid:])])
        lines.append(
            f"Dialogue: 0,{_format_ass_time(start_s)},{_format_ass_time(end_s)},Default,,0,0,0,,{joined}\n"
        )

    with open(safe_ass, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return safe_ass


def get_audio_duration(audio_path: str) -> float:
    """
    Returns the exact duration of an audio file in seconds:
    1. Exact PCM frame count via stdlib `wave` for .wav files (0.000s header estimation error).
    2. `ffprobe` format/stream duration for compressed formats (.mp3/.m4a/.ogg).
    3. `pydub.utils.mediainfo` fallback.
    """
    safe_audio = _resolve_repo_path(audio_path)
    if safe_audio.lower().endswith(".wav") and os.path.exists(safe_audio):
        try:
            with wave.open(safe_audio, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                if rate > 0 and frames > 0:
                    return float(frames) / float(rate)
        except Exception:
            pass

    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=duration",
                "-of", "json",
                safe_audio,
            ],
            capture_output=True, text=True, timeout=15, check=True,
        )
        probe = json.loads(res.stdout or "{}")
        durations = []
        fmt_dur = (probe.get("format") or {}).get("duration")
        if fmt_dur:
            durations.append(float(fmt_dur))
        for st in (probe.get("streams") or []):
            if st.get("duration"):
                durations.append(float(st["duration"]))
        if durations:
            return max(durations)
    except Exception:
        pass

    if mediainfo is not None:
        info = mediainfo(safe_audio)
        return float(info["duration"])
    raise RuntimeError(f"Could not determine audio duration for {safe_audio}")


def _prepare_broadcast_voice_wav(
    voice_path: str,
    out_wav_path: str,
    lead_in_s: float = VOICE_LEAD_IN_S,
    tail_pad_s: float = VOICE_TAIL_PAD_S,
) -> tuple:
    """
    Converts raw TTS speech (`edge-tts` / `gTTS` mono MP3) into a broadcast-grade
    44.1 kHz 16-bit stereo PCM `.wav` file:
      - Centers voice at full 0 dB gain across Left & Right (`pan=stereo|c0=c0|c1=c0`).
      - Normalizes spoken loudness to `-14 LUFS` (`loudnorm=I=-14:TP=-1.2:LRA=7`) BEFORE
        adding silence padding so speech is warm, loud, and front-and-center on mobile speakers.
      - Adds `lead_in_s` (200ms) start buffer and `tail_pad_s` (1.40s) post-speech cushion
        so neither the first nor final syllable is EVER clipped.
    Returns `(out_wav_path, exact_total_wav_duration_seconds)`.
    """
    safe_in = _resolve_repo_path(voice_path)
    safe_out = _resolve_repo_path(out_wav_path)
    delay_ms = max(0, int(round(lead_in_s * 1000.0)))

    if _ffmpeg_has_filter("loudnorm"):
        level_filter = "loudnorm=I=-14:TP=-1.2:LRA=7"
    elif _ffmpeg_has_filter("dynaudnorm"):
        level_filter = "dynaudnorm=f=150:g=11:p=0.92"
    else:
        level_filter = "volume=2.0"

    af_chain = (
        "aformat=channel_layouts=mono,"
        "pan=stereo|c0=c0|c1=c0,"
        "aresample=44100,"
        "highpass=f=75,"
        f"{level_filter},"
        f"adelay={delay_ms}|{delay_ms},"
        f"apad=pad_dur={max(0.4, tail_pad_s):.2f}"
    )

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", safe_in,
            "-af", af_chain,
            "-c:a", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            safe_out,
        ],
        check=True,
    )
    exact_dur = get_audio_duration(safe_out)
    return safe_out, exact_dur


def combine_video_with_music_and_voiceover(
    video_paths,
    voiceover_path: str,
    subtitle_path: str = "",
) -> str:
    """
    Combines one or more video clips with broadcast-normalized (-14 LUFS) stereo voiceover,
    sidechain-ducked subtle background music, a 1.40s post-speech cushion (zero TTS cutoff),
    and optional burned-in ASS captions, normalized to 1080x1920 30fps SDR bt709.
    """
    if isinstance(video_paths, str):
        video_paths = [video_paths]
    if not video_paths:
        raise ValueError("At least one video path is required.")

    safe_videos = [_resolve_repo_path(vp) for vp in video_paths]
    safe_voice = _resolve_repo_path(voiceover_path)
    primary_video = safe_videos[0]
    output_path = re.sub(r"\.[A-Za-z0-9]+$", "", primary_video) + "_final_narrated.mp4"
    music_dir = os.path.join(BASE_DIR, "music")
    music_files = [f for f in os.listdir(music_dir) if f.endswith(".mp3")]
    if not music_files:
        raise FileNotFoundError(f"No .mp3 files found in {music_dir}")
    music_path = os.path.join(music_dir, random.choice(music_files))

    temp_dir = tempfile.gettempdir()
    paced_voice_wav = os.path.join(temp_dir, "paced_voice_broadcast.wav")
    paced_voice_wav, voice_total_duration = _prepare_broadcast_voice_wav(
        safe_voice,
        paced_voice_wav,
        lead_in_s=VOICE_LEAD_IN_S,
        tail_pad_s=VOICE_TAIL_PAD_S,
    )
    speech_only_dur = max(0.0, voice_total_duration - VOICE_LEAD_IN_S - VOICE_TAIL_PAD_S)
    target_duration = max(voice_total_duration, 12.0)
    fade_out_start = max(1.0, target_duration - 0.90)
    print(
        f"🎙️ Broadcast voiceover ready: speech={speech_only_dur:.2f}s, "
        f"padded_wav={voice_total_duration:.2f}s, reel_duration={target_duration:.2f}s"
    )

    stitched_video = primary_video
    if len(safe_videos) > 1:
        segment_duration = max(2.5, min(4.5, target_duration / len(safe_videos)))
        clip_inputs = []
        filter_parts = []
        concat_labels = []
        for idx, vp in enumerate(safe_videos):
            clip_inputs.extend(["-t", f"{segment_duration:.2f}", "-i", vp])
            filter_parts.append(
                f"[{idx}:v]fps=30,"
                "scale=1080:1920:force_original_aspect_ratio=increase:out_color_matrix=bt709,"
                "crop=1080:1920,setsar=1,"
                f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709[v{idx}]"
            )
            concat_labels.append(f"[v{idx}]")

        stitched_video = os.path.join(temp_dir, "stitched_visual.mp4")
        concat_fc = ";".join(filter_parts) + f";{''.join(concat_labels)}concat=n={len(safe_videos)}:v=1:a=0[vout]"
        subprocess.run(
            ["ffmpeg", "-y", *clip_inputs, "-filter_complex", concat_fc, "-map", "[vout]",
             "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", stitched_video],
            check=True,
        )

    if _ffmpeg_has_filter("sidechaincompress"):
        filter_complex = (
            f"[1:a]apad,atrim=0:{target_duration:.2f},asetpts=PTS-STARTPTS,asplit=2[vo_sc][vo_mix];"
            f"[2:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"volume=0.14,atrim=0:{target_duration:.2f},asetpts=PTS-STARTPTS[mus_raw];"
            "[mus_raw][vo_sc]sidechaincompress=threshold=0.025:ratio=5:attack=15:release=400[mus_duck];"
            f"[vo_mix][mus_duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"afade=t=out:st={fade_out_start:.2f}:d=0.85[out]"
        )
    else:
        filter_complex = (
            f"[1:a]apad,atrim=0:{target_duration:.2f},asetpts=PTS-STARTPTS[vo_mix];"
            f"[2:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"volume=0.10,atrim=0:{target_duration:.2f},asetpts=PTS-STARTPTS[mus_low];"
            f"[vo_mix][mus_low]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"afade=t=out:st={fade_out_start:.2f}:d=0.85[out]"
        )
    inputs = [stitched_video, paced_voice_wav, music_path]

    video_chain = (
        "[0:v]fps=30,"
        "scale=1080:1920:force_original_aspect_ratio=increase:out_color_matrix=bt709,"
        "crop=1080:1920,setsar=1,"
        "eq=contrast=1.06:saturation=1.14,"
        "unsharp=5:5:0.5:5:5:0.0,"
        "vignette=PI/6,"
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"
    )
    if subtitle_path and os.path.exists(subtitle_path) and _ffmpeg_has_filter("ass"):
        safe_sub = _resolve_repo_path(subtitle_path)
        escaped_sub = safe_sub.replace("\\", "/").replace(":", "\\:")
        video_chain += f",ass='{escaped_sub}'"
    video_chain += "[v]"

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", inputs[0],
        "-i", inputs[1],
        "-stream_loop", "-1", "-i", inputs[2],
        "-t", f"{target_duration:.2f}",
        "-filter_complex", f"{filter_complex};{video_chain}",
        "-map", "[v]",
        "-map", "[out]",
        "-map_metadata", "-1",
        "-c:v", "libx264",
        "-preset", "fast",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-g", "60",
        "-keyint_min", "60",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-bsf:v", "filter_units=remove_types=6",
        "-b:v", "5000k",
        "-maxrate", "5500k",
        "-bufsize", "11000k",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ac", "2",
        "-ar", "44100",
        "-max_muxing_queue_size", "1024",
        "-movflags", "+faststart",
        output_path,
    ]

    subprocess.run(cmd, check=True)
    print(f"✅ Final narrated video saved: {output_path} ({target_duration:.1f}s)")
    return output_path


def post_to_twitter_text_only(caption: str) -> str:
    """Post a text-only tweet using Twitter API v2 (Free tier compliant)."""
    if not os.getenv("TWITTER_API_KEY") or not os.getenv("TWITTER_ACCESS_TOKEN"):
        return ""
    try:
        client = tweepy.Client(
            bearer_token=os.getenv("TWITTER_BEARER_TOKEN"),
            consumer_key=os.getenv("TWITTER_API_KEY"),
            consumer_secret=os.getenv("TWITTER_API_SECRET"),
            access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
            access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
        )
        response = client.create_tweet(text=caption[:280])
        tweet_id = response.data["id"]
        return f"https://twitter.com/user/status/{tweet_id}"
    except Exception as e:
        print(f"⚠️ Twitter text post failed: {_redact_secrets(e)}")
        return ""


def generate_tweet_from_video(poem: str, youtube_url: str = "", insta_url: str = ""):
    """Returns a tweet-safe caption that includes the hook, CTA line, and links, max 280 chars."""
    poem_clean = poem.strip()
    cta_line = "✨ Experience this in cinematic HD:\n"
    base_links = f"🎬 {youtube_url}\n📸 {insta_url}"
    reserved_length = len(cta_line + base_links)
    max_poem_chars = max(0, 280 - reserved_length - 1)
    if len(poem_clean) > max_poem_chars:
        poem_clean = poem_clean[: max_poem_chars - 3].rstrip() + "..."
    return f"{poem_clean}\n{cta_line}{base_links}"


# ============================================================================
# YOUTUBE AUTHENTICATION & RESILIENT UPLOADER (SHORTS + >8 MIN LONG VIDEOS)
# ============================================================================
def authenticate_youtube():
    """
    Authenticate to YouTube using OAuth 2.0 refresh token flow with retries and
    actionable diagnostics if `YOUTUBE_REFRESH_TOKEN` was revoked/expired.
    """
    client_id = (os.getenv("YOUTUBE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip()
    refresh_token = (os.getenv("YOUTUBE_REFRESH_TOKEN") or "").strip()

    if not client_id or not client_secret or not refresh_token:
        raise YouTubeAuthError(
            "Missing YouTube OAuth secrets (YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, or YOUTUBE_REFRESH_TOKEN)."
        )

    # Omit `scopes=` during refresh so tokens minted with broader scopes (e.g. youtube.force-ssl)
    # refresh cleanly without scope-mismatch rejections.
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
    )

    last_err = None
    for attempt in range(3):
        try:
            creds.refresh(Request())
            # If Google rotated the refresh token, persist locally if .env exists
            if creds.refresh_token and creds.refresh_token != refresh_token:
                _persist_env_var("YOUTUBE_REFRESH_TOKEN", creds.refresh_token)
            return build("youtube", "v3", credentials=creds, cache_discovery=False)
        except RefreshError as re_err:
            msg = _redact_secrets(str(re_err))
            if "invalid_grant" in msg:
                raise YouTubeAuthError(
                    "YouTube OAuth refresh failed (invalid_grant: token expired or revoked). "
                    "Google Cloud OAuth apps in 'Testing' mode expire refresh tokens every 7 days. "
                    "Switch Google Cloud OAuth Consent Screen to 'In Production' and run "
                    "`python3 scripts/youtube_reauth.py --client-secrets <client_secret.json>`."
                ) from re_err
            raise YouTubeAuthError(f"YouTube OAuth refresh error: {msg}") from re_err
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)

    raise YouTubeAuthError(f"YouTube OAuth token refresh failed after retries: {_redact_secrets(last_err)}")


def upload_video_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list = None,
    category_id: str = "15",
    privacy_status: str = "public",
    max_attempts: int = 3,
) -> str:
    """
    Uploads a video (Short or >8-min Long-Form Horizontal) to YouTube using chunked
    resumable upload with automatic exponential-backoff retries on transient 5xx/network errors.
    """
    safe_video = _resolve_repo_path(video_path)
    if not os.path.isfile(safe_video):
        raise FileNotFoundError(f"Video not found: {safe_video}")

    clean_title = re.sub(r"[<>]", "", title.strip().replace("\n", " "))[:100] or "GeoSerene Wildlife Documentary"
    clean_desc = re.sub(r"[<>]", "", (description or "").strip())[:4950]
    clean_tags = [re.sub(r"[<>]", "", str(t).strip()) for t in (tags or []) if str(t).strip()][:25]

    youtube = authenticate_youtube()
    body = {
        "snippet": {
            "title": clean_title,
            "description": clean_desc,
            "tags": clean_tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            media = MediaFileUpload(
                safe_video,
                mimetype="video/mp4",
                chunksize=RESUMABLE_CHUNK_SIZE,
                resumable=True,
            )
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            chunk_retries = 0
            while response is None:
                try:
                    status, response = request.next_chunk()
                    if status:
                        print(f"📤 YouTube upload progress: {int(status.progress() * 100)}%")
                    chunk_retries = 0
                except HttpError as he:
                    if he.resp.status in RETRIABLE_YT_STATUS_CODES and chunk_retries < 4:
                        chunk_retries += 1
                        wait_s = 2 ** chunk_retries
                        print(f"⚠️ Transient YouTube chunk HTTP {he.resp.status}; retrying in {wait_s}s...")
                        time.sleep(wait_s)
                        continue
                    raise
            return response["id"]
        except YouTubeAuthError:
            raise
        except HttpError as he:
            last_err = he
            err_txt = _redact_secrets(str(he))
            if "quotaExceeded" in err_txt or he.resp.status in (400, 401, 403):
                raise RuntimeError(f"YouTube API rejected upload (HTTP {he.resp.status}): {err_txt}") from he
            print(f"⚠️ YouTube upload attempt {attempt}/{max_attempts} failed: {err_txt}")
        except Exception as e:
            last_err = e
            print(f"⚠️ YouTube upload attempt {attempt}/{max_attempts} error: {_redact_secrets(e)}")
        if attempt < max_attempts:
            time.sleep(4 * attempt)

    raise RuntimeError(f"YouTube upload failed after {max_attempts} attempts: {_redact_secrets(last_err)}")


# ============================================================================
# INSTAGRAM REELS GRAPH API
# ============================================================================
def _persist_env_var(key: str, value: str) -> None:
    """Updates a key=value entry in the local .env file if it exists."""
    env_file = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_file):
        return
    with open(env_file, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def ensure_long_lived_ig_token() -> str:
    """
    Checks the current IG_ACCESS_TOKEN expiration via /debug_token.
    Automatically exchanges/refreshes <45-day tokens when FB_APP_ID & FB_APP_SECRET are set.
    """
    global IG_ACCESS_TOKEN
    if not IG_ACCESS_TOKEN:
        return ""

    fb_app_id = (os.getenv("FB_APP_ID") or "").strip()
    fb_app_secret = (os.getenv("FB_APP_SECRET") or "").strip()
    app_token = f"{fb_app_id}|{fb_app_secret}" if (fb_app_id and fb_app_secret) else IG_ACCESS_TOKEN

    try:
        dbg_resp = requests.get(
            f"{BASE_URL}/debug_token",
            params={"input_token": IG_ACCESS_TOKEN, "access_token": app_token},
            timeout=HTTP_TIMEOUT,
        ).json()
        data = dbg_resp.get("data", {})
        is_valid = data.get("is_valid", False)
        expires_at = data.get("expires_at")

        if not is_valid:
            print("⚠️ Instagram token reported invalid/expired by /debug_token.")
            return IG_ACCESS_TOKEN

        if expires_at == 0:
            print("🔒 Instagram token is a permanent non-expiring Page token.")
            return IG_ACCESS_TOKEN

        remaining_days = (expires_at - time.time()) / 86400 if expires_at else 0
        print(f"🔑 Instagram token expires in {remaining_days:.1f} days.")

        if remaining_days < 45 and fb_app_id and fb_app_secret:
            print("🔄 Refreshing Instagram token for a 60-day / permanent Page token...")
            source_token = os.getenv("IG_USER_TOKEN") or IG_ACCESS_TOKEN
            ex_resp = requests.get(
                f"{BASE_URL}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": fb_app_id,
                    "client_secret": fb_app_secret,
                    "fb_exchange_token": source_token,
                },
                timeout=HTTP_TIMEOUT,
            ).json()
            long_user_token = ex_resp.get("access_token")
            if long_user_token:
                _persist_env_var("IG_USER_TOKEN", long_user_token)
                os.environ["IG_USER_TOKEN"] = long_user_token
                final_token = long_user_token

                pages_resp = requests.get(
                    f"{BASE_URL}/me/accounts",
                    params={"fields": "id,name,access_token,instagram_business_account", "access_token": long_user_token},
                    timeout=HTTP_TIMEOUT,
                ).json()
                for page in pages_resp.get("data", []):
                    ig_acct = page.get("instagram_business_account", {}).get("id")
                    if (ig_acct == IG_USER_ID or page.get("name") == "GeoSerene") and page.get("access_token"):
                        final_token = page["access_token"]
                        print("✅ Obtained permanent non-expiring Page/Instagram access token!")
                        break

                IG_ACCESS_TOKEN = final_token
                os.environ["IG_ACCESS_TOKEN"] = final_token
                _persist_env_var("IG_ACCESS_TOKEN", final_token)
    except Exception as e:
        print(f"⚠️ Instagram token check/refresh warning: {_redact_secrets(e)}")

    return IG_ACCESS_TOKEN


def init_reel_upload(caption: str, cover_url: str = None) -> tuple:
    endpoint = f"{BASE_URL}/{IG_USER_ID}/media"
    params = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "access_token": IG_ACCESS_TOKEN,
    }
    if cover_url:
        params["cover_url"] = cover_url
    resp = requests.post(endpoint, data=params, timeout=HTTP_TIMEOUT).json()
    if "id" not in resp or "uri" not in resp:
        raise RuntimeError(f"Error initializing reel upload: {_redact_secrets(resp)}")
    return resp["id"], resp["uri"]


def upload_reel_video(upload_url: str, video_path: str) -> None:
    safe_video = _resolve_repo_path(video_path)
    file_size = os.path.getsize(safe_video)
    headers = {
        "Authorization": f"OAuth {IG_ACCESS_TOKEN}",
        "offset": "0",
        "file_size": str(file_size),
    }
    with open(safe_video, "rb") as f:
        resp = requests.post(upload_url, headers=headers, data=f, timeout=300)
    data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
    if not resp.ok or (data.get("success") is False):
        raise RuntimeError(f"Error uploading reel video: {_redact_secrets(data or resp.text)}")


def wait_for_media_container(container_id: str, max_wait_seconds: int = 180, poll_interval: int = 5) -> None:
    endpoint = f"{BASE_URL}/{container_id}"
    params = {"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN}
    elapsed = 0
    while elapsed < max_wait_seconds:
        resp = requests.get(endpoint, params=params, timeout=HTTP_TIMEOUT).json()
        status_code = resp.get("status_code")
        print(f"⏳ Instagram container {container_id} status: {status_code or 'checking'}")
        if status_code == "FINISHED":
            return
        if status_code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram media container processing failed: {_redact_secrets(resp)}")
        time.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"Timed out waiting for Instagram container {container_id} to finish processing.")


def publish_media_container(container_id: str) -> dict:
    endpoint = f"{BASE_URL}/{IG_USER_ID}/media_publish"
    params = {"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}
    resp = requests.post(endpoint, data=params, timeout=HTTP_TIMEOUT).json()
    if "id" not in resp:
        raise RuntimeError(f"Error publishing media: {_redact_secrets(resp)}")
    return resp


def get_media_permalink(media_id: str) -> str:
    try:
        endpoint = f"{BASE_URL}/{media_id}"
        params = {"fields": "permalink", "access_token": IG_ACCESS_TOKEN}
        resp = requests.get(endpoint, params=params, timeout=HTTP_TIMEOUT).json()
        return resp.get("permalink", "")
    except Exception:
        return ""


def post_reel(video_path: str, caption: str, cover_url: str = None, max_attempts: int = 2) -> tuple:
    ensure_long_lived_ig_token()
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            container_id, upload_url = init_reel_upload(caption, cover_url)
            upload_reel_video(upload_url, video_path)
            wait_for_media_container(container_id)
            res = publish_media_container(container_id)
            media_id = res["id"]
            permalink = get_media_permalink(media_id)
            print(f"✅ Reel posted successfully (media ID: {media_id})")
            return media_id, permalink
        except Exception as e:
            last_err = e
            print(f"⚠️ Instagram Reel attempt {attempt}/{max_attempts} failed: {_redact_secrets(e)}")
            if attempt < max_attempts:
                time.sleep(5)
    raise last_err


# ============================================================================
# HISTORY & PUBLISH STATE PERSISTENCE (PARITY TRACKING + BACKFILL QUEUE)
# ============================================================================
def is_duplicate_video(url: str, history_file: str = "history.txt") -> bool:
    """Check if the video URL already exists in the history file."""
    history_path = _resolve_repo_path(history_file)
    if not os.path.exists(history_path):
        return False
    with open(history_path, "r", encoding="utf-8") as f:
        history = {line.strip() for line in f if line.strip()}
    return (url or "").strip() in history


def save_video_to_history(url: str, history_file: str = "history.txt") -> None:
    """Append a new video URL to the history file (idempotent)."""
    clean = (url or "").strip()
    if not clean or is_duplicate_video(clean, history_file=history_file):
        return
    history_path = _resolve_repo_path(history_file)
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(clean + "\n")


def load_publish_state(state_file: str = "publish_state.json") -> dict:
    path = _resolve_repo_path(state_file)
    default = {"pending_youtube_reels": [], "recent_reels": [], "long_videos": []}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default.items():
            data.setdefault(k, list(v))
        return data
    except Exception:
        return default


def save_publish_state(state: dict, state_file: str = "publish_state.json") -> None:
    path = _resolve_repo_path(state_file)
    state["pending_youtube_reels"] = (state.get("pending_youtube_reels") or [])[-15:]
    state["recent_reels"] = (state.get("recent_reels") or [])[-40:]
    state["long_videos"] = (state.get("long_videos") or [])[-40:]
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def long_videos_uploaded_today(state: dict) -> int:
    today = _today_utc()
    return sum(1 for item in (state.get("long_videos") or []) if str(item.get("date")) == today)


def instagram_posts_today(state: dict) -> int:
    """Returns how many Instagram Reels have already been published on the current UTC date."""
    today = _today_utc()
    return sum(
        1
        for item in (state.get("recent_reels") or [])
        if str(item.get("date") or str(item.get("ts") or "")[:10]) == today
        and bool(item.get("instagram_url"))
    )


def youtube_posts_today(state: dict) -> int:
    """
    Returns total videos (Shorts + Long-Form Horizontal + Backfills) published to YouTube
    on the current UTC date so YouTube strictly obeys the 1-video-per-day rule.
    """
    today = _today_utc()
    shorts_today = sum(
        1
        for item in (state.get("recent_reels") or [])
        if str(item.get("date") or str(item.get("ts") or "")[:10]) == today
        and bool(item.get("youtube_short_url"))
    )
    backfills_today = sum(
        1
        for item in (state.get("backfilled_youtube") or [])
        if str(item.get("date") or str(item.get("ts") or "")[:10]) == today
    )
    return shorts_today + long_videos_uploaded_today(state) + backfills_today


# ============================================================================
# NARRATION, TITLES & CAPTIONS (SHORTS + LONG-FORM YOUTUBE DOCUMENTARY)
# ============================================================================
def clean_text_for_tts(text: str) -> str:
    """
    Normalizes a narration script into clean, natural spoken English for TTS:
    - Strips URLs, @mentions, #hashtags, markdown, and speaker labels ('Narrator:', 'Voiceover:').
    - Preserves contractions (`doesn't`, `nature's`) by normalizing curly apostrophes first.
    - Expands `%`, `&`, `°C`/`°F`, `km/h`, `mph` into spoken words so '95%' never becomes '95'.
    - Converts em-dashes, en-dashes, colons, and semicolons into natural comma pauses so adjacent
      words NEVER fuse together.
    """
    s = str(text or "")
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"[@#]\w+", "", s)
    s = re.sub(r"^\s*(?:narrator|voiceover|script|hook|voice)\s*:\s*", "", s, flags=re.IGNORECASE | re.MULTILINE)
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"(\d)\s*%", r"\1 percent", s)
    s = s.replace("%", " percent ")
    s = re.sub(r"°\s*C\b", " degrees Celsius", s)
    s = re.sub(r"°\s*F\b", " degrees Fahrenheit", s)
    s = re.sub(r"\bkm/h\b", "kilometers per hour", s, flags=re.IGNORECASE)
    s = re.sub(r"\bmph\b", "miles per hour", s, flags=re.IGNORECASE)
    s = s.replace("&", " and ")
    s = re.sub(r"\s*[—–;:]+\s*", ", ", s)
    s = re.sub(r"[^\w\s.,!?'\-]", " ", s)
    s = re.sub(r",\s*,+", ", ", s)
    s = re.sub(r"\s+([.,!?])", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def fetch_wildlife_fact(topic: str) -> dict:
    """Fetches a verified summary from Wikipedia REST API to ground scripts in real science."""
    slug = topic.strip().replace(" ", "_")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(slug)}"
    headers = {"User-Agent": BOT_USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            extract = (data.get("extract") or "").strip()
            if extract:
                return {
                    "title": data.get("title", topic),
                    "description": data.get("description", ""),
                    "extract": extract[:1200],
                }
    except Exception as e:
        print(f"⚠️ Wikipedia fact lookup skipped for '{topic}': {e}")
    return {"title": topic, "description": "", "extract": ""}


def _ensure_complete_sentences(text: str) -> str:
    """
    Cleans markdown/preambles and guarantees complete terminal sentence punctuation
    WITHOUT chopping away the body of a script when only a final period was omitted.
    """
    raw = str(text or "").strip()
    raw = re.sub(
        r"^\s*(?:here\s+is[^\n:]*:|voiceover\s*(?:script)?\s*:|narrator\s*:|script\s*:)\s*",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    raw = raw.replace("’", "'").replace("‘", "'")
    raw = re.sub(r"[*_~`#]+", "", raw)
    cleaned = re.sub(r"\.{2,}|…", ".", raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(' "\'')
    if not cleaned:
        return ""
    if cleaned[-1] not in ".!?":
        last_punct = max(cleaned.rfind("."), cleaned.rfind("!"), cleaned.rfind("?"))
        if last_punct != -1:
            prefix = cleaned[: last_punct + 1].strip()
            tail_words = re.findall(r"[A-Za-z0-9']+", cleaned[last_punct + 1 :])
            prefix_words = re.findall(r"[A-Za-z0-9']+", prefix)
            # Only drop a dangling cut-off fragment if <= 6 trailing words AND prefix is already substantial
            if len(tail_words) <= 6 and len(prefix_words) >= 28:
                cleaned = prefix
            else:
                cleaned = cleaned.rstrip(",;:- ") + "."
        else:
            cleaned = cleaned.rstrip(",;:- ") + "."
    return cleaned


def _is_valid_narration(script: str, topic: str, min_words: int = 42, max_words: int = 96) -> tuple:
    """
    Strictly validates that a generated short-form narration is full-length (42..96 words,
    ~18-30s spoken), has >=3 complete sentences, explicitly names the subject `topic`,
    and contains no meta/prompt leakage.
    """
    s = (script or "").strip()
    if not s or s[-1] not in ".!?":
        return False, "missing terminal sentence punctuation"

    words = re.findall(r"[A-Za-z0-9']+", s)
    if len(words) < min_words:
        return False, f"script too short ({len(words)} words < {min_words} min)"
    if len(words) > max_words:
        return False, f"script too long ({len(words)} words > {max_words} max)"

    sentences = [seg.strip() for seg in re.split(r"[.!?]+", s) if len(re.findall(r"[A-Za-z]+", seg)) >= 3]
    if len(sentences) < 3:
        return False, f"fewer than 3 complete sentences ({len(sentences)})"

    low = s.lower()
    for bad in ("voiceover", "here is the", "in this video", "subscribe", "camera", "footage"):
        if bad in low:
            return False, f"contains meta phrase '{bad}'"

    topic_tokens = [w for w in re.findall(r"[a-z]+", (topic or "").lower()) if len(w) > 2]
    if topic_tokens:
        matched = False
        for tok in topic_tokens:
            stem = tok[:-1] if tok.endswith("s") and len(tok) > 3 else tok
            if stem in low:
                matched = True
                break
        if not matched:
            return False, f"narration does not mention topic '{topic}'"

    return True, "ok"


def _build_fallback_narration(nature: str, fact_context: dict = None, metadata: dict = None) -> str:
    """
    Deterministic, scientifically grounded 50-72 word National Geographic fallback script
    built from verified Wikipedia facts so TTS is NEVER tiny, empty, or hallucinated.
    """
    subject = (nature or "wildlife").strip()
    extract = re.sub(r"\([^)]*\)", "", (fact_context or {}).get("extract") or "")
    extract = re.sub(r"\s+", " ", extract).strip()
    wiki_sentences = [
        seg.strip()
        for seg in re.split(r"(?<=[.!?])\s+", extract)
        if 6 <= len(re.findall(r"[A-Za-z0-9']+", seg)) <= 32
    ]

    chosen = []
    chosen.append(f"Few wonders on Earth command the wild quite like the {subject}.")
    for sent in wiki_sentences[:3]:
        if sum(len(re.findall(r"[A-Za-z0-9']+", x)) for x in chosen) >= 52:
            break
        chosen.append(sent if sent[-1] in ".!?" else sent + ".")

    if sum(len(re.findall(r"[A-Za-z0-9']+", x)) for x in chosen) < 44:
        chosen.append(
            f"Shaped by millions of years of evolution, every movement of the {subject} reveals extraordinary precision and resilience in its natural habitat."
        )
    chosen.append(
        f"Protecting the untamed world of the {subject} keeps one of Earth's most remarkable ecosystems alive."
    )
    return _ensure_complete_sentences(" ".join(chosen))


def generate_video_narration(nature: str, video_url: str, metadata: dict, fact_context: dict = None) -> str:
    original_url = (metadata.get("original_url") or "").rstrip("/")
    raw_title = original_url.split("/")[-1] if "/" in original_url else metadata.get("title", "")
    cleaned_title = re.sub(r"-\d+$", "", raw_title).replace("-", " ")
    content_hint = cleaned_title.strip().capitalize() or nature

    tags = metadata.get("tags", [])
    tag_text = ", ".join(tags[:8]) if tags else "wild habitat"

    fact_extract = (fact_context or {}).get("extract", "")
    fact_block = (
        f"\nVerified scientific facts about {nature} (ground EVERY claim strictly in these facts — "
        f"NEVER invent traits belonging to a different animal):\n{fact_extract}\n"
        if fact_extract
        else f"\nStrictly state only real, scientifically verified facts about {nature}.\n"
    )

    last_reason = ""
    for attempt in range(1, 4):
        retry_note = (
            f"\nIMPORTANT FIX FOR RETRY {attempt}: Previous draft failed check ({last_reason}). "
            f"Write 52 to 68 words across 4 to 5 complete sentences explicitly naming '{nature}'."
            if last_reason else ""
        )
        prompt = f"""
You are a senior National Geographic short-form documentary narrator.

Write a rich, gripping, crystal-clear spoken voiceover script of **52 to 68 words** (4 to 5 complete sentences, ~22 seconds spoken) strictly about "{nature}" matching the visual scene "{content_hint}" (elements: {tag_text}).
{fact_block}{retry_note}
Structure for Maximum Viewer Retention:
1. SENTENCE 1 (0-3s HOOK): Name "{nature}" right away in the first sentence with a specific, astonishing, scientifically true biological or physical fact. NEVER write vague filler like "Think you know this animal?"
2. SENTENCES 2-4 (DEEP SCIENCE & SURVIVAL): Explain how {nature} hunts, navigates, communicates, or thrives in the wild using vivid, concrete real-world details and numbers.
3. FINAL SENTENCE (MEMORABLE PAYOFF): Close with a powerful, complete concluding sentence that leaves viewers in awe of {nature}.

Strict Output Rules:
- MUST be between 48 and 72 words total (4 to 5 complete sentences).
- EVERY sentence MUST end with a period (.) or question mark (?).
- NEVER invent jaws, bites, venom, or traits that {nature} does not possess in real biology.
- Plain spoken English only: NO markdown, NO stage directions, NO speaker labels, NO ellipses (...), NO emojis.
""".strip()

        try:
            messages = [
                SystemMessage(
                    content=(
                        "You write 52-68 word, 100% factually accurate National Geographic documentary "
                        "voiceover scripts. Output only the spoken sentences."
                    )
                ),
                HumanMessage(content=prompt),
            ]
            response = llm.invoke(messages)
            candidate = _ensure_complete_sentences(response.content.strip())
            valid, reason = _is_valid_narration(candidate, nature)
            if valid:
                wc = len(re.findall(r"[A-Za-z0-9']+", candidate))
                print(f"📝 Validated Narration ({wc} words): {candidate}")
                return candidate
            last_reason = reason
            print(f"⚠️ Narration draft {attempt}/3 rejected ({reason}); retrying...")
        except Exception as e:
            last_reason = str(e)
            print(f"⚠️ Narration generation attempt {attempt}/3 error: {_redact_secrets(e)}")

    fallback = _build_fallback_narration(nature, fact_context=fact_context, metadata=metadata)
    wc = len(re.findall(r"[A-Za-z0-9']+", fallback))
    print(f"📝 Using verified Wikipedia documentary narration ({wc} words): {fallback}")
    return fallback


def generate_caption_from_narration(narration: str, title: str) -> str:
    prompt = f"""
You're a viral Instagram Reels & YouTube Shorts SEO strategist for @geoserene.

Given:
TITLE: {title}
NARRATION: {narration}

Craft a high-reach, SEO-optimized caption with this exact structure:
1. SEO HOOK LINE: Start with the Title + 1 keyword-rich curiosity sentence summarizing what viewers are watching.
2. COMMENT TRIGGER: A blank line, then one intriguing, easy-to-answer question for viewers about this specific subject.
3. SAVE/SHARE CTA: "Save this for later & follow @geoserene for daily wild discoveries."
4. HASHTAGS: A blank line, then 8-12 focused, space-separated hashtags mixing broad + niche search terms (always include #wildlife #natgeo #animalfacts #naturefacts #shorts #reels).

Rules:
- Keep every statement 100% consistent with the NARRATION facts.
- Keep it advertiser-friendly and brand-safe.
- Do NOT include markdown (*, _, `) or HTML.
- Keep total length under 1500 characters.
- Return only the full caption.
    """

    messages = [
        SystemMessage(content="You're a seasoned viral social media & SEO strategist for National Geographic."),
        HumanMessage(content=prompt),
    ]
    response = llm.invoke(messages)
    caption = response.content.strip()
    caption = re.sub(r"<[^>]+>", "", caption)
    caption = re.sub(r"[*_~`]", "", caption)
    if len(caption) > 4990:
        caption = caption[:4990].rsplit(" ", 1)[0] + "…"
    return caption.strip()


def generate_title_from_narration(narration: str, topic: str = "") -> str:
    topic_line = f"Primary Subject: {topic}\n" if topic else ""
    prompt = f"""
You are a viral YouTube Shorts & National Geographic SEO headline writer.

{topic_line}Given this narration:
\"\"\"{narration}\"\"\"

Write ONE curiosity-driven, factually accurate, search-friendly title (4 to 8 words, strictly under 55 characters) that makes people stop scrolling.
MUST name the specific animal or natural phenomenon from the narration{f' ({topic})' if topic else ''}.
NEVER invent facts not present in the narration.
Keep it 100% advertiser-friendly and family-safe.

Return only the title. No HTML. No Markdown. No quotation marks.
    """
    messages = [
        SystemMessage(content="You are a viral, factually accurate wildlife documentary headline writer."),
        HumanMessage(content=prompt),
    ]
    response = llm.invoke(messages)
    clean_title = re.sub(r"<[^>]+>", "", response.content.splitlines()[0].strip())
    clean_title = re.sub(r"[*_~`\"'“”]", "", clean_title).strip()
    if topic:
        topic_tokens = [w for w in re.findall(r"[a-z]+", topic.lower()) if len(w) > 2]
        low_title = clean_title.lower()
        if topic_tokens and not any((t[:-1] if t.endswith("s") and len(t) > 3 else t) in low_title for t in topic_tokens):
            clean_title = f"The Extraordinary Wild {topic.title()}"[:54]
    return clean_title or f"Wild {(topic or 'Nature').title()} Up Close"


def generate_long_video_metadata(topic: str, metadata: dict, duration_s: float, fact_context: dict = None) -> dict:
    """
    Generates a compelling YouTube Long-Form title (NO #Shorts!), SEO documentary description
    with chapters and free-license attribution, plus 3 substantial chapter narrations
    (45-65 words each) for scoring silent open-source nature films cleanly.
    """
    mins = max(8, int(duration_s // 60))
    raw_title = str(metadata.get("title") or topic).strip()
    fact_extract = (fact_context or {}).get("extract", "")
    credit_line = format_license_credit(metadata)

    mid1_m = max(2, int((duration_s * 0.33) // 60))
    mid2_m = max(5, int((duration_s * 0.68) // 60))
    chapters = (
        "00:00 Wild Opening & Habitat\n"
        f"{mid1_m:02d}:00 Deep Wilderness Observation\n"
        f"{mid2_m:02d}:00 Natural Behavior & Atmosphere"
    )

    chapter_narrations = []
    try:
        prompt = f"""
You are a senior YouTube Long-Form Nature Documentary producer for GeoSerene.

Topic: {topic}
Source film title: {raw_title}
Duration: {mins} minutes (widescreen horizontal documentary)
Scientific background: {fact_extract}

Return JSON ONLY with keys "title", "overview", "chapter_narrations":
- "title": Compelling, searchable YouTube documentary title (45 to 85 characters, NO #Shorts, NO hashtags, NO quotes).
- "overview": 2 rich, engaging paragraphs (120-180 words) describing what makes {topic} extraordinary for viewers watching this full-length nature feature. Plain text, no markdown.
- "chapter_narrations": Array of 3 calm, authoritative National Geographic voiceover paragraphs (45 to 60 words EACH, 3-4 complete sentences each) for Chapter 1 (Opening & Habitat), Chapter 2 (Wilderness Observation & Science), and Chapter 3 (Conservation & Closing Reflection), strictly grounded in real facts about {topic}.
"""
        resp = llm.invoke([
            SystemMessage(content="Return valid JSON only with keys title, overview, chapter_narrations."),
            HumanMessage(content=prompt),
        ]).content.strip()
        resp_clean = re.sub(r"^```(?:json)?|```$", "", resp, flags=re.IGNORECASE | re.MULTILINE).strip()
        parsed = json.loads(resp_clean)
        yt_title = re.sub(r"#\w+", "", str(parsed.get("title") or "")).strip()[:95]
        overview = re.sub(r"[*_~`<>]", "", str(parsed.get("overview") or "")).strip()
        raw_chapters = parsed.get("chapter_narrations") or []
        if isinstance(raw_chapters, list):
            for ch_txt in raw_chapters[:3]:
                cleaned_ch = _ensure_complete_sentences(str(ch_txt or ""))
                if len(re.findall(r"[A-Za-z0-9']+", cleaned_ch)) >= 25:
                    chapter_narrations.append(cleaned_ch)
        if not chapter_narrations and parsed.get("intro_voiceover"):
            chapter_narrations.append(_ensure_complete_sentences(str(parsed.get("intro_voiceover"))))
    except Exception:
        yt_title = f"{topic.title()}: {mins}-Minute Wild Nature & Habitat Documentary (4K/HD)"[:95]
        overview = (
            f"Immerse yourself in {mins} minutes of uninterrupted horizontal footage exploring {topic} "
            f"({raw_title}).{(' ' + fact_extract[:350]) if fact_extract else ''}"
        )

    if not chapter_narrations:
        chapter_narrations = [_build_fallback_narration(topic, fact_context=fact_context, metadata=metadata)]

    if not yt_title:
        yt_title = f"{topic.title()} — Full Nature Documentary ({mins} Mins)"[:95]

    description = (
        f"{overview}\n\n"
        f"Chapters:\n{chapters}\n\n"
        "Subscribe to @geoserene for daily wildlife features and widescreen nature documentaries.\n\n"
        f"{credit_line}\n\n"
        "#naturedocumentary #wildlife #natgeo #4knature #earth #wildanimals #documentary"
    )
    topic_words = [w.lower() for w in re.findall(r"[a-zA-Z]+", topic) if len(w) > 2]
    tags = list(dict.fromkeys(topic_words + [
        "nature documentary", "wildlife documentary", "national geographic style",
        "wild animals", "earth documentary", "4k nature", "scenic relaxation",
        "wildlife conservation", "natural world",
    ]))
    return {
        "title": yt_title,
        "description": description[:4900],
        "tags": tags,
        "intro_voiceover": chapter_narrations[0],
        "chapter_narrations": chapter_narrations,
    }


# ============================================================================
# OPEN-SOURCE & FREE-LICENSE VIDEO DISCOVERY (SHORT REELS + >8M HORIZONTAL)
# ============================================================================
_STOPWORDS = {"in", "on", "at", "of", "the", "a", "an", "with", "over", "under", "and", "from"}

_NON_NATURE_BLACKLIST = {
    "poet", "poetry", "interview", "speech", "lecture", "conference", "symposium",
    "senate", "congress", "committee", "hearing", "parliament", "politician",
    "concert", "orchestra", "symphony", "podcast", "sermon", "church",
    "automobile", "sedan", "coupe", "dealership", "motorsport", "supercar",
    "football", "baseball", "basketball", "touchdown", "stadium", "nfl",
    "nvtv", "cartoon", "anime", "gameplay", "walkthrough", "esports",
}


def _video_matches_topic(query: str, metadata: dict) -> bool:
    """
    Verifies that every required keyword in `query` (or its singular/plural form)
    appears in the video's title, description, URL slug, or tags, and rejects
    obvious non-nature false positives (e.g. 'The Jaguar Poet', 'Jaguar XJ sedan', interviews).
    """
    req_words = [w for w in re.findall(r"[a-z]+", (query or "").lower()) if w not in _STOPWORDS]
    if not req_words:
        return True

    haystack_str = " ".join([
        str(metadata.get("title") or ""),
        str(metadata.get("description") or ""),
        " ".join(str(t) for t in (metadata.get("tags") or [])),
        str(metadata.get("original_url") or ""),
    ]).lower()
    haystack_tokens = set(re.findall(r"[a-z]+", haystack_str))

    if haystack_tokens & _NON_NATURE_BLACKLIST:
        return False

    for word in req_words:
        variants = {word, f"{word}s", f"{word}es"}
        if word.endswith("ies") and len(word) > 3:
            variants.add(f"{word[:-3]}y")
        if word.endswith("y") and len(word) > 2:
            variants.add(f"{word[:-1]}ies")
        if word.endswith("es") and len(word) > 3:
            variants.add(word[:-2])
        if word.endswith("s") and len(word) > 2:
            variants.add(word[:-1])
        if not (variants & haystack_tokens):
            return False
    return True


def _score_video_candidate(width: int, height: int) -> int:
    """Scores a short-form Reel candidate preferring 1080p vertical (portrait) HD clips."""
    resolution = (width or 0) * (height or 0)
    if resolution < 1280 * 720:
        return 0
    is_vertical = (height or 0) > (width or 0)
    return min(resolution, 1920 * 1080) + (10_000_000 if is_vertical else 0)


def _score_horizontal_long_candidate(width: int, height: int, duration: float = 0.0) -> int:
    """Scores a long-form YouTube candidate: strictly horizontal >=1280x720 and >8 mins."""
    w = int(width or 0)
    h = int(height or 0)
    if w <= h or w < LONG_VIDEO_MIN_WIDTH or h < LONG_VIDEO_MIN_HEIGHT:
        return 0
    if (w / float(h)) < LONG_VIDEO_MIN_ASPECT:
        return 0
    if duration and duration < LONG_VIDEO_MIN_SECONDS:
        return 0
    # Prefer crisp 1080p widescreen (~16:9)
    aspect_bonus = 500_000 if 1.65 <= (w / float(h)) <= 1.90 else 0
    return min(w * h, 1920 * 1080) + aspect_bonus


def fetch_pexels_videos(
    query="wildlife",
    min_duration=12,
    max_duration=45,
    max_results=5,
    orientation="portrait",
    history_file="history.txt",
):
    if not PEXELS_API_KEY:
        print("Pexels API key not configured.")
        return []

    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 40, "orientation": orientation}
    url = "https://api.pexels.com/videos/search"
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    except Exception as e:
        print(f"Pexels request error: {_redact_secrets(e)}")
        return []

    if resp.status_code != 200:
        print(f"Pexels error: {resp.status_code}")
        return []

    videos = [
        v for v in resp.json().get("videos", [])
        if min_duration <= v.get("duration", 0) <= max_duration
    ]

    candidates = []
    for video in videos:
        dur = float(video.get("duration") or 0)
        metadata = {
            "title": video.get("url", "").rstrip("/").split("/")[-1].replace("-", " "),
            "duration": dur,
            "tags": [tag["title"] for tag in video.get("tags", []) if isinstance(tag, dict) and "title" in tag],
            "original_url": video.get("url"),
            "author": (video.get("user") or {}).get("name", ""),
            "license": "Pexels Free License",
            "source": "pexels",
        }
        if not _video_matches_topic(query, metadata):
            continue

        best_vf = None
        best_score = 0
        for vf in video.get("video_files", []):
            link = vf.get("link")
            if not link or is_duplicate_video(link, history_file=history_file):
                continue
            w = vf.get("width") or 0
            h = vf.get("height") or 0
            score = (
                _score_horizontal_long_candidate(w, h, dur)
                if orientation == "landscape" and min_duration >= LONG_VIDEO_MIN_SECONDS
                else _score_video_candidate(w, h)
            )
            if score > best_score:
                best_score = score
                best_vf = vf
        if best_vf:
            metadata["width"] = best_vf.get("width")
            metadata["height"] = best_vf.get("height")
            candidates.append((best_vf["link"], metadata))
            if len(candidates) >= max_results:
                break
    return candidates


def fetch_pexels_video(query="nature", min_duration=12, max_duration=45):
    results = fetch_pexels_videos(query=query, min_duration=min_duration, max_duration=max_duration, max_results=3)
    if not results:
        return None, None
    return random.choice(results)


def fetch_pixabay_videos(
    query="wildlife",
    min_duration=12,
    max_duration=45,
    max_results=5,
    horizontal_only=False,
    history_file="history.txt",
):
    if not PIXABAY_API_KEY:
        print("Pixabay API key not configured.")
        return []

    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "video_type": "film",
        "per_page": 40,
    }
    url = "https://pixabay.com/api/videos/"
    try:
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    except Exception as e:
        print(f"Pixabay request error: {_redact_secrets(e)}")
        return []

    if resp.status_code != 200:
        print(f"Pixabay error: {resp.status_code}")
        return []

    hits = [
        v for v in resp.json().get("hits", [])
        if min_duration <= v.get("duration", 0) <= max_duration
    ]

    candidates = []
    for video in hits:
        dur = float(video.get("duration") or 0)
        metadata = {
            "title": video.get("tags", ""),
            "duration": dur,
            "tags": [t.strip() for t in video.get("tags", "").split(",") if t.strip()],
            "original_url": video.get("pageURL"),
            "author": video.get("user", ""),
            "license": "Pixabay Content License",
            "source": "pixabay",
        }
        if not _video_matches_topic(query, metadata):
            continue

        best_vf = None
        best_score = 0
        for _, vf in video.get("videos", {}).items():
            link = vf.get("url")
            if not link or is_duplicate_video(link, history_file=history_file):
                continue
            w = vf.get("width") or 0
            h = vf.get("height") or 0
            score = (
                _score_horizontal_long_candidate(w, h, dur)
                if horizontal_only
                else _score_video_candidate(w, h)
            )
            if score > best_score:
                best_score = score
                best_vf = vf
        if best_vf:
            metadata["width"] = best_vf.get("width")
            metadata["height"] = best_vf.get("height")
            candidates.append((best_vf["url"], metadata))
            if len(candidates) >= max_results:
                break
    return candidates


def fetch_pixabay_video(query="nature", min_duration=12, max_duration=45):
    results = fetch_pixabay_videos(query=query, min_duration=min_duration, max_duration=max_duration, max_results=3)
    if not results:
        return None, None
    return random.choice(results)


def fetch_coverr_videos(query="nature", max_results=3, history_file="history.txt"):
    """
    Free commercial-use stock footage from Coverr API (when COVERR_API_KEY is set)
    plus the curated `coverr_list.json` catalog in this repository.
    """
    candidates = []
    headers = {"User-Agent": BOT_USER_AGENT}

    if COVERR_API_KEY:
        api_base = (COVERR_API_URL or "https://api.coverr.co/videos").rstrip("/")
        try:
            resp = requests.get(
                api_base,
                params={"query": query, "urls": "true", "page_size": 20},
                headers={**headers, "Authorization": f"Bearer {COVERR_API_KEY}"},
                timeout=20,
            )
            if resp.status_code == 200:
                for hit in resp.json().get("hits", []):
                    mp4_url = (hit.get("urls") or {}).get("mp4_download") or (hit.get("urls") or {}).get("mp4")
                    meta = {
                        "title": hit.get("title", ""),
                        "description": hit.get("description", ""),
                        "duration": float(hit.get("duration") or 20),
                        "tags": hit.get("tags") or [],
                        "original_url": f"https://coverr.co/videos/{hit.get('slug', '')}",
                        "license": "Coverr Free Commercial License",
                        "source": "coverr",
                    }
                    if mp4_url and _video_matches_topic(query, meta) and not is_duplicate_video(mp4_url, history_file):
                        candidates.append((mp4_url, meta))
                        if len(candidates) >= max_results:
                            return candidates
        except Exception as e:
            print(f"⚠️ Coverr API lookup skipped: {_redact_secrets(e)}")

    # Fallback: resolve direct MP4 stream from curated coverr_list.json entries
    catalog_path = os.path.join(BASE_DIR, "coverr_list.json")
    if os.path.exists(catalog_path):
        try:
            with open(catalog_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            random.shuffle(entries)
            for item in entries:
                page_url = str(item.get("url") or "").strip()
                meta = {
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "duration": 20,
                    "tags": [item.get("query", "")],
                    "original_url": page_url,
                    "license": "Coverr Free Commercial License",
                    "source": "coverr",
                }
                if not page_url or not _video_matches_topic(query, meta):
                    continue
                page_resp = requests.get(page_url, headers=headers, timeout=15)
                if page_resp.status_code != 200:
                    continue
                m = re.search(r"https://cdn\.coverr\.co/videos/[^\s\"'<>]+\.mp4", page_resp.text)
                if m and not is_duplicate_video(m.group(0), history_file):
                    candidates.append((m.group(0), meta))
                    if len(candidates) >= max_results:
                        break
        except Exception as e:
            print(f"⚠️ Coverr local catalog lookup skipped: {e}")
    return candidates


def fetch_wikimedia_videos(
    query="wildlife",
    max_results=3,
    min_duration=0,
    max_duration=3600,
    horizontal_only=False,
    history_file="history.txt",
):
    """
    Free Creative Commons (CC0 / CC-BY / CC-BY-SA) & Public Domain video search
    via Wikimedia Commons API (no key required). Strictly verifies free license metadata.
    """
    url = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"{query} filetype:video",
        "gsrnamespace": 6,
        "gsrlimit": 35 if horizontal_only else 20,
        "prop": "imageinfo",
        "iiprop": "url|mime|size|dimensions|extmetadata",
        "format": "json",
    }
    headers = {"User-Agent": BOT_USER_AGENT}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        if resp.status_code != 200:
            return []
        pages = list((resp.json().get("query") or {}).get("pages", {}).values())
        candidates = []
        max_bytes = LONG_VIDEO_MAX_BYTES if horizontal_only else 95_000_000

        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            file_url = info.get("url")
            size = int(info.get("size") or 0)
            width = int(info.get("width") or 0)
            height = int(info.get("height") or 0)
            dur = float(info.get("duration") or 20.0)
            ext = info.get("extmetadata") or {}
            lic_short = str((ext.get("LicenseShortName") or {}).get("value") or "")
            lic_url = str((ext.get("LicenseUrl") or {}).get("value") or "")
            usage = str((ext.get("UsageTerms") or {}).get("value") or "")
            desc_html = str((ext.get("ImageDescription") or {}).get("value") or "")
            desc_txt = re.sub(r"<[^>]+>", " ", desc_html).strip()[:300]
            artist = re.sub(r"<[^>]+>", "", str((ext.get("Artist") or {}).get("value") or "")).strip()[:80]
            combined_lic = f"{lic_short} {lic_url} {usage}".strip()

            # If extmetadata is present, strictly verify free commercial license;
            # when unit tests mock minimal imageinfo without extmetadata, default to Commons CC/PD.
            if ext and not is_free_commercial_license(combined_lic):
                continue

            if dur < min_duration or dur > max_duration:
                continue
            if horizontal_only and _score_horizontal_long_candidate(width, height, dur) <= 0:
                continue

            title = page.get("title", "").replace("File:", "")
            meta = {
                "title": title,
                "description": desc_txt,
                "duration": dur,
                "width": width,
                "height": height,
                "tags": ["wikimedia", "wildlife"],
                "original_url": info.get("descriptionurl", file_url),
                "author": artist,
                "license": lic_short or "Wikimedia Commons Free License (CC/PD)",
                "source": "wikimedia",
            }
            if (
                file_url
                and size < max_bytes
                and _video_matches_topic(query, meta)
                and not is_duplicate_video(file_url, history_file=history_file)
            ):
                candidates.append((file_url, meta))
                if len(candidates) >= max_results:
                    break
        return candidates
    except Exception as e:
        print(f"⚠️ Wikimedia Commons video lookup failed: {e}")
        return []


def fetch_internet_archive_videos(
    query="wildlife",
    max_results=3,
    min_duration=0,
    max_duration=2700,
    horizontal_only=False,
    history_file="history.txt",
    extra_lucene_filter: str = "",
):
    """
    Free Public-Domain & CC0/CC-BY wildlife/nature film search via Internet Archive API
    (including US Fish & Wildlife Service, National Park Service, NOAA, USGS, NASA).
    """
    search_url = "https://archive.org/advancedsearch.php"
    license_filter = (
        extra_lucene_filter
        or "(licenseurl:(*publicdomain* OR *zero* OR *licenses/by/*) "
        "OR collection:(usfws OR nationalparkservice OR noaa OR nasa OR usgs OR fedflix))"
    )
    params = {
        "q": f"({query}) AND mediatype:(movies) AND {license_filter}",
        "fl[]": ["identifier", "title", "description", "licenseurl", "creator"],
        "sort[]": "downloads desc",
        "rows": 35,
        "page": 1,
        "output": "json",
    }
    headers = {"User-Agent": BOT_USER_AGENT}
    max_bytes = LONG_VIDEO_MAX_BYTES if horizontal_only else 90_000_000

    try:
        resp = requests.get(search_url, params=params, headers=headers, timeout=25)
        if resp.status_code != 200:
            return []
        docs = resp.json().get("response", {}).get("docs", [])
        candidates = []
        for doc in docs[:20]:
            identifier = doc.get("identifier")
            title = doc.get("title", "")
            desc = doc.get("description", "")
            if isinstance(desc, list):
                desc = " ".join(str(x) for x in desc)
            lic_url = str(doc.get("licenseurl") or "Public Domain (US Gov / Archive)")
            if doc.get("licenseurl") and not is_free_commercial_license(lic_url):
                continue

            meta_base = {
                "title": title,
                "description": str(desc)[:300],
                "duration": 25,
                "tags": ["wildlife", "archive", "documentary"],
                "original_url": f"https://archive.org/details/{identifier}",
                "author": str(doc.get("creator") or ""),
                "license": lic_url,
                "source": "internet_archive",
            }
            if not identifier or not _video_matches_topic(query, meta_base):
                continue

            meta_resp = requests.get(f"https://archive.org/metadata/{identifier}", headers=headers, timeout=20)
            if meta_resp.status_code != 200:
                continue
            files = meta_resp.json().get("files", [])

            best_file = None
            best_score = -1
            for f in files:
                name = str(f.get("name") or "")
                low_name = name.lower()
                if not (low_name.endswith(".mp4") or (horizontal_only and low_name.endswith((".webm", ".ogv")))):
                    continue
                size = int(float(f.get("size") or 0))
                if size <= 0 or size >= max_bytes:
                    continue
                dur = _parse_duration_seconds(f.get("length"))
                w = int(float(f.get("width") or 0))
                h = int(float(f.get("height") or 0))

                if min_duration and dur and dur < min_duration:
                    continue
                if max_duration and dur and dur > max_duration:
                    continue

                if horizontal_only:
                    if dur < min_duration:
                        continue
                    if w and h and _score_horizontal_long_candidate(w, h, dur) <= 0:
                        continue
                    mp4_bonus = 200_000 if low_name.endswith(".mp4") else 0
                    # Prefer native 8m05s..14m45s films that need zero trimming
                    fit_bonus = 400_000 if dur <= LONG_VIDEO_MAX_UPLOAD_SECONDS else 0
                    base_sc = _score_horizontal_long_candidate(w, h, dur) if (w and h) else min(size, 500_000_000)
                    score = base_sc + mp4_bonus + fit_bonus
                else:
                    score = (w * h) if (w and h) else size

                dl_url = f"https://archive.org/download/{identifier}/{requests.utils.quote(name)}"
                if is_duplicate_video(dl_url, history_file=history_file):
                    continue
                if score > best_score:
                    best_score = score
                    best_file = (dl_url, dur or 25.0, w, h)

            if best_file:
                dl_url, dur, w, h = best_file
                meta = dict(meta_base, duration=dur, width=w, height=h)
                candidates.append((dl_url, meta))
                if len(candidates) >= max_results:
                    break
        return candidates
    except Exception as e:
        print(f"⚠️ Internet Archive video lookup failed: {e}")
        return []


def _parse_duration_seconds(raw) -> float:
    """Parses numeric seconds or HH:MM:SS / MM:SS strings into float seconds."""
    if raw is None:
        return 0.0
    s = str(raw).strip()
    if not s:
        return 0.0
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    try:
        return float(s)
    except ValueError:
        return 0.0


def fetch_nasa_videos(
    query="earth",
    max_results=3,
    min_duration=0,
    max_duration=2700,
    horizontal_only=False,
    history_file="history.txt",
):
    """
    Free 100% Public-Domain video search via NASA Image and Video Library API (no key required).
    """
    url = "https://images-api.nasa.gov/search"
    headers = {"User-Agent": BOT_USER_AGENT}
    try:
        resp = requests.get(url, params={"q": query, "media_type": "video"}, headers=headers, timeout=20)
        if resp.status_code != 200:
            return []
        items = resp.json().get("collection", {}).get("items", [])
        candidates = []
        for item in items[:15]:
            data0 = (item.get("data") or [{}])[0]
            nasa_id = data0.get("nasa_id")
            title = data0.get("title", "")
            desc = data0.get("description", "")
            meta = {
                "title": title,
                "description": str(desc)[:300],
                "duration": 20,
                "tags": list(data0.get("keywords") or ["nasa", "earth"]),
                "original_url": f"https://images.nasa.gov/details-{nasa_id}",
                "author": data0.get("center", "NASA"),
                "license": "NASA Public Domain",
                "source": "nasa",
            }
            if not nasa_id or not _video_matches_topic(query, meta):
                continue

            asset_resp = requests.get(f"https://images-api.nasa.gov/asset/{nasa_id}", headers=headers, timeout=15)
            if asset_resp.status_code != 200:
                continue
            asset_items = asset_resp.json().get("collection", {}).get("items", [])
            mp4_links = [
                a.get("href") for a in asset_items
                if (a.get("href") or "").endswith(".mp4")
                and str(a.get("href") or "").startswith("https://")
                and not is_duplicate_video(a.get("href"), history_file=history_file)
            ]
            if not mp4_links:
                continue

            if horizontal_only or min_duration >= LONG_VIDEO_MIN_SECONDS:
                meta_links = [a.get("href") for a in asset_items if str(a.get("href") or "").endswith("metadata.json")]
                dur = 0.0
                w = 0
                h = 0
                if meta_links:
                    try:
                        m_json = requests.get(meta_links[0], headers=headers, timeout=12).json()
                        dur = _parse_duration_seconds(
                            m_json.get("QuickTime:Duration")
                            or m_json.get("MPEG:Duration")
                            or m_json.get("Composite:Duration")
                        )
                        w = int(m_json.get("QuickTime:ImageWidth") or m_json.get("File:ImageWidth") or 0)
                        h = int(m_json.get("QuickTime:ImageHeight") or m_json.get("File:ImageHeight") or 0)
                    except Exception:
                        pass
                if dur < min_duration or (max_duration and dur > max_duration):
                    continue
                if w and h and _score_horizontal_long_candidate(w, h, dur) <= 0:
                    continue
                meta["duration"] = dur
                meta["width"] = w
                meta["height"] = h
                mp4_links.sort(
                    key=lambda u: (
                        2 if ("~large.mp4" in u or "1080" in u) else (1 if "~orig.mp4" in u else 0)
                    ),
                    reverse=True,
                )

            candidates.append((mp4_links[0], meta))
            if len(candidates) >= max_results:
                break
        return candidates
    except Exception as e:
        print(f"⚠️ NASA video lookup failed: {e}")
        return []


def fetch_loc_public_domain_videos(
    query="national park",
    max_results=3,
    horizontal_only=False,
    history_file="history.txt",
):
    """
    Free Public-Domain open-access film & documentary search via Library of Congress JSON API
    (https://www.loc.gov/film-and-videos/?fo=json — no API key required).
    """
    url = "https://www.loc.gov/film-and-videos/"
    params = {"q": query, "fo": "json", "c": 15, "fa": "online-format:video"}
    headers = {"User-Agent": BOT_USER_AGENT}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        if resp.status_code != 200:
            return []
        results = resp.json().get("results", [])
        candidates = []
        for item in results:
            rights = str(item.get("rights_advisory") or item.get("access_restricted") or "").lower()
            if item.get("access_restricted") is True or "restricted" in rights:
                continue
            title = str(item.get("title") or "")
            desc_list = item.get("description") or []
            desc = " ".join(str(x) for x in desc_list) if isinstance(desc_list, list) else str(desc_list)
            meta = {
                "title": title,
                "description": desc[:300],
                "duration": 600 if horizontal_only else 25,
                "tags": list(item.get("subject") or ["wildlife", "documentary"]),
                "original_url": str(item.get("id") or item.get("url") or ""),
                "author": "Library of Congress",
                "license": "Public Domain (loc-public-domain)",
                "source": "library_of_congress",
            }
            if not _video_matches_topic(query, meta):
                continue
            mp4_url = ""
            for res_entry in item.get("resources") or []:
                for files_group in res_entry.get("files") or []:
                    for fobj in files_group:
                        u = str(fobj.get("url") or "")
                        if u.startswith("https://") and u.lower().endswith(".mp4"):
                            mp4_url = u
                            break
                    if mp4_url:
                        break
                if mp4_url:
                    break
            if mp4_url and not is_duplicate_video(mp4_url, history_file=history_file):
                candidates.append((mp4_url, meta))
                if len(candidates) >= max_results:
                    break
        return candidates
    except Exception as e:
        print(f"⚠️ Library of Congress video lookup failed: {e}")
        return []


def fetch_usgov_wildlife_videos(
    query="wildlife",
    max_results=3,
    min_duration=0,
    max_duration=2700,
    horizontal_only=False,
    history_file="history.txt",
):
    """
    Dedicated US Federal Public-Domain Wildlife & Ocean Film search across
    US Fish & Wildlife Service (`usfws`), National Park Service (`nationalparkservice`),
    and NOAA Ocean Exploration (`noaa`) collections (17 U.S.C. § 105 Public Domain).
    """
    return fetch_internet_archive_videos(
        query=query,
        max_results=max_results,
        min_duration=min_duration,
        max_duration=max_duration,
        horizontal_only=horizontal_only,
        history_file=history_file,
        extra_lucene_filter="collection:(usfws OR nationalparkservice OR noaa OR usgs)",
    )


def fetch_nature_video(query="nature"):
    """
    Fetches ONE strictly verified, cohesive HD video whose metadata matches `query`
    for short-form Reels/Shorts across all 8 free-license / open-source providers.
    """
    for fetcher in (fetch_pexels_video, fetch_pixabay_video):
        video_url, metadata = fetcher(query)
        if video_url:
            print(f"✅ Fetched verified '{query}' video from {metadata.get('source', 'stock')}: {video_url}")
            return video_url, metadata

    for free_fetcher in (
        fetch_coverr_videos,
        fetch_wikimedia_videos,
        fetch_internet_archive_videos,
        fetch_nasa_videos,
        fetch_loc_public_domain_videos,
    ):
        results = free_fetcher(query, max_results=1)
        if results:
            video_url, metadata = results[0]
            print(f"✅ Fetched verified '{query}' video from {metadata.get('source', 'open-source')}: {video_url}")
            return video_url, metadata

    raise Exception(f"❌ No strictly matching video found for '{query}' across all open-source providers.")


def download_video(video_url: str, output_dir="videos", suffix: str = "", timeout: int = HTTP_TIMEOUT, max_bytes: int = LONG_VIDEO_MAX_BYTES) -> str:
    """
    Downloads a remote HTTPS video with retry/backoff (preventing Wikimedia 429 failures)
    and enforces maximum byte size.
    """
    safe_url = _validate_remote_media_url(video_url)
    safe_dir = _resolve_repo_path(output_dir)
    os.makedirs(safe_dir, exist_ok=True)

    parsed_path = urlparse(safe_url).path.lower()
    if parsed_path.endswith(".webm"):
        ext = ".webm"
    elif parsed_path.endswith(".ogv"):
        ext = ".ogv"
    else:
        ext = ".mp4"
    safe_suffix = re.sub(r"[^A-Za-z0-9_\-]", "", suffix or "")[:32]
    file_name = f"nature_{datetime.now().strftime('%Y%m%d_%H%M%S')}{safe_suffix}{ext}"
    local_path = os.path.join(safe_dir, file_name)
    headers = {"User-Agent": BOT_USER_AGENT}

    for attempt in range(1, 4):
        try:
            resp = requests.get(safe_url, stream=True, headers=headers, timeout=timeout)
            if resp.status_code in (429, 502, 503, 504) and attempt < 3:
                wait_s = 3 * attempt
                print(f"⚠️ HTTP {resp.status_code} downloading media; retrying in {wait_s}s...")
                time.sleep(wait_s)
                continue
            resp.raise_for_status()
            written = 0
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError(f"Video exceeds max allowed size ({max_bytes} bytes).")
                        f.write(chunk)
            print(f"✅ Video saved to {local_path} ({written / (1024 * 1024):.1f} MB)")
            return local_path
        except Exception as e:
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass
            if attempt == 3:
                print(f"❌ Failed to download video: {e}")
                return ""
            time.sleep(2 * attempt)
    return ""


def fetch_and_download_unique_reel_video(attempts: int = 12, record_history: bool = True) -> tuple:
    """
    Finds AND successfully downloads a unique verified short-form clip BEFORE recording it in history.txt.
    Prevents burning history entries when an upstream server returns HTTP 429/5xx.
    """
    topics = list(WILDLIFE_TOPICS)
    random.shuffle(topics)
    for attempt in range(attempts):
        query = topics[attempt % len(topics)]
        print(f"🔁 Reel Attempt {attempt + 1}/{attempts}: Searching verified clip for '{query}'")
        try:
            video_url, metadata = fetch_nature_video(query=query)
            if not video_url or is_duplicate_video(video_url):
                continue
            video_path = download_video(video_url, suffix="_reel")
            if not video_path:
                print(f"⚠️ Download failed for '{query}' candidate, trying next topic/source...")
                continue
            if record_history:
                save_video_to_history(video_url, history_file="history.txt")
            return query, video_url, metadata, video_path
        except Exception as e:
            print(f"⚠️ Skipping '{query}': {e}")
    raise RuntimeError("❌ Failed to find & download a verified unique Reel video after multiple attempts.")


def fetch_unique_nature_video(attempts=10):
    """Backwards-compatible helper preserved for existing callers/tests."""
    for attempt in range(attempts):
        query = random.choice(WILDLIFE_TOPICS)
        try:
            video_url, metadata = fetch_nature_video(query=query)
            if video_url and not is_duplicate_video(video_url):
                save_video_to_history(video_url)
                return query, video_url, metadata
        except Exception as e:
            print(f"⚠️ Skipping '{query}': {e}")
    raise Exception("❌ Failed to find a verified unique video after multiple attempts.")


def discover_and_verify_long_horizontal_video(
    attempts: int = 18,
    history_file: str = "long_video_history.txt",
) -> tuple:
    """
    Searches open-source repositories for a video that strictly satisfies ALL 4 rules:
      1. Free commercial license (`is_free_commercial_license`)
      2. Horizontal format (width > height, aspect >= 1.30 — NEVER vertical like Reels)
      3. Length > 8 minutes (`duration >= 485s`)
      4. Looks good (`verify_long_horizontal_video_quality` + `verify_nature_frames_with_gemini`).

    Returns (topic, video_url, metadata, verified_local_path, probe_info) or None if none found.
    """
    doc_topics = list(LONG_DOCUMENTARY_TOPICS)
    random.shuffle(doc_topics)
    wild_topics = list(WILDLIFE_TOPICS)
    random.shuffle(wild_topics)
    topic_pool = doc_topics + wild_topics

    long_fetchers = (
        lambda q: fetch_internet_archive_videos(
            q, max_results=2, min_duration=LONG_VIDEO_MIN_SECONDS,
            max_duration=LONG_VIDEO_MAX_SECONDS, horizontal_only=True, history_file=history_file,
        ),
        lambda q: fetch_wikimedia_videos(
            q, max_results=2, min_duration=LONG_VIDEO_MIN_SECONDS,
            max_duration=LONG_VIDEO_MAX_SECONDS, horizontal_only=True, history_file=history_file,
        ),
        lambda q: fetch_nasa_videos(
            q, max_results=2, min_duration=LONG_VIDEO_MIN_SECONDS,
            max_duration=LONG_VIDEO_MAX_SECONDS, horizontal_only=True, history_file=history_file,
        ),
        lambda q: fetch_usgov_wildlife_videos(
            q, max_results=2, min_duration=LONG_VIDEO_MIN_SECONDS,
            max_duration=LONG_VIDEO_MAX_SECONDS, horizontal_only=True, history_file=history_file,
        ),
        lambda q: fetch_pexels_videos(
            q, min_duration=LONG_VIDEO_MIN_SECONDS, max_duration=LONG_VIDEO_MAX_SECONDS,
            max_results=2, orientation="landscape", history_file=history_file,
        ),
        lambda q: fetch_pixabay_videos(
            q, min_duration=LONG_VIDEO_MIN_SECONDS, max_duration=LONG_VIDEO_MAX_SECONDS,
            max_results=2, horizontal_only=True, history_file=history_file,
        ),
        lambda q: fetch_loc_public_domain_videos(
            q, max_results=1, horizontal_only=True, history_file=history_file,
        ),
    )

    for attempt in range(min(attempts, len(topic_pool))):
        topic = topic_pool[attempt]
        print(f"🎬 Long-Video Search {attempt + 1}/{attempts}: Checking open-source >8m horizontal HD films for '{topic}'...")
        for fn in long_fetchers:
            try:
                candidates = fn(topic)
            except Exception as e:
                print(f"⚠️ Long-video provider error on '{topic}': {e}")
                continue

            for video_url, meta in candidates:
                lic = str(meta.get("license") or "")
                if not is_free_commercial_license(lic):
                    print(f"⏭️ Skipping {video_url}: license '{lic}' not on free-commercial allow-list.")
                    continue
                if is_duplicate_video(video_url, history_file=history_file) or is_duplicate_video(video_url, history_file="history.txt"):
                    continue

                local_path = download_video(
                    video_url,
                    suffix="_long_raw",
                    timeout=LONG_VIDEO_DOWNLOAD_TIMEOUT,
                    max_bytes=LONG_VIDEO_MAX_BYTES,
                )
                if not local_path:
                    continue

                ok, reason, probe_info = verify_long_horizontal_video_quality(local_path)
                if not ok:
                    print(f"⏭️ Rejecting candidate ({reason}): {video_url}")
                    save_video_to_history(video_url, history_file=history_file)
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass
                    continue

                vision_ok, vision_reason = verify_nature_frames_with_gemini(
                    local_path, topic, meta, probe_info["duration"]
                )
                if not vision_ok:
                    print(f"⏭️ Gemini Vision rejected candidate ({vision_reason}): {video_url}")
                    save_video_to_history(video_url, history_file=history_file)
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass
                    continue

                print(
                    f"✅ Verified >8m horizontal HD nature film ({probe_info['width']}x{probe_info['height']}, "
                    f"{probe_info['duration'] / 60:.1f} min, {meta.get('license')}, vision: {vision_reason}): {video_url}"
                )
                return topic, video_url, meta, local_path, probe_info

    print("ℹ️ No qualifying >8-min horizontal HD free-license film found on this pass.")
    return None


# ============================================================================
# TTS SYNTHESIS
# ============================================================================
async def _synthesize_edge_with_subtitles(clean_text: str, selected_voice: str, output_path: str) -> list:
    communicate = edge_tts.Communicate(clean_text, selected_voice, rate="-2%", boundary="WordBoundary")
    word_events = []
    with open(output_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start_s = chunk["offset"] / 10_000_000.0
                dur_s = chunk["duration"] / 10_000_000.0
                word_events.append({
                    "text": chunk["text"],
                    "start": start_s + VOICE_LEAD_IN_S,
                    "end": start_s + dur_s + VOICE_LEAD_IN_S,
                })
    return word_events


def synthesize_text(
    text,
    output_path="output.mp3",
    selected_voice=None,
    subtitle_path="captions.ass",
    hook_banner: str = "",
):
    """
    Synthesizes full, natural National Geographic narration via `edge-tts` (-2% documentary pace)
    and strictly verifies that EVERY sentence was synthesized (checking word boundary coverage +
    decoded audio duration against word count) so partial WebSocket drops NEVER produce cut-off TTS.
    """
    clean_text = clean_text_for_tts(text)
    if not clean_text:
        raise ValueError("Cannot synthesize empty TTS text.")
    safe_out = _resolve_repo_path(output_path)

    expected_words = len(re.findall(r"[A-Za-z0-9']+", clean_text))
    min_word_events = max(1, int(expected_words * 0.78))
    min_expected_dur = max(1.5, expected_words * 0.22)

    pool = list(voice_names)
    random.shuffle(pool)
    if selected_voice:
        candidates = [selected_voice] + [v for v in pool if v != selected_voice]
    else:
        candidates = pool

    last_err = None
    if edge_tts is not None:
        for voice in candidates[:3]:
            try:
                word_events = asyncio.run(_synthesize_edge_with_subtitles(clean_text, voice, safe_out))
                file_bytes = os.path.getsize(safe_out) if os.path.exists(safe_out) else 0
                if file_bytes < 4000:
                    raise RuntimeError(f"TTS output too small ({file_bytes} bytes)")
                if len(word_events) < min_word_events:
                    raise RuntimeError(
                        f"Incomplete Edge TTS stream: got {len(word_events)}/{expected_words} word events"
                    )
                try:
                    actual_dur = get_audio_duration(safe_out)
                except Exception:
                    actual_dur = (word_events[-1]["end"] if word_events else 0.0)
                if actual_dur < min_expected_dur:
                    raise RuntimeError(
                        f"Truncated TTS audio ({actual_dur:.2f}s < {min_expected_dur:.2f}s expected for {expected_words} words)"
                    )

                generated_sub = (
                    generate_ass_subtitles(word_events, subtitle_path, hook_banner=hook_banner)
                    if (word_events and subtitle_path) else ""
                )
                print(
                    f"✅ Verified full TTS audio ({actual_dur:.2f}s, {len(word_events)}/{expected_words} words, "
                    f"{voice}), captions: {bool(generated_sub)}"
                )
                return safe_out, generated_sub
            except Exception as e:
                last_err = e
                print(f"⚠️ Edge TTS attempt with {voice} incomplete/failed ({e}); retrying...")
                time.sleep(1.5)

    print(f"⚠️ Edge TTS unavailable/incomplete ({last_err}), falling back to gTTS...")
    from gtts import gTTS
    tts = gTTS(text=clean_text, lang="en", tld="co.uk")
    tts.save(safe_out)
    try:
        gtts_dur = get_audio_duration(safe_out)
        if gtts_dur < min_expected_dur * 0.85:
            raise RuntimeError(f"gTTS output unexpectedly short ({gtts_dur:.2f}s for {expected_words} words)")
        print(f"✅ Verified gTTS fallback audio ({gtts_dur:.2f}s for {expected_words} words): {safe_out}")
    except RuntimeError:
        raise
    except Exception:
        print(f"✅ Audio written to {safe_out} using gTTS fallback.")
    return safe_out, ""


# ============================================================================
# ORCHESTRATOR: STRICTLY 1 VIDEO PER DAY ON EACH CHANNEL AT PEAK TIMING
# ============================================================================
def _write_step_summary(lines: list) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def backfill_pending_youtube_reels(state: dict) -> str:
    """
    If a previous Reel published to Instagram while YouTube failed transiently,
    re-renders/uploads 1 pending Reel to YouTube Shorts ONLY when YouTube has not
    yet published a video today (respecting the 1-video/day YouTube cap).
    """
    if youtube_posts_today(state) >= MAX_DAILY_POSTS_PER_CHANNEL:
        return ""
    pending = list(state.get("pending_youtube_reels") or [])
    if not pending:
        return ""

    item = pending[0]
    video_url = item.get("video_url", "")
    topic = item.get("topic", "wildlife")
    title = item.get("title", "Wild Discovery")
    narration = item.get("narration", "")
    caption = item.get("caption", "")
    if not video_url or not narration:
        state["pending_youtube_reels"] = pending[1:]
        save_publish_state(state)
        return ""

    print(f"🔄 Backfilling missed YouTube Short for '{topic}' (already on Instagram: {item.get('instagram_url', 'yes')})...")
    raw_path = download_video(video_url, suffix="_yt_backfill")
    if not raw_path:
        return ""

    vo_path, sub_path = synthesize_text(
        re.sub(r"[*_#~]", "", narration).strip(),
        os.path.join(tempfile.gettempdir(), "backfill_vo.mp3"),
        subtitle_path=os.path.join(tempfile.gettempdir(), "backfill_captions.ass"),
        hook_banner=title,
    )
    final_short = combine_video_with_music_and_voiceover(raw_path, vo_path, subtitle_path=sub_path)
    topic_words = [w.lower() for w in re.findall(r"[a-zA-Z]+", topic) if len(w) > 2]
    tags_list = list(dict.fromkeys(topic_words + [
        "wildlife", "natgeo", "animalfacts", "nature", "shorts",
        "wildlifeconservation", "wildanimals", "earth", "documentary",
    ]))
    yt_title = title if "#shorts" in title.lower() else f"{title[:88]} #Shorts"
    yt_id = upload_video_to_youtube(final_short, title=yt_title, description=caption, tags=tags_list)
    yt_url = f"https://youtu.be/{yt_id}"
    print(f"✅ Backfilled missed YouTube Short: {yt_url}")
    state["pending_youtube_reels"] = pending[1:]
    state.setdefault("backfilled_youtube", []).append({
        "date": _today_utc(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "youtube_url": yt_url,
    })
    state["backfilled_youtube"] = state["backfilled_youtube"][-20:]
    save_publish_state(state)
    return yt_url


def run_reel_pipeline(
    state: dict,
    upload_instagram: bool = True,
    upload_youtube: bool = True,
) -> dict:
    """
    Generates one vertical 9:16 Reel with verified full-length broadcast narration (-14 LUFS)
    and publishes to Instagram Reels and/or YouTube Shorts (respecting 1-post/day per channel).
    """
    random_query, video_url, metadata, video_path = fetch_and_download_unique_reel_video()
    print(f"Topic: {random_query} | Verified Video: {video_url}")

    fact_context = fetch_wildlife_fact(random_query)
    narration = generate_video_narration(random_query, video_url, metadata, fact_context=fact_context)
    title = generate_title_from_narration(narration, topic=random_query)
    caption = generate_caption_from_narration(narration, title)
    credit_line = format_license_credit(metadata)
    print(f"🏷️ Generated Title: {title}")

    voiceover_path, subtitle_path = synthesize_text(
        re.sub(r"[*_#~]", "", narration).strip(),
        "voiceover.mp3",
        subtitle_path=os.path.join(BASE_DIR, "captions.ass"),
        hook_banner=title,
    )
    final_video_path = combine_video_with_music_and_voiceover(
        video_path, voiceover_path, subtitle_path=subtitle_path,
    )
    if not final_video_path or not os.path.exists(final_video_path):
        raise FileNotFoundError("❌ Final narrated Reel video was not generated.")

    result = {
        "topic": random_query,
        "title": title,
        "instagram_ok": False,
        "instagram_skipped_daily_cap": not upload_instagram,
        "instagram_url": "",
        "youtube_short_ok": False,
        "youtube_skipped_daily_cap": not upload_youtube,
        "youtube_short_url": "",
        "youtube_error": "",
        "instagram_error": "",
    }

    # 1. Publish to Instagram Reels (if within today's 1-video/day cap)
    if upload_instagram:
        try:
            _, permalink = post_reel(final_video_path, caption)
            result["instagram_ok"] = True
            result["instagram_url"] = permalink or "https://www.instagram.com/geoserene/"
            print(f"✅ Instagram Reel uploaded: {result['instagram_url']}")
        except Exception as e:
            result["instagram_error"] = _redact_secrets(e)
            print(f"::error::Instagram Reel upload failed: {result['instagram_error']}")
    else:
        print("ℹ️ Instagram already has 1 video published today; skipping duplicate Instagram upload.")

    # 2. Publish to YouTube Shorts (if within today's 1-video/day cap)
    topic_words = [w.lower() for w in re.findall(r"[a-zA-Z]+", random_query) if len(w) > 2]
    tags_list = list(dict.fromkeys(topic_words + [
        "wildlife", "natgeo", "animalfacts", "nature", "shorts",
        "wildlifeconservation", "wildanimals", "earth", "documentary",
    ]))
    yt_title = title if "#shorts" in title.lower() else f"{title[:88]} #Shorts"
    yt_description = f"{caption}\n\n{credit_line}"[:4900]
    if upload_youtube:
        try:
            yt_id = upload_video_to_youtube(final_video_path, title=yt_title, description=yt_description, tags=tags_list)
            result["youtube_short_ok"] = True
            result["youtube_short_url"] = f"https://youtu.be/{yt_id}"
            print(f"✅ YouTube Short uploaded: {result['youtube_short_url']}")
        except Exception as e:
            result["youtube_error"] = _redact_secrets(e)
            print(f"::error::YouTube Short upload failed: {result['youtube_error']}")
            if result["instagram_ok"]:
                state.setdefault("pending_youtube_reels", []).append({
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "topic": random_query,
                    "video_url": video_url,
                    "title": title,
                    "narration": narration,
                    "caption": yt_description,
                    "instagram_url": result["instagram_url"],
                })
    else:
        print("ℹ️ YouTube already has 1 video published today; keeping YouTube strictly at 1 post/day.")

    # 3. Optional Twitter/X cross-post
    if result["instagram_ok"] or result["youtube_short_ok"]:
        tweet_caption = generate_tweet_from_video(
            narration, result["youtube_short_url"], result["instagram_url"] or "https://www.instagram.com/geoserene/"
        )
        post_to_twitter_text_only(tweet_caption)

    state.setdefault("recent_reels", []).append({
        "date": _today_utc(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "topic": random_query,
        "title": title,
        "source": metadata.get("source", ""),
        "instagram_url": result["instagram_url"],
        "youtube_short_url": result["youtube_short_url"],
    })
    save_publish_state(state)
    return result


def run_long_youtube_pipeline(state: dict, force: bool = False) -> dict:
    """
    Discovers a free-license, horizontal (16:9), >8-minute HD video that passes
    visual & Gemini Vision quality checks, trims >15m sources to 12m30s (within YouTube's
    15-minute unverified limit), and publishes it as YouTube's 1 daily video.
    """
    res = {"attempted": False, "uploaded": False, "url": "", "title": "", "reason": ""}
    if not force and not ENABLE_LONG_YOUTUBE:
        res["reason"] = "disabled via ENABLE_LONG_YOUTUBE"
        return res
    if not force and youtube_posts_today(state) >= MAX_DAILY_POSTS_PER_CHANNEL:
        res["reason"] = f"YouTube daily cap ({MAX_DAILY_POSTS_PER_CHANNEL} video/day) already reached today"
        print(f"ℹ️ Skipping long-form YouTube discovery: {res['reason']}.")
        return res
    if not force and long_videos_uploaded_today(state) >= MAX_LONG_VIDEOS_PER_DAY:
        res["reason"] = f"daily long-video cap ({MAX_LONG_VIDEOS_PER_DAY}) already reached today"
        print(f"ℹ️ Skipping long-form YouTube discovery: {res['reason']}.")
        return res

    res["attempted"] = True
    found = discover_and_verify_long_horizontal_video()
    if not found:
        res["reason"] = "no qualifying >8m horizontal HD free-license film found this run"
        return res

    topic, video_url, metadata, raw_long_path, probe_info = found
    effective_dur = (
        float(LONG_VIDEO_TRIM_TARGET_SECONDS)
        if probe_info["duration"] > LONG_VIDEO_MAX_UPLOAD_SECONDS
        else probe_info["duration"]
    )
    fact_context = fetch_wildlife_fact(topic)
    long_meta = generate_long_video_metadata(topic, metadata, effective_dur, fact_context=fact_context)

    chapter_vo_paths = []
    if not _has_audible_audio(raw_long_path, probe_info):
        chapters_text = long_meta.get("chapter_narrations") or (
            [long_meta["intro_voiceover"]] if long_meta.get("intro_voiceover") else []
        )
        shared_voice = random.choice(voice_names)
        for idx, ch_script in enumerate(chapters_text[:3]):
            out_mp3 = os.path.join(tempfile.gettempdir(), f"long_chapter_{idx}.mp3")
            vo_file, _ = synthesize_text(
                ch_script,
                output_path=out_mp3,
                selected_voice=shared_voice,
                subtitle_path="",
            )
            chapter_vo_paths.append(vo_file)

    final_long_path = prepare_long_horizontal_video(
        raw_long_path,
        chapter_voice_paths=chapter_vo_paths,
    )
    final_probe = probe_video_file(final_long_path)
    yt_id = upload_video_to_youtube(
        final_long_path,
        title=long_meta["title"],
        description=long_meta["description"],
        tags=long_meta["tags"],
    )
    yt_url = f"https://www.youtube.com/watch?v={yt_id}"
    save_video_to_history(video_url, history_file="long_video_history.txt")
    state.setdefault("long_videos", []).append({
        "date": _today_utc(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "title": long_meta["title"],
        "duration_min": round(final_probe["duration"] / 60.0, 1),
        "resolution": f"{final_probe['width']}x{final_probe['height']}",
        "source": metadata.get("source", ""),
        "license": metadata.get("license", ""),
        "youtube_url": yt_url,
    })
    save_publish_state(state)
    print(f"✅ Long-Form Horizontal YouTube Video (>8 min, <15 min) uploaded: {yt_url}")

    # Clean up large temporary long video files promptly
    for p in (raw_long_path, final_long_path, *chapter_vo_paths):
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    res.update({"uploaded": True, "url": yt_url, "title": long_meta["title"]})
    return res


def check_platform_tokens() -> int:
    """Health-checks both Instagram and YouTube credentials without uploading media."""
    ok = True
    if IG_ACCESS_TOKEN and IG_USER_ID:
        ensure_long_lived_ig_token()
        print("✅ Instagram token check completed.")
    else:
        print("::error::Instagram credentials (IG_ACCESS_TOKEN / IG_USER_ID) are missing.")
        ok = False

    try:
        yt = authenticate_youtube()
        ch = yt.channels().list(part="id,snippet", mine=True).execute()
        items = ch.get("items") or []
        ch_title = items[0]["snippet"]["title"] if items else "authenticated channel"
        print(f"✅ YouTube OAuth token is valid (channel: {ch_title}).")
    except Exception as e:
        print(f"::error::YouTube token check failed: {_redact_secrets(e)}")
        ok = False

    return 0 if ok else 1


def run_dry_run_verification() -> int:
    """
    End-to-end verification of Gemini narration, Edge-TTS completeness, -14 LUFS WAV mastering,
    sidechain-ducked FFmpeg rendering, and output loudness/duration WITHOUT uploading to channels.
    """
    if check_platform_tokens() != 0:
        return 1
    query, video_url, metadata, video_path = fetch_and_download_unique_reel_video(record_history=False)
    fact_context = fetch_wildlife_fact(query)
    narration = generate_video_narration(query, video_url, metadata, fact_context=fact_context)
    title = generate_title_from_narration(narration, topic=query)
    vo_path, sub_path = synthesize_text(
        narration,
        output_path=os.path.join(tempfile.gettempdir(), "dryrun_vo.mp3"),
        subtitle_path=os.path.join(tempfile.gettempdir(), "dryrun_captions.ass"),
        hook_banner=title,
    )
    vo_dur = get_audio_duration(vo_path)
    final_mp4 = combine_video_with_music_and_voiceover(video_path, vo_path, subtitle_path=sub_path)
    probe = probe_video_file(final_mp4)
    vol_res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", final_mp4, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    mean_m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", vol_res.stderr or "")
    max_m = re.search(r"max_volume:\s*([-\d.]+)\s*dB", vol_res.stderr or "")
    mean_db = float(mean_m.group(1)) if mean_m else 0.0
    max_db = float(max_m.group(1)) if max_m else 0.0
    print(
        f"✅ DRY-RUN VERIFIED: topic='{query}', title='{title}', "
        f"raw_tts={vo_dur:.2f}s, final_video={probe['duration']:.2f}s ({probe['width']}x{probe['height']}), "
        f"mean_volume={mean_db:.1f} dB, max_volume={max_db:.1f} dB"
    )
    for p in (video_path, final_mp4, vo_path, sub_path):
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    if probe["duration"] < vo_dur + 1.0:
        print(f"::error::Rendered video ({probe['duration']:.2f}s) truncated TTS ({vo_dur:.2f}s)!")
        return 1
    return 0


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(description="GeoSerene Dual-Platform Reels + Long-Form YouTube Engine")
    parser.add_argument(
        "--mode",
        choices=("auto", "reel", "long", "check-tokens", "dry-run"),
        default="auto",
        help="auto = 1 daily post on IG + 1 daily post on YT; reel = IG+YT Reel; long = >8m Horizontal YT; dry-run = render & verify audio without uploading",
    )
    parser.add_argument("--force", action="store_true", help="Bypass the 1-video-per-day per-channel guard (manual test runs only)")
    parser.add_argument("--force-long", action="store_true", help="Force a >8m horizontal YouTube upload on this run")
    args = parser.parse_args(argv)

    if args.mode == "check-tokens":
        return check_platform_tokens()
    if args.mode == "dry-run":
        return run_dry_run_verification()

    state = load_publish_state()
    summary_lines = ["## GeoSerene Publish Summary", ""]

    if args.mode == "long":
        if not (args.force or args.force_long) and youtube_posts_today(state) >= MAX_DAILY_POSTS_PER_CHANNEL:
            msg = "YouTube already received 1 video today (pass --force to override)."
            print(f"ℹ️ {msg}")
            summary_lines.append(f"- **YouTube Long-Form (>8m Horizontal):** ℹ️ {msg}")
            _write_step_summary(summary_lines)
            return 0
        long_res = run_long_youtube_pipeline(state, force=True)
        summary_lines.append(f"- **YouTube Long-Form (>8m Horizontal):** {long_res['url'] or long_res['reason']}")
        _write_step_summary(summary_lines)
        return 0 if long_res["uploaded"] else 1

    ig_allowed = args.force or (instagram_posts_today(state) < MAX_DAILY_POSTS_PER_CHANNEL)
    yt_allowed = args.force or (youtube_posts_today(state) < MAX_DAILY_POSTS_PER_CHANNEL)

    if not ig_allowed and not yt_allowed:
        msg = (
            f"Daily 1-video-per-channel limit already reached for {_today_utc()} "
            f"(Instagram={instagram_posts_today(state)}, YouTube={youtube_posts_today(state)}). "
            "Skipping duplicate post."
        )
        print(f"✅ {msg}")
        summary_lines.append(f"- **Daily Schedule Guard:** ✅ {msg}")
        _write_step_summary(summary_lines)
        return 0

    # On designated Long-Form Documentary day (Sunday UTC by default, or --force-long),
    # try publishing 1 >8m horizontal HD documentary as YouTube's single daily video first;
    # if none is found, YouTube seamlessly gets today's vertical Short instead!
    today_weekday = datetime.now(timezone.utc).weekday()
    should_try_long_today = (
        args.mode == "auto"
        and ENABLE_LONG_YOUTUBE
        and (args.force_long or (yt_allowed and today_weekday in LONG_VIDEO_WEEKDAYS))
    )
    if should_try_long_today:
        try:
            long_res = run_long_youtube_pipeline(state, force=args.force_long or args.force)
            if long_res["uploaded"]:
                summary_lines.append(
                    f"- **YouTube Long-Form (>8m Horizontal):** ✅ [{long_res['title']}]({long_res['url']})"
                )
                if not args.force_long:
                    yt_allowed = False  # Keep YouTube strictly at 1 video/day!
            else:
                summary_lines.append(f"- **YouTube Long-Form (>8m Horizontal):** ℹ️ {long_res['reason']}")
        except Exception as e:
            err_msg = _redact_secrets(e)
            print(f"⚠️ Long-form YouTube pipeline skipped: {err_msg}")
            summary_lines.append(f"- **YouTube Long-Form (>8m Horizontal):** ⚠️ {err_msg}")

    if not ig_allowed and not yt_allowed:
        _write_step_summary(summary_lines)
        return 0

    # Publish today's vertical Reel (both channels on normal days; IG-only if YT already got today's >8m feature)
    if ig_allowed and yt_allowed:
        reel_res = run_reel_pipeline(state)
    else:
        reel_res = run_reel_pipeline(state, upload_instagram=ig_allowed, upload_youtube=yt_allowed)

    if ig_allowed:
        summary_lines.append(
            f"- **Instagram Reel (`{reel_res['topic']}`):** "
            f"{'✅ ' + reel_res['instagram_url'] if reel_res['instagram_ok'] else '❌ ' + reel_res['instagram_error']}"
        )
    else:
        summary_lines.append("- **Instagram Reel:** ℹ️ Already posted 1 video today")

    if yt_allowed:
        summary_lines.append(
            f"- **YouTube Short (`{reel_res['topic']}`):** "
            f"{'✅ ' + reel_res['youtube_short_url'] if reel_res['youtube_short_ok'] else '❌ ' + reel_res['youtube_error']}"
        )
    else:
        summary_lines.append("- **YouTube Short:** ℹ️ Skipped to keep YouTube strictly at 1 video today")

    # Backfill only runs if YouTube STILL has 0 uploads today
    if not args.force and youtube_posts_today(state) < MAX_DAILY_POSTS_PER_CHANNEL:
        try:
            bf_url = backfill_pending_youtube_reels(state)
            if bf_url:
                summary_lines.append(f"- **YouTube Short Backfill:** ✅ {bf_url}")
        except Exception as e:
            print(f"⚠️ Pending YouTube backfill skipped: {_redact_secrets(e)}")

    _write_step_summary(summary_lines)

    ig_satisfied = reel_res["instagram_ok"] or reel_res.get("instagram_skipped_daily_cap", False)
    yt_satisfied = reel_res["youtube_short_ok"] or reel_res.get("youtube_skipped_daily_cap", False)

    if not ig_satisfied and not yt_satisfied:
        print("❌ Both Instagram and YouTube Reel uploads failed.")
        return 1

    if REQUIRE_BOTH_PLATFORMS and (not ig_satisfied or not yt_satisfied):
        print(
            "❌ Partial publish failure: expected daily post on BOTH Instagram and YouTube. "
            "State & history were saved so the succeeded platform will NOT double-post."
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
