"""Stage 7 — the web app the calendar invite links to.

    uvicorn web.app:app --reload --port 8000

Three pages, server-rendered, no build step and no new dependencies:

    /                     this week's meals
    /cook/{meal_plan_id}  the recipe page (what the calendar links to)
    /pantry               what's in the fridge

Why server-rendered: the data already lives in Supabase and `lib.db` already
reads it, so a client-side app would mean shipping keys to the browser and
re-implementing the same queries in JavaScript for no benefit. This keeps the
Supabase key server-side and the page fast on a phone on a kitchen wifi.

`APP_BASE_URL` must point at wherever this is deployed BEFORE stage 6 runs —
the cook URL is baked into each calendar description at write time.

The guided-voice button is deliberately present and disabled. The page already
carries the recipe context the agent will need (see `voice_payload`), so
wiring ElevenLabs in later is one function, not a redesign.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import config
from lib import db
from lib.normalize import normalize_ingredient
from stages.plan import usable_pantry
from web import views

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="InstaCook")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def _week_meals(week_start: date | None = None) -> list[dict]:
    """Meals for the week, each with its recipe attached."""
    week = (week_start or config.week_start()).isoformat()
    meals = sorted(db.select("meal_plan", "*", week_start_date=week),
                   key=lambda m: m["planned_start_time"])
    recipes = {r["id"]: r for r in db.successful_recipes()}
    for meal in meals:
        meal["recipe"] = recipes.get(meal["recipe_id"])
    return [m for m in meals if m["recipe"]]


def _week_order(week_start: date | None = None) -> dict | None:
    """The week's Instacart order.

    One cart for the whole week on purpose: the point of InstaCook is
    collating several reels into a single shop that already accounts for what
    the pantry covers. A cart per meal would re-buy the same onion five times.
    """
    week = (week_start or config.week_start()).isoformat()
    rows = db.select("instacart_orders", "*", week_start_date=week)
    return rows[0] if rows else None


def voice_payload(meal: dict) -> dict:
    """Everything the guided-cooking agent will need, assembled now.

    Stage 7's voice half isn't built yet. Shipping the context with the page
    means turning it on is a matter of handing this to the agent, rather than
    reshaping the page around it later.
    """
    recipe = meal["recipe"]
    return {
        "meal_id": meal["id"],
        "title": recipe.get("title"),
        "servings": recipe.get("servings"),
        "steps": recipe.get("steps") or [],
        "ingredients": [
            {"name": i["name"], "quantity": i.get("quantity"),
             "unit": i.get("unit"), "approximate": i.get("is_approximate")}
            for i in recipe.get("ingredients", [])
        ],
    }


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    week_start = config.week_start()
    return views.week_page(
        week_start=week_start,
        meals=_week_meals(week_start),
        order=_week_order(week_start),
    )


@app.get("/cook/{meal_id}", response_class=HTMLResponse)
def cook(meal_id: str) -> str:
    """What the calendar invite links to."""
    rows = db.select("meal_plan", "*", id=meal_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No such meal")
    meal = rows[0]

    recipe = db.get_recipe_with_ingredients(meal["recipe_id"])
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe missing")
    meal["recipe"] = recipe

    week = date.fromisoformat(meal["week_start_date"])
    return views.cook_page(
        meal=meal,
        order=_week_order(week),
        voice=voice_payload(meal),
    )


@app.get("/pantry", response_class=HTMLResponse)
def pantry() -> str:
    return views.pantry_page(
        items=sorted(db.select("pantry"),
                     key=lambda r: (r["expiry_date"] or "9999-99-99",
                                    r["ingredient_name"])),
        today=config.now_local().date(),
        usable=set(usable_pantry()),
    )


# ---------------------------------------------------------------------------
# pantry writes
#
# The real path for these is the Instagram agent: you tell it what you bought
# and it adds them here. This form is the manual fallback and the way to see
# the data model working before that lands.
# ---------------------------------------------------------------------------

@app.post("/pantry/add")
def pantry_add(
    name: str = Form(...),
    quantity: str = Form(""),
    unit: str = Form("unit"),
    expiry_date: str = Form(""),
) -> RedirectResponse:
    clean_name, qty, clean_unit = normalize_ingredient(name, quantity or None, unit)
    if clean_name:
        existing = db.select("pantry", "*", ingredient_name=clean_name)
        row = {
            "ingredient_name": clean_name,
            "quantity": qty if qty is not None else 1,
            "unit": clean_unit,
            "expiry_date": expiry_date or None,
        }
        if existing:
            db.update("pantry", existing[0]["id"], row)
        else:
            db.insert("pantry", row)
    return RedirectResponse("/pantry", status_code=303)


@app.post("/pantry/remove")
def pantry_remove(item_id: str = Form(...)) -> RedirectResponse:
    db.delete_where("pantry", id=item_id)
    return RedirectResponse("/pantry", status_code=303)
