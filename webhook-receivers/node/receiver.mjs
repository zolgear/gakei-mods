// GAKEI の Webhook の最小の受け口(Node.js 24 の標準の機能だけ)。
//
// - 署名(X-Gakei-Signature、HMAC-SHA256)と時刻のずれ(X-Gakei-Timestamp)を確かめる。
// - ai.before_send に、禁止語で許可・拒否を返す。
// - asset.ingested と run.finished には空の判定([])を返す(受けたことだけを返す)。
// - 本文は application/json と multipart/form-data の両方を受ける。
//
// 使い方:
//   GAKEI_WEBHOOK_SECRET=<署名の鍵> node receiver.mjs
//   # http://127.0.0.1:8799/ で待ち受ける(HOST、PORT、BANNED_WORDS で変えられる)

import { createHmac, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";

export const MAX_SKEW_SECONDS = 300;
const MAX_BODY_BYTES = 64 * 1024 * 1024;

/** X-Gakei-Signature の値: "sha256=" + HMAC-SHA256(鍵, "<timestamp>." + 本文) の hex。 */
export function sign(secret, timestamp, body /* Buffer */) {
  return (
    "sha256=" +
    createHmac("sha256", secret)
      .update(Buffer.concat([Buffer.from(`${timestamp}.`), body]))
      .digest("hex")
  );
}

/**
 * 署名が合い、時刻が前後 maxSkew 秒以内なら true。
 * body は受け取ったバイト列そのもの(JSON として読み直してから計算すると合わない)。
 */
export function verify(secret, timestamp, signature, body, { now = Date.now() / 1000, maxSkew = MAX_SKEW_SECONDS } = {}) {
  if (!timestamp || !signature || !/^\d+$/.test(timestamp)) return false;
  if (Math.abs(now - Number(timestamp)) > maxSkew) return false;
  const a = Buffer.from(sign(secret, timestamp, body));
  const b = Buffer.from(signature);
  return a.length === b.length && timingSafeEqual(a, b);
}

/** 本文からイベントの JSON を取り出す(multipart は標準の Request で読む)。 */
export async function parseEvent(contentType, body) {
  if (!/^multipart\/form-data/i.test(contentType ?? "")) {
    return { event: JSON.parse(body.toString("utf8")), images: [] };
  }
  const form = await new Request("http://localhost/", {
    method: "POST",
    headers: { "content-type": contentType },
    body,
  }).formData();
  const eventPart = form.get("event");
  if (!eventPart) throw new Error("the multipart body has no 'event' part");
  const event = JSON.parse(typeof eventPart === "string" ? eventPart : await eventPart.text());
  // 画像の原本(File)。名前は "<Asset の id>.<拡張子>"。
  const images = form.getAll("images");
  return { event, images };
}

const normalize = (text) => text.normalize("NFKC").toLowerCase();

/** イベントへの応答 [HTTP の状態, 応答の JSON]。 */
export function handleEvent(event, { bannedWords }) {
  if (event.schema_version !== 1) return [400, { error: "unsupported schema_version" }];
  if (event.event === "ai.before_send") {
    for (const part of event.subject.text ?? []) {
      const text = normalize(part.text ?? "");
      const hit = bannedWords.find((word) => text.includes(normalize(word)));
      if (hit) return [200, { decision: "deny", reason: `禁止語「${hit}」を含みます` }];
    }
    return [200, { decision: "allow" }];
  }
  if (event.event === "asset.ingested" || event.event === "run.finished") return [200, []];
  return [400, { error: `unknown event: ${event.event}` }];
}

export function createReceiver({ secret, bannedWords }) {
  return createServer((req, res) => {
    const reply = (status, payload) => {
      const data = Buffer.from(JSON.stringify(payload));
      res.writeHead(status, { "content-type": "application/json", "content-length": data.length });
      res.end(data);
    };
    if (req.method !== "POST") return reply(405, { error: "method not allowed" });
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) req.destroy();
      else chunks.push(chunk);
    });
    req.on("end", async () => {
      // 署名は、受け取ったバイト列そのものに対して確かめる。
      const body = Buffer.concat(chunks);
      if (!verify(secret, req.headers["x-gakei-timestamp"], req.headers["x-gakei-signature"], body)) {
        return reply(401, { error: "invalid signature" });
      }
      let event;
      try {
        ({ event } = await parseEvent(req.headers["content-type"], body));
      } catch (e) {
        return reply(400, { error: `invalid body: ${e.message}` });
      }
      const [status, payload] = handleEvent(event, { bannedWords });
      console.log(`${event.event} ${event.event_id} -> ${JSON.stringify(payload)}`);
      reply(status, payload);
    });
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const secret = process.env.GAKEI_WEBHOOK_SECRET;
  if (!secret) {
    console.error("GAKEI_WEBHOOK_SECRET を設定してください");
    process.exit(2);
  }
  const bannedWords = (process.env.BANNED_WORDS ?? "社外秘,confidential")
    .split(",")
    .map((w) => w.trim())
    .filter(Boolean);
  const host = process.env.HOST ?? "127.0.0.1";
  const port = Number(process.env.PORT ?? 8799);
  createReceiver({ secret, bannedWords }).listen(port, host, () => {
    console.error(`GAKEI の Webhook を http://${host}:${port}/ で待ち受けます`);
  });
}
