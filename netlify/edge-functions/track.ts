// First-party engagement beacon. No third-party services, no ad tech.
// Accepts small JSON events from pages on inframind.eu and records them
// in the same audit store used by the access gate.

// ── Audit store. The Blobs context is injected only into serverless
// functions, so the edge functions talk to /_im/store instead. Shared
// secret in the header; every call is best-effort.
const STORE_URL = "https://inframind.eu/_im/store";
async function storeAppend(rec: unknown): Promise<void> {
  const key = Netlify.env.get("GAP_SECRET") || "";
  const r = await fetch(STORE_URL, {
    method: "POST",
    headers: { "content-type": "application/json", "x-gap-key": key },
    body: JSON.stringify(rec),
  });
  if (!r.ok) throw new Error("store append " + r.status);
}
async function storeRead(): Promise<any[]> {
  const key = Netlify.env.get("GAP_SECRET") || "";
  const r = await fetch(STORE_URL, { headers: { "x-gap-key": key } });
  if (!r.ok) throw new Error("store read " + r.status);
  const j = await r.json();
  return Array.isArray(j) ? j : [];
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
    await storeAppend(rec);
  } catch (e) {
    console.log("GAPLOG_BLOB_FAIL " + String(e));
  }

  return new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
};

export const config = { path: "/_im/t" };
