"""イベントの記録(GAKEI の Python の Mod)。

`ai.before_send`、`asset.ingested`、`run.finished` を受け、1イベント1行の JSON(JSON Lines)で
`<log_dir>/audit-YYYY-MM-DD.jsonl` に追記する(日付はイベントの時刻の UTC)。

- `ai.before_send` には常に許可を返す。記録に失敗しても送信は止めない(サーバーのログに残す)。
- `asset.ingested` と `run.finished` は、書けなければ例外にする。GAKEI が配達の失敗として
  間隔を空けて送り直す。送り直しで同じイベントが2行になることがあるので、読む側は
  `event_id` で重複を除く。
- 判定は返さない(マニフェストに `returns_verdicts = false`)。
- 使うのは `gakei_mod` と標準ライブラリだけ。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gakei_mod import AI_BEFORE_SEND, RUN_FINISHED, Decision, Event, ModContext

# 同じプロセスの中で、同じファイルへの書き込みが混ざらないようにする。
_lock = asyncio.Lock()


def _text_value(text: str, include_text: bool, max_chars: int) -> Any:
    if not include_text:
        # 文は残さず、長さと sha256 だけ残す(同じ文が送られたかどうかは突き合わせられる)。
        return {"chars": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def build_record(event: Event, settings: dict[str, Any]) -> dict[str, Any]:
    """記録する1行の中身。封筒(イベントの JSON)を元に、設定に従って文と実行した人を伏せる。"""
    include_text = bool(settings.get("include_text", True))
    include_actor = bool(settings.get("include_actor", True))
    max_chars = max(1, int(settings.get("max_text_chars") or 2000))

    record = event.to_json()
    if not include_actor and record.get("actor") is not None:
        record["actor"] = {"id": record["actor"]["id"]}
    subject = record["subject"]
    if event.event == AI_BEFORE_SEND:
        for part in subject.get("text") or []:
            part["text"] = _text_value(part["text"], include_text, max_chars)
    elif event.event == RUN_FINISHED:
        subject["prompt"] = _text_value(subject.get("prompt") or "", include_text, max_chars)
    if not include_text:
        # パラメーターは送った値そのままで、プロンプトなどの文を含むことがある。
        subject.pop("params", None)
    return {"logged_at": datetime.now(UTC).isoformat(), **record}


def log_path(log_dir: str, event: Event) -> Path:
    try:
        day = datetime.fromisoformat(event.occurred_at).astimezone(UTC).date().isoformat()
    except ValueError:
        day = datetime.now(UTC).date().isoformat()
    return Path(log_dir) / f"audit-{day}.jsonl"


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def write_record(event: Event, ctx: ModContext) -> Path:
    settings = dict(ctx.settings)
    log_dir = str(settings.get("log_dir") or "").strip()
    if not log_dir:
        raise RuntimeError("log_dir is not set")
    if not Path(log_dir).is_absolute():
        raise RuntimeError("log_dir must be an absolute path")
    record = build_record(event, settings)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    path = log_path(log_dir, event)
    async with _lock:
        # ファイルの書き込みは同期の処理なので、スレッドへ逃がす(GAKEI のイベントループを止めない)。
        await asyncio.to_thread(_append, path, line)
    return path


async def on_ai_before_send(event: Event, ctx: ModContext) -> Decision:
    try:
        await write_record(event, ctx)
    except Exception:  # noqa: BLE001 - 記録の失敗で送信を止めない
        ctx.logger.exception("イベントを記録できませんでした(送信は止めません)")
    return Decision.allow()


async def on_asset_ingested(event: Event, ctx: ModContext) -> None:
    await write_record(event, ctx)


async def on_run_finished(event: Event, ctx: ModContext) -> None:
    await write_record(event, ctx)
