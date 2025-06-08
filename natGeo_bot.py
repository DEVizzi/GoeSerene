import os
import random
import requests
import subprocess
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.schema import SystemMessage, HumanMessage

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from datetime import datetime
import re
from google.cloud import texttospeech
import tweepy
import tempfile
from pydub.utils import mediainfo
from pydub import AudioSegment


# Load environment variables
load_dotenv()

# YouTube OAuth scope and token endpoint
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
# Instagram Graph API credentials
IG_ACCESS_TOKEN=os.getenv("IG_ACCESS_TOKEN")
IG_USER_ID=os.getenv("IG_USER_ID")

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "myvoiceoverbot-a0da14b71e20.json"

BASE_URL = "https://graph.facebook.com/v17.0"
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
COVERR_API_URL = os.getenv("COVERR_API_URL")
COVERR_API_KEY=os.getenv("COVERR_API_KEY")

# Initialize Gemini Chat Model with a supported model name
llm = ChatGoogleGenerativeAI(
    model="models/gemini-1.5-flash",
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
    #"en-US-Chirp-HD-D",
    #"en-US-Chirp-HD-F",
    "en-US-Chirp3-HD-Algieba",
    "en-US-Chirp3-HD-Algieba",
    "en-US-Chirp3-HD-Aoede",
    #"en-US-Chirp3-HD-Erinome",
    "en-US-Chirp3-HD-Gacrux",
    "en-US-Chirp3-HD-Iapetus",
    #"en-US-Chirp3-HD-Laomedeia",
    #"en-US-Chirp3-HD-Pulcherrima",
    #"en-US-Chirp3-HD-Rasalgethi",
    #"en-US-Chirp3-HD-Sadaltager",
    "en-US-Chirp3-HD-Sulafat",
    #"en-US-Chirp3-HD-Vindemiatrix",
]


def get_audio_duration(audio_path: str) -> float:
    """Returns the duration of an audio file in seconds."""
    info = mediainfo(audio_path)
    return float(info['duration'])


def combine_video_with_music_and_voiceover(video_path: str, voiceover_path: str) -> str:
    """
    Combines the given video with soft background music and a voiceover.
    - If the video has no audio, only music and voiceover are mixed.
    - The voiceover is delayed slightly and mixed over background music at 50% volume.
    - The voiceover is time-aligned with the video duration (preprocessed before).
    """
    output_path = video_path.replace(".mp4", "_final_narrated.mp4")
    music_dir = "music"
    music_files = [f for f in os.listdir(music_dir) if f.endswith(".mp3")]
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

    # Step 2: Detect if input video has audio
    has_audio = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "default=nw=1", video_path
    ], capture_output=True, text=True)
    
    include_video_audio = "codec_type=audio" in has_audio.stdout

    # Step 3: Prepare filter_complex
    if include_video_audio:
        # Inputs: 0 = video, 1 = music, 2 = voiceover
        filter_complex = (
            "[0:a]volume=0.75[vid];"
            "[1:a]volume=0.35[music];"
            "[vid][music]amix=inputs=2:duration=longest[bg];"
            "[bg][2:a]amix=inputs=2:duration=first[out]"
        )
        inputs = [video_path, music_path, delayed_voice]
    else:
        # Inputs: 0 = video, 1 = voiceover, 2 = music
        filter_complex = (
            "[2:a]volume=0.35[music];"
            "[music][1:a]amix=inputs=2:duration=first[out]"
        )
        inputs = [video_path, delayed_voice, music_path]

    cmd = [
        "ffmpeg", "-y",
        "-i", inputs[0],  # original video
        "-i", inputs[1],  # music or delayed voiceover
        "-i", inputs[2],  # voiceover or music
        "-filter_complex",
        f"{filter_complex};[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1[v]",
        "-map", "[v]",       # ✅ only the scaled video stream
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

    #print(f"✅ Uploaded to YouTube: https://youtu.be/{response['id']}")
    return response["id"]

#********************************Youtube End**************************************************************

#*****************************INSTAGRAM***BEGIN***********************************
def init_reel_upload(caption: str, cover_url: str = None) -> (str, str):
    endpoint = f"{BASE_URL}/{IG_USER_ID}/media"
    params = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "access_token": IG_ACCESS_TOKEN
    }
    if cover_url:
        params["cover_url"] = cover_url
    resp = requests.post(endpoint, data=params).json()
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
        resp = requests.post(upload_url, headers=headers, data=f)
    data = resp.json() if resp.headers.get('Content-Type', '').startswith('application/json') else {}
    if not resp.ok or (data.get("success") is False):
        raise RuntimeError(f"Error uploading reel video: {data or resp.text}")

def publish_media_container(container_id: str) -> dict:
    endpoint = f"{BASE_URL}/{IG_USER_ID}/media_publish"
    params = {
        "creation_id": container_id,
        "access_token": IG_ACCESS_TOKEN
    }
    resp = requests.post(endpoint, data=params).json()
    if "id" not in resp:
        raise RuntimeError(f"Error publishing media: {resp}")
    return resp

def post_reel(video_path: str, caption: str, cover_url: str = None) -> str:
    container_id, upload_url = init_reel_upload(caption, cover_url)
    upload_reel_video(upload_url, video_path)
    res = publish_media_container(container_id)
    return f"✅ Reel posted successfully (media ID: {res['id']})"
#*****************************INSTAGRAM***END*************************************

def is_duplicate_video(url: str, history_file: str = "history.txt") -> bool:
    """
    Check if the video URL already exists in the history file.
    """
    if not os.path.exists(history_file):
        return False
    with open(history_file, "r", encoding="utf-8") as f:
        history = f.read().splitlines()
    return url.strip() in history

def save_video_to_history(url: str, history_file: str = "history.txt"):
    """
    Append a new video URL to the history file.
    """
    with open(history_file, "a", encoding="utf-8") as f:
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

# Function: Generate Instagram Caption using Gemini
def generate_video_narration(nature: str, video_url: str, metadata: dict) -> str:
    """
    Generates a Nat Geo-style narration for the given video metadata.
    Narration duration is matched to video length (for TTS sync).
    """
    # Extract semantic context
    raw_title = metadata.get("original_url", "").split("/")[-2]
    # Remove trailing hyphenated numeric ID if present
    cleaned_title = re.sub(r"-\d+$", "", raw_title).replace("-", " ")
    content_hint = cleaned_title.strip().capitalize() or "A natural scene"

    duration = metadata.get("duration", 15)  # seconds
    tags = metadata.get("tags", [])
    tag_text = ", ".join(tags) if tags else "natural elements"

    # Estimate max narration word count
    words_per_second = 2  # slightly slower than normal for TTS clarity
    max_words = int(duration * words_per_second * 0.9)  # 90% safety margin

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


def fetch_pexels_video(query="nature", min_duration=15):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 30}
    url = "https://api.pexels.com/videos/search"
    resp = requests.get(url, headers=headers, params=params)

    if resp.status_code != 200:
        print(f"Pexels error: {resp.status_code}")
        return None, None

    videos = [v for v in resp.json().get("videos", []) if v["duration"] >= min_duration]
    if not videos:
        print("No Pexels videos found ≥ 15 seconds.")
        return None, None

    best_video_file = None
    best_video_meta = None
    best_quality = 0

    for video in videos:
        for vf in video["video_files"]:
            resolution = vf["width"] * vf["height"]
            is_vertical = vf["height"] > vf["width"]

            # Higher preference if vertical; HD quality (720p or above)
            if resolution >= 1280 * 720:
                score = resolution + (1e6 if is_vertical else 0)
                if score > best_quality:
                    best_quality = score
                    best_video_file = vf
                    best_video_meta = video

    if not best_video_file:
        print("No suitable HD video found.")
        return None, None

    print("📱 Using vertical video (portrait mode)" if best_video_file["height"] > best_video_file["width"]
          else "🖥️ Using horizontal video (landscape mode)")

    metadata = {
        "title": best_video_meta.get("url", "").split("/")[-1].replace("-", " ").replace("/", ""),
        "duration": best_video_meta.get("duration"),
        "width": best_video_meta.get("width"),
        "height": best_video_meta.get("height"),
        "tags": [tag["title"] for tag in best_video_meta.get("tags", [])] if "tags" in best_video_meta else [],
        "original_url": best_video_meta.get("url")
    }

    return best_video_file["link"], metadata


def fetch_pixabay_video(query="nature", min_duration=15):
    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "video_type": "film",
        "per_page": 30
    }
    url = "https://pixabay.com/api/videos/"
    resp = requests.get(url, params=params)

    if resp.status_code != 200:
        print(f"Pixabay error: {resp.status_code}")
        return None, None

    hits = [v for v in resp.json().get("hits", []) if v["duration"] >= min_duration]
    if not hits:
        print("No Pixabay videos found ≥ 15 seconds.")
        return None, None

    best_video_file = None
    best_video_meta = None
    best_quality = 0

    for video in hits:
        for _, vf in video["videos"].items():
            resolution = vf["width"] * vf["height"]
            is_vertical = vf["height"] > vf["width"]

            if resolution >= 1280 * 720:
                score = resolution + (1e6 if is_vertical else 0)
                if score > best_quality:
                    best_quality = score
                    best_video_file = vf
                    best_video_meta = video

    if not best_video_file:
        print("No suitable HD video found.")
        return None, None

    print("📱 Using vertical video (portrait mode)" if best_video_file["height"] > best_video_file["width"]
          else "🖥️ Using horizontal video (landscape mode)")

    metadata = {
        "title": best_video_meta.get("tags", ""),
        "duration": best_video_meta.get("duration"),
        "width": best_video_file["width"],
        "height": best_video_file["height"],
        "tags": [t.strip() for t in best_video_meta.get("tags", "").split(",")],
        "original_url": best_video_meta.get("pageURL")
    }

    return best_video_file["url"], metadata


'''
def backup_videos():
    """
    Picks a random video and its metadata from coverr_list.json.
    Used as fallback when Pexels and Pixabay fail.
    """
    try:
        with open("coverr_list.json", "r", encoding="utf-8") as f:
            video_list = json.load(f)

        selected = random.choice(video_list)
        metadata = {
            "title": selected.get("title", ""),
            "description": selected.get("description", ""),
            "query": selected.get("query", ""),
            "original_url": selected.get("url")
        }
        return selected["url"], metadata

    except Exception as e:
        print(f"❌ Failed to fetch backup Coverr video: {e}")
        return None, None
'''
def fetch_nature_video(query="nature"):
    """
    Tries Pexels, then Pixabay, then Coverr API, then falls back to local coverr_list.json.
    """
    
    video_url, metadata = fetch_pexels_video(query)
    if video_url:
        print("✅ Fetched video from Pexels:", video_url)
        return video_url, metadata

    video_url, metadata = fetch_pixabay_video(query)
    if video_url:
        print("✅ Fetched video from Pixabay:", video_url)
        return video_url, metadata
    '''
    video_url, metadata = backup_videos()
    if video_url:
        print("✅ Fetched video from local Coverr backup list:", video_url)
        return video_url, metadata
    '''
    raise Exception("❌ Failed to fetch a nature video from all sources and backups.")

def download_video(video_url: str, output_dir="videos") -> str:
    os.makedirs(output_dir, exist_ok=True)
    file_name = f"nature_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    local_path = os.path.join(output_dir, file_name)
    try:
        resp = requests.get(video_url, stream=True)
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024*1024):  # 1MB
                if chunk:
                    f.write(chunk)
        print(f"✅ Video saved to {local_path}")
        return local_path
    except Exception as e:
        print(f"❌ Failed to download video: {e}")
        return ""

def synthesize_text(text, output_path="output.mp3", selected_voice=None):
    client = texttospeech.TextToSpeechClient()

    # Clean text to avoid unwanted spoken characters
    clean_text = clean_text_for_tts(text)

    # Pick random voice if not provided
    if not selected_voice:
        selected_voice = random.choice(voice_names)

    input_text = texttospeech.SynthesisInput(text=clean_text)

    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name=selected_voice
    )

    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.1,
        effects_profile_id=["handset-class-device"]
    )

    response = client.synthesize_speech(
        input=input_text, voice=voice, audio_config=audio_config
    )

    with open(output_path, "wb") as out:
        out.write(response.audio_content)
        print(f"✅ Audio content written to {output_path} using voice: {selected_voice}")

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
        exit()

    video_path = download_video(video_url)
    if not video_path:
        exit()

    narration = generate_video_narration(random_query, video_url, metadata)
    print(f"Narration:\n{narration}")

    voiceover_path = synthesize_text(re.sub(r'[*_#~]', '', narration).strip(), "voiceover.mp3")

    # Generate title
    title = generate_title_from_narration(narration)
    print(f" title-->{title}")
    # Generate caption
    caption = generate_caption_from_narration(narration,title)
    print(f"Generated caption: {caption}")    

    # 1. Make Video
    final_video_path = combine_video_with_music_and_voiceover(video_path, voiceover_path)

    # ✅ Add validation here
    if not final_video_path or not os.path.exists(final_video_path):
        raise FileNotFoundError("❌ Final narrated video was not generated correctly.")
       
    # 2. Publish to Instagram
    post_reel(final_video_path, caption)
    print(f"✅ Reel Uploaded.")
        
    # 3. Publish to Youtube
    tags_list = ["nature", "natgeo", "wildlife", "adventure", "travel", "explore", "beautiful", "amazing", "unforgettable", "naturelovers", "naturephotography", "naturelovers", "naturephotography", "naturelovers", "naturephotography"]
    yt_id = upload_video_to_youtube(final_video_path, title=title, description=caption, tags=tags_list)
    print(f"✅ YouTube Shorts uploaded: https://youtu.be/{yt_id}")
        
    # 4. Publish to Twitter (X)
    yt_url = f"https://youtu.be/{yt_id}" if 'yt_id' in locals() else ""
    insta_url = "https://www.instagram.com/poeminpixels/"  # replace with your real profile link
    tweet_caption = generate_tweet_from_video(narration, yt_url, insta_url)

    # 5. Post to Twitter (text only)
    tweet_url = post_to_twitter_text_only(tweet_caption)
    print(f"✅ tweet done successfully:{tweet_url}")
    

