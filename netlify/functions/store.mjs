// Append-only audit store for the GAP proposal gate.
// Lives in a serverless function because Netlify injects NETLIFY_BLOBS_CONTEXT
// here but NOT in edge functions. No npm dependencies.

const STORE = "gap-access";
const KEY = "log";
const MAX = 800;

function blobCtx() {
  const raw = process.env.NETLIFY_BLOBS_CONTEXT;
  if (!raw) {
    const keys = Object.keys(process.env).filter((k) => /BLOB|NETLIFY|SITE|DEPLOY/i.test(k));
    console.log("STORE_NOCTX keys=" + JSON.stringify(keys));
    return null;
  }
  try {
    return JSON.parse(Buffer.from(raw, "base64").toString("utf8"));
  } catch (e) {
    console.log("STORE_PARSE " + String(e));
    return null;
  }
}

function blobUrl(c) {
  const base = c.uncachedEdgeURL || c.edgeURL || c.apiURL;
  if (!base || !c.siteID) return null;
  return `${base}/${c.siteID}/site:${STORE}/${KEY}`;
}

async function readLog() {
  const c = blobCtx();
  if (!c) return null;
  const u = blobUrl(c);
  if (!u) return null;
  const r = await fetch(u, { headers: { authorization: `Bearer ${c.token}` } });
  if (r.status === 404) return [];
  if (!r.ok) throw new Error("blob get " + r.status);
  return await r.json();
}

async function writeLog(log) {
  const c = blobCtx();
  if (!c) return false;
  const u = blobUrl(c);
  if (!u) return false;
  const r = await fetch(u, {
    method: "PUT",
    headers: { authorization: `Bearer ${c.token}`, "content-type": "application/json" },
    body: JSON.stringify(log),
  });
  if (!r.ok) throw new Error("blob put " + r.status);
  return true;
}

export default async (req) => {
  const secret = process.env.GAP_SECRET;
  if (!secret || req.headers.get("x-gap-key") !== secret) {
    return new Response("Not Found", { status: 404 });
  }

  try {
    if (req.method === "DELETE") {
      const ok = await writeLog([]);
      return Response.json({ ok, cleared: true });
    }
    if (req.method === "POST") {
      const rec = await req.json();
      const log = (await readLog()) || [];
      log.push(rec);
      const ok = await writeLog(log.slice(-MAX));
      return Response.json({ ok, count: log.length });
    }
    const log = (await readLog()) || [];
    return Response.json(log);
  } catch (e) {
    console.log("STORE_FAIL " + String(e));
    return new Response(String(e), { status: 500 });
  }
};

export const config = { path: "/_im/store" };
