"""sha256 の一覧での判定(GAKEI の Python の Mod)。

画像の sha256 を、拒否の一覧と印の一覧に照らして判定を返す。

- `asset.ingested`: 取り込んだ画像が拒否の一覧にあれば `deny`、印の一覧にあれば `flag`。
- `run.finished`: 出力の画像ごとに同じく判定し、一致があれば Run にもまとめの判定を付ける。
- `ai.before_send`: 送る画像が拒否の一覧にあれば、送信を止める(設定 `block_send`)。

`deny` を受けた画像は、GAKEI が保存しない(取り消せない)。取り込みのたびに確かめ、拒否する画像を
一度も保存させたくないときは、`asset.ingested` を「判定を待つ」で購読する。

一覧のファイルの形(1行に1つ。`#` から行末は無視。ラベルは省略できる):

    # sha256                                                          ラベル
    9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08  社外の素材
    e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855

使うのは `gakei_mod` と標準ライブラリだけ。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gakei_mod import (
    AssetIngestedSubject,
    BeforeSendSubject,
    Decision,
    Event,
    ImageRef,
    ModContext,
    RunFinishedSubject,
    Verdict,
    VerdictTarget,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Match:
    verdict: str  # "deny" か "flag"
    label: str


# 一覧のファイルは、変わったときだけ読み直す(パスと更新時刻で覚える)。
_file_cache: dict[str, tuple[float, dict[str, str]]] = {}


def parse_list(text: str) -> dict[str, str]:
    """一覧の中身を {sha256: ラベル} にする(ラベルが無ければ空の文字列)。形の合わない行は捨てる。"""
    entries: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        sha, _, label = line.partition(" ")
        sha = sha.strip().lower()
        if _SHA256.match(sha):
            entries[sha] = label.strip()
    return entries


def _read_list_file(path: str) -> dict[str, str]:
    file = Path(path)
    mtime = file.stat().st_mtime
    cached = _file_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    entries = parse_list(file.read_text(encoding="utf-8"))
    _file_cache[path] = (mtime, entries)
    return entries


async def _load(settings: dict[str, Any], file_key: str, inline_key: str) -> dict[str, str]:
    entries = parse_list(str(settings.get(inline_key) or "").replace(",", "\n"))
    path = str(settings.get(file_key) or "").strip()
    if path:
        # ファイルの読み込みは同期の処理なので、スレッドへ逃がす(GAKEI のイベントループを止めない)。
        # 読めなければ例外にする(非同期の購読は送り直し、判定を待つ購読は「失敗のとき」に従う)。
        entries = {**entries, **await asyncio.to_thread(_read_list_file, path)}
    return entries


class Blocklist:
    def __init__(self, deny: dict[str, str], flag: dict[str, str], settings: dict[str, Any]):
        self.deny = deny
        self.flag = flag
        self.deny_label = str(settings.get("deny_label") or "deny list")
        self.flag_label = str(settings.get("flag_label") or "flag list")

    @classmethod
    async def load(cls, settings: dict[str, Any]) -> Blocklist:
        deny = await _load(settings, "deny_file", "deny_sha256")
        flag = await _load(settings, "flag_file", "flag_sha256")
        return cls(deny, flag, settings)

    def match(self, sha256: str) -> Match | None:
        """拒否の一覧を先に見る(両方にあれば deny)。"""
        sha256 = sha256.lower()
        if sha256 in self.deny:
            return Match("deny", self.deny[sha256] or self.deny_label)
        if sha256 in self.flag:
            return Match("flag", self.flag[sha256] or self.flag_label)
        return None


def _verdict_for(image: ImageRef, match: Match | None, report_ok: bool) -> Verdict | None:
    if image.asset_id is None:
        # Asset でない画像(テスト送信のダミーなど)は、判定の対象にできない。
        return None
    target = VerdictTarget(type="asset", id=image.asset_id)
    if match is None:
        return Verdict(target=target, verdict="ok", label="一覧に無し") if report_ok else None
    return Verdict(
        target=target,
        verdict=match.verdict,  # type: ignore[arg-type]
        label=match.label,
        score=1.0,
        message=f"sha256 {image.sha256[:12]}… が一覧に一致しました",
        reference=image.sha256,
    )


async def on_asset_ingested(event: Event, ctx: ModContext) -> list[Verdict]:
    subject = event.subject
    assert isinstance(subject, AssetIngestedSubject)
    settings = dict(ctx.settings)
    blocklist = await Blocklist.load(settings)
    match = blocklist.match(subject.asset.sha256)
    verdict = _verdict_for(subject.asset, match, bool(settings.get("report_ok")))
    if match is not None:
        ctx.logger.info("一覧に一致: %s (%s)", subject.asset.asset_id, match.verdict)
    return [verdict] if verdict is not None else []


async def on_run_finished(event: Event, ctx: ModContext) -> list[Verdict]:
    subject = event.subject
    assert isinstance(subject, RunFinishedSubject)
    settings = dict(ctx.settings)
    report_ok = bool(settings.get("report_ok"))
    blocklist = await Blocklist.load(settings)
    verdicts: list[Verdict] = []
    matches: list[Match] = []
    for image in subject.outputs:
        match = blocklist.match(image.sha256)
        if match is not None:
            matches.append(match)
        verdict = _verdict_for(image, match, report_ok)
        if verdict is not None:
            verdicts.append(verdict)
    if matches:
        # Run にもまとめの判定を付ける(どれかが deny なら deny)。Run への deny は記録と表示だけで、
        # 画像を残さない処理は Asset への deny で行われる。
        worst = "deny" if any(m.verdict == "deny" for m in matches) else "flag"
        verdicts.append(
            Verdict(
                target=VerdictTarget(type="run", id=subject.run_id),
                verdict=worst,  # type: ignore[arg-type]
                label=matches[0].label,
                message=(
                    f"出力 {len(subject.outputs)} 枚のうち {len(matches)} 枚が一覧に一致しました"
                ),
            )
        )
    return verdicts


async def on_ai_before_send(event: Event, ctx: ModContext) -> Decision:
    subject = event.subject
    assert isinstance(subject, BeforeSendSubject)
    settings = dict(ctx.settings)
    if not settings.get("block_send", True) or not subject.images:
        return Decision.allow()
    blocklist = await Blocklist.load(settings)
    for image in subject.images:
        match = blocklist.match(image.sha256)
        if match is not None and match.verdict == "deny":
            return Decision.deny(
                f"送る画像が一覧に一致しました({match.label})", reference=image.sha256
            )
    return Decision.allow()
