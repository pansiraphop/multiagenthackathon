"""HTML for the InstaCook pages.

Plain functions returning strings — no template engine, no build step, one
less dependency to install at 2pm. Every value that reaches the page goes
through `esc()`.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta

import config
from lib.normalize import render_amount
from lib.schemas import advance_prep, attended_minutes


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def layout(title: str, body: str, *, nav: str = "", bar: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{esc(title)} · InstaCook</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<div class="wrap">
  <div class="top">
    <a class="brand" href="/">Insta<span>Cook</span></a>
    <nav>{nav}</nav>
  </div>
  {body}
  <footer class="page">
    <span>Recipes pulled from your saved reels.</span>
    <a href="/pantry">Pantry</a>
  </footer>
</div>
{bar}
<script src="/static/app.js"></script>
</body>
</html>"""


def _nav(current: str) -> str:
    items = [("/", "This week"), ("/pantry", "Pantry")]
    return "".join(
        f'<a href="{href}"{" aria-current=\'page\'" if href == current else ""}>'
        f"{esc(label)}</a>"
        for href, label in items
    )


def _local(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(config.TIMEZONE)


# ---------------------------------------------------------------------------
# this week
# ---------------------------------------------------------------------------

def week_page(week_start: date, meals: list[dict], order: dict | None) -> str:
    if meals:
        rows = []
        for meal in meals:
            recipe = meal["recipe"]
            start = _local(meal["planned_start_time"])
            minutes = attended_minutes(recipe)
            meta = [f"{minutes} min"]
            if recipe.get("cuisine"):
                meta.append(esc(recipe["cuisine"]))
            if recipe.get("servings"):
                meta.append(f"serves {recipe['servings']}")
            rows.append(f"""
    <a class="meal" href="/cook/{esc(meal['id'])}">
      <div class="meal-when">{start:%A} &middot; {start:%H:%M}</div>
      <div class="meal-title">{esc(recipe.get('title') or 'Untitled')}</div>
      <div class="meal-meta">{' &middot; '.join(meta)}</div>
    </a>""")
        body_meals = f'<div class="meals">{"".join(rows)}</div>'
    else:
        body_meals = ('<div class="empty">No meals planned yet. '
                      'Run the planner and they will appear here.</div>')

    cart = ""
    if order and order.get("cart_url"):
        built = order.get("method") != "fallback_links"
        count = order.get("item_count") or order.get("unresolved_item_count") or 0
        note = ("Everything these meals need that isn't already in your pantry."
                if built else
                "No cart could be built automatically — this opens a search so "
                "you can add the items yourself.")
        cart = f"""
  <h2>Shopping</h2>
  <p class="muted tiny" style="margin-top:0">{esc(note)}</p>
  <a class="btn" href="{esc(order['cart_url'])}" target="_blank" rel="noopener">
    Open shopping list &middot; {count} items
  </a>"""

    ends = week_start + timedelta(days=6)
    body = f"""
  <p class="eyebrow">{week_start.day} {week_start:%b} &ndash; {ends.day} {ends:%b}</p>
  <h1>Your week</h1>
  <p class="lede">Five meals, fitted around what's already on your calendar and
  what's about to go off in your fridge.</p>
  <h2>Meals</h2>
  {body_meals}
  {cart}
"""
    return layout("Your week", body, nav=_nav("/"))


# ---------------------------------------------------------------------------
# one recipe
# ---------------------------------------------------------------------------

def cook_page(meal: dict, order: dict | None, voice: dict) -> str:
    recipe = meal["recipe"]
    start = _local(meal["planned_start_time"])
    active = recipe.get("est_time_minutes")
    attended = attended_minutes(recipe)

    facts = []
    if active and active != attended:
        facts.append(f"<span><b>{active} min</b> hands-on</span>")
        facts.append(f"<span><b>{attended} min</b> total</span>")
    else:
        facts.append(f"<span><b>{attended} min</b></span>")
    if recipe.get("servings"):
        facts.append(f"<span>serves <b>{recipe['servings']}</b></span>")
    if recipe.get("cuisine"):
        facts.append(f"<span>{esc(recipe['cuisine'])}</span>")

    notices = []
    lead = advance_prep(recipe)
    if lead:
        begin = _local(meal["planned_start_time"]) - timedelta(minutes=lead)
        notices.append(f"""
  <div class="notice">
    <strong>Start ahead</strong>
    This needs {lead // 60} hours of marinating or chilling first — begin by
    <b>{begin:%A %H:%M}</b>.
  </div>""")
    if recipe.get("provenance") == "reconstructed":
        notices.append("""
  <div class="notice">
    <strong>Reconstructed</strong>
    The reel didn't include a full recipe, so this was rebuilt from the dish
    name and a few reputable sources. Worth a read before you shop.
  </div>""")

    ingredients = recipe.get("ingredients", [])
    items = []
    for index, ing in enumerate(ingredients):
        row = {
            "quantity": float(ing["quantity"]) if ing["quantity"] is not None else None,
            "unit": ing.get("unit"),
            "qualitative_note": ing.get("qualitative_note"),
            "is_approximate": ing.get("is_approximate"),
        }
        amount = render_amount(row)
        approx = ' <span class="approx">est.</span>' if ing.get("is_approximate") else ""
        items.append(f"""
    <li><label class="check">
      <input type="checkbox" data-key="ing:{esc(meal['id'])}:{index}">
      <span class="box" aria-hidden="true"></span>
      <span class="label">{esc(ing['name'])}</span>
      <span class="amount">{esc(amount)}{approx}</span>
    </label></li>""")

    steps = recipe.get("steps") or []
    step_items = "".join(
        f'<li data-key="step:{esc(meal["id"])}:{n}">'
        f'<span class="step-text">{esc(step)}</span></li>'
        for n, step in enumerate(steps)
    )

    reason = ""
    if meal.get("score_reason"):
        reason = f'<p class="lede">{esc(meal["score_reason"])}</p>'

    reel = ""
    if recipe.get("source_url"):
        reel = (f'<a href="{esc(recipe["source_url"])}" target="_blank" '
                f'rel="noopener">Watch the original reel</a>')

    cart_href = (order or {}).get("cart_url")
    cart_btn = (
        f'<a class="btn btn-ghost" href="{esc(cart_href)}" target="_blank" '
        f'rel="noopener">Shopping list</a>'
        if cart_href else
        '<a class="btn btn-ghost" href="/">Your week</a>'
    )

    body = f"""
  <p class="eyebrow">{start:%A} &middot; {start:%H:%M}</p>
  <h1>{esc(recipe.get('title') or 'Untitled')}</h1>
  {reason}
  <div class="facts">{''.join(facts)}</div>
  {''.join(notices)}

  <div class="section-head">
    <h2>Ingredients</h2>
    <span class="progress" data-progress="ing:{esc(meal['id'])}"></span>
  </div>
  <ul class="rows" data-checklist="ing:{esc(meal['id'])}">{''.join(items)}</ul>

  <div class="section-head">
    <h2>Method</h2>
    <span class="progress" data-progress="step:{esc(meal['id'])}"></span>
  </div>
  <ol class="steps" data-steps="step:{esc(meal['id'])}">{step_items}</ol>

  <p class="tiny muted" style="margin-top:28px">{reel}</p>
  <div class="bar-spacer"></div>
"""

    # The voice half isn't wired yet. The button is present and honest about
    # it, and the context the agent will need already ships with the page.
    bar = f"""
<div class="bar">
  <div class="inner">
    <button class="btn" id="voice" aria-disabled="true" disabled>
      Guided cooking
    </button>
    {cart_btn}
    <p class="note">Hands-free voice guidance is coming next.</p>
  </div>
</div>
<script id="cook-context" type="application/json">{json.dumps(voice)}</script>"""

    return layout(recipe.get("title") or "Recipe", body,
                  nav=_nav(""), bar=bar)


# ---------------------------------------------------------------------------
# pantry
# ---------------------------------------------------------------------------

def _expiry_chip(expiry: str | None, today: date) -> str:
    if not expiry:
        return '<span class="chip chip-quiet">staple</span>'
    days = (date.fromisoformat(expiry) - today).days
    if days < 0:
        return '<span class="chip chip-urgent">expired</span>'
    if days == 0:
        return '<span class="chip chip-urgent">today</span>'
    if days <= 3:
        return f'<span class="chip chip-urgent">{days}d left</span>'
    if days <= 7:
        return f'<span class="chip chip-warn">{days}d left</span>'
    return f'<span class="chip chip-fresh">{days}d</span>'


def pantry_page(items: list[dict], today: date, usable: set[str]) -> str:
    groups: dict[str, list[dict]] = {"Use soon": [], "This week": [], "Staples": []}
    for item in items:
        expiry = item.get("expiry_date")
        if not expiry:
            groups["Staples"].append(item)
            continue
        days = (date.fromisoformat(expiry) - today).days
        groups["Use soon" if days <= 3 else "This week"].append(item)

    sections = []
    for name, rows in groups.items():
        if not rows:
            continue
        lines = []
        for item in rows:
            qty = item.get("quantity")
            amount = ""
            if qty is not None:
                pretty = f"{float(qty):g}"
                unit = item.get("unit") or "unit"
                amount = pretty if unit == "unit" else f"{pretty} {unit}"
            expired = (item.get("expiry_date")
                       and item["ingredient_name"] not in usable)
            dim = ' style="opacity:.55"' if expired else ""
            lines.append(f"""
    <li><div class="check"{dim}>
      <span class="label">{esc(item['ingredient_name'])}</span>
      <span class="amount">{esc(amount)}</span>
      {_expiry_chip(item.get('expiry_date'), today)}
      <form method="post" action="/pantry/remove">
        <input type="hidden" name="item_id" value="{esc(item['id'])}">
        <button class="link-remove" type="submit"
                aria-label="Remove {esc(item['ingredient_name'])}">Remove</button>
      </form>
    </div></li>""")
        sections.append(f'<h2>{esc(name)}</h2><ul class="rows">{"".join(lines)}</ul>')

    if not sections:
        sections = ['<div class="empty">Your pantry is empty. Add something '
                    'below, or tell the Instagram agent what you have.</div>']

    units = "".join(
        f'<option value="{u}">{u}</option>' for u in
        ("unit", "g", "kg", "ml", "l", "cup", "tbsp", "tsp")
    )

    body = f"""
  <p class="eyebrow">{len(items)} items</p>
  <h1>Pantry</h1>
  <p class="lede">What you already have. The planner reaches for whatever is
  closest to going off, and the shopping list only covers the gap.</p>

  <div class="notice" style="border-left-color: var(--clay); background: var(--clay-soft)">
    <strong style="color: var(--clay)">Coming next</strong>
    Tell the Instagram agent what you bought and it will fill this in for you.
    Until then, add things by hand below.
  </div>

  {''.join(sections)}

  <h2>Add something</h2>
  <form class="add-form" method="post" action="/pantry/add">
    <div class="field">
      <label for="name">Ingredient</label>
      <input id="name" name="name" type="text" required placeholder="spinach"
             autocapitalize="none" autocomplete="off">
    </div>
    <div class="pair">
      <div class="field">
        <label for="quantity">Amount</label>
        <input id="quantity" name="quantity" type="text" inputmode="decimal"
               placeholder="400">
      </div>
      <div class="field">
        <label for="unit">Unit</label>
        <select id="unit" name="unit">{units}</select>
      </div>
    </div>
    <div class="field">
      <label for="expiry_date">Use by</label>
      <input id="expiry_date" name="expiry_date" type="date">
    </div>
    <button class="btn" type="submit">Add</button>
  </form>
"""
    return layout("Pantry", body, nav=_nav("/pantry"))
