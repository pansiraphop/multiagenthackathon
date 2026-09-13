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
        description="Numeric amount, or null when the source gives a qualitative "
                    "amount. Never invent a number.",
    )
    unit: Unit = Field(description="One of the fixed units. Countable items use 'unit'.")
    qualitative_note: str | None = Field(
        default=None,
        description="Required when quantity is null: the source's own phrasing, "
                    "e.g. 'a good glug', 'to taste', 'a handful'.",
    )


class ExtractedRecipe(BaseModel):
    title: str
    cuisine: Cuisine
    est_time_minutes: int = Field(
        description="Active cooking time in minutes, excluding marinating or chilling."
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
            f"{config.EST_TIME_MIN} and {config.EST_TIME_MAX}"
        )
    if r.servings <= 0:
        problems.append(f"servings is {r.servings}, must be positive")
    if not r.steps:
        problems.append("steps is empty")
    if not r.ingredients:
        problems.append("ingredients is empty")
    if not r.title.strip():
        problems.append("title is empty")

    for ing in r.ingredients:
        if not ing.name.strip():
            problems.append("an ingredient has an empty name")
            continue
        if ing.quantity is not None and ing.quantity <= 0:
            problems.append(
                f"{ing.name}: quantity is {ing.quantity}; use a positive number "
                f"or null with a qualitative_note"
            )
        if ing.quantity is None and not ing.qualitative_note:
            problems.append(
                f"{ing.name}: quantity is null, so qualitative_note is required"
            )

    return problems


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
        title="Palak Paneer", cuisine="indian", est_time_minutes=40, servings=4,
        steps=["Blanch the spinach.", "Fry the paneer.", "Simmer together."],
        ingredients=[
            Ingredient(name="spinach", quantity=400, unit="g"),
            Ingredient(name="paneer", quantity=200, unit="g"),
            Ingredient(name="olive oil", quantity=None, unit="unit",
                       qualitative_note="a good glug"),
        ],
        source_sufficiency="complete",
    )
    assert validate_recipe(good) == []
    assert not needs_reconstruction(good)

    bad = good.model_copy(update={"est_time_minutes": 400, "steps": []})
    problems = validate_recipe(bad)
    assert any("est_time_minutes" in p for p in problems)
    assert any("steps is empty" in p for p in problems)

    # Null quantity without a note is the silent-corruption case.
    nullq = good.model_copy(update={
        "ingredients": [Ingredient(name="salt", quantity=None, unit="tsp")]
    })
    assert any("qualitative_note is required" in p for p in validate_recipe(nullq))

    # Gate
    assert needs_reconstruction(good.model_copy(update={"source_sufficiency": "partial"}))
    assert needs_reconstruction(good.model_copy(update={
        "ingredients": [Ingredient(name="spinach", quantity=1, unit="g")]
    })), "fewer than 3 ingredients escalates even when the model says complete"

    print("schemas: all self-tests passed")
