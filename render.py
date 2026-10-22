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
    REFRESH_SECONDS how often the Kindle should check (default 60)

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
REFRESH = int(os.environ.get("REFRESH_SECONDS") or 60)
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
    "top": font("bold", 23),
    "num": font("bold", 18),
    "row": font("regular", 21),
    "row_b": font("bold", 21),
    "row_meta": font("semibold", 15),
    "day": font("bold", 14),
    "footer": font("regular", 14),
    "pager": font("regular", 15),
    "pager_on": font("bold", 15),
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


def plan(tasks, now):
    today = now.date()
    yday = today - dt.timedelta(days=1)
    end = week_end(today)
    next_end = end + dt.timedelta(days=7)

    items = [{"title": " ".join((t.get("title") or "(untitled)").split()),
              "list": list_name,
              "priority": t.get("priority", 0) or 0,
              "due": due_of(t)} for t, list_name in tasks]
    dated = [i for i in items if i["due"]]

    today_all = [i for i in dated if i["due"][0] == today]
    yesterday = [i for i in dated if i["due"][0] == yday]
    missed = [i for i in dated if i["due"][0] < today]
    this_week = [i for i in dated if today < i["due"][0] <= end]
    next_week = [i for i in dated if end < i["due"][0] <= next_end]
    later = [i for i in dated if i["due"][0] > next_end]
    undated = [i for i in items if not i["due"]]

    # top 3: most important today, then earliest
    by_importance = sorted(today_all, key=lambda i: (-i["priority"], when(i)[1]))
    top3 = by_importance[:3]
    also_today = sorted(by_importance[3:], key=lambda i: when(i)[1])

    yesterday.sort(key=lambda i: (-i["priority"], when(i)[1]))
    this_week.sort(key=lambda i: when(i))
    next_week.sort(key=lambda i: when(i))
    later.sort(key=lambda i: when(i))
    missed.sort(key=lambda i: (-i["priority"], when(i)))        # important, then oldest
    undated.sort(key=lambda i: (-i["priority"], i["title"].lower()))

    return {"now": now, "today": today, "end": end,
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
    if right_text:
        d.text((W - M - rw, y + 9), right_text, font=rf, fill=BLACK if right_bold else MID)
    return y + ROW


TOP_ROW = 46


def top_row(d, y, n, item, right_text):
    """Big numbered row for the Top 3."""
    s = 26
    d.rectangle((M, y + 8, M + s, y + 8 + s), fill=BLACK)
    num = str(n)
    d.text((M + (s - tw(d, num, F["num"])) / 2, y + 9), num, font=F["num"], fill=WHITE)
    rf = F["row_meta"]
    rw = tw(d, right_text, rf)
    x = M + s + 14
    d.text((x, y + 7), fit(d, item["title"], F["top"], W - M - x - rw - 14), font=F["top"], fill=BLACK)
    if right_text:
        d.text((W - M - rw, y + 15), right_text, font=rf, fill=DARK)
    return y + TOP_ROW


def more_line(d, y, text):
    d.text((M + 28, y + 4), text, font=F["row_meta"], fill=DARK)
    return y + 28


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
                d.text((M, y + 4), g, font=F["day"], fill=DARK)
                d.line((M + tw(d, g, F["day"]) + 10, y + 13, W - M, y + 13), fill=LIGHT, width=1)
                y += 26
        y = row(d, y, i, right_fn(i, now), right_bold)
        shown += 1
    if len(items) > shown:
        y = more_line(d, y, more_text.format(n=len(items) - shown))
    return y + 10


def section_height(items, rows, group_fn=None):
    """Height a section() call will use."""
    shown = items[:rows]
    h = 30 + len(shown) * ROW + 10
    if group_fn:
        h += len({group_fn(i) for i in shown}) * 26
    if len(items) > len(shown):
        h += 28
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


PAGES = ["Today", "Missed", "Later"]


def draw_header(d, now, title, sub=None):
    y = 20
    d.text((M, y), title, font=F["head"], fill=BLACK)
    if sub:
        d.text((W - M - tw(d, sub, F["head_sub"]), y + 12), sub, font=F["head_sub"], fill=DARK)
    y += 46
    d.rectangle((M, y, W - M, y + 2), fill=BLACK)
    return y + 14


def draw_pager(d, page, counts):
    """Footer: which page you're on, and what's on the others."""
    d.line((M, H - 44, W - M, H - 44), fill=LIGHT, width=1)
    x = M
    for k, name in enumerate(PAGES):
        on = k == page
        txt = name + (f" {counts[k]}" if counts[k] is not None and k else "")
        r = 5
        cy = H - 24
        if on:
            d.ellipse((x, cy - r, x + 2 * r, cy + r), fill=BLACK)
        else:
            d.ellipse((x, cy - r, x + 2 * r, cy + r), outline=MID, width=2)
        x += 2 * r + 8
        f = F["pager_on"] if on else F["pager"]
        d.text((x, cy - 10), txt, font=f, fill=BLACK if on else MID)
        x += tw(d, txt, f) + 22
    hint = "tap sides ‹ ›"
    d.text((W - M - tw(d, hint, F["footer"]), H - 33), hint, font=F["footer"], fill=MID)


BOTTOM = H - 56   # last usable pixel above the pager


# ---------------------------------------------------------------- page 1: Today
def page_today(p):
    now, today = p["now"], p["today"]
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    n_week = len(p["this_week"])
    y = draw_header(d, now, now.strftime("%A %d %b").replace(" 0", " "),
                    f"{p['n_today']} today  ·  {n_week} rest of week")

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
        y = section(d, y, "YESTERDAY", p["yesterday"], now, right_list, rows[1],
                    right_bold=False, more_text="+ {n} more from yesterday")
    if p["this_week"]:
        y = section(d, y, "REST OF THIS WEEK", p["this_week"], now, right_time, rows[2],
                    group_fn=by_day, more_text="+ {n} more this week")
    elif not (p["top3"] or p["also_today"] or p["yesterday"]):
        d.text((M, y + 20), "All clear for this week.", font=F["big"], fill=BLACK)

    draw_pager(d, 0, [p["n_today"], len(p["missed"]), len(p["next_week"]) + len(p["later"])])
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

    draw_pager(d, 1, [p["n_today"], len(missed), len(p["next_week"]) + len(p["later"])])
    return img


# ---------------------------------------------------------------- page 3: Later
def page_later(p):
    now, today = p["now"], p["today"]
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    nxt, later, undated = p["next_week"], p["later"], p["undated"]
    nw_start = p["end"] + dt.timedelta(days=1)
    title = "Next week & later"
    y = draw_header(d, now, title, f"from {nw_start.strftime('%a %d %b').replace(' 0', ' ')}")

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
        y = section(d, y, "NO DATE", undated, now, right_list, rows[2],
                    more_text="+ {n} more without a date")

    draw_pager(d, 2, [p["n_today"], len(p["missed"]), len(nxt) + len(later)])
    return img


def render_pages(p):
    return [page_today(p), page_missed(p), page_later(p)]


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
def write_site(images, base_url, dash_path):
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
    out = [({"title": t[0], "dueDate": at(t[1]), **allday, "priority": (t[2] if len(t) > 2 else 0)}, "Home") for t in old]
    out += [
        ({"title": "Transfer domains to new registrar", "dueDate": at(0, 9, 30), "priority": 3}, "Inbox"),
        ({"title": "Book domain and connect repo", "dueDate": at(0, 15)}, "Inbox"),
        ({"title": "Review pull request from the team", "dueDate": at(0, 18), "priority": 5}, "Work"),
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
    now = dt.datetime.now(TZ)
    if "--now" in sys.argv:   # e.g. --now 2026-10-22T16:00 for previews
        now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]).replace(tzinfo=TZ)

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
        images = render_pages(plan(tasks, now))
    except TokenError:
        img = render_message(now, "Reconnect TickTick", [
            "Your TickTick access token has expired or was revoked.",
            "Run get-token.ps1 again and update the TICKTICK_TOKEN secret on GitHub.",
        ])
        images = [img, img, img]
    except Exception as e:
        print("error:", repr(e), file=sys.stderr)
        img = render_message(now, "Couldn't reach TickTick", [
            "Will retry on the next refresh.",
            f"Details: {type(e).__name__}",
        ])
        images = [img, img, img]

    write_site(images, base_url, dash_path)


if __name__ == "__main__":
    main()
