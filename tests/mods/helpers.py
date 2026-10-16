"""テストで使うイベントと `ModContext` の組み立て。

`conftest.py` が `gakei_mod` を読めるようにした後に読む。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import gakei_mod


def defaults(mod: Any) -> dict[str, Any]:
    """マニフェストの設定の既定値(GAKEI が画面で保存した値を重ねる前の値)。

    既定値の無い項目は None(GAKEI も同じ)。"""
    return {key: spec.get("default") for key, spec in mod.manifest.get("settings", {}).items()}


class FakeImages:
    """`ctx.open_image` の代わり。sha256 → バイト列。"""

    def __init__(self, images: dict[str, bytes] | None = None) -> None:
        self.images = images or {}

    async def open(self, ref: gakei_mod.ImageRef | str) -> bytes:
        sha = ref.sha256 if isinstance(ref, gakei_mod.ImageRef) else ref
        return self.images[sha]


def make_ctx(mod_id: str, settings: dict[str, Any], images: FakeImages | None = None):
    images = images or FakeImages()
    submitted: list[gakei_mod.Verdict] = []

    async def submit(verdicts: list[gakei_mod.Verdict]) -> int:
        submitted.extend(verdicts)
        return len(verdicts)

    ctx = gakei_mod.ModContext(
        mod_id=mod_id,
        settings=settings,
        open_image=images.open,
        logger=logging.getLogger(f"gakei.mods.{mod_id}"),
        submit_verdicts=submit,
        open_image_with_lineage=images.open,
        get_run=None,
    )
    return ctx


def image(sha256: str, *, asset_id: str | None = "auto", role: str | None = None):
    return gakei_mod.ImageRef(
        asset_id=str(uuid.uuid4()) if asset_id == "auto" else asset_id,
        sha256=sha256,
        width=8,
        height=8,
        bytes=100,
        mime="image/png",
        role=role,
    )


def _envelope(name: str, subject: Any) -> gakei_mod.Event:
    return gakei_mod.Event(
        event=name,
        event_id=str(uuid.uuid4()),
        occurred_at="2026-10-16T03:04:05.123456+00:00",
        instance=gakei_mod.Instance(version="0.0.0", name="test"),
        actor=gakei_mod.Actor(id=str(uuid.uuid4()), email="user@example.com", name="利用者"),
        subject=subject,
    )


def before_send_event(
    texts: list[tuple[str, str]],
    *,
    purpose: str = "image_run",
    locality: str = "cloud",
    images: list[gakei_mod.ImageRef] | None = None,
) -> gakei_mod.Event:
    return _envelope(
        gakei_mod.AI_BEFORE_SEND,
        gakei_mod.BeforeSendSubject(
            purpose=purpose,
            target=gakei_mod.Target(
                provider="openai",
                model="gpt-image-1",
                host="api.openai.com",
                locality=locality,
                locality_source="default",
            ),
            text=[gakei_mod.TextPart(role=r, text=t) for r, t in texts],
            params={"size": "1024x1024"},
            images=images or [],
            run_id=str(uuid.uuid4()),
        ),
    )


def asset_ingested_event(img: gakei_mod.ImageRef, source: str = "upload") -> gakei_mod.Event:
    return _envelope(
        gakei_mod.ASSET_INGESTED,
        gakei_mod.AssetIngestedSubject(asset=img, kind=source, source=source),
    )


def run_finished_event(outputs: list[gakei_mod.ImageRef], prompt: str = "a cat") -> gakei_mod.Event:
    return _envelope(
        gakei_mod.RUN_FINISHED,
        gakei_mod.RunFinishedSubject(
            run_id=str(uuid.uuid4()),
            status="succeeded",
            provider="openai",
            model="gpt-image-1",
            operation="generate",
            prompt=prompt,
            params={"size": "1024x1024", "prompt": prompt},
            outputs=outputs,
        ),
    )
