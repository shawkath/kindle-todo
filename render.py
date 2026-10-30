#!/usr/bin/env python3
"""
Kindle TickTick dashboard renderer: three pages, ADHD-friendly.

    Page 1  Today     Top 3 for today (most important first), the rest of today,
                      anything still open from yesterday, and the rest of this week
    Page 2  Missed    every overdue task, grouped by how long ago, never hidden
    Page 3  Later     next week (by day), later (by month), and tasks with no date

Fetches open tasks from the TickTick Open API, draws three 600x800 grayscale
images for a Kindle (8th gen), and writes a static "TRMNL-compatible" site:

    site/<DASH_PATH>/api/display   -> JSON {image_url, filename, refresh_rate, pages:[...]}
    site/<DASH_PATH>/p1-<hash>.png, p2-<hash>.png, p3-<hash>.png

`image_url` is page 1, so the stock TRMNL plugin still works (one page). The
paging version of the plugin (kindle-plugin/trmnl.koplugin) reads `pages`.

Environment variables
    TICKTICK_TOKEN  TickTick access token (required unless --demo)
    DASH_PATH       secret folder name for the site (required unless --demo)
    BASE_URL        e.g. https://username.github.io/kindle-todo  (set by the workflow)
    TZ_NAME         time zone for "today" (default Asia/Kolkata)
    LISTS           optional comma-separated TickTick list names to show
    WEEK            "calendar" (today .. Sunday, default) or "rolling" (next 7 days)
    REFRESH_SECONDS how often the Kindle should check (default 3600 = hourly)

Image file names are hashes of the pictures, and the pictures have no clock in
them, so the Kindle only downloads and redraws when something actually changes.

Run `python render.py --demo` to render sample data to preview-1/2/3.png.
"""

import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- settings
W, H = 600, 800            # Kindle 8th gen, portrait
M = 28                     # side margin
API = "https://api.ticktick.com/open/v1"
HERE = Path(__file__).resolve().parent
FONT_DIR = HERE / "fonts"

TZ = ZoneInfo(os.environ.get("TZ_NAME") or "Asia/Kolkata")
REFRESH = int(os.environ.get("REFRESH_SECONDS") or 3600)
WEEK_MODE = (os.environ.get("WEEK") or "calendar").lower()
# Night mode: "22-6" = Kindle turns Wi-Fi off from 10pm and wakes at 6am. "off" disables it.
QUIET_HOURS = (os.environ.get("QUIET_HOURS") or "22-6").strip().lower()


def quiet_window(now):
    """If `now` is inside quiet hours, return the datetime they end; else None."""
    if QUIET_HOURS in ("", "off", "none", "0"):
        return None
    try:
        a, b = (int(x) for x in QUIET_HOURS.split("-"))
    except ValueError:
        return None
    h = now.hour
    inside = (a <= h or h < b) if a > b else (a <= h < b)
    if not inside:
        return None
    end = now.replace(hour=b, minute=0, second=0, microsecond=0)
    if end <= now:
        end += dt.timedelta(days=1)
    return end

# e-ink friendly greys (Kindle shows 16 levels; keep text dark)
BLACK, DARK, MID, LIGHT, WHITE = 0, 55, 100, 205, 255
ROW = 54                   # one task row: title line + list/tag line
GROUP_H = 30               # height of a day/month heading inside a section
BATTERY_W = 120            # top-right corner left empty for the Kindle battery overlay


def font(weight, size):
    name = {"regular": "Inter-Regular.otf",
            "semibold": "Inter-SemiBold.otf",
            "bold": "Inter-Bold.otf"}[weight]
    return ImageFont.truetype(str(FONT_DIR / name), size)


F = {
    "head": font("bold", 32),
    "head_sub": font("regular", 18),
    "label": font("bold", 16),
    "top": font("bold", 27),
    "num": font("bold", 20),
    "row": font("regular", 25),
    "row_b": font("bold", 25),
    "row_meta": font("semibold", 19),
    "sub": font("regular", 16),
    "day": font("bold", 16),
    "footer": font("regular", 16),
    "pager": font("regular", 17),
    "pager_on": font("bold", 17),
    "big": font("bold", 34),
    "body": font("regular", 21),
}


# ---------------------------------------------------------------- TickTick
class TokenError(Exception):
    pass


def api_get(path, token):
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "kindle-ticktick-dashboard",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise TokenError(f"HTTP {e.code}") from e
        raise


def fetch_tasks(token, wanted):
    """Return list of (task, list_name) for open tasks."""
    projects = [{"id": "inbox", "name": "Inbox"}]
    projects += [p for p in api_get("/project", token) if not p.get("closed")]
    if wanted:
        keep = {w.strip().lower() for w in wanted.split(",") if w.strip()}
        projects = [p for p in projects if p["name"].lower() in keep]

    out = []
    for p in projects:
        try:
            data = api_get(f"/project/{p['id']}/data", token)
        except TokenError:
            raise
        except Exception as e:  # one bad list should not kill the screen
            print(f"warning: could not read list {p['name']}: {e}", file=sys.stderr)
            continue
        open_tasks = [t for t in (data.get("tasks", []) or []) if t.get("status", 0) == 0]
        if p["id"] == "inbox":
            print(f"inbox: {len(open_tasks)} open tasks")
        for t in open_tasks:                      # status 0 = not completed
            out.append((t, p["name"]))
    return out


# ---------------------------------------------------------------- planning
def parse_ts(raw):
    """Parse TickTick timestamps in any of the shapes it sends:
    2026-10-22T18:30:00+0000, 2026-10-22T18:30:00.000+0000, ...Z, ...+00:00"""
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+0000")
    if "." in s:                                   # drop milliseconds
        head, _, rest = s.partition(".")
        tz = rest.lstrip("0123456789")
        s = head + tz
    if len(s) >= 6 and s[-3] == ":" and s[-6] in "+-":   # +00:00 -> +0000
        s = s[:-3] + s[-2:]
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%d"):
        try:
            when = dt.datetime.strptime(s, fmt)
            return when if when.tzinfo else when.replace(tzinfo=TZ)
        except ValueError:
            continue
    print(f"warning: couldn't read date {raw!r}", file=sys.stderr)
    return None


def due_of(task):
    """Return (date, time_or_None) in local TZ, or None if no date.
    Uses dueDate, falling back to startDate (TickTick sometimes sets only one)."""
    when = parse_ts(task.get("dueDate")) or parse_ts(task.get("startDate"))
    if when is None:
        return None
    if task.get("isAllDay"):
        try:
            tz = ZoneInfo(task.get("timeZone") or str(TZ))
        except Exception:
            tz = TZ
        return when.astimezone(tz).date(), None
    local = when.astimezone(TZ)
    return local.date(), local.time()


def week_end(today):
    if WEEK_MODE == "rolling":
        return today + dt.timedelta(days=6)
    return today + dt.timedelta(days=6 - today.weekday())   # through Sunday


def when(i):
    d, t = i["due"]
    return (d, t or dt.time(23, 59, 59))


# ---------------------------------------------------------------- priority score
# One number per task, used to rank Top 3, Yesterday, Missed and the Energy menu.
#   TickTick priority  High +40 / Medium +20 / Low +10
#   due today          +30
#   days late          up to +25 (rises fast at first, then levels off)
#   someone waiting    +15  (#for-Name tag, or "to/for/with Name" in the title)
#   money / penalty    +15  (bill, pay, renew, insurance, tax, reimburse, ...)
#   quick win          +5   (#5m..#15m tag, or a guessed no-brainer)
#   overdue recurring  -20  (the next one comes round anyway)
import math as _math
import re as _re0

PRIORITY_POINTS = {5: 40, 3: 20, 1: 10}
MONEY_WORDS = {"bill", "bills", "pay", "payment", "renew", "renewal", "insurance", "tax", "taxes",
               "reimburse", "reimbursement", "emi", "fee", "fees", "fine", "invoice", "salary",
               "rent", "premium", "dues", "refund"}
PEOPLE_STOP = {"the", "a", "an", "my", "me", "home", "work", "office", "it", "this", "that",
               "today", "tomorrow", "next", "all", "everyone", "team"}


def someone_waiting(item):
    if any(t.lower().startswith("for-") for t in item["tags"]):
        return True
    for m in _re0.finditer(r"\b(?:to|for|with)\s+([A-Za-z][\w']+)", item["title"]):
        w = m.group(1)
        if w.lower() not in PEOPLE_STOP and (w[0].isupper() or w.lower().endswith(("bridge", "corp", "ltd"))):
            return True
    return False


def is_quick(item):
    mins = None
    for t in item["tags"]:
        mm = _re0.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", t.lower())
        if mm and (mm.group(1) or mm.group(2)):
            mins = int(mm.group(1) or 0) * 60 + int(mm.group(2) or 0)
    if mins is not None:
        return mins <= 15
    words = set(_re0.findall(r"[a-z]+", item["title"].lower()))
    return bool(words & {"bill", "pay", "call", "book", "renew", "recharge", "reply", "text", "whatsapp"})


def score(item, today):
    s = PRIORITY_POINTS.get(item["priority"], 0)
    due = item["due"]
    late = (today - due[0]).days if due else 0
    if due and late == 0:
        s += 30
    if late > 0:
        s += min(25, round(8 * _math.log2(1 + late)))
    if someone_waiting(item):
        s += 15
    if set(_re0.findall(r"[a-z]+", item["title"].lower())) & MONEY_WORDS:
        s += 15
    if is_quick(item):
        s += 5
    if late > 0 and item.get("repeat"):
        s -= 20
    return s


def plan(tasks, now):
    today = now.date()
    yday = today - dt.timedelta(days=1)
    end = week_end(today)
    next_end = end + dt.timedelta(days=7)

    items = [{"title": " ".join((t.get("title") or "(untitled)").split()),
              "list": clean(list_name),
              "tags": [clean(x) for x in (t.get("tags") or []) if clean(x)],
              "priority": t.get("priority", 0) or 0,
              "repeat": bool(t.get("repeatFlag")),
              "due": due_of(t)} for t, list_name in tasks]
    for i in items:
        i["score"] = score(i, today)
    dated = [i for i in items if i["due"]]

    today_all = [i for i in dated if i["due"][0] == today]
    yesterday = [i for i in dated if i["due"][0] == yday]
    missed = [i for i in dated if i["due"][0] < today]
    this_week = [i for i in dated if today < i["due"][0] <= end]
    next_week = [i for i in dated if end < i["due"][0] <= next_end]
    later = [i for i in dated if i["due"][0] > next_end]
    undated = [i for i in items if not i["due"]]

    # top 3: most important today, then earliest
    by_importance = sorted(today_all, key=lambda i: (-i["score"], when(i)[1]))
    top3 = by_importance[:3]
    also_today = sorted(by_importance[3:], key=lambda i: when(i)[1])

    yesterday.sort(key=lambda i: (-i["score"], when(i)[1]))
    this_week.sort(key=lambda i: when(i))
    next_week.sort(key=lambda i: when(i))
    later.sort(key=lambda i: when(i))
    missed.sort(key=lambda i: (-i["score"], when(i)))           # highest score, then oldest
    undated.sort(key=lambda i: (-i["priority"], i["title"].lower()))

    return {"now": now, "today": today, "end": end, "today_all": today_all,
            "top3": top3, "also_today": also_today, "n_today": len(today_all),
            "yesterday": yesterday, "this_week": this_week,
            "missed": missed, "next_week": next_week, "later": later, "undated": undated}


# ---------------------------------------------------------------- drawing helpers
def tw(d, s, f):
    return d.textlength(s, font=f)


def fit(d, s, f, width):
    """Single line, truncated with an ellipsis."""
    if tw(d, s, f) <= width:
        return s
    while s and tw(d, s + "…", f) > width:
        s = s[:-1]
    return s.rstrip() + "…"


def wrap(d, s, f, width, max_lines):
    words, lines, cur = s.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if tw(d, trial, f) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = fit(d, lines[-1] + " …", f, width)
    return [fit(d, l, f, width) for l in lines]


def clean(s):
    """Drop emoji and other symbols the e-ink font can't draw (they'd show as boxes)."""
    import unicodedata
    out = "".join(ch for ch in str(s or "")
                  if ord(ch) <= 0xFFFF and unicodedata.category(ch) not in ("So", "Cs", "Co", "Mn")
                  and ch not in "\u200d\ufe0f")
    return " ".join(out.split())


def subline(item):
    """'Home Maintenance  #urgent #car' shown under each title."""
    parts = [item["list"]] if item["list"] else []
    parts += [f"#{t}" for t in item.get("tags", [])]
    return "   ".join(parts)


def late_label(days):
    """3d, 2w, 4mo, 1y: short enough for a narrow column."""
    if days < 14:
        return f"{days}d"
    if days < 60:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def clock(t):
    return t.strftime("%I:%M%p").lstrip("0").lower()   # 3:00pm


def day_name(day, today):
    if day == today:
        return "TODAY"
    if day == today + dt.timedelta(days=1):
        return "TOMORROW"
    return day.strftime("%a %d %b").upper().replace(" 0", " ")


# right-hand column text for each kind of list
def right_today(i, now):
    t = i["due"][1]
    if not t:
        return ""
    return f"was {clock(t)}" if t < now.time() else clock(t)


def right_time(i, now):
    t = i["due"][1]
    return clock(t) if t else ""


def right_late(i, now):
    late = (now.date() - i["due"][0]).days
    return f"{late_label(late)} late"


def right_date(i, now):
    d, t = i["due"]
    return d.strftime("%d %b").lstrip("0") + (f" {clock(t)}" if t else "")


def right_list(i, now):
    return i["list"]


# ---------------------------------------------------------------- building blocks
def label(d, y, text, inverted=False):
    """Small uppercase section label. Inverted = white on black bar."""
    if inverted:
        wpx = tw(d, text, F["label"]) + 16
        d.rectangle((M, y, M + wpx, y + 24), fill=BLACK)
        d.text((M + 8, y + 3), text, font=F["label"], fill=WHITE)
    else:
        d.text((M, y + 3), text, font=F["label"], fill=DARK)
    return y + 32


def row(d, y, item, right_text, right_bold=False):
    """Task row: checkbox + title (big), list/tags underneath (small), time on the right."""
    box = 20
    d.rectangle((M, y + 8, M + box, y + 8 + box), outline=BLACK, width=2)
    rf = F["row_meta"]
    rw = tw(d, right_text, rf)
    x = M + box + 14
    title_f = F["row_b"] if item["priority"] >= 5 else F["row"]
    d.text((x, y + 2), fit(d, item["title"], title_f, W - M - x - rw - 14),
           font=title_f, fill=BLACK)
    if right_text:
        d.text((W - M - rw, y + 7), right_text, font=rf, fill=BLACK if right_bold else DARK)
    sub = subline(item)
    if sub:
        d.text((x, y + 31), fit(d, sub, F["sub"], W - M - x), font=F["sub"], fill=MID)
    return y + ROW


TOP_ROW = 62


def top_row(d, y, n, item, right_text):
    """Big numbered row for the Top 3."""
    sz = 30
    d.rectangle((M, y + 6, M + sz, y + 6 + sz), fill=BLACK)
    num = str(n)
    d.text((M + (sz - tw(d, num, F["num"])) / 2, y + 9), num, font=F["num"], fill=WHITE)
    rf = F["row_meta"]
    rw = tw(d, right_text, rf)
    x = M + sz + 14
    d.text((x, y + 3), fit(d, item["title"], F["top"], W - M - x - rw - 14), font=F["top"], fill=BLACK)
    if right_text:
        d.text((W - M - rw, y + 10), right_text, font=rf, fill=DARK)
    sub = subline(item)
    if sub:
        d.text((x, y + 35), fit(d, sub, F["sub"], W - M - x), font=F["sub"], fill=MID)
    return y + TOP_ROW


def more_line(d, y, text):
    d.text((M + 34, y + 4), text, font=F["row_meta"], fill=DARK)
    return y + 32


def section(d, y, title, items, now, right_fn, rows, inverted=False, right_bold=False,
            group_fn=None, more_text="+ {n} more"):
    """Draw a labelled list with at most `rows` task rows (group headers don't count)."""
    y = label(d, y, f"{title}  {len(items)}" if items else title, inverted)
    shown, group = 0, object()
    for i in items[:rows]:
        if group_fn:
            g = group_fn(i)
            if g != group:
                group = g
                d.text((M, y + 5), g, font=F["day"], fill=DARK)
                d.line((M + tw(d, g, F["day"]) + 10, y + 15, W - M, y + 15), fill=LIGHT, width=1)
                y += GROUP_H
        y = row(d, y, i, right_fn(i, now), right_bold)
        shown += 1
    if len(items) > shown:
        y = more_line(d, y, more_text.format(n=len(items) - shown))
    return y + 10


def section_height(items, rows, group_fn=None):
    """Height a section() call will use."""
    shown = items[:rows]
    h = 32 + len(shown) * ROW + 10
    if group_fn:
        h += len({group_fn(i) for i in shown}) * GROUP_H
    if len(items) > len(shown):
        h += 32
    return h


def fit_rows(items, space, group_fn=None, min_rows=0):
    """Largest row count whose section fits in `space`."""
    best = min(min_rows, len(items))
    for n in range(len(items) + 1):
        if section_height(items, n, group_fn) <= space:
            best = n
    return best


def share_rows(groups, space):
    """Split `space` between several sections fairly: everyone gets a few rows,
    then leftovers go to whoever still has tasks, top to bottom.
    groups = [(items, group_fn), ...]  ->  [rows, ...]"""
    rows = [0] * len(groups)
    active = [k for k, (items, _) in enumerate(groups) if items]

    def total():
        return sum(section_height(groups[k][0], rows[k], groups[k][1]) for k in active)

    changed = True
    while changed:
        changed = False
        for k in active:
            if rows[k] < len(groups[k][0]):
                rows[k] += 1
                if total() > space:
                    rows[k] -= 1
                else:
                    changed = True
    return rows


PAGES = ["Today", "Missed", "Later", "Energy", "Rescue"]


def draw_header(d, now, title, sub=None):
    """Title on the left; counts next to it; top-right corner left empty for
    the battery the Kindle draws on top."""
    y = 16
    d.text((M, y), title, font=F["head"], fill=BLACK)
    if sub:
        right = W - M - BATTERY_W
        sw = tw(d, sub, F["head_sub"])
        x = max(M + tw(d, title, F["head"]) + 18, right - sw)
        d.text((x, y + 12), fit(d, sub, F["head_sub"], right - x), font=F["head_sub"], fill=DARK)
    y += 48
    d.rectangle((M, y, W - M, y + 2), fill=BLACK)
    return y + 14


SYNCED_AT = None   # set in main(): when GitHub last read TickTick


def draw_pager(d, page, counts):
    """Footer: which page you're on, and what's on the others."""
    d.line((M, H - 44, W - M, H - 44), fill=LIGHT, width=1)
    x = M
    for k, name in enumerate(PAGES):
        on = k == page
        txt = name + (f" {counts[k]}" if counts[k] else "")
        r = 4
        cy = H - 25
        if on:
            d.ellipse((x, cy - r, x + 2 * r, cy + r), fill=BLACK)
        else:
            d.ellipse((x, cy - r, x + 2 * r, cy + r), outline=MID, width=2)
        x += 2 * r + 6
        f = F["pager_on"] if on else F["pager"]
        d.text((x, cy - 10), txt, font=f, fill=BLACK if on else MID)
        x += tw(d, txt, f) + 13
    if SYNCED_AT:
        note = f"synced {clock(SYNCED_AT.time())}"
        d.text((W - M - tw(d, note, F["footer"]), H - 34), note, font=F["footer"], fill=MID)


BOTTOM = H - 56   # last usable pixel above the pager


# ---------------------------------------------------------------- page 1: Today
def page_today(p):
    now, today = p["now"], p["today"]
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    n_week = len(p["this_week"])
    y = draw_header(d, now, now.strftime("%a %d %b").replace(" 0", " "),
                    f"{p['n_today']} today · {n_week} this week")

    # Top 3
    y = label(d, y, "TOP 3 TODAY", inverted=True)
    if p["top3"]:
        for n, i in enumerate(p["top3"], 1):
            y = top_row(d, y, n, i, right_today(i, now))
    else:
        d.text((M, y + 4), "Nothing due today.", font=F["row"], fill=MID)
        y += ROW
    y += 12

    by_day = lambda i: day_name(i["due"][0], today)
    groups = [(p["also_today"], None), (p["yesterday"], None), (p["this_week"], by_day)]
    rows = share_rows(groups, BOTTOM - y)

    if p["also_today"]:
        y = section(d, y, "ALSO TODAY", p["also_today"], now, right_today, rows[0])
    if p["yesterday"]:
        y = section(d, y, "YESTERDAY", p["yesterday"], now, right_time, rows[1],
                    right_bold=False, more_text="+ {n} more from yesterday")
    if p["this_week"]:
        y = section(d, y, "REST OF THIS WEEK", p["this_week"], now, right_time, rows[2],
                    group_fn=by_day, more_text="+ {n} more this week")
    elif not (p["top3"] or p["also_today"] or p["yesterday"]):
        d.text((M, y + 20), "All clear for this week.", font=F["big"], fill=BLACK)

    draw_pager(d, 0, pager_counts(p))
    return img


# ---------------------------------------------------------------- page 2: Missed
def page_missed(p):
    now, today = p["now"], p["today"]
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    missed = p["missed"]
    y = draw_header(d, now, "Missed", f"{len(missed)} overdue")

    if not missed:
        d.text((M, y + 160), "Nothing missed.", font=F["big"], fill=BLACK)
        d.text((M, y + 210), "You're fully caught up.", font=F["body"], fill=DARK)
    else:
        def age(i):
            days = (today - i["due"][0]).days
            if days <= 1:
                return 0
            if days <= 7:
                return 1
            if days <= 31:
                return 2
            if days <= 92:
                return 3
            return 4
        names = ["YESTERDAY", "THIS PAST WEEK", "THIS PAST MONTH", "1–3 MONTHS AGO", "OLDER THAN 3 MONTHS"]
        buckets = [[i for i in missed if age(i) == k] for k in range(5)]
        groups = [(b, None) for b in buckets]
        rows = share_rows(groups, BOTTOM - y)
        for k, b in enumerate(buckets):
            if b:
                y = section(d, y, names[k], b, now, right_late, rows[k], right_bold=True,
                            more_text="+ {n} more — open TickTick")

    draw_pager(d, 1, pager_counts(p))
    return img


# ---------------------------------------------------------------- page 3: Later
def page_later(p):
    now, today = p["now"], p["today"]
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    nxt, later, undated = p["next_week"], p["later"], p["undated"]
    nw_start = p["end"] + dt.timedelta(days=1)
    title = "Next week & later"
    y = draw_header(d, now, title, f"from {nw_start.strftime('%d %b').lstrip('0')}")

    by_day = lambda i: day_name(i["due"][0], today)
    by_month = lambda i: i["due"][0].strftime("%B %Y").upper()
    groups = [(nxt, by_day), (later, by_month), (undated, None)]
    rows = share_rows(groups, BOTTOM - y)

    if not (nxt or later or undated):
        d.text((M, y + 160), "Nothing planned yet.", font=F["big"], fill=BLACK)
    if nxt:
        y = section(d, y, "NEXT WEEK", nxt, now, right_time, rows[0], group_fn=by_day,
                    more_text="+ {n} more next week")
    if later:
        y = section(d, y, "LATER", later, now, right_date, rows[1], group_fn=by_month)
    if undated:
        y = section(d, y, "NO DATE", undated, now, lambda i, n: "", rows[2],
                    more_text="+ {n} more without a date")

    draw_pager(d, 2, pager_counts(p))
    return img


# ---------------------------------------------------------------- page 4: Energy menu
import re as _re

ENERGY_TAGS = {
    "sharp": "sharp", "deep": "sharp", "high": "sharp", "focus": "sharp",
    "okay": "okay", "ok": "okay", "medium": "okay", "admin": "okay",
    "drained": "drained", "low": "drained", "easy": "drained", "quick": "drained",
}
# best guesses when a task has no energy tag (whole words, case-insensitive)
DRAINED_WORDS = ("bill", "pay", "call", "book", "renew", "recharge", "order", "buy", "reply",
                 "haircut", "clean", "cleaning", "water", "remind", "text", "whatsapp", "email back")
SHARP_WORDS = ("proposal", "design", "write", "plan", "strategy", "migration", "research", "review",
               "build", "create", "draft", "prepare", "presentation", "wireframe", "spec", "seo", "audit")
DEFAULT_MINUTES = {"sharp": 60, "okay": 20, "drained": 5}


def energy_of(item):
    for t in item["tags"]:
        e = ENERGY_TAGS.get(t.lower())
        if e:
            return e
    words = set(_re.findall(r"[a-z]+", item["title"].lower()))
    if words & set(SHARP_WORDS):
        return "sharp"
    if words & set(DRAINED_WORDS):
        return "drained"
    return "okay"


def minutes_of(item):
    """From a #5m / #30m / #1h / #1h30m tag; None if not tagged."""
    for t in item["tags"]:
        m = _re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", t.lower())
        if m and (m.group(1) or m.group(2)):
            return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return None


def fmt_minutes(n, guessed):
    s = f"{n // 60}h{n % 60:02d}" if n >= 60 and n % 60 else (f"{n // 60}h" if n >= 60 else f"{n}m")
    return ("~" + s) if guessed else s


def suggested_energy(now):
    h = now.hour
    if 6 <= h < 12:
        return "sharp", f"{now:%A} morning · usually your sharpest hours"
    if 12 <= h < 17:
        return "okay", f"{now:%A} afternoon · good for admin and errands"
    if 17 <= h < 21:
        return "drained", f"{now:%A} evening · keep it light"
    return "drained", "Late · only tiny things, then rest"


def page_energy(p):
    now, today = p["now"], p["today"]
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    y = draw_header(d, now, "Energy menu", "pick by how you feel")
    best, line = suggested_energy(now)
    d.text((M, y), line, font=F["sub"], fill=DARK)
    y += 28

    # what's actionable now: today, missed, and the rest of this week
    pool = p["today_all"] + p["missed"] + p["this_week"]

    def rank(i):
        d0 = i["due"][0]
        return (-i["score"], d0)

    courses = {"sharp": [], "okay": [], "drained": []}
    for i in sorted(pool, key=rank):
        courses[energy_of(i)].append(i)

    meta = {"sharp": ("Sharp", 3, "deep work"), "okay": ("Okay", 2, "admin & errands"),
            "drained": ("Drained", 1, "no-brainers")}
    order = ["sharp", "okay", "drained"]
    ROW_E = 40
    HEAD_E = 46

    def course_h(n, total):
        return HEAD_E + n * ROW_E + (30 if total > n else 0) + 8

    # rows per course: the suggested one gets first pick of the space
    space = BOTTOM - y - 3 * 12 - 26
    rows = {k: 0 for k in order}
    for k in [best] + [k for k in order if k != best]:
        rows[k] = min(len(courses[k]), 1)
    changed = True
    while changed:
        changed = False
        for k in [best, best] + [k for k in order if k != best]:
            if rows[k] < len(courses[k]):
                rows[k] += 1
                if sum(course_h(rows[c], len(courses[c])) for c in order) > space:
                    rows[k] -= 1
                else:
                    changed = True

    for k in order:
        items = courses[k]
        n = rows[k]
        name, bars, desc = meta[k]
        hl = k == best
        h = course_h(n, len(items))
        if hl:
            d.rectangle((M, y, W - M, y + h), fill=BLACK)
            fg, sub = WHITE, LIGHT
        else:
            d.rectangle((M, y, W - M, y + h), outline=BLACK, width=2)
            fg, sub = BLACK, DARK
        for b in range(3):
            x = M + 14 + b * 16
            d.rectangle((x, y + 14, x + 11, y + 32), fill=fg if b < bars else (MID if hl else LIGHT))
        d.text((M + 70, y + 9), name, font=F["row_b"], fill=fg)
        d.text((M + 70 + tw(d, name, F["row_b"]) + 12, y + 15),
               f"{desc} · {len(items)}" + ("  ← now" if hl else ""), font=F["sub"], fill=sub)
        ty = y + HEAD_E
        if not items:
            d.text((M + 18, ty - 4), "nothing here right now", font=F["sub"], fill=sub)
        for i in items[:n]:
            mins = minutes_of(i)
            right = fmt_minutes(mins if mins else DEFAULT_MINUTES[k], mins is None)
            late = (today - i["due"][0]).days
            if late > 0:
                right = f"{late_label(late)} late · " + right
            elif late == 0:
                right = "today · " + right
            rw = tw(d, right, F["row_meta"])
            tf = F["row_b"] if i["priority"] >= 5 else F["row"]
            d.text((M + 18, ty), fit(d, "• " + i["title"], tf, W - 2 * M - rw - 40), font=tf, fill=fg)
            d.text((W - M - 14 - rw, ty + 5), right, font=F["row_meta"], fill=sub)
            ty += ROW_E
        if len(items) > n:
            d.text((M + 18, ty), f"+ {len(items) - n} more", font=F["row_meta"], fill=sub)
        y += h + 12

    d.text((M, min(y, BOTTOM - 22)), "Tag #sharp #okay #drained and #15m to sort these better.",
           font=F["sub"], fill=MID)
    draw_pager(d, 3, pager_counts(p))
    return img


# ---------------------------------------------------------------- page 5: Rescue mode
HISTORY_FILE = HERE / "history" / "overdue.json"


def update_history(p):
    """Remember today's overdue count (last value of the day wins). Kept 60 days.
    The workflow restores/saves this file with actions/cache, so nothing is committed."""
    hist = {}
    try:
        hist = json.loads(HISTORY_FILE.read_text())
    except Exception:
        pass
    hist[p["today"].isoformat()] = len(p["missed"])
    keep = sorted(hist)[-60:]
    hist = {k: hist[k] for k in keep}
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(hist))
    except Exception as e:
        print("warning: couldn't save history:", e, file=sys.stderr)
    return hist


def streak_of(hist, today):
    """Days in a row (ending today) where the pile didn't grow."""
    n, day = 0, today
    while True:
        prev = day - dt.timedelta(days=1)
        a, b = hist.get(day.isoformat()), hist.get(prev.isoformat())
        if a is None or b is None or a > b:
            return n
        n += 1
        day = prev


def triage_for(i, today):
    """A decision, not an action. Checked in this order."""
    t = i["title"].lower()
    age = (today - i["due"][0]).days
    words = set(_re.findall(r"[a-z]+", t))
    if words & MONEY_WORDS:
        return "PAY IT", "money: settle it, then it's gone"
    if someone_waiting(i):
        return "MESSAGE", "a 2-line message unblocks it"
    if words & {"checkup", "appointment", "registration", "renewal", "visit", "meet", "meeting",
                "doctor", "dentist", "haircut", "service", "certificate"}:
        return "SCHEDULE", "pick a day for it"
    if words & {"cleaning", "clean", "wash", "fix", "repair", "laundry"}:
        return "DELEGATE", "who else can do it?"
    if age > 180 and i["priority"] < 3:
        return "DROP?", "still needed?"
    return "DO IT", "under 15 min? just do it"


def page_rescue(p, hist):
    now, today = p["now"], p["today"]
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    missed = p["missed"]
    y = draw_header(d, now, "Rescue mode", "shrink the pile")
    HUGE = font("bold", 56)
    MEDB = font("bold", 24)
    MEDR = font("regular", 21)

    if not missed:
        d.text((M, y + 150), "Pile cleared.", font=F["big"], fill=BLACK)
        d.text((M, y + 200), "Nothing overdue. Enjoy it.", font=F["body"], fill=DARK)
        draw_pager(d, 4, pager_counts(p))
        return img

    # big number + trend vs a week ago
    n = len(missed)
    d.text((M, y - 4), str(n), font=HUGE, fill=BLACK)
    x = M + tw(d, str(n), HUGE) + 14
    d.text((x, y + 2), "overdue tasks", font=MEDB, fill=BLACK)
    week_ago = hist.get((today - dt.timedelta(days=7)).isoformat())
    if week_ago is None:
        older = [k for k in sorted(hist) if k < today.isoformat()]
        week_ago = hist[older[0]] if older else None
        since = dt.date.fromisoformat(older[0]).strftime("%a") if older else None
    else:
        since = "last " + (today - dt.timedelta(days=7)).strftime("%A")
    if week_ago is None:
        trend = "tracking starts today"
    elif week_ago > n:
        trend = f"down {week_ago - n} since {since}"
    elif week_ago < n:
        trend = f"up {n - week_ago} since {since}"
    else:
        trend = f"same as {since}"
    d.text((x, y + 34), trend, font=MEDR, fill=DARK)
    y += 76

    # last 7 days bars + streak
    days = [today - dt.timedelta(days=k) for k in range(6, -1, -1)]
    vals = [hist.get(dd.isoformat()) for dd in days]
    top = max([v for v in vals if v is not None] + [1])
    bw = 30
    for k, (dd, v) in enumerate(zip(days, vals)):
        bx = M + k * (bw + 10)
        if v is not None:
            h = max(3, int(52 * v / top))
            d.rectangle((bx, y + 54 - h, bx + bw, y + 54), fill=BLACK if k == 6 else LIGHT)
        else:
            d.line((bx, y + 54, bx + bw, y + 54), fill=LIGHT, width=2)
        d.text((bx + 9, y + 58), dd.strftime("%a")[0], font=F["label"], fill=MID)
    st = streak_of(hist, today)
    sx = M + 7 * (bw + 10) + 10
    d.text((sx, y + 8), f"{st}-day streak" if st else "start a streak", font=MEDB, fill=BLACK)
    d.text((sx, y + 38), "pile not growing" if st else "clear one today", font=F["sub"], fill=DARK)
    y += 92

    # where it's piling up
    y = label(d, y, "WHERE IT'S PILING UP")
    by_list = {}
    for i in missed:
        by_list.setdefault(i["list"] or "No list", []).append(i)
    areas = sorted(by_list.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:5]
    most = max(len(v) for _, v in areas)
    name_w, bar_max = 200, W - 2 * M - 200 - 120
    for name, items in areas:
        oldest = max((today - i["due"][0]).days for i in items)
        d.text((M, y), fit(d, name, MEDR, name_w - 10), font=MEDR, fill=BLACK)
        bl = max(8, int(bar_max * len(items) / most))
        d.rectangle((M + name_w, y + 6, M + name_w + bl, y + 24), fill=BLACK)
        d.text((M + name_w + bl + 10, y + 3), f"{len(items)} · {late_label(oldest)}",
               font=F["row_meta"], fill=DARK)
        y += 34
    y += 10

    # 3-minute triage: oldest first, spread across lists
    y = label(d, y, "3-MINUTE TRIAGE: DECIDE, DON'T DO", inverted=True)
    pool = sorted(missed, key=lambda i: i["due"][0])
    picks, seen = [], set()
    for i in pool:
        if i["list"] not in seen:
            picks.append(i)
            seen.add(i["list"])
        if len(picks) == 3:
            break
    for i in pool:
        if len(picks) == 3:
            break
        if i not in picks:
            picks.append(i)
    for i in picks:
        if y + 58 > BOTTOM:
            break
        verdict, hint = triage_for(i, today)
        vw = tw(d, verdict, F["label"]) + 18
        d.text((M, y), fit(d, i["title"], MEDB, W - 2 * M - vw - 12), font=MEDB, fill=BLACK)
        late = (today - i["due"][0]).days
        d.text((M, y + 31), fit(d, f"{late_label(late)} late · {i['list']}   → {hint}", F["sub"], W - 2 * M),
               font=F["sub"], fill=DARK)
        d.rectangle((W - M - vw, y + 4, W - M, y + 30), outline=BLACK, width=2)
        d.text((W - M - vw + 9, y + 8), verdict, font=F["label"], fill=BLACK)
        y += 62

    draw_pager(d, 4, pager_counts(p))
    return img


def pager_counts(p):
    return [None, len(p["missed"]), None, None, None]


def page_night(p, wake):
    """Shown as page 1 during quiet hours: tomorrow's plan, then the Kindle rests."""
    now, today = p["now"], p["today"]
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    tomorrow = today + dt.timedelta(days=1) if now.hour >= 12 else today
    y = draw_header(d, now, "Good night", "screen rests till " + clock(wake.time()))
    d.text((M, y + 10), "Rest. Tomorrow is planned.", font=F["big"], fill=BLACK)
    y += 70

    due_next = [i for i in (p["this_week"] + p["today_all"] + p["next_week"]) if i["due"][0] == tomorrow]
    due_next.sort(key=lambda i: (-i["score"], when(i)[1]))
    label_txt = "FIRST THING " + ("TOMORROW" if tomorrow != today else "THIS MORNING")
    y = label(d, y, label_txt, inverted=True)
    if due_next:
        for n, i in enumerate(due_next[:3], 1):
            t = i["due"][1]
            y = top_row(d, y, n, i, clock(t) if t else "")
        if len(due_next) > 3:
            y = more_line(d, y, f"+ {len(due_next) - 3} more tomorrow")
    else:
        d.text((M, y + 4), "Nothing scheduled. Pick one from Missed in the morning.", font=F["sub"], fill=DARK)
        y += 40
    y += 14
    if p["missed"]:
        top = p["missed"][0]
        y = label(d, y, f"STILL WAITING  {len(p['missed'])} MISSED")
        d.text((M, y), fit(d, "Most important: " + top["title"], F["row"], W - 2 * M), font=F["row"], fill=BLACK)
        y += 40
    d.text((M, BOTTOM - 30), f"Wi-Fi is off to save battery. Back at {clock(wake.time())}.",
           font=F["sub"], fill=MID)
    draw_pager(d, 0, pager_counts(p))
    return img


def render_pages(p, hist=None):
    if hist is None:
        hist = {p["today"].isoformat(): len(p["missed"])}
    return [page_today(p), page_missed(p), page_later(p), page_energy(p), page_rescue(p, hist)]


def render_message(now, title, body_lines):
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    y = draw_header(d, now, now.strftime("%A %d %b").replace(" 0", " "))
    d.text((M, y + 120), title, font=F["big"], fill=BLACK)
    yy = y + 175
    for ln in body_lines:
        for sub in wrap(d, ln, F["body"], W - 2 * M, 4):
            d.text((M, yy), sub, font=F["body"], fill=DARK)
            yy += 28
        yy += 8
    return img


# ---------------------------------------------------------------- output
def write_site(images, base_url, dash_path, sleep_until=None):
    """Write one PNG per page plus api/display.

    api/display keeps the original single-screen fields (image_url/filename =
    page 1) so the stock TRMNL plugin still works; the paging plugin reads `pages`."""
    root = HERE / "site"
    if root.exists():
        shutil.rmtree(root)
    out = root / dash_path
    (out / "api").mkdir(parents=True)
    (root / ".nojekyll").write_text("")
    (root / "index.html").write_text("<!doctype html><title>.</title>")

    pages = []
    for k, img in enumerate(images, 1):
        tmp = out / f"tmp{k}.png"
        img.save(tmp, optimize=True)
        digest = hashlib.sha1(tmp.read_bytes()).hexdigest()[:12]
        name = f"p{k}-{digest}.png"
        tmp.rename(out / name)
        pages.append({"image_url": f"{base_url.rstrip('/')}/{dash_path}/{name}", "filename": name})
        print("wrote", out / name)

    payload = {
        "image_url": pages[0]["image_url"],
        "filename": "-".join(pg["filename"][3:15] for pg in pages) + ".png",
        "refresh_rate": REFRESH,
        "sleep_until": sleep_until,
        "pages": pages,
    }
    (out / "api" / "display").write_text(json.dumps(payload))


def demo_tasks():
    today = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    def at(days, h=None, m=0):
        t = today + dt.timedelta(days=days, hours=h or 0, minutes=m)
        return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")

    allday = {"isAllDay": True, "timeZone": "Asia/Kolkata"}
    old = [("Book haircut", -380), ("Set up smart-home reminders", -335), ("Submit certificate application", -330),
           ("Call family friend", -331), ("Register documents at office", -331, 5), ("Monthly budgeting", -325, 3),
           ("Migrate email to new workspace", -324), ("Pay electricity bill", -313, 5), ("Eye check-up", -308),
           ("Send posters to designer", -308), ("Renew vehicle insurance", -40, 3), ("Return library books", -9),
           ("Reply to accountant", -1), ("Water the plants", -1)]
    out = [({"title": t[0], "dueDate": at(t[1]), **allday, "priority": (t[2] if len(t) > 2 else 0)}, "🏠 Home Maintenance") for t in old]
    out += [
        ({"title": "Transfer domains to new registrar", "dueDate": at(0, 9, 30), "priority": 3, "tags": ["TD"]}, "Inbox"),
        ({"title": "Book domain and connect repo", "dueDate": at(0, 15)}, "Inbox"),
        ({"title": "Review pull request from the team", "dueDate": at(0, 18), "priority": 5, "tags": ["WS", "urgent"]}, "Work"),
        ({"title": "Pay maid salary", "dueDate": at(0), **allday}, "Home"),
        ({"title": "Gym", "dueDate": at(0, 19, 30)}, "Personal"),
        ({"title": "Call plumber", "dueDate": at(1, 11)}, "Home"),
        ({"title": "Draft sale page copy", "dueDate": at(1, 17), "priority": 3}, "Side projects"),
        ({"title": "Weekly groceries", "dueDate": at(2), **allday}, "Home"),
        ({"title": "Plan next week", "dueDate": at(3, 19), "priority": 3}, "Inbox"),
        ({"title": "Dentist appointment", "dueDate": at(9, 10)}, "Personal"),
        ({"title": "Buy printer ink"}, "Inbox"),
    ]
    return out


def main():
    global SYNCED_AT
    now = dt.datetime.now(TZ)
    if "--now" in sys.argv:   # e.g. --now 2026-10-22T16:00 for previews
        now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]).replace(tzinfo=TZ)
    SYNCED_AT = now

    if "--demo" in sys.argv:
        for k, img in enumerate(render_pages(plan(demo_tasks(), now)), 1):
            img.save(HERE / f"preview-{k}.png")
            print("wrote", HERE / f"preview-{k}.png")
        return

    token = os.environ.get("TICKTICK_TOKEN", "").strip()
    dash_path = os.environ.get("DASH_PATH", "").strip().strip("/")
    base_url = os.environ.get("BASE_URL", "").strip()
    if not (token and dash_path and base_url):
        sys.exit("Missing TICKTICK_TOKEN, DASH_PATH or BASE_URL")

    try:
        tasks = fetch_tasks(token, os.environ.get("LISTS", ""))
        dated = sum(1 for t, _ in tasks if due_of(t))
        print(f"tasks: {len(tasks)} open, {dated} with a date, {len(tasks) - dated} without")
        sample = next((t.get("dueDate") or t.get("startDate") for t, _ in tasks
                       if t.get("dueDate") or t.get("startDate")), None)
        print(f"sample date format from TickTick: {sample!r}")
        p = plan(tasks, now)
        print(f"today: {p['n_today']}, yesterday: {len(p['yesterday'])}, missed: {len(p['missed'])}, "
              f"rest of week: {len(p['this_week'])}, next week: {len(p['next_week'])}, "
              f"later: {len(p['later'])}, no date: {len(p['undated'])}  (as of {now:%a %d %b %H:%M})")
        images = render_pages(p, update_history(p))
        wake = quiet_window(now)
        if wake:
            images[0] = page_night(p, wake)
    except TokenError:
        img = render_message(now, "Reconnect TickTick", [
            "Your TickTick access token has expired or was revoked.",
            "Run get-token.ps1 again and update the TICKTICK_TOKEN secret on GitHub.",
        ])
        images = [img] * len(PAGES)
    except Exception as e:
        print("error:", repr(e), file=sys.stderr)
        img = render_message(now, "Couldn't reach TickTick", [
            "Will retry on the next refresh.",
            f"Details: {type(e).__name__}",
        ])
        images = [img] * len(PAGES)

    wake = quiet_window(now)
    write_site(images, base_url, dash_path, int(wake.timestamp()) if wake else None)


if __name__ == "__main__":
    main()
