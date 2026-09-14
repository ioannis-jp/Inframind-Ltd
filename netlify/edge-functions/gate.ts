// ─────────────────────────────────────────────────────────────
// INFRAMIND · server-side access gate for /p/gap-payments-2026/*
// Protects the HTML **and** the PDFs. No password, no bytes.
// Secrets live in Netlify env vars, never in the repo.
// ─────────────────────────────────────────────────────────────

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

const BASE = "/p/gap-payments-2026";
const SESSION_COOKIE = "gap_s";
const VISITOR_COOKIE = "im_v";
const TTL_DAYS = 30;
const VALID_UNTIL = Date.parse("2026-10-10T23:59:59+03:00");

const enc = new TextEncoder();
const hex = (b: ArrayBuffer) =>
  [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");

async function sha256(s: string) {
  return hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}

async function hmac(secret: string, msg: string) {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(msg)));
}

function cookie(req: Request, name: string) {
  const m = (req.headers.get("cookie") || "")
    .match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : null;
}

// ── audit trail ────────────────────────────────────────────────
async function audit(request: Request, context: any, ev: Record<string, unknown>) {
  const rec = {
    ts: new Date().toISOString(),
    ...ev,
    ip: request.headers.get("x-nf-client-connection-ip") || "",
    ua: request.headers.get("user-agent") || "",
    ref: request.headers.get("referer") || "",
    geo: context?.geo
      ? [context.geo.city, context.geo.country?.code].filter(Boolean).join(", ")
      : "",
  };
  // Always emit to the Netlify log stream (fallback record).
  console.log("GAPLOG " + JSON.stringify(rec));
  // Durable record in Netlify Blobs (best effort — never blocks access).
  try {
    await storeAppend(rec);
  } catch (e) {
    console.log("GAPLOG_BLOB_FAIL " + String(e));
  }
}

// ── email notification (reuses the site's existing Formspree endpoint).
// Deliberately minimal payload: event, document and time only — no IP,
// no user agent, no location leaves to the third party.
const FORMSPREE_URL = "https://formspree.io/f/mlgvlvnr";
const NETLIFY_FORM_URL = "https://inframind.eu/forms/notify.html";

async function notify(subject: string, body: string) {
  // Channel 1 — Netlify Forms: same provider as the hosting, recipient set
  // in the Netlify UI, submissions visible there.
  try {
    const r = await fetch(NETLIFY_FORM_URL, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        "form-name": "gap-notify",
        "bot-field": "",
        subject,
        message: body,
      }).toString(),
    });
    console.log("GAPLOG_NOTIFY_NETLIFY status=" + r.status);
  } catch (e) {
    console.log("GAPLOG_NOTIFY_NETLIFY_FAIL " + String(e));
  }

  // Channel 2 — Formspree, the endpoint the site already uses.
  try {
    const r = await fetch(FORMSPREE_URL, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ _subject: subject, message: body }),
    });
    console.log("GAPLOG_NOTIFY_FORMSPREE status=" + r.status);
  } catch (e) {
    console.log("GAPLOG_NOTIFY_FORMSPREE_FAIL " + String(e));
  }
}

function nowCy() {
  try {
    return new Date().toLocaleString("el-GR", {
      timeZone: "Asia/Nicosia", day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return new Date().toISOString(); }
}

// ── login screen ───────────────────────────────────────────────
function loginPage(next: string, failed: boolean, expired = false) {
  return `<!DOCTYPE html><html lang="el"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">
<meta name="referrer" content="no-referrer">
<title>G.A.P. Vassilopoulos &middot; Sliq</title>
<link rel="icon" type="image/png" href="/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--black:#0a0a0a;--dark:#1a1a1a;--grey:#6b6b6b;--white:#fff;--border:#e0e0e0}
body{font-family:'Inter',sans-serif;color:var(--dark);background:var(--white);font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased;overflow-x:hidden}
#gate{position:relative;min-height:100vh;display:flex;align-items:center;padding:0 8%;overflow:hidden}
#gate::before{content:'';position:absolute;inset:0;background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);background-size:80px 80px;opacity:.25;pointer-events:none;z-index:0}
.inner{position:relative;z-index:2;width:100%;max-width:520px}
.inner img{height:92px;width:auto;display:block;margin-bottom:56px}
.label{font-size:10px;letter-spacing:.25em;text-transform:uppercase;color:var(--grey);font-weight:600;display:block;margin-bottom:18px}
h1{font-size:clamp(32px,4.6vw,54px);font-weight:800;letter-spacing:-.03em;line-height:1.08;color:var(--black);margin-bottom:20px}
h1 .light{font-weight:300}
.lede{font-size:16px;font-weight:300;color:var(--grey);line-height:1.85;margin-bottom:44px;max-width:400px}
form{display:flex;gap:12px;max-width:440px}
input{flex:1;font-family:inherit;font-size:15px;padding:13px 16px;border:1px solid var(--border);background:var(--white);color:var(--dark);outline:none;transition:border-color .25s}
input:focus{border-color:var(--black)}
button{display:inline-block;padding:14px 30px;border:1.5px solid var(--black);background:transparent;font-family:inherit;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--black);font-weight:500;cursor:pointer;transition:background .25s,color .25s}
button:hover{background:var(--black);color:var(--white)}
.err{font-size:13px;color:#8a2b2b;margin-top:16px;min-height:20px}
.note{margin-top:52px;padding-top:22px;border-top:1px solid var(--border);font-size:12.5px;font-weight:300;color:var(--grey);max-width:440px;line-height:1.8}
@media(max-width:760px){form{flex-direction:column}button{width:100%}#gate{padding:0 7%}}
</style></head><body>
<div id="gate"><div class="inner">
<img src="/logo.png" alt="Inframind">
<span class="label">Ιδιωτική σελίδα</span>
<h1>G.A.P. Vassilopoulos<br><span class="light">Vpayments &middot; Sliq</span></h1>
${
    expired
      ? `<p class="lede">Η περίοδος ισχύος της πρότασης έχει παρέλθει. Παρακαλώ επικοινωνήστε στο <a href="mailto:ioannis@inframind.eu" style="color:var(--dark)">ioannis@inframind.eu</a> για ανανέωση της πρόσβασης.</p>`
      : `<p class="lede">Το περιεχόμενο προορίζεται αποκλειστικά για τον παραλήπτη. Παρακαλώ εισαγάγετε τον κωδικό πρόσβασης.</p>
<form method="POST" action="${BASE}/">
  <input type="password" name="pw" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Κωδικός πρόσβασης" autofocus>
  <input type="hidden" name="next" value="${next.replace(/"/g, "&quot;")}">
  <button type="submit">Είσοδος</button>
</form>
<div class="err">${failed ? "Λανθασμένος κωδικός." : "&nbsp;"}</div>
<p class="note">Ο έλεγχος πρόσβασης εκτελείται στον διακομιστή. Χωρίς έγκυρο κωδικό δεν σερβίρεται ούτε η σελίδα ούτε τα συνημμένα αρχεία. Η σύνδεση είναι κρυπτογραφημένη (HTTPS/TLS) και η σελίδα δεν ευρετηριάζεται από μηχανές αναζήτησης.</p>`
  }
</div></div></body></html>`;
}

const HTML = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "no-store, private",
  "x-robots-tag": "noindex, nofollow, noarchive",
  "referrer-policy": "no-referrer",
};

export default async (request: Request, context: any) => {
  const url = new URL(request.url);
  const secret = Netlify.env.get("GAP_SECRET");
  const pwHash = Netlify.env.get("GAP_PW_HASH");

  if (!secret || !pwHash) {
    return new Response("Access control not configured.", { status: 503 });
  }

  if (Date.now() > VALID_UNTIL) {
    return new Response(loginPage(BASE + "/", false, true), { status: 410, headers: HTML });
  }

  // ── login submission ──
  if (request.method === "POST") {
    const form = await request.formData();
    const pw = String(form.get("pw") || "").trim();
    let next = String(form.get("next") || BASE + "/");
    if (!next.startsWith(BASE)) next = BASE + "/";

    if (pw && (await sha256(pw)) === pwHash) {
      const vid = cookie(request, VISITOR_COOKIE) ||
        crypto.randomUUID().replace(/-/g, "").slice(0, 16);
      const exp = Date.now() + TTL_DAYS * 86400000;
      const sig = await hmac(secret, vid + "." + exp);
      const h = new Headers({ location: next });
      h.append(
        "set-cookie",
        `${SESSION_COOKIE}=${vid}.${exp}.${sig}; Path=${BASE}; Max-Age=${TTL_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax`,
      );
      h.append(
        "set-cookie",
        `${VISITOR_COOKIE}=${vid}; Path=/; Max-Age=7776000; Secure; SameSite=Lax`,
      );
      await audit(request, context, { type: "unlock", vid });
      if (!cookie(request, "gap_n")) {
        await notify(
          "INFRAMIND · GAP: άνοιξαν τη σελίδα της πρότασης",
          `Η ιδιωτική σελίδα της πρότασης προς G.A.P. Vassilopoulos άνοιξε με τον κωδικό.\n\nΏρα (Κύπρος): ${nowCy()}\n\nΛεπτομέρειες στον πίνακα καταγραφής στο inframind.eu.`,
        );
        h.append(
          "set-cookie",
          `gap_n=1; Path=${BASE}; Max-Age=${TTL_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax`,
        );
      }
      return new Response(null, { status: 303, headers: h });
    }

    await audit(request, context, { type: "bad_password" });
    return new Response(loginPage(next, true), { status: 401, headers: HTML });
  }

  // ── session check ──
  const tok = cookie(request, SESSION_COOKIE);
  let vid: string | null = null;
  if (tok) {
    const [v, exp, sig] = tok.split(".");
    if (v && exp && sig && Number(exp) > Date.now() && (await hmac(secret, v + "." + exp)) === sig) {
      vid = v;
    }
  }

  if (!vid) {
    await audit(request, context, { type: "locked_hit", path: url.pathname });
    return new Response(loginPage(url.pathname, false), { status: 401, headers: HTML });
  }

  const isPdf = url.pathname.toLowerCase().endsWith(".pdf");
  await audit(request, context, {
    type: isPdf ? "document_open" : "page_view",
    vid,
    path: url.pathname,
  });

  const res = await context.next();
  const out = new Response(res.body, res);
  out.headers.set("cache-control", "no-store, private");
  out.headers.set("x-robots-tag", "noindex, nofollow, noarchive");
  out.headers.set("referrer-policy", "no-referrer");

  if (isPdf) {
    const doc = (url.pathname.split("/").pop() || "").replace(/\.pdf$/i, "");
    const flag = "gap_d_" + doc.replace(/[^a-z0-9]/gi, "");
    if (doc && !cookie(request, flag)) {
      const title = doc === "proposal"
        ? "την πρόταση συνεργασίας"
        : doc === "presentation"
        ? "την παρουσίαση"
        : doc;
      await notify(
        `INFRAMIND · GAP: άνοιξαν ${title}`,
        `Άνοιγμα εγγράφου: ${doc}.pdf\n\nΏρα (Κύπρος): ${nowCy()}\n\nΛεπτομέρειες στον πίνακα καταγραφής στο inframind.eu.`,
      );
      out.headers.append(
        "set-cookie",
        `${flag}=1; Path=${BASE}; Max-Age=${TTL_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax`,
      );
    }
  }
  return out;
};

export const config = {
  path: ["/p/gap-payments-2026", "/p/gap-payments-2026/*"],
};
