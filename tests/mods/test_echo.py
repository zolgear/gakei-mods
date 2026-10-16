from __future__ import annotations

import asyncio
import logging
import types

import gakei_mod
import pytest
from helpers import (
    asset_ingested_event,
    before_send_event,
    defaults,
    image,
    make_ctx,
    run_finished_event,
)

LOGGER = "gakei.mods.echo"


@pytest.fixture
def mod(load_mod):
    return load_mod("echo")


@pytest.fixture
def settings(load_mod):
    return defaults(load_mod("echo"))


@pytest.fixture
def sleeps(mod, monkeypatch):
    """asyncio.sleep の代わりに、待とうとした秒数を覚える(テストでは実際に待たない)。"""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(mod.module, "asyncio", types.SimpleNamespace(sleep=fake_sleep))
    return recorded


def call(mod, event, settings, ctx=None):
    handler = mod.handlers[event.event]
    return asyncio.run(handler(event, ctx or make_ctx("echo", settings)))


def ctx_with_run(settings, prompt: str | None):
    """`get_run` が使える ModContext(prompt が None なら Run が見つからない)。"""
    base = make_ctx("echo", settings)

    async def get_run(run_id: str):
        if prompt is None:
            return None
        return run_finished_event([], prompt=prompt).subject

    return gakei_mod.ModContext(
        mod_id=base.mod_id,
        settings=base.settings,
        open_image=base.open_image,
        logger=base.logger,
        submit_verdicts=base.submit_verdicts,
        get_run=get_run,
    )


# -- ai.before_send ---------------------------------------------------------------------


def test_before_send_default_allows(mod, settings):
    decision = call(mod, before_send_event([("prompt", "a cat")]), settings)
    assert decision.decision == "allow"


def test_before_send_setting_deny(mod, settings):
    settings = {**settings, "before_send": "deny"}
    decision = call(mod, before_send_event([("prompt", "a cat")]), settings)
    assert decision.decision == "deny"
    assert decision.reason == "echo: 設定 before_send による拒否"


def test_before_send_marker_overrides_setting(mod, settings):
    decision = call(mod, before_send_event([("prompt", "a cat [[echo:deny]]")]), settings)
    assert decision.decision == "deny"
    assert decision.reason == "echo: マーカー [[echo:deny]] による拒否"
    settings = {**settings, "before_send": "deny"}
    decision = call(mod, before_send_event([("prompt", "a cat [[ECHO:allow]]")]), settings)
    assert decision.decision == "allow"


def test_before_send_reads_every_text_part(mod, settings):
    event = before_send_event([("prompt", "a cat"), ("negative_prompt", "[[echo:deny]]")])
    assert call(mod, event, settings).decision == "deny"


def test_before_send_ignores_verdict_markers(mod, settings):
    event = before_send_event([("prompt", "[[echo:flag]] [[echo:ok]]")])
    assert call(mod, event, settings).decision == "allow"


def test_deny_wins_over_allow(mod, settings):
    event = before_send_event([("prompt", "[[echo:allow]] [[echo:deny]]")])
    assert call(mod, event, settings).decision == "deny"


def test_scoped_marker_wins_over_unscoped(mod, settings):
    # 送信は通し、取り込みと Run だけ deny にする、という書き方ができる。
    event = before_send_event([("prompt", "[[echo:deny]] [[echo:send:allow]]")])
    assert call(mod, event, settings).decision == "allow"
    # ほかのイベント向けのマーカーは効かない。
    event = before_send_event([("prompt", "[[echo:run:deny]]")])
    assert call(mod, event, settings).decision == "allow"


def test_markers_can_be_turned_off(mod, settings):
    settings = {**settings, "markers": False}
    event = before_send_event([("prompt", "[[echo:deny]] [[echo:error]]")])
    assert call(mod, event, settings).decision == "allow"


# -- 待つ・例外 -------------------------------------------------------------------------


def test_sleep_marker_and_deny(mod, settings, sleeps):
    event = before_send_event([("prompt", "[[echo:sleep=3]] [[echo:deny]]")])
    assert call(mod, event, settings).decision == "deny"
    assert sleeps == [3.0]


def test_sleep_marker_is_capped(mod, settings, sleeps):
    call(mod, before_send_event([("prompt", "[[echo:sleep=999]]")]), settings)
    assert sleeps == [60.0]


def test_sleep_marker_overrides_delay_setting(mod, settings, sleeps):
    settings = {**settings, "delay_seconds": 20}
    call(mod, before_send_event([("prompt", "a cat")]), settings)
    call(mod, before_send_event([("prompt", "[[echo:sleep=1.5]]")]), settings)
    assert sleeps == [20.0, 1.5]


def test_no_sleep_by_default(mod, settings, sleeps):
    call(mod, before_send_event([("prompt", "a cat")]), settings)
    assert sleeps == []


def test_error_marker_raises(mod, settings, sleeps):
    with pytest.raises(RuntimeError, match="マーカー"):
        call(mod, before_send_event([("prompt", "[[echo:sleep=2]][[echo:error]]")]), settings)
    # 待ってから例外を投げる(時間切れと失敗を分けて確かめられる)。
    assert sleeps == [2.0]


@pytest.mark.parametrize("value", ["run.finished", "run", " RUN_FINISHED , foo"])
def test_fail_events_setting(mod, settings, value):
    settings = {**settings, "fail_events": value}
    with pytest.raises(RuntimeError, match="設定 fail_events"):
        call(mod, run_finished_event([image("a" * 64)]), settings)
    # 列挙していないイベントは失敗しない。
    assert call(mod, before_send_event([("prompt", "a cat")]), settings).decision == "allow"


# -- 不正な設定 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("before_send", "block"),
        ("before_send", 1),
        ("ingested", "maybe"),
        ("delay_seconds", -1),
        ("delay_seconds", "abc"),
        ("delay_seconds", True),
        ("markers", "yes"),
    ],
)
def test_invalid_settings_fall_back_with_warning(mod, settings, sleeps, caplog, key, value):
    settings = {**settings, key: value}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert call(mod, before_send_event([("prompt", "a cat")]), settings).decision == "allow"
        assert call(mod, asset_ingested_event(image("a" * 64)), settings) == []
    assert sleeps == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(key in w for w in warnings)


def test_delay_setting_is_capped(mod, settings, sleeps, caplog):
    settings = {**settings, "delay_seconds": 1000}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        call(mod, before_send_event([("prompt", "a cat")]), settings)
    assert sleeps == [300.0]
    assert any("delay_seconds" in r.getMessage() for r in caplog.records)


# -- asset.ingested ---------------------------------------------------------------------


def test_ingested_default_returns_nothing(mod, settings):
    assert call(mod, asset_ingested_event(image("a" * 64)), settings) == []


def test_ingested_setting_verdict(mod, settings):
    img = image("a" * 64)
    [verdict] = call(mod, asset_ingested_event(img), {**settings, "ingested": "deny"})
    assert verdict.target == gakei_mod.VerdictTarget(type="asset", id=img.asset_id)
    assert (verdict.verdict, verdict.label) == ("deny", "echo")
    assert verdict.message == "echo: 設定 ingested による判定"


def test_ingested_reads_markers_from_the_run_prompt(mod, settings):
    img = image("a" * 64)
    event = asset_ingested_event(img, source="generated")
    event = gakei_mod.Event(
        event=event.event,
        event_id=event.event_id,
        occurred_at=event.occurred_at,
        instance=event.instance,
        actor=event.actor,
        subject=gakei_mod.AssetIngestedSubject(
            asset=img, kind="generated", source="generated", run_id="run-1"
        ),
    )
    [verdict] = call(mod, event, settings, ctx_with_run(settings, "a cat [[echo:ingested:flag]]"))
    assert verdict.verdict == "flag"
    assert verdict.message == "echo: マーカー [[echo:ingested:flag]] による判定"
    # Run が見つからなければ設定だけで決める。
    assert call(mod, event, settings, ctx_with_run(settings, None)) == []


def test_ingested_upload_uses_settings_only(mod, settings):
    # アップロードには Run が無い(マーカーを読む文が無い)。
    [verdict] = call(mod, asset_ingested_event(image("a" * 64)), {**settings, "ingested": "ok"})
    assert verdict.verdict == "ok"


def test_ingested_dummy_image_without_asset_id(mod, settings):
    event = asset_ingested_event(image("a" * 64, asset_id=None))
    assert call(mod, event, {**settings, "ingested": "deny"}) == []


# -- run.finished -----------------------------------------------------------------------


def test_run_finished_marker_per_output_and_run(mod, settings):
    outputs = [image("a" * 64), image("b" * 64), image("c" * 64, asset_id=None)]
    event = run_finished_event(outputs, prompt="a cat [[echo:flag]]")
    verdicts = call(mod, event, settings)
    by_target = {(v.target.type, v.target.id): v.verdict for v in verdicts}
    assert by_target == {
        ("asset", outputs[0].asset_id): "flag",
        ("asset", outputs[1].asset_id): "flag",
        ("run", event.subject.run_id): "flag",
    }
    assert all(v.message == "echo: マーカー [[echo:flag]] による判定" for v in verdicts)
    # GAKEI と同じ読み取りで、契約に合う。
    assert gakei_mod.verdicts_from_json([v.to_json() for v in verdicts]) == verdicts


def test_run_finished_ignores_allow_and_strongest_wins(mod, settings):
    event = run_finished_event([image("a" * 64)], prompt="[[echo:allow]]")
    assert call(mod, event, settings) == []
    event = run_finished_event([image("a" * 64)], prompt="[[echo:ok]] [[echo:deny]] [[echo:flag]]")
    assert {v.verdict for v in call(mod, event, settings)} == {"deny"}


def test_run_finished_setting(mod, settings):
    event = run_finished_event([image("a" * 64)])
    verdicts = call(mod, event, {**settings, "run_finished": "ok"})
    assert [v.verdict for v in verdicts] == ["ok", "ok"]


# -- ログ -------------------------------------------------------------------------------


def test_logs_summary_and_payload_but_not_settings(mod, settings, caplog):
    secret = "s3cr3t-value-for-test"
    settings = {**settings, "unrelated_secret": secret}
    with caplog.at_level(logging.INFO, logger=LOGGER):
        call(mod, before_send_event([("prompt", "a cat [[echo:deny]]")]), settings)
    messages = [r.getMessage() for r in caplog.records]
    summary = messages[0]
    assert "event=ai.before_send" in summary
    assert "purpose=image_run" in summary
    assert "target=openai/gpt-image-1" in summary
    assert "locality=cloud" in summary
    assert "応答=deny(出どころ: マーカー [[echo:deny]])" in summary
    # 封筒全体(プロンプトを含む)。
    assert any('"prompt"' in m and "a cat [[echo:deny]]" in m for m in messages[1:])
    assert all(secret not in m for m in messages)


def test_log_payload_off(mod, settings, caplog):
    settings = {**settings, "log_payload": False}
    with caplog.at_level(logging.INFO, logger=LOGGER):
        call(mod, run_finished_event([image("a" * 64)], prompt="a cat"), settings)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "event=run.finished" in messages[0]
    assert "outputs=1[aaaaaaaaaaaa]" in messages[0]
    assert "a cat" not in messages[0]
