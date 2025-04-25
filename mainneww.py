import os
import random
import requests
from dotenv import load_dotenv
from langchain.tools import tool
from instagrapi import Client
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.schema import HumanMessage
from moviepy.editor import ImageClip, AudioFileClip

# Load environment variables
load_dotenv()

# Initialize Gemini Chat Model
llm = ChatGoogleGenerativeAI(
    model="models/gemini-1.5-flash",
    temperature=0.7
)

@tool
def search_plant_image(query: str) -> str:
    response = requests.get(
        "https://api.unsplash.com/search/photos",
        params={"query": query, "client_id": os.getenv("UNSPLASH_ACCESS_KEY"), "per_page": 1}
    ).json()
    return response["results"][0]["urls"]["regular"]

@tool
def generate_caption(plant: str, image_url: str) -> str:
    prompt = f"Write an Instagram caption with emojis and relevant hashtags for the plant '{plant}'. Keep it under 450 characters."
    messages = [HumanMessage(content=prompt)]
    response = llm.invoke(messages)
    return response.content.strip()

@tool
def post_to_instagram(video_path: str, caption: str) -> str:
    cl = Client()
    cl.login(os.getenv("INSTAGRAM_USERNAME"), os.getenv("INSTAGRAM_PASSWORD"))
    cl.video_upload(video_path, caption)
    return "Posted successfully."

PLANT_LIST = [
    "Monstera deliciosa", "Fiddle Leaf Fig", "Snake Plant", "Peace Lily", "Spider Plant", "ZZ Plant"
]

def download_random_music() -> str:
    response = requests.get(
        "https://pixabay.com/api/music/",
        params={
            "key": os.getenv("PIXABAY_API_KEY"),
            "genre": "happy",
            "per_page": 20
        }
    ).json()
    track = random.choice(response["hits"])
    music_url = track["audio"]
    music_path = "bg_music.mp3"
    with open(music_path, "wb") as f:
        f.write(requests.get(music_url).content)
    return music_path

def create_video(image_path: str, music_path: str) -> str:
    clip = ImageClip(image_path, duration=10)
    clip = clip.set_audio(AudioFileClip(music_path)).set_fps(24).resize(height=720)
    video_path = "post_video.mp4"
    clip.write_videofile(video_path, codec='libx264', audio_codec='aac')
    return video_path

if __name__ == "__main__":
    plant = random.choice(PLANT_LIST)
    image_url = search_plant_image(plant)
    caption = generate_caption.invoke({"plant": plant, "image_url": image_url})

    img_data = requests.get(image_url).content
    img_path = "plant.jpg"
    with open(img_path, "wb") as f:
        f.write(img_data)

    music_path = download_random_music()
    video_path = create_video(img_path, music_path)

    result = post_to_instagram.invoke({"video_path": video_path, "caption": caption})
    print(result)

    os.remove(img_path)
    os.remove(music_path)
    os.remove(video_path)
