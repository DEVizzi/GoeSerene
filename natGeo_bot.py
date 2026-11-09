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


NATURE_QUERIES = [
    # Landscapes & Natural Phenomena
    "rainforest canopy drone", "sunset over lake", "snowy mountain peaks", "crashing ocean waves",
    "desert dunes wind", "aurora borealis timelapse", "volcanic eruption", "sandstorm in desert",
    "glacier melting timelapse", "cloud inversion in mountains", "lightning storm over hills",
    "starry night sky over mountains", "fog rolling over hills", "moonlight over ocean",
    "golden hour landscape", "rainbow over valley", "waterfall in rainforest", "frozen waterfall",
    "geysers erupting", "ice cave interior",

    # Wildlife & Animals
    "slow motion butterflies", "birds flying over cliffs", "wild horses running", "elephants at watering hole",
    "underwater sea turtles", "humpback whales breaching", "lions resting in savannah",
    "monkeys swinging in trees", "eagles soaring over peaks", "penguins walking on ice",
    "fox in snowy forest", "timelapse of ants", "deer in misty forest", "owls in moonlight",

    # Aerial & Drone Shots
    "tropical island aerial", "canyon drone flyover", "forest from above", "coastline aerial view",
    "aerial of winding river", "mountain pass from drone", "hot air balloons over valley",

    # Seasonal Themes
    "autumn leaves falling", "heavy snowfall in woods", "spring blossoms", "summer meadow breeze",
    "winter forest silence", "first frost in morning light", "fall colors from above",

    # Water & Weather
    "ocean waves crashing on rocks", "river stream with rocks", "storm clouds rolling in",
    "thunderstorm sky timelapse", "tide pools with sea life", "rain drops on leaves close-up",
    "rippling lake at sunrise", "flooded forest reflection", "underwater coral reef",

    # Macro & Close-Up
    "close-up raindrops on leaves", "bees pollinating flowers", "snail crawling on moss",
    "dew on spider web", "macro of flower opening", "insects on wildflowers", "butterfly on leaf macro",

    # Forest & Flora
    "foggy morning forest", "sun rays breaking through trees", "lush jungle greenery",
    "pine forest in snow", "moss-covered rocks in forest", "blossoming cherry trees",
    "dense bamboo forest", "wildflowers in alpine meadow", "ferns in rainforest floor",

    # Unique Environments
    "salt flats reflection", "lava flowing into ocean", "iceberg floating", "cave with waterfall",
    "mangrove swamp", "savannah sunset", "frozen lake reflections", "crystal clear mountain lake",

    # Atmospheric and Light Phenomena
    "sun halo in sky", "sunset through fog", "crescent moon in dusk sky", "double rainbow in valley", "golden mist sunrise",

    # Wildlife & Behaviors (filtered)
    "flamingos in salt lake", "dolphins jumping in ocean", "eagles catching fish",
    "bison crossing river", "sea otters floating", "polar bears in arctic", "herd of deer at dawn",
    "bird migration in sky", "baby animals in wild",

    # Fantasy & Mystical Moods (filtered)
    "enchanted forest with mist", "fairy light woods", "foggy swamp sunrise",
    "moonlight path in forest", "twilight sky with silhouettes", "mystic mountain valley",

    # Natural Textures and Elements (filtered)
    "bark textures close-up", "dripping water in cave", "ripples on water surface", "tree roots exposed", "lichen growing on stone", "flowing sand in desert",

    # Weather Extremes & Drama (filtered)
    "sun piercing storm clouds", "wild wind in tall grass",

    # Biomes & Unique Locations
    "sahara desert sunset", "patagonia glacier view", "icelandic hot springs", "norwegian fjord drone",
    "new zealand rainforest", "grand canyon at dusk", "amazon river aerial", "antarctic ice shelf",
    "savannah storm chase", "alpine village in snow", "coastal cliffs with waves", "cenote underwater cave",

    # Natural Rhythms & Movement
    "leaves rustling in breeze", "waves lapping on shore", "bees swarming hive", "grass swaying in wind",
    "timelapse clouds over mountains", "sunlight flickering through leaves", "forest shadows moving",

    # Human-Nature Harmony
    "hiker on mountain ridge", "tent under starry sky", "campfire in forest clearing", "kayaking in calm lake",
    "yoga in nature sunrise", "treehouse in jungle", "canoe on misty river"
]


voice_names = [
    "en-US-ChristopherNeural",
    "en-US-GuyNeural",
    "en-US-EricNeural",
    "en-US-AndrewNeural",
    "en-US-BrianNeural",
    "en-GB-RyanNeural",
]


def get_audio_duration(audio_path: str) -> float:
    """Returns the duration of an audio file in seconds."""
    info = mediainfo(audio_path)
    return float(info["duration"])


def combine_video_with_music_and_voiceover(video_path: str, voiceover_path: str) -> str:
    """
    Combines the given video with soft background music and a voiceover.
    - If the video has no audio, only music and voiceover are mixed.
    - The voiceover is delayed slightly and mixed over background music.
    """
    output_path = video_path.replace(".mp4", "_final_narrated.mp4")
    music_dir = os.path.join(BASE_DIR, "music")
    music_files = [f for f in os.listdir(music_dir) if f.endswith(".mp3")]
    if not music_files:
        raise FileNotFoundError(f"No .mp3 files found in {music_dir}")
    music_path = os.path.join(music_dir, random.choice(music_files))

    temp_dir = tempfile.gettempdir()
    delayed_voice = os.path.join(temp_dir, "delayed_voice.mp3")

    # Step 1: Apply 1.5s delay + 3s fade-in to voiceover
    subprocess.run([
        "ffmpeg", "-y",
        "-i", voiceover_path,
        "-af", "adelay=1500|1500,afade=t=in:ss=0:d=3",
        delayed_voice
    ], check=True)

    # Step 2: Detect if input video has audio using ffmpeg
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", video_path],
        capture_output=True, text=True
    )
    include_video_audio = "Audio:" in (probe.stderr or "")

    # Step 3: Prepare filter_complex (normalize=0 prevents volume drops/jumps when voiceover ends)
    if include_video_audio:
        # Inputs: 0 = video, 1 = music, 2 = voiceover
        filter_complex = (
            "[0:a]volume=0.75[vid];"
            "[1:a]volume=0.35[music];"
            "[vid][music]amix=inputs=2:duration=first:normalize=0[bg];"
            "[bg][2:a]amix=inputs=2:duration=first:normalize=0[out]"
        )
        inputs = [video_path, music_path, delayed_voice]
    else:
        # Inputs: 0 = video, 1 = voiceover, 2 = music
        filter_complex = (
            "[2:a]volume=0.35[music];"
            "[music][1:a]amix=inputs=2:duration=first:normalize=0[out]"
        )
        inputs = [video_path, delayed_voice, music_path]

    cmd = [
        "ffmpeg", "-y",
        "-i", inputs[0],  # original video
        "-i", inputs[1],  # music or delayed voiceover
        "-i", inputs[2],  # voiceover or music
        "-filter_complex",
        f"{filter_complex};[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v]",
        "-map", "[v]",       # ✅ only the scaled/cropped 9:16 video stream
        "-map", "[out]",     # ✅ final mixed audio stream
        "-c:v", "libx264",
        "-preset", "fast",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-b:v", "5500k",
        "-maxrate", "6000k",
        "-bufsize", "12000k",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-shortest",
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

    # Calculate how many characters are left for the poem
    max_poem_chars = 280 - reserved_length

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
            "categoryId": "22"
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


def post_reel(video_path: str, caption: str, cover_url: str = None) -> tuple:
    ensure_long_lived_ig_token()
    container_id, upload_url = init_reel_upload(caption, cover_url)
    upload_reel_video(upload_url, video_path)
    wait_for_media_container(container_id)
    res = publish_media_container(container_id)
    media_id = res["id"]
    permalink = get_media_permalink(media_id)
    print(f"✅ Reel posted successfully (media ID: {media_id})")
    return media_id, permalink
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
    # Replace commas with period + space (to induce a pause without saying "comma")
    text = text.replace(",", ". ")
    # Remove other special characters except ., !, ?
    text = re.sub(r"[^\w\s.!?]", "", text)
    # Collapse multiple spaces
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# Function: Generate NatGeo Narration using Gemini
def generate_video_narration(nature: str, video_url: str, metadata: dict) -> str:
    """
    Generates a Nat Geo-style narration for the given video metadata.
    Narration duration is matched to video length (for TTS sync).
    """
    # Extract semantic context safely from original_url or title
    original_url = (metadata.get("original_url") or "").rstrip("/")
    raw_title = original_url.split("/")[-1] if "/" in original_url else metadata.get("title", "")
    # Remove trailing hyphenated numeric ID if present
    cleaned_title = re.sub(r"-\d+$", "", raw_title).replace("-", " ")
    content_hint = cleaned_title.strip().capitalize() or "A natural scene"

    duration = min(metadata.get("duration") or 15, 60)  # cap narration target at 60s
    tags = metadata.get("tags", [])
    tag_text = ", ".join(tags) if tags else "natural elements"

    # Estimate max narration word count
    words_per_second = 2  # slightly slower than normal for TTS clarity
    max_words = int(duration * words_per_second * 0.85)  # safety margin so voiceover finishes before video ends

    prompt = f"""
You are a poetic and cinematic National Geographic narrator.

Write a narration of approximately {max_words} words (±10%) for a nature video based on the search query:
"{nature}", with a title inferred as "{content_hint}", and featuring elements such as {tag_text}.

Narration should be timed for a {duration}-second voiceover (for Google TTS).
The tone must be awe-inspiring, poetic, and educational — without mentioning video, camera, or film.
Use imagery, emotion, and metaphor.

Do not exceed the time limit. Use a slow, steady narration pace.

Example:
"Bathed in golden sunlight, the earth whispers in sandstone — shaped by eons, silent and eternal."

Now write the narration.
    """

    messages = [
        SystemMessage(content="You are a poetic narrator writing a National Geographic voiceover."),
        HumanMessage(content=prompt)
    ]

    response = llm.invoke(messages)
    narration = response.content.strip()

    return narration


def generate_caption_from_narration(narration: str, title: str) -> str:
    """
    Generates a clean Instagram caption from narration and title,
    ensuring it's under 5000 characters and free of HTML/markdown formatting.
    """
    prompt = f"""
You're a social media strategist for National Geographic.

Given the following:

TITLE: {title}
NARRATION: {narration}

Craft an Instagram caption with this structure:

1. Start with the cinematic TITLE (title case, no formatting or HTML)
2. Follow with the poetic narration
3. Add a short, emotional call-to-action like:
   “Follow @geoserene for more stories written by wind and time.”
4. Use an emoji divider like: 🌍🌿✨ ———— ✨🌿🌍
5. End with 15–25 relevant, trending hashtags, space-separated (not line-separated)

Rules:
- Do NOT include any markdown or HTML (e.g., no <center>, **, etc.)
- Keep the entire caption under 5000 characters.
- Return only the full caption.
    """

    messages = [
        SystemMessage(content="You're a seasoned social media strategist for National Geographic."),
        HumanMessage(content=prompt)
    ]

    response = llm.invoke(messages)
    caption = response.content.strip()

    # ✨ Cleanup: Strip HTML/markdown if any slipped through
    caption = re.sub(r"<[^>]+>", "", caption)        # Remove HTML tags
    caption = re.sub(r"[*_~`]", "", caption)         # Remove markdown (*, _, etc.)
    caption = re.sub(r"['\"“”]", lambda m: m.group(0).strip(), caption)

    # ✨ Enforce Instagram's 5000-char limit
    if len(caption) > 4990:
        caption = caption[:4990].rsplit(" ", 1)[0] + "…"

    return caption.strip()


def generate_title_from_narration(narration: str) -> str:
    """
    Generates a powerful, cinematic title (3–7 words) from a poetic nature narration.
    Returns clean, plain text suitable for YouTube or Instagram.
    """
    prompt = f"""
You are a National Geographic headline writer.

Given the following poetic narration:

\"\"\"{narration}\"\"\"

Generate a single, cinematic and emotional title (3 to 7 words max) that captures the essence of the scene.
It should feel powerful, poetic, and timeless — like a documentary title.
Avoid generic terms like "Nature", "Video", or "Scenery".

Return only the title. No HTML. No Markdown. No quotation marks.
    """

    messages = [
        SystemMessage(content="You are a National Geographic headline writer."),
        HumanMessage(content=prompt)
    ]

    response = llm.invoke(messages)
    raw_title = response.content.strip()

    # Remove any quotes, HTML tags, or markdown remnants
    clean_title = re.sub(r"<[^>]+>", "", raw_title)           # remove HTML tags
    clean_title = re.sub(r"[*_~`]", "", clean_title)          # remove markdown characters
    clean_title = re.sub(r"['\"“”]", "", clean_title)         # remove quote characters
    return clean_title.strip()


def fetch_pexels_video(query="nature", min_duration=15, max_duration=60):
    if not PEXELS_API_KEY:
        print("Pexels API key not configured.")
        return None, None

    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 30, "orientation": "portrait"}
    url = "https://api.pexels.com/videos/search"
    resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)

    if resp.status_code != 200:
        print(f"Pexels error: {resp.status_code}")
        return None, None

    videos = [
        v for v in resp.json().get("videos", [])
        if min_duration <= v.get("duration", 0) <= max_duration
    ]
    if not videos:
        print(f"No Pexels videos found between {min_duration}s and {max_duration}s.")
        return None, None

    # Shuffle videos so repeated queries still yield diverse results
    random.shuffle(videos)

    best_video_file = None
    best_video_meta = None
    best_quality = 0

    for video in videos:
        for vf in video.get("video_files", []):
            link = vf.get("link")
            if not link or is_duplicate_video(link):
                continue
            width = vf.get("width") or 0
            height = vf.get("height") or 0
            resolution = width * height
            is_vertical = height > width

            # Prefer HD/FullHD vertical videos; cap preference at 1080p so 4K horizontal never beats 1080p vertical
            if resolution >= 1280 * 720:
                res_score = min(resolution, 1920 * 1080)
                score = res_score + (10_000_000 if is_vertical else 0)
                if score > best_quality:
                    best_quality = score
                    best_video_file = vf
                    best_video_meta = video

    if not best_video_file:
        print("No suitable unique HD video found on Pexels.")
        return None, None

    print("📱 Using vertical video (portrait mode)" if best_video_file["height"] > best_video_file["width"]
          else "🖥️ Using horizontal video (landscape mode)")

    metadata = {
        "title": best_video_meta.get("url", "").rstrip("/").split("/")[-1].replace("-", " "),
        "duration": best_video_meta.get("duration"),
        "width": best_video_file.get("width"),
        "height": best_video_file.get("height"),
        "tags": [tag["title"] for tag in best_video_meta.get("tags", []) if isinstance(tag, dict) and "title" in tag],
        "original_url": best_video_meta.get("url")
    }

    return best_video_file["link"], metadata


def fetch_pixabay_video(query="nature", min_duration=15, max_duration=60):
    if not PIXABAY_API_KEY:
        print("Pixabay API key not configured.")
        return None, None

    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "video_type": "film",
        "per_page": 30
    }
    url = "https://pixabay.com/api/videos/"
    resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)

    if resp.status_code != 200:
        print(f"Pixabay error: {resp.status_code}")
        return None, None

    hits = [
        v for v in resp.json().get("hits", [])
        if min_duration <= v.get("duration", 0) <= max_duration
    ]
    if not hits:
        print(f"No Pixabay videos found between {min_duration}s and {max_duration}s.")
        return None, None

    random.shuffle(hits)

    best_video_file = None
    best_video_meta = None
    best_quality = 0

    for video in hits:
        for _, vf in video.get("videos", {}).items():
            link = vf.get("url")
            if not link or is_duplicate_video(link):
                continue
            width = vf.get("width") or 0
            height = vf.get("height") or 0
            resolution = width * height
            is_vertical = height > width

            if resolution >= 1280 * 720:
                res_score = min(resolution, 1920 * 1080)
                score = res_score + (10_000_000 if is_vertical else 0)
                if score > best_quality:
                    best_quality = score
                    best_video_file = vf
                    best_video_meta = video

    if not best_video_file:
        print("No suitable unique HD video found on Pixabay.")
        return None, None

    print("📱 Using vertical video (portrait mode)" if best_video_file["height"] > best_video_file["width"]
          else "🖥️ Using horizontal video (landscape mode)")

    metadata = {
        "title": best_video_meta.get("tags", ""),
        "duration": best_video_meta.get("duration"),
        "width": best_video_file["width"],
        "height": best_video_file["height"],
        "tags": [t.strip() for t in best_video_meta.get("tags", "").split(",") if t.strip()],
        "original_url": best_video_meta.get("pageURL")
    }

    return best_video_file["url"], metadata


def fetch_nature_video(query="nature"):
    """
    Tries Pexels first, then falls back to Pixabay.
    """
    video_url, metadata = fetch_pexels_video(query)
    if video_url:
        print("✅ Fetched video from Pexels:", video_url)
        return video_url, metadata

    video_url, metadata = fetch_pixabay_video(query)
    if video_url:
        print("✅ Fetched video from Pixabay:", video_url)
        return video_url, metadata

    raise Exception("❌ Failed to fetch a nature video from all sources.")


def download_video(video_url: str, output_dir="videos") -> str:
    os.makedirs(output_dir, exist_ok=True)
    file_name = f"nature_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    local_path = os.path.join(output_dir, file_name)
    try:
        resp = requests.get(video_url, stream=True, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1MB
                if chunk:
                    f.write(chunk)
        print(f"✅ Video saved to {local_path}")
        return local_path
    except Exception as e:
        print(f"❌ Failed to download video: {e}")
        return ""


def synthesize_text(text, output_path="output.mp3", selected_voice=None):
    # Clean text to avoid unwanted spoken characters
    clean_text = clean_text_for_tts(text)

    # Pick random voice if not provided
    if not selected_voice:
        selected_voice = random.choice(voice_names)

    try:
        communicate = edge_tts.Communicate(clean_text, selected_voice)
        asyncio.run(communicate.save(output_path))
        print(f"✅ Audio content written to {output_path} using Edge Neural TTS voice: {selected_voice}")
        return output_path
    except Exception as e:
        print(f"⚠️ Edge TTS failed ({e}), falling back to gTTS...")
        from gtts import gTTS
        tts = gTTS(text=clean_text, lang="en", tld="co.uk")
        tts.save(output_path)
        print(f"✅ Audio content written to {output_path} using gTTS fallback.")
        return output_path


def fetch_unique_nature_video(attempts=5):
    for attempt in range(attempts):
        query = random.choice(NATURE_QUERIES)
        print(f"🔁 Attempt {attempt+1}: Trying query '{query}'")
        try:
            video_url, metadata = fetch_nature_video(query=query)
            if video_url and not is_duplicate_video(video_url):
                save_video_to_history(video_url)
                print("✅ Unique video found.")
                return query, video_url, metadata
            else:
                print("⚠️ Duplicate or invalid video, retrying...")
        except Exception as e:
            print(f"❌ Error during fetch: {e}")
    raise Exception("❌ Failed to find a unique video after multiple attempts.")


# Main execution
if __name__ == "__main__":
    try:
        random_query, video_url, metadata = fetch_unique_nature_video()
        print(f"Fetched video URL: {video_url}")
        print(f"Video Metadata: {metadata}")
    except Exception as e:
        print(str(e))
        sys.exit(1)

    video_path = download_video(video_url)
    if not video_path:
        sys.exit(1)

    narration = generate_video_narration(random_query, video_url, metadata)
    print(f"Narration:\n{narration}")

    voiceover_path = synthesize_text(re.sub(r"[*_#~]", "", narration).strip(), "voiceover.mp3")

    # Generate title
    title = generate_title_from_narration(narration)
    print(f" title-->{title}")
    # Generate caption
    caption = generate_caption_from_narration(narration, title)
    print(f"Generated caption: {caption}")

    # 1. Make Video
    final_video_path = combine_video_with_music_and_voiceover(video_path, voiceover_path)

    # ✅ Validate final video
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

    # 3. Publish to YouTube
    tags_list = [
        "nature", "natgeo", "wildlife", "adventure", "travel",
        "explore", "beautiful", "amazing", "unforgettable",
        "naturelovers", "naturephotography"
    ]
    try:
        yt_id = upload_video_to_youtube(final_video_path, title=title, description=caption, tags=tags_list)
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

    

