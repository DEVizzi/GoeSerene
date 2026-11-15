import os
import sys
import time
import random
import requests
import subprocess
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from datetime import datetime
import asyncio
import re
import edge_tts
import tweepy
import tempfile
from pydub.utils import mediainfo


# Load environment variables
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# YouTube OAuth scope and token endpoint
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
# Instagram Graph API credentials
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
IG_USER_ID = os.getenv("IG_USER_ID")

BASE_URL = "https://graph.facebook.com/v19.0"
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
COVERR_API_URL = os.getenv("COVERR_API_URL")
COVERR_API_KEY = os.getenv("COVERR_API_KEY")

HTTP_TIMEOUT = 60


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
    def __init__(self, model: str = "models/gemini-flash-lite-latest", temperature: float = 0.7):
        self.model = model if model.startswith("models/") else f"models/{model}"
        self.temperature = temperature

    def invoke(self, messages: list) -> _GeminiResponse:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY environment variable is not set.")
        sys_parts = [m.content for m in messages if isinstance(m, SystemMessage)]
        user_parts = [m.content for m in messages if not isinstance(m, SystemMessage)]
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "\n\n".join(user_parts)}]}],
            "generationConfig": {"temperature": self.temperature},
        }
        if sys_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n".join(sys_parts)}]}
        for candidate_model in (self.model, "models/gemini-3.1-flash-lite", "models/gemini-flash-latest"):
            url = f"https://generativelanguage.googleapis.com/v1beta/{candidate_model}:generateContent?key={api_key}"
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return _GeminiResponse(text)
        resp.raise_for_status()


# Initialize Gemini Chat Model
llm = ChatGoogleGenerativeAI(
    model="models/gemini-flash-lite-latest",
    temperature=0.7
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

# Keep backwards-compatible alias
NATURE_QUERIES = WILDLIFE_TOPICS


voice_names = [
    "en-US-AndrewNeural",
    "en-GB-RyanNeural",
    "en-US-BrianNeural",
    "en-US-ChristopherNeural",
]


def _ffmpeg_has_filter(filter_name: str) -> bool:
    """Checks if the installed ffmpeg binary supports a given filter."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=10
        )
        return bool(re.search(rf"\b{re.escape(filter_name)}\b", res.stdout or ""))
    except Exception:
        return False


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
    - Uses WrapStyle: 0 (smart wrapping), Fontsize: 52, and 120px side margins so text
      NEVER overflows the 1080x1920 vertical frame.
    - Splits chunks at natural speech pauses (>0.18s gap) so sentences never bleed together.
    - Optionally displays a 2.6s top hook banner to stop scrollers in the first 2 seconds.
    """
    if not word_events:
        return ""

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

    # Group words into short chunks (max `words_per_chunk` words and <=18 chars per line),
    # breaking immediately whenever there is a speech pause (>0.18s) between words.
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
        # If multiple words exceed 14 chars total, stack them on two lines via \N
        if len(words_upper) > 1 and len(joined) > 14:
            mid = len(words_upper) // 2
            joined = "\\N".join([" ".join(words_upper[:mid]), " ".join(words_upper[mid:])])
        lines.append(
            f"Dialogue: 0,{_format_ass_time(start_s)},{_format_ass_time(end_s)},Default,,0,0,0,,{joined}\n"
        )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return ass_path


def get_audio_duration(audio_path: str) -> float:
    """Returns the duration of an audio file in seconds."""
    info = mediainfo(audio_path)
    return float(info["duration"])


def combine_video_with_music_and_voiceover(
    video_paths,
    voiceover_path: str,
    subtitle_path: str = "",
) -> str:
    """
    Combines one or more video clips with background music, immediate voiceover (0.1s lead-in),
    and optional burned-in ASS captions, normalized to 1080x1920 30fps SDR bt709.
    - When multiple clips are passed, each clip is sliced into fast-paced ~3s segments and concatenated.
    """
    if isinstance(video_paths, str):
        video_paths = [video_paths]
    if not video_paths:
        raise ValueError("At least one video path is required.")

    primary_video = video_paths[0]
    output_path = primary_video.replace(".mp4", "_final_narrated.mp4")
    music_dir = os.path.join(BASE_DIR, "music")
    music_files = [f for f in os.listdir(music_dir) if f.endswith(".mp3")]
    if not music_files:
        raise FileNotFoundError(f"No .mp3 files found in {music_dir}")
    music_path = os.path.join(music_dir, random.choice(music_files))

    voice_duration = get_audio_duration(voiceover_path)
    target_duration = max(voice_duration + 0.6, 10.0)

    temp_dir = tempfile.gettempdir()
    paced_voice = os.path.join(temp_dir, "paced_voice.mp3")

    # Start voiceover immediately (tiny 100ms lead-in) so the 0-2s hook hits right away
    subprocess.run([
        "ffmpeg", "-y",
        "-i", voiceover_path,
        "-af", "adelay=100|100",
        paced_voice
    ], check=True)

    # Build normalized 9:16 visual stream (multi-clip fast cuts if >1 clip provided)
    stitched_video = primary_video
    if len(video_paths) > 1:
        segment_duration = max(2.0, min(2.6, target_duration / len(video_paths)))
        clip_inputs = []
        filter_parts = []
        concat_labels = []
        for idx, vp in enumerate(video_paths):
            clip_inputs.extend(["-t", f"{segment_duration:.2f}", "-i", vp])
            filter_parts.append(
                f"[{idx}:v]fps=30,"
                "scale=1080:1920:force_original_aspect_ratio=increase:out_color_matrix=bt709,"
                "crop=1080:1920,setsar=1,"
                f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709[v{idx}]"
            )
            concat_labels.append(f"[v{idx}]")

        stitched_video = os.path.join(temp_dir, "stitched_visual.mp4")
        concat_fc = ";".join(filter_parts) + f";{''.join(concat_labels)}concat=n={len(video_paths)}:v=1:a=0[vout]"
        subprocess.run(
            ["ffmpeg", "-y", *clip_inputs, "-filter_complex", concat_fc, "-map", "[vout]",
             "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", stitched_video],
            check=True,
        )

    # Mix background music (low) + immediate voiceover
    filter_complex = (
        "[2:a]volume=0.22[music];"
        "[music][1:a]amix=inputs=2:duration=longest:normalize=0[out]"
    )
    inputs = [stitched_video, paced_voice, music_path]

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
        escaped_sub = subtitle_path.replace("\\", "/").replace(":", "\\:")
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
        "-b:a", "128k",
        "-ac", "2",
        "-ar", "44100",
        "-max_muxing_queue_size", "1024",
        "-movflags", "+faststart",
        output_path
    ]

    subprocess.run(cmd, check=True)
    print(f"✅ Final narrated video saved: {output_path}")
    return output_path


def post_to_twitter_text_only(caption: str) -> str:
    """
    Post a text-only tweet using Twitter API v2 (Free tier compliant).
    """
    try:
        client = tweepy.Client(
            bearer_token=os.getenv("TWITTER_BEARER_TOKEN"),
            consumer_key=os.getenv("TWITTER_API_KEY"),
            consumer_secret=os.getenv("TWITTER_API_SECRET"),
            access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
            access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
        )

        response = client.create_tweet(text=caption[:280])
        tweet_id = response.data["id"]
        return f"https://twitter.com/user/status/{tweet_id}"

    except Exception as e:
        print(f"⚠️ Twitter text post failed: {e}")
        return ""


def generate_tweet_from_video(poem: str, youtube_url: str = "", insta_url: str = ""):
    """
    Returns:
    - tweet_caption: A tweet-safe caption that includes the poem, CTA line, and both links, max 280 characters.
    """
    poem_clean = poem.strip()

    # Fixed CTA and links (reserve space)
    cta_line = '✨ Experience this poem in cinematic art:\n'
    base_links = f"🎬 {youtube_url}\n📸 {insta_url}"
    reserved_length = len(cta_line + base_links)

    # Calculate how many characters are left for the poem (including the separating newline)
    max_poem_chars = max(0, 280 - reserved_length - 1)

    # Trim poem if needed
    if len(poem_clean) > max_poem_chars:
        poem_clean = poem_clean[:max_poem_chars - 3].rstrip() + "..."

    tweet_caption = f"{poem_clean}\n{cta_line}{base_links}"
    return tweet_caption


#********************************Youtube Start************************************************************
def authenticate_youtube():
    """
    Authenticate to YouTube using refresh token flow.
    Requires env vars: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
    """
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri=TOKEN_URI,
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_video_to_youtube(video_path: str, title: str, description: str, tags: list = None) -> str:
    """
    Upload a video to YouTube and return the video ID.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    title = title.strip().replace("\n", " ")[:100] or "Cinematic Poem"

    youtube = authenticate_youtube()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": "15"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"📤 Upload progress: {int(status.progress() * 100)}%")

    return response["id"]

#********************************Youtube End**************************************************************


#*****************************INSTAGRAM***BEGIN***********************************
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
    - If it is a short-lived token or has < 45 days remaining (and FB_APP_SECRET is set),
      automatically exchanges it for a 60-day long-lived token (and permanent Page token if available)
      and saves the refreshed token to .env.
    """
    global IG_ACCESS_TOKEN
    if not IG_ACCESS_TOKEN:
        return ""

    fb_app_id = os.getenv("FB_APP_ID", "867332745608249")
    fb_app_secret = os.getenv("FB_APP_SECRET", "")
    app_token = f"{fb_app_id}|{fb_app_secret}" if fb_app_secret else IG_ACCESS_TOKEN

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
            print(f"⚠️ Instagram token is invalid/expired: {data.get('error', dbg_resp)}")
            return IG_ACCESS_TOKEN

        if expires_at == 0:
            print("🔒 Instagram token is a permanent non-expiring Page token.")
            return IG_ACCESS_TOKEN

        remaining_days = (expires_at - time.time()) / 86400 if expires_at else 0
        print(f"🔑 Instagram token expires in {remaining_days:.1f} days.")

        if remaining_days < 45 and fb_app_secret:
            print("🔄 Automatically exchanging/refreshing Instagram token for a 60-day / permanent token...")
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

                # Derive a permanent Page token (expires_at == 0) from the 60-day user token
                pages_resp = requests.get(
                    f"{BASE_URL}/me/accounts",
                    params={"fields": "id,name,access_token,instagram_business_account", "access_token": long_user_token},
                    timeout=HTTP_TIMEOUT,
                ).json()
                for page in pages_resp.get("data", []):
                    ig_acct = page.get("instagram_business_account", {}).get("id")
                    if (ig_acct == IG_USER_ID or page.get("name") == "GeoSerene") and page.get("access_token"):
                        final_token = page["access_token"]
                        print("✅ Obtained permanent non-expiring Page/Instagram access token via /me/accounts!")
                        break

                IG_ACCESS_TOKEN = final_token
                os.environ["IG_ACCESS_TOKEN"] = final_token
                _persist_env_var("IG_ACCESS_TOKEN", final_token)
                print("✅ Refreshed IG_ACCESS_TOKEN saved to .env.")
    except Exception as e:
        print(f"⚠️ Instagram token check/refresh warning: {e}")

    return IG_ACCESS_TOKEN


def init_reel_upload(caption: str, cover_url: str = None) -> tuple:
    endpoint = f"{BASE_URL}/{IG_USER_ID}/media"
    params = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "access_token": IG_ACCESS_TOKEN
    }
    if cover_url:
        params["cover_url"] = cover_url
    resp = requests.post(endpoint, data=params, timeout=HTTP_TIMEOUT).json()
    if "id" not in resp or "uri" not in resp:
        raise RuntimeError(f"Error initializing reel upload: {resp}")
    return resp["id"], resp["uri"]


def upload_reel_video(upload_url: str, video_path: str) -> None:
    file_size = os.path.getsize(video_path)
    headers = {
        "Authorization": f"OAuth {IG_ACCESS_TOKEN}",
        "offset": "0",
        "file_size": str(file_size)
    }
    with open(video_path, "rb") as f:
        resp = requests.post(upload_url, headers=headers, data=f, timeout=300)
    data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
    if not resp.ok or (data.get("success") is False):
        raise RuntimeError(f"Error uploading reel video: {data or resp.text}")


def wait_for_media_container(container_id: str, max_wait_seconds: int = 180, poll_interval: int = 5) -> None:
    """
    Polls the Instagram Graph API container status until it is FINISHED processing.
    Prevents Graph API error 9007 ('Media ID is not available') on publish.
    """
    endpoint = f"{BASE_URL}/{container_id}"
    params = {
        "fields": "status_code,status",
        "access_token": IG_ACCESS_TOKEN
    }
    elapsed = 0
    while elapsed < max_wait_seconds:
        resp = requests.get(endpoint, params=params, timeout=HTTP_TIMEOUT).json()
        status_code = resp.get("status_code")
        print(f"⏳ Instagram container {container_id} status: {status_code or resp}")
        if status_code == "FINISHED":
            return
        if status_code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram media container processing failed: {resp}")
        time.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"Timed out waiting for Instagram container {container_id} to finish processing.")


def publish_media_container(container_id: str) -> dict:
    endpoint = f"{BASE_URL}/{IG_USER_ID}/media_publish"
    params = {
        "creation_id": container_id,
        "access_token": IG_ACCESS_TOKEN
    }
    resp = requests.post(endpoint, data=params, timeout=HTTP_TIMEOUT).json()
    if "id" not in resp:
        raise RuntimeError(f"Error publishing media: {resp}")
    return resp


def get_media_permalink(media_id: str) -> str:
    """Fetches the public Instagram permalink for a published media ID."""
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
            print(f"⚠️ Instagram Reel attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                time.sleep(5)
    raise last_err
#*****************************INSTAGRAM***END*************************************


def is_duplicate_video(url: str, history_file: str = "history.txt") -> bool:
    """
    Check if the video URL already exists in the history file.
    """
    history_path = history_file if os.path.isabs(history_file) else os.path.join(BASE_DIR, history_file)
    if not os.path.exists(history_path):
        return False
    with open(history_path, "r", encoding="utf-8") as f:
        history = f.read().splitlines()
    return url.strip() in history


def save_video_to_history(url: str, history_file: str = "history.txt"):
    """
    Append a new video URL to the history file.
    """
    history_path = history_file if os.path.isabs(history_file) else os.path.join(BASE_DIR, history_file)
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(url.strip() + "\n")


def clean_text_for_tts(text: str) -> str:
    # Remove URLs
    text = re.sub(r"http\S+", "", text)
    # Remove hashtags and mentions
    text = re.sub(r"[@#]\w+", "", text)
    # Remove markdown/special characters while keeping natural punctuation (, . ! ? ' -)
    text = re.sub(r"[^\w\s.,!?'\-]", "", text)
    # Collapse multiple spaces
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def fetch_wildlife_fact(topic: str) -> dict:
    """
    Fetches a real, verified summary from the free Wikipedia REST API
    so Gemini grounds scripts in astonishing real wildlife facts.
    """
    slug = topic.strip().replace(" ", "_")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(slug)}"
    headers = {"User-Agent": "GeoSereneBot/1.0 (wildlife-educational-reels)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            extract = (data.get("extract") or "").strip()
            if extract:
                return {
                    "title": data.get("title", topic),
                    "description": data.get("description", ""),
                    "extract": extract[:900],
                }
    except Exception as e:
        print(f"⚠️ Wikipedia fact lookup skipped for '{topic}': {e}")
    return {"title": topic, "description": "", "extract": ""}


def _ensure_complete_sentences(text: str) -> str:
    """
    Removes ellipses (...) and drops any trailing incomplete sentence fragment
    so the narration never cuts off mid-sentence.
    """
    cleaned = re.sub(r"\.{2,}|…", ".", text or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return ""
    # If the text does not end with terminal punctuation, trim back to the last complete sentence
    if cleaned[-1] not in ".!?":
        last_punct = max(cleaned.rfind("."), cleaned.rfind("!"), cleaned.rfind("?"))
        if last_punct != -1:
            cleaned = cleaned[:last_punct + 1].strip()
        else:
            cleaned += "."
    return cleaned


# Function: Generate Viral Hook-First NatGeo Narration using Gemini
def generate_video_narration(nature: str, video_url: str, metadata: dict, fact_context: dict = None) -> str:
    """
    Generates a high-retention, hook-first National Geographic-style narration
    grounded in real wildlife facts and structured with complete sentences.
    """
    original_url = (metadata.get("original_url") or "").rstrip("/")
    raw_title = original_url.split("/")[-1] if "/" in original_url else metadata.get("title", "")
    cleaned_title = re.sub(r"-\d+$", "", raw_title).replace("-", " ")
    content_hint = cleaned_title.strip().capitalize() or nature

    duration = min(max(metadata.get("duration") or 20, 12), 32)
    tags = metadata.get("tags", [])
    tag_text = ", ".join(tags[:8]) if tags else "wild habitat"

    fact_extract = (fact_context or {}).get("extract", "")
    fact_block = f"\nVerified biological/scientific context:\n{fact_extract}\n" if fact_extract else ""

    target_words = max(28, min(int(duration * 2.1), 68))

    prompt = f"""
You are a viral National Geographic short-form documentary writer.

Write a punchy, high-retention voiceover script of 3 to 5 complete sentences (approximately {target_words} words) strictly about "{nature}" matching the scene "{content_hint}" (elements: {tag_text}).
{fact_block}
Strict Viral Structure:
1. FIRST SENTENCE (0-2s HOOK): Start immediately with a shocking, counter-intuitive, or high-stakes fact or question. NEVER start with "In the heart of", "Bathed in", or generic scenery descriptions.
2. MIDDLE (ESCALATION): Deliver 2-3 vivid, mind-blowing survival/behavioral facts in short, complete sentences.
3. FINAL SENTENCE (STRONG PAYOFF): End with a complete, powerful concluding sentence that delivers a memorable takeaway.

Rules:
- EVERY sentence MUST be 100% grammatically complete and end with a period (.) or question mark (?).
- NEVER use ellipses (...) or leave any sentence unfinished.
- Do NOT mention video, camera, lens, or film.
- Plain spoken text only (no stage directions, no brackets, no markdown, no emojis).

Now write the voiceover script.
    """

    messages = [
        SystemMessage(content="You write viral, scientifically grounded National Geographic short-form scripts with complete sentences."),
        HumanMessage(content=prompt)
    ]

    response = llm.invoke(messages)
    return _ensure_complete_sentences(response.content.strip())


def generate_caption_from_narration(narration: str, title: str) -> str:
    """
    Generates an SEO- and engagement-optimized Instagram/YouTube caption with
    searchable keywords, a comment-bait question, Save/Share CTA, and 8-12 targeted hashtags.
    """
    prompt = f"""
You're a viral Instagram Reels & YouTube Shorts SEO strategist for @geoserene.

Given:
TITLE: {title}
NARRATION: {narration}

Craft a high-reach, SEO-optimized caption with this exact structure:
1. SEO HOOK LINE: Start with the Title + 1 keyword-rich curiosity sentence summarizing what viewers are watching (helps Instagram/YouTube Search index the topic).
2. COMMENT TRIGGER: A blank line, then one intriguing, easy-to-answer question for viewers (e.g., "Would you want to witness this in person? Comment YES or NO 👇").
3. SAVE/SHARE CTA: "Save this for later & follow @geoserene for daily wild discoveries."
4. HASHTAGS: A blank line, then 8-12 focused, space-separated hashtags mixing broad + niche search terms (always include #wildlife #natgeo #animalfacts #naturefacts #shorts #reels).

Rules:
- Keep it advertiser-friendly and brand-safe (no profanity or explicit words that trigger shadowbans).
- Do NOT include markdown (*, _, `) or HTML.
- Keep total length under 1500 characters.
- Return only the full caption.
    """

    messages = [
        SystemMessage(content="You're a seasoned viral social media & SEO strategist for National Geographic."),
        HumanMessage(content=prompt)
    ]

    response = llm.invoke(messages)
    caption = response.content.strip()

    caption = re.sub(r"<[^>]+>", "", caption)
    caption = re.sub(r"[*_~`]", "", caption)
    caption = re.sub(r"['\"“”]", lambda m: m.group(0).strip(), caption)

    if len(caption) > 4990:
        caption = caption[:4990].rsplit(" ", 1)[0] + "…"

    return caption.strip()


def generate_title_from_narration(narration: str) -> str:
    """
    Generates a curiosity-driven, searchable, brand-safe title (4-8 words, <=55 chars)
    suitable for YouTube Shorts, Instagram Search, and the 2.6s top hook banner.
    """
    prompt = f"""
You are a viral YouTube Shorts & National Geographic SEO headline writer.

Given this narration:
\"\"\"{narration}\"\"\"

Write ONE curiosity-driven, search-friendly title (4 to 8 words, strictly under 55 characters) that makes people stop scrolling.
Include the specific animal or phenomenon name.
Keep it 100% advertiser-friendly and family-safe (no explicit/adult words that limit reach).
Avoid generic titles like "Beauty of Nature" or "Silent Wilderness".

Return only the title. No HTML. No Markdown. No quotation marks.
    """

    messages = [
        SystemMessage(content="You are a viral wildlife documentary headline writer."),
        HumanMessage(content=prompt)
    ]

    response = llm.invoke(messages)
    raw_title = response.content.strip()

    clean_title = re.sub(r"<[^>]+>", "", raw_title)
    clean_title = re.sub(r"[*_~`]", "", clean_title)
    clean_title = re.sub(r"['\"“”]", "", clean_title)
    return clean_title.strip()


_STOPWORDS = {"in", "on", "at", "of", "the", "a", "an", "with", "over", "under", "and"}


def _video_matches_topic(query: str, metadata: dict) -> bool:
    """
    Strictly verifies that every required keyword in `query` (or its singular/plural form)
    appears in the video's title, URL slug, or tags.
    Prevents fuzzy stock search mismatches (e.g., 'platypus' returning 'duck' videos).
    """
    req_words = [w for w in re.findall(r"[a-z]+", (query or "").lower()) if w not in _STOPWORDS]
    if not req_words:
        return True

    haystack_str = " ".join([
        str(metadata.get("title") or ""),
        " ".join(str(t) for t in (metadata.get("tags") or [])),
        str(metadata.get("original_url") or ""),
    ]).lower()
    haystack_tokens = set(re.findall(r"[a-z]+", haystack_str))

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
    """Scores a video file preferring 1080p vertical (portrait) HD clips."""
    resolution = (width or 0) * (height or 0)
    if resolution < 1280 * 720:
        return 0
    is_vertical = (height or 0) > (width or 0)
    return min(resolution, 1920 * 1080) + (10_000_000 if is_vertical else 0)


def fetch_pexels_videos(query="wildlife", min_duration=12, max_duration=45, max_results=5):
    if not PEXELS_API_KEY:
        print("Pexels API key not configured.")
        return []

    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 40, "orientation": "portrait"}
    url = "https://api.pexels.com/videos/search"
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    except Exception as e:
        print(f"Pexels request error: {e}")
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
        metadata = {
            "title": video.get("url", "").rstrip("/").split("/")[-1].replace("-", " "),
            "duration": video.get("duration"),
            "tags": [tag["title"] for tag in video.get("tags", []) if isinstance(tag, dict) and "title" in tag],
            "original_url": video.get("url"),
            "source": "pexels",
        }
        if not _video_matches_topic(query, metadata):
            continue

        best_vf = None
        best_score = 0
        for vf in video.get("video_files", []):
            link = vf.get("link")
            if not link or is_duplicate_video(link):
                continue
            score = _score_video_candidate(vf.get("width") or 0, vf.get("height") or 0)
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


def fetch_pixabay_videos(query="wildlife", min_duration=12, max_duration=45, max_results=5):
    if not PIXABAY_API_KEY:
        print("Pixabay API key not configured.")
        return []

    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "video_type": "film",
        "per_page": 40
    }
    url = "https://pixabay.com/api/videos/"
    try:
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    except Exception as e:
        print(f"Pixabay request error: {e}")
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
        metadata = {
            "title": video.get("tags", ""),
            "duration": video.get("duration"),
            "tags": [t.strip() for t in video.get("tags", "").split(",") if t.strip()],
            "original_url": video.get("pageURL"),
            "source": "pixabay",
        }
        if not _video_matches_topic(query, metadata):
            continue

        best_vf = None
        best_score = 0
        for _, vf in video.get("videos", {}).items():
            link = vf.get("url")
            if not link or is_duplicate_video(link):
                continue
            score = _score_video_candidate(vf.get("width") or 0, vf.get("height") or 0)
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


def fetch_nasa_videos(query="earth", max_results=3):
    """
    Free public-domain video search via NASA Image and Video Library API (no key required).
    """
    url = "https://images-api.nasa.gov/search"
    try:
        resp = requests.get(url, params={"q": query, "media_type": "video"}, timeout=20)
        if resp.status_code != 200:
            return []
        items = resp.json().get("collection", {}).get("items", [])
        candidates = []
        for item in items[:10]:
            nasa_id = (item.get("data") or [{}])[0].get("nasa_id")
            title = (item.get("data") or [{}])[0].get("title", "")
            meta = {
                "title": title,
                "duration": 20,
                "tags": ["nasa", "earth"],
                "original_url": f"https://images.nasa.gov/details-{nasa_id}",
                "source": "nasa",
            }
            if not nasa_id or not _video_matches_topic(query, meta):
                continue
            asset_resp = requests.get(f"https://images-api.nasa.gov/asset/{nasa_id}", timeout=15)
            if asset_resp.status_code != 200:
                continue
            mp4_links = [
                a.get("href") for a in asset_resp.json().get("collection", {}).get("items", [])
                if (a.get("href") or "").endswith(".mp4") and not is_duplicate_video(a.get("href"))
            ]
            if mp4_links:
                candidates.append((mp4_links[0], meta))
                if len(candidates) >= max_results:
                    break
        return candidates
    except Exception as e:
        print(f"⚠️ NASA video lookup failed: {e}")
        return []


def fetch_internet_archive_videos(query="wildlife", max_results=3):
    """
    Free public-domain wildlife/nature video search via Internet Archive API (no key required).
    """
    search_url = "https://archive.org/advancedsearch.php"
    params = {
        "q": f"({query}) AND mediatype:(movies)",
        "fl[]": ["identifier", "title"],
        "rows": 15,
        "page": 1,
        "output": "json",
    }
    try:
        resp = requests.get(search_url, params=params, timeout=20)
        if resp.status_code != 200:
            return []
        docs = resp.json().get("response", {}).get("docs", [])
        candidates = []
        for doc in docs[:8]:
            identifier = doc.get("identifier")
            title = doc.get("title", "")
            meta = {
                "title": title,
                "duration": 25,
                "tags": ["wildlife", "archive"],
                "original_url": f"https://archive.org/details/{identifier}",
                "source": "internet_archive",
            }
            if not identifier or not _video_matches_topic(query, meta):
                continue
            meta_resp = requests.get(f"https://archive.org/metadata/{identifier}", timeout=15)
            if meta_resp.status_code != 200:
                continue
            files = meta_resp.json().get("files", [])
            mp4_files = [
                f for f in files
                if (f.get("name") or "").lower().endswith(".mp4")
                and int(f.get("size") or 0) < 80_000_000
            ]
            if mp4_files:
                dl_url = f"https://archive.org/download/{identifier}/{mp4_files[0]['name']}"
                if not is_duplicate_video(dl_url):
                    candidates.append((dl_url, meta))
                    if len(candidates) >= max_results:
                        break
        return candidates
    except Exception as e:
        print(f"⚠️ Internet Archive video lookup failed: {e}")
        return []


def fetch_wikimedia_videos(query="wildlife", max_results=3):
    """
    Free Creative Commons / Public Domain video search via Wikimedia Commons API (no key required).
    """
    url = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"{query} filetype:video",
        "gsrnamespace": 6,
        "gsrlimit": 10,
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "format": "json",
    }
    headers = {"User-Agent": "GeoSereneBot/1.0 (wildlife-educational-reels)"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        if resp.status_code != 200:
            return []
        pages = list((resp.json().get("query") or {}).get("pages", {}).values())
        candidates = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            file_url = info.get("url")
            size = info.get("size") or 0
            title = page.get("title", "").replace("File:", "")
            meta = {
                "title": title,
                "duration": 20,
                "tags": ["wikimedia", "wildlife"],
                "original_url": info.get("descriptionurl", file_url),
                "source": "wikimedia",
            }
            if file_url and size < 80_000_000 and _video_matches_topic(query, meta) and not is_duplicate_video(file_url):
                candidates.append((file_url, meta))
                if len(candidates) >= max_results:
                    break
        return candidates
    except Exception as e:
        print(f"⚠️ Wikimedia Commons video lookup failed: {e}")
        return []


def fetch_nature_video(query="nature"):
    """
    Fetches ONE strictly verified, cohesive HD video whose metadata explicitly matches `query`.
    Tries Pexels, Pixabay, Wikimedia Commons, Internet Archive, and NASA in order.
    """
    for fetcher in (fetch_pexels_video, fetch_pixabay_video):
        video_url, metadata = fetcher(query)
        if video_url:
            print(f"✅ Fetched verified '{query}' video from {metadata.get('source', 'stock')}: {video_url}")
            return video_url, metadata

    for free_fetcher in (fetch_wikimedia_videos, fetch_internet_archive_videos, fetch_nasa_videos):
        results = free_fetcher(query, max_results=1)
        if results:
            video_url, metadata = results[0]
            print(f"✅ Fetched verified '{query}' video from {metadata.get('source', 'free-archive')}: {video_url}")
            return video_url, metadata

    raise Exception(f"❌ No strictly matching video found for '{query}' across all sources.")


def download_video(video_url: str, output_dir="videos", suffix: str = "") -> str:
    os.makedirs(output_dir, exist_ok=True)
    ext = ".mp4"
    file_name = f"nature_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}{ext}"
    local_path = os.path.join(output_dir, file_name)
    headers = {"User-Agent": "GeoSereneBot/1.0 (wildlife-educational-reels)"}
    try:
        resp = requests.get(video_url, stream=True, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        print(f"✅ Video saved to {local_path}")
        return local_path
    except Exception as e:
        print(f"❌ Failed to download video: {e}")
        return ""


async def _synthesize_edge_with_subtitles(clean_text: str, selected_voice: str, output_path: str) -> list:
    """Streams edge-tts audio + WordBoundary events to capture exact caption timestamps."""
    communicate = edge_tts.Communicate(clean_text, selected_voice, rate="+4%", boundary="WordBoundary")
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
                    "start": start_s + 0.1,  # match 100ms adelay lead-in
                    "end": start_s + dur_s + 0.1,
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
    Synthesizes speech using Edge Neural TTS and generates an ASS subtitle file
    from WordBoundary timestamps for free burned-in captions.
    Returns (audio_path, subtitle_path_or_empty).
    """
    clean_text = clean_text_for_tts(text)

    if not selected_voice:
        selected_voice = random.choice(voice_names)

    try:
        word_events = asyncio.run(_synthesize_edge_with_subtitles(clean_text, selected_voice, output_path))
        generated_sub = (
            generate_ass_subtitles(word_events, subtitle_path, hook_banner=hook_banner)
            if word_events else ""
        )
        print(f"✅ Audio written to {output_path} ({selected_voice}), captions: {bool(generated_sub)}")
        return output_path, generated_sub
    except Exception as e:
        print(f"⚠️ Edge TTS failed ({e}), falling back to gTTS...")
        from gtts import gTTS
        tts = gTTS(text=clean_text, lang="en", tld="co.uk")
        tts.save(output_path)
        print(f"✅ Audio written to {output_path} using gTTS fallback.")
        return output_path, ""


def fetch_unique_nature_video(attempts=10):
    for attempt in range(attempts):
        query = random.choice(WILDLIFE_TOPICS)
        print(f"🔁 Attempt {attempt + 1}: Searching strictly verified clip for '{query}'")
        try:
            video_url, metadata = fetch_nature_video(query=query)
            if video_url and not is_duplicate_video(video_url):
                save_video_to_history(video_url)
                print(f"✅ Verified unique video found for '{query}' ({metadata.get('title')}).")
                return query, video_url, metadata
            else:
                print("⚠️ Duplicate or invalid video, retrying...")
        except Exception as e:
            print(f"⚠️ Skipping '{query}': {e}")
    raise Exception("❌ Failed to find a verified unique video after multiple attempts.")


# Main execution
if __name__ == "__main__":
    try:
        random_query, video_url, metadata = fetch_unique_nature_video()
        print(f"Topic: {random_query} | Verified Video: {video_url}")
        print(f"Video Metadata: {metadata}")
    except Exception as e:
        print(str(e))
        sys.exit(1)

    video_path = download_video(video_url)
    if not video_path:
        sys.exit(1)

    fact_context = fetch_wildlife_fact(random_query)
    narration = generate_video_narration(random_query, video_url, metadata, fact_context=fact_context)
    print(f"Narration:\n{narration}")

    title = generate_title_from_narration(narration)
    print(f" title-->{title}")
    caption = generate_caption_from_narration(narration, title)
    print(f"Generated caption: {caption}")

    voiceover_path, subtitle_path = synthesize_text(
        re.sub(r"[*_#~]", "", narration).strip(),
        "voiceover.mp3",
        subtitle_path=os.path.join(BASE_DIR, "captions.ass"),
        hook_banner=title,
    )

    # 1. Make Video (single cohesive verified video + immediate voiceover + burned-in captions)
    final_video_path = combine_video_with_music_and_voiceover(
        video_path, voiceover_path, subtitle_path=subtitle_path
    )

    if not final_video_path or not os.path.exists(final_video_path):
        raise FileNotFoundError("❌ Final narrated video was not generated correctly.")

    insta_url = "https://www.instagram.com/geoserene/"
    yt_url = ""
    published_any = False

    # 2. Publish to Instagram
    try:
        _, permalink = post_reel(final_video_path, caption)
        if permalink:
            insta_url = permalink
        published_any = True
        print("✅ Reel Uploaded.")
    except Exception as e:
        print(f"❌ Instagram Reel upload failed: {e}")

    # 3. Publish to YouTube Shorts
    topic_words = [w.lower() for w in re.findall(r"[a-zA-Z]+", random_query) if len(w) > 2]
    tags_list = list(dict.fromkeys(topic_words + [
        "wildlife", "natgeo", "animalfacts", "nature", "shorts",
        "wildlifeconservation", "wildanimals", "earth", "documentary"
    ]))
    yt_title = title if "#shorts" in title.lower() else f"{title[:88]} #Shorts"
    try:
        yt_id = upload_video_to_youtube(final_video_path, title=yt_title, description=caption, tags=tags_list)
        yt_url = f"https://youtu.be/{yt_id}"
        published_any = True
        print(f"✅ YouTube Shorts uploaded: {yt_url}")
    except Exception as e:
        print(f"❌ YouTube upload failed: {e}")

    # 4. Publish to Twitter (X)
    tweet_caption = generate_tweet_from_video(narration, yt_url, insta_url)
    tweet_url = post_to_twitter_text_only(tweet_caption)
    if tweet_url:
        published_any = True
        print(f"✅ Tweet done successfully: {tweet_url}")

    if not published_any:
        print("❌ All publishing targets failed.")
        sys.exit(1)


    

