"""Landscape layout (800x600) for the Kindle dashboard.

Rules (agreed with the owner, ADHD-friendly):
  * every task is ONE line, full text: never wrapped, never cut with "..."
    (rows run the full width; an extra-long title shrinks its own font a little)
  * work pages show only what you need to act: title, where, when, size, who's waiting
  * insights live on the Insights page (plus a slim band on Today), as numbers
  * one black element per page, max ~11 rows, "+ N more on the X page" instead of squeezing

Uses the data planning from render.py (plan(), score, energy, focus lists...)."""
import datetime as dt
import json
import os
import sys

from PIL import Image, ImageDraw

import render as R

W, H, M = 800, 600, 20
BLACK, DARK, MID, LIGHT, PALE, WHITE = 0, 40, 75, 190, 232, 255
f = R.font
T_TITLE = f("bold", 24)
T_DATE = f("regular", 18)
T_LABEL = f("semibold", 13)
T_META = f("regular", 14)
T_META_B = f("semibold", 14)
T_MED_B = f("bold", 20)
T_STAT = f("bold", 26)
T_BIG = f("bold", 34)
ROW_SIZES = [18, 17, 16, 15, 14, 13, 12]
ROW_H = 30
HEAD_H = 28
TOP_Y = 60
BOTTOM = H - 42
DAY_END = int(os.environ.get("DAY_END") or 23)          # "day left" counts down to this hour
LIST_MAX_PAGES = int(os.environ.get("LIST_MAX_PAGES") or 3)
HIST_DIR = R.HERE / "history"


def tw(d, s, ft):
    return d.textlength(s, font=ft)


def row_font(d, text, width, bold=False):
    """Largest size that fits the whole text on one line."""
    for s in ROW_SIZES:
        ft = f("bold" if bold else "regular", s)
        if tw(d, text, ft) <= width:
            return ft
    return f("bold" if bold else "regular", ROW_SIZES[-1])


# ------------------------------------------------------------------ small text helpers
def span(n):
    if n < 60:
        return f"{n} mins"
    return f"{n // 60}h" if n % 60 == 0 else f"{n // 60}h {n % 60}m"


def hm(minutes):
    h, m = divmod(max(0, int(minutes)), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def when_text(i, today, with_day=True):
    if not i["due"]:
        return ""
    d0, t = i["due"]
    if d0 < today:
        return f"{R.late_label((today - d0).days)} late"
    if d0 == today:
        return R.clock(t) if t else "today"
    if not with_day:
        return R.clock(t) if t else ""
    if d0 == today + dt.timedelta(days=1):
        day = "tomorrow"
    elif d0 <= today + dt.timedelta(days=6):
        day = d0.strftime("%a")
    else:
        day = d0.strftime("%d %b").lstrip("0")
    return day + (f" {R.clock(t)}" if t else "")


def meta_for(i, today, where=True, when=True, with_day=True):
    parts = []
    if where:
        w = i.get("section") if where == "section" else i["list"]
        if w:
            parts.append(w)
    if when:
        wt = when_text(i, today, with_day)
        if wt:
            parts.append(wt)
    m = R.minutes_of(i)
    if m:
        parts.append(f"~{span(m)}")
    if R.someone_waiting(i):
        parts.append("someone waiting")
    return " · ".join(parts)


# ------------------------------------------------------------------ page chrome
class Book:
    """Collects pages; knows the footer names (one per page *family*)."""

    def __init__(self, p, families):
        self.p = p
        self.families = families          # e.g. ["Today", "Week", "Missed", ...]
        self.images, self.titles = [], []

    def add(self, img, title):
        self.images.append(img)
        self.titles.append(title)


def new_page(book, family, title=None, sub=None, part=None):
    """Blank page with header and footer. part = (k, n) for continued pages."""
    p = book.p
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    title = title or family
    d.text((M, 12), title, font=T_TITLE, fill=BLACK)
    x = M + tw(d, title, T_TITLE) + 14
    date = p["now"].strftime("%a %d %b").replace(" 0", " ")
    d.text((x, 18), date, font=T_DATE, fill=DARK)
    x += tw(d, date, T_DATE) + 18
    extra = sub or ""
    if part and part[1] > 1:
        extra = (extra + " · " if extra else "") + f"continued {part[0]}/{part[1]}"
    if extra:
        d.text((x, 21), extra, font=T_META_B, fill=BLACK)
    # top-right 130px stays empty: the Kindle draws battery + Wi-Fi there
    d.line((M, 52, W - M, 52), fill=BLACK, width=2)

    # footer: every page family by name, current one inverted
    d.line((M, H - 34, W - M, H - 34), fill=LIGHT, width=1)
    fx = M
    missed_n = len(p["missed"])
    for name in book.families:
        txt = name + (f" {missed_n}" if name == "Missed" and missed_n else "")
        if name == family and part and part[1] > 1:
            txt += f" {part[0]}/{part[1]}"
        pw = tw(d, txt, T_META_B) + 12
        if name == family:
            d.rectangle((fx, H - 28, fx + pw, H - 8), fill=BLACK)
            d.text((fx + 6, H - 26), txt, font=T_META_B, fill=WHITE)
        else:
            d.text((fx + 6, H - 26), txt, font=T_META_B, fill=DARK)
        fx += pw + 2
    if R.SYNCED_AT:
        s = "synced " + R.clock(R.SYNCED_AT.time())
        if fx + tw(d, s, T_META_B) + 10 < W - M:
            d.text((W - M - tw(d, s, T_META_B), H - 26), s, font=T_META_B, fill=BLACK)
    return img, d


def section_head(d, y, text, right=""):
    d.text((M, y + 6), text, font=T_LABEL, fill=DARK)
    lx = M + tw(d, text, T_LABEL) + 8
    rx = W - M - (tw(d, right, T_META_B) + 8 if right else 0)
    d.line((lx, y + 14, rx, y + 14), fill=LIGHT, width=1)
    if right:
        d.text((W - M - tw(d, right, T_META_B), y + 5), right, font=T_META_B, fill=BLACK)
    return y + HEAD_H


def task_row(d, y, title, meta="", marker="check", n=None, bold=False, chip=None):
    """One task, one line, full text."""
    x = M
    if marker == "check":
        d.rectangle((x, y + 8, x + 13, y + 21), outline=BLACK, width=2)
        tx = x + 22
    elif marker == "number":
        d.rectangle((x, y + 4, x + 22, y + 26), fill=BLACK)
        d.text((x + 11 - tw(d, str(n), T_META_B) / 2, y + 6), str(n), font=T_META_B, fill=WHITE)
        tx = x + 30
    else:
        tx = x
    right_w = 0
    if chip:
        cw = tw(d, chip, T_LABEL) + 14
        d.rectangle((W - M - cw, y + 5, W - M, y + 25), outline=BLACK, width=2)
        d.text((W - M - cw + 7, y + 8), chip, font=T_LABEL, fill=BLACK)
        right_w = cw + 10
    if meta:
        mw = tw(d, meta, T_META)
        d.text((W - M - right_w - mw, y + 8), meta, font=T_META, fill=DARK)
        right_w += mw
    ft = row_font(d, title, W - M - right_w - 16 - tx, bold)
    d.text((tx, y + 5 + (18 - ft.size) // 2), title, font=ft, fill=BLACK)
    return y + ROW_H


def more_line(d, y, text):
    d.text((M + 22, y + 2), text, font=T_META_B, fill=DARK)
    return y + 24


# ------------------------------------------------------------------ space sharing
def share(sections, space, first_bonus=None):
    """sections: [(n_items, fixed_px)] -> rows per section that fit in `space` px.
    One row each in turn (the first_bonus section gets two turns)."""
    rows = [0] * len(sections)

    def height():
        h = 0
        for (n, fixed), r in zip(sections, rows):
            if n == 0 and fixed == 0:
                continue
            h += fixed + r * ROW_H + (24 if n > r else 0)
        return h

    order = list(range(len(sections)))
    if first_bonus is not None:
        order = [first_bonus] + order
    grew = True
    while grew:
        grew = False
        for k in order:
            if rows[k] < sections[k][0]:
                rows[k] += 1
                if height() > space:
                    rows[k] -= 1
                else:
                    grew = True
    return rows


# ------------------------------------------------------------------ flowing (continued) pages
def flow(book, family, blocks, first_y=TOP_Y, sub=None, top_draw=None, max_pages=LIST_MAX_PAGES):
    """blocks: ("head", text, right) | ("row", kwargs) | ("note", text).
    Splits over continued pages; a section split across pages repeats its heading."""
    pages, cur, y = [], [], first_y
    k, last_head = 0, None
    limit = BOTTOM - 22                    # room for "more on the next page" at the bottom
    while k < len(blocks):
        b = blocks[k]
        need = (HEAD_H + ROW_H) if b[0] == "head" else (ROW_H if b[0] == "row" else 24)
        rest_fits = (y + sum(HEAD_H if x[0] == "head" else ROW_H if x[0] == "row" else 24
                             for x in blocks[k:]) <= BOTTOM)
        if y + need > (BOTTOM if rest_fits else limit):
            pages.append(cur)
            cur, y = [], TOP_Y
            if b[0] == "row" and last_head:
                cur.append(("head", last_head[1] + " (cont.)", ""))
                y += HEAD_H
            continue
        if b[0] == "head":
            last_head = b
        cur.append(b)
        y += HEAD_H if b[0] == "head" else (ROW_H if b[0] == "row" else 24)
        k += 1
    pages.append(cur)

    if len(pages) > max_pages:
        left = sum(1 for pg in pages[max_pages:] for b in pg if b[0] == "row")
        pages = pages[:max_pages]
        last = pages[-1]
        start = first_y if max_pages == 1 else TOP_Y

        def used(pg):
            return start + sum(HEAD_H if b[0] == "head" else ROW_H if b[0] == "row" else 24 for b in pg)
        while last and used(last) + 24 > BOTTOM:
            if last.pop()[0] == "row":
                left += 1
        last.append(("note", f"+ {left} more — open TickTick"))

    n = len(pages)
    for idx, pg in enumerate(pages):
        img, d = new_page(book, family, sub=sub if idx == 0 else None, part=(idx + 1, n))
        y = first_y if idx == 0 else TOP_Y
        if idx == 0 and top_draw:
            top_draw(d)
        for b in pg:
            if b[0] == "head":
                y = section_head(d, y, b[1], b[2])
            elif b[0] == "row":
                y = task_row(d, y, **b[1])
            else:
                y = more_line(d, y, b[1])
        if idx < n - 1:
            t = "more on the next page  >"
            d.text((W - M - tw(d, t, T_META_B), BOTTOM - 18), t, font=T_META_B, fill=BLACK)
        book.add(img, family if idx == 0 else f"{family} {idx + 1}")


# ------------------------------------------------------------------ history: cleared + postponed
def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def track(p):
    """Compare with the previous run: tasks that vanished = cleared; due dates that
    moved later = postponed (recurring ones moving forward = cleared too).
    Stored in history/ (kept by the workflow's actions/cache). Returns a dict."""
    today = p["today"].isoformat()
    snap_f, clr_f = HIST_DIR / "snapshot.json", HIST_DIR / "cleared.json"
    prev = load_json(snap_f, {})
    cleared = load_json(clr_f, {})
    pool = p["all"] + p.get("extra", [])
    cur = {}
    for i in pool:
        if not i.get("id"):
            continue
        due = i["due"][0].isoformat() if i["due"] else None
        old = prev.get(i["id"], {})
        moves = old.get("moves", 0)
        if old.get("d") and due and due > old["d"]:
            if i.get("repeat"):
                cleared[today] = cleared.get(today, 0) + 1
            else:
                moves += 1
        cur[i["id"]] = {"t": i["title"], "d": due, "moves": moves, "first": old.get("first", today)}
    if prev and cur:                       # skip the very first run and failed fetches
        gone = [k for k in prev if k not in cur]
        if len(gone) <= max(10, len(prev) // 3):   # a mass "disappearance" = a filter change, not work
            cleared[today] = cleared.get(today, 0) + len(gone)
    cleared = {k: cleared[k] for k in sorted(cleared)[-60:]}
    try:
        HIST_DIR.mkdir(parents=True, exist_ok=True)
        snap_f.write_text(json.dumps(cur))
        clr_f.write_text(json.dumps(cleared))
    except Exception as e:
        print("warning: couldn't save history:", e, file=sys.stderr)
    for i in pool:
        rec = cur.get(i.get("id"))
        i["moves"] = rec["moves"] if rec else 0
        i["first_seen"] = rec["first"] if rec else today
    return {"cleared": cleared}


# ------------------------------------------------------------------ the pages
def do_now(p):
    if p["top3"]:
        return p["top3"][0]
    if p["missed"]:
        return p["missed"][0]
    if p["this_week"]:
        return p["this_week"][0]
    return None


def planned_minutes(items):
    return sum(R.minutes_of(i) or R.DEFAULT_MINUTES[R.energy_of(i)] for i in items)


def day_left(now):
    end = now.replace(hour=DAY_END % 24, minute=0, second=0, microsecond=0)
    return max(0, (end - now).total_seconds() / 60)


def week_counts(p):
    today = p["today"]
    days = [today + dt.timedelta(days=k) for k in range(7)]
    dated = [i for i in p["all"] if i["due"]]
    return [(d0, sum(1 for i in dated if i["due"][0] == d0)) for d0 in days]


def page_today(book, p, hist, tr):
    now, today = p["now"], p["today"]
    img, d = new_page(book, "Today")
    # ---- insight band (numbers only)
    y, th, gap = TOP_Y, 62, 10
    tw_ = (W - 2 * M - 3 * gap) / 4
    left = day_left(now)
    x0 = M
    d.rectangle((x0, y, x0 + tw_, y + th), outline=BLACK, width=2)
    d.text((x0 + 10, y + 6), "DAY LEFT", font=T_LABEL, fill=DARK)
    s = hm(left)
    d.text((x0 + 10, y + 21), s, font=T_STAT, fill=BLACK)
    bx0 = x0 + 18 + tw(d, s, T_STAT)
    if bx0 < x0 + tw_ - 30:
        total = (DAY_END - 7) * 60
        d.rectangle((bx0, y + 32, x0 + tw_ - 10, y + 44), outline=BLACK, width=2)
        done_w = (x0 + tw_ - 10 - bx0) * min(1, max(0, 1 - left / total))
        d.rectangle((bx0, y + 32, bx0 + max(3, done_w), y + 44), fill=BLACK)

    x0 += tw_ + gap
    d.rectangle((x0, y, x0 + tw_, y + th), outline=BLACK, width=2)
    d.text((x0 + 10, y + 6), "TODAY'S WORK", font=T_LABEL, fill=DARK)
    s = hm(planned_minutes(p["today_all"]))
    d.text((x0 + 10, y + 21), s, font=T_STAT, fill=BLACK)
    small = f"{len(p['today_all'])} tasks"
    if x0 + 18 + tw(d, s, T_STAT) + tw(d, small, T_META_B) < x0 + tw_ - 6:
        d.text((x0 + 18 + tw(d, s, T_STAT), y + 31), small, font=T_META_B, fill=BLACK)

    x0 += tw_ + gap
    d.rectangle((x0, y, x0 + tw_, y + th), outline=BLACK, width=2)
    d.text((x0 + 10, y + 6), "WEEK LOAD", font=T_LABEL, fill=DARK)
    wc = week_counts(p)
    mx = max(1, max(n for _, n in wc))
    bw = (tw_ - 20) / 7
    base = y + th - 18
    for k, (d0, n) in enumerate(wc):
        bx = x0 + 10 + k * bw
        bh = 22 * n / mx
        if k == 0:
            d.rectangle((bx + 3, base - bh, bx + bw - 3, base), fill=BLACK)
        else:
            d.rectangle((bx + 3, base - max(bh, 1), bx + bw - 3, base), outline=BLACK, width=1, fill=PALE)
        dn = d0.strftime("%a")[0]
        d.text((bx + bw / 2 - tw(d, dn, T_LABEL) / 2, base + 1), dn, font=T_LABEL, fill=DARK)

    x0 += tw_ + gap
    d.rectangle((x0, y, x0 + tw_, y + th), outline=BLACK, width=2)
    d.text((x0 + 10, y + 6), "CLEARED · LATE", font=T_LABEL, fill=DARK)
    c = tr["cleared"].get(today.isoformat(), 0)
    d.text((x0 + 10, y + 21), str(c), font=T_STAT, fill=BLACK)
    d.text((x0 + 16 + tw(d, str(c), T_STAT), y + 31), "done", font=T_META_B, fill=BLACK)
    late = len(p["missed"])
    lx = x0 + tw_ / 2 + 4
    d.text((lx, y + 21), str(late), font=T_STAT, fill=BLACK)
    week_ago = (hist or {}).get((today - dt.timedelta(days=7)).isoformat())
    if week_ago is not None and week_ago != late:
        arrow = f"▼{week_ago - late}" if late < week_ago else f"▲{late - week_ago}"
        d.text((lx + 6 + tw(d, str(late), T_STAT), y + 31), arrow, font=T_META_B, fill=BLACK)
    else:
        d.text((lx + 6 + tw(d, str(late), T_STAT), y + 31), "late", font=T_META_B, fill=BLACK)

    # ---- do this now (the one black element)
    y += th + 10
    top = do_now(p)
    bar_h = 36
    step = top.get("step") if top else None
    if step:
        bar_h = 58
    d.rectangle((M, y, W - M, y + bar_h), fill=BLACK)
    d.text((M + 10, y + 11), "DO THIS NOW", font=T_LABEL, fill=LIGHT)
    if top:
        meta = meta_for(top, today)
        mw = tw(d, meta, T_META)
        d.text((W - M - 10 - mw, y + 10), meta, font=T_META, fill=LIGHT)
        avail = W - M - 10 - mw - 16 - (M + 112)
        ft = T_MED_B
        for s_ in (20, 19, 18, 17, 16, 15, 14, 13, 12):
            ft = f("bold", s_)
            if tw(d, top["title"], ft) <= avail:
                break
        d.text((M + 112, y + 7 + (20 - ft.size) // 2), top["title"], font=ft, fill=WHITE)
        if step:
            d.text((M + 10, y + 36), "FIRST STEP", font=T_LABEL, fill=LIGHT)
            sf = row_font(d, step, W - 2 * M - 112 - 10)
            d.text((M + 112, y + 34), step, font=sf, fill=WHITE)
    else:
        d.text((M + 112, y + 7), "Nothing urgent. Pick something from Energy.", font=T_MED_B, fill=WHITE)
    y += bar_h + 8

    # ---- the rest, shared fairly
    then = [i for i in p["top3"] if i is not top][:2]
    also = p["also_today"]
    yday = [i for i in p["yesterday"] if i is not top]
    week = p["this_week"]
    secs = [("THEN", then, "number", None),
            ("ALSO TODAY", also, "check", "more on Huron, Perso…"),
            ("STILL OPEN FROM YESTERDAY", yday, "check", "more on Missed"),
            ("REST OF THE WEEK", week, "check", "more on Week")]
    secs = [s_ for s_ in secs if s_[1]]
    rows = share([(len(s_[1]), HEAD_H + 2) for s_ in secs], BOTTOM - y)
    n_num = 2
    for (name, items, marker, more_where), r in zip(secs, rows):
        if r == 0:
            continue
        y = section_head(d, y, name, str(len(items)) if marker != "number" else "")
        for i in items[:r]:
            if marker == "number":
                y = task_row(d, y, i["title"], meta_for(i, today), "number", n_num, bold=True)
                n_num += 1
            else:
                y = task_row(d, y, i["title"], meta_for(i, today, with_day=name.startswith("REST")))
        if len(items) > r:
            y = more_line(d, y, f"+ {len(items) - r} {more_where}")
        y += 2
    if not secs and not top:
        d.text((M, y + 40), "All clear for today.", font=T_BIG, fill=BLACK)
    book.add(img, "Today")


def page_week(book, p):
    today = p["today"]
    blocks = []
    carried = p["missed"]
    if carried:
        blocks.append(("head", "CARRIED OVER", f"{len(carried)}"))
        for i in carried[:3]:
            blocks.append(("row", dict(title=i["title"], meta=meta_for(i, today))))
        if len(carried) > 3:
            blocks.append(("note", f"+ {len(carried) - 3} more on the Missed page"))
    if p["today_all"]:
        blocks.append(("head", f"TODAY · {today.strftime('%a %d %b').replace(' 0', ' ').upper()}",
                       f"{len(p['today_all'])}"))
        blocks.append(("note", "on the Today page"))

    def by_day(items, label_fmt):
        groups = {}
        for i in items:
            groups.setdefault(i["due"][0], []).append(i)
        for d0 in sorted(groups):
            g = sorted(groups[d0], key=lambda i: R.when(i))
            blocks.append(("head", d0.strftime(label_fmt).replace(" 0", " ").upper(), f"{len(g)}"))
            for i in g:
                blocks.append(("row", dict(title=i["title"], meta=meta_for(i, today, with_day=False))))

    by_day(p["this_week"], "%a %d %b")
    if p["next_week"]:
        by_day(p["next_week"], "NEXT WEEK · %a %d %b")
    if p["later"]:
        blocks.append(("head", "LATER", f"{len(p['later'])}"))
        for i in p["later"]:
            blocks.append(("row", dict(title=i["title"], meta=meta_for(i, today))))
    if not blocks:
        blocks.append(("note", "Nothing planned this week."))
    flow(book, "Week", blocks)


def page_missed(book, p):
    today = p["today"]
    missed = p["missed"]
    blocks = []
    if missed:
        blocks.append(("head", "DECIDE, DON'T DO · 3 MINUTES", ""))
        oldest = sorted(missed, key=lambda i: R.when(i))[:3]
        for i in oldest:
            verdict, _ = R.triage_for(i, today)
            blocks.append(("row", dict(title=i["title"], meta=meta_for(i, today), marker=None, chip=verdict)))
        rest = [i for i in missed if i not in oldest]
        buckets = [("YESTERDAY", 1, 1), ("PAST WEEK", 2, 7), ("PAST MONTH", 8, 31),
                   ("1 – 3 MONTHS", 32, 92), ("OLDER", 93, 10 ** 6)]
        for name, lo, hi in buckets:
            g = [i for i in rest if lo <= (today - i["due"][0]).days <= hi]
            if g:
                blocks.append(("head", name, str(len(g))))
                for i in g:
                    blocks.append(("row", dict(title=i["title"], meta=meta_for(i, today))))
    else:
        blocks.append(("note", "Nothing missed. Nice."))
    flow(book, "Missed", blocks, sub=f"{len(missed)} late" if missed else None)


def page_energy(book, p):
    now, today = p["now"], p["today"]
    img, d = new_page(book, "Energy")
    best, line = R.suggested_energy(now)
    d.text((M, TOP_Y), line + "  →  start with the black box", font=T_META_B, fill=BLACK)
    y = TOP_Y + 26
    pool = p["today_all"] + p["missed"] + p["this_week"]
    courses = {"sharp": [], "okay": [], "drained": []}
    for i in sorted(pool, key=lambda i: (-i["score"], i["due"][0])):
        courses[R.energy_of(i)].append(i)
    if not courses[best]:                  # nothing at this energy level: highlight the next one
        best = next((k for k in ("okay", "drained", "sharp") if courses[k]), best)
    meta = {"sharp": ("SHARP", "deep work"), "okay": ("OKAY", "admin & errands"),
            "drained": ("DRAINED", "no-brainers")}
    order = [best] + [k for k in ("sharp", "okay", "drained") if k != best]
    BOX_HEAD = 34
    rows = share([(len(courses[k]), BOX_HEAD + 12) for k in order], BOTTOM - y, first_bonus=0)
    for k, r in zip(order, rows):
        items = courses[k]
        name, desc = meta[k]
        hl = k == best
        h = BOX_HEAD + r * ROW_H + (24 if len(items) > r else 0) + 4
        if not items:
            h = BOX_HEAD + 4
        if hl:
            d.rectangle((M, y, W - M, y + BOX_HEAD), fill=BLACK)
            fg, sub = WHITE, LIGHT
        else:
            d.rectangle((M, y, W - M, y + BOX_HEAD), outline=BLACK, width=2)
            fg, sub = BLACK, DARK
        bars = {"sharp": 3, "okay": 2, "drained": 1}[k]
        for b in range(3):
            bx = M + 10 + b * 12
            d.rectangle((bx, y + 10, bx + 8, y + 24), fill=fg if b < bars else (MID if hl else LIGHT))
        d.text((M + 56, y + 7), name, font=f("bold", 18), fill=fg)
        d.text((M + 64 + tw(d, name, f("bold", 18)), y + 10), f"{desc} · {len(items)}", font=T_META, fill=sub)
        if items:
            vals = sorted({R.minutes_of(i) or R.DEFAULT_MINUTES[k] for i in items[:r]})
            size = ("~" + span(vals[0]) + " each") if len(vals) == 1 else f"{span(vals[0])} – {span(vals[-1])}"
            d.text((W - M - 10 - tw(d, size, T_META_B), y + 9), size, font=T_META_B, fill=fg)
        yy = y + BOX_HEAD + 2
        for i in items[:r]:
            late = (today - i["due"][0]).days
            m = i["list"] + (f" · {R.late_label(late)} late" if late > 0 else "")
            yy = task_row(d, yy, i["title"], m)
        if len(items) > r:
            yy = more_line(d, yy, f"+ {len(items) - r} more")
        y += h + 8
    book.add(img, "Energy")


def page_list(book, p, name, items):
    today = p["today"]
    late = sum(1 for i in items if i["due"] and i["due"][0] < today)
    sub = f"{len(items)} open" + (f" · {late} late" if late else "")
    top5, rest = items[:5], items[5:]
    blocks = []
    if top5:
        blocks.append(("head", "TOP 5", ""))
        for n, i in enumerate(top5, 1):
            blocks.append(("row", dict(title=i["title"], meta=meta_for(i, today, where="section"),
                                       marker="number", n=n, bold=True)))
    groups = {}
    for i in rest:
        groups.setdefault((i["section_pos"], i["section"]), []).append(i)
    for (pos, sec), g in sorted(groups.items()):
        g.sort(key=lambda i: (-i["score"], i["order"]))
        blocks.append(("head", (sec or "No section").upper(), str(len(g))))
        for i in g:
            blocks.append(("row", dict(title=i["title"], meta=meta_for(i, today, where=False))))
    if not items:
        blocks.append(("note", "Nothing open here."))
    flow(book, name, blocks, sub=sub)


def page_insights(book, p, hist, tr, lists):
    now, today = p["now"], p["today"]
    img, d = new_page(book, "Insights")
    y = TOP_Y
    gap = 10
    # ---- time: 3 tiles
    tw_ = (W - 2 * M - 2 * gap) / 3
    left = day_left(now)
    planned = planned_minutes(p["today_all"])
    tiles = [("DAY LEFT", hm(left), f"until {DAY_END % 12 or 12}{'pm' if DAY_END >= 12 else 'am'}"),
             ("PLANNED TODAY", hm(planned), f"{len(p['today_all'])} tasks"),
             ("SPARE AFTER THAT", hm(max(0, left - planned)),
              "over-planned!" if planned > left else "if you start now")]
    for k, (lab, big, small) in enumerate(tiles):
        x0 = M + k * (tw_ + gap)
        d.rectangle((x0, y, x0 + tw_, y + 62), outline=BLACK, width=2)
        d.text((x0 + 10, y + 6), lab, font=T_LABEL, fill=DARK)
        d.text((x0 + 10, y + 21), big, font=T_STAT, fill=BLACK)
        d.text((x0 + 18 + tw(d, big, T_STAT), y + 31), small, font=T_META_B, fill=BLACK)
    y += 72
    nxt = sorted([i for i in p["today_all"] if i["due"][1] and i["due"][1] > now.time()],
                 key=lambda i: i["due"][1])
    if nxt:
        i = nxt[0]
        mins = (dt.datetime.combine(today, i["due"][1]) - now.replace(tzinfo=None)).total_seconds() / 60
        y = task_row(d, y, f"Next fixed thing: {R.clock(i['due'][1])}  {i['title']}", f"in {hm(mins)}", marker=None)
    else:
        y = task_row(d, y, "No fixed times left today: your time is your own.", "", marker=None)
    y += 4

    # ---- load (left) + late pile (right)
    bw_ = (W - 2 * M - gap) / 2
    bh_ = 128
    d.rectangle((M, y, M + bw_, y + bh_), outline=BLACK, width=2)
    d.text((M + 10, y + 6), "THIS WEEK'S LOAD", font=T_LABEL, fill=DARK)
    wc = week_counts(p)
    mx = max(1, max(n for _, n in wc))
    cw = (bw_ - 20) / 7
    base = y + bh_ - 22
    for k, (d0, n) in enumerate(wc):
        bx = M + 10 + k * cw
        h = 58 * n / mx
        if k == 0:
            d.rectangle((bx + 6, base - h, bx + cw - 6, base), fill=BLACK)
        else:
            d.rectangle((bx + 6, base - max(h, 1), bx + cw - 6, base), outline=BLACK, width=2, fill=PALE)
        d.text((bx + cw / 2 - tw(d, str(n), T_META_B) / 2, base - h - 17), str(n), font=T_META_B, fill=BLACK)
        dn = d0.strftime("%a")
        d.text((bx + cw / 2 - tw(d, dn, T_META) / 2, base + 3), dn, font=T_META, fill=DARK)
    heavy = max(wc[1:], key=lambda x: x[1]) if len(wc) > 1 else None
    light = min(wc[1:], key=lambda x: x[1]) if len(wc) > 1 else None
    if heavy and light and heavy[1] - light[1] >= 4:
        tip = f"Move 2 from {heavy[0]:%a} to {light[0]:%a}?"
        d.text((M + bw_ - 10 - tw(d, tip, T_META_B), y + 6), tip, font=T_META_B, fill=BLACK)

    x2 = M + bw_ + gap
    d.rectangle((x2, y, x2 + bw_, y + bh_), outline=BLACK, width=2)
    d.text((x2 + 10, y + 6), "LATE PILE", font=T_LABEL, fill=DARK)
    late = len(p["missed"])
    d.text((x2 + 10, y + 22), str(late), font=T_BIG, fill=BLACK)
    week_ago = (hist or {}).get((today - dt.timedelta(days=7)).isoformat())
    ax = x2 + 18 + tw(d, str(late), T_BIG)
    if week_ago is not None:
        diff = late - week_ago
        trend = "same as last week" if diff == 0 else (f"▼ {-diff} vs last week" if diff < 0 else f"▲ {diff} vs last week")
        d.text((ax, y + 26), trend, font=T_META_B, fill=BLACK)
    streak = R.streak_of(hist or {}, today)
    if streak:
        d.text((ax, y + 44), f"not growing for {streak} day{'s' if streak > 1 else ''}", font=T_META, fill=DARK)
    if p["missed"]:
        oldest = min(p["missed"], key=lambda i: i["due"][0])
        age = (today - oldest["due"][0]).days
        piles = {}
        for i in p["missed"]:
            piles[i["list"]] = piles.get(i["list"], 0) + 1
        top_list, top_n = max(piles.items(), key=lambda kv: kv[1])
        d.text((x2 + 10, y + 74), f"oldest: {R.late_label(age)} · most in {top_list} ({top_n})", font=T_META_B, fill=BLACK)
    vals = [(hist or {}).get((today - dt.timedelta(days=6 - k)).isoformat()) for k in range(7)]
    known = [v for v in vals if v is not None]
    if known:
        lo, hi = min(known), max(known)
        sx = x2 + 10
        for k, v in enumerate(vals):
            if v is None:
                continue
            h = 6 + (16 * (v - lo) / (hi - lo) if hi > lo else 8)
            d.rectangle((sx + k * 18, y + bh_ - 8 - h, sx + k * 18 + 12, y + bh_ - 8), fill=BLACK if k == 6 else LIGHT)
    y += bh_ + 10

    # ---- progress line
    cl = tr["cleared"]
    c_today = cl.get(today.isoformat(), 0)
    week_start = today - dt.timedelta(days=today.weekday())
    c_week = sum(v for k, v in cl.items() if k >= week_start.isoformat())
    best = max(((k, v) for k, v in cl.items() if k >= (today - dt.timedelta(days=6)).isoformat()),
               key=lambda kv: kv[1], default=None)
    parts = [f"Cleared today: {c_today}", f"this week: {c_week}"]
    if best and best[1]:
        parts.append(f"best day: {dt.date.fromisoformat(best[0]):%a} ({best[1]})")
    d.rectangle((M, y, W - M, y + 32), outline=BLACK, width=2)
    d.text((M + 10, y + 7), "PROGRESS", font=T_LABEL, fill=DARK)
    d.text((M + 90, y + 6), "   ·   ".join(parts), font=T_META_B, fill=BLACK)
    y += 42

    # ---- avoiding
    pool = [i for i in p["all"] + p.get("extra", []) if i["due"] and i["due"][0] < today]
    pool.sort(key=lambda i: (-(i.get("moves", 0) * 30 + (today - i["due"][0]).days)))
    avoid = [i for i in pool if i.get("moves", 0) > 0 or (today - i["due"][0]).days > 30][:3]
    if avoid:
        y = section_head(d, y, "YOU MIGHT BE AVOIDING", "pick one, do 5 minutes")
        for i in avoid:
            bits = []
            if i.get("moves"):
                bits.append(f"moved {i['moves']}×")
            bits.append(f"{R.late_label((today - i['due'][0]).days)} late")
            bits.append(i["list"])
            y = task_row(d, y, i["title"], " · ".join(bits))
        y += 4

    # ---- balance across lists
    if lists and y + 30 <= BOTTOM:
        d.text((M, y + 6), "BALANCE", font=T_LABEL, fill=DARK)
        x = M + 74
        for name, items in lists:
            lt = sum(1 for i in items if i["due"] and i["due"][0] < today)
            chip = f"{name} {len(items)}" + (f" · {lt} late" if lt else "")
            pw = tw(d, chip, T_META_B) + 16
            if x + pw > W - M:
                break
            d.rectangle((x, y + 2, x + pw, y + 26), outline=BLACK, width=2 if lt else 1)
            d.text((x + 8, y + 6), chip, font=T_META_B, fill=BLACK)
            x += pw + 8
    book.add(img, "Insights")


def page_night(p, wake, families):
    book = Book(p, families)
    now, today = p["now"], p["today"]
    img, d = new_page(book, "Today", title="Good night", sub="updates resume " + R.clock(wake.time()))
    tomorrow = today + dt.timedelta(days=1) if now.hour >= 12 else today
    d.text((M, TOP_Y + 6), "Rest. Tomorrow is planned.", font=T_BIG, fill=BLACK)
    y = TOP_Y + 60
    due_next = [i for i in p["all"] if i["due"] and i["due"][0] == tomorrow]
    due_next.sort(key=lambda i: (-i["score"], R.when(i)[1]))
    y = section_head(d, y, "FIRST THING " + ("TOMORROW" if tomorrow != today else "THIS MORNING"),
                     str(len(due_next)) if due_next else "")
    for n, i in enumerate(due_next[:5], 1):
        y = task_row(d, y, i["title"], meta_for(i, today, with_day=False), "number", n, bold=n == 1)
    if len(due_next) > 5:
        y = more_line(d, y, f"+ {len(due_next) - 5} more tomorrow")
    if not due_next:
        y = more_line(d, y, "Nothing scheduled. Pick one from Missed in the morning.")
    if p["missed"]:
        y = section_head(d, y + 8, "STILL WAITING", f"{len(p['missed'])} late")
        y = task_row(d, y, p["missed"][0]["title"], meta_for(p["missed"][0], today))
    d.text((M, BOTTOM - 20), "Updates pause overnight. Tap the middle to update now.", font=T_META_B, fill=DARK)
    return img


def message_page(now, title, lines):
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    d.text((M, 12), now.strftime("%A %d %b").replace(" 0", " "), font=T_TITLE, fill=BLACK)
    d.line((M, 52, W - M, 52), fill=BLACK, width=2)
    d.text((M, 180), title, font=T_BIG, fill=BLACK)
    y = 236
    for ln in lines:
        d.text((M, y), ln, font=row_font(d, ln, W - 2 * M), fill=DARK)
        y += 32
    return img


def render_all(p, hist):
    """All landscape pages. Returns (images, titles)."""
    tr = track(p)
    lists = R.focus_lists(p)
    families = ["Today", "Week", "Missed", "Energy"] + [n for n, _ in lists] + ["Insights"]
    book = Book(p, families)
    page_today(book, p, hist, tr)
    page_week(book, p)
    page_missed(book, p)
    page_energy(book, p)
    for name, items in lists:
        page_list(book, p, name, items)
    page_insights(book, p, hist, tr, lists)
    return book.images, book.titles, families
