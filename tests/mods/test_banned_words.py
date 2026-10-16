from __future__ import annotations

import asyncio

import pytest
from helpers import before_send_event, defaults, make_ctx


@pytest.fixture
def mod(load_mod):
    return load_mod("banned-words")


@pytest.fixture
def settings(load_mod):
    return defaults(load_mod("banned-words"))


def call(mod, event, settings):
    return asyncio.run(mod.handlers["ai.before_send"](event, make_ctx("banned-words", settings)))


def test_allows_clean_text(mod, settings):
    assert call(mod, before_send_event([("prompt", "a cat")]), settings).decision == "allow"


def test_denies_banned_word(mod, settings):
    decision = call(mod, before_send_event([("prompt", "社外秘の資料を描いて")]), settings)
    assert decision.decision == "deny"
    assert "社外秘" in decision.reason
    assert "prompt" in decision.reason


def test_normalizes_width_and_case(mod, settings):
    event = before_send_event([("negative_prompt", "ＣＯＮＦＩＤＥＮＴＩＡＬ")])
    assert call(mod, event, settings).decision == "deny"


def test_default_skips_local_destinations(mod, settings):
    # 既定は cloud と unset だけを検査する。
    event = before_send_event([("prompt", "confidential")], locality="local")
    assert call(mod, event, settings).decision == "allow"
    event = before_send_event([("prompt", "confidential")], locality="unset")
    assert call(mod, event, settings).decision == "deny"


def test_purpose_filter(mod, settings):
    settings = {**settings, "purposes": "image_run"}
    event = before_send_event([("user", "confidential")], purpose="assistant")
    assert call(mod, event, settings).decision == "allow"
    event = before_send_event([("prompt", "confidential")], purpose="image_run")
    assert call(mod, event, settings).decision == "deny"


def test_role_filter(mod, settings):
    settings = {**settings, "roles": "prompt"}
    event = before_send_event([("system", "confidential"), ("prompt", "a cat")])
    assert call(mod, event, settings).decision == "allow"


def test_words_file(mod, settings, tmp_path):
    words = tmp_path / "words.txt"
    words.write_text("# コメント\nproject-x  # 開発中の名前\n\n", encoding="utf-8")
    settings = {**settings, "words": "", "words_file": str(words)}
    assert call(mod, before_send_event([("prompt", "Project-X logo")]), settings).decision == "deny"
    assert call(mod, before_send_event([("prompt", "project")]), settings).decision == "allow"


def test_missing_words_file_raises(mod, settings, tmp_path):
    # 読めなければ例外にする。GAKEI は送る前のチェックの失敗として扱う(既定は「止める」)。
    settings = {**settings, "words_file": str(tmp_path / "missing.txt")}
    with pytest.raises(FileNotFoundError):
        call(mod, before_send_event([("prompt", "a cat")]), settings)
