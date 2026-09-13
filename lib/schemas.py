"""Extraction schema, semantic validator, and the escalation gate.

Drop-in for stage 1 (`extract.py`). Structured outputs make these shapes
schema-valid at the API level; everything here is the semantic layer on top.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

import config

Unit = Literal["g", "kg", "ml", "l", "cup", "tbsp", "tsp", "unit"]

Cuisine = Literal[
    "italian", "mexican", "indian", "chinese", "japanese", "thai",
    "mediterranean", "american", "korean", "middle_eastern", "other",
]

Sufficiency = Literal["complete", "partial", "insufficient"]


class Ingredient(BaseModel):
    name: str = Field(
        description="Lowercase, singular, no descriptors or amounts. "
                    "'2 large ripe Tomatoes, diced' -> 'tomato'."
    )
    quantity: float | None = Field(
        default=None,
        description="Numeric amount, ALWAYS filled in. When the source gives a "
                    "qualitative amount ('a good glug'), estimate a sensible "
                    "number for this specific ingredient and set is_approximate "
                    "true. Null only if there is genuinely nothing to estimate "
                    "from.",
    )
    unit: Unit = Field(description="One of the fixed units. Countable items use 'unit'.")
    is_approximate: bool = Field(
        default=False,
        description="True when quantity is your estimate of a qualitative amount "
                    "or a range, rather than a number the source stated. The cook "
                    "sees this as '~2 tbsp (a good glug)'.",
    )
    qualitative_note: str | None = Field(
        default=None,
        description="Required whenever is_approximate is true: the source's own "
                    "phrasing, e.g. 'a good glug', 'to taste', 'a handful'.",
    )


class ExtractedRecipe(BaseModel):
    title: str
    cuisine: Cuisine
    est_time_minutes: int = Field(
        description="ACTIVE hands-on minutes only: chopping, searing, stirring, "
                    "plating. Not waiting time of any kind."
    )
    total_time_minutes: int = Field(
        description="The whole span the cook must be AT HOME, from starting to "
                    "eating. Active work plus unattended cooking they have to be "
                    "around for: braising, roasting, baking, simmering, resting a "
                    "roast. Equals est_time_minutes for a quick stir-fry; much "
                    "larger for a two-hour braise. Must be >= est_time_minutes.",
    )
    advance_prep_minutes: int = Field(
        default=0,
        description="Lead time needed BEFORE that session, during which the cook "
                    "need not be present: marinating, brining, chilling, soaking, "
                    "overnight rising, freezing. 0 if none. An overnight marinade "
                    "is roughly 720.",
    )
    servings: int
    steps: list[str] = Field(
        description="Ordered, imperative, one action per step. No engagement filler."
    )
    ingredients: list[Ingredient]
    source_sufficiency: Sufficiency = Field(
        description="How much of this came from the source rather than your own "
                    "knowledge. 'complete' = the source specified the ingredients "
                    "and method. 'insufficient' = the source barely described the "
                    "dish at all."
    )


class DishIdentification(BaseModel):
    """First step of the reconstruction path: what dish is this, if any?"""

    dish_name: str | None = Field(
        default=None,
        description="The specific dish, as precisely as the source supports "
                    "('gochujang butter noodles', not 'noodles'). Null if the "
                    "source gives no usable signal at all.",
    )
    confident: bool = Field(
        description="True only if the source genuinely identifies the dish. "
                    "Guessing from a generic food shot is not confidence."
    )
    reasoning: str = Field(description="One sentence on what the signal was.")


class MealReasons(BaseModel):
    """Batched score_reason generation for stage 3 — one call for the whole week."""

    reasons: list[str] = Field(
        description="One per meal, in the order given. Under 20 words each, "
                    "no preamble, states why this meal and why this slot."
    )


# --- semantic validation ---------------------------------------------------

def validate_recipe(r: ExtractedRecipe) -> list[str]:
    """Problems to feed back to the model. Empty list means acceptable."""
    problems: list[str] = []

    if not (config.EST_TIME_MIN <= r.est_time_minutes <= config.EST_TIME_MAX):
        problems.append(
            f"est_time_minutes is {r.est_time_minutes}, must be between "
            f"{config.EST_TIME_MIN} and {config.EST_TIME_MAX} (active work only)"
        )
    if not (config.EST_TIME_MIN <= r.total_time_minutes <= config.TOTAL_TIME_MAX):
        problems.append(
            f"total_time_minutes is {r.total_time_minutes}, must be between "
            f"{config.EST_TIME_MIN} and {config.TOTAL_TIME_MAX}"
        )
    if r.total_time_minutes < r.est_time_minutes:
        problems.append(
            f"total_time_minutes ({r.total_time_minutes}) is less than "
            f"est_time_minutes ({r.est_time_minutes}); the attended span cannot "
            f"be shorter than the hands-on work inside it"
        )
    if not (0 <= r.advance_prep_minutes <= config.MAX_ADVANCE_PREP_MINUTES):
        problems.append(
            f"advance_prep_minutes is {r.advance_prep_minutes}, must be between 0 "
            f"and {config.MAX_ADVANCE_PREP_MINUTES}"
        )
    # Waiting that requires presence belongs in total_time_minutes; waiting that
    # doesn't belongs in advance_prep_minutes. Putting a braise in the latter
    # would let the planner book a 40-minute window for a 3-hour dish.
    if r.advance_prep_minutes and r.total_time_minutes == r.est_time_minutes:
        for step in r.steps:
            low = step.lower()
            if any(word in low for word in
                   ("braise", "roast for", "bake for", "simmer for", "slow cook")):
                problems.append(
                    "a step describes unattended cooking that needs the cook at "
                    "home, but total_time_minutes equals est_time_minutes; that "
                    "time belongs in total_time_minutes, not advance_prep_minutes"
                )
                break
    if r.servings <= 0:
        problems.append(f"servings is {r.servings}, must be positive")
    if not r.steps:
        problems.append("steps is empty")
    if not r.ingredients:
        problems.append("ingredients is empty")
    if not r.title.strip():
        problems.append("title is empty")

    seen: set[str] = set()
    for ing in r.ingredients:
        if not ing.name.strip():
            problems.append("an ingredient has an empty name")
            continue

        key = ing.name.strip().lower()
        if key in seen:
            problems.append(f"{ing.name}: listed twice; combine into one entry")
        seen.add(key)

        if ing.quantity is not None and ing.quantity <= 0:
            problems.append(
                f"{ing.name}: quantity is {ing.quantity}; give a positive estimate "
                f"with is_approximate true"
            )
        if ing.quantity is None:
            problems.append(
                f"{ing.name}: quantity is null. Estimate a usable amount for this "
                f"ingredient and set is_approximate true — a cook at the stove "
                f"cannot act on a missing amount"
            )
        if ing.is_approximate and not ing.qualitative_note:
            problems.append(
                f"{ing.name}: is_approximate is true, so qualitative_note must "
                f"hold the source's phrasing"
            )
        # A number the source never stated must be labelled, or the cook can't
        # tell an estimate from a measurement.
        if (ing.quantity is not None and not ing.is_approximate
                and ing.qualitative_note):
            problems.append(
                f"{ing.name}: has a qualitative_note but is_approximate is false; "
                f"set it true so the amount is shown as an estimate"
            )

    return problems


def attended_minutes(recipe) -> int:
    """How long the cook is committed for. What the planner must fit on.

    Accepts an ExtractedRecipe or a recipes row. Falls back to active time when
    total is missing, which is the only safe default: better to under-book a
    slot than to silently schedule a three-hour braise into forty minutes.
    """
    if isinstance(recipe, dict):
        total = recipe.get("total_time_minutes")
        active = recipe.get("est_time_minutes")
    else:
        total = getattr(recipe, "total_time_minutes", None)
        active = getattr(recipe, "est_time_minutes", None)
    return int(total or active or 0)


def advance_prep(recipe) -> int:
    """Lead time needed before the cook session, in minutes. 0 for most dishes."""
    if isinstance(recipe, dict):
        return int(recipe.get("advance_prep_minutes") or 0)
    return int(getattr(recipe, "advance_prep_minutes", 0) or 0)


# --- the escalation gate ---------------------------------------------------

def needs_reconstruction(r: ExtractedRecipe) -> bool:
    """Whether to escalate to the web-research path.

    Deliberately deterministic Python rather than a model decision: the branch
    has to be reproducible and countable. "4 of 6 reels had usable transcripts,
    2 were reconstructed" only exists if this is a hard rule.
    """
    return (
        r.source_sufficiency != "complete"
        or len(r.ingredients) < config.MIN_INGREDIENTS_FOR_COMPLETE
        or len(r.steps) < config.MIN_STEPS_FOR_COMPLETE
    )


if __name__ == "__main__":
    good = ExtractedRecipe(
        title="Palak Paneer", cuisine="indian", est_time_minutes=40,
        total_time_minutes=40, advance_prep_minutes=0, servings=4,
        steps=["Blanch the spinach.", "Fry the paneer.", "Simmer together."],
        ingredients=[
            Ingredient(name="spinach", quantity=400, unit="g"),
            Ingredient(name="paneer", quantity=200, unit="g"),
            # A qualitative amount, estimated and labelled — the shape we now
            # want, because a cook can act on "~2 tbsp".
            Ingredient(name="olive oil", quantity=2, unit="tbsp",
                       is_approximate=True, qualitative_note="a good glug"),
        ],
        source_sufficiency="complete",
    )
    assert validate_recipe(good) == []
    assert not needs_reconstruction(good)

    bad = good.model_copy(update={"est_time_minutes": 400, "steps": []})
    problems = validate_recipe(bad)
    assert any("est_time_minutes" in p for p in problems)
    assert any("steps is empty" in p for p in problems)

    # A null quantity is now itself the problem: it's unusable at the stove.
    nullq = good.model_copy(update={
        "ingredients": [Ingredient(name="salt", quantity=None, unit="tsp")]
    })
    assert any("quantity is null" in p for p in validate_recipe(nullq))

    # An estimate must carry the source's phrasing...
    unlabelled = good.model_copy(update={
        "ingredients": [Ingredient(name="olive oil", quantity=2, unit="tbsp",
                                   is_approximate=True)]
    })
    assert any("qualitative_note must" in p for p in validate_recipe(unlabelled))

    # ...and a number derived from a qualitative phrase must be flagged, or the
    # cook can't tell an estimate from a measurement.
    unflagged = good.model_copy(update={
        "ingredients": [Ingredient(name="olive oil", quantity=2, unit="tbsp",
                                   qualitative_note="a good glug")]
    })
    assert any("set it true" in p for p in validate_recipe(unflagged))

    # Duplicates would become two shopping rows and a constraint violation.
    dupes = good.model_copy(update={
        "ingredients": [
            Ingredient(name="olive oil", quantity=1, unit="tbsp"),
            Ingredient(name="Olive Oil", quantity=2, unit="tbsp"),
        ]
    })
    assert any("listed twice" in p for p in validate_recipe(dupes))

    # Timing: the attended span can't be shorter than the work inside it.
    inverted = good.model_copy(update={"total_time_minutes": 20})
    assert any("less than" in p for p in validate_recipe(inverted))

    braise = good.model_copy(update={
        "est_time_minutes": 30, "total_time_minutes": 150, "advance_prep_minutes": 0})
    assert validate_recipe(braise) == [], "a long attended braise is valid"
    assert attended_minutes(braise) == 150, "the planner must fit on the attended span"

    # Unattended-but-present time misfiled as advance prep is the dangerous case.
    misfiled = good.model_copy(update={
        "est_time_minutes": 30, "total_time_minutes": 30,
        "advance_prep_minutes": 120,
        "steps": ["Sear the beef.", "Braise for two hours."]})
    assert any("belongs in total_time_minutes" in p for p in validate_recipe(misfiled))

    # Falls back to active time when total is missing.
    assert attended_minutes({"est_time_minutes": 25}) == 25
    assert attended_minutes({"total_time_minutes": 150, "est_time_minutes": 30}) == 150
    assert advance_prep({"advance_prep_minutes": None}) == 0

    # Gate
    assert needs_reconstruction(good.model_copy(update={"source_sufficiency": "partial"}))
    assert needs_reconstruction(good.model_copy(update={
        "ingredients": [Ingredient(name="spinach", quantity=1, unit="g")]
    })), "fewer than 3 ingredients escalates even when the model says complete"

    print("schemas: all self-tests passed")
