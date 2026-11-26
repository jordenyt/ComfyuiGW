# `workflows.config.json` reference

Every workflow family folder under `workflows/` may contain a `workflows.config.json`.
The gateway merges all of them into one map, exposed by `GET /api/workflows`.

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
      "steps": "13.inputs.steps",
      "width": "8.inputs.width",
      "height": "8.inputs.height"
    },
    "inputs": {
      "image1": {"type": "image", "default": null},
      "image2": {"type": "image", "default": null, "optional": true},
      "positive": {"type": "text", "default": ""},
      "duration": {"type": "integer", "default": 5},
      "steps": {"type": "integer", "default": 4},
      "width": {"type": "integer", "default": 864},
      "height": {"type": "integer", "default": 480}
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

## Keys

| Key | Required | Meaning |
|---|---|---|
| `prompt_workflow` | yes | File name of the prompt workflow, relative to the folder holding this config. A path with a directory is resolved relative to the repository root. |
| `fields` | yes | Maps a payload key to the node input it fills. See below. |
| `inputs` | no | Declares the runflow form fields. Its presence makes the workflow runnable from the web UI. |
| `output` | no | Declares the files the workflow produces, so the gateway can find them. |
| `descriptions` | no | Label shown in the app, the runflow list and `/api/workflows`. |
| `auto_image_tags` | no | `true` prepends `<Picture N>` tags to the `positive` prompt, for models that resolve them positionally. |

## `fields`

Each entry is `payload key -> "node_id.inputs.input_name"`. The target is resolved inside
the prompt workflow:

- `node_id` must exist in the workflow, otherwise the field is ignored (the gateway never
  creates phantom nodes).
- `input_name` may be a literal dotted name, e.g. `1.inputs.sampling_mode.seed`. The
  gateway prefers the longest literal match, so dotted node inputs are supported.
- One payload key may fill several inputs: use an array of paths.

Special payload keys:

| Key | Behaviour |
|---|---|
| `seed` | Always overwritten with a fresh random value (1 .. 2^31-1) when the workflow is built. The declared path decides where the seed lands. |
| `image1`, `image2`, … | The payload value is an image (base64 on `/comfyui_workflow`, an uploaded file on `/comfyui_runflow`). The gateway stores it in the ComfyUI `input` folder and fills the node input with the resulting path. |
| `video1`, `audio1`, … | Same as images, but the file is kept with its own extension. |
| `positive`, `prompt`, `negative`, `duration`, `width`, … | Plain values, copied verbatim into the node input. |

## `inputs`

One entry per runflow form field:

```json
"inputs": {
  "image1": {"type": "image", "default": null},
  "image2": {"type": "image", "default": null, "optional": true},
  "positive": {"type": "text", "default": ""},
  "duration": {"type": "integer", "default": 5},
  "cn_strength": {"type": "float", "default": 0.6},
  "steps": {"type": "integer", "default": 4},
  "choices": {"type": "string", "default": "match", "choices": ["match", "max"]}
}
```

| Key | Meaning |
|---|---|
| `type` | `text`, `string`, `integer`, `float`, `array`, `image`, `video`, `audio`. |
| `default` | Value pre-filled in the form. `null` for files. |
| `optional` | `true` marks the field as optional. When it is not supplied, the gateway strips the source node and its connections (including autogrow slots) from the prompt. |
| `choices` | Allowed values for a `string` field; the form renders radio buttons. |

A field listed in `inputs` must also be listed in `fields`, otherwise the form sends a value
the gateway cannot place.

## `output`

```json
"output": {"files": [{"prefix": "caption", "extension": "txt"}]}
```

- `prefix` - the file name prefix the saver node writes. A prefix with a subfolder
  (`video/MiniMax_H3`) makes the gateway search that subfolder of the ComfyUI output
  directory.
- `extension` - without the dot.

The gateway uses this spec as the fallback search when ComfyUI's history entry for the job
has already been evicted. A workflow with a `.txt` output is reported with
`produces_text: true` and appears in the app's AI prompt picker.

## `auto_image_tags`

`"auto_image_tags": true` prepends `<Picture 1> <Picture 2>` (one tag per supplied image,
in payload order) to the `positive` prompt before the prompt is built. Use it for models
that address their reference images positionally, such as MiniMax H3.

## Name resolution

- `POST /comfyui_workflow` accepts either `workflow_name` or `workflow_name_api`; the
  gateway tries `<name>_api` first and falls back to the bare name.
- `GET /api/workflows` lists the merged map with the absolute `prompt_workflow` path, the
  `fields`, the `inputs`, the `output` spec and the flags `produces_text`, `has_inputs`,
  `has_output_config`, `auto_image_tags`.
