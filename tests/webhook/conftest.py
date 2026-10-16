"""Webhook の受け口(webhook-receivers/python)のテストの準備。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "webhook-receivers" / "python"))
