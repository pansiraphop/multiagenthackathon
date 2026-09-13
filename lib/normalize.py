"""Ingredient normalization — the one helper both paths import.

If the pantry says "tomato" and a recipe says "Tomatoes", nothing matches and the
shopping list is silently wrong. Every ingredient name must pass through here
before it touches the database.

Run this file directly to execute the self-tests: `python -m lib.normalize`
"""

from __future__ import annotations

import re

# Unit aliases -> the fixed enum in config.UNITS.
_UNIT_ALIASES = {
    "g": "g", "gram": "g", "grams": "g", "gm": "g", "gms": "g", "gr": "g",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "kilogram": "kg", "kilograms": "kg",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml",
    "millilitre": "ml", "millilitres": "ml", "mls": "ml", "cc": "ml",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l", "ltr": "l",
    "cup": "cup", "cups": "cup", "c": "cup",
    "tbsp": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "tbs": "tbsp", "tbspn": "tbsp", "T": "tbsp",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp", "tspn": "tsp", "t": "tsp",
}

# Anything countable or unitless collapses to "unit".
_UNITLESS = {
    "unit", "units", "piece", "pieces", "pc", "pcs", "whole", "clove", "cloves",
    "slice", "slices", "can", "cans", "tin", "tins", "sprig", "sprigs",
    "handful", "handfuls", "pinch", "pinches", "dash", "bunch", "bunches",
    "head", "heads", "stalk", "stalks", "large", "medium", "small", "",
}

# Leading descriptors to strip from names. Size/quality words carry no
# shopping-list meaning and only break matching.
_DESCRIPTORS = {
    "large", "medium", "small", "extra", "ripe", "fresh", "frozen", "dried",
    "chopped", "diced", "minced", "sliced", "grated", "shredded", "crushed",
    "ground", "whole", "raw", "cooked", "boneless", "skinless", "organic",
    "unsalted", "salted", "finely", "roughly", "thinly", "warm", "cold", "hot",
    "good", "quality", "optional", "plus", "more", "taste",
}

# Words ending in 's' that are already singular. Stripping the 's' mangles them.
_ALREADY_SINGULAR = {
    "hummus", "couscous", "molasses", "greens", "oats", "grits", "asparagus",
    "watercress", "swiss", "brussels", "bass", "haas", "miso", "chives",
}


def normalize_unit(unit: str | None) -> str:
    """Map any unit spelling onto the fixed enum. Unknown units become 'unit'."""
    if unit is None:
        return "unit"
    raw = str(unit).strip()
    if raw in _UNIT_ALIASES:           # case-sensitive first: "T" vs "t"
        return _UNIT_ALIASES[raw]
    low = raw.lower().rstrip(".")
    if low in _UNIT_ALIASES:
        return _UNIT_ALIASES[low]
    if low in _UNITLESS:
        return "unit"
    return "unit"


def singularize(word: str) -> str:
    """Naive singularization. Good enough for ingredient names, and predictable."""
    if word in _ALREADY_SINGULAR or len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"          # berries -> berry
    if word.endswith("oes"):
        return word[:-2]                # tomatoes -> tomato
    if word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]                # peaches -> peach
    if word.endswith("ves"):
        return word[:-3] + "f"          # leaves -> leaf
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]                # onions -> onion
    return word


def normalize_name(name: str) -> str:
    """Lowercase, strip descriptors and parentheticals, singularize the head noun."""
    if not name:
        return ""
    s = name.lower().strip()
    s = re.sub(r"\([^)]*\)", " ", s)        # drop "(diced)"
    s = s.split(",")[0]                      # "tomatoes, diced" -> "tomatoes"
    s = re.sub(r"\b(?:to taste|for garnish|for serving|as needed)\b", " ", s)
    s = re.sub(r"[^a-z\s-]", " ", s)         # drop digits and punctuation
    s = re.sub(r"\s+", " ", s).strip()

    words = [w for w in s.split() if w not in _DESCRIPTORS]
    if not words:                            # name was ALL descriptors
        words = s.split()
    if not words:
        return ""

    # Strip leading measure words the extractor left in the name:
    # "cloves garlic" -> "garlic", "cups flour" -> "flour". Only ever leading,
    # and never the last word standing, so "cup" alone survives as a name.
    while len(words) > 1 and (words[0] in _UNIT_ALIASES or words[0] in _UNITLESS):
        words.pop(0)

    # Singularize only the final word — the head noun in English.
    # "sweet potatoes" -> "sweet potato", not "sweet potatoe".
    words[-1] = singularize(words[-1])
    return " ".join(words).strip()


def normalize_ingredient(
    name: str,
    qty: float | int | str | None = None,
    unit: str | None = None,
) -> tuple[str, float | None, str]:
    """Return (normalized_name, quantity, normalized_unit).

    quantity stays None when it is None or unparseable — null means a
    qualitative amount ("a good glug") and must never be invented.
    """
    clean_name = normalize_name(name)
    clean_unit = normalize_unit(unit)

    if qty is None or qty == "":
        return clean_name, None, clean_unit
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return clean_name, None, clean_unit
    if q <= 0:
        return clean_name, None, clean_unit
    return clean_name, q, clean_unit


if __name__ == "__main__":
    # Name normalization
    assert normalize_name("2 large ripe Tomatoes (diced)") == "tomato"
    assert normalize_name("Tomatoes") == "tomato"
    assert normalize_name("  ONIONS  ") == "onion"
    assert normalize_name("Sweet Potatoes") == "sweet potato"
    assert normalize_name("cherry berries") == "cherry berry"
    assert normalize_name("boneless skinless chicken thighs") == "chicken thigh"
    assert normalize_name("olive oil") == "olive oil"
    assert normalize_name("hummus") == "hummus"
    assert normalize_name("couscous") == "couscous"
    assert normalize_name("baby spinach leaves") == "baby spinach leaf"
    assert normalize_name("salt, to taste") == "salt"
    assert normalize_name("3 cloves garlic") == "garlic"
    assert normalize_name("2 cups all-purpose flour") == "all-purpose flour"
    assert normalize_name("cup") == "cup", "a bare unit word survives as a name"
    assert normalize_name("fresh") == "fresh", "all-descriptor names must survive"

    # Unit normalization
    assert normalize_unit("Tablespoons") == "tbsp"
    assert normalize_unit("tsp.") == "tsp"
    assert normalize_unit("T") == "tbsp", "capital T is tablespoon"
    assert normalize_unit("t") == "tsp", "lowercase t is teaspoon"
    assert normalize_unit("grams") == "g"
    assert normalize_unit("Kilograms") == "kg"
    assert normalize_unit("cloves") == "unit"
    assert normalize_unit(None) == "unit"
    assert normalize_unit("smidgen") == "unit", "unknown units degrade, never raise"

    # Full helper
    assert normalize_ingredient("Tomatoes", 2, "whole") == ("tomato", 2.0, "unit")
    assert normalize_ingredient("Olive Oil", None, None) == ("olive oil", None, "unit")
    assert normalize_ingredient("Flour", "250", "grams") == ("flour", 250.0, "g")
    assert normalize_ingredient("Salt", "a pinch", "tsp") == ("salt", None, "tsp")
    assert normalize_ingredient("Sugar", -5, "g") == ("sugar", None, "g")

    print("normalize: all self-tests passed")
