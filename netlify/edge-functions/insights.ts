// Private read-only dashboard over the audit store.
// URL: /_im/insights?k=<admin key>   (404 without the key)

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

const enc = new TextEncoder();
const hex = (b: ArrayBuffer) =>
  [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
const sha256 = async (s: string) =>
  hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));

const esc = (s: unknown) =>
  String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));

const LABEL: Record<string, string> = {
  unlock: "Είσοδος με κωδικό",
  page_view: "Άνοιγμα σελίδας πρότασης",
  document_open: "Άνοιγμα / λήψη εγγράφου",
  document_click: "Κλικ σε έγγραφο",
  engagement: "Χρόνος στη σελίδα πρότασης",
  site_view: "Επίσκεψη στο inframind.eu",
  site_time: "Χρόνος στο inframind.eu",
  locked_hit: "Προσπάθεια χωρίς σύνδεση",
  bad_password: "Λανθασμένος κωδικός",
};

function fmt(ts: string) {
  try {
    return new Date(ts).toLocaleString("el-GR", {
      timeZone: "Asia/Nicosia", day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return ts; }
}

export default async (request: Request) => {
  const url = new URL(request.url);
  const adminHash = Netlify.env.get("GAP_ADMIN_HASH");
  const key = url.searchParams.get("k") || "";
  if (!adminHash || !key || (await sha256(key)) !== adminHash) {
    return new Response("Not Found", { status: 404 });
  }

  let items: any[] = [];
  let storeErr = "";
  try {
    items = (await blobGet("gap-access", "log")) || [];
    items = items.filter(Boolean).sort((a: any, b: any) => (a.ts < b.ts ? 1 : -1));
  } catch (e) {
    storeErr = String(e);
  }

  const count = (t: string) => items.filter((i) => i.type === t).length;
  const docs = items.filter((i) => i.type === "document_open");
  const byDoc: Record<string, number> = {};
  for (const d of docs) {
    const n = String(d.path || "").split("/").pop() || "?";
    byDoc[n] = (byDoc[n] || 0) + 1;
  }
  const propSeconds = items
    .filter((i) => i.type === "engagement")
    .reduce((s, i) => s + (+i.seconds || 0), 0);
  const siteSeconds = items
    .filter((i) => i.type === "site_time")
    .reduce((s, i) => s + (+i.seconds || 0), 0);
  const siteViews = count("site_view");
  const firstUnlock = [...items].reverse().find((i) => i.type === "unlock");
  const mins = (s: number) => (s < 60 ? `${Math.round(s)} δευτ.` : `${Math.round(s / 60)} λεπτά`);

  const tile = (l: string, v: string, s = "") =>
    `<div class="tile"><span class="tl">${esc(l)}</span><strong>${esc(v)}</strong>${s ? `<span class="ts">${esc(s)}</span>` : ""}</div>`;

  const rows = items.slice(0, 400).map((i) =>
    `<tr><td class="mono">${esc(fmt(i.ts))}</td><td>${esc(LABEL[i.type] || i.type)}</td>` +
    `<td class="mono">${esc((i.path || "").split("/").pop() || i.detail || "")}` +
    `${i.seconds ? ` · ${esc(mins(+i.seconds))}` : ""}</td>` +
    `<td class="dim">${esc(i.geo || "")}</td><td class="dim mono">${esc(i.ip || "")}</td></tr>`
  ).join("");

  const html = `<!DOCTYPE html><html lang="el"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Access insights · GAP</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#fafafa;color:#1a1a1a;font-size:15px;line-height:1.6;padding:48px 6%}
.label{font-size:10px;letter-spacing:.25em;text-transform:uppercase;color:#6b6b6b;font-weight:600;display:block;margin-bottom:14px}
h1{font-size:clamp(26px,3.4vw,40px);font-weight:800;letter-spacing:-.03em;margin-bottom:36px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:0;border:1px solid #e0e0e0;background:#fff;margin-bottom:40px}
.tile{padding:24px 22px;border-right:1px solid #e0e0e0}
.tile:last-child{border-right:none}
.tl{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:#6b6b6b;font-weight:600;display:block;margin-bottom:10px}
.tile strong{font-size:30px;font-weight:800;letter-spacing:-.02em;display:block}
.ts{font-size:12.5px;color:#6b6b6b;font-weight:300}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e0e0e0;font-size:13.5px}
th{text-align:left;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:#6b6b6b;font-weight:600;padding:14px 16px;border-bottom:1px solid #e0e0e0}
td{padding:12px 16px;border-bottom:1px solid #f0f0f0;vertical-align:top}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.dim{color:#8a8a8a;font-weight:300}
.err{background:#fff4f4;border:1px solid #e8c9c9;padding:14px 16px;margin-bottom:24px;font-size:13px;color:#8a2b2b}
@media(max-width:760px){.tile{border-right:none;border-bottom:1px solid #e0e0e0}body{padding:32px 5%}}
</style></head><body>
<span class="label">INFRAMIND · Ιδιωτικό</span>
<h1>G.A.P. Vassilopoulos<br>Καταγραφή πρόσβασης</h1>
${storeErr ? `<div class="err">Το μόνιμο αρχείο δεν απάντησε (${esc(storeErr)}). Τα συμβάντα παραμένουν στα Netlify function logs.</div>` : ""}
<div class="tiles">
${tile("Είσοδοι με κωδικό", String(count("unlock")), firstUnlock ? "1η: " + fmt(firstUnlock.ts) : "καμία ακόμη")}
${tile("Ανοίγματα εγγράφων", String(docs.length), Object.entries(byDoc).map(([k, v]) => `${k}: ${v}`).join(" · "))}
${tile("Χρόνος στην πρόταση", propSeconds ? mins(propSeconds) : "—")}
${tile("Επισκέψεις inframind.eu", String(siteViews), siteSeconds ? mins(siteSeconds) : "")}
${tile("Αποτυχημένοι κωδικοί", String(count("bad_password")))}
</div>
<table><thead><tr><th>Ημερομηνία / ώρα (Κύπρος)</th><th>Συμβάν</th><th>Λεπτομέρεια</th><th>Τοποθεσία</th><th>IP</th></tr></thead>
<tbody>${rows || `<tr><td colspan="5" class="dim">Καμία καταγραφή ακόμη.</td></tr>`}</tbody></table>
</body></html>`;

  return new Response(html, {
    status: 200,
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
  });
};

export const config = { path: "/_im/insights" };
