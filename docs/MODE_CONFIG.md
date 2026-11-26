# `mode.config.json` reference

A mode is a high-level preset: a named bundle of workflow plus fixed parameter values.
Modes are what the Stable Diffusion Sketch app shows as its model/mode list.

- `mode.config.json` lives in the same folder as the workflows it refers to.
- `GET /mode_config` returns the modes of every folder, merged.
- The app resolves the `$placeholders` in `fields` before it sends the payload.

```json
[
  {
    "name": "Krea2Refine",
    "title": "Krea2 Refine",
    "showT2I": false,
    "show": true,
    "configurable": false,
    "default": {"type": "img2img", "steps": 4, "baseImage": "background", "sdSize": 768},
    "fields": {
      "workflow": "krea2_refine",
      "background": "$background",
      "paint": "$paint",
      "maskBlur": 10,
      "positive": "$positive",
      "negative": "$negative"
    }
  }
]
```

## Keys

| Key | Meaning |
|---|---|
| `name` | Identifier of the mode. Unique, no spaces. |
| `title` | Label shown in the app. |
| `showT2I` | `true` shows the mode in the text-to-image section of the app. |
| `show` | `false` hides the mode without deleting it. |
| `configurable` | `true` lets the user change the parameters listed in `default`. |
| `default` | Default values for the configurable parameters, plus the app's own hints (`type`, `steps`, `baseImage`, `sdSize`, …). |
| `fields` | The payload the mode sends. `workflow` is required; the other entries are either fixed values or `$placeholders`. |

## The `workflow` entry

`fields.workflow` names the workflow **without** the `_api` suffix - the app and the
gateway resolve it to `<name>_api` when such a workflow exists:

```json
"fields": {"workflow": "krea2_refine"}     // -> workflows/krea2/krea2_refine_api.json
```

## Placeholders

| Placeholder | Filled with |
|---|---|
| `$background` | The background image of the canvas. |
| `$reference` | The reference image. |
| `$paint` | The painted mask. |
| `$mask` | The mask (same image, mask channel). |
| `$positive` | The positive prompt the user typed. |
| `$negative` | The negative prompt. |
| `$steps`, `$cfg`, `$denoise` | The sampler settings of the app. |
| `$maskBlur` | The mask blur radius. |
| `$width`, `$height`, `$size` | The requested canvas size. |
| `$batchSize` | Number of images to generate. |

A placeholder only resolves when the matching key exists in the workflow's `fields` map -
otherwise the app drops it and the gateway never sees it.

## Rules that keep modes working

1. Every key in `fields` (except `workflow`) must exist in the target workflow's
   `fields` map, otherwise the value is sent but the gateway cannot place it.
2. `workflow` must resolve to an existing workflow, otherwise the app has nothing to run.
3. Keep one mode per model family and per task (txt2img, img2img, refine, ref2va …). The
   app lists them in the order of the array.
4. Modes that need a mask (`$paint`) only make sense for workflows whose `fields` map
   contains the mask node input.

## Example set

```json
[
  {
    "name": "Krea2",
    "title": "Krea2 txt2img",
    "showT2I": true,
    "show": true,
    "configurable": false,
    "default": {"type": "txt2img", "steps": 4, "sdSize": 768},
    "fields": {"workflow": "krea2_txt2img", "positive": "$positive", "negative": "$negative"}
  },
  {
    "name": "Krea2Img2img",
    "title": "Krea2 img2img",
    "showT2I": false,
    "show": true,
    "configurable": false,
    "default": {"type": "img2img", "steps": 4, "baseImage": "background", "sdSize": 768},
    "fields": {"workflow": "krea2_img2img", "background": "$background", "positive": "$positive"}
  }
]
```

## Verify

```bash
curl -s http://127.0.0.1:5000/mode_config | python -m json.tool
```

Each entry must name a workflow that exists in `GET /api/workflows`.
