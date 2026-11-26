# Publishing a workflow to the runflow web UI

The runflow UI is the browser front end of the gateway: a folder tree of workflows, one
form per workflow, and a progress page per job.

- `http://127.0.0.1:5000/runflow` - the families (folders) that contain runnable workflows
- `http://127.0.0.1:5000/runflow/<family>` - the runnable workflows of that family
- `http://127.0.0.1:5000/workflow/<name>` - the form of one workflow
- `http://127.0.0.1:5000/workflow_status?job_id=...` - the progress page of a job

## What makes a workflow appear

The gateway lists a workflow in the runflow UI when its configuration has an `inputs`
block. That block is also what the form renders.

```json
"inputs": {
  "image1": {"type": "image", "default": null},
  "image2": {"type": "image", "default": null, "optional": true},
  "positive": {"type": "text", "default": ""},
  "duration": {"type": "integer", "default": 5},
  "steps": {"type": "integer", "default": 4},
  "width": {"type": "integer", "default": 864},
  "height": {"type": "integer", "default": 480}
}
```

Rules:

- The `inputs` keys must also appear in `fields`; otherwise the form sends a value the
  gateway cannot place.
- `type` decides the widget: `text` -> textarea, `string` -> text input (radio buttons
  when `choices` is present), `integer` / `float` -> number input, `array` -> comma
  separated list, `image` / `video` / `audio` -> file upload.
- `default` pre-fills the widget. Use `null` for files.
- `optional: true` marks the field optional. An optional field that is not supplied is
  stripped from the prompt: the gateway removes the source node and disconnects it from
  its consumers, including autogrow slots.

The folder only shows up in `/runflow` when at least one of its workflows has an `inputs`
block.

## Submitting from the form

The form posts multipart form data to `/comfyui_runflow`:

```
POST /comfyui_runflow
Content-Type: multipart/form-data

workflow=ref2va_h3_api
image1=<file>
image2=<file>
positive=A cinematic shot ...
duration=3
steps=4
```

- The workflow name is the config key, with the `_api` suffix.
- File fields are stored in the ComfyUI `input` folder under a content-addressed name and
  the node input receives the resulting path.
- The response is the HTML progress page. Send `Accept: application/json` to get the job
  envelope instead:

```json
{"status": "Queued", "job_id": "566ab0c4-...", "state": "queued",
 "workflow": "ref2va_h3_api", "queue_position": 1, "queue_length": 1}
```

## Optional fields and node stripping

When an optional field is left empty, the gateway removes the node that fed it and
disconnects it from every consumer:

- a plain slot is removed from the consumer's `inputs`;
- an autogrow slot (`ref_images.ref_image_1`) is removed as well, so the model is not fed
  a stale reference image.

This is why an optional reference image disappears from the graph instead of falling back
to the file name baked into the workflow.

## Agent use

Agents normally use the JSON endpoint, which accepts the same payload keys:

```
POST /comfyui_workflow
Content-Type: application/json

{"workflow": "ref2va_h3", "image1": "<raw base64>", "positive": "...", "duration": 3}
```

The multipart endpoint is required for video and audio payloads, because JSON cannot carry
binary uploads.

## Progress and results

The progress page follows one job: while it waits in the queue it shows
`Queued (position N of M)`, and the Interrupt button cancels that job only. When the job
is done, the outputs are the files listed by the workflow's `output.files` spec, found in
the ComfyUI output directory (and in its subfolders when the prefix contains one).
