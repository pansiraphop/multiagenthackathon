"""Supabase access. Every stage talks to the database through here.

Stages never import each other — database rows are the only interface between
them. That is what lets both paths be built in parallel.
"""

from __future__ import annotations

import functools
from typing import Any

from supabase import Client, create_client

import config


@functools.lru_cache(maxsize=1)
def client() -> Client:
    """Cached Supabase client. Raises early with a useful message if unconfigured."""
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_KEY missing. Copy .env.example to .env and fill "
            "them in (see DATABASE.md)."
        )
    return create_client(config.SUPABASE_URL, config.SUPABASE_KEY)


# --- generic helpers -------------------------------------------------------

def insert(table: str, rows: dict | list[dict]) -> list[dict]:
    """Insert one row or many. Returns the inserted rows, ids included."""
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return []
    return client().table(table).insert(rows).execute().data or []


def update(table: str, row_id: str, values: dict) -> list[dict]:
    return client().table(table).update(values).eq("id", row_id).execute().data or []


def select(table: str, columns: str = "*", **eq) -> list[dict]:
    """select(columns) with equality filters: select('pantry', ingredient_name='tomato')."""
    q = client().table(table).select(columns)
    for key, value in eq.items():
        q = q.eq(key, value)
    return q.execute().data or []


def delete_where(table: str, **eq) -> None:
    """Used by the delete-then-insert idempotency rule on derived tables."""
    q = client().table(table).delete()
    for key, value in eq.items():
        q = q.eq(key, value)
    q.execute()


# --- stage 1 write path ----------------------------------------------------

def insert_recipe(
    *,
    raw_caption: str | None = None,
    raw_transcript: str | None = None,
    source_url: str | None = None,
    title: str | None = None,
    cuisine: str | None = None,
    est_time_minutes: int | None = None,
    total_time_minutes: int | None = None,
    advance_prep_minutes: int = 0,
    servings: int = 2,
    steps: list[str] | None = None,
    extraction_status: str = "pending",
    provenance: str = "transcript",
    source_sufficiency: str | None = None,
) -> dict:
    """Insert a recipes row. id, created_at and updated_at default server-side."""
    row = {
        "raw_caption": raw_caption,
        "raw_transcript": raw_transcript,
        "source_url": source_url,
        "title": title,
        "cuisine": cuisine,
        "est_time_minutes": est_time_minutes,
        # Falls back to active time when the caller has nothing better. The
        # planner fits on this, so it must never be null for a success row.
        "total_time_minutes": (total_time_minutes
                               if total_time_minutes is not None
                               else est_time_minutes),
        "advance_prep_minutes": advance_prep_minutes,
        "servings": servings,
        "steps": steps or [],
        "extraction_status": extraction_status,
        "provenance": provenance,
        "source_sufficiency": source_sufficiency,
    }
    return insert("recipes", row)[0]


def insert_ingredients(recipe_id: str, ingredients: list[dict[str, Any]]) -> list[dict]:
    """Insert recipe_ingredients.

    Pass rows built by `normalize.to_ingredient_row()` — it guarantees the
    normalized name, a usable quantity, a canonical unit and the
    is_approximate flag. Anything else risks the silent pantry-mismatch bug.
    """
    rows = [
        {
            "recipe_id": recipe_id,
            "name": ing["name"],
            "quantity": ing.get("quantity"),
            "unit": ing.get("unit"),
            "qualitative_note": ing.get("qualitative_note"),
            "is_approximate": bool(ing.get("is_approximate", False)),
        }
        for ing in ingredients
        if ing.get("name")
    ]
    return insert("recipe_ingredients", rows) if rows else []


def get_recipe(recipe_id: str) -> dict | None:
    rows = select("recipes", "*", id=recipe_id)
    return rows[0] if rows else None


def get_recipe_with_ingredients(recipe_id: str) -> dict | None:
    """A recipe plus its ingredient rows under an 'ingredients' key."""
    recipe = get_recipe(recipe_id)
    if not recipe:
        return None
    recipe["ingredients"] = select("recipe_ingredients", "*", recipe_id=recipe_id)
    return recipe


def successful_recipes() -> list[dict]:
    """Recipes the planner is allowed to consider.

    Filtering on extraction_status is not optional: title, est_time_minutes and
    steps are nullable, so a pending or failed row would blow up the planner's
    slot-fitting comparison.
    """
    rows = (
        client()
        .table("recipes")
        .select("*, recipe_ingredients(*)")
        .eq("extraction_status", "success")
        .execute()
        .data
        or []
    )
    for r in rows:
        r["ingredients"] = r.pop("recipe_ingredients", [])
    return rows
