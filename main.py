import os
import random
import requests
from dotenv import load_dotenv
from langchain.tools import tool
from instagrapi import Client
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.schema import HumanMessage

# Load environment variables
load_dotenv()

# Initialize Gemini Chat Model with a supported model name
llm = ChatGoogleGenerativeAI(
    model="models/gemini-1.5-flash",
    temperature=0.7
)

# Function: Search for a Plant Image on Unsplash
@tool
def search_plant_image(query: str) -> str:
    """
    Return the URL of a random Unsplash image matching the plant query.
    """
    response = requests.get(
        "https://api.unsplash.com/photos/random",
        params={
            "query": query,
            "client_id": os.getenv("UNSPLASH_ACCESS_KEY")
        }
    ).json()
    # The 'urls' object provides multiple sizes; use 'regular' for a balanced resolution.
    return response["urls"]["regular"]

# Function: Post an Image to Instagram
@tool
def post_to_instagram(image_url: str, caption: str) -> str:
    """
    Download the image from image_url and post it to Instagram with the caption.
    """
    cl = Client()
    cl.login(
        os.getenv("INSTAGRAM_USERNAME"),
        os.getenv("INSTAGRAM_PASSWORD")
    )
    img_data = requests.get(image_url).content
    temp_path = "temp.jpg"
    with open(temp_path, "wb") as img_file:
        img_file.write(img_data)
    cl.photo_upload(temp_path, caption)
    os.remove(temp_path)
    return "Posted successfully."

# List of popular plants
PLANT_LIST = [
    "Monstera deliciosa", "Fiddle Leaf Fig", "Snake Plant", "Peace Lily",
    "Spider Plant", "ZZ Plant", "Pothos", "Rubber Plant",
    "Philodendron", "Succulent", "Aloe Vera", "Bird of Paradise",
    "Chinese Evergreen", "Dieffenbachia", "Boston Fern", "Areca Palm",
    "Bamboo Palm", "Dracaena Marginata", "Calathea", "Croton",
    "Jade Plant", "English Ivy", "Anthurium", "Kalanchoe",
    "Begonia", "Cast Iron Plant", "Prayer Plant", "Schefflera",
    "Asparagus Fern", "Peperomia", "Aglaonema", "Hoya",
    "String of Pearls", "String of Hearts", "Oxalis", "Air Plant",
    "Moth Orchid", "Nerve Plant", "Lucky Bamboo", "Money Tree",
    "Cactus", "Swiss Cheese Plant", "Tropical Pitcher Plant",
    "African Violet", "Parlor Palm", "Maidenhair Fern",
    "Silver Satin Pothos", "Echeveria", "Yucca", "Sansevieria Cylindrica",
    "Variegated Monstera", "Alocasia Polly", "Raven ZZ Plant", "Fatsia Japonica",
    "Lipstick Plant", "Dischidia", "Tradescantia", "Banana Plant",
    "Elephant Ear Plant", "Tillandsia Xerographica", "Staghorn Fern",
    "Homalomena", "Caladium", "Epipremnum Cebu Blue", "Philodendron Pink Princess",
    "Philodendron Gloriosum", "Philodendron Melanochrysum",
    "Monstera Adansonii", "Syngonium", "Senecio", "Tacca Chantrieri (Bat Plant)",
    "Bonsai Tree", "Lithops (Living Stones)", "Maranta Leuconeura (Red Prayer Plant)",
    "Coleus", "Dumb Cane", "Mimosa Pudica (Sensitive Plant)",
    "String of Dolphins", "String of Bananas", "Senecio Rowleyanus",
    "Rhipsalis", "Velvet Philodendron", "Scindapsus Pictus", "Pilea Peperomioides",
    "Pilea Moon Valley", "Aeschynanthus", "Nephrolepis exaltata", "Aralia",
    "Clusia Rosea", "Zebra Plant", "Moon Cactus", "Bromeliad", "Torch Ginger"
]


# Function: Generate Instagram Caption using Gemini
@tool
def generate_caption(plant: str, image_url: str) -> str:
    """
    Use Gemini to create a caption with emojis and hashtags for the given plant.
    """
    prompt = (
        f"Write a super random Instagram caption with emojis and relevant and famous plant hashtags for the plant '{plant}'. "
        "Write an interesting fact about the plant '{plant}'. "
        "Keep it under 450 characters."
    )
    # Create a human message for the chat model
    messages = [HumanMessage(content=prompt)]
    # Invoke the model
    response = llm.invoke(messages)
    caption = response.content.strip()

    return caption

# Main execution
if __name__ == "__main__":
    # 1. Select a random plant
    plant = random.choice(PLANT_LIST)
    print(f"Selected plant: {plant}")

    # 2. Fetch image URL
    image_url = search_plant_image(plant)
    print(f"Fetched image URL: {image_url}")

    # 3. Generate caption
    caption = generate_caption.invoke({"plant": plant, "image_url": image_url})

    print(f"Generated caption: {caption}")

    # 4. Post to Instagram
    result = post_to_instagram.invoke({"image_url": image_url, "caption": caption})

    print(result)
