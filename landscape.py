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
GREY_LINE = 150          # card borders: visible but quiet
f = R.font
T_TITLE = f("bold", 26)
T_DATE = f("semibold", 20)
T_LABEL = f("semibold", 16)
T_META = f("semibold", 17)
T_META_B = f("semibold", 17)
T_MED_B = f("bold", 22)
T_STAT = f("bold", 28)
T_BIG = f("bold", 36)
ROW_SIZES = [22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12]
ROW_H = 36
HEAD_H = 32
TOP_Y = 66
BOTTOM = H - 46
DAY_END = int(os.environ.get("DAY_END") or 23)          # "day left" counts down to this hour
LIST_MAX_PAGES = int(os.environ.get("LIST_MAX_PAGES") or 3)
HIST_DIR = R.HERE / "history"


def tw(d, s, ft):
    return d.textlength(s, font=ft)


def row_font(d, text, width, bold=False):
    """Largest size that fits the whole text on one line."""
    for s in ROW_SIZES:
        ft = f("bold" if bold else "semibold", s)
        if tw(d, text, ft) <= width:
            return ft
    return f("bold" if bold else "semibold", ROW_SIZES[-1])


# ------------------------------------------------------------------ small text helpers
def span(n):
    if n < 60:
        return f"{n} mins"
    return f"{n // 60}h" if n % 60 == 0 else f"{n // 60}h {n % 60}m"


def hm(minutes):
    h, m = divmod(max(0, int(minutes)), 60)
    if h and not m:
        return f"{h}h"
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
    d.line((M, 56, W - M, 56), fill=BLACK, width=2)

    # footer: every page family by name, current one inverted
    d.line((M, H - 38, W - M, H - 38), fill=LIGHT, width=1)
    fx = M
    missed_n = len(p["missed"])
    for name in book.families:
        txt = name + (f" {missed_n}" if name == "Missed" and missed_n else "")
        if name == family and part and part[1] > 1:
            txt += f" {part[0]}/{part[1]}"
        pw = tw(d, txt, T_META_B) + 12
        if name == family:
            d.rectangle((fx, H - 31, fx + pw, H - 8), fill=BLACK)
            d.text((fx + 6, H - 29), txt, font=T_META_B, fill=WHITE)
        else:
            d.text((fx + 6, H - 29), txt, font=T_META_B, fill=DARK)
        fx += pw + 2
    if R.SYNCED_AT:
        s = "synced " + R.clock(R.SYNCED_AT.time())
        if fx + tw(d, s, T_META_B) + 10 < W - M:
            d.text((W - M - tw(d, s, T_META_B), H - 29), s, font=T_META_B, fill=BLACK)
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
        d.rectangle((x, y + 10, x + 15, y + 25), outline=BLACK, width=2)
        tx = x + 25
    elif marker == "number":
        d.rectangle((x, y + 5, x + 25, y + 30), fill=BLACK)
        d.text((x + 13 - tw(d, str(n), T_META_B) / 2, y + 8), str(n), font=T_META_B, fill=WHITE)
        tx = x + 33
    else:
        tx = x
    right_w = 0
    if chip:
        cw = tw(d, chip, T_LABEL) + 14
        d.rectangle((W - M - cw, y + 6, W - M, y + 30), outline=BLACK, width=2)
        d.text((W - M - cw + 7, y + 9), chip, font=T_LABEL, fill=BLACK)
        right_w = cw + 10
    # Keep the title big: if it doesn't fit at full size, drop details from the right
    # side first ("someone waiting", then the list name) before shrinking the title.
    if meta:
        parts = meta.split(" · ")
        options = [parts, [p_ for p_ in parts if p_ != "someone waiting"]]
        options.append(options[-1][1:] if len(options[-1]) > 1 else options[-1])
        options.append([])
        full = f("bold" if bold else "semibold", ROW_SIZES[0])
        for opt in options:
            m_ = " · ".join(opt)
            mw = tw(d, m_, T_META) if m_ else 0
            if tw(d, title, full) <= W - M - right_w - mw - 16 - tx or not opt:
                break
        meta = m_
    if meta:
        mw = tw(d, meta, T_META)
        d.text((W - M - right_w - mw, y + 10), meta, font=T_META, fill=DARK)
        right_w += mw
    ft = row_font(d, title, W - M - right_w - 16 - tx, bold)
    d.text((tx, y + 6 + (22 - ft.size) // 2), title, font=ft, fill=BLACK)
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
    done_f = HIST_DIR / "done.json"
    done = load_json(done_f, {})            # {date: [titles you finished]}
    done_today = done.setdefault(today, [])
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
                done_today.append(i["title"])
            else:
                moves += 1
        cur[i["id"]] = {"t": i["title"], "d": due, "moves": moves, "first": old.get("first", today)}
    if prev and cur:                       # skip the very first run and failed fetches
        gone = [k for k in prev if k not in cur]
        if len(gone) <= max(10, len(prev) // 3):   # a mass "disappearance" = a filter change, not work
            cleared[today] = cleared.get(today, 0) + len(gone)
            done_today.extend(prev[k].get("t", "") for k in gone)
    cleared = {k: cleared[k] for k in sorted(cleared)[-60:]}
    done = {k: done[k][-40:] for k in sorted(done)[-7:]}
    try:
        HIST_DIR.mkdir(parents=True, exist_ok=True)
        snap_f.write_text(json.dumps(cur))
        clr_f.write_text(json.dumps(cleared))
        done_f.write_text(json.dumps(done))
    except Exception as e:
        print("warning: couldn't save history:", e, file=sys.stderr)
    for i in pool:
        rec = cur.get(i.get("id"))
        i["moves"] = rec["moves"] if rec else 0
        i["first_seen"] = rec["first"] if rec else today
    return {"cleared": cleared, "done": done}


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
    y, th, gap = TOP_Y, 68, 10
    tw_ = (W - 2 * M - 3 * gap) / 4
    left = day_left(now)
    x0 = M
    d.rectangle((x0, y, x0 + tw_, y + th), outline=BLACK, width=2)
    d.text((x0 + 10, y + 6), "DAY LEFT", font=T_LABEL, fill=DARK)
    s = hm(left)
    d.text((x0 + 10, y + 23), s, font=T_STAT, fill=BLACK)
    bx0 = x0 + 18 + tw(d, s, T_STAT)
    if bx0 < x0 + tw_ - 30:
        total = (DAY_END - 7) * 60
        d.rectangle((bx0, y + 36, x0 + tw_ - 10, y + 48), outline=BLACK, width=2)
        done_w = (x0 + tw_ - 10 - bx0) * min(1, max(0, 1 - left / total))
        d.rectangle((bx0, y + 36, bx0 + max(3, done_w), y + 48), fill=BLACK)

    x0 += tw_ + gap
    d.rectangle((x0, y, x0 + tw_, y + th), outline=BLACK, width=2)
    d.text((x0 + 10, y + 6), "TODAY'S WORK", font=T_LABEL, fill=DARK)
    s = hm(planned_minutes(p["today_all"]))
    d.text((x0 + 10, y + 23), s, font=T_STAT, fill=BLACK)
    small = f"{len(p['today_all'])} tasks"
    if x0 + 18 + tw(d, s, T_STAT) + tw(d, small, T_META_B) < x0 + tw_ - 6:
        d.text((x0 + 18 + tw(d, s, T_STAT), y + 34), small, font=T_META_B, fill=BLACK)

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
    d.text((x0 + 10, y + 23), str(c), font=T_STAT, fill=BLACK)
    d.text((x0 + 16 + tw(d, str(c), T_STAT), y + 34), "done", font=T_META_B, fill=BLACK)
    late = len(p["missed"])
    lx = x0 + tw_ / 2 + 4
    d.text((lx, y + 23), str(late), font=T_STAT, fill=BLACK)
    week_ago = (hist or {}).get((today - dt.timedelta(days=7)).isoformat())
    if week_ago is not None and week_ago != late:
        arrow = f"▼{week_ago - late}" if late < week_ago else f"▲{late - week_ago}"
        d.text((lx + 6 + tw(d, str(late), T_STAT), y + 34), arrow, font=T_META_B, fill=BLACK)
    else:
        d.text((lx + 6 + tw(d, str(late), T_STAT), y + 34), "late", font=T_META_B, fill=BLACK)

    # ---- do this now (the one black element)
    y += th + 10
    top = do_now(p)
    bar_h = 40
    step = top.get("step") if top else None
    if step:
        bar_h = 66
    d.rectangle((M, y, W - M, y + bar_h), fill=BLACK)
    d.text((M + 10, y + 11), "DO THIS NOW", font=T_LABEL, fill=LIGHT)
    if top:
        meta = meta_for(top, today)
        mw = tw(d, meta, T_META)
        d.text((W - M - 10 - mw, y + 10), meta, font=T_META, fill=LIGHT)
        avail = W - M - 10 - mw - 16 - (M + 136)
        ft = T_MED_B
        for s_ in (22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12):
            ft = f("bold", s_)
            if tw(d, top["title"], ft) <= avail:
                break
        d.text((M + 136, y + 7 + (22 - ft.size) // 2), top["title"], font=ft, fill=WHITE)
        if step:
            d.text((M + 10, y + 41), "FIRST STEP", font=T_LABEL, fill=LIGHT)
            sf = row_font(d, step, W - 2 * M - 136 - 10)
            d.text((M + 136, y + 39), step, font=sf, fill=WHITE)
    else:
        d.text((M + 136, y + 7), "Nothing urgent. Pick something from Energy.", font=T_MED_B, fill=WHITE)
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
        d.text((M + 56, y + 7), name, font=f("bold", 22), fill=fg)
        d.text((M + 64 + tw(d, name, f("bold", 22)), y + 10), f"{desc} · {len(items)}", font=T_META, fill=sub)
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
    if name.lower() == R.ASANA_LIST.lower() and R.ASANA_WS_SHOWN:     # say which Asana workspace this is
        sub = f"{' + '.join(R.ASANA_WS_SHOWN)} workspace · " + sub
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
    """Insights as a tidy grid of cards (easy to follow on e-ink):
        headline
        [done today] [streak] [this week] [late]
        [wins, last 7 days]      [late pile, last 7 days]
        [today's wins]           [your lists]
        ███ one next best move ███"""
    now, today = p["now"], p["today"]
    img, d = new_page(book, "Insights")
    hist = hist or {}
    cl = tr["cleared"]
    first_day = min((dt.date.fromisoformat(k) for k in cl), default=today)   # tracking started
    done_n = cl.get(today.isoformat(), 0)
    yday_done = cl.get((today - dt.timedelta(days=1)).isoformat())
    week_total = sum(cl.get((today - dt.timedelta(days=k)).isoformat(), 0) for k in range(7))
    prev_week = sum(cl.get((today - dt.timedelta(days=7 + k)).isoformat(), 0) for k in range(7))
    late = len(p["missed"])
    week_ago = hist.get((today - dt.timedelta(days=7)).isoformat())
    left = day_left(now)
    n_today = len(p["today_all"])
    after_hours = left <= 0
    streak, best_streak = streaks(cl, today)
    best_day = max(cl.values(), default=0)
    W2 = W - 2 * M
    GAP = 10
    PAD = 12
    T_CAP = f("semibold", 14)
    T_NOTE = f("semibold", 15)

    def card(x0, y0, x1, y1, title=None, note=None):
        d.rounded_rectangle((x0, y0, x1, y1), radius=8, outline=GREY_LINE, width=2)
        if title:
            d.text((x0 + PAD, y0 + 8), title, font=T_CAP, fill=DARK)
        if note:
            d.text((x1 - PAD - tw(d, note, T_NOTE), y0 + 7), note, font=T_NOTE, fill=BLACK)

    # ---------- headline (no card: it's the page's voice)
    if done_n >= 5:
        head = f"Strong day: {done_n} done."
    elif done_n:
        head = f"{done_n} done today. Keep the momentum."
    else:
        head = "Fresh start: one small win gets you going."
    nxt = sorted([i for i in p["today_all"] if i["due"][1] and i["due"][1] > now.time()], key=lambda i: i["due"][1])
    if after_hours:
        sub = "The day is over. Rest; tomorrow is planned."
    elif nxt:
        sub = f"{n_today} task{'s' if n_today != 1 else ''} today · next fixed: {R.clock(nxt[0]['due'][1])} {nxt[0]['title']}"
    else:
        sub = f"{n_today} task{'s' if n_today != 1 else ''} today · {hm(left)} of day left"
    y = TOP_Y - 2
    hf = f("bold", 23)
    if tw(d, head, hf) > W2:
        hf = row_font(d, head, W2, bold=True)
    d.text((M, y), head, font=hf, fill=BLACK)
    d.text((M, y + 30), sub, font=row_font(d, sub, W2) if tw(d, sub, T_META) > W2 else T_META, fill=DARK)
    y += 58

    # ---------- 4 stat cards
    sh = 64
    cw = (W2 - 3 * GAP) / 4
    T_NUM = f("bold", 28)
    n1 = ("best day!" if done_n and done_n >= best_day else
          f"+{done_n - yday_done} vs yday" if yday_done is not None and done_n > yday_done else "")
    n2 = ("best ever!" if streak and streak >= best_streak else
          "keep it today" if streak and done_n == 0 else
          f"best {best_streak}" if streak else "start one")
    n3 = f"▲ {week_total - prev_week}" if prev_week and week_total > prev_week else ""
    n4 = ""
    if week_ago is not None and week_ago != late:
        n4 = f"▼ {week_ago - late}" if late < week_ago else f"▲ {late - week_ago}"
    stats = [("DONE TODAY", str(done_n), n1), ("STREAK", f"{streak}d", n2),
             ("THIS WEEK", str(week_total), n3), ("LATE", str(late), n4)]
    for k, (lab, num, note) in enumerate(stats):
        x0 = M + k * (cw + GAP)
        card(x0, y, x0 + cw, y + sh, lab)
        d.text((x0 + PAD, y + 27), num, font=T_NUM, fill=BLACK)
        if note and x0 + PAD + tw(d, num, T_NUM) + 8 + tw(d, note, T_NOTE) <= x0 + cw - 6:
            d.text((x0 + PAD + tw(d, num, T_NUM) + 8, y + 39), note, font=T_NOTE, fill=BLACK)
    y += sh + GAP

    # ---------- 2 chart cards
    chh = 122
    hw = (W2 - GAP) / 2
    days7 = [today - dt.timedelta(days=6 - k) for k in range(7)]

    def chart(x0, title, note, vals):
        card(x0, y, x0 + hw, y + chh, title, note)
        known = [v for v in vals if v is not None]
        mx = max(known + [1])
        inner = hw - 2 * PAD
        bw = inner / 7
        base = y + chh - 24
        for k, v in enumerate(vals):
            bx = x0 + PAD + k * bw
            lab = days7[k].strftime("%a")[:2]
            today_bar = k == 6
            d.text((bx + bw / 2 - tw(d, lab, f("semibold", 13)) / 2, base + 4), lab,
                   font=f("semibold", 13), fill=BLACK if today_bar else DARK)
            if v is None:                      # before tracking started: no bar, no number
                continue
            h = (chh - 66) * v / mx if v else 2
            d.rectangle((bx + 7, base - h, bx + bw - 7, base), fill=BLACK if today_bar else LIGHT)
            s_ = str(v)
            d.text((bx + bw / 2 - tw(d, s_, T_NOTE) / 2, base - h - 19), s_, font=T_NOTE, fill=BLACK)

    wins = [cl.get(d0.isoformat(), 0) if d0 >= first_day else None for d0 in days7]
    chart(M, "WINS · LAST 7 DAYS", f"{sum(v or 0 for v in wins)} done", wins)
    lv = [hist.get(d0.isoformat()) for d0 in days7]
    lv[-1] = late
    known = [v for v in lv if v is not None]
    trend = ""
    if len(known) >= 2 and known[-1] != known[0]:
        trend = f"▼ {known[0] - known[-1]}" if known[-1] < known[0] else f"▲ {known[-1] - known[0]}"
    if late == 0:
        trend = "clean slate!"
    chart(M + hw + GAP, "LATE PILE · LAST 7 DAYS", trend, lv)
    y += chh + GAP

    # ---------- bottom cards: today's wins | your lists
    bar_h = 42
    bar_y = BOTTOM - bar_h
    bh = bar_y - GAP - y
    done_titles = [t for t in tr.get("done", {}).get(today.isoformat(), []) if t][::-1]
    card(M, y, M + hw, y + bh, "TODAY'S WINS", str(len(done_titles)) if done_titles else None)
    yy = y + 32
    rows = max(0, int((y + bh - 8 - yy) // 28))
    if done_titles:
        show = done_titles[:rows] if len(done_titles) <= rows else done_titles[:max(0, rows - 1)]
        for t in show:
            x0 = M + PAD
            d.line((x0 + 1, yy + 12, x0 + 6, yy + 18), fill=BLACK, width=3)
            d.line((x0 + 6, yy + 18, x0 + 14, yy + 6), fill=BLACK, width=3)
            ft = row_font(d, t, hw - 2 * PAD - 22)
            if ft.size > 18:
                ft = f("semibold", 18)
            d.text((x0 + 22, yy + 1 + (18 - ft.size) // 2), t, font=ft, fill=BLACK)
            yy += 28
        if len(done_titles) > len(show):
            d.text((M + PAD + 22, yy), f"+ {len(done_titles) - len(show)} more", font=T_NOTE, fill=DARK)
    else:
        d.text((M + PAD, yy + 2), "Nothing ticked off yet.", font=f("semibold", 18), fill=BLACK)
        d.text((M + PAD, yy + 28), "Your first win shows up here.", font=T_NOTE, fill=DARK)

    rx = M + hw + GAP
    card(rx, y, rx + hw, y + bh, "YOUR LISTS", "■ = late")
    yy = y + 32
    mx = max([len(items) for _, items in lists] + [1])
    name_w = max([tw(d, n, f("semibold", 17)) for n, _ in lists] + [50]) + 12
    for name, items in lists:
        if yy + 24 > y + bh - 6:
            break
        lt = sum(1 for i in items if i["due"] and i["due"][0] < today)
        x0 = rx + PAD
        d.text((x0, yy), name, font=f("semibold", 17), fill=BLACK)
        cnt = f"{len(items)}" + (f" · {lt} late" if lt else "")
        cnt_w = tw(d, cnt, T_NOTE)
        bx0, bx1 = x0 + name_w, rx + hw - PAD - cnt_w - 10
        full = (bx1 - bx0) * len(items) / mx
        if items:
            d.rectangle((bx0, yy + 6, bx0 + full, yy + 17), fill=LIGHT)
            if lt:
                d.rectangle((bx0, yy + 6, bx0 + full * lt / len(items), yy + 17), fill=BLACK)
        d.text((rx + hw - PAD - cnt_w, yy + 2), cnt, font=T_NOTE, fill=BLACK if lt else DARK)
        yy += 28

    # ---------- the one next move
    wc = week_counts(p)
    move_lbl, move = next_move(p, tr, wc, after_hours)
    d.rounded_rectangle((M, bar_y, W - M, bar_y + bar_h), radius=8, fill=BLACK)
    d.text((M + 14, bar_y + 13), move_lbl, font=T_LABEL, fill=LIGHT)
    tx = M + 26 + tw(d, move_lbl, T_LABEL)
    ft = f("bold", 21)
    for s_ in (21, 20, 19, 18, 17, 16, 15, 14):
        ft = f("bold", s_)
        if tw(d, move, ft) <= W - M - 14 - tx:
            break
    d.text((tx, bar_y + 9 + (21 - ft.size) // 2), move, font=ft, fill=WHITE)
    book.add(img, "Insights")


def streaks(cleared, today):
    """(current, best) runs of days with at least one task done. Today only breaks the
    run once it's over, so a 0 this morning still shows yesterday's streak."""
    days = sorted(dt.date.fromisoformat(k) for k, v in cleared.items() if v > 0)
    best, run, prev = 0, 0, None
    for d0 in days:
        run = run + 1 if prev and (d0 - prev).days == 1 else 1
        best, prev = max(best, run), d0
    cur, d0 = 0, today if cleared.get(today.isoformat(), 0) > 0 else today - dt.timedelta(days=1)
    while cleared.get(d0.isoformat(), 0) > 0:
        cur += 1
        d0 -= dt.timedelta(days=1)
    return cur, max(best, cur)


def next_move(p, tr, wc, after_hours):
    """One concrete suggestion, picked from the insights (ADHD: insight -> one action)."""
    today = p["today"]
    if after_hours:
        tomorrow = today + dt.timedelta(days=1)
        first = sorted([i for i in p["all"] if i["due"] and i["due"][0] == tomorrow],
                       key=lambda i: (-i["score"], R.when(i)[1]))
        if first:
            return "TOMORROW, START WITH", first[0]["title"]
        return "NEXT BEST MOVE", "Rest. Tomorrow, pick one task from Missed."
    pool = [i for i in p["all"] + p.get("extra", []) if i["due"] and i["due"][0] < today]
    avoided = sorted([i for i in pool if i.get("moves", 0) >= 2 or (today - i["due"][0]).days > 30],
                     key=lambda i: -(i.get("moves", 0) * 30 + (today - i["due"][0]).days))
    if avoided:
        return "5 MINUTES ON", avoided[0]["title"]
    if len(wc) > 2:
        heavy = max(wc[1:], key=lambda x: x[1])
        light = min(wc[1:], key=lambda x: x[1])
        if heavy[1] - light[1] >= 3:
            return "NEXT BEST MOVE", (f"{heavy[0]:%a} has {heavy[1]} tasks, {light[0]:%a} has {light[1]}: "
                                      f"move one to {light[0]:%a}")
    if p["missed"]:
        return "CLEAR ONE LATE TASK", p["missed"][0]["title"]
    if p["top3"]:
        return "NEXT BEST MOVE", p["top3"][0]["title"]
    return "NEXT BEST MOVE", "You're on track. Pick one from Energy."


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
    d.line((M, 56, W - M, 56), fill=BLACK, width=2)
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
