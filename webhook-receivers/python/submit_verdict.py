"""判定を後から GAKEI へ送る例(Python の標準ライブラリだけ)。

外部の審査に時間がかかるとき、Webhook の配達にはすぐ空の応答(`[]`)を返し、結果が出てから
`POST /api/mods/{Webhook の id}/verdicts` へ送る。認証は、その Webhook の署名の鍵による署名
(送られてくるときと同じ形式)。

使い方:

    GAKEI_WEBHOOK_SECRET=<署名の鍵> python submit_verdict.py \\
        --gakei-url http://127.0.0.1:8000 --webhook-id <Webhook の id> \\
        --asset <Asset の id> --verdict deny --label "侵害の疑い" --reference case-1234

- 送れる対象は、その Webhook に届いた Asset と Run だけ(対象外が1つでもあれば 403)。
- `deny` を送ると、GAKEI はその画像を残さない(取り消せない)。
- Webhook の id は、管理者設定の「Mod」の Webhook の一覧で確かめる。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    )
    return "sha256=" + digest.hexdigest()


def build_verdicts(
    *,
    assets: list[str],
    runs: list[str],
    verdict: str,
    label: str | None = None,
    message: str | None = None,
    score: float | None = None,
    reference: str | None = None,
    url: str | None = None,
) -> list[dict[str, Any]]:
    targets = [("asset", a) for a in assets] + [("run", r) for r in runs]
    verdicts = []
    for target_type, target_id in targets:
        item: dict[str, Any] = {
            "target": {"type": target_type, "id": target_id},
            "verdict": verdict,
        }
        for key, value in (
            ("label", label),
            ("message", message),
            ("score", score),
            ("reference", reference),
            ("url", url),
        ):
            if value is not None:
                item[key] = value
        verdicts.append(item)
    return verdicts


def build_request(
    gakei_url: str,
    webhook_id: str,
    secret: str,
    verdicts: list[dict[str, Any]],
    *,
    timestamp: int | None = None,
) -> urllib.request.Request:
    """署名を付けた要求。署名するのは、送るバイト列そのもの。"""
    body = json.dumps(verdicts, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()) if timestamp is None else timestamp)
    endpoint = f"{gakei_url.rstrip('/')}/api/mods/{quote(webhook_id, safe='')}/verdicts"
    return urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Gakei-Timestamp": ts,
            "X-Gakei-Signature": sign(secret, ts, body),
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判定を GAKEI へ後から送る")
    parser.add_argument("--gakei-url", required=True, help="例: http://127.0.0.1:8000")
    parser.add_argument("--webhook-id", required=True)
    parser.add_argument("--asset", action="append", default=[], help="対象の Asset の id(複数可)")
    parser.add_argument("--run", action="append", default=[], help="対象の Run の id(複数可)")
    parser.add_argument("--verdict", required=True, choices=["ok", "flag", "deny"])
    parser.add_argument("--label")
    parser.add_argument("--message")
    parser.add_argument("--score", type=float)
    parser.add_argument("--reference")
    parser.add_argument("--url")
    args = parser.parse_args(argv)

    secret = os.environ.get("GAKEI_WEBHOOK_SECRET", "")
    if not secret:
        print("GAKEI_WEBHOOK_SECRET を設定してください", file=sys.stderr)
        return 2
    if not args.asset and not args.run:
        print("--asset か --run を1つ以上指定してください", file=sys.stderr)
        return 2
    verdicts = build_verdicts(
        assets=args.asset,
        runs=args.run,
        verdict=args.verdict,
        label=args.label,
        message=args.message,
        score=args.score,
        reference=args.reference,
        url=args.url,
    )
    request = build_request(args.gakei_url, args.webhook_id, secret, verdicts)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.read().decode("utf-8"))  # {"accepted": <記録した数>}
            return 0
    except urllib.error.HTTPError as e:
        # 401: 署名・時刻が合わない、403: 対象外、404: Webhook が無い・無効、422: 形が合わない
        print(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
