from __future__ import annotations

import asyncio
import json

import pytest
from helpers import (
    asset_ingested_event,
    before_send_event,
    defaults,
    image,
    make_ctx,
    run_finished_event,
)


@pytest.fixture
def mod(load_mod):
    return load_mod("audit-log")


@pytest.fixture
def settings(load_mod, tmp_path):
    return {**defaults(load_mod("audit-log")), "log_dir": str(tmp_path / "logs")}


def call(mod, event, settings):
    handler = mod.handlers[event.event]
    return asyncio.run(handler(event, make_ctx("audit-log", settings)))


def read_lines(settings):
    path = next(iter(sorted((__import__("pathlib").Path(settings["log_dir"])).glob("*.jsonl"))))
    assert path.name == "audit-2026-10-16.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_records_all_three_events(mod, settings):
    decision = call(mod, before_send_event([("prompt", "a cat")]), settings)
    assert decision.decision == "allow"
    assert call(mod, asset_ingested_event(image("a" * 64)), settings) is None
    assert call(mod, run_finished_event([image("b" * 64)]), settings) is None
    lines = read_lines(settings)
    assert [line["event"] for line in lines] == ["ai.before_send", "asset.ingested", "run.finished"]
    assert lines[0]["subject"]["text"] == [{"role": "prompt", "text": "a cat"}]
    assert lines[0]["actor"]["email"] == "user@example.com"
    assert all("event_id" in line and "logged_at" in line for line in lines)


def test_redacts_text_and_actor(mod, settings):
    settings = {**settings, "include_text": False, "include_actor": False}
    call(mod, before_send_event([("prompt", "secret plan")]), settings)
    call(mod, run_finished_event([image("b" * 64)], prompt="secret plan"), settings)
    before, run = read_lines(settings)
    assert before["subject"]["text"][0]["text"]["chars"] == len("secret plan")
    assert "params" not in before["subject"]
    assert run["subject"]["prompt"]["sha256"] == before["subject"]["text"][0]["text"]["sha256"]
    assert "params" not in run["subject"]
    assert set(before["actor"]) == {"id"}
    assert "secret plan" not in json.dumps([before, run])


def test_truncates_long_text(mod, settings):
    settings = {**settings, "max_text_chars": 5}
    call(mod, before_send_event([("prompt", "abcdefghij")]), settings)
    assert read_lines(settings)[0]["subject"]["text"][0]["text"] == "abcde…"


def test_before_send_allows_even_if_logging_fails(mod, settings):
    settings = {**settings, "log_dir": ""}
    assert call(mod, before_send_event([("prompt", "a cat")]), settings).decision == "allow"


def test_async_events_raise_when_logging_fails(mod, settings):
    # 非同期のイベントは例外にして、GAKEI に送り直してもらう。
    settings = {**settings, "log_dir": ""}
    with pytest.raises(RuntimeError):
        call(mod, asset_ingested_event(image("a" * 64)), settings)
