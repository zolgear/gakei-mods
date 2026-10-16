"""GAKEI の Webhook の受け口の例(Python の標準ライブラリだけ)。

- 署名(`X-Gakei-Signature`、HMAC-SHA256)と時刻のずれ(`X-Gakei-Timestamp`)を確かめる。
- 本文は `application/json`(既定)と `multipart/form-data`(「画像を送る」をオンにしたとき)の
  両方を受ける。
- `ai.before_send` には、禁止語と拒否する画像の sha256 で許可・拒否を返す。
- `asset.ingested` と `run.finished` には、sha256 の一覧で判定(`deny` / `flag`)を返す。

使い方:

    GAKEI_WEBHOOK_SECRET=<登録した署名の鍵> python receiver.py
    # http://127.0.0.1:8799/ で待ち受ける。GAKEI の管理者設定の「Mod」で、この URL を登録する

環境変数:

| 名前 | 既定 | 内容 |
|---|---|---|
| GAKEI_WEBHOOK_SECRET | (必須) | Webhook に登録した署名の鍵 |
| HOST / PORT | 127.0.0.1 / 8799 | 待ち受けるアドレスとポート |
| BANNED_WORDS | 社外秘,confidential | 禁止語(カンマ区切り) |
| CHECK_LOCALITIES | cloud,unset | 禁止語を検査する送り先の区分(空ならすべて) |
| DENY_SHA256 | (空) | 拒否する画像の sha256(カンマ区切り) |
| FLAG_SHA256 | (空) | 印を付ける画像の sha256(カンマ区切り) |
| SAVE_DIR | (空) | 指定すると、受けたイベントと画像を `<SAVE_DIR>/<event_id>/` に保存する |

これは例であり、本番で使うときは TLS の終端(リバースプロキシなど)の後ろに置く。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA_VERSION = 1
MAX_SKEW_SECONDS = 300
MAX_BODY_BYTES = 64 * 1024 * 1024


# -- 署名 -------------------------------------------------------------------------------


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """`X-Gakei-Signature` の値: `sha256=` + HMAC-SHA256(鍵, "<timestamp>." + 本文) の hex。"""
    digest = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    )
    return "sha256=" + digest.hexdigest()


def verify(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    *,
    now: float | None = None,
    max_skew: int = MAX_SKEW_SECONDS,
) -> bool:
    """署名が合い、時刻が前後 `max_skew` 秒以内なら True。

    `body` は受け取ったバイト列そのもの(multipart なら境界を含む全体)。JSON として読み直して
    から計算すると合わない。
    """
    if not timestamp or not signature:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > max_skew:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


# -- 本文 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceivedImage:
    filename: str
    mime: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def parse_body(content_type: str, body: bytes) -> tuple[dict[str, Any], list[ReceivedImage]]:
    """本文からイベントの JSON と画像を取り出す。形が合わなければ ValueError。"""
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        return json.loads(body), []
    # multipart/form-data: 部分 `event`(event.json)と、画像の数だけの部分 `images`。
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise ValueError("invalid multipart body")
    event: dict[str, Any] | None = None
    images: list[ReceivedImage] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if name == "event":
            event = json.loads(data)
        elif name == "images":
            images.append(
                ReceivedImage(
                    filename=part.get_filename() or "image.bin",
                    mime=part.get_content_type(),
                    data=data,
                )
            )
    if event is None:
        raise ValueError("the multipart body has no 'event' part")
    return event, images


# -- 判断 -------------------------------------------------------------------------------


def _csv(value: str | None) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


@dataclass
class Config:
    secret: str
    banned_words: list[str] = field(default_factory=list)
    check_localities: set[str] = field(default_factory=set)
    deny_sha256: set[str] = field(default_factory=set)
    flag_sha256: set[str] = field(default_factory=set)
    save_dir: Path | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        secret = env.get("GAKEI_WEBHOOK_SECRET", "")
        if not secret:
            raise SystemExit("GAKEI_WEBHOOK_SECRET を設定してください")
        save_dir = env.get("SAVE_DIR", "").strip()
        return cls(
            secret=secret,
            banned_words=sorted(_csv(env.get("BANNED_WORDS", "社外秘,confidential"))),
            check_localities=_csv(env.get("CHECK_LOCALITIES", "cloud,unset")),
            deny_sha256={s.lower() for s in _csv(env.get("DENY_SHA256"))},
            flag_sha256={s.lower() for s in _csv(env.get("FLAG_SHA256"))},
            save_dir=Path(save_dir) if save_dir else None,
        )


def decide_before_send(subject: dict[str, Any], config: Config) -> dict[str, Any]:
    """`ai.before_send` の応答(`{"decision": "allow" | "deny", "reason"?, "reference"?}`)。"""
    for image in subject.get("images") or []:
        if str(image.get("sha256", "")).lower() in config.deny_sha256:
            return {"decision": "deny", "reason": "送る画像が拒否の一覧に一致しました"}
    locality = (subject.get("target") or {}).get("locality")
    if config.check_localities and locality not in config.check_localities:
        return {"decision": "allow"}
    for part in subject.get("text") or []:
        text = _normalize(str(part.get("text", "")))
        for word in config.banned_words:
            if _normalize(word) in text:
                return {"decision": "deny", "reason": f"禁止語「{word}」を含みます"}
    return {"decision": "allow"}


def _image_verdict(image: dict[str, Any], config: Config) -> dict[str, Any] | None:
    asset_id = image.get("asset_id")
    sha = str(image.get("sha256", "")).lower()
    if not asset_id:
        return None  # Asset でない画像(テスト送信のダミー)は判定の対象にできない
    if sha in config.deny_sha256:
        return {
            "target": {"type": "asset", "id": asset_id},
            "verdict": "deny",
            "label": "拒否の一覧に一致",
            "score": 1.0,
            "reference": sha,
        }
    if sha in config.flag_sha256:
        return {
            "target": {"type": "asset", "id": asset_id},
            "verdict": "flag",
            "label": "要確認の一覧に一致",
            "reference": sha,
        }
    return None


def _check_received(
    images: list[dict[str, Any]], received: list[ReceivedImage]
) -> list[dict[str, Any]]:
    """multipart で受けた画像の sha256 が、イベントの値と合うか(合わなければ flag)。"""
    by_sha = {r.sha256 for r in received}
    verdicts = []
    for image in images:
        if image.get("asset_id") and received and image.get("sha256") not in by_sha:
            verdicts.append(
                {
                    "target": {"type": "asset", "id": image["asset_id"]},
                    "verdict": "flag",
                    "label": "受け取った画像が一致しません",
                }
            )
    return verdicts


def decide_verdicts(
    event: dict[str, Any], received: list[ReceivedImage], config: Config
) -> list[dict[str, Any]]:
    """`asset.ingested` / `run.finished` の応答(判定の配列。返すものが無ければ空)。"""
    subject = event["subject"]
    if event["event"] == "asset.ingested":
        images = [subject["asset"]]
    else:
        images = list(subject.get("outputs") or [])
    verdicts = [v for v in (_image_verdict(i, config) for i in images) if v is not None]
    verdicts += _check_received(images, received)
    return verdicts


def handle_event(
    event: dict[str, Any], received: list[ReceivedImage], config: Config
) -> tuple[int, Any]:
    """(HTTP の状態, 応答の JSON)。"""
    if event.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        # 知らない版は受けない(GAKEI はこの呼び出しを失敗として扱う)。
        return 400, {"error": "unsupported schema_version"}
    name = event.get("event")
    if name == "ai.before_send":
        return 200, decide_before_send(event["subject"], config)
    if name in ("asset.ingested", "run.finished"):
        return 200, decide_verdicts(event, received, config)
    return 400, {"error": f"unknown event: {name}"}


def save_received(save_dir: Path, event: dict[str, Any], images: list[ReceivedImage]) -> None:
    folder = save_dir / re.sub(r"[^A-Za-z0-9-]", "_", str(event.get("event_id", "unknown")))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "event.json").write_text(json.dumps(event, ensure_ascii=False, indent=2), "utf-8")
    for image in images:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(image.filename).name) or "image.bin"
        (folder / name).write_bytes(image.data)


# -- HTTP ------------------------------------------------------------------------------


def make_handler(config: Config) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "gakei-webhook-example"

        def _reply(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802 - http.server の名前
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._reply(413, {"error": "too large"})
                return
            # 署名は、受け取ったバイト列そのものに対して確かめる。
            body = self.rfile.read(length)
            if not verify(
                config.secret,
                self.headers.get("X-Gakei-Timestamp"),
                self.headers.get("X-Gakei-Signature"),
                body,
            ):
                self._reply(401, {"error": "invalid signature"})
                return
            try:
                event, images = parse_body(self.headers.get("Content-Type", ""), body)
            except ValueError as e:
                self._reply(400, {"error": f"invalid body: {e}"})
                return
            if config.save_dir is not None:
                save_received(config.save_dir, event, images)
            status, payload = handle_event(event, images, config)
            self.log_message(
                "%s %s -> %s", event.get("event"), event.get("event_id"), json.dumps(payload)
            )
            self._reply(status, payload)

    return Handler


def make_server(config: Config, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(config))


def main() -> None:
    config = Config.from_env()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8799"))
    server = make_server(config, host, port)
    print(f"GAKEI の Webhook を http://{host}:{port}/ で待ち受けます", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
