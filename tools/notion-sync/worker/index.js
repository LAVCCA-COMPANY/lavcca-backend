// Notion webhook -> GitHub workflow_dispatch. Only wakes the sync up: the run itself recomputes
// what changed, so dropped, duplicated or merged events cost latency, never data.
// Bindings: KV, NOTION_VERIFICATION_TOKEN (secret), GITHUB_TOKEN (secret: fine-grained, Actions write on
// one repo only -- workflow_dispatch needs no Contents permission, unlike repository_dispatch),
// GITHUB_REPO ("owner/name"), GITHUB_WORKFLOW (default notion-sync.yml), THROTTLE_SECONDS (default 300).
const enc = new TextEncoder();
const MAX_IDS = 50;

async function validSignature(secret, body, header) {
  const hex = (header || "").startsWith("sha256=") ? header.slice(7) : "";
  if (!/^[0-9a-f]{64}$/i.test(hex)) return false;
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  const sig = new Uint8Array(hex.match(/../g).map((h) => parseInt(h, 16)));
  return crypto.subtle.verify("HMAC", key, sig, enc.encode(body)); // constant-time compare
}

async function dispatch(env, pending) {
  try {
    const wf = env.GITHUB_WORKFLOW || "notion-sync.yml";
    const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${wf}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "lavcca-notion-webhook",
      },
      body: JSON.stringify({ ref: "main", inputs: { mode: "apply", hint: JSON.stringify(pending) } }),
    });
    return r.status === 204;
  } catch {
    return false;
  }
}

const throttleMs = (env) => 1000 * Number(env.THROTTLE_SECONDS || 300);

async function readPending(env) {
  return JSON.parse((await env.KV.get("pending")) || '{"ids":[],"types":[]}');
}

// Leading edge dispatches at once; events inside the window wait for the cron flush.
// shortcut: KV is eventually consistent, so two concurrent webhooks can drop one pending id;
// the run's own change detection and the scheduled reconcile cover it.
async function sendOrQueue(env, pending, now) {
  const last = Number((await env.KV.get("last_dispatch")) || 0);
  if (now - last >= throttleMs(env) && (await dispatch(env, pending))) {
    await env.KV.put("last_dispatch", String(now));
    await env.KV.delete("pending");
    return "dispatched";
  }
  await env.KV.put("pending", JSON.stringify(pending));
  return now - last >= throttleMs(env) ? "dispatch_failed" : "queued";
}

export async function handle(request, env, now = Date.now()) {
  if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
  const body = await request.text();
  let msg;
  try {
    msg = JSON.parse(body);
  } catch {
    return new Response("bad json", { status: 400 });
  }
  if (msg.verification_token && !env.NOTION_VERIFICATION_TOKEN) {
    // one-time subscription handshake: the owner reads it from KV, pastes it into Notion,
    // stores it as the NOTION_VERIFICATION_TOKEN secret, then deletes this KV key.
    await env.KV.put("verification_token", msg.verification_token);
    return new Response("ok");
  }
  if (!env.NOTION_VERIFICATION_TOKEN ||
      !(await validSignature(env.NOTION_VERIFICATION_TOKEN, body, request.headers.get("X-Notion-Signature")))) {
    return new Response("bad signature", { status: 401 });
  }
  if (!msg.id || (await env.KV.get(`ev:${msg.id}`))) return new Response("duplicate");
  const pending = await readPending(env);
  const id = msg.entity && msg.entity.id;
  if (id && !pending.ids.includes(id) && pending.ids.length < MAX_IDS) pending.ids.push(id);
  if (msg.type && !pending.types.includes(msg.type)) pending.types.push(msg.type);
  const outcome = await sendOrQueue(env, pending, now);
  if (outcome === "dispatch_failed") return new Response("dispatch failed", { status: 503 }); // Notion retries
  await env.KV.put(`ev:${msg.id}`, "1", { expirationTtl: 86400 });
  return new Response(outcome);
}

export async function flush(env, now = Date.now()) {
  const raw = await env.KV.get("pending");
  if (!raw) return "idle";
  return sendOrQueue(env, JSON.parse(raw), now);
}

export default {
  fetch: (request, env) => handle(request, env),
  scheduled: (event, env, ctx) => ctx.waitUntil(flush(env)),
};
