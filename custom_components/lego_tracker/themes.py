"""LEGO themes (all LEGO product lines), for switching themes off under Deals → Settings.
Sets keep the theme name their source gave; matching ignores case, spaces and punctuation."""
from __future__ import annotations

import re

THEMES = (
    "4 Juniors", "Advanced Models", "Adventurers", "Agents", "Alpha Team", "Animal Crossing", "Aqua Raiders",
    "Aquazone", "Architecture", "Atlantis", "Avatar", "Avatar The Last Airbender", "Baby", "Basic", "Batman",
    "Belville", "Ben 10", "Bionicle", "Bluey", "Boats", "Boost", "Botanicals", "Brick Sketches", "BrickHeadz",
    "BrickLink", "Bricks and More", "Building Set with People", "Bulk Bricks", "Cars", "Castle", "City", "Classic",
    "Clikits", "Creator", "Dacta", "DC Comics Super Heroes", "DC Super Hero Girls", "Dimensions", "Dino", "Dino 2010",
    "Dino Attack", "Dinosaurs", "Discovery", "Disney", "Disney Princess", "Dots", "DREAMZzz", "Duplo", "Editions",
    "Education", "Elves", "Exclusive", "Exo-Force", "Explore", "Fabuland", "Factory", "FORMA", "Fortnite",
    "Freestyle", "Friends", "Fusion", "Gabby's Dollhouse", "Galidor", "Games", "Ghostbusters", "Harry Potter",
    "HERO Factory", "Hidden Side", "Hobby Set", "Homemaker", "Horizon", "Icons", "Ideas", "Indiana Jones",
    "Island Xtreme Stunts", "Jack Stone", "Juniors", "Jurassic World", "KPop Demon Hunters", "Legends of Chima",
    "LEGO Art", "LEGO Games", "LEGO Originals", "LEGOLAND", "Make and Create", "Marvel Super Heroes",
    "Master Builder Academy", "Mickey Mouse", "Mindstorms", "Minecraft", "Minifigure Series", "Minions", "Minitalia",
    "Mixels", "Model Team", "Monkie Kid", "Monster Fighters", "Mursten", "Nexo Knights", "Nike", "Ninjago",
    "One Piece", "Overwatch", "Pharaoh's Quest", "Pirates", "Pirates of the Caribbean", "PlayStation", "Pokémon",
    "Power Functions", "Power Miners", "Powered Up", "Primo", "Prince of Persia", "Promotional", "Quatro", "Racers",
    "Rock Raiders", "Samsonite", "Scala", "Scooby-Doo", "Seasonal", "Serious Play", "Service Packs", "Shrek",
    "Sonic the Hedgehog", "Space", "Speed Champions", "Spider-Man", "SpongeBob SquarePants", "Sports", "Spybotics",
    "Star Wars", "Stranger Things", "Studios", "Super Mario", "System i Leg", "Technic",
    "Teenage Mutant Ninja Turtles", "The Angry Birds Movie", "The Hobbit", "The Legend of Zelda",
    "The LEGO Batman Movie", "The LEGO Movie", "The Lego Movie 2 The Second Part", "The LEGO Ninjago Movie",
    "The Lone Ranger", "The Lord of the Rings", "The Powerpuff Girls", "The Simpsons", "Time Cruisers", "Town",
    "Toy Story", "Trains", "Trolls World Tour", "Ultra Agents", "Unikitty!", "Universal Building Set", "Vidiyo",
    "Vikings", "Wednesday", "Western", "Wicked", "World City", "World Racers", "Xtra", "Znap",
)
# the same theme under another name at another source (Brickset, LEGO.com)
ALIASES = {"collectableminifigures": "minifigureseries", "minifigures": "minifigureseries", "marvel": "marvelsuperheroes",
           "dc": "dccomicssuperheroes", "dcsuperheroes": "dccomicssuperheroes", "legoart": "art", "art": "art",
           "miscellaneous": "exclusive", "spiderman": "spiderman", "pokemon": "pokemon"}


def key(theme: str | None) -> str:
    """'Star Wars', 'star-wars' and 'STAR WARS™' all become 'starwars'."""
    k = re.sub(r"[^a-z0-9]", "", (theme or "").lower().replace("é", "e").replace("lego ", ""))
    return ALIASES.get(k, k)
