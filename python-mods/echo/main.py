"""エコー(GAKEI の Python の Mod。デバッグ用)。

Mod の連携を作る人のための確認用の Mod。本番では使わない。

- 3つのイベント(`ai.before_send`、`asset.ingested`、`run.finished`)を受け、イベントごとに1行の
  要約をログに出す。設定 `log_payload` がオンなら、封筒全体も整形した JSON で出す。
- 応答(許可・拒否・判定)を、管理者設定の値か、プロンプトなどの文に書いたマーカーで決める。
  マーカーは設定より優先する。

マーカー(大文字・小文字は区別しない。1つの文に複数書ける):

    [[echo:deny]]  [[echo:allow]]  [[echo:flag]]  [[echo:ok]]  [[echo:error]]  [[echo:sleep=5]]

`[[echo:send:deny]]`、`[[echo:ingested:deny]]`、`[[echo:run:flag]]` のように、イベントを
前に付けると、そのイベントだけに効く(付けないものより優先する)。

- `ai.before_send` は allow / deny を読む(flag と ok は無関係なので無視する)。
- `asset.ingested` と `run.finished` は ok / flag / deny を読む(allow は無視する)。
- 応答のマーカーが複数あれば、強いほうを採る(deny > flag > ok > allow)。
- `error` は例外を投げる。`sleep=N` は N 秒待ってから応答する(上限 60 秒)。

設定の値(`ctx.settings`)はログに出さない(ほかの Mod を真似て秘密の項目を足したときに漏らさない
ため)。使うのは `gakei_mod` と標準ライブラリだけ。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from gakei_mod import (
    AI_BEFORE_SEND,
    ASSET_INGESTED,
    RUN_FINISHED,
    AssetIngestedSubject,
    BeforeSendSubject,
    Decision,
    Event,
    ImageRef,
    ModContext,
    RunFinishedSubject,
    Verdict,
    VerdictTarget,
)

# マーカーの sleep の上限(秒)。
MARKER_SLEEP_MAX = 60.0
# 設定 delay_seconds の上限(秒)。GAKEI の待つ時間の上限と同じ。
DELAY_MAX = 300.0

LABEL = "echo"

# イベントの短い名前(マーカーの前置きと fail_events で使う)。
SCOPES = {AI_BEFORE_SEND: "send", ASSET_INGESTED: "ingested", RUN_FINISHED: "run"}
_EVENT_ALIASES = {
    **{name: name for name in SCOPES},
    **{short: name for name, short in SCOPES.items()},
    "before_send": AI_BEFORE_SEND,
    "run_finished": RUN_FINISHED,
}

# イベントごとの応答の設定の項目、選べる値、既定値。
_RESPONSE_SETTINGS: dict[str, tuple[str, tuple[str, ...], str]] = {
    AI_BEFORE_SEND: ("before_send", ("allow", "deny"), "allow"),
    ASSET_INGESTED: ("ingested", ("none", "ok", "flag", "deny"), "none"),
    RUN_FINISHED: ("run_finished", ("none", "ok", "flag", "deny"), "none"),
}

# 応答のマーカーの強さ(複数あれば強いほうを採る)。
_STRENGTH = {"allow": 0, "ok": 1, "flag": 2, "deny": 3}

_MARKER = re.compile(
    r"\[\[\s*echo\s*:\s*(?:(send|ingested|run)\s*:\s*)?([a-z]+)\s*(?:=\s*([^\]\s]*)\s*)?\]\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Marker:
    raw: str  # 書かれたとおりの文字列(ログと理由に出す)
    scope: str | None  # send / ingested / run(付けなければ None)
    action: str
    value: str | None


@dataclass(frozen=True)
class Plan:
    """このイベントへの応答の決め方。`*_from` は出どころ(「設定 …」「マーカー [[…]]」)。"""

    response: str | None  # before_send は allow / deny、判定は ok / flag / deny / None
    response_from: str
    delay: float
    delay_from: str | None
    fail_from: str | None  # 例外を投げるなら、その出どころ


# -- 設定 -------------------------------------------------------------------------------


def _warn(ctx: ModContext, key: str, fallback: Any) -> None:
    # 値そのものは出さない(設定の値はログに出さない決まり)。
    ctx.logger.warning("echo: 設定 %s の値が不正なので、%r として扱います", key, fallback)


def _choice(ctx: ModContext, event_name: str) -> str:
    key, choices, default = _RESPONSE_SETTINGS[event_name]
    raw = ctx.settings.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    value = str(raw).strip().lower() if isinstance(raw, str) else None
    if value not in choices:
        _warn(ctx, key, default)
        return default
    return value


def _bool(ctx: ModContext, key: str, default: bool) -> bool:
    raw = ctx.settings.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    _warn(ctx, key, default)
    return default


def _delay(ctx: ModContext) -> float:
    raw = ctx.settings.get("delay_seconds")
    if raw is None or raw == "":
        return 0.0
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = float(raw)
    except (TypeError, ValueError):
        _warn(ctx, "delay_seconds", 0)
        return 0.0
    if math.isnan(value) or value < 0:
        _warn(ctx, "delay_seconds", 0)
        return 0.0
    if value > DELAY_MAX:
        _warn(ctx, "delay_seconds", DELAY_MAX)
        return DELAY_MAX
    return value


def _fail_events(ctx: ModContext) -> set[str]:
    raw = ctx.settings.get("fail_events")
    if raw is None:
        return set()
    if not isinstance(raw, str):
        _warn(ctx, "fail_events", "")
        return set()
    names: set[str] = set()
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        name = _EVENT_ALIASES.get(item)
        if name is None:
            ctx.logger.warning("echo: 設定 fail_events に知らないイベントがあるので無視します")
            continue
        names.add(name)
    return names


# -- マーカー -----------------------------------------------------------------------------


def find_markers(texts: list[str]) -> list[Marker]:
    found: list[Marker] = []
    for text in texts:
        for m in _MARKER.finditer(text or ""):
            scope = m.group(1).lower() if m.group(1) else None
            found.append(Marker(m.group(0), scope, m.group(2).lower(), m.group(3)))
    return found


def _sleep_seconds(ctx: ModContext, marker: Marker) -> float | None:
    try:
        value = float(marker.value or "")
    except ValueError:
        value = math.nan
    if math.isnan(value) or value < 0:
        ctx.logger.warning("echo: マーカー %s の秒数が不正なので無視します", marker.raw)
        return None
    return min(value, MARKER_SLEEP_MAX)


def make_plan(event_name: str, texts: list[str], ctx: ModContext) -> Plan:
    """設定とマーカーから、このイベントへの応答を決める(マーカーが優先)。"""
    key, _, _ = _RESPONSE_SETTINGS[event_name]
    setting = _choice(ctx, event_name)
    response: str | None = None if setting == "none" else setting
    response_from = f"設定 {key}"
    delay = _delay(ctx)
    delay_from = "設定 delay_seconds" if delay > 0 else None
    fail_from = "設定 fail_events" if event_name in _fail_events(ctx) else None

    if not _bool(ctx, "markers", True):
        return Plan(response, response_from, delay, delay_from, fail_from)

    scope = SCOPES[event_name]
    relevant = [m for m in find_markers(texts) if m.scope in (None, scope)]
    allowed = ("allow", "deny") if event_name == AI_BEFORE_SEND else ("ok", "flag", "deny")
    responses = [m for m in relevant if m.action in allowed]
    # イベントを付けたマーカーがあれば、付けないものより優先する。
    scoped = [m for m in responses if m.scope is not None]
    if scoped:
        responses = scoped
    if responses:
        chosen = max(responses, key=lambda m: _STRENGTH[m.action])
        response, response_from = chosen.action, f"マーカー {chosen.raw}"

    sleeps = [(s, m) for m in relevant if m.action == "sleep" for s in [_sleep_seconds(ctx, m)]]
    sleeps = [(s, m) for s, m in sleeps if s is not None]
    if sleeps:
        seconds, marker = max(sleeps, key=lambda pair: pair[0])
        delay, delay_from = seconds, f"マーカー {marker.raw}"

    errors = [m for m in relevant if m.action == "error"]
    if errors:
        fail_from = f"マーカー {errors[0].raw}"

    known = {"allow", "deny", "ok", "flag", "error", "sleep"}
    for m in relevant:
        if m.action not in known:
            ctx.logger.warning("echo: 知らないマーカー %s を無視します", m.raw)
    return Plan(response, response_from, delay, delay_from, fail_from)


# -- ログ -------------------------------------------------------------------------------


def _image(img: ImageRef) -> str:
    role = f"{img.role}:" if img.role else ""
    return f"{role}{img.sha256[:12]}"


def _images(images: list[ImageRef]) -> str:
    return f"{len(images)}[{', '.join(_image(i) for i in images)}]"


def summarize(event: Event) -> str:
    """イベントの要約(1行)。文は長さだけ出す(中身は log_payload の JSON で見る)。"""
    s = event.subject
    head = f"event={event.event} event_id={event.event_id}"
    if isinstance(s, BeforeSendSubject):
        t = s.target
        texts = ", ".join(f"{p.role}:{len(p.text)}字" for p in s.text)
        return (
            f"{head} purpose={s.purpose} target={t.provider}/{t.model or '-'}"
            f" locality={t.locality}({t.locality_source}) text=[{texts}]"
            f" images={_images(s.images)} run_id={s.run_id or '-'}"
            f" asset_id={s.asset_id or '-'}"
        )
    if isinstance(s, AssetIngestedSubject):
        a = s.asset
        return (
            f"{head} kind={s.kind} source={s.source} asset_id={a.asset_id or '-'}"
            f" sha256={a.sha256[:12]} size={a.width}x{a.height} mime={a.mime}"
            f" run_id={s.run_id or '-'} rating={s.rating or '-'}"
        )
    assert isinstance(s, RunFinishedSubject)
    error = f" error={s.error.code}" if s.error is not None else ""
    return (
        f"{head} status={s.status} target={s.provider}/{s.model} operation={s.operation}"
        f" run_id={s.run_id} prompt={len(s.prompt)}字 inputs={_images(s.inputs)}"
        f" outputs={_images(s.outputs)}{error}"
    )


def _log(event: Event, ctx: ModContext, plan: Plan, extra: str = "") -> None:
    response = plan.response or "none(判定を返さない)"
    parts = [summarize(event), extra, f"応答={response}(出どころ: {plan.response_from})"]
    if plan.delay > 0:
        parts.append(f"待つ={plan.delay:g}秒({plan.delay_from})")
    if plan.fail_from:
        parts.append(f"例外を投げる({plan.fail_from})")
    ctx.logger.info("echo: %s", " ".join(p for p in parts if p))
    if _bool(ctx, "log_payload", True):
        payload = json.dumps(event.to_json(), ensure_ascii=False, indent=2)
        ctx.logger.info("echo: 封筒 %s\n%s", event.event_id, payload)


async def _wait_and_maybe_fail(event: Event, plan: Plan) -> None:
    if plan.delay > 0:
        await asyncio.sleep(plan.delay)
    if plan.fail_from:
        raise RuntimeError(f"echo: {plan.fail_from} による失敗({event.event}。確認用)")


def _verdict(target: VerdictTarget, plan: Plan, event: Event) -> Verdict:
    return Verdict(
        target=target,
        verdict=plan.response,  # type: ignore[arg-type]
        label=LABEL,
        message=f"echo: {plan.response_from} による判定",
        reference=event.event_id,
    )


# -- イベント -----------------------------------------------------------------------------


async def on_ai_before_send(event: Event, ctx: ModContext) -> Decision:
    subject = event.subject
    assert isinstance(subject, BeforeSendSubject)
    plan = make_plan(AI_BEFORE_SEND, [p.text for p in subject.text], ctx)
    _log(event, ctx, plan)
    await _wait_and_maybe_fail(event, plan)
    if plan.response == "deny":
        return Decision.deny(f"echo: {plan.response_from} による拒否", reference=event.event_id)
    return Decision.allow(reference=event.event_id)


async def on_asset_ingested(event: Event, ctx: ModContext) -> list[Verdict]:
    subject = event.subject
    assert isinstance(subject, AssetIngestedSubject)
    texts: list[str] = []
    note = ""
    if subject.run_id and _bool(ctx, "markers", True):
        # 生成の出力なら、その Run のプロンプトからマーカーを読む(アップロードなどは設定だけ)。
        if ctx.get_run is None:
            note = "run_prompt=読めない(get_run なし)"
        else:
            try:
                run = await ctx.get_run(subject.run_id)
            except Exception:  # noqa: BLE001 - 読めなくても設定で応答する
                ctx.logger.exception("echo: Run %s を読めませんでした", subject.run_id)
                run = None
            if run is None:
                note = "run_prompt=読めない"
            else:
                texts.append(run.prompt)
                note = f"run_prompt={len(run.prompt)}字"
    plan = make_plan(ASSET_INGESTED, texts, ctx)
    _log(event, ctx, plan, note)
    await _wait_and_maybe_fail(event, plan)
    if plan.response is None or subject.asset.asset_id is None:
        # テスト送信のダミーの画像は asset_id が null なので、判定の対象にできない。
        return []
    return [_verdict(VerdictTarget(type="asset", id=subject.asset.asset_id), plan, event)]


async def on_run_finished(event: Event, ctx: ModContext) -> list[Verdict]:
    subject = event.subject
    assert isinstance(subject, RunFinishedSubject)
    plan = make_plan(RUN_FINISHED, [subject.prompt], ctx)
    _log(event, ctx, plan)
    await _wait_and_maybe_fail(event, plan)
    if plan.response is None:
        return []
    # 出力の画像ごとと、Run に同じ判定を付ける。Run への deny は記録と表示だけで、画像を残さない
    # 処理は Asset への deny で行われる。
    verdicts = [
        _verdict(VerdictTarget(type="asset", id=image.asset_id), plan, event)
        for image in subject.outputs
        if image.asset_id is not None
    ]
    verdicts.append(_verdict(VerdictTarget(type="run", id=subject.run_id), plan, event))
    return verdicts
