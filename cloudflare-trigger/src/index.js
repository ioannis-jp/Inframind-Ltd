/**
 * Cloudflare Worker — MOTION refresh trigger
 *
 * Fires on a reliable cron schedule and calls the GitHub workflow_dispatch API
 * to trigger the existing Python script in the Inframind-Ltd repo. This sidesteps
 * GitHub's unreliable scheduled-workflow cron, while keeping all data-processing
 * logic in the existing repo (no JS port required).
 *
 * Cost-control architecture (effective 2026-06-02):
 *   • Cron fires every 5 min for data freshness.
 *   • By default the workflow commits with [skip ci] → Netlify does NOT rebuild.
 *   • At each 6-hour boundary (00 / 06 / 12 / 18 UTC) the Worker passes
 *     deploy=true → the workflow OMITS [skip ci] → Netlify rebuilds.
 *   • Result: max 4 Netlify production deploys / day = 60 credits/day cap.
 *
 * Secrets (set via `wrangler secret put`):
 *   GITHUB_PAT       — GitHub Personal Access Token with `workflow` scope
 *   TRIGGER_KEY      — (optional) query-param key for the manual /trigger endpoint
 *
 * Vars (set in wrangler.toml [vars]):
 *   GITHUB_OWNER     — repository owner (e.g. "ioannis-jp")
 *   GITHUB_REPO      — repository name (e.g. "Inframind-Ltd")
 *   WORKFLOW_FILE    — workflow filename (e.g. "refresh-motion.yml")
 *   GITHUB_REF       — branch to run on (default: "main")
 */

// Hours (UTC) at which a deploy-trigger commit is produced. 4×/day = 6h cadence.
const DEPLOY_HOURS_UTC = [0, 6, 12, 18];

function shouldDeployNow(now) {
  // True only if we're within the first cron window of a deploy hour.
  // Cron fires every 5 min → the first firing of an hour has minute < 5.
  return DEPLOY_HOURS_UTC.includes(now.getUTCHours()) && now.getUTCMinutes() < 5;
}

async function dispatchWorkflow(env, deploy) {
  const owner    = env.GITHUB_OWNER;
  const repo     = env.GITHUB_REPO;
  const workflow = env.WORKFLOW_FILE;
  const ref      = env.GITHUB_REF || "main";

  if (!owner || !repo || !workflow) {
    throw new Error("Missing required vars: GITHUB_OWNER / GITHUB_REPO / WORKFLOW_FILE");
  }
  if (!env.GITHUB_PAT) {
    throw new Error("Missing GITHUB_PAT secret — run `wrangler secret put GITHUB_PAT`");
  }

  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
  const body = {
    ref,
    inputs: { deploy: deploy ? "true" : "false" },
  };

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${env.GITHUB_PAT}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "inframind-motion-trigger/1.1",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (response.status === 204) {
    return { ok: true, status: 204 };
  }
  const text = await response.text();
  return { ok: false, status: response.status, body: text };
}

export default {
  async scheduled(event, env, ctx) {
    const now = new Date();
    const startedAt = now.toISOString();
    const deploy = shouldDeployNow(now);
    try {
      const result = await dispatchWorkflow(env, deploy);
      if (result.ok) {
        console.log(
          `[${startedAt}] dispatched (cron=${event.cron}, deploy=${deploy}) ` +
          `→ ${env.GITHUB_OWNER}/${env.GITHUB_REPO}@${env.GITHUB_REF || "main"}`
        );
      } else {
        console.error(
          `[${startedAt}] dispatch failed: HTTP ${result.status} — ${result.body}`
        );
      }
    } catch (err) {
      console.error(`[${startedAt}] dispatch threw: ${err.message}`);
    }
  },

  // Manual / health-check endpoint.
  //   GET /trigger              → fire with deploy=false (data only)
  //   GET /trigger?deploy=true  → fire with deploy=true  (rebuild Netlify)
  //   GET /                     → info text
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/trigger") {
      if (env.TRIGGER_KEY && url.searchParams.get("key") !== env.TRIGGER_KEY) {
        return new Response("unauthorized", { status: 401 });
      }
      const deploy = url.searchParams.get("deploy") === "true";
      try {
        const result = await dispatchWorkflow(env, deploy);
        if (result.ok) {
          return new Response(`triggered (deploy=${deploy})\n`, { status: 200 });
        }
        return new Response(
          `dispatch failed: HTTP ${result.status} — ${result.body}\n`,
          { status: 502 }
        );
      } catch (err) {
        return new Response(`dispatch threw: ${err.message}\n`, { status: 500 });
      }
    }
    return new Response(
      "Inframind MOTION trigger Worker — runs on cron, no public endpoints.\n" +
      "GET /trigger              → data-only (no Netlify rebuild)\n" +
      "GET /trigger?deploy=true  → force Netlify rebuild\n",
      { status: 200, headers: { "Content-Type": "text/plain" } }
    );
  },
};
