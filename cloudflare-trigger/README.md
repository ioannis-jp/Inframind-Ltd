# Inframind MOTION Trigger — Cloudflare Worker (Path A)

Cloudflare Worker that fires every 5 minutes during Cyprus operating hours and
calls the GitHub `workflow_dispatch` API to trigger the existing
`refresh-motion.yml` workflow in `ioannis-jp/Inframind-Ltd`.

**Why:** GitHub's scheduled-workflow cron is unreliable (only fires 2–4 times
per day at high frequencies even when configured for every 10 minutes). Manual
dispatch via the API is reliable, and Cloudflare's cron triggers fire on the
exact minute, every time.

## One-time setup

You need: macOS Terminal, an Apple/Google email for Cloudflare signup, and a
GitHub Personal Access Token.

### 1. Cloudflare account

1. Go to https://dash.cloudflare.com/sign-up — sign up with email (free).
2. Verify the email link.
3. No billing card needed for the free tier.

### 2. Install Wrangler CLI (Cloudflare's deploy tool)

```bash
# If you don't have Node.js: brew install node
npm install -g wrangler

# Verify
wrangler --version

# Log in (opens browser, authorizes wrangler against your Cloudflare account)
wrangler login
```

### 3. Create GitHub Personal Access Token

1. Go to https://github.com/settings/personal-access-tokens (fine-grained tokens).
2. **Generate new token** → settings:
   - Resource owner: `ioannis-jp`
   - Repository access: **Only select repositories** → `Inframind-Ltd`
   - Repository permissions → **Actions: Read and write**
   - Expiration: 1 year (or longer)
3. Copy the token (`github_pat_...`) immediately — shown only once.

### 4. Configure the Worker

```bash
cd '/Users/johnpanagiotidis/Documents/Claude/Projects/Inframind Inside job/website/cloudflare-trigger'

# Tell Wrangler about the GitHub token (one-time, stored encrypted in Cloudflare)
wrangler secret put GITHUB_PAT
# (paste the token when prompted, press Enter)

# Optional: set a key for the manual HTTPS /trigger endpoint
# wrangler secret put TRIGGER_KEY
```

### 5. Deploy

```bash
wrangler deploy
```

Output will include the Worker URL (e.g. `https://inframind-motion-trigger.<your-subdomain>.workers.dev`).

### 6. Verify

```bash
# Watch live logs (Ctrl-C to exit)
wrangler tail

# Or manually invoke the HTTPS endpoint
curl 'https://inframind-motion-trigger.<your-subdomain>.workers.dev/trigger'

# Check GitHub Actions tab — should see a new "Refresh MOTION data" run within seconds
```

## Day-to-day

- **View runs:** Cloudflare dash → Workers & Pages → `inframind-motion-trigger` → Logs.
- **Change cadence:** edit `[triggers].crons` in `wrangler.toml`, then `wrangler deploy`.
- **Rotate GitHub token:** `wrangler secret put GITHUB_PAT` again.

## What to monitor first 24h

- GitHub Actions tab should show ~216 runs/day during operating hours (instead
  of the 2–4 we were getting with GitHub's scheduled cron).
- `daily_history.json` and `daily_executed.json` (when km tracking is re-enabled
  in Path B) should populate reliably.

## Costs

Free tier covers our usage entirely:

| Resource | Free quota | Our use |
|---|---|---|
| Worker requests | 100,000/day | ~216/day |
| Cron triggers | unlimited | 1 worker |
| CPU time | 10ms/request | ~50ms (fits) |

**Total: €0/month.**

## Architecture notes

- The Worker holds NO state. Every invocation is independent.
- `GITHUB_PAT` is stored encrypted by Cloudflare; only the Worker can decrypt it
  at runtime. Never commit it to git.
- If we ever want to remove this layer, just `wrangler delete` and the GitHub
  Actions scheduled cron resumes (less reliable but functional baseline).
