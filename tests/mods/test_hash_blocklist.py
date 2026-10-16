from __future__ import annotations

import asyncio

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

DENY = "d" * 64
FLAG = "f" * 64
OTHER = "0" * 64


@pytest.fixture
def mod(load_mod):
    return load_mod("hash-blocklist")


@pytest.fixture
def settings(load_mod):
    return {
        **defaults(load_mod("hash-blocklist")),
        "deny_sha256": DENY,
        "flag_sha256": FLAG,
    }


def call(mod, event, settings):
    handler = mod.handlers[event.event]
    return asyncio.run(handler(event, make_ctx("hash-blocklist", settings)))


def test_asset_ingested_deny_and_flag(mod, settings):
    img = image(DENY)
    [verdict] = call(mod, asset_ingested_event(img), settings)
    assert verdict.verdict == "deny"
    assert verdict.target == gakei_mod.VerdictTarget(type="asset", id=img.asset_id)
    assert verdict.label == "禁止の一覧に一致"
    [verdict] = call(mod, asset_ingested_event(image(FLAG)), settings)
    assert verdict.verdict == "flag"
    assert call(mod, asset_ingested_event(image(OTHER)), settings) == []


def test_verdicts_satisfy_the_contract(mod, settings):
    # GAKEI と同じ読み取りで、契約に合うことを確かめる。
    verdicts = call(mod, run_finished_event([image(DENY), image(FLAG)]), settings)
    assert gakei_mod.verdicts_from_json([v.to_json() for v in verdicts]) == verdicts


def test_report_ok(mod, settings):
    settings = {**settings, "report_ok": True}
    [verdict] = call(mod, asset_ingested_event(image(OTHER)), settings)
    assert verdict.verdict == "ok"


def test_run_finished_per_output_and_run_summary(mod, settings):
    outputs = [image(OTHER), image(FLAG), image(DENY)]
    event = run_finished_event(outputs)
    verdicts = call(mod, event, settings)
    by_target = {(v.target.type, v.target.id): v.verdict for v in verdicts}
    assert by_target == {
        ("asset", outputs[1].asset_id): "flag",
        ("asset", outputs[2].asset_id): "deny",
        ("run", event.subject.run_id): "deny",
    }


def test_images_without_asset_id_get_only_run_verdict(mod, settings):
    # テスト送信のダミーの画像は asset_id が null。Asset への判定は作れない。
    event = run_finished_event([image(DENY, asset_id=None)])
    [verdict] = call(mod, event, settings)
    assert verdict.target.type == "run"


def test_list_file_with_labels(mod, settings, tmp_path):
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text(f"# 一覧\n{OTHER.upper()}  社外の素材\nnot-a-hash\n", encoding="utf-8")
    settings = {**settings, "deny_file": str(deny_file)}
    [verdict] = call(mod, asset_ingested_event(image(OTHER)), settings)
    assert (verdict.verdict, verdict.label) == ("deny", "社外の素材")


def test_before_send_blocks_denied_input(mod, settings):
    event = before_send_event([("prompt", "edit")], images=[image(DENY, role="primary")])
    decision = call(mod, event, settings)
    assert decision.decision == "deny"
    assert decision.reference == DENY
    # flag の一覧は送信を止めない。
    event = before_send_event([("prompt", "edit")], images=[image(FLAG, role="primary")])
    assert call(mod, event, settings).decision == "allow"
    settings = {**settings, "block_send": False}
    event = before_send_event([("prompt", "edit")], images=[image(DENY, role="primary")])
    assert call(mod, event, settings).decision == "allow"
