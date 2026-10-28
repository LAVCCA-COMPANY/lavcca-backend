import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { handle, flush } from "./index.js";

const SECRET = "secret_test";
let kv, env, dispatched, githubStatus;

class FakeKV {
  constructor() { this.m = new Map(); }
  async get(k) { return this.m.has(k) ? this.m.get(k) : null; }
  async put(k, v) { this.m.set(k, v); }
  async delete(k) { this.m.delete(k); }
}

beforeEach(() => {
  kv = new FakeKV();
  env = { KV: kv, NOTION_VERIFICATION_TOKEN: SECRET, GITHUB_TOKEN: "ghp_x", GITHUB_REPO: "LAVCCA-COMPANY/lavcca-backend", THROTTLE_SECONDS: "300" };
  dispatched = [];
  githubStatus = 204;
  globalThis.fetch = async (url, init) => {
    dispatched.push({ url, body: JSON.parse(init.body), auth: init.headers.Authorization });
    if (githubStatus === "throw") throw new Error("network");
    return new Response(null, { status: githubStatus });
  };
});

function event(id, page = "p1", type = "page.content_updated") {
  return { id, type, entity: { id: page, type: "page" } };
}

function req(obj, { sign = true, secret = SECRET, method = "POST" } = {}) {
  const body = JSON.stringify(obj);
  const headers = {};
  if (sign) headers["X-Notion-Signature"] = "sha256=" + createHmac("sha256", secret).update(body).digest("hex");
  return new Request("https://w.example/", { method, body: method === "POST" ? body : undefined, headers });
}

const T = 1_800_000_000_000;

test("first event dispatches immediately with page id and type", async () => {
  const r = await handle(req(event("e1")), env, T);
  assert.equal(r.status, 200);
  assert.equal(await r.text(), "dispatched");
  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].url, "https://api.github.com/repos/LAVCCA-COMPANY/lavcca-backend/actions/workflows/notion-sync.yml/dispatches");
  assert.equal(dispatched[0].body.ref, "main");
  assert.equal(dispatched[0].body.inputs.mode, "apply");
  assert.deepEqual(JSON.parse(dispatched[0].body.inputs.hint), { ids: ["p1"], types: ["page.content_updated"] });
  assert.equal(await kv.get("pending"), null);
});

test("bad, missing or wrong-secret signature is rejected without dispatch", async () => {
  for (const r of [req(event("e1"), { sign: false }), req(event("e1"), { secret: "other" })]) {
    assert.equal((await handle(r, env, T)).status, 401);
  }
  const tampered = req(event("e1"));
  const body = JSON.stringify(event("e1", "evil"));
  const forged = new Request("https://w.example/", { method: "POST", body, headers: { "X-Notion-Signature": tampered.headers.get("X-Notion-Signature") } });
  assert.equal((await handle(forged, env, T)).status, 401);
  assert.equal(dispatched.length, 0);
});

test("duplicate event id (Notion retry) is acknowledged and ignored", async () => {
  await handle(req(event("e1")), env, T);
  const r = await handle(req(event("e1")), env, T + 400_000);
  assert.equal(await r.text(), "duplicate");
  assert.equal(dispatched.length, 1);
});

test("events inside the throttle window are merged and flushed by cron", async () => {
  await handle(req(event("e1", "p1")), env, T);
  assert.equal(await (await handle(req(event("e2", "p2")), env, T + 10_000)).text(), "queued");
  await handle(req(event("e3", "p2", "page.moved")), env, T + 20_000);
  await handle(req(event("e4", "p1")), env, T + 30_000);
  assert.equal(dispatched.length, 1);
  assert.equal(await flush(env, T + 100_000), "queued");          // still inside the window
  assert.equal(await flush(env, T + 300_000), "dispatched");
  assert.deepEqual(JSON.parse(dispatched[1].body.inputs.hint), { ids: ["p2", "p1"], types: ["page.content_updated", "page.moved"] });
  assert.equal(await flush(env, T + 900_000), "idle");
});

test("GitHub failure returns 503 so Notion retries, keeps pending for cron, then recovers", async () => {
  for (const failure of [500, "throw"]) {
    githubStatus = failure;
    const r = await handle(req(event(`e-${failure}`)), env, T);
    assert.equal(r.status, 503);
    assert.equal(await kv.get(`ev:e-${failure}`), null);          // not marked seen: the retry is processed
    assert.ok(await kv.get("pending"));
  }
  githubStatus = 204;
  assert.equal(await flush(env, T + 1), "dispatched");
  assert.equal(await kv.get("pending"), null);
  const retry = await handle(req(event("e-500")), env, T + 400_000); // Notion's own retry arrives later
  assert.equal(retry.status, 200);
});

test("verification handshake is stored only while no secret is configured", async () => {
  const noSecret = { ...env, NOTION_VERIFICATION_TOKEN: undefined };
  const r = await handle(req({ verification_token: "secret_handshake" }, { sign: false }), noSecret, T);
  assert.equal(r.status, 200);
  assert.equal(await kv.get("verification_token"), "secret_handshake");
  const later = await handle(req({ verification_token: "secret_attacker" }, { sign: false }), env, T);
  assert.equal(later.status, 401);
  assert.equal(await kv.get("verification_token"), "secret_handshake");
  assert.equal(dispatched.length, 0);
});

test("non-POST and malformed JSON rejected", async () => {
  assert.equal((await handle(req({}, { method: "GET" }), env, T)).status, 405);
  const bad = new Request("https://w.example/", { method: "POST", body: "{" });
  assert.equal((await handle(bad, env, T)).status, 400);
});

test("id list is capped so client_payload stays small", async () => {
  githubStatus = 500;
  for (let i = 0; i < 80; i++) await handle(req(event(`e${i}`, `p${i}`)), env, T);
  assert.equal(JSON.parse(await kv.get("pending")).ids.length, 50);
});
