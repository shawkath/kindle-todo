# Kindle TickTick dashboard (free)

Shows your open TickTick tasks on a jailbroken Kindle running KOReader +
the TRMNL plugin, with no TRMNL licence. GitHub Actions renders a 600×800
image every 30 minutes and publishes it on GitHub Pages; the Kindle polls it.

```
TickTick API → GitHub Actions (render.py) → GitHub Pages → Kindle (TRMNL plugin)
```

## What the screen shows (focus layout)
1. **DO FIRST** (one-line black bar at the top): one task picked for you. A missed high/medium-priority task
   wins; otherwise the most important task due today.
2. **MISSED**: overdue tasks are never hidden. They're shown with how many days
   late, most important first, then oldest. They take up to ~40% of the screen;
   the rest is summarised as "+ N more missed".
3. **THIS WEEK**: everything else due from today to Sunday, grouped by day and
   sorted by time. High-priority tasks are bold.
4. **Footer**: how many tasks are parked for later or have no date, so nothing
   silently disappears.

Tip: to push something into DO FIRST, set it to **High** priority in TickTick.

## Setup

### 1. TickTick app
1. Go to https://developer.ticktick.com/manage → **+ App Name** → create an app.
2. Set **OAuth redirect URL** to exactly `http://localhost` and save.
3. Note the **Client ID** and **Client Secret**.

### 2. Get your token (Windows PowerShell)
```
powershell -ExecutionPolicy Bypass -File .\get-token.ps1
```
It prints `TICKTICK_TOKEN` and a random `DASH_PATH`. Keep both.

### 3. GitHub repo
1. Create a new **public** repo, e.g. `kindle-todo`.
2. **Add file → Upload files** → drag in everything from this folder
   (including the `.github` folder) → Commit.
3. **Settings → Secrets and variables → Actions → New repository secret**:
   - `TICKTICK_TOKEN` = the token
   - `DASH_PATH` = the random string
4. **Settings → Pages → Build and deployment → Source: GitHub Actions**.
5. **Actions** tab → enable workflows if asked → **Update Kindle dashboard → Run workflow**.
6. When it's green, open
   `https://<your-username>.github.io/kindle-todo/<DASH_PATH>/api/display`
   — you should see a small JSON with an `image_url`. Open that URL to see the screen.

Optional repository **variables** (same page, "Variables" tab):
- `LISTS` — only show these lists, comma-separated, e.g. `Inbox,Work`
- `TZ_NAME` — defaults to `Asia/Kolkata`
- `WEEK` — `calendar` (today → Sunday, default) or `rolling` (next 7 days, handy on weekends)

### 4. Kindle (KOReader → TRMNL plugin)
1. Put `trmnl.koplugin` from https://github.com/usetrmnl/trmnl-koreader into
   `koreader/plugins/` (close KOReader before connecting USB).
2. In KOReader: **Tools → TRMNL Display → Configure TRMNL**
   - **API Key**: anything, e.g. `free`
   - **Base URL**: `https://<your-username>.github.io/kindle-todo/<DASH_PATH>`
3. Turn on Wi-Fi → **Fetch screen now**.
4. Dashboard mode: **Tools → More tools → Keep alive** on,
   **Settings → Device → Auto suspend timeout** off,
   then **TRMNL Display → Enable auto-refresh**. Keep it plugged in.

## Maintenance
- **"Reconnect TickTick" on the Kindle** → token expired. Run `get-token.ps1`
  again and update the `TICKTICK_TOKEN` secret.
- The `keepalive` workflow makes one empty commit a month so GitHub doesn't
  pause the schedule.

## Privacy
GitHub Pages is public. Your screen lives at a long random URL (`DASH_PATH`)
that isn't linked anywhere, so it's unlisted, not private. Anyone who has
the exact link can see the image. Don't put anything on the list you'd mind
leaking.

## Preview locally
```
pip install pillow
python render.py --demo     # writes preview.png with sample tasks
```

Fonts: Inter (SIL OFL 1.1), see `fonts/LICENSE-Inter.txt`.
