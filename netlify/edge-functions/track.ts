// First-party engagement beacon. No third-party services, no ad tech.
// Accepts small JSON events from pages on inframind.eu and records them
// in the same audit store used by the access gate.

// ── Netlify Blobs over plain fetch (no npm import: the edge bundler
// cannot resolve npm modules). Every call is best-effort; failures are
// swallowed by the caller so access is never blocked by the log store.
function blobCtx(): any {
  const raw = Netlify.env.get("NETLIFY_BLOBS_CONTEXT");
  if (!raw) return null;
  try { return JSON.parse(atob(raw)); } catch { return null; }
}
function blobUrl(c: any, store: string, key: string) {
  const base = c.uncachedEdgeURL || c.edgeURL;
  if (!base || !c.siteID) return null;
  return `${base}/${c.siteID}/site:${store}/${encodeURIComponent(key)}`;
}
async function blobGet(store: string, key: string): Promise<any> {
  const c = blobCtx(); if (!c) return null;
  const u = blobUrl(c, store, key); if (!u) return null;
  const r = await fetch(u, { headers: { authorization: `Bearer ${c.token}` } });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error("blob get " + r.status);
  return await r.json();
}
async function blobSet(store: string, key: string, value: unknown): Promise<void> {
  const c = blobCtx(); if (!c) return;
  const u = blobUrl(c, store, key); if (!u) return;
  const r = await fetch(u, {
    method: "PUT",
    headers: { authorization: `Bearer ${c.token}`, "content-type": "application/json" },
    body: JSON.stringify(value),
  });
  if (!r.ok) throw new Error("blob set " + r.status);
}

const ALLOWED = new Set(["engagement", "document_click", "site_view", "site_time"]);

export default async (request: Request, context: any) => {
  if (request.method !== "POST") return new Response(null, { status: 405 });

  let ev: any = {};
  try {
    const raw = await request.text();
    if (raw.length > 2000) return new Response(null, { status: 413 });
    ev = JSON.parse(raw);
  } catch {
    return new Response(null, { status: 400 });
  }

  const type = String(ev.type || "");
  if (!ALLOWED.has(type)) return new Response(null, { status: 400 });

  const m = (request.headers.get("cookie") || "").match(/(?:^|;\s*)im_v=([^;]*)/);
  const vid = m ? decodeURIComponent(m[1]).slice(0, 32) : "";
  if (!vid) return new Response(null, { status: 204 }); // unknown visitor → not recorded

  const rec = {
    ts: new Date().toISOString(),
    type,
    vid,
    path: String(ev.path || "").slice(0, 200),
    detail: String(ev.detail || "").slice(0, 200),
    seconds: Number.isFinite(+ev.seconds) ? Math.min(+ev.seconds, 86400) : 0,
    scroll: Number.isFinite(+ev.scroll) ? Math.round(+ev.scroll) : 0,
    ip: request.headers.get("x-nf-client-connection-ip") || "",
    ua: request.headers.get("user-agent") || "",
    geo: context?.geo
      ? [context.geo.city, context.geo.country?.code].filter(Boolean).join(", ")
      : "",
  };

  console.log("GAPLOG " + JSON.stringify(rec));
  try {
    const log = (await blobGet("gap-access", "log")) || [];
    log.push(rec);
    await blobSet("gap-access", "log", log.slice(-800));
  } catch (e) {
    console.log("GAPLOG_BLOB_FAIL " + String(e));
  }

  return new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
};

export const config = { path: "/_im/t" };
