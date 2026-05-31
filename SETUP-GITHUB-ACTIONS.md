# Automated MOTION data refresh — one-time setup

This sets up a free GitHub Actions cron that refreshes the MOTION data on the
live site every 6 hours (4x/day) — no Mac dependency, no manual work.

Once configured:
- GitHub Actions fetches the MOTION GTFS-RT feed every 6 hours
- Updates `data/motion-stats.json` (Hero B — last 6h executed)
- Regenerates `data/planned.json` daily (Hero A — today's plan)
- Commits the new JSON to the repo
- Netlify auto-deploys on the commit
- Live site shows fresh numbers automatically

## One-time setup (~15 minutes)

### 1. Create a GitHub repo for the website

Go to https://github.com/new and create a repo (e.g. `inframind-website`).
Keep it **private** if you prefer; Actions still works on the free tier.

### 2. Push the `website/` folder to it

On your Mac, from this `website/` directory:

```bash
cd "/Users/johnpanagiotidis/Documents/Claude/Projects/Inframind Inside job/website"
git init -b main
git add .
git commit -m "Initial commit — inframind.eu site"
git remote add origin https://github.com/<YOUR-USERNAME>/inframind-website.git
git push -u origin main
```

Replace `<YOUR-USERNAME>` with your GitHub username.

### 3. Connect Netlify to the repo

1. Open https://app.netlify.com/projects/inframind-eu → **Project settings → Build & deploy → Continuous deployment → Link repository**
2. Pick GitHub → authorize → select your `inframind-website` repo → branch `main`
3. **Publish directory:** `.` (root) — there is no build step
4. **Build command:** leave blank
5. Save — every commit to `main` will now auto-deploy

This **replaces** the drag-and-drop workflow. From now on Netlify deploys from git.

### 4. Verify Actions is enabled

In the repo on GitHub → **Actions** tab. You should see the workflow
"Refresh MOTION data" listed. Click **Run workflow** to test it once manually.

After ~2 min, check:
- The Actions run completes green
- New commit "chore(data): refresh MOTION stats" appeared
- Netlify shows a fresh deploy
- https://inframind.eu Hero B numbers update

### 5. Schedule

The workflow runs automatically at `03:00, 09:00, 15:00, 21:00 UTC`
(= `06:00, 12:00, 18:00, 00:00` Cyprus time).

Each run:
- Fetches a fresh GTFS-RT snapshot
- Adds it to the 6h rolling window in `data/_rolling_snapshots.json`
- Computes the union → writes `data/motion-stats.json`
- If date changed → regenerates `data/planned.json` from GTFS static

## Troubleshooting

**Action fails with connection refused / timeout:**
The MOTION endpoint (`20.19.98.194:8328`) may be restricted from public cloud
IPs. If GitHub's runners cannot reach it, options are:
1. Run the script on a small VPS instead (Hetzner / DigitalOcean ~ €4/mo)
2. Keep the Mac launcher as the source, push from Mac to repo manually
3. Set `MOTION_FEED_URL` secret in GitHub repo settings to point to an
   alternate proxy you control

**Hero shows old numbers:**
Hard-refresh the page (`⌘ Shift R`) — the JSON files have `cache: no-store`,
but the HTML is cached by Netlify CDN ~5 minutes after each deploy.

**Want different refresh times:**
Edit the `cron:` line in `.github/workflows/refresh-motion.yml`. Cron uses UTC.

## Files in this setup

| File | Purpose |
|---|---|
| `.github/workflows/refresh-motion.yml` | The cron workflow |
| `scripts/refresh_motion_data.py` | Python: fetches + parses + writes JSON |
| `data/motion-stats.json` | Hero B data (auto-generated) |
| `data/planned.json` | Hero A data (auto-generated) |
| `data/_rolling_snapshots.json` | Internal 6h buffer (auto-generated) |
| `gtfs/*.zip` | GTFS static feeds, source of `planned.json` (commit these) |

**Important:** Keep the `gtfs/` zip files committed to the repo so GitHub
Actions can generate `planned.json`. They are ~few MB total.
