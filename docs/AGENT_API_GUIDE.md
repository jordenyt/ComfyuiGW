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
| 1 | **One job at a time.** The gateway keeps a single global job state (`comfyui_state`). | A second submission overwrites the first job's state. Progress/results become ambiguous. Queue jobs; never parallelise. |
| 2 | **Wait for `progress == "Done"` exactly.** Not `"done"`, not `"Done!"`. | The literal string set on completion is `Done`. Anything else never matches and you poll forever. |
| 3 | **Use `filepath`, not `file_url`, from `/find_workflow_outputs`.** | `file_url` is emitted as `/download_file/<path>`, but the route only accepts the query form `/download_file?filepath=...`. The `file_url` string 404s. Build the URL yourself from `filepath`. |
| 4 | **Base64 images must be raw base64 — no `data:` prefix.** | `/comfyui_workflow` sniffs each string with a Pillow decode. A `data:image/jpeg;base64,` prefix fails the sniff, so the value is passed through verbatim as a filename and the job fails. |
| 5 | **Images only on the JSON endpoint.** Video and audio must use the multipart endpoint. | Only image payloads are auto-decoded and written to ComfyUI's input dir. |
| 6 | **Resolve `$placeholders` yourself before POSTing.** | The gateway does no `$` substitution. A literal `"$positive"` is written into the prompt as-is. See §4. |
| 7 | **`seed` is always randomised.** | Any `seed` you send is overwritten with a fresh random value. Send it only for readability. |
| 8 | **Use the exact `<name>_api` key where exactness is required.** | `/find_workflow_outputs` and `/comfyui_runflow` do **no** `_api` fallback. See §3. |

---

## 2. Connecting

The gateway is stateless HTTP. No auth, no API key, no session token. Base your client on one host constant:

```bash
GW="http://192.168.1.10:5000"
curl -s "$GW/comfyui_status"
```

Health check before any job:

```bash
curl -s "$GW/start_comfyui"     # idempotent: "started" or "already started"
curl -s "$GW/comfyui_status"    # {"progress": "...", "step": "..."}
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
# -> {"status":"Got Prompt"}
```

Rules for this endpoint:

- `workflow` is the only required key.
- Any string value that sniffs as a valid base64 **image** is written into ComfyUI's `input/`
  directory as `<key>_<sessionid>.<ext>` and replaced with the absolute server-side path. Identical
  bytes already present are reused instead of re-saved.
- Non-image strings are passed through untouched — which is exactly why the `data:` prefix rule
  matters.
- Numbers, booleans, and JSON arrays pass through as-is. Send `array`-typed fields as real JSON
  arrays; the comma-splitting behaviour described in §4.2 belongs to the multipart path only.
- Omitting a key leaves the workflow's own default in place. Fields not present in `fields` are
  ignored.

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
`/api/workflows`) — otherwise it silently queues nothing and returns the progress page.

Coercion applied to form fields, per the `inputs` declaration:

| `type` | Form encoding | Result sent to ComfyUI |
|---|---|---|
| `image`, `video`, `audio` | file part | file uploaded, original extension preserved |
| `integer` | text field | cast to `int` |
| `float` | text field | cast to `float` |
| `array` | `a,b,c` | list of stripped strings |
| `text`, `string` | text field | raw string |

The response is an **HTML progress page**, not JSON. Ignore the body and poll §5.
If ComfyUI is down this endpoint returns HTTP 200 with `{"message": "ComfyUI is not started."}`
— check the content type before assuming you got the page.

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
workflow and **blocks inside the request** until `output/caption.txt` appears, then returns
`{"caption": "..."}`. Use a generous client timeout.

> Caveat: the wait loop has no timeout. If the caption workflow errors out, the request hangs until
> the process restarts. Prefer `/comfyui_workflow` + `/find_workflow_outputs` when you can.

---

## 5. Job lifecycle

```
submit ──► poll /comfyui_status ──► progress == "Done" ──► fetch outputs ──► download files
```

Poll roughly once per second. The response carries both a message and a step counter with an ETA:

```json
{"progress": "Processing \"KSampler\" (12/41)...", "step": "--> Step: 7/20 [00:31<00:58]"}
```

```bash
until [ "$(curl -s "$GW/comfyui_status" | python -c 'import json,sys; print(json.load(sys.stdin)["progress"])')" = "Done" ]; do
  curl -s "$GW/comfyui_status"; echo
  sleep 1
done
```

Because the HTTP call is queued synchronously before the response returns, `progress` is reset to
`""` by the time your POST completes. There is no stale-`"Done"` race to guard against — provided
you obey rule 1 and do not submit a second job while one is running.

**Cancelling.** `GET /comfyui_interrupt` stops the running ComfyUI job and force-sets
`progress` to `Done`, so your poll will terminate. Consequence: `Done` no longer implies outputs
exist. Always check the output listing for emptiness rather than assuming success. Do not poll
`/comfyui_interrupt` to check status — it is destructive.

**Timeouts.** Video workflows routinely run for many minutes. Use a generous ceiling (10–30 min)
and treat exceeding it as inconclusive, then `GET /comfyui_status` once more before deciding.

---

## 6. Retrieving results

Two endpoints, and **which one works depends on the workflow's configuration**. Check
`has_output_config` in `/api/workflows`.

### 6.1 `/find_workflow_outputs/{workflow}` — the modern path

Requires `has_output_config: true` **and** the exact `_api` key.

```bash
curl -s "$GW/find_workflow_outputs/ref2v_h3_extend_api"
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
  "debug": {"workflow": "ref2v_h3_extend_api", "workflow_start_time": 1767225400.0, "...": "..."}
}
```

- Only files modified **at or after the current job's start time** are returned, so results from
  previous runs never leak in.
- `file_url` is present but **broken** — the route expects a query parameter. Build the download URL
  from `filepath` instead:
  `"/download_file?filepath=" + urllib.parse.quote(filepath)`.
- When `has_output_config` is `false` you get `{"files": [], "has_output_config": false, ...}`.
  That is a configuration fact, not an error — switch to `/comfyui_result`.
- When the job produced nothing you get `files: []` with `has_output_config: true`. The `debug`
  block shows the resolved search directory and start time, which is the fastest way to diagnose.

### 6.2 `/comfyui_result` — base64 images only

```bash
curl -s "$GW/comfyui_result"
# -> {"processed_image": ["<base64 jpg>", "<base64 jpg>"]}
```

The key is **`processed_image`** (a list of raw base64 strings) — *not* `images`.

Constraints, all of which will bite you:

- Only files directly inside ComfyUI's `output/` directory whose name **starts with `api`** and
  **ends with `.jpg`** are returned. Most workflows use a different save prefix (`H3`, `i2v_wan`,
  `v2v_save`, …), so this endpoint returns `[]` for them.
- Subdirectories are not scanned.
- Submitting via `/comfyui_workflow` deletes pre-existing `api*` files from `output/` at submit
  time, which is what keeps the list scoped to one run.

Decode the strings:

```python
import base64
raw = requests.get(f"{GW}/comfyui_result").json()["processed_image"]
for i, b64 in enumerate(raw):
    open(f"out_{i}.jpg", "wb").write(base64.b64decode(b64))
```

### 6.3 Result routing cheat sheet

| Situation | Endpoint |
|---|---|
| `/api/workflows` shows `has_output_config: true` | `GET /find_workflow_outputs/{key}` |
| Image workflow with no `output` block, saves as `api*.jpg` | `GET /comfyui_result` |
| Video / audio / text output | `GET /find_workflow_outputs/{key}` (needs an `output` block) |
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

---

## 7. Endpoint reference

### Discovery & docs

| Method | Path | Returns |
|---|---|---|
| GET | `/` | HTML index linking every entry point |
| GET | `/agent_guide` | This guide, rendered HTML |
| GET | `/agent_guide.md` | This guide, raw Markdown |
| GET | `/api/workflows` | JSON map of workflow name → fields / inputs / output metadata |
| GET | `/api/endpoints` | JSON inventory of routes with purpose notes |
| GET | `/openapi.json`, `/docs`, `/redoc` | FastAPI's generated schema and Swagger UI |

### Execution

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/comfyui_workflow` | JSON: `{"workflow": str, ...fields}` | `{"status":"Got Prompt"}` |
| POST | `/comfyui_runflow` | multipart: `workflow` + declared `inputs` | HTML progress page |
| POST | `/comfyui_caption` | JSON: `{"workflow": str, "background": base64}` | `{"caption": str}` (blocking) |

### Status, control, results

| Method | Path | Returns / effect |
|---|---|---|
| GET | `/comfyui_status` | `{"progress": str, "step": str}` |
| GET | `/comfyui_interrupt` | Cancels the running job; sets progress to `Done` |
| GET | `/comfyui_free` | Unloads models and frees VRAM |
| GET | `/start_comfyui` | Starts ComfyUI (idempotent) |
| GET | `/stop_comfyui` | Stops ComfyUI |
| GET | `/comfyui_result` | `{"processed_image": [base64 jpg]}` |
| GET | `/find_workflow_outputs/{workflow}` | `{"files": [...], "has_output_config": bool, "debug": {...}}` |
| GET | `/find_latest_runflow` | `{"file_url", "filename", "filepath"}` |
| GET | `/download_file?filepath=` | File stream |
| GET | `/loras` | JSON array of LoRA relative paths |

### Web UI

| Method | Path | Purpose |
|---|---|---|
| GET | `/runflow` | Workflow browser, grouped by directory |
| GET | `/runflow/{path}` | Workflow browser for a subdirectory |
| GET | `/workflow/{name}` | Generated HTML form for a workflow (bare name, no `_api`) |
| GET | `/workflow_status` | Standalone progress page |
| POST | `/comfyui_runflow` | Form submission target |

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
    """Submit a job. Image kwargs may be file paths; they are base64-encoded for you."""
    payload = {"workflow": workflow}
    for key, value in fields.items():
        if isinstance(value, str) and os.path.isfile(value):
            value = b64_image(value)
        payload[key] = value
    return _check(requests.post(f"{GW}/comfyui_workflow", json=payload, timeout=120))


def run_multipart(workflow, files=None, **fields):
    """Submit a job that takes video/audio (or any file) uploads."""
    data = {"workflow": workflow}
    data.update({k: str(v) for k, v in fields.items() if v is not None})
    parts = {k: (os.path.basename(p), open(p, "rb"), mimetypes.guess_type(p)[0] or "application/octet-stream")
             for k, p in (files or {}).items()}
    try:
        resp = requests.post(f"{GW}/comfyui_runflow", data=data, files=parts, timeout=120)
    finally:
        for fh in (f[1] for f in parts.values()):
            fh.close()
    return _check(resp) or resp.text


def wait(timeout=1800, interval=1.0):
    """Block until progress == 'Done'. Returns the final status dict."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _check(requests.get(f"{GW}/comfyui_status", timeout=30))
        if last.get("progress") == "Done":
            return last
        time.sleep(interval)
    raise GatewayError(f"timed out after {timeout}s; last status: {last}")


def outputs(workflow):
    """Download paths for the finished job, newest first."""
    info = _check(requests.get(f"{GW}/find_workflow_outputs/{workflow}", timeout=30))
    if not info.get("has_output_config"):
        raise GatewayError(f"{workflow} declares no output block; try result_images() instead")
    return [f["filepath"] for f in info.get("files", [])]


def result_images():
    """Base64 jpgs — only for workflows that save as api*.jpg in ComfyUI's output root."""
    return _check(requests.get(f"{GW}/comfyui_result", timeout=120)).get("processed_image", [])


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
    run(workflow, **fields)
    wait(timeout=timeout)
    if catalog().get(workflow, {}).get("has_output_config"):
        return [download(p, os.path.join(out_dir, os.path.basename(p)))
                for p in outputs(workflow)]
    return [p for p in result_images()]      # base64 strings; decode as needed


def interrupt():
    """Cancels the job. Note: progress is forced to 'Done' even though outputs are absent."""
    requests.get(f"{GW}/comfyui_interrupt", timeout=30)


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
   progress page that polls `/comfyui_status` every second.
5. When it shows `Done`, the page calls `/find_workflow_outputs/<name>` and renders each file inline
   — images as `<img>`, video as `<video>`, audio as `<audio>`, `.txt` with a copy button.
6. **Interrupt** posts to `/comfyui_interrupt`; **Free Memory** calls `/comfyui_free`.

To drive the form programmatically, the POST is identical to §4.2 — reuse the multipart client
rather than scraping HTML. Only the generated form itself requires a browser.

---

## 10. Errors and troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `503 ComfyUI is not started.` | Gateway up, ComfyUI down | `GET /start_comfyui`, wait for port 8188 |
| `500 Workflow 'X' not found in any workflows.config.json` | Unknown name | Check `/api/workflows`; add `_api` for exact-match endpoints |
| `400 Missing 'workflow' key` | Omit the key | Always send `workflow` |
| `500 Failed to get prompt_id from ComfyUI` | Node/field mismatch — a `fields` path no longer matches the workflow JSON | Re-check `fields` against the workflow; do not invent keys |
| Job runs but result list is empty | No `output` block on this workflow | Use `/comfyui_result` instead |
| `/comfyui_result` returns `[]` | Outputs are not `api*.jpg` in the output root | Use `/find_workflow_outputs` |
| `file_url` 404s when downloaded | Path-style URL against a query-parameter route | Build `/download_file?filepath=<quote(filepath)>` |
| Workflow ignores my value | Key absent from `fields` | Only keys in `fields` are applied |
| Prompt contains a literal `$positive` | Placeholder not resolved | Resolve `$` per §3.1 |
| Image ignored / node reports a missing file | `data:` prefix defeated the image sniff | Send raw base64 |
| Poll never reaches `Done` | Job still running, or `/comfyui_interrupt` never called | Raise timeout; re-check status once |
| `Done` but no files | Job was interrupted, or produced nothing | Treat as failure; check `debug` in the outputs response |
| Second job reports the first job's progress | Concurrent submissions | Serialise jobs (rule 1) |

Server-side logs are at `log_level="warning"` by default. Raise verbosity in the `uvicorn.run(...)`
call at the bottom of `comfyui_server.py` when debugging.

---

## 11. Limits and caveats

- **Single job at a time.** Global state; parallel submissions corrupt each other's bookkeeping.
- **No authentication.** Anything that can reach port 5000 can start/stop ComfyUI and read any
  file path the server can read. Do not expose this port beyond a trusted LAN.
- **No request-size enforcement.** `app.state.max_request_size` is set to 50 MB but never checked
  by any middleware. Keep base64 payloads well under that anyway — a 1280×720 JPEG inflates by ~33%
  as base64.
- **Uploads accumulate.** Input files are written per submission with a random session suffix;
  identical bytes are deduplicated, but distinct uploads are not garbage-collected.
- **Interrupt is destructive and ambiguous.** It forces `Done`; always verify outputs exist.
- **`/comfyui_caption` can hang indefinitely** if its workflow fails.
- **`/download_file` is filesystem-scoped.** Pass only paths the server returned.
- **Mode presets are advisory.** A preset's `fields` describe intent; the workflow's `fields`
  define the payload contract. When they disagree, `/api/workflows` wins.