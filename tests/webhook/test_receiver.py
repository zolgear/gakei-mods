"""Webhook の受け口の例のテスト。外部には通信しない(127.0.0.1 の空きポートで立てる)。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request

import httpx
import pytest
import receiver
import submit_verdict

SECRET = "test-secret"
DENY = "d" * 64
FLAG = "f" * 64


def config(**kw) -> receiver.Config:
    return receiver.Config(
        secret=SECRET,
        banned_words=["confidential", "社外秘"],
        check_localities={"cloud", "unset"},
        deny_sha256={DENY},
        flag_sha256={FLAG},
        **kw,
    )


def image(sha: str, asset_id: str | None = "asset-1", role: str | None = None) -> dict:
    return {
        "asset_id": asset_id,
        "sha256": sha,
        "width": 8,
        "height": 8,
        "bytes": 3,
        "mime": "image/png",
        "role": role,
    }


def envelope(event: str, subject: dict) -> dict:
    return {
        "schema_version": 1,
        "event": event,
        "event_id": "0b6f4c1e-3f8a-4b7e-9c55-2d1a7a0c9e10",
        "occurred_at": "2026-10-16T03:04:05+00:00",
        "instance": {"version": "0.0.0", "name": None},
        "actor": None,
        "subject": subject,
    }


def before_send(text: str, locality: str = "cloud", images: list | None = None) -> dict:
    return envelope(
        "ai.before_send",
        {
            "purpose": "image_run",
            "target": {
                "provider": "openai",
                "model": "gpt-image-1",
                "host": "api.openai.com",
                "locality": locality,
                "locality_source": "default",
                "workflow": None,
                "connection": None,
            },
            "text": [{"role": "prompt", "text": text}],
            "params": {},
            "images": images or [],
            "run_id": None,
            "asset_id": None,
        },
    )


def run_finished(outputs: list) -> dict:
    return envelope(
        "run.finished",
        {
            "run_id": "run-1",
            "status": "succeeded",
            "provider": "openai",
            "model": "gpt-image-1",
            "operation": "generate",
            "prompt": "a cat",
            "params": {},
            "inputs": [],
            "outputs": outputs,
            "usage": None,
            "error": None,
        },
    )


# -- 署名 ------------------------------------------------------------------------------


def test_signature_ok():
    body = b'{"a":1}'
    ts = str(int(time.time()))
    assert receiver.verify(SECRET, ts, receiver.sign(SECRET, ts, body), body)


def test_signature_wrong_secret_or_body():
    body = b'{"a":1}'
    ts = str(int(time.time()))
    assert not receiver.verify(SECRET, ts, receiver.sign("other", ts, body), body)
    assert not receiver.verify(SECRET, ts, receiver.sign(SECRET, ts, body), b'{"a":2}')
    assert not receiver.verify(SECRET, ts, None, body)
    assert not receiver.verify(SECRET, "not-a-number", "sha256=00", body)


def test_signature_old_or_future_timestamp():
    body = b"{}"
    now = 1_800_000_000
    for ts in (str(now - 301), str(now + 301)):
        assert not receiver.verify(SECRET, ts, receiver.sign(SECRET, ts, body), body, now=now)
    ts = str(now - 299)
    assert receiver.verify(SECRET, ts, receiver.sign(SECRET, ts, body), body, now=now)


def test_signature_matches_gakei_format():
    # docs/mods.md の形: "sha256=" + HMAC-SHA256(鍵, "<timestamp>." + 本文) の hex
    import hmac

    body = b'{"x":"\xe3\x81\x82"}'
    expected = "sha256=" + hmac.new(b"k", b"123." + body, hashlib.sha256).hexdigest()
    assert receiver.sign("k", "123", body) == expected


# -- 判断 ------------------------------------------------------------------------------


def test_before_send_decisions():
    cfg = config()
    assert receiver.handle_event(before_send("a cat"), [], cfg) == (200, {"decision": "allow"})
    status, payload = receiver.handle_event(before_send("ＣＯＮＦＩＤＥＮＴＩＡＬ"), [], cfg)
    assert payload["decision"] == "deny"
    # local の送り先は禁止語を検査しない。
    assert receiver.handle_event(before_send("confidential", "local"), [], cfg)[1] == {
        "decision": "allow"
    }
    # 拒否の一覧にある画像は、送り先に関わらず止める。
    event = before_send("a cat", "local", [image(DENY, role="primary")])
    assert receiver.handle_event(event, [], cfg)[1]["decision"] == "deny"


def test_run_finished_verdicts():
    event = run_finished([image(DENY, "a1"), image(FLAG, "a2"), image("0" * 64, "a3")])
    status, verdicts = receiver.handle_event(event, [], config())
    assert status == 200
    assert [(v["target"]["id"], v["verdict"]) for v in verdicts] == [("a1", "deny"), ("a2", "flag")]


def test_asset_ingested_verdict():
    event = envelope(
        "asset.ingested",
        {
            "asset": image(DENY),
            "kind": "upload",
            "source": "upload",
            "run_id": None,
            "rating": None,
        },
    )
    [verdict] = receiver.handle_event(event, [], config())[1]
    assert verdict["verdict"] == "deny"


def test_unknown_schema_version_is_rejected():
    event = {**before_send("a cat"), "schema_version": 2}
    assert receiver.handle_event(event, [], config())[0] == 400


# -- multipart -------------------------------------------------------------------------


def multipart(event: dict, images: list[tuple[str, bytes]]) -> tuple[str, bytes]:
    """GAKEI と同じ形の multipart の本文(GAKEI も httpx で組み立てる)。"""
    body_json = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    files = [("event", ("event.json", body_json, "application/json"))]
    files += [("images", (name, data, "image/png")) for name, data in images]
    request = httpx.Request("POST", "http://example.invalid/", files=files)
    return request.headers["Content-Type"], request.read()


def test_parse_multipart_keeps_binary_bytes():
    png = bytes(range(256)) * 4
    sha = hashlib.sha256(png).hexdigest()
    event = run_finished([image(sha, "a1")])
    content_type, body = multipart(event, [("a1.png", png)])
    parsed, images = receiver.parse_body(content_type, body)
    assert parsed == event
    assert [(i.filename, i.mime, i.data) for i in images] == [("a1.png", "image/png", png)]
    # 受けた画像の sha256 がイベントの値と合うので、追加の判定は無い。
    assert receiver.handle_event(parsed, images, config())[1] == []


def test_multipart_with_mismatched_image_is_flagged():
    event = run_finished([image("1" * 64, "a1")])
    content_type, body = multipart(event, [("a1.png", b"other bytes")])
    parsed, images = receiver.parse_body(content_type, body)
    [verdict] = receiver.handle_event(parsed, images, config())[1]
    assert verdict["verdict"] == "flag"


def test_parse_json_body():
    event = before_send("a cat")
    parsed, images = receiver.parse_body("application/json", json.dumps(event).encode())
    assert parsed == event and images == []


# -- HTTP ------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    srv = receiver.make_server(config(save_dir=tmp_path / "received"), "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/", tmp_path / "received"
    srv.shutdown()
    srv.server_close()


def post(url: str, body: bytes, content_type: str, *, secret=SECRET, ts=None):
    ts = str(int(time.time())) if ts is None else ts
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "X-Gakei-Event": "test",
            "X-Gakei-Timestamp": ts,
            "X-Gakei-Signature": receiver.sign(secret, ts, body),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_json_roundtrip(server):
    url, _ = server
    body = json.dumps(before_send("社外秘")).encode()
    status, payload = post(url, body, "application/json")
    assert status == 200 and payload["decision"] == "deny"


def test_http_rejects_bad_signature_and_old_timestamp(server):
    url, _ = server
    body = json.dumps(before_send("a cat")).encode()
    assert post(url, body, "application/json", secret="wrong")[0] == 401
    assert post(url, body, "application/json", ts=str(int(time.time()) - 3600))[0] == 401


def test_http_multipart_saves_images(server):
    url, saved = server
    png = b"\x89PNG fake"
    event = run_finished([image(DENY, "a1")])
    content_type, body = multipart(event, [("a1.png", png)])
    status, payload = post(url, body, content_type)
    assert status == 200
    # DENY の sha256 は実際の画像と合わないので、deny と「一致しません」の flag が返る。
    assert sorted(v["verdict"] for v in payload) == ["deny", "flag"]
    folder = saved / event["event_id"]
    assert (folder / "a1.png").read_bytes() == png
    assert json.loads((folder / "event.json").read_text("utf-8")) == event


# -- 判定を後から送る ----------------------------------------------------------------------


def test_submit_verdict_request_is_signed():
    verdicts = submit_verdict.build_verdicts(
        assets=["a1"], runs=["r1"], verdict="deny", label="侵害の疑い", reference="case-1"
    )
    request = submit_verdict.build_request(
        "http://gakei.example.com/", "wh/1", SECRET, verdicts, timestamp=1_800_000_000
    )
    assert request.full_url == "http://gakei.example.com/api/mods/wh%2F1/verdicts"
    assert request.get_method() == "POST"
    body = request.data
    assert json.loads(body) == [
        {
            "target": {"type": "asset", "id": "a1"},
            "verdict": "deny",
            "label": "侵害の疑い",
            "reference": "case-1",
        },
        {
            "target": {"type": "run", "id": "r1"},
            "verdict": "deny",
            "label": "侵害の疑い",
            "reference": "case-1",
        },
    ]
    headers = {k.lower(): v for k, v in request.header_items()}
    assert receiver.verify(
        SECRET, headers["x-gakei-timestamp"], headers["x-gakei-signature"], body, now=1_800_000_000
    )


def test_submit_verdict_requires_secret(monkeypatch, capsys):
    monkeypatch.delenv("GAKEI_WEBHOOK_SECRET", raising=False)
    code = submit_verdict.main(
        [
            "--gakei-url",
            "http://127.0.0.1:1",
            "--webhook-id",
            "x",
            "--asset",
            "a",
            "--verdict",
            "ok",
        ]
    )
    assert code == 2
