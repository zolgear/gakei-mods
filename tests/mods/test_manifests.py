"""サンプルの Mod のマニフェストと関数が、GAKEI の決まりに合うこと。"""

from __future__ import annotations

import gakei_mod
import pytest

MOD_IDS = ["audit-log", "banned-words", "echo", "hash-blocklist"]


def test_all_mods_are_found(mod_folders):
    assert sorted(mod_folders) == MOD_IDS


@pytest.mark.parametrize("mod_id", MOD_IDS)
def test_required_fields_and_handlers(mod_id, load_mod):
    mod = load_mod(mod_id)
    manifest = mod.manifest
    assert manifest["api_version"] == gakei_mod.API_VERSION
    assert mod.folder.name == manifest["id"]
    assert manifest["version"] and manifest["module"]
    assert set(manifest["events"]) <= set(gakei_mod.EVENTS)
    assert set(mod.handlers) == set(manifest["events"])


@pytest.mark.parametrize("mod_id", MOD_IDS)
def test_gakei_accepts_the_manifest(mod_id, load_mod):
    """GAKEI の検証で確かめる(GAKEI の依存が入った環境のときだけ)。"""
    try:
        from app.mods.loader import parse_manifest
    except ImportError:
        pytest.skip("app.mods.loader を読めない(GAKEI の backend の環境で回すと確かめる)")
    parsed = parse_manifest(load_mod(mod_id).manifest)
    assert parsed.id == mod_id


@pytest.mark.parametrize(
    ("mod_id", "returns_verdicts"),
    [("audit-log", False), ("banned-words", False), ("echo", True), ("hash-blocklist", True)],
)
def test_returns_verdicts(mod_id, returns_verdicts, load_mod):
    assert load_mod(mod_id).manifest.get("returns_verdicts", True) is returns_verdicts
