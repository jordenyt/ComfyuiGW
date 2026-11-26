# Creating a ComfyUI workflow for the gateway

This document is the checklist for turning a ComfyUI graph into a workflow the gateway can
run. It applies to agents and to humans. Follow it in order; every step ends with a check
you can perform against the running gateway.

- [1. Pick the family folder](#1-pick-the-family-folder)
- [2. Write the prompt workflow (API format)](#2-write-the-prompt-workflow-api-format)
- [3. Use stock ComfyUI nodes only](#3-use-stock-comfyui-nodes-only)
- [4. Autogrow and dynamic-combo inputs](#4-autogrow-and-dynamic-combo-inputs)
- [5. Model and file names must match exactly](#5-model-and-file-names-must-match-exactly)
- [6. Check the workflow against ComfyUI](#6-check-the-workflow-against-comfyui)
- [7. Register it in workflows.config.json](#7-register-it-in-workflows-configjson)
- [8. Publish it to the runflow web UI](#8-publish-it-to-the-runflow-web-ui)
- [9. Add a mode preset](#9-add-a-mode-preset)

## 1. Pick the family folder

Every workflow lives in its own folder under `workflows/`, one folder per model family:

```
workflows/
├── krea2/
│   ├── krea2_txt2img_api.json
│   ├── krea2_img2img_api.json
│   ├── krea2_refine_api.json
│   ├── workflows.config.json
│   └── mode.config.json
├── MiniMaxH3/
│   ├── h3_ref2va_api.json
│   └── workflows.config.json
└── prompt/
    ├── caption_qwen35_api.json
    ├── PE_Krea2_txt2img_api.json
    └── workflows.config.json
```

The folder name is free, but keep it stable: the app and the runflow UI show it, and the
`prompt_workflow` path in `workflows.config.json` is resolved relative to it.

## 2. Write the prompt workflow (API format)

The gateway reads **API-format** graphs: a JSON object keyed by node id, where each node
has `class_type` and `inputs`. Links are `["<source node id>", <output index>]`.

```json
{
  "1": {
    "inputs": {
      "unet_name": "MiniMax\\minimax_h3_ref2va_pruned_int8_convrot.safetensors",
      "weight_dtype": "default"
    },
    "class_type": "UNETLoader",
    "_meta": {"title": "Load MiniMax H3 ref2va", "cnr_id": "comfy-core", "ver": "0.46.0"}
  },
  "8": {
    "inputs": {
      "clip": ["2", 0],
      "prompt": ["9", 0],
      "width": 864,
      "height": 480,
      "length": ["20", 1],
      "ref_images.ref_image_0": ["6", 0]
    },
    "class_type": "MiniMaxH3ReferenceToVideo"
  }
}
```

Rules:

- Node ids are **strings**. The id is what `workflows.config.json` refers to.
- `inputs` may hold plain values (`864`, `"text"`, `true`) or links (`["2", 0]`).
- `_meta` is optional; it only carries the display title shown by ComfyUI.
- The gateway converts **UI-format** graphs too (`convert_ui_workflow_to_api`), but saving
  the API format keeps the file diff-friendly and reviewable.

## 3. Use stock ComfyUI nodes only

The workflow must run on a plain ComfyUI install. The authoritative test is the
`python_module` of the node in `GET http://<comfyui>:8188/object_info`:

```bash
curl -s http://127.0.0.1:8188/object_info | python -c "import json,sys; \
  o=json.load(sys.stdin); \
  [print(k, o[k]['python_module']) for k in o if 'MiniMax' in k]"
```

| `python_module` | Verdict |
|---|---|
| `nodes`, `comfy_extras.*`, `comfy.*` | stock ComfyUI — allowed |
| `custom_nodes.*` | third-party pack — not allowed on the `main` branch |

Replacements for common custom nodes:

| Custom node | Stock replacement |
|---|---|
| `Image Save` (WAS) | `SaveImage` (writes PNG into the output root) |
| `GetImageSize+` (essentials) | `GetImageSize` (outputs `width`, `height`, `batch_size`) |
| `Save Text File` (WAS) | `SaveText` (`text`, `filename_prefix`, `format`) |
| `ImageResizeKJv2` | `ImageScaleToTotalPixels` / `ImageScale` |
| `JWIntegerMin` (KJNodes) | `PrimitiveInt` + `ComfyMathExpression` |
| `CLIPLoaderGGUF` | `CLIPLoader` with the quantised `.safetensors` |

## 4. Autogrow and dynamic-combo inputs

Nodes with a variable number of inputs (autogrow) and nodes with a mode switch
(dynamic combo) use **flat dotted keys** in the API format - not nested objects:

```json
{
  "8": {"inputs": {"ref_images.ref_image_0": ["6", 0], "ref_images.ref_image_1": ["7", 0]}},
  "20": {"inputs": {"expression": "max(5, round(a * 24))", "values.a": ["19", 0]}},
  "1": {"inputs": {"sampling_mode": "on", "sampling_mode.temperature": 0.7, "sampling_mode.seed": 42}}
}
```

- The key after the dot is the autogrow slot name (`ref_image_0`, `values.a`).
- For a dynamic combo, the parent key holds the selected option (`"on"`, `"auto"`), and
  each option field is written as `parent.field` (`sampling_mode.seed`).
- The gateway resolves dotted field paths the same way, so `"seed": "1.inputs.sampling_mode.seed"`
  in `workflows.config.json` sets that literal key - it does **not** create a nested object.

## 5. Model and file names must match exactly

Combo inputs are validated against the file list. Copy the names from
`GET /object_info`; subfolders use a backslash and the case matters:

```json
{"unet_name": "MiniMax\\minimax_h3_ref2va_pruned_int8_convrot.safetensors"}
{"lora_name": "MinimaxH3\\minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"}
{"vae_name": "minimax_h3_video_vae_int8_convrot.safetensors"}
```

Output files follow the same rule: the `filename_prefix` of the saver node must start with
the `prefix` declared in `workflows.config.json`, and the extension must match:

| Saver node | `filename_prefix` | File produced |
|---|---|---|
| `SaveImage` | `api` | `output/api_00003_.png` |
| `SaveText` | `caption` | `output/caption_00001.txt` |
| `SaveVideo` | `video/MiniMax_H3` | `output/video/MiniMax_H3_00001_.mp4` |

A prefix may contain a subfolder (`video/MiniMax_H3`); the gateway then searches that
subfolder of the ComfyUI output directory.

## 6. Check the workflow against ComfyUI

Validate before registering. Build the prompt and submit it to ComfyUI directly:

```bash
curl -s -X POST http://127.0.0.1:8188/prompt \
     -H "Content-Type: application/json" \
     -d '{"prompt": { ...your workflow... }}'
```

A rejected prompt answers with `node_errors`, naming the node and the input that failed:

```json
{"node_errors": {"8": {"errors": [{"type": "required_input_missing",
                                   "extra_info": {"input_name": "model"}}]}}}
```

Fix and repeat until it runs. Then confirm the outputs land where the config expects them.

## 7. Register it in workflows.config.json

Add an entry to the `workflows.config.json` of the family folder. The key is the workflow
name; the `_api` suffix is the convention for API workflows.

```json
{
  "ref2va_h3_api": {
    "prompt_workflow": "h3_ref2va_api.json",
    "descriptions": "Reference Images to Video with Minimax-H3 ref2va Model",
    "auto_image_tags": true,
    "fields": {
      "seed": "10.inputs.noise_seed",
      "image1": "6.inputs.image",
      "image2": "7.inputs.image",
      "positive": "9.inputs.value",
      "duration": "19.inputs.value",
      "steps": "13.inputs.steps"
    },
    "inputs": {
      "image1": {"type": "image", "default": null},
      "image2": {"type": "image", "default": null, "optional": true},
      "positive": {"type": "text", "default": ""},
      "duration": {"type": "integer", "default": 5},
      "steps": {"type": "integer", "default": 4}
    },
    "output": {
      "files": [
        {"prefix": "video/MiniMax_H3", "extension": "webm"},
        {"prefix": "video/MiniMax_H3", "extension": "mp4"}
      ]
    }
  }
}
```

Full field reference: [WORKFLOWS_CONFIG.md](WORKFLOWS_CONFIG.md).

Check the registration from the running gateway:

```bash
curl -s http://127.0.0.1:5000/api/workflows | python -m json.tool
```

`produces_text` is derived from the output spec (a `.txt` output makes the workflow appear
in the app's AI prompt picker). `has_inputs` marks the workflows that the runflow form can
drive.

## 8. Publish it to the runflow web UI

A workflow shows up in `/runflow` as soon as it has an `inputs` block - the `inputs` key is
what makes the folder appear in the browser list and the workflow appear on its form page:

- `http://127.0.0.1:5000/runflow` - list of families (folders with runnable workflows)
- `http://127.0.0.1:5000/runflow/MiniMaxH3` - the workflows of that family
- `http://127.0.0.1:5000/workflow/ref2va_h3` - the form for that workflow

Details and the multipart endpoint: [RUNFLOW_WORKFLOW.md](RUNFLOW_WORKFLOW.md).

## 9. Add a mode preset

Modes are the app-facing presets (Stable Diffusion Sketch). They live in
`mode.config.json` in the same folder as the workflow and are merged by `GET /mode_config`.
Full reference: [MODE_CONFIG.md](MODE_CONFIG.md).

## Checklist

- [ ] Workflow JSON is API format, node ids are strings
- [ ] Every node is stock ComfyUI (`python_module` check on `/object_info`)
- [ ] Autogrow / dynamic-combo keys are flat and dotted
- [ ] Model, LoRA and VAE names match the combo lists exactly
- [ ] `POST /prompt` on ComfyUI accepts the graph and produces the expected files
- [ ] `workflows.config.json` entry has `fields` for every payload key
- [ ] `inputs` block present if the workflow must appear in `/runflow`
- [ ] `output.files` prefix/extension match what the saver node writes
- [ ] `mode.config.json` entry added if the app must offer it
