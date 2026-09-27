# body-swap API: calling it

Base URL: `http://body-swap.<tailnet>.ts.net` (Tailscale Service `svc:body-swap`,
port 80, tailnet only). The caller's machine must be on the tailnet and the ACL
must allow it to reach `svc:body-swap`. There is no token: the tailnet decides who can call.

The address stays the same across VMs. While no VM is hosting it, connections
fail outright (no 503). Retry with backoff, and check `/healthz` first.

| Method | Path | What |
|---|---|---|
| GET | `/healthz` | `{"status": "starting"\|"ready"\|"error", "detail", "median_job_seconds"}`. 503 until ready |
| POST | `/jobs` | multipart: `scene`, `character` files (required). Returns 202 + job |
| POST | `/jobs?wait=N` | same, blocks up to N s (max 600). Returns 200 when finished, 202 if still going |
| GET | `/jobs/{id}` | job record |
| GET | `/jobs/{id}/image` | `image/png`; 404 `not_ready` until done |
| DELETE | `/jobs/{id}` | cancel if still queued |
| GET | `/queue` | `{"depth", "running", "capacity": 50}` |
| POST | `/v1/chat/completions` | local VLM (Qwen3-VL-2B Q8), OpenAI-compatible, passed through |
| GET | `/v1/models` | llama-server's model list |

## POST /jobs fields

| field | default | notes |
|---|---|---|
| `scene`, `character` | required | image files; character is cut out automatically (rembg) |
| `output_size` | `720p` | `480p` / `720p` / `1080p` / `1440p` (short side; aspect kept) |
| `upscale` | `none` | `2x` = Real-ESRGAN after generation (1080p -> 4K) |
| `pose_override` | "" | hand-written pose clause; overrides the prompt writer's |
| `prompt` | "" | full literal prompt; bypasses the prompt writer |
| `idempotency_key` | none | same key + job not error/canceled -> the existing job, 200 |

Pose fidelity (mannequin pre-pass + skeleton + refcontrol LoRA) turns on
automatically for back-turned subjects, as in the notebook. It roughly doubles job time.

## Job record

```json
{"job_id": "j_a6873773", "status": "done",
 "created_at": "...", "started_at": "...", "finished_at": "...",
 "prompt_used": "This exact image 1, but the man in the black cap is replaced by ...",
 "fields": {"person": "...", "pose": "...", "background": "..."},
 "pose_fidelity": false, "output_size": "1280x720", "seconds": 15.3,
 "error": null, "options": {...}}
```
`status`: `queued` (+ `position`, `eta_seconds`) · `running` · `done` · `error` · `canceled`.
`error` = `{"code": "oom"|"comfy_rejected_graph"|"generation_failed", "detail": "one line"}`.
Other errors: 400 `missing_file`/`bad_output_size`/`bad_upscale`/`expected_multipart`,
413 `too_large` (60 MB), 429 `queue_full`, 503 `not_ready`.

## Measured (A100, 2026-09-27)

First job 15.3 s including loading FLUX; the notebook reports about 8 s per job
once warm. The first VLM call after boot took 41 s (warm-up); after that 0.2-0.4 s.

## Examples

```bash
B=http://body-swap.<tailnet>.ts.net
curl -s -X POST "$B/jobs?wait=120" -F scene=@scene.png -F character=@char.png -F idempotency_key=run1-p07
curl -s "$B/jobs/j_a6873773/image" -o out.png

# VLM: image as a data URL
python3 - <<'EOF'
import base64, json, urllib.request
img = base64.b64encode(open("scene.png", "rb").read()).decode()
body = {"messages": [{"role": "user", "content": [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}},
    {"type": "text", "text": "Describe this image in one sentence."}]}],
    "temperature": 0, "max_tokens": 80}
req = urllib.request.Request("http://body-swap.<tailnet>.ts.net/v1/chat/completions",
                             json.dumps(body).encode(), {"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req))["choices"][0]["message"]["content"])
EOF
```
The VLM supports `response_format: {"type": "json_schema", ...}` for structured output. The
pipeline uses it at temperature 0 because 0.2 gave different pose text between runs. It
has one slot and shares the GPU with FLUX, so heavy VLM traffic slows jobs down.
