"""HTML for the InstaCook pages.

Plain functions returning strings — no template engine, no build step. Every
value that reaches the page goes through `esc()`.

Layout follows the delivery apps: a card grid and a persistent action rail on
desktop, a single column ending in a thumb-reach bar on mobile. Those aren't
the same layout at different widths; they're different layouts, which is why
the rail and the bar are separate elements rather than one that reflows.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta

import config
from lib.normalize import render_amount
from lib.schemas import advance_prep, attended_minutes

# Food leads, but we have no photography. A deterministic colour per cuisine
# plus the dish monogram reads as a designed tile rather than a missing image —
# and it makes the week scannable by colour, which a list of titles never is.
CUISINE_TILES = {
    "italian":        ("#F3E2D8", "#A2472A"),
    "indian":         ("#F7EBCF", "#96631A"),
    "japanese":       ("#DFE6EC", "#3B5670"),
    "korean":         ("#F6DED9", "#A43524"),
    "thai":           ("#E2EDDC", "#4A6B3C"),
    "mexican":        ("#F8E4CE", "#9C5514"),
    "chinese":        ("#F6DCD9", "#94302C"),
    "mediterranean":  ("#E7EADA", "#5A6634"),
    "american":       ("#EEE6DA", "#7A5B39"),
    "middle_eastern": ("#F3E7D4", "#8A6222"),
    "other":          ("#ECE7DE", "#6A6258"),
}

DARK_TILES = {
    "italian":        ("#2E1E17", "#E09266"),
    "indian":         ("#2C2413", "#D9AE5C"),
    "japanese":       ("#1B222A", "#8FB0CC"),
    "korean":         ("#2E1A16", "#E28775"),
    "thai":           ("#1D2719", "#9BBE89"),
    "mexican":        ("#2E2013", "#DFA162"),
    "chinese":        ("#2C1817", "#DE8079"),
    "mediterranean":  ("#22261A", "#AFBB7E"),
    "american":       ("#272017", "#C4A078"),
    "middle_eastern": ("#2A2216", "#D5AC63"),
    "other":          ("#221F1C", "#A09890"),
}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def tile_style(cuisine: str | None) -> str:
    key = (cuisine or "other").lower()
    bg, ink = CUISINE_TILES.get(key, CUISINE_TILES["other"])
    dbg, dink = DARK_TILES.get(key, DARK_TILES["other"])
    return (f"--tile:{bg};--tile-ink:{ink};"
            f"--tile-dark:{dbg};--tile-dark-ink:{dink}")


def monogram(title: str | None) -> str:
    return esc((title or "?").strip()[:1].upper())


def layout(title: str, body: str, *, current: str = "", bar: str = "") -> str:
    nav = "".join(
        f'<a href="{href}"{" aria-current=\'page\'" if href == current else ""}>'
        f"{esc(label)}</a>"
        for href, label in (("/", "This week"), ("/pantry", "Pantry"))
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{esc(title)} · InstaCook</title>
<link rel="stylesheet" href="/static/style.css">
<style>
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) [style*="--tile-dark"] {{
    --tile: var(--tile-dark); --tile-ink: var(--tile-dark-ink);
  }}
}}
</style>
</head>
<body>
<header class="appbar"><div class="inner">
  <a class="brand" href="/"><span class="dot">IC</span> InstaCook</a>
  <nav>{nav}</nav>
</div></header>
<div class="wrap">
  {body}
  <footer class="page">
    <span>Recipes from your saved reels.</span>
    <a href="/pantry">Pantry</a>
  </footer>
</div>
{bar}
<script src="/static/app.js"></script>
</body>
</html>"""


def _local(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(config.TIMEZONE)


def _cart_panel(order: dict | None, *, heading: str = "Shopping") -> str:
    if not (order and order.get("cart_url")):
        return f"""<div class="panel">
    <h3>{esc(heading)}</h3>
    <p class="tiny muted" style="margin:0 0 14px">The shopping list hasn't been
    built for this week yet.</p>
    <a class="btn btn-ghost" href="/">Your week</a>
  </div>"""

    built = order.get("method") != "fallback_links"
    count = order.get("item_count") or order.get("unresolved_item_count") or 0
    window = ""
    if order.get("delivery_window_start") and order.get("delivery_window_end"):
        begin = _local(order["delivery_window_start"])
        finish = _local(order["delivery_window_end"])
        window = f"""<div class="panel-row"><span>Arriving</span>
      <b>{begin:%a} {begin:%H:%M}&ndash;{finish:%H:%M}</b></div>"""

    note = ("" if built else
            '<p class="tiny muted" style="margin:12px 0 0">No cart could be built '
            'automatically, so this opens a search and you add the items.</p>')

    return f"""<div class="panel">
    <h3>{esc(heading)}</h3>
    <div class="panel-rows">
      <div class="panel-row"><span>Items to buy</span><b>{count}</b></div>
      {window}
    </div>
    <a class="btn" href="{esc(order['cart_url'])}" target="_blank" rel="noopener">
      Open shopping list</a>
    {note}
  </div>"""


# ---------------------------------------------------------------------------
# this week
# ---------------------------------------------------------------------------

def week_page(week_start: date, meals: list[dict], order: dict | None,
              pantry_covered: int = 0) -> str:
    cards = []
    for meal in meals:
        recipe = meal["recipe"]
        start = _local(meal["planned_start_time"])
        bits = [f"{attended_minutes(recipe)} min"]
        if recipe.get("servings"):
            bits.append(f"serves {recipe['servings']}")
        if recipe.get("cuisine"):
            bits.append(esc(str(recipe["cuisine"]).replace("_", " ")))

        tags = []
        if advance_prep(recipe):
            tags.append('<span class="pill pill-warn">start ahead</span>')
        if recipe.get("provenance") == "reconstructed":
            tags.append('<span class="pill pill-quiet">reconstructed</span>')

        cards.append(f"""
    <a class="card" href="/cook/{esc(meal['id'])}" style="{tile_style(recipe.get('cuisine'))}">
      <div class="tile">
        <span class="day">{start:%a} {start:%H:%M}</span>
        <span class="mono">{monogram(recipe.get('title'))}</span>
      </div>
      <div class="card-body">
        <div class="card-title">{esc(recipe.get('title') or 'Untitled')}</div>
        <div class="meta">{''.join(f'<span>{b}</span>' for b in bits)}</div>
        {f'<div class="card-tags">{"".join(tags)}</div>' if tags else ''}
      </div>
    </a>""")

    grid = (f'<div class="meals">{"".join(cards)}</div>' if cards else
            '<div class="empty">No meals planned yet. Run the planner and they '
            'will appear here.</div>')

    count = 0
    if order:
        count = order.get("item_count") or order.get("unresolved_item_count") or 0
    ends = week_start + timedelta(days=6)

    body = f"""
  <p class="eyebrow">{week_start.day} {week_start:%b} &ndash; {ends.day} {ends:%b}</p>
  <h1>Your week</h1>
  <p class="lede">Meals fitted around what's already on your calendar and what's
  about to go off in your fridge.</p>

  <div class="stats">
    <div><b>{len(meals)}</b><span>Meals</span></div>
    <div><b>{count}</b><span>To buy</span></div>
    <div><b>{pantry_covered}</b><span>From pantry</span></div>
  </div>

  <div class="shell">
    <main>
      <h2 style="margin-top:0">Scheduled</h2>
      {grid}
    </main>
    <aside class="rail">{_cart_panel(order)}</aside>
  </div>
"""
    return layout("Your week", body, current="/")


# ---------------------------------------------------------------------------
# one recipe
# ---------------------------------------------------------------------------

def cook_page(meal: dict, order: dict | None, voice: dict) -> str:
    recipe = meal["recipe"]
    start = _local(meal["planned_start_time"])
    active = recipe.get("est_time_minutes")
    attended = attended_minutes(recipe)

    bits = []
    if active and active != attended:
        bits += [f"{active} min hands-on", f"{attended} min total"]
    else:
        bits.append(f"{attended} min")
    if recipe.get("servings"):
        bits.append(f"serves {recipe['servings']}")
    if recipe.get("cuisine"):
        bits.append(esc(str(recipe["cuisine"]).replace("_", " ")))

    notices = []
    lead = advance_prep(recipe)
    if lead:
        begin = start - timedelta(minutes=lead)
        notices.append(f"""
  <div class="notice"><span class="mark">!</span><div>
    <strong>Start ahead</strong>
    Needs {lead // 60} hours of marinating or chilling first &mdash; begin by
    <b>{begin:%A %H:%M}</b>.
  </div></div>""")
    if recipe.get("provenance") == "reconstructed":
        notices.append("""
  <div class="notice"><span class="mark">i</span><div>
    <strong>Reconstructed</strong>
    The reel didn't include a full recipe, so this was rebuilt from the dish name
    and a few reputable sources. Worth a read before you shop.
  </div></div>""")

    items = []
    for index, ing in enumerate(recipe.get("ingredients", [])):
        row = {
            "quantity": float(ing["quantity"]) if ing["quantity"] is not None else None,
            "unit": ing.get("unit"),
            "qualitative_note": ing.get("qualitative_note"),
            "is_approximate": ing.get("is_approximate"),
        }
        approx = ' <span class="approx">est</span>' if ing.get("is_approximate") else ""
        items.append(f"""
    <li><label class="check">
      <input type="checkbox" data-key="ing:{esc(meal['id'])}:{index}">
      <span class="box" aria-hidden="true"></span>
      <span class="label">{esc(ing['name'])}</span>
      <span class="amount">{esc(render_amount(row))}{approx}</span>
    </label></li>""")

    steps = "".join(
        f'<li data-key="step:{esc(meal["id"])}:{n}">'
        f'<span class="step-text">{esc(step)}</span></li>'
        for n, step in enumerate(recipe.get("steps") or [])
    )

    reason = (f'<p class="lede" style="margin-bottom:0">{esc(meal["score_reason"])}</p>'
              if meal.get("score_reason") else "")
    reel = (f'<a href="{esc(recipe["source_url"])}" target="_blank" rel="noopener">'
            f'Watch the original reel</a>' if recipe.get("source_url") else "")

    cook_panel = f"""<div class="panel">
    <h3>Cook</h3>
    <div class="panel-rows">
      <div class="panel-row"><span>Scheduled</span><b>{start:%a %d %b, %H:%M}</b></div>
      <div class="panel-row"><span>Time needed</span><b>{attended} min</b></div>
    </div>
    <div class="btn-row">
      <button class="btn" id="voice" aria-disabled="true" disabled>Guided cooking</button>
    </div>
    <p class="tiny muted" style="margin:10px 0 0">Hands-free voice guidance is
    coming next.</p>
  </div>"""

    body = f"""
  <div class="hero" style="{tile_style(recipe.get('cuisine'))}">
    <span class="mono" aria-hidden="true">{monogram(recipe.get('title'))}</span>
    <p class="eyebrow">{start:%A} &middot; {start:%H:%M}</p>
    <h1>{esc(recipe.get('title') or 'Untitled')}</h1>
    <div class="meta">{''.join(f'<span>{b}</span>' for b in bits)}</div>
  </div>

  <div class="shell">
    <main>
      {reason}
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
      <ol class="steps" data-steps="step:{esc(meal['id'])}">{steps}</ol>

      <p class="tiny muted" style="margin-top:26px">{reel}</p>
    </main>
    <aside class="rail">
      {cook_panel}
      {_cart_panel(order, heading="This week's shop")}
    </aside>
  </div>
  <div class="bar-spacer"></div>
"""

    cart_href = (order or {}).get("cart_url")
    secondary = (f'<a class="btn btn-ghost" href="{esc(cart_href)}" target="_blank" '
                 f'rel="noopener">Shopping list</a>' if cart_href else
                 '<a class="btn btn-ghost" href="/">Your week</a>')

    bar = f"""
<div class="bar"><div class="inner">
  <button class="btn" id="voice-mobile" aria-disabled="true" disabled>Guided cooking</button>
  {secondary}
  <p class="note">Hands-free voice guidance is coming next.</p>
</div></div>
<script id="cook-context" type="application/json">{json.dumps(voice)}</script>"""

    return layout(recipe.get("title") or "Recipe", body, bar=bar)


# ---------------------------------------------------------------------------
# pantry
# ---------------------------------------------------------------------------

def _expiry_pill(expiry: str | None, today: date) -> str:
    if not expiry:
        return '<span class="pill pill-quiet">staple</span>'
    days = (date.fromisoformat(expiry) - today).days
    if days < 0:
        return '<span class="pill pill-urgent">expired</span>'
    if days == 0:
        return '<span class="pill pill-urgent">use today</span>'
    if days <= 3:
        return f'<span class="pill pill-urgent">{days}d left</span>'
    if days <= 7:
        return f'<span class="pill pill-warn">{days}d left</span>'
    return f'<span class="pill pill-fresh">{days}d</span>'


def pantry_page(items: list[dict], today: date, usable: set[str]) -> str:
    groups: dict[str, list[dict]] = {"Use soon": [], "This week": [], "Staples": []}
    for item in items:
        expiry = item.get("expiry_date")
        if not expiry:
            groups["Staples"].append(item)
        else:
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
            expired = item.get("expiry_date") and item["ingredient_name"] not in usable
            dim = ' style="opacity:.5"' if expired else ""
            lines.append(f"""
    <li><div class="check"{dim}>
      <span class="label">{esc(item['ingredient_name'])}</span>
      <span class="amount">{esc(amount)}</span>
      {_expiry_pill(item.get('expiry_date'), today)}
      <form method="post" action="/pantry/remove">
        <input type="hidden" name="item_id" value="{esc(item['id'])}">
        <button class="link-remove" type="submit"
                aria-label="Remove {esc(item['ingredient_name'])}">Remove</button>
      </form>
    </div></li>""")
        sections.append(
            f'<div class="section-head"><h2>{esc(name)}</h2>'
            f'<span class="progress">{len(rows)}</span></div>'
            f'<ul class="rows">{"".join(lines)}</ul>')

    if not sections:
        sections = ['<div class="empty">Your pantry is empty. Add something, or '
                    'tell the Instagram agent what you have.</div>']

    units = "".join(f'<option value="{u}">{u}</option>'
                    for u in ("unit", "g", "kg", "ml", "l", "cup", "tbsp", "tsp"))

    soon = len(groups["Use soon"])
    body = f"""
  <p class="eyebrow">Your kitchen</p>
  <h1>Pantry</h1>
  <p class="lede">What you already have. The planner reaches for whatever is
  closest to going off, and the shopping list only covers the gap.</p>

  <div class="stats">
    <div><b>{len(items)}</b><span>Items</span></div>
    <div><b>{soon}</b><span>Use soon</span></div>
    <div><b>{len(groups['Staples'])}</b><span>Staples</span></div>
  </div>

  <div class="shell">
    <main>{''.join(sections)}</main>
    <aside class="rail">
      <div class="panel">
        <h3>Add an item</h3>
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
          <button class="btn" type="submit">Add to pantry</button>
        </form>
      </div>
      <div class="notice notice-clay" style="margin:0">
        <span class="mark">&rarr;</span><div>
        <strong>Coming next</strong>
        Tell the Instagram agent what you bought and it will fill this in for you.
      </div></div>
    </aside>
  </div>
"""
    return layout("Pantry", body, current="/pantry")
