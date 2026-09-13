"""HTML for the InstaCook pages.

Plain functions returning strings — no template engine, no build step. Every
value that reaches the page goes through `esc()`.

Note `titlecase()`: ingredient names and cuisines are stored lowercase because
normalization needs them that way for matching. Lowercase is a storage
decision, not a display one, so nothing reaches the page uncapitalised.

Mobile is one column ending in a thumb-reach bar; desktop is content plus a
sticky rail, with the bar removed. Two layouts, not one reflowed.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta

import config
from lib.normalize import render_amount
from lib.schemas import advance_prep, attended_minutes

# Words that shouldn't be capitalised mid-phrase.
_MINOR = {"and", "or", "of", "with", "in", "to", "a", "the", "for"}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def titlecase(value: str | None) -> str:
    """'sun-dried tomato' -> 'Sun-dried Tomato'. Storage is lowercase; the page
    is not."""
    if not value:
        return ""
    words = str(value).replace("_", " ").split()
    out = []
    for index, word in enumerate(words):
        if index and word.lower() in _MINOR:
            out.append(word.lower())
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def layout(title: str, body: str, *, current: str = "", bar: str = "") -> str:
    links = []
    for href, label in (("/", "This week"), ("/pantry", "Pantry")):
        current_attr = ' aria-current="page"' if href == current else ""
        links.append(f'<a href="{href}"{current_attr}>{esc(label)}</a>')
    nav = "".join(links)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light">
<title>{esc(title)} — InstaCook</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet"
      href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<header class="appbar"><div class="inner">
  <a class="brand" href="/">Insta<em>Cook</em></a>
  <nav>{nav}</nav>
</div></header>
<div class="wrap">
  {body}

</div>
{bar}
<script src="/static/app.js"></script>
</body>
</html>"""


def _local(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(config.TIMEZONE)


def _tags(recipe: dict) -> list[str]:
    tags = []
    if advance_prep(recipe):
        tags.append('<span class="tag tag-amber">Start ahead</span>')
    if recipe.get("provenance") == "reconstructed":
        tags.append('<span class="tag tag-grey">Reconstructed</span>')
    return tags


def _shop_panel(order: dict | None, *, heading: str = "This week's shop") -> str:
    if not (order and order.get("cart_url")):
        return f"""<div class="panel">
    <h3>{esc(heading)}</h3>
    <p class="tiny muted" style="margin:0 0 14px">No shopping list has been built
    for this week yet.</p>
    <a class="btn btn-ghost" href="/">View the week</a>
  </div>"""

    built = order.get("method") != "fallback_links"
    count = order.get("item_count") or order.get("unresolved_item_count") or 0

    window = ""
    if order.get("delivery_window_start") and order.get("delivery_window_end"):
        begin = _local(order["delivery_window_start"])
        finish = _local(order["delivery_window_end"])
        window = (f'<div class="panel-row"><span>Arriving</span>'
                  f'<b>{begin:%a} {begin:%H:%M}–{finish:%H:%M}</b></div>')

    note = ("" if built else
            '<p class="tiny muted" style="margin:12px 0 0">No cart could be built '
            'automatically, so this opens a search and you add the items yourself.</p>')

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
    rows = []
    for meal in meals:
        recipe = meal["recipe"]
        start = _local(meal["planned_start_time"])
        bits = [f"{attended_minutes(recipe)} min"]
        if recipe.get("servings"):
            bits.append(f"Serves {recipe['servings']}")
        if recipe.get("cuisine"):
            bits.append(esc(titlecase(recipe["cuisine"])))
        tags = _tags(recipe)

        rows.append(f"""
    <a class="meal" href="/cook/{esc(meal['id'])}">
      <span class="when">
        <span class="d">{start:%a}</span>
        <span class="t">{start:%H:%M}</span>
      </span>
      <span class="meal-main">
        <span class="meal-title">{esc(recipe.get('title') or 'Untitled')}</span>
        <span class="meta">{''.join(f'<span>{b}</span>' for b in bits)}</span>
        {f'<span class="meal-tags">{"".join(tags)}</span>' if tags else ''}
      </span>
      <svg class="chev" viewBox="0 0 16 16" aria-hidden="true" width="16" height="16"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </a>""")

    listing = (f'<div class="meals">{"".join(rows)}</div>' if rows else
               '<div class="empty">No meals planned yet. Run the planner and they '
               'will appear here.</div>')

    count = 0
    if order:
        count = order.get("item_count") or order.get("unresolved_item_count") or 0
    ends = week_start + timedelta(days=6)

    body = f"""
  <p class="kicker">{week_start.day} {week_start:%b} – {ends.day} {ends:%b}</p>
  <h1>Your week</h1>
  <div class="stats">
    <div><b>{len(meals)}</b><span>Meals</span></div>
    <div><b>{count}</b><span>To buy</span></div>
    <div class="accent"><b>{pantry_covered}</b><span>Already have</span></div>
  </div>

  <div class="shell">
    <main>
      <div class="section-head"><h2>Scheduled</h2></div>
      {listing}
    </main>
    <aside class="rail">{_shop_panel(order)}</aside>
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
        bits.append(f"Serves {recipe['servings']}")
    if recipe.get("cuisine"):
        bits.append(esc(titlecase(recipe["cuisine"])))

    notices = []
    lead = advance_prep(recipe)
    if lead:
        begin = start - timedelta(minutes=lead)
        notices.append(
            f'<div class="notice">Start {lead // 60}h ahead — begin by '
            f'<b>{begin:%A} at {begin:%H:%M}</b>.</div>')


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
      <span class="label">{esc(titlecase(ing['name']))}</span>
      <span class="amount">{esc(render_amount(row))}{approx}</span>
    </label></li>""")

    steps = "".join(
        f'<li data-key="step:{esc(meal["id"])}:{n}" data-step-index="{n}">'
        f'<span class="step-text">{esc(step)}</span></li>'
        for n, step in enumerate(recipe.get("steps") or [])
    )

    reason = (f'<p class="why">{esc(meal["score_reason"])}</p>'
              if meal.get("score_reason") else "")
    reel = (f'<a href="{esc(recipe["source_url"])}" target="_blank" rel="noopener">'
            f'Watch the original reel</a>' if recipe.get("source_url") else "")

    cook_panel = f"""<div class="panel" id="guide-panel">
    <h3>Cook</h3>
    <div class="panel-rows">
      <div class="panel-row"><span>Scheduled</span><b>{start:%a} {start.day} {start:%b}, {start:%H:%M}</b></div>
      <div class="panel-row"><span>Time needed</span><b>{attended} min</b></div>
    </div>
    <button class="btn" type="button" id="voice" data-guide-start>Start guided cooking</button>
    <div class="guide" id="guide" hidden>
      <div class="guide-meta">
        <span class="guide-count" id="guide-count">Step 1 of 1</span>
        <span class="guide-label" id="guide-label"></span>
      </div>
      <p class="guide-text" id="guide-text"></p>
      <div class="guide-status" id="guide-status" aria-live="polite"></div>
      <audio id="guide-audio" preload="auto"></audio>
      <div class="btn-row">
        <button class="btn btn-ghost" type="button" id="guide-replay">Replay</button>
        <button class="btn" type="button" id="guide-next">Next</button>
      </div>
    </div>
  </div>"""

    body = f"""
  <div class="rhead">
    <p class="kicker">{start:%A} at {start:%H:%M}</p>
    <h1>{esc(recipe.get('title') or 'Untitled')}</h1>
    <div class="meta">{''.join(f'<span>{b}</span>' for b in bits)}</div>
    {f'<div class="rtags">{"".join(_tags(recipe))}</div>' if _tags(recipe) else ''}
  </div>

  <div class="shell">
    <aside class="rail">
      {cook_panel}
      {_shop_panel(order)}
    </aside>
    <main>
      {reason}
      {''.join(notices)}

      <div class="section-head">
        <h2>Ingredients</h2>
        <span class="count" data-progress="ing:{esc(meal['id'])}"></span>
      </div>
      <ul class="rows" data-checklist="ing:{esc(meal['id'])}">{''.join(items)}</ul>

      <div class="section-head">
        <h2>Method</h2>
        <span class="count" data-progress="step:{esc(meal['id'])}"></span>
      </div>
      <ol class="steps" data-steps="step:{esc(meal['id'])}">{steps}</ol>

      <p class="tiny muted" style="margin-top:26px">{reel}</p>
    </main>
  
  </div>
  <div class="bar-spacer"></div>
"""

    cart_href = (order or {}).get("cart_url")
    secondary = (f'<a class="btn btn-ghost" href="{esc(cart_href)}" target="_blank" '
                 f'rel="noopener">Shopping list</a>' if cart_href else
                 '<a class="btn btn-ghost" href="/">Your week</a>')

    bar = f"""
<div class="bar"><div class="inner">
  <button class="btn" type="button" id="voice-mobile" data-guide-start hidden>Start guided cooking</button>
  {secondary}
</div></div>
<script id="cook-context" type="application/json">{json.dumps(voice)}</script>"""

    return layout(recipe.get("title") or "Recipe", body, bar=bar)


# ---------------------------------------------------------------------------
# pantry
# ---------------------------------------------------------------------------

def _expiry_tag(expiry: str | None, today: date) -> str:
    if not expiry:
        return '<span class="tag tag-grey">Staple</span>'
    days = (date.fromisoformat(expiry) - today).days
    if days < 0:
        return '<span class="tag tag-red">Expired</span>'
    if days == 0:
        return '<span class="tag tag-red">Use today</span>'
    if days <= 3:
        return f'<span class="tag tag-red">{days} days left</span>'
    if days <= 7:
        return f'<span class="tag tag-amber">{days} days left</span>'
    return f'<span class="tag tag-green">{days} days</span>'


def pantry_page(items: list[dict], today: date, usable: set[str]) -> str:
    groups: dict[str, list[dict]] = {"Use soon": [], "Later this week": [], "Staples": []}
    for item in items:
        expiry = item.get("expiry_date")
        if not expiry:
            groups["Staples"].append(item)
        else:
            days = (date.fromisoformat(expiry) - today).days
            groups["Use soon" if days <= 3 else "Later this week"].append(item)

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
      <span class="label">{esc(titlecase(item['ingredient_name']))}</span>
      <span class="amount">{esc(amount)}</span>
      {_expiry_tag(item.get('expiry_date'), today)}
      <form method="post" action="/pantry/remove">
        <input type="hidden" name="item_id" value="{esc(item['id'])}">
        <button class="link-remove" type="submit"
                aria-label="Remove {esc(item['ingredient_name'])}">Remove</button>
      </form>
    </div></li>""")
        sections.append(
            f'<div class="section-head"><h2>{esc(name)}</h2>'
            f'<span class="count">{len(rows)}</span></div>'
            f'<ul class="rows">{"".join(lines)}</ul>')

    if not sections:
        sections = ['<div class="empty">Your pantry is empty. Add something, or '
                    'tell the Instagram agent what you have.</div>']

    units = "".join(f'<option value="{u}">{u}</option>'
                    for u in ("unit", "g", "kg", "ml", "l", "cup", "tbsp", "tsp"))

    body = f"""
  <p class="kicker">Your kitchen</p>
  <h1>Pantry</h1>
  <div class="stats">
    <div><b>{len(items)}</b><span>Items</span></div>
    <div class="accent"><b>{len(groups['Use soon'])}</b><span>Use soon</span></div>
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
            <input id="name" name="name" type="text" required placeholder="Spinach"
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

    </aside>
  </div>
"""
    return layout("Pantry", body, current="/pantry")
