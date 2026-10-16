"""禁止語のチェック(GAKEI の Python の Mod)。

外部の AI へ送る直前(`ai.before_send`)に呼ばれ、送る文(プロンプト、ネガティブプロンプト、
アシスタントへの発言など)に禁止語が含まれていれば拒否する。

- 比べる前に、文と禁止語の両方を NFKC で正規化し、大文字・小文字を区別しない
  (全角の「ＣＯＮＦＩＤＥＮＴＩＡＬ」も「confidential」に一致する)。
- 目的(`purpose`)、送り先の区分(`target.locality`)、文の種類(`role`)で、検査する範囲を絞れる。
  既定では、組織の外(`cloud`)と、区分を決めていない(`unset`)送り先だけを検査する。
- 使うのは `gakei_mod` と標準ライブラリだけ。
"""

from __future__ import annotations

import asyncio
import unicodedata
from pathlib import Path
from typing import Any

from gakei_mod import BeforeSendSubject, Decision, Event, ModContext


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _csv(value: Any) -> set[str]:
    """カンマ区切りの設定を集合にする(空白を落とし、空の要素は捨てる)。"""
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


# 禁止語のファイルは、変わったときだけ読み直す(パスと更新時刻で覚える)。
_file_cache: dict[str, tuple[float, list[str]]] = {}


def _read_words_file(path: str) -> list[str]:
    """1行に1語。`#` から行末は無視する。読めなければ例外(送る前のチェックの失敗になる)。"""
    file = Path(path)
    mtime = file.stat().st_mtime
    cached = _file_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    words: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        word = line.split("#", 1)[0].strip()
        if word:
            words.append(word)
    _file_cache[path] = (mtime, words)
    return words


async def load_words(settings: dict[str, Any]) -> list[str]:
    words = sorted(_csv(settings.get("words")))
    path = str(settings.get("words_file") or "").strip()
    if path:
        # ファイルの読み込みは同期の処理なので、スレッドへ逃がす(GAKEI のイベントループを止めない)。
        words += await asyncio.to_thread(_read_words_file, path)
    return words


def in_scope(subject: BeforeSendSubject, settings: dict[str, Any]) -> bool:
    """この送信を検査するか(目的と送り先の区分で絞る。空の設定は絞らない)。"""
    purposes = _csv(settings.get("purposes"))
    if purposes and subject.purpose not in purposes:
        return False
    localities = _csv(settings.get("localities"))
    if localities and subject.target.locality not in localities:
        return False
    return True


def find_banned(
    subject: BeforeSendSubject, words: list[str], roles: set[str]
) -> tuple[str, str] | None:
    """最初に見つかった (禁止語, 文の種類)。無ければ None。"""
    normalized = [(word, _normalize(word)) for word in words]
    for part in subject.text:
        if roles and part.role not in roles:
            continue
        text = _normalize(part.text)
        for word, needle in normalized:
            if needle and needle in text:
                return word, part.role
    return None


async def on_ai_before_send(event: Event, ctx: ModContext) -> Decision:
    subject = event.subject
    assert isinstance(subject, BeforeSendSubject)
    settings = dict(ctx.settings)
    if not in_scope(subject, settings):
        return Decision.allow()
    words = await load_words(settings)
    hit = find_banned(subject, words, _csv(settings.get("roles")))
    if hit is None:
        return Decision.allow()
    word, role = hit
    # 送ろうとした文そのものはログに出さない(禁止語と文の種類だけ)。
    ctx.logger.info(
        "禁止語で拒否しました: purpose=%s locality=%s role=%s",
        subject.purpose,
        subject.target.locality,
        role,
    )
    # reason は利用者の画面に出る(「禁止語のチェック が送信を止めました: …」)。
    return Decision.deny(f"禁止語「{word}」を含みます({role})")
