# ComfyUI Gateway — Agent Integration Guide

Canonical reference for programs and agents that drive `comfyui_server.py` over the LAN.

- **Base URL:** `http://<host>:5000` (the server binds `0.0.0.0:5000`)
- **This document:** `GET /agent_guide.md` (raw Markdown) · `GET /agent_guide` (HTML)
- **Machine-readable companions:** `GET /api/workflows` · `GET /api/endpoints` · `GET /openapi.json`

Everything below is derived from the actual implementation in `comfyui_server.py`,
`comfyui_workflow.py`, `comfyui_websocket.py`, and the `workflows/*/workflows.config.json` files.

---

## 1. Read this first — the rules that prevent most failures

| # | Rule | Why it matters |
|---|------|----------------|
| 1 | **Every submission gets a `job_id`. Poll and cancel by that id.** | Jobs are queued: ComfyUI runs one prompt at a time, the gateway keeps one record per job. A second submission no longer destroys the first job's state — but a poll without a `job_id` is ambiguous as soon as two jobs are active and fails with `409`. |
| 2 | **Wait for `state == "done"` (or `progress == "Done"`).** Not `"done"`, not `"Done!"`. | `state` is `queued`, `running`, `done`, `error` or `cancelled`. The literal string set on completion is `Done`. Anything else never matches and you poll forever. |
| 3 | **Use `filepath`, not `file_url`, from `/find_workflow_outputs`.** | `file_url` is emitted as `/download_file/<path>`, but the route only accepts the query form `/download_file?filepath=...`. The `file_url` string 404s. Build the URL yourself from `filepath`. |
| 4 | **Base64 images must be raw base64 — no `data:` prefix.** | `/comfyui_workflow` sniffs each string with a Pillow decode. A `data:image/jpeg;base64,` prefix fails the sniff, so the value is passed through verbatim as a filename and the job fails. |
| 5 | **Images only on the JSON endpoint.** Video and audio must use the multipart endpoint. | Only image payloads are auto-decoded and written to ComfyUI's input dir. |
| 6 | **Resolve `$placeholders` yourself before POSTing.** | The gateway does no `$` substitution. A literal `"$positive"` is written into the prompt as-is. See §4. |
| 7 | **`seed` is always randomised.** | Any `seed` you send is overwritten with a fresh random value in `1 .. 2^31-1`. Send it only for readability. The upper bound is the smallest max any seed input declares (the OpenAI API `Seed` option node), so the value is accepted by every workflow. |
| 8 | **Use the exact `<name>_api` key where exactness is required.** | `/find_workflow_outputs` and `/comfyui_runflow` do **no** `_api` fallback. See §3. |
| 9 | **Cancel with `POST /comfyui_cancel/{job_id}`, never with a global kill.** | The cancel is scoped to that job: a running job is interrupted, a queued job is dequeued. `POST /comfyui_interrupt_all` kills whatever ComfyUI runs — including jobs started from the ComfyUI web UI. |

---

## 2. Connecting

The gateway is stateless HTTP. No auth, no API key, no session token. Base your client on one host constant:

```bash
GW="http://192.168.1.10:5000"
curl -s "$GW/api/jobs"
```

Health check before any job:

```bash
curl -s "$GW/start_comfyui"     # idempotent: "started" or "already started"
curl -s "$GW/api/jobs"          # {"jobs": [...]} - every job the gateway knows
```

`POST /comfyui_workflow` returns **HTTP 503** if ComfyUI is not running. If you get 503, call
`GET /start_comfyui` and retry after ComfyUI finishes booting on port 8188.

> Note: the server must be started from the repository root (`run.bat` / `run.sh`), because both
> `templates/` and the workflow JSON paths in `workflows.config.json` are resolved relative to the
> current working directory.

---

## 3. Discovery — what can I run?

There are two distinct layers. Confusing them is the most common source of wrong payloads.

### 3.1 Presets (`/mode_config`) — high-level, ready-made configurations

`GET /mode_config` returns a merged JSON array. It concatenates the repo-root `mode_config.json`
with every `workflows/**/mode.config.json`.

```json
[
  {
    "name": "zImageJorden",
    "title": "Z-Image Turbo Jorden txt2img",
    "showT2I": true,
    "show": true,
    "configurable": true,
    "default": {"type": "txt2img", "cfgScale": 1, "steps": 9, "sdSize": 1024},
    "fields": {
      "workflow": "zImage_txt2img",
      "positive": "$positive",
      "negative": "$negative",
      "batch_size": "$batchSize",
      "width": "$width",
      "height": "$height",
      "steps": "$steps",
      "cfg": "$cfg",
      "lora": "zImage\\Jorden_zImage_Turbo.safetensors",
      "lora_str": 0.8,
      "unet": "zImage\\z_image_turbo_int8_convrot.safetensors"
    }
  }
]
```

Semantics of a preset's `fields`:

- **Literal values** (e.g. `"lora": "zImage\\Jorden_zImage_Turbo.safetensors"`, `"lora_str": 0.8`)
  are fixed tuning for that preset — pass them through unchanged.
- **`"$name"` values are placeholders you must resolve.** Substitute from the user request, then
  fall back to the preset's `default` object.
- **`fields.workflow` is the bare workflow name** (`zImage_txt2img`). The actual config key is
  `zImage_txt2img_api`. Append `_api`, or rely on the fallback described next.
- A placeholder you cannot fill: **omit the key** rather than sending an empty string. Omitting
  keeps the workflow's baked-in default; sending `""` overwrites it with nothing.

The placeholder vocabulary is small and fixed across presets, and its `default` keys are named
differently from the fields they feed:

| `$placeholder` | Reads from `default` | Notes |
|---|---|---|
| `$positive` | *(none — user text)* | The main prompt. Always required in practice. |
| `$negative` | *(none — user text)* | Negative prompt |
| `$background` | `baseImage` | Image to transform. Base64 image file. |
| `$mask` | `baseImage` | Inpaint mask. Base64 image file. |
| `$reference` | *(none)* | Secondary reference image |
| `$batchSize` | *(none)* | Writes to the `batch_size` field |
| `$steps` | `steps` | Iteration count |
| `$cfg` | `cfgScale` | Guidance / CFG |
| `$denoise` | `denoise` | img2img / inpaint strength |
| `$maskBlur` | `maskBlur` | Mask feather, in pixels |
| `$size` | `sdSize` | Single size field (img2img, inpaint, pose presets) |
| `$width` / `$height` | `sdSize` | Split into two fields (txt2img presets) — both derive from `sdSize` |

`$size` and `$width`/`$height` are alternatives: whichever pair a preset actually uses is the one
present in its `fields`, so drive it from `sdSize` and let the preset's shape decide the split.

Unknown placeholders such as `$type` or `$guidance` in a `default` block have no counterpart in
`fields` — they belong to the host application, not the gateway. Ignore them.

### 3.2 Workflows (`/api/workflows`) — the executable layer

`GET /api/workflows` returns every workflow found across `workflows/**/workflows.config.json`,
keyed by its exact config name, including the `fields` map that decides which payload keys are
meaningful and the `output` map that decides how to fetch results.

```bash
curl -s "$GW/api/workflows" | python -c "import json,sys; d=json.load(sys.stdin); print(list(d))"
```

Each entry looks like:

```json
{
  "zImage_txt2img_api": {
    "prompt_workflow": "E:\\Workspace\\ComfyuiGW\\workflows\\azImage\\zImage_txt2img_api.json",
    "fields": {"positive": "35.inputs.text", "negative": "7.inputs.text", "seed": "44.inputs.seed"},
    "has_inputs": false,
    "has_output_config": false,
    "descriptions": null,
    "output": null
  }
}
```

Interpretation:

- **`fields`** — the authoritative list of accepted payload keys. A key you send that is absent from
  `fields` is silently ignored. `fields` values are ComfyUI node paths (`"35.inputs.text"`); the
  3-part form `"114:78.inputs.text"` denotes a nested node id. A list value means "write this key to
  several nodes" (e.g. one LoRA strength into two inputs).
- **`has_inputs`** — the workflow can be driven from the web form / multipart endpoint. Workflows
  with only a `fields` map (most image workflows) are JSON-endpoint only.
- **`has_output_config`** — the workflow declares an `output` block, so results are retrievable via
  `/find_workflow_outputs`. If `false`, fall back to `/comfyui_result` (§6).
- **`descriptions`** — human blurb; `null` when absent.

> **Known quirk — `_inputs`.** A few entries spell the block `"_inputs"` instead of `"inputs"`.
> That is not a typo to work around: it is the convention used to *disable* a workflow's web form.
> Treat `has_inputs: false` as authoritative.

### 3.3 Workflow name resolution — which suffix to send where

| Endpoint | Resolution behaviour | What to send |
|---|---|---|
| `POST /comfyui_workflow` | tries `<name>_api` first, falls back to `<name>` | Either form works |
| `POST /comfyui_caption` | tries `<name>_api` first, falls back to `<name>` | Either form works |
| `POST /comfyui_runflow` | **exact match only** | Must be `zImage_txt2img_api` style (with `_api`) |
| `GET /find_workflow_outputs/{workflow}` | **exact match only** | Must be `zImage_txt2img_api` style (with `_api`) |
| `GET /workflow/{workflow}` (HTML) | always appends `_api` | Bare name: `zImage_txt2img` |

Underscore rule: for the two exact-match endpoints, take the key exactly as it appears in
`/api/workflows`. Do not strip or add the suffix yourself.

---

## 4. Running a workflow

### 4.1 Text-to-image, JSON endpoint (recommended for agents)

`POST /comfyui_workflow`, `Content-Type: application/json`. This is the primary programmatic entry
point.

```bash
curl -s -X POST "$GW/comfyui_workflow" \
  -H "Content-Type: application/json" \
  -d '{
        "workflow": "zImage_txt2img_api",
        "positive": "a cyberpunk skyline at dusk, wide angle",
        "negative": "blurry, low quality",
        "width": 1280,
        "height": 720,
        "steps": 9,
        "cfg": 1,
        "batch_size": 1
      }'
# -> {"status":"Queued","job_id":"0f1c…","state":"queued","workflow":"zImage_txt2img_api",
#     "queue_position":1,"queue_length":1}
```

Keep the returned `job_id` — it is the handle for polling (§5) and cancelling (§5, "Cancelling").
The response is returned as soon as the prompt is accepted by ComfyUI's queue; the job may still
be waiting behind another one.

Rules for this endpoint:

- `workflow` is the only required key.
- Any string value that sniffs as a valid base64 **image** is written into ComfyUI's `input/`
  directory as `<key>_<job_id>.<ext>` and replaced with the absolute server-side path. Identical
  bytes already present are reused instead of re-saved.
- Non-image strings are passed through untouched — which is exactly why the `data:` prefix rule
  matters.
- Numbers, booleans, and JSON arrays pass through as-is. Send `array`-typed fields as real JSON
  arrays; the comma-splitting behaviour described in §4.2 belongs to the multipart path only.
- Omitting a key leaves the workflow's own default in place. Fields not present in `fields` are
  ignored.
- An optional `X-Client-Id` header is echoed back in the job record, so you can group your own
  jobs (`GET /api/jobs?client_id=…`).

### 4.2 Image / video / audio, multipart endpoint

`POST /comfyui_runflow`, `Content-Type: multipart/form-data`. Required whenever the workflow takes
a **video** or **audio** input — those bytes cannot be smuggled through the JSON endpoint.

```bash
curl -s -X POST "$GW/comfyui_runflow" \
  -F "workflow=fl2v_h3_api" \
  -F "start_img=@first_frame.png" \
  -F "last_img=@last_frame.png" \
  -F "positive=walking through neon rain" \
  -F "duration=5"
```

This endpoint **requires the workflow to declare an `inputs` block** (`has_inputs: true` in
`/api/workflows`) — otherwise it fails with **400** and a message that says so. An unknown
workflow name fails with **404**.

Coercion applied to form fields, per the `inputs` declaration:

| `type` | Form encoding | Result sent to ComfyUI |
|---|---|---|
| `image`, `video`, `audio` | file part | file uploaded, original extension preserved |
| `integer` | text field | cast to `int` |
| `float` | text field | cast to `float` |
| `array` | `a,b,c` | list of stripped strings |
| `text`, `string` | text field | raw string |

The response depends on the `Accept` header:

- **Default** (`Accept: text/html` or absent): an **HTML progress page**, like the web form gets.
  Ignore the body and poll §5 with the `job_id` it embeds.
- **`Accept: application/json`**: the same envelope as `/comfyui_workflow` —
  `{"status":"Queued","job_id":"…","state":"queued","workflow":"…","queue_position":1,"queue_length":1}`.
  Use this when you drive the multipart endpoint from a program.

```bash
curl -s -X POST "$GW/comfyui_runflow" \
  -H "Accept: application/json" \
  -F "workflow=fl2v_h3_api" \
  -F "start_img=@first_frame.png" \
  -F "last_img=@last_frame.png" \
  -F "positive=walking through neon rain" \
  -F "duration=5"
# -> {"status":"Queued","job_id":"…","state":"queued", …}
```

If ComfyUI is down this endpoint returns HTTP 200 with `{"message": "ComfyUI is not started."}`
— check the content type before assuming you got the job envelope.

### 4.3 Optional fields and automatic node stripping

Inputs declared `"optional": true` gain real behaviour. When an optional field is omitted or empty,
the gateway removes its source node from the prompt and cascades the removal through any consumer
nodes that become fully orphaned — a branch of the graph is deleted only when nothing else feeds it.
Fields without `"optional": true` are never stripped; their node keeps its baked-in default instead.

Practical effect: for workflows with many optional reference slots (e.g. `ref2v_h3_api` accepts
`image1`…`image9`, `video1`, `audio1`…`audio3`), send only what you need and omit the rest. Do not
send empty strings to "skip" them.

### 4.4 Reference-image auto-tagging

Workflows with `"auto_image_tags": true` in their config have `<Picture N>` tags prepended to
`positive` automatically, ordered by the numeric suffix of the `image<N>` keys you supplied. This
lets prompts refer to attachments positionally. You do not add the tags yourself.

### 4.5 Captioning (blocking)

`POST /comfyui_caption` with `{"workflow": ..., "background": "<base64 image>"}` runs a caption
workflow and **blocks inside the request** until the caption file of *that job* appears, then
returns `{"caption": "...", "job_id": "..."}`. The caption is read from the job's own output
files, so two caption jobs running at the same time never mix their text up.

The wait is bounded: after **900 seconds** the request returns
`{"caption": "", "error": "timeout", "job_id": "..."}`. If the workflow fails, the response carries
an `error` string instead of a caption. Use a client timeout of at least 900 s.

> Prefer the job flow below — it does not block and the job can be stopped.

### 4.6 Text output (image + text → text)

Workflows that write a text file (captions, tags, generated video prompts) are marked with
`"produces_text": true` in `/api/workflows`. They are driven like any other workflow:

```
POST /comfyui_workflow   {"workflow": "caption_api", "image1": "<base64>", "prompt": "..."}
   → {"job_id": "…"}
GET  /comfyui_status?job_id=…          → poll until "state": "done"
GET  /comfyui_text?job_id=…             → {"job_id", "state", "text", "error", "files"}
```

The text workflows share one key convention, so a client can fill them in without knowing the
workflow:

| Key | Meaning |
|---|---|
| `image1` | the background image |
| `image2` | the reference image |
| `image3`…`image9` | further reference images |
| `prompt` | the positive prompt |

A workflow only receives the keys it declares in its `fields` — `caption_api` asks for `image1`
and `prompt`, `video_prompt_MiniMax_H3_3i_api` for `image1`…`image3` and `prompt`, and
`video_prompt_MiniMax_H3_ref_api` for `prompt` only. Keys that are not declared are ignored, and
optional keys that are left out are stripped from the prompt (§4.3).

`GET /comfyui_text` returns the text the job produced, read from that job's own output files. Use
`POST /comfyui_cancel/{job_id}` to stop the job while it runs.

---

## 5. Job lifecycle

```
submit ──► job_id ──► poll /comfyui_status?job_id=… ──► state == "done" ──► fetch outputs ──► download files
```

Every submission returns a `job_id` (§4.1). The gateway keeps one record per job, and ComfyUI
runs the jobs one after another, so a second submission waits instead of overwriting the first
one's state.

Poll roughly once per second. The response carries the state, a message and a step counter with
an ETA:

```json
{
  "job_id": "0f1c…",
  "state": "running",
  "progress": "Processing \"KSampler\" (12/41)...",
  "step": "--> Step: 7/20 [00:31<00:58]",
  "queue_position": null,
  "queue_length": 0,
  "error": null,
  "workflow": "zImage_txt2img_api",
  "submitted_at": 1767225400.0, "started_at": 1767225401.2, "ended_at": null
}
```

```bash
until [ "$(curl -s "$GW/comfyui_status?job_id=$JOB" | python -c 'import json,sys; print(json.load(sys.stdin)["state"])')" = "done" ]; do
  curl -s "$GW/comfyui_status?job_id=$JOB"; echo
  sleep 1
done
```

`state` is one of:

| `state` | Meaning | Keep polling? |
|---|---|---|
| `queued` | Accepted by ComfyUI, waiting behind other jobs. `queue_position` says how far back. | yes |
| `running` | Executing now. | yes |
| `done` | Finished. `progress` is `Done`. Fetch the outputs. | no |
| `error` | ComfyUI reported an error. `error` holds the message. | no |
| `cancelled` | The job was cancelled. | no |

**Polling without a `job_id`.** `GET /comfyui_status` without one still works while exactly one
job is active. With several active jobs it fails with **409** and a `detail` that asks for the
`job_id` — it never silently answers with another job's data. The same rule applies to
`/comfyui_result`.

**Cancelling.** `POST /comfyui_cancel/{job_id}` stops exactly that job: a running job is
interrupted, a queued job is removed from the queue without running. The response is
`{"job_id": "…", "cancelled": true, "state": "cancelled"}`. Cancelling a job that already
finished returns `200` with `"cancelled": false` and the real terminal state; cancelling an id
the gateway never issued returns **404**.

`GET /comfyui_interrupt?job_id=…` is an alias for the same call and **requires** the `job_id`
(without it: **400**). `POST /comfyui_interrupt_all` is the only global kill — it stops whatever
ComfyUI is running, including jobs started from the ComfyUI web UI or another device.

**Timeouts.** Video workflows routinely run for many minutes. Use a generous ceiling (10–30 min)
and treat exceeding it as inconclusive, then `GET /comfyui_status` once more before deciding.
A job that ComfyUI lost (backend restart) comes back with `state: "error"` and
`error: "ComfyUI restarted; job lost"`.

---

## 6. Retrieving results

Two endpoints, and **which one works depends on the workflow's configuration**. Check
`has_output_config` in `/api/workflows`.

### 6.1 `/find_workflow_outputs/{workflow}` — the modern path

Requires `has_output_config: true` **and** the exact `_api` key.

```bash
curl -s "$GW/find_workflow_outputs/ref2v_h3_extend_api?job_id=$JOB"
```

```json
{
  "files": [
    {
      "filename": "H3_extend_clip_00001_.webm",
      "filepath": "E:\\Workspace\\ComfyUI\\output\\H3_extend_clip_00001_.webm",
      "extension": "webm",
      "mod_time": 1767225600.0,
      "file_url": "/download_file/E%3A%5CWorkspace%5C..."
    }
  ],
  "has_output_config": true,
  "debug": {"workflow": "ref2v_h3_extend_api", "job_id": "0f1c…", "workflow_start_time": 1767225400.0, "...": "..."}
}
```

- Only files produced **during that job** are returned, so results from previous runs never leak
  in. Pass `?job_id=…` to scope the listing to one job; without it the gateway falls back to the
  single active job, or to every matching file when several jobs are active.
- `file_url` is present but **broken** — the route expects a query parameter. Build the download URL
  from `filepath` instead:
  `"/download_file?filepath=" + urllib.parse.quote(filepath)`.
- When `has_output_config` is `false` you get `{"files": [], "has_output_config": false, ...}`.
  That is a configuration fact, not an error — switch to `/comfyui_result`.
- When the job produced nothing you get `files: []` with `has_output_config: true`. The `debug`
  block shows the resolved search directory and start time, which is the fastest way to diagnose.
  Note that ComfyUI caches node outputs: re-running an identical prompt can reuse a cached node and
  therefore not rewrite the output file.

### 6.2 `/comfyui_result` — base64 images only

```bash
curl -s "$GW/comfyui_result?job_id=$JOB"
# -> {"job_id": "…", "state": "done", "processed_image": ["<base64 jpg>", "<base64 jpg>"], "files": [...]}
```

The key is **`processed_image`** (a list of raw base64 strings) — *not* `images*.

Constraints, all of which will bite you:

- Only files directly inside ComfyUI's `output/` directory whose name **starts with `api`** and
  **ends with `.jpg`** are returned. Most workflows use a different save prefix (`H3`, `i2v_wan`,
  `v2v_save`, …), so this endpoint returns `[]` for them.
- Subdirectories are not scanned.
- The images are the ones the **given job** produced. Pass `?job_id=…`; without it the endpoint
  serves the single active job, and answers **409** when that is ambiguous.

Decode the strings:

```python
import base64
raw = requests.get(f"{GW}/comfyui_result?job_id={job_id}").json()["processed_image"]
for i, b64 in enumerate(raw):
    open(f"out_{i}.jpg", "wb").write(base64.b64decode(b64))
```

### 6.3 Result routing cheat sheet

| Situation | Endpoint |
|---|---|
| `/api/workflows` shows `has_output_config: true` | `GET /find_workflow_outputs/{key}?job_id=…` |
| Image workflow with no `output` block, saves as `api*.jpg` | `GET /comfyui_result?job_id=…` |
| Video / audio / text output | `GET /find_workflow_outputs/{key}?job_id=…` (needs an `output` block) |
| Anything produced a `.txt` (e.g. captions) | `/find_workflow_outputs`, then download and read |

### 6.4 Downloading files

```bash
# Build from the `filepath` field, URL-encoding it
python -c "
import urllib.parse, urllib.request
p = r'E:\Workspace\ComfyUI\output\H3_extend_clip_00001_.webm'
urllib.request.urlretrieve('http://192.168.1.10:5000/download_file?filepath=' + urllib.parse.quote(p), 'out.webm')
"
```

`GET /download_file?filepath=<absolute path>` streams the file with
`Content-Disposition: filename="<basename>"`. It resolves against the whole filesystem — it is not
confined to the output directory, so only pass paths the server handed you.

`GET /find_latest_runflow` returns the newest `runflow*` file in the output directory. Its
`file_url` has the same broken path-style form as §6.1; use the returned `filepath` instead.
Pass `?job_id=…` to restrict the search to the files of one job.

---

## 7. Endpoint reference

### Discovery & docs

| Method | Path | Returns |
|---|---|---|
| GET | `/` | HTML index linking every entry point |
| GET | `/agent_guide` | This guide, rendered HTML |
| GET | `/agent_guide.md` | This guide, raw Markdown |
| GET | `/api/workflows` | JSON map of workflow name → fields / inputs / output metadata (`produces_text` marks text workflows) |
| GET | `/api/endpoints` | JSON inventory of routes with purpose notes |
| GET | `/openapi.json`, `/docs`, `/redoc` | FastAPI's generated schema and Swagger UI |

### Execution

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/comfyui_workflow` | JSON: `{"workflow": str, ...fields}` | `{"status":"Queued","job_id": str, "state": str, …}` |
| POST | `/comfyui_runflow` | multipart: `workflow` + declared `inputs` | HTML progress page, or the job envelope with `Accept: application/json` |
| POST | `/comfyui_caption` | JSON: `{"workflow": str, "background": base64}` | `{"caption": str, "job_id": str}` (blocking, 900 s cap) |
| GET | `/comfyui_text?job_id=` | `{"job_id", "state", "text", "error", "files"}` — the text a job produced |

### Status, control, results

| Method | Path | Returns / effect |
|---|---|---|
| GET | `/comfyui_status?job_id=` | `{"job_id", "state", "progress", "step", "queue_position", "queue_length", "error", …}` |
| GET | `/comfyui_result?job_id=` | `{"job_id", "state", "processed_image": [base64 jpg], "files": [...]}` |
| POST | `/comfyui_cancel/{job_id}` | Cancels that job only: `{"job_id", "cancelled": bool, "state": str}` |
| GET | `/comfyui_interrupt?job_id=` | Alias for the cancel; **requires** `job_id` (400 without it) |
| POST | `/comfyui_interrupt_all` | Kills whatever ComfyUI is running, whoever submitted it |
| GET | `/api/jobs` | `{"jobs": [job record, …]}` — optional `?client_id=` / `?state=` filters |
| GET | `/api/jobs/{job_id}` | One job record plus its resolved `outputs` |
| GET | `/comfyui_free` | Unloads models and frees VRAM |
| GET | `/start_comfyui` | Starts ComfyUI (idempotent) |
| GET | `/stop_comfyui` | Stops ComfyUI |
| GET | `/find_workflow_outputs/{workflow}?job_id=` | `{"files": [...], "has_output_config": bool, "debug": {...}}` |
| GET | `/find_latest_runflow?job_id=` | `{"file_url", "filename", "filepath"}` |
| GET | `/download_file?filepath=` | File stream |
| GET | `/loras` | JSON array of LoRA relative paths |

### Web UI

| Method | Path | Purpose |
|---|---|---|
| GET | `/runflow` | Workflow browser, grouped by directory |
| GET | `/runflow/{path}` | Workflow browser for a subdirectory |
| GET | `/workflow/{name}` | Generated HTML form for a workflow (bare name, no `_api`) |
| GET | `/workflow_status?job_id=` | Standalone progress page; follows the given job, or the single active one |
| POST | `/comfyui_runflow` | Form submission target — each submission is its own job |

---

## 8. Reference client

Self-contained Python client implementing the full lifecycle. Requires only `requests`.

```python
#!/usr/bin/env python3
"""Minimal ComfyUI Gateway client. See docs/AGENT_API_GUIDE.md."""
import base64
import mimetypes
import os
import sys
import time
import urllib.parse

import requests

GW = os.environ.get("GW", "http://192.168.1.10:5000")


class GatewayError(RuntimeError):
    pass


def _check(resp):
    if not resp.ok:
        raise GatewayError(f"{resp.request.method} {resp.url} -> {resp.status_code}: {resp.text[:500]}")
    return resp.json() if resp.content else None


def b64_image(path):
    """Raw base64 (no data: URI prefix) — the gateway sniffs images with Pillow."""
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def ensure_running():
    _check(requests.get(f"{GW}/start_comfyui", timeout=30))


def catalog():
    """workflow name -> {'fields': {...}, 'has_inputs': bool, 'has_output_config': bool, ...}"""
    return _check(requests.get(f"{GW}/api/workflows", timeout=30))


def presets():
    """Merged mode presets: [{'name','title','default','fields': {...}}]"""
    return _check(requests.get(f"{GW}/mode_config", timeout=30))


def run(workflow, **fields):
    """Submit a job. Image kwargs may be file paths; they are base64-encoded for you.

    Returns the job envelope; keep the `job_id` - it identifies the job from
    here on.
    """
    payload = {"workflow": workflow}
    for key, value in fields.items():
        if isinstance(value, str) and os.path.isfile(value):
            value = b64_image(value)
        payload[key] = value
    return _check(requests.post(f"{GW}/comfyui_workflow", json=payload, timeout=120))


def run_multipart(workflow, files=None, **fields):
    """Submit a job that takes video/audio (or any file) uploads.

    `Accept: application/json` makes the gateway answer with the job envelope
    instead of the HTML progress page.
    """
    data = {"workflow": workflow}
    data.update({k: str(v) for k, v in fields.items() if v is not None})
    parts = {k: (os.path.basename(p), open(p, "rb"), mimetypes.guess_type(p)[0] or "application/octet-stream")
             for k, p in (files or {}).items()}
    try:
        resp = requests.post(f"{GW}/comfyui_runflow", data=data, files=parts,
                             headers={"Accept": "application/json"}, timeout=120)
    finally:
        for fh in (f[1] for f in parts.values()):
            fh.close()
    return _check(resp) or resp.text


def wait(job_id, timeout=1800, interval=1.0):
    """Block until the job is done. Returns the final status dict."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _check(requests.get(f"{GW}/comfyui_status?job_id={job_id}", timeout=30))
        state = last.get("state")
        if state in ("done", "error", "cancelled") or last.get("progress") == "Done":
            return last
        time.sleep(interval)
    raise GatewayError(f"timed out after {timeout}s; last status: {last}")


def outputs(workflow, job_id):
    """Download paths for the finished job, newest first."""
    info = _check(requests.get(f"{GW}/find_workflow_outputs/{workflow}?job_id={job_id}", timeout=30))
    if not info.get("has_output_config"):
        raise GatewayError(f"{workflow} declares no output block; try result_images() instead")
    return [f["filepath"] for f in info.get("files", [])]


def result_images(job_id):
    """Base64 jpgs — only for workflows that save as api*.jpg in ComfyUI's output root."""
    return _check(requests.get(f"{GW}/comfyui_result?job_id={job_id}", timeout=120)).get("processed_image", [])


def download(filepath, dest):
    """Always build the URL from `filepath`; the API's file_url field is not usable."""
    url = f"{GW}/download_file?filepath=" + urllib.parse.quote(filepath)
    resp = requests.get(url, timeout=600, stream=True)
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(1 << 16):
            fh.write(chunk)
    return dest


def execute(workflow, out_dir=".", timeout=1800, **fields):
    """Full lifecycle for one image workflow."""
    ensure_running()
    job = run(workflow, **fields)
    job_id = job["job_id"]
    final = wait(job_id, timeout=timeout)
    if final.get("state") == "error":
        raise GatewayError(final.get("error") or "job failed")
    if catalog().get(workflow, {}).get("has_output_config"):
        return [download(p, os.path.join(out_dir, os.path.basename(p)))
                for p in outputs(workflow, job_id)]
    return [p for p in result_images(job_id)]      # base64 strings; decode as needed


def cancel(job_id):
    """Cancels exactly that job - a queued job is dequeued, a running one interrupted."""
    return _check(requests.post(f"{GW}/comfyui_cancel/{job_id}", timeout=30))


def interrupt_all():
    """Kills whatever ComfyUI is running, whoever submitted it. Avoid when possible."""
    return _check(requests.post(f"{GW}/comfyui_interrupt_all", timeout=30))


if __name__ == "__main__":
    if sys.argv[1:2] == ["list"]:
        for name, cfg in sorted(catalog().items()):
            flags = f" form={cfg['has_inputs']} out={cfg['has_output_config']}"
            print(f"{name}{flags}\n    {cfg.get('descriptions') or ''}")
    elif sys.argv[1:2] == ["presets"]:
        for p in presets():
            print(f"{p['name']:<28} {p['title']}  -> {p['fields'].get('workflow')}")
    else:
        print(__doc__)
```

Usage:

```python
from comfyui_client import execute, presets, catalog

for p in presets():
    print(p["name"], "->", p["fields"]["workflow"])

paths = execute(
    "zImage_txt2img_api",
    positive="a cyberpunk skyline at dusk",
    negative="blurry, low quality",
    width=1280, height=720, steps=9, cfg=1,
)
```

---

## 9. Driving the web form

Useful when you drive a real browser (Playwright, Selenium) instead of issuing requests.

1. Navigate to `http://<host>:5000/runflow`. The listing shows only workflows that declare
   `inputs`, grouped by the subdirectories under `workflows/`.
2. Follow a subdirectory link (`/runflow/<dir>`) to scope the listing, or `..` to go up.
3. Click a workflow to open `/workflow/<bare_name>`. The form is generated from the `inputs`
   block: text → textarea, `string` with `choices` → radio buttons, `integer`/`float` → number
   input, `image`/`video`/`audio` → file input, `array` → comma-separated text.
4. Fill and submit. The browser POSTs `multipart/form-data` to `/comfyui_runflow` and lands on a
   progress page that polls `/comfyui_status?job_id=…` every second. The page keeps the job id in
   its URL, so a reload re-attaches to the same job.
5. The page shows the job's state: `Queued (position N of M)` while it waits behind other jobs,
   then the live progress, and finally the results. When the job is done the page calls
   `/find_workflow_outputs/<name>?job_id=…` and renders each file inline — images as `<img>`,
   video as `<video>`, audio as `<audio>`, `.txt` with a copy button.
6. **Interrupt** posts to `/comfyui_cancel/<job_id>` and therefore cancels **that job only** —
   other jobs in the queue keep running. **Free Memory** calls `/comfyui_free`.

Two tabs can run two workflows at the same time: each tab follows its own job.

To drive the form programmatically, the POST is identical to §4.2 — reuse the multipart client
rather than scraping HTML. Only the generated form itself requires a browser.

---

## 10. Errors and troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `503 ComfyUI is not started.` | Gateway up, ComfyUI down | `GET /start_comfyui`, wait for port 8188 |
| `500 Workflow 'X' not found in any workflows.config.json` | Unknown name | Check `/api/workflows`; add `_api` for exact-match endpoints |
| `400 Missing 'workflow' key` | Omit the key | Always send `workflow` |
| `404 Unknown job_id: …` | The gateway restarted, or the id is wrong | The job registry lives in memory; submit again |
| `409 job_id is required: N jobs are active` | Poll without a `job_id` while several jobs run | Poll `/comfyui_status?job_id=<id>` |
| `400 job_id is required. Use POST /comfyui_cancel/{job_id}…` | `/comfyui_interrupt` without a `job_id` | Cancel by id |
| `500 Failed to get prompt_id from ComfyUI` | Node/field mismatch — a `fields` path no longer matches the workflow JSON | Re-check `fields` against the workflow; do not invent keys |
| Job runs but result list is empty | No `output` block on this workflow | Use `/comfyui_result` instead |
| `/comfyui_result` returns `[]` | Outputs are not `api*.jpg` in the output root | Use `/find_workflow_outputs` |
| `file_url` 404s when downloaded | Path-style URL against a query-parameter route | Build `/download_file?filepath=<quote(filepath)>` |
| Workflow ignores my value | Key absent from `fields` | Only keys in `fields` are applied |
| Prompt contains a literal `$positive` | Placeholder not resolved | Resolve `$` per §3.1 |
| Image ignored / node reports a missing file | `data:` prefix defeated the image sniff | Send raw base64 |
| Poll never reaches `done` | The job is queued behind another client | Check `state` and `queue_position`; raise the timeout |
| `state` is `error`, `error` says "ComfyUI restarted; job lost" | The ComfyUI backend restarted mid-queue | Submit again |
| `Done` but no files | The job produced nothing, or ComfyUI served a cached node | Treat as failure; check `debug` in the outputs response |
| Second job reports the first job's progress | You polled without a `job_id` | Poll with the `job_id` the submit returned |

Server-side logs are at `log_level="warning"` by default. Raise verbosity in the `uvicorn.run(...)`
call at the bottom of `comfyui_server.py` when debugging.

---

## 11. Limits and caveats

- **Jobs are queued, not parallel.** ComfyUI runs one prompt at a time; the gateway queues the rest
  and reports `queue_position`. Jobs are *not* lost when several are active — but a poll without a
  `job_id` is ambiguous and answers `409`.
- **The job registry is in memory.** Restarting the gateway forgets every job id; ComfyUI's own
  queue is in memory too, so a restart of either side loses the pending prompts.
- **No authentication.** Anything that can reach port 5000 can start/stop ComfyUI and read any
  file path the server can read. Do not expose this port beyond a trusted LAN.
- **No request-size enforcement.** `app.state.max_request_size` is set to 50 MB but never checked
  by any middleware. Keep base64 payloads well under that anyway — a 1280×720 JPEG inflates by ~33%
  as base64.
- **Uploads accumulate.** Input files are written per submission, named `<field>_<job_id>.<ext>`;
  identical bytes are deduplicated, but distinct uploads are garbage-collected only after the job
  that owns them has finished.
- **`/comfyui_interrupt_all` is global.** It stops whatever ComfyUI runs, including jobs from the
  ComfyUI web UI. `/comfyui_cancel/{job_id}` is the scoped alternative.
- **`/comfyui_caption` blocks** until the caption workflow finishes (900 s cap). The job flow
  (`/comfyui_workflow` → `/comfyui_status` → `/comfyui_text`) does not block and can be cancelled.
- **`/download_file` is filesystem-scoped.** Pass only paths the server returned.
- **ComfyUI caches node outputs.** Re-running an identical prompt can reuse a cached node and
  therefore not rewrite the output file, so the output listing of the second run can be empty.
- **Mode presets are advisory.** A preset's `fields` describe intent; the workflow's `fields`
  define the payload contract. When they disagree, `/api/workflows` wins.