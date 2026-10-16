# GAKEI mod samples

Samples for the mod feature of [GAKEI](https://github.com/zolgear/gakei), a self-hosted image generation web tool. Mods let GAKEI call Python code placed on the server (Python mods) or an external system (webhooks) at fixed points (events). The specification is GAKEI's [docs/mods.md](https://github.com/zolgear/gakei/blob/dev/docs/mods.md) (Japanese; design in ADR-0051). The Japanese [README.md](README.md) is the primary document.

## Which sample does what

| Goal | Sample | Kind | Events |
|---|---|---|---|
| Stop prompts that contain banned words before they are sent to an AI | [`python-mods/banned-words`](python-mods/banned-words) | Python mod | `ai.before_send` |
| Log what is sent to AIs, and every ingest and run | [`python-mods/audit-log`](python-mods/audit-log) | Python mod | all three |
| Deny or flag specific images (a SHA-256 list) | [`python-mods/hash-blocklist`](python-mods/hash-blocklist) | Python mod | `asset.ingested`, `run.finished`, `ai.before_send` |
| Check and judge in your own system (separate server) | [`webhook-receivers/python`](webhook-receivers/python) | Webhook | all three |
| Return verdicts later (slow reviews) | [`webhook-receivers/python/submit_verdict.py`](webhook-receivers/python/submit_verdict.py) | Webhook | verdict submission API |
| Receive webhooks in Node.js | [`webhook-receivers/node`](webhook-receivers/node) | Webhook | `ai.before_send` |
| Save generated images to a local folder | GAKEI's bundled "Save to a local folder" (`local-export`) | Python mod | `run.finished`, `asset.ingested` |

For saving images locally, enable GAKEI's bundled mod `local-export` in the admin settings ("Mod"). This repository does not duplicate it; read its code (`backend/app/mods/builtin/local_export/` in GAKEI) as a reference.

- **Python mods** need no extra server, but run inside the GAKEI process with the same privileges (see "Cautions").
- **Webhooks** are received by a separate server in any language. Requests from GAKEI are signed.

## Supported GAKEI versions

- GAKEI with the mod feature (ADR-0051): from the next release; until then the dev image `ghcr.io/zolgear/gakei:dev` (or the `dev` branch).
- Contract `schema_version` 1, Python mod `api_version` 1.
- The manifest key `returns_verdicts` (marks mods that never return verdicts) takes effect on GAKEI versions that read it; older builds ignore it without an error (do not enable "wait for verdict" for such mods there).

## Installing a Python mod

Copy the mod folder under GAKEI's `MODS_DIR` (default `DATA_DIR/mods`).

```bash
mkdir -p /path/to/gakei/data/mods
cp -r python-mods/banned-words /path/to/gakei/data/mods/
```

With Docker, put it in `mods/` of the `DATA_DIR` (`/data`) volume, or mount it read-only:

```bash
docker run -d --name gakei -p 127.0.0.1:8000:8000 \
  -v gakei-data:/data \
  -v "$PWD/python-mods/banned-words:/data/mods/banned-words:ro" \
  ghcr.io/zolgear/gakei:dev
```

If a mod writes files (such as `audit-log`), also mount the destination folder and make it writable by the container's uid 10001.

**Restart GAKEI after adding or changing a mod.** Then, as an administrator, open the admin settings "Mod" (`/settings/mods`), fill in the mod's settings, enable it, and use "Test send" to check the response. No mod runs until it is enabled.

## Python mod samples

All of them use only `gakei_mod` (GAKEI's public module) and the Python standard library.

### `banned-words`

Denies `ai.before_send` when the text to be sent contains a banned word (NFKC-normalized, case-insensitive).

| Setting | Default | Meaning |
|---|---|---|
| `words` | `社外秘,confidential` | Banned words (comma-separated) |
| `words_file` | empty | File with one word per line (`#` starts a comment); see `words.example.txt` |
| `purposes` | empty (all) | Purposes to check: `image_run`, `annotation`, `assistant`, `embedding` |
| `localities` | `cloud,unset` | Destination localities to check: `local`, `cloud`, `unset` |
| `roles` | empty (all) | Text roles to check: `prompt`, `negative_prompt`, `user`, ... |

### `audit-log`

Appends every event as one JSON line to `<log_dir>/audit-YYYY-MM-DD.jsonl` (UTC date of the event). Always allows `ai.before_send`, even if writing fails. For `asset.ingested` and `run.finished` it raises on failure so GAKEI retries; deduplicate by `event_id`. `returns_verdicts = false`.

| Setting | Default | Meaning |
|---|---|---|
| `log_dir` | empty (required) | Absolute path on the GAKEI server |
| `include_text` | on | Record prompts, other text and `params`; off records only length and SHA-256 of text and drops `params` |
| `include_actor` | on | Record the user's id, email and name; off keeps only the id |
| `max_text_chars` | 2000 | Maximum characters per text |

### `hash-blocklist`

Returns `deny` / `flag` verdicts for images whose SHA-256 is on the deny / flag list (`asset.ingested`, and per output plus a run summary for `run.finished`), and denies `ai.before_send` when an input image is on the deny list.

Settings: `deny_file`, `flag_file` (one `sha256 label` per line; see `deny-list.example.txt`), `deny_sha256`, `flag_sha256` (comma-separated), `deny_label`, `flag_label`, `report_ok` (also return `ok`), `block_send`.

Subscribe to `asset.ingested` with **"wait for verdict"** so that denied images are never written to storage. **A `deny` cannot be undone**: GAKEI discards the image (and other images with the same content). Put only images you are sure about on the deny list; use the flag list otherwise.

## Webhook receiver samples

- `webhook-receivers/python/receiver.py` (standard library `http.server`, Python 3.12+): verifies the HMAC-SHA256 signature and timestamp skew (±300 s) on the raw body, accepts JSON and `multipart/form-data`, answers `ai.before_send` with allow/deny and returns verdicts for `asset.ingested` / `run.finished`. Configure with `GAKEI_WEBHOOK_SECRET` (required), `HOST`, `PORT` (8799), `BANNED_WORDS`, `CHECK_LOCALITIES`, `DENY_SHA256`, `FLAG_SHA256`, `SAVE_DIR`.
- `webhook-receivers/python/submit_verdict.py`: sends verdicts later to `POST /api/mods/{webhook id}/verdicts`, signed with the webhook secret.
- `webhook-receivers/node/receiver.mjs` (Node.js 24, no dependencies): verifies the signature and answers `ai.before_send`.

Register the receiver URL and secret in the admin settings "Mod", then use "Test send". Put receivers behind TLS termination in production.

## JSON Schema

[`schema/mods-schema.json`](schema/mods-schema.json) is the JSON Schema (2020-12) of the events, responses and the verdict submission body, exported from GAKEI's backend:

```bash
cd backend
uv run python -m app.tools.export_mod_schema - > /path/to/gakei-mods/schema/mods-schema.json
```

## Tests

```bash
uv run pytest                                          # webhook receiver tests; Python mod tests are skipped
GAKEI_BACKEND=/path/to/gakei/backend uv run pytest     # also test the Python mods
cd webhook-receivers/node && npm test                  # Node.js receiver tests
```

The mod tests load `app/mods/api.py` from `GAKEI_BACKEND` as `gakei_mod` (it only needs the standard library), and call the mod functions with a real `ModContext` and fake image readers. No test talks to an external network.

## Cautions

- **Python mods run inside the GAKEI process with the same privileges as GAKEI.** They can read and write the database, images and `secrets.json` (including API keys), and send them elsewhere. There is no sandbox. Install only mods you have read and trust.
- **Events contain the user's id, email and display name, as well as prompts and images.** Enabling a webhook or a mod that sends data out passes them to that system; confirm how it handles them first.
- With "block" on failure, an outage of the checking system also stops generation, auto titles/tags, the assistant and remote embeddings.
- These are samples; adapt them to your requirements.

## License

[Apache License 2.0](LICENSE), same as GAKEI.
