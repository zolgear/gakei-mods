from __future__ import annotations

import os
from pathlib import Path


def pytest_report_header() -> str:
    backend = os.environ.get("GAKEI_BACKEND", "").strip()
    if backend and (Path(backend) / "app" / "mods" / "api.py").is_file():
        return f"GAKEI_BACKEND: {backend}(Python の Mod のテストも回す)"
    return "GAKEI_BACKEND が無いので、tests/mods(Python の Mod のテスト)は集めない"
