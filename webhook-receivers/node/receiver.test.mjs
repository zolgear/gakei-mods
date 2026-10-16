// node --test で回す(外部には通信しない。127.0.0.1 の空きポートで受け口を立てる)。
import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { createReceiver, handleEvent, parseEvent, sign, verify } from "./receiver.mjs";

const SECRET = "test-secret";
const now = () => Math.floor(Date.now() / 1000);

function beforeSend(text) {
  return {
    schema_version: 1,
    event: "ai.before_send",
    event_id: "00000000-0000-0000-0000-000000000001",
    occurred_at: "2026-10-16T00:00:00+00:00",
    instance: { version: "0.0.0", name: null },
    actor: null,
    subject: {
      purpose: "image_run",
      target: { provider: "openai", model: "gpt-image-1", host: "api.openai.com", locality: "cloud", locality_source: "default", workflow: null, connection: null },
      text: [{ role: "prompt", text }],
      params: {},
      images: [],
      run_id: null,
      asset_id: null,
    },
  };
}

test("正しい署名は通る", () => {
  const body = Buffer.from('{"a":1}');
  const ts = String(now());
  assert.equal(verify(SECRET, ts, sign(SECRET, ts, body), body), true);
});

test("誤った鍵・書き換えた本文は通らない", () => {
  const body = Buffer.from('{"a":1}');
  const ts = String(now());
  assert.equal(verify(SECRET, ts, sign("other", ts, body), body), false);
  assert.equal(verify(SECRET, ts, sign(SECRET, ts, body), Buffer.from('{"a":2}')), false);
  assert.equal(verify(SECRET, ts, undefined, body), false);
});

test("古い時刻は通らない", () => {
  const body = Buffer.from("{}");
  const ts = String(now() - 301);
  assert.equal(verify(SECRET, ts, sign(SECRET, ts, body), body), false);
});

test("禁止語を含めば拒否する(全角も)", () => {
  const opts = { bannedWords: ["confidential"] };
  assert.deepEqual(handleEvent(beforeSend("a cat"), opts), [200, { decision: "allow" }]);
  assert.equal(handleEvent(beforeSend("ＣＯＮＦＩＤＥＮＴＩＡＬ memo"), opts)[1].decision, "deny");
});

test("multipart からイベントと画像を取り出す", async () => {
  const form = new FormData();
  form.append("event", new Blob([JSON.stringify(beforeSend("x"))], { type: "application/json" }), "event.json");
  form.append("images", new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" }), "abc.png");
  const request = new Request("http://localhost/", { method: "POST", body: form });
  const body = Buffer.from(await request.arrayBuffer());
  const { event, images } = await parseEvent(request.headers.get("content-type"), body);
  assert.equal(event.event, "ai.before_send");
  assert.equal(images.length, 1);
  assert.equal(images[0].name, "abc.png");
});

let server;
let base;
before(async () => {
  server = createReceiver({ secret: SECRET, bannedWords: ["confidential"] });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}/`;
});
after(() => server.close());

async function post(body, { secret = SECRET, ts = String(now()) } = {}) {
  const res = await fetch(base, {
    method: "POST",
    headers: { "content-type": "application/json", "x-gakei-timestamp": ts, "x-gakei-signature": sign(secret, ts, body) },
    body,
  });
  return [res.status, await res.json()];
}

test("HTTP: 署名が正しければ応答を返す", async () => {
  const [status, payload] = await post(Buffer.from(JSON.stringify(beforeSend("confidential"))));
  assert.equal(status, 200);
  assert.equal(payload.decision, "deny");
});

test("HTTP: 署名が誤り・時刻が古ければ 401", async () => {
  const body = Buffer.from(JSON.stringify(beforeSend("a cat")));
  assert.equal((await post(body, { secret: "wrong" }))[0], 401);
  assert.equal((await post(body, { ts: String(now() - 3600) }))[0], 401);
});
