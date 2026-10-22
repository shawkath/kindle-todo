#!/usr/bin/env python3
"""
Kindle TickTick dashboard renderer (ADHD-friendly "focus" layout).

Screen, top to bottom:
    1. DO THIS FIRST  one task, picked for you (missed + high priority first)
    2. MISSED         overdue tasks, never hidden, with "days late"
    3. THIS WEEK      remaining tasks due today .. end of week, grouped by day
    4. footer         counts of what's parked (later / no date) + update time

Fetches open tasks from the TickTick Open API, draws a 600x800 grayscale
image for a Kindle (8th gen), and writes a static "TRMNL-compatible" site
that the TRMNL KOReader plugin can poll:

    site/<DASH_PATH>/api/display      -> JSON {image_url, filename, refresh_rate}
    site/<DASH_PATH>/screen-<hash>.png

Environment variables
    TICKTICK_TOKEN  TickTick access token (required unless --demo)
    DASH_PATH       secret folder name for the site (required unless --demo)
    BASE_URL        e.g. https://username.github.io/kindle-todo  (set by the workflow)
    TZ_NAME         time zone for "today" (default Asia/Kolkata)
    LISTS           optional comma-separated TickTick list names to show
                    (default: Inbox + all lists)
    WEEK            "calendar" (today .. Sunday, default) or "rolling" (next 7 days)
    REFRESH_SECONDS how often the Kindle should check (default 1800)

Run `python render.py --demo` to render sample data to preview.png.
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
REFRESH = int(os.environ.get("REFRESH_SECONDS") or 1800)
WEEK_MODE = (os.environ.get("WEEK") or "calendar").lower()

# e-ink friendly greys (Kindle shows 16 levels; keep text dark)
BLACK, DARK, MID, LIGHT, WHITE = 0, 55, 100, 205, 255
ROW = 34                   # height of one compact task row


def font(weight, size):
    name = {"regular": "Inter-Regular.otf",
            "semibold": "Inter-SemiBold.otf",
            "bold": "Inter-Bold.otf"}[weight]
    return ImageFont.truetype(str(FONT_DIR / name), size)


F = {
    "head": font("bold", 30),
    "head_sub": font("regular", 17),
    "label": font("bold", 14),
    "focus": font("bold", 21),
    "focus_meta": font("regular", 16),
    "row": font("regular", 21),
    "row_b": font("bold", 21),
    "row_meta": font("semibold", 15),
    "day": font("bold", 14),
    "footer": font("regular", 14),
    "big": font("bold", 34),
    "body": font("regular", 20),
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
        for t in data.get("tasks", []) or []:
            if t.get("status", 0) == 0:          # 0 = not completed
                out.append((t, p["name"]))
    return out


# ---------------------------------------------------------------- planning
def due_of(task):
    """Return (date, time_or_None) in local TZ, or None if no due date."""
    raw = task.get("dueDate")
    if not raw:
        return None
    try:
        when = dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    if task.get("isAllDay"):
        tz = ZoneInfo(task.get("timeZone") or str(TZ))
        return when.astimezone(tz).date(), None
    local = when.astimezone(TZ)
    return local.date(), local.time()


def week_end(today):
    if WEEK_MODE == "rolling":
        return today + dt.timedelta(days=6)
    return today + dt.timedelta(days=6 - today.weekday())   # through Sunday


def plan(tasks, now):
    today = now.date()
    end = week_end(today)
    items = []
    for t, list_name in tasks:
        items.append({"title": " ".join((t.get("title") or "(untitled)").split()),
                      "list": list_name,
                      "priority": t.get("priority", 0) or 0,
                      "due": due_of(t)})

    missed = [i for i in items if i["due"] and i["due"][0] < today]
    week = [i for i in items if i["due"] and today <= i["due"][0] <= end]
    later = [i for i in items if i["due"] and i["due"][0] > end]
    undated = [i for i in items if not i["due"]]

    # also treat timed tasks earlier today that have passed as "missed"
    for i in list(week):
        d, t = i["due"]
        if d == today and t and t < (now - dt.timedelta(minutes=30)).time():
            week.remove(i)
            missed.append(i)

    def when(i):
        d, t = i["due"]
        return (d, t or dt.time(23, 59, 59))

    # missed: most important first, then the oldest (the ones you keep skipping)
    missed.sort(key=lambda i: (-i["priority"], when(i)))
    week.sort(key=lambda i: (when(i)[0], when(i)[1], -i["priority"]))   # day, then clock time

    # pick ONE task to start with
    focus = None
    hi_missed = [i for i in missed if i["priority"] >= 3]
    today_items = [i for i in week if i["due"][0] == today]
    if hi_missed:
        focus = hi_missed[0]
    elif today_items:
        focus = sorted(today_items, key=lambda i: (-i["priority"], when(i)[1]))[0]
    elif missed:
        focus = missed[0]
    elif week:
        focus = week[0]
    n_missed, n_week = len(missed), len(week)
    for bucket in (missed, week):
        if focus in bucket:
            bucket.remove(focus)

    return {"focus": focus, "missed": missed, "week": week,
            "n_missed": n_missed, "n_week": n_week,
            "later": len(later), "undated": len(undated),
            "total": len(items), "end": end}


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


def clock(t):
    return t.strftime("%I:%M%p").lstrip("0").lower()   # 3:00pm


def label(d, y, text, inverted=False):
    """Small uppercase section label. Inverted = white on black bar."""
    if inverted:
        wpx = tw(d, text, F["label"]) + 16
        d.rectangle((M, y, M + wpx, y + 22), fill=BLACK)
        d.text((M + 8, y + 3), text, font=F["label"], fill=WHITE)
    else:
        d.text((M, y + 3), text, font=F["label"], fill=DARK)
    return y + 30


def row(d, y, item, right_text, right_bold=False):
    """One compact line: checkbox, title, right-aligned meta."""
    box = 16
    d.rectangle((M, y + 8, M + box, y + 8 + box), outline=BLACK, width=2)
    rf = F["row_meta"]
    rw = tw(d, right_text, rf)
    x = M + box + 12
    title_f = F["row_b"] if item["priority"] >= 5 else F["row"]
    d.text((x, y + 3), fit(d, item["title"], title_f, W - M - x - rw - 14),
           font=title_f, fill=BLACK)
    d.text((W - M - rw, y + 9), right_text, font=rf, fill=BLACK if right_bold else MID)
    return y + ROW


# ---------------------------------------------------------------- screens
def draw_header(d, now, p):
    y = 20
    d.text((M, y), now.strftime("%A %d %b").replace(" 0", " "), font=F["head"], fill=BLACK)
    if p is not None:
        sub = f"{p['n_week']} this week"
        if p["n_missed"]:
            sub = f"{p['n_missed']} missed  ·  " + sub
        d.text((W - M - tw(d, sub, F["head_sub"]), y + 12), sub, font=F["head_sub"], fill=DARK)
    y += 46
    d.rectangle((M, y, W - M, y + 2), fill=BLACK)
    return y + 14


def draw_focus(d, y, item, now):
    """Compact one-line black bar: DO FIRST | title ........ meta"""
    # work out the meta + available width first so we know if it needs 2 lines
    tag = "DO FIRST"
    tag_w = tw(d, tag, F["label"])
    x0 = M + 14 + tag_w + 24

    meta = ""
    if item["due"]:
        day, t = item["due"]
        late = (now.date() - day).days
        if late > 0:
            meta = f"{late}d late"
        elif day == now.date():
            meta = clock(t) if t else "today"
        else:
            meta = day.strftime("%a") + (f" {clock(t)}" if t else "")
    mw = tw(d, meta, F["row_meta"])
    room = W - M - 14 - mw - 14 - x0
    lines = [fit(d, item["title"], F["focus"], room)]   # always one line
    h = 46

    d.rounded_rectangle((M, y, W - M, y + h), radius=8, fill=BLACK)
    d.text((M + 14, y + 15), tag, font=F["label"], fill=LIGHT)
    d.line((x0 - 12, y + 12, x0 - 12, y + h - 12), fill=MID, width=1)
    d.text((W - M - 14 - mw, y + 16), meta, font=F["row_meta"], fill=LIGHT)
    for n, ln in enumerate(lines):
        d.text((x0, y + 10 + n * 28), ln, font=F["focus"], fill=WHITE)
    return y + h + 16


def draw_missed(d, y, missed, now, max_rows):
    y = label(d, y, f"MISSED  {len(missed)}", inverted=True)
    shown = missed[:max_rows]
    if len(missed) > max_rows:          # make room for the "+N" line
        shown = missed[:max_rows - 1]
    for i in shown:
        late = (now.date() - i["due"][0]).days
        right = f"{late}d late" if late > 0 else f"today {clock(i['due'][1])}"
        y = row(d, y, i, right, right_bold=True)
    rest = len(missed) - len(shown)
    if rest:
        d.text((M + 28, y + 4), f"+ {rest} more missed — open TickTick", font=F["row_meta"], fill=DARK)
        y += 28
    return y + 12


def draw_week(d, y, week, now, bottom):
    today = now.date()
    y = label(d, y, "THIS WEEK")
    if not week:
        d.text((M, y + 2), "Nothing else due this week.", font=F["row"], fill=MID)
        return y + ROW, 0

    shown, cur_day = 0, None
    for idx, i in enumerate(week):
        day, t = i["due"]
        need = ROW + (26 if day != cur_day else 0)
        remaining = len(week) - idx
        if y + need > bottom - (26 if remaining > 1 else 0):
            break
        if day != cur_day:
            cur_day = day
            name = "TODAY" if day == today else ("TOMORROW" if day == today + dt.timedelta(days=1)
                                                  else day.strftime("%a %d").upper().replace(" 0", " "))
            d.text((M, y + 4), name, font=F["day"], fill=DARK)
            d.line((M + tw(d, name, F["day"]) + 10, y + 13, W - M, y + 13), fill=LIGHT, width=1)
            y += 26
        y = row(d, y, i, clock(t) if t else "")
        shown += 1

    rest = len(week) - shown
    if rest:
        d.text((M + 28, y + 4), f"+ {rest} more this week", font=F["row_meta"], fill=DARK)
        y += 28
    return y, shown


def draw_footer(d, now, p=None):
    parts = []
    if p is not None:
        if p["later"]:
            parts.append(f"{p['later']} later")
        if p["undated"]:
            parts.append(f"{p['undated']} no date")
    parts.append(f"updated {clock(now.time())}")
    d.line((M, H - 40, W - M, H - 40), fill=LIGHT, width=1)
    d.text((M, H - 31), "  ·  ".join(parts), font=F["footer"], fill=MID)


def render(p, now):
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    y = draw_header(d, now, p)
    bottom = H - 52

    if not p["focus"]:
        d.text((M, y + 160), "All clear.", font=F["big"], fill=BLACK)
        d.text((M, y + 210), "Nothing missed, nothing due this week.", font=F["body"], fill=DARK)
        draw_footer(d, now, p)
        return img

    y = draw_focus(d, y, p["focus"], now)

    if p["missed"]:
        # missed tasks always show, but never take more than ~40% of what's left
        space_rows = max(2, int((bottom - y) * 0.4) // ROW)
        y = draw_missed(d, y, p["missed"], now, space_rows)

    draw_week(d, y, p["week"], now, bottom)
    draw_footer(d, now, p)
    return img


def render_message(now, title, body_lines):
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    y = draw_header(d, now, None)
    d.text((M, y + 120), title, font=F["big"], fill=BLACK)
    yy = y + 175
    for ln in body_lines:
        for sub in wrap(d, ln, F["body"], W - 2 * M, 4):
            d.text((M, yy), sub, font=F["body"], fill=DARK)
            yy += 28
        yy += 8
    draw_footer(d, now)
    return img


# ---------------------------------------------------------------- output
def write_site(img, base_url, dash_path):
    root = HERE / "site"
    if root.exists():
        shutil.rmtree(root)
    out = root / dash_path
    (out / "api").mkdir(parents=True)
    (root / ".nojekyll").write_text("")
    (root / "index.html").write_text("<!doctype html><title>.</title>")

    png = out / "tmp.png"
    img.save(png, optimize=True)
    digest = hashlib.sha1(png.read_bytes()).hexdigest()[:12]
    name = f"screen-{digest}.png"
    png.rename(out / name)

    payload = {
        "image_url": f"{base_url.rstrip('/')}/{dash_path}/{name}",
        "filename": name,
        "refresh_rate": REFRESH,
    }
    (out / "api" / "display").write_text(json.dumps(payload))
    print("wrote", out / name)


def demo_tasks():
    today = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    def at(days, h=None, m=0):
        t = today + dt.timedelta(days=days, hours=h or 0, minutes=m)
        return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")

    allday = {"isAllDay": True, "timeZone": "Asia/Kolkata"}
    return [
        ({"title": "Send design review notes to the team", "dueDate": at(-3, 18), "priority": 5}, "Work"),
        ({"title": "Renew vehicle insurance", "dueDate": at(-9), **allday, "priority": 3}, "Personal"),
        ({"title": "Reply to accountant", "dueDate": at(-1, 11)}, "Inbox"),
        ({"title": "Return library books", "dueDate": at(-2), **allday}, "Personal"),
        ({"title": "Book car service", "dueDate": at(0, 10, 30), "priority": 3}, "Personal"),
        ({"title": "Review wireframes and finalise the dashboard flows", "dueDate": at(0, 21), "priority": 5}, "Work"),
        ({"title": "Pay electricity bill", "dueDate": at(0), **allday}, "Inbox"),
        ({"title": "Call plumber", "dueDate": at(1, 11)}, "Home"),
        ({"title": "Draft sale page copy", "dueDate": at(1, 17), "priority": 3}, "Side projects"),
        ({"title": "Weekly groceries", "dueDate": at(2), **allday}, "Home"),
        ({"title": "Read accessibility study guide ch. 2", "dueDate": at(3, 9)}, "Learning"),
        ({"title": "Plan next week", "dueDate": at(3, 19), "priority": 3}, "Inbox"),
        ({"title": "Dentist appointment", "dueDate": at(9, 10)}, "Personal"),
        ({"title": "Buy printer ink"}, "Inbox"),
        ({"title": "Back up Kindle notes"}, "Inbox"),
    ]


def main():
    now = dt.datetime.now(TZ)
    if "--now" in sys.argv:   # e.g. --now 2026-10-22T16:00 for previews
        now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]).replace(tzinfo=TZ)

    if "--demo" in sys.argv:
        render(plan(demo_tasks(), now), now).save(HERE / "preview.png")
        print("wrote", HERE / "preview.png")
        return

    token = os.environ.get("TICKTICK_TOKEN", "").strip()
    dash_path = os.environ.get("DASH_PATH", "").strip().strip("/")
    base_url = os.environ.get("BASE_URL", "").strip()
    if not (token and dash_path and base_url):
        sys.exit("Missing TICKTICK_TOKEN, DASH_PATH or BASE_URL")

    try:
        tasks = fetch_tasks(token, os.environ.get("LISTS", ""))
        img = render(plan(tasks, now), now)
    except TokenError:
        img = render_message(now, "Reconnect TickTick", [
            "Your TickTick access token has expired or was revoked.",
            "Run get-token.ps1 again and update the TICKTICK_TOKEN secret on GitHub.",
        ])
    except Exception as e:
        print("error:", repr(e), file=sys.stderr)
        img = render_message(now, "Couldn't reach TickTick", [
            "Will retry on the next refresh.",
            f"Details: {type(e).__name__}",
        ])

    write_site(img, base_url, dash_path)


if __name__ == "__main__":
    main()
