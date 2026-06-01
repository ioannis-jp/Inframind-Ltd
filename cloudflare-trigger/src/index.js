/**
 * Cloudflare Worker — MOTION refresh trigger
 *
 * Fires on a reliable cron schedule and calls the GitHub workflow_dispatch API
 * to trigger the existing Python script in the Inframind-Ltd repo. This sidesteps
 * GitHub's unreliable scheduled-workflow cron, while keeping all data-processing
 * logic in the existing repo (no JS port required).
 *
 * Secrets (set via `wrangler secret put`):
 *   GITHUB_PAT       — GitHub Personal Access Token with `workflow` scope
 *
 * Vars (set in wrangler.toml [vars]):
 *   GITHUB_OWNER     — repository owner (e.g. "ioannis-jp")
 *   GITHUB_REPO      — repository name (e.g. "Inframind-Ltd")
 *   WORKFLOW_FILE    — workflow filename (e.g. "refresh-motion.yml")
 *   GITHUB_REF       — branch to run on (default: "main")
 */

export default {
  async scheduled(event, env, ctx) {
    const owner    = env.GITHUB_OWNER;
    const repo     = env.GITHUB_REPO;
    const workflow = env.WORKFLOW_FILE;
    const ref      = env.GITHUB_REF || "main";

    if (!owner || !repo || !workflow) {
      console.error("Missing required vars: GITHUB_OWNER / GITHUB_REPO / WORKFLOW_FILE");
      return;
    }
    if (!env.GITHUB_PAT) {
      console.error("Missing GITHUB_PAT secret — run `wrangler secret put GITHUB_PAT`");
      return;
    }

    const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
    const startedAt = new Date().toISOString();

    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${env.GITHUB_PAT}`,
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "inframind-motion-trigger/1.0",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref }),
      });

      if (response.status === 204) {
        console.log(`[${startedAt}] dispatched ${workflow} on ${owner}/${repo}@${ref} (cron=${event.cron})`);
        return;
      }

      const text = await response.text();
      console.error(`[${startedAt}] dispatch failed: HTTP ${response.status} — ${text}`);
    } catch (err) {
      console.error(`[${startedAt}] dispatch threw: ${err.message}`);
    }
  },

  // Optional: HTTP endpoint for manual testing / health check.
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/trigger") {
      // Manual trigger via HTTPS GET (requires ?key=<env.TRIGGER_KEY> if set)
      if (env.TRIGGER_KEY && url.searchParams.get("key") !== env.TRIGGER_KEY) {
        return new Response("unauthorized", { status: 401 });
      }
      await this.scheduled({ cron: "manual" }, env, ctx);
      return new Response("triggered\n", { status: 200 });
    }
    return new Response(
      "Inframind MOTION trigger Worker — runs on cron, no public endpoints.\n",
      { status: 200, headers: { "Content-Type": "text/plain" } }
    );
  },
};
