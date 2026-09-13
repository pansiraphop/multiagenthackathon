"""Ingredient normalization — the one helper both paths import.

If the pantry says "tomato" and a recipe says "Tomatoes", nothing matches and the
shopping list is silently wrong. Every ingredient must pass through here before
it touches the database.

Handles the messy reality of recipe text: unicode fractions (1/2 cup), mixed
numbers, ranges, imperial units, measure words stuck inside names, qualitative
amounts, duplicates, and non-food "ingredients" like pasta water.

Run the self-tests: `python -m lib.normalize`
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

# --- units -----------------------------------------------------------------

# Spellings that map straight onto the canonical enum, no rescaling.
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

# Imperial and volumetric units that must be CONVERTED, not flattened.
# Without this, "8 oz" silently becomes "8 unit" — the quantity survives but
# means something completely different, which is worse than failing outright.
_UNIT_CONVERSIONS: dict[str, tuple[float, str]] = {
    "oz": (28.3495, "g"), "ounce": (28.3495, "g"), "ounces": (28.3495, "g"),
    "lb": (453.592, "g"), "lbs": (453.592, "g"),
    "pound": (453.592, "g"), "pounds": (453.592, "g"),
    "fl oz": (29.5735, "ml"), "floz": (29.5735, "ml"),
    "fluid ounce": (29.5735, "ml"), "fluid ounces": (29.5735, "ml"),
    "pint": (473.176, "ml"), "pints": (473.176, "ml"), "pt": (473.176, "ml"),
    "quart": (946.353, "ml"), "quarts": (946.353, "ml"), "qt": (946.353, "ml"),
    "gallon": (3.78541, "l"), "gallons": (3.78541, "l"), "gal": (3.78541, "l"),
    "stick": (113.0, "g"), "sticks": (113.0, "g"),          # butter
}

# Countable or unitless — collapse to "unit".
_UNITLESS = {
    "unit", "units", "piece", "pieces", "pc", "pcs", "whole", "clove", "cloves",
    "slice", "slices", "can", "cans", "tin", "tins", "sprig", "sprigs",
    "handful", "handfuls", "pinch", "pinches", "dash", "dashes", "bunch",
    "bunches", "head", "heads", "stalk", "stalks", "large", "medium", "small",
    "glug", "glugs", "drizzle", "splash", "squeeze", "knob", "sheet", "sheets",
    "leaf", "leaves", "ear", "ears", "fillet", "fillets", "strip", "strips", "",
}

CANONICAL_UNITS = ("g", "kg", "ml", "l", "cup", "tbsp", "tsp", "unit")

# --- name cleanup ----------------------------------------------------------

_DESCRIPTORS = {
    "large", "medium", "small", "extra", "ripe", "fresh", "freshly", "frozen",
    "dried", "chopped", "diced", "minced", "sliced", "grated", "shredded",
    "crushed", "ground", "whole", "raw", "cooked", "boneless", "skinless",
    "organic", "unsalted", "salted", "finely", "roughly", "thinly", "warm",
    "cold", "hot", "good", "quality", "optional", "plus", "more", "taste",
    "packed", "heaping", "generous", "approximately", "about", "roasted",
    "toasted", "peeled", "trimmed", "halved", "quartered", "cubed", "softened",
    "melted", "room", "temperature", "divided", "beaten", "rinsed", "drained",
}

_ALREADY_SINGULAR = {
    "hummus", "couscous", "molasses", "greens", "oats", "grits", "asparagus",
    "watercress", "swiss", "brussels", "bass", "haas", "miso", "chives",
    "hummous", "harissa", "gochujang", "tahini", "kimchi", "panko", "dashi",
}

# Non-food lines that must never reach the shopping list. They are still real
# recipe ingredients (pasta water genuinely is one) so they stay in
# recipe_ingredients — stage 4 filters them out with is_non_food().
_NON_FOOD = {
    "water", "pasta water", "tap water", "cold water", "warm water",
    "hot water", "boiling water", "ice", "ice cube", "cooking water",
    "starchy water", "reserved pasta water",
}
# ...but these are groceries you genuinely buy.
_WATER_ALLOWLIST = {
    "coconut water", "rose water", "rosewater", "orange blossom water",
    "sparkling water", "soda water", "tonic water", "mineral water",
    "coconut milk water",
}

# --- quantity words --------------------------------------------------------

_NUMBER_WORDS = {
    "a": 1.0, "an": 1.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0,
    "five": 5.0, "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0,
    "ten": 10.0, "eleven": 11.0, "twelve": 12.0, "half": 0.5, "quarter": 0.25,
    "dozen": 12.0,
}
# These are inherently vague — the value is a guess, so flag it.
_VAGUE_WORDS = {"couple": 2.0, "few": 3.0, "several": 3.0, "some": 1.0}

_FRACTION_CHARS = "¼-¾⅐-⅞"

# Fallback approximations, used ONLY when the model returns a qualitative
# amount with no number. Deliberately coarse: the model has the ingredient
# context ("a handful of parsley" vs "a handful of almonds") and should do the
# approximating. This exists so a null never reaches a cook standing at a stove.
_QUALITATIVE_FALLBACKS: list[tuple[str, float, str]] = [
    (r"good\s+glug|generous\s+glug|big\s+glug", 2.0, "tbsp"),
    (r"glug|drizzle|splash|squeeze|knob", 1.0, "tbsp"),
    (r"big\s+pinch|large\s+pinch|generous\s+pinch", 0.5, "tsp"),
    (r"pinch|dash", 0.25, "tsp"),
    (r"to\s+taste", 0.5, "tsp"),
    (r"small\s+handful", 0.125, "cup"),
    (r"handful", 0.25, "cup"),
    (r"bunch", 1.0, "unit"),
    (r"sprig", 2.0, "unit"),
    (r"few\s+drops|drop", 0.25, "tsp"),
    (r"for\s+(?:serving|garnish|drizzling|frying)", 1.0, "unit"),
]


# ---------------------------------------------------------------------------
# quantity parsing
# ---------------------------------------------------------------------------

def _single_number(tok: str) -> float | None:
    """Parse one numeric token: '2', '2.5', '1/2', '1 1/2', '1½', '½', 'two'."""
    tok = tok.strip().lower().replace(",", "")
    if not tok:
        return None

    if tok in _NUMBER_WORDS:
        return _NUMBER_WORDS[tok]

    # mixed or simple ascii fraction — check before plain digits so that
    # "1/2" isn't read as the leading "1"
    m = re.fullmatch(r"(?:(\d+)\s+)?(\d+)\s*[/⁄]\s*(\d+)", tok)
    if m:
        whole = float(m.group(1) or 0)
        num, den = float(m.group(2)), float(m.group(3))
        if den:
            return whole + num / den

    # optional integer followed by a unicode fraction: "1½", "½"
    m = re.fullmatch(rf"(\d+(?:\.\d+)?)?\s*([{_FRACTION_CHARS}])", tok)
    if m:
        whole = float(m.group(1) or 0)
        try:
            return whole + unicodedata.numeric(m.group(2))
        except (TypeError, ValueError):
            pass

    if re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return float(tok)

    return None


def parse_quantity(raw: Any) -> tuple[float | None, bool]:
    """Return (value, was_approximated).

    Handles plain numbers, decimals, ascii and unicode fractions, mixed
    numbers, number words, and ranges. A range collapses to its midpoint and
    is flagged approximate, because "2-3 cloves" is not a measurement.
    """
    if raw is None:
        return None, False
    if isinstance(raw, bool):
        return None, False
    if isinstance(raw, (int, float)):
        return (float(raw), False) if raw > 0 else (None, False)

    s = str(raw).strip().lower()
    if not s:
        return None, False

    # vague counts: "a few cloves"
    for word, value in _VAGUE_WORDS.items():
        if re.search(rf"\b{word}\b", s):
            return value, True

    # ranges: "2-3", "2 to 3", "2–3"
    m = re.fullmatch(
        rf"([\d\s./⁄{_FRACTION_CHARS}]+?)\s*(?:-|–|—|to)\s*"
        rf"([\d\s./⁄{_FRACTION_CHARS}]+)",
        s,
    )
    if m:
        lo, hi = _single_number(m.group(1)), _single_number(m.group(2))
        if lo is not None and hi is not None:
            return (lo + hi) / 2, True
        if lo is not None:
            return lo, True

    direct = _single_number(s)
    if direct is not None:
        return direct, False

    # last resort: pull the first number out of noisy text ("about 2 cups-ish")
    m = re.search(rf"\d+(?:\.\d+)?|[{_FRACTION_CHARS}]", s)
    if m:
        val = _single_number(m.group(0))
        if val is not None:
            return val, True

    return None, False


# ---------------------------------------------------------------------------
# unit parsing
# ---------------------------------------------------------------------------

def _clean_unit_text(unit: Any) -> str:
    return re.sub(r"\s+", " ", str(unit).replace(".", " ").strip().lower()).strip()


def normalize_unit(unit: Any) -> str:
    """Map any unit spelling onto the canonical enum. Unknown units become 'unit'.

    Note this does NOT rescale quantities — use normalize_measure() for units
    like oz and lb, which need the number changed too.
    """
    if unit is None:
        return "unit"
    raw = str(unit).strip()
    if raw in _UNIT_ALIASES:                 # case-sensitive: "T" vs "t"
        return _UNIT_ALIASES[raw]
    low = _clean_unit_text(raw)
    if low in _UNIT_ALIASES:
        return _UNIT_ALIASES[low]
    if low in _UNIT_CONVERSIONS:
        return _UNIT_CONVERSIONS[low][1]
    if low in _UNITLESS:
        return "unit"
    return "unit"


def normalize_measure(
    qty: float | None, unit: Any
) -> tuple[float | None, str]:
    """Convert (qty, unit) onto the canonical enum, rescaling where needed.

    (8, 'oz')   -> (226.8, 'g')
    (1, 'stick')-> (113.0, 'g')
    (2, 'cups') -> (2.0, 'cup')
    """
    raw = str(unit).strip() if unit is not None else ""
    low = _clean_unit_text(raw) if raw else ""

    if low in _UNIT_CONVERSIONS and raw not in _UNIT_ALIASES:
        factor, target = _UNIT_CONVERSIONS[low]
        return (round(qty * factor, 2) if qty is not None else None), target

    return qty, normalize_unit(unit)


# ---------------------------------------------------------------------------
# name normalization
# ---------------------------------------------------------------------------

def normalize_name(name: Any) -> str:
    """Lowercase, strip descriptors/parentheticals/measure words, singularize."""
    if not name:
        return ""
    s = str(name).lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"\([^)]*\)", " ", s)             # drop "(diced)"
    s = s.split(",")[0]                           # "tomatoes, diced"
    s = re.sub(
        r"\b(?:to taste|for garnish|for serving|for drizzling|as needed|"
        r"if desired|or more|plus extra)\b", " ", s)
    s = re.sub(r"[^a-z\s-]", " ", s)              # drop digits and punctuation
    s = re.sub(r"\s+", " ", s).strip(" -")

    words = [w for w in s.split() if w not in _DESCRIPTORS]
    if not words:                                 # name was ALL descriptors
        words = s.split()
    if not words:
        return ""

    # Strip leading measure words the extractor left in the name:
    # "cloves garlic" -> "garlic". Never the last word standing, so a bare
    # "cup" survives as a name.
    while len(words) > 1 and (words[0] in _UNIT_ALIASES
                              or words[0] in _UNITLESS
                              or words[0] in _UNIT_CONVERSIONS):
        words.pop(0)

    # Trailing "of" from "handful of parsley" -> "of parsley"
    while len(words) > 1 and words[0] in {"of", "or", "and"}:
        words.pop(0)

    # Singularize only the head noun. "sweet potatoes" -> "sweet potato".
    words[-1] = singularize(words[-1])
    return " ".join(words).strip()


def singularize(word: str) -> str:
    """Naive singularization. Predictable, which matters more than perfect."""
    if word in _ALREADY_SINGULAR or len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"           # berries -> berry
    if word.endswith("oes"):
        return word[:-2]                 # tomatoes -> tomato
    if word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]                 # peaches -> peach
    if word.endswith("ves"):
        return word[:-3] + "f"           # leaves -> leaf
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]                 # onions -> onion
    return word


def is_non_food(name: str) -> bool:
    """Water, ice and friends — real recipe ingredients, never shopping items."""
    n = normalize_name(name)
    if n in _WATER_ALLOWLIST:
        return False
    if n in _NON_FOOD:
        return True
    return n == "water" or n.endswith(" water")


# ---------------------------------------------------------------------------
# qualitative approximation
# ---------------------------------------------------------------------------

def approximate_qualitative(note: str | None, name: str = "") -> tuple[float, str]:
    """Coarse fallback for a qualitative amount with no number.

    Only fires when the model didn't approximate. A cook needs something
    actionable — "olive oil: null" is useless at a stove — and the caller
    marks the result is_approximate so it's never mistaken for a measurement.
    """
    text = f"{note or ''} {name}".lower()
    for pattern, qty, unit in _QUALITATIVE_FALLBACKS:
        if re.search(pattern, text):
            return qty, unit
    return 1.0, "unit"


# ---------------------------------------------------------------------------
# the main entry points
# ---------------------------------------------------------------------------

def normalize_ingredient(
    name: Any,
    qty: Any = None,
    unit: Any = None,
) -> tuple[str, float | None, str]:
    """Return (normalized_name, quantity, normalized_unit).

    Convenience wrapper. Use to_ingredient_row() for the full DB row, which
    also carries the is_approximate flag.
    """
    row = to_ingredient_row(name, qty, unit)
    return row["name"], row["quantity"], row["unit"]


def to_ingredient_row(
    name: Any,
    qty: Any = None,
    unit: Any = None,
    qualitative_note: str | None = None,
    is_approximate: bool = False,
) -> dict[str, Any]:
    """Build a recipe_ingredients row. The canonical write path for stage 1.

    Guarantees a usable quantity: if the amount was qualitative and no number
    was supplied, it approximates and sets is_approximate=True. A null quantity
    only survives when there is genuinely nothing to go on.
    """
    clean_name = normalize_name(name)
    value, range_approx = parse_quantity(qty)
    value, clean_unit = normalize_measure(value, unit)
    approx = bool(is_approximate) or range_approx

    if value is None:
        # No number anywhere. Approximate from the phrasing so the cook gets
        # something to act on, and flag it.
        note_source = qualitative_note or str(name or "")
        value, fallback_unit = approximate_qualitative(note_source, clean_name)
        # Trust the stated unit if it was meaningful, else take the fallback's.
        if clean_unit == "unit":
            clean_unit = fallback_unit
        approx = True
        if not qualitative_note:
            qualitative_note = (note_source.strip() or None)

    if value is not None and value <= 0:
        value, clean_unit = approximate_qualitative(qualitative_note, clean_name)
        approx = True

    return {
        "name": clean_name,
        "quantity": round(value, 2) if value is not None else None,
        "unit": clean_unit,
        "qualitative_note": qualitative_note,
        "is_approximate": approx,
    }


def dedupe_ingredients(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate ingredients within one recipe.

    Reels routinely list an ingredient twice ("olive oil" in the marinade and
    again for the pan). Without this you get two shopping rows and a unique
    constraint violation downstream.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not row.get("name"):
            continue
        key = (row["name"], row["unit"])
        if key not in merged:
            merged[key] = dict(row)
            continue
        existing = merged[key]
        a, b = existing.get("quantity"), row.get("quantity")
        existing["quantity"] = round((a or 0) + (b or 0), 2) if (a or b) else None
        existing["is_approximate"] = (
            existing.get("is_approximate") or row.get("is_approximate") or False
        )
        existing["qualitative_note"] = (
            existing.get("qualitative_note") or row.get("qualitative_note")
        )
    return list(merged.values())


MAX_SOURCE_CHARS = 12000


def clean_source_text(text: Any) -> str:
    """Prepare a caption or transcript for extraction.

    Strips emoji and zero-width junk (Instagram captions are full of both),
    collapses the decorative whitespace reels use, and caps the length so a
    runaway auto-transcript can't blow up a request. Raises on empty input so
    the caller records a clean failure instead of paying for a doomed API call.
    """
    if text is None:
        raise ValueError("source text is empty")

    s = str(text)
    s = s.replace("​", "").replace("﻿", "")     # zero-width, BOM
    # Drop symbol/pictograph codepoints but keep letters, digits, punctuation
    # and the fraction characters the quantity parser relies on.
    s = "".join(
        ch for ch in s
        if unicodedata.category(ch) not in {"So", "Cf", "Cs", "Co"}
        or ch in "¼½¾"
    )
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = s.strip()

    if not s:
        raise ValueError("source text is empty after cleaning")

    if len(s) > MAX_SOURCE_CHARS:
        s = s[:MAX_SOURCE_CHARS].rsplit(" ", 1)[0] + " […truncated]"
    return s


def render_amount(row: dict[str, Any]) -> str:
    """Human-readable amount for calendar descriptions and the cook session.

    Approximations are shown as "~2 tbsp (a good glug)" — usable, and visibly
    not the source's own measurement.
    """
    qty, unit = row.get("quantity"), row.get("unit") or "unit"
    note = row.get("qualitative_note")

    if qty is None:
        return note or "to taste"

    pretty = f"{qty:g}"
    amount = pretty if unit == "unit" else f"{pretty} {unit}"

    if row.get("is_approximate"):
        return f"~{amount} ({note})" if note else f"~{amount}"
    return amount


if __name__ == "__main__":
    # --- names -------------------------------------------------------------
    assert normalize_name("2 large ripe Tomatoes (diced)") == "tomato"
    assert normalize_name("Tomatoes") == "tomato"
    assert normalize_name("  ONIONS  ") == "onion"
    assert normalize_name("Sweet Potatoes") == "sweet potato"
    assert normalize_name("cherry berries") == "cherry berry"
    assert normalize_name("boneless skinless chicken thighs") == "chicken thigh"
    assert normalize_name("olive oil") == "olive oil"
    assert normalize_name("hummus") == "hummus"
    assert normalize_name("baby spinach leaves") == "baby spinach leaf"
    assert normalize_name("salt, to taste") == "salt"
    assert normalize_name("3 cloves garlic") == "garlic"
    assert normalize_name("2 cups all-purpose flour") == "all-purpose flour"
    assert normalize_name("handful of parsley") == "parsley"
    assert normalize_name("8 oz cream cheese") == "cream cheese"
    assert normalize_name("cup") == "cup", "bare unit word survives as a name"
    assert normalize_name("fresh") == "fresh", "all-descriptor names survive"
    assert normalize_name("") == ""
    assert normalize_name(None) == ""

    # --- quantities --------------------------------------------------------
    assert parse_quantity(2) == (2.0, False)
    assert parse_quantity("2.5") == (2.5, False)
    assert parse_quantity("1/2") == (0.5, False)
    assert parse_quantity("1 1/2") == (1.5, False)
    assert parse_quantity("½") == (0.5, False)          # ½
    assert parse_quantity("1½") == (1.5, False)         # 1½
    assert parse_quantity("¼") == (0.25, False)         # ¼
    assert parse_quantity("two") == (2.0, False)
    assert parse_quantity("a") == (1.0, False)
    assert parse_quantity("2-3") == (2.5, True), "ranges are approximate"
    assert parse_quantity("2 to 3") == (2.5, True)
    assert parse_quantity("a few") == (3.0, True)
    assert parse_quantity("a couple") == (2.0, True)
    assert parse_quantity("about 2 cups-ish") == (2.0, True)
    assert parse_quantity("a good glug") == (None, False)
    assert parse_quantity(None) == (None, False)
    assert parse_quantity("") == (None, False)
    assert parse_quantity(0) == (None, False)
    assert parse_quantity(-5) == (None, False)

    # --- units -------------------------------------------------------------
    assert normalize_unit("Tablespoons") == "tbsp"
    assert normalize_unit("tsp.") == "tsp"
    assert normalize_unit("T") == "tbsp", "capital T is tablespoon"
    assert normalize_unit("t") == "tsp", "lowercase t is teaspoon"
    assert normalize_unit("grams") == "g"
    assert normalize_unit("cloves") == "unit"
    assert normalize_unit(None) == "unit"
    assert normalize_unit("smidgen") == "unit", "unknown units degrade, never raise"

    # conversions must rescale the number, not just relabel the unit
    assert normalize_measure(8, "oz") == (226.8, "g")
    assert normalize_measure(1, "lb") == (453.59, "g")
    assert normalize_measure(1, "stick") == (113.0, "g")
    assert normalize_measure(2, "fl oz") == (59.15, "ml")
    assert normalize_measure(1, "pint") == (473.18, "ml")
    assert normalize_measure(2, "cups") == (2.0, "cup")
    assert normalize_measure(None, "oz") == (None, "g")

    # --- rows --------------------------------------------------------------
    r = to_ingredient_row("Tomatoes", 2, "whole")
    assert (r["name"], r["quantity"], r["unit"], r["is_approximate"]) == \
        ("tomato", 2.0, "unit", False)

    r = to_ingredient_row("Flour", "250", "grams")
    assert (r["quantity"], r["unit"]) == (250.0, "g")

    r = to_ingredient_row("Cream cheese", "8", "oz")
    assert (r["quantity"], r["unit"]) == (226.8, "g"), "imperial gets converted"

    # the headline behaviour: qualitative amounts get a usable number + a flag
    r = to_ingredient_row("Olive oil", None, None, qualitative_note="a good glug")
    assert r["quantity"] == 2.0 and r["unit"] == "tbsp" and r["is_approximate"]
    assert render_amount(r) == "~2 tbsp (a good glug)"

    r = to_ingredient_row("Parsley", None, None, qualitative_note="a handful")
    assert (r["quantity"], r["unit"]) == (0.25, "cup") and r["is_approximate"]

    r = to_ingredient_row("Salt", None, "tsp", qualitative_note="to taste")
    assert r["quantity"] == 0.5 and r["unit"] == "tsp" and r["is_approximate"]

    r = to_ingredient_row("Black pepper", None, None, qualitative_note="a pinch")
    assert (r["quantity"], r["unit"]) == (0.25, "tsp")

    # a model-supplied approximation is respected, not overwritten
    r = to_ingredient_row("Olive oil", 2, "tbsp",
                          qualitative_note="a good glug", is_approximate=True)
    assert r["quantity"] == 2.0 and r["is_approximate"]
    assert render_amount(r) == "~2 tbsp (a good glug)"

    # exact amounts render clean, with no tilde
    assert render_amount(to_ingredient_row("Flour", 250, "g")) == "250 g"
    assert render_amount(to_ingredient_row("Lemon", 1, "whole")) == "1"

    # ranges propagate the approximate flag
    r = to_ingredient_row("Garlic", "2-3", "cloves")
    assert r["quantity"] == 2.5 and r["is_approximate"]

    # --- non-food ----------------------------------------------------------
    assert is_non_food("water")
    assert is_non_food("Pasta Water")
    assert is_non_food("reserved pasta water")
    assert is_non_food("ice")
    assert not is_non_food("coconut water"), "coconut water is a grocery"
    assert not is_non_food("rose water")
    assert not is_non_food("watermelon"), "substring must not match"

    # --- dedupe ------------------------------------------------------------
    merged = dedupe_ingredients([
        to_ingredient_row("Olive oil", 2, "tbsp"),
        to_ingredient_row("olive oil", 1, "tbsp"),
        to_ingredient_row("Garlic", 2, "cloves"),
    ])
    assert len(merged) == 2
    oil = next(m for m in merged if m["name"] == "olive oil")
    assert oil["quantity"] == 3.0, "same name and unit sum"

    merged = dedupe_ingredients([
        to_ingredient_row("Salt", None, None, qualitative_note="to taste"),
        to_ingredient_row("Salt", None, None, qualitative_note="a pinch"),
    ])
    assert len(merged) == 1 and merged[0]["is_approximate"]

    # --- source text guard -------------------------------------------------
    assert clean_source_text("  hello   world  ") == "hello world"
    assert clean_source_text("pasta 🍅🔥 recipe") == "pasta recipe", "emoji stripped, whitespace collapsed"
    assert clean_source_text("a​b") == "ab", "zero-width stripped"
    assert "½" in clean_source_text("½ cup"), "fractions survive"
    assert clean_source_text("x" * 20000).endswith("[…truncated]")
    for bad in (None, "", "   ", "​"):
        try:
            clean_source_text(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass

    print("normalize: all self-tests passed")
