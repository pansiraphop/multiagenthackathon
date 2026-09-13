"""HTML for the InstaCook pages.

Plain functions returning strings — no template engine, no build step. Every
value that reaches the page goes through `esc()`.

Note `titlecase()`: ingredient names and cuisines are stored lowercase because
normalization needs them that way for matching. Lowercase is a storage
decision, not a display one, so nothing reaches the page uncapitalised.

Cook pages lead with a centered play/pause button; desktop adds a sticky
shopping-list rail beside ingredients and method.
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
<link rel="icon" href="/static/icon.png" type="image/png">
<link rel="apple-touch-icon" href="/static/icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet"
      href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<header class="appbar"><div class="inner">
  <a class="brand" href="/">
    <img class="brand-mark" src="/static/icon.png" alt="" width="34" height="34">
    <span>Insta<em>Cook</em></span>
  </a>
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


def _instacart_href(order: dict | None, items: list[dict] | None = None) -> str | None:
    """URL the shopping-cart CTA opens. Always Instacart when there's anything to buy."""
    cart_url = (order or {}).get("cart_url")
    if cart_url:
        return cart_url
    if not items:
        return None
    retailer = (config.INSTACART_RETAILER or "").strip()
    if retailer:
        return f"https://www.instacart.com/store/{retailer}/storefront"
    return "https://www.instacart.com/"


def _shop_panel(order: dict | None, items: list[dict] | None = None,
                *, heading: str = "This week's shop") -> str:
    items = items or []
    cart_url = _instacart_href(order, items)

    if not items and not cart_url:
        return f"""<div class="panel shop-panel">
    <h3>{esc(heading)}</h3>
    <p class="tiny muted" style="margin:0">No shopping list for this week yet.
    Schedule a meal and it will appear here.</p>
  </div>"""

    built = bool(order) and order.get("method") in (
        "browser_automation", "browser_ordered", "mixed")
    count = len(items) or (order or {}).get("item_count") or (order or {}).get(
        "unresolved_item_count") or 0

    window = ""
    if order and order.get("delivery_window_start") and order.get("delivery_window_end"):
        begin = _local(order["delivery_window_start"])
        finish = _local(order["delivery_window_end"])
        window = (f'<div class="panel-row"><span>Arriving</span>'
                  f'<b>{begin:%a} {begin:%H:%M}–{finish:%H:%M}</b></div>')

    lines = []
    for row in items[:40]:
        qty = row.get("quantity_needed")
        amount = render_amount({
            "quantity": float(qty) if qty is not None else None,
            "unit": row.get("unit"),
        }, compact=True)
        lines.append(
            f'<li><span class="shop-name">{esc(titlecase(row.get("ingredient_name")))}</span>'
            f'<span class="shop-amt">{esc(amount)}</span></li>')
    listing = (f'<ul class="shop-items">{"".join(lines)}</ul>' if lines else "")

    cta = ""
    if cart_url:
        label = "Open on Instacart" if built else "Shop on Instacart"
        cta = (f'<a class="btn" href="{esc(cart_url)}" target="_blank" rel="noopener">'
               f'{label}</a>')
        if not built:
            cta += ('<p class="tiny muted" style="margin:12px 0 0">Opens Instacart with '
                    'this week\'s missing ingredients.</p>')

    return f"""<div class="panel shop-panel">
    <h3>{esc(heading)}</h3>
    <div class="panel-rows">
      <div class="panel-row"><span>Items to buy</span><b>{count}</b></div>
      {window}
    </div>
    {listing}
    {cta}
  </div>"""


def _cart_bar(order: dict | None, items: list[dict] | None = None) -> str:
    """Thumb-reach Instacart CTA on mobile. Hidden on desktop via CSS."""
    cart_url = _instacart_href(order, items)
    if not cart_url:
        return '<div class="bar-spacer"></div>'
    count = len(items or []) or (order or {}).get("item_count") or (order or {}).get(
        "unresolved_item_count") or 0
    label = f"Instacart · {count} items" if count else "Open Instacart"
    return f"""<div class="bar-spacer"></div>
<div class="bar"><div class="inner">
  <a class="btn" href="{esc(cart_url)}" target="_blank" rel="noopener">{esc(label)}</a>
</div></div>"""


# ---------------------------------------------------------------------------
# this week
# ---------------------------------------------------------------------------

def week_page(week_start: date, meals: list[dict], order: dict | None,
              pantry_covered: int = 0, shop_items: list[dict] | None = None) -> str:
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

    shop_items = shop_items or []
    count = len(shop_items)
    if not count and order:
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
    <aside class="rail">{_shop_panel(order, shop_items)}</aside>
  </div>
"""
    return layout("Your week", body, current="/",
                  bar=_cart_bar(order, shop_items))


# ---------------------------------------------------------------------------
# one recipe
# ---------------------------------------------------------------------------

def cook_page(meal: dict, order: dict | None, voice: dict,
              shop_items: list[dict] | None = None) -> str:
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
      <span class="amount">{esc(render_amount(row, compact=True))}{approx}</span>
    </label></li>""")

    steps = "".join(
        f'<li data-key="step:{esc(meal["id"])}:{n}" data-step-index="{n}">'
        f'<span class="step-text">{esc(step)}</span></li>'
        for n, step in enumerate(recipe.get("steps") or [])
    )

    reel = (f'<a href="{esc(recipe["source_url"])}" target="_blank" rel="noopener">'
            f'Watch the original reel</a>' if recipe.get("source_url") else "")

    body = f"""
  <div class="rhead">
    <p class="kicker">{start:%A} at {start:%H:%M}</p>
    <h1>{esc(recipe.get('title') or 'Untitled')}</h1>
    <div class="meta">{''.join(f'<span>{b}</span>' for b in bits)}</div>
    {f'<div class="rtags">{"".join(_tags(recipe))}</div>' if _tags(recipe) else ''}
  </div>

  <div class="cook-cta">
    <button class="cook-btn" type="button" id="cook-toggle"
            data-guide-start aria-pressed="false">
      <span class="cook-btn-label" id="cook-btn-label">Begin Cooking</span>
    </button>
    <div class="guide" id="guide" hidden>
      <div class="guide-meta">
        <span class="guide-count" id="guide-count">Step 1 of 1</span>
        <span class="guide-label" id="guide-label"></span>
      </div>
      <p class="guide-text" id="guide-text"></p>
      <div class="guide-status" id="guide-status" aria-live="polite"></div>
      <audio id="guide-audio" preload="auto"></audio>
    </div>
  </div>

  <div class="shell">
    <main>
      {''.join(notices)}

      <div class="section-head">
        <h2>Ingredients</h2>
        <span class="count" data-progress="ing:{esc(meal['id'])}"></span>
      </div>
      <ul class="rows" data-checklist="ing:{esc(meal['id'])}">{''.join(items)}</ul>

      <div class="shop-inline">
        {_shop_panel(order, shop_items)}
      </div>

      <div class="section-head">
        <h2>Method</h2>
        <span class="count" data-progress="step:{esc(meal['id'])}"></span>
      </div>
      <ol class="steps" data-steps="step:{esc(meal['id'])}">{steps}</ol>

      <p class="tiny muted" style="margin-top:26px">{reel}</p>
    </main>
  </div>
"""

    bar = f"""{_cart_bar(order, shop_items)}
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
