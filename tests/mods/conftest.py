"""Python の Mod のテストの準備。

`gakei_mod` は GAKEI のプロセスの中にしか無い。ここでは、環境変数 `GAKEI_BACKEND` に GAKEI の
`backend/` のフォルダを指定してもらい、その `app/mods/api.py`(`gakei_mod` の本体。標準ライブラリ
だけで書かれている)を `gakei_mod` の名前で読み込む。GAKEI の依存のパッケージは要らない。

- Mod は GAKEI と同じく、フォルダを1つのパッケージとして import する(`load_mod`)。
- マニフェストは、GAKEI の検証(`app.mods.loader.parse_manifest`)が読み込めればそれで確かめ、
  読み込めなければ(GAKEI の依存が無い環境)必須の項目だけを確かめる(`test_manifests.py`)。
- `GAKEI_BACKEND` が無ければ、このフォルダのテストは集めない(見出しにそう出す)。
- テストで使う `ModContext` は本物(`gakei_mod.ModContext`)に、偽の画像の読み出しなどを渡す
  (`helpers.py`)。
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
import tomllib
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
MODS_DIR = REPO / "python-mods"
MANIFEST_NAME = "gakei-mod.toml"
HANDLER_NAMES = {
    "ai.before_send": "on_ai_before_send",
    "asset.ingested": "on_asset_ingested",
    "run.finished": "on_run_finished",
}

_backend = os.environ.get("GAKEI_BACKEND", "").strip()
API_PATH = Path(_backend).resolve() / "app" / "mods" / "api.py" if _backend else None
ENABLED = API_PATH is not None and API_PATH.is_file()

if not ENABLED:
    collect_ignore_glob = ["test_*.py"]
else:
    _spec = importlib.util.spec_from_file_location("gakei_mod", API_PATH)
    assert _spec is not None and _spec.loader is not None
    gakei_mod = importlib.util.module_from_spec(_spec)
    sys.modules["gakei_mod"] = gakei_mod
    _spec.loader.exec_module(gakei_mod)
    # GAKEI の検証(`app.mods.loader`)を試すため(GAKEI の依存が無ければ、その試験だけ飛ばす)。
    sys.path.append(str(API_PATH.parents[2]))


@dataclass
class LoadedMod:
    id: str
    folder: Path
    manifest: dict[str, Any]
    module: types.ModuleType
    handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)


def read_manifest(folder: Path) -> dict[str, Any]:
    with (folder / MANIFEST_NAME).open("rb") as f:
        return tomllib.load(f)


def import_mod(folder: Path) -> LoadedMod:
    """GAKEI と同じく、Mod のフォルダを1つのパッケージとして import する(相対 import が効く)。"""
    manifest = read_manifest(folder)
    mod_id = manifest["id"]
    package = "_gakei_mods_test." + "".join(c if c.isalnum() else "_" for c in mod_id)
    for name in [n for n in sys.modules if n == package or n.startswith(package + ".")]:
        del sys.modules[name]
    if "_gakei_mods_test" not in sys.modules:
        root = types.ModuleType("_gakei_mods_test")
        root.__path__ = []  # type: ignore[attr-defined]
        sys.modules["_gakei_mods_test"] = root
    pkg = types.ModuleType(package)
    pkg.__path__ = [str(folder)]  # type: ignore[attr-defined]
    pkg.__package__ = package
    sys.modules[package] = pkg
    module = importlib.import_module(f"{package}.{manifest['module']}")
    handlers = {}
    for event in manifest["events"]:
        handler = getattr(module, HANDLER_NAMES[event])
        assert inspect.iscoroutinefunction(handler), f"{HANDLER_NAMES[event]} must be async"
        handlers[event] = handler
    return LoadedMod(mod_id, folder, manifest, module, handlers)


@pytest.fixture(scope="session")
def mod_folders() -> dict[str, Path]:
    found = {}
    for folder in sorted(p for p in MODS_DIR.iterdir() if (p / MANIFEST_NAME).is_file()):
        found[read_manifest(folder)["id"]] = folder
    return found


@pytest.fixture(scope="session")
def load_mod(mod_folders: dict[str, Path]) -> Callable[[str], LoadedMod]:
    cache: dict[str, LoadedMod] = {}

    def _load(mod_id: str) -> LoadedMod:
        if mod_id not in cache:
            cache[mod_id] = import_mod(mod_folders[mod_id])
        return cache[mod_id]

    return _load
