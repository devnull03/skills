---
name: colab-tailscale-api
description: Turn a Colab GPU project (a notebook, a ComfyUI pipeline, a model script) into a private HTTP API on the user's Tailscale tailnet at a stable URL that survives VM restarts, via a Tailscale Service (svc:NAME). Covers the job-queue server (upload, poll or wait, download, idempotency), passing through a local model server such as llama.cpp's OpenAI endpoint, joining an ephemeral Colab VM to the tailnet in userspace mode, hosting the service, admin approval and ACLs, and smoke testing. Use it whenever someone wants to "expose", "serve", "make an API for", "put on tailscale" or "call from another project" anything that runs on Colab, or a Colab-like ephemeral GPU VM, even if they only say "api", "endpoint" or "server" and never mention Tailscale.
---

# Colab project → Tailscale API

The shape: your pipeline runs on a Colab VM. `scripts/job_server.py` wraps it in
a job queue on `127.0.0.1:PORT`. `tailscale serve --service=svc:NAME` publishes
it as `http://NAME.<tailnet>.ts.net`. The service's name and IP belong to the
tailnet, not the VM, so the next VM takes over the same address. There is no
public exposure and no API token: tailnet membership plus ACLs is the auth.

Worked example: body-swap, a character-replacement pipeline (ComfyUI + FLUX +
a local VLM, shipped as a Colab notebook) served this way.
`examples/body-swap-endpoints.md` is its caller doc; copy that shape for a new
API. VM work goes through the **run-colab** skill's `driver.py`. Load that skill
too, for its VM rules (A100 first, only the main agent stops VMs, pull results incrementally).

## 1. The user does this once per API (you can't)

Tell them exactly this, then wait:

1. **Auth key** (Settings → Keys): reusable, ephemeral, pre-authorized, tagged
   `tag:colab-temp`. Put it in the project's `.env` as `TS_AUTH_KEY="tskey-auth-..."`.
   One key can serve every project.
2. **Service** (Services → Create): name e.g. `my-api`, endpoint `tcp:80`.
   Ask them for the full domain it shows (`my-api.<tailnet>.ts.net`).
3. **Policy file**: auto-approve VMs as hosts, or every new VM waits for a click:
   ```json
   "autoApprovers": { "services": { "svc:my-api": ["tag:colab-temp"] } }
   ```
   plus a grant or ACL letting the calling devices reach `svc:my-api:80`.

Port: tell them **tcp:80** (plain HTTP over the tailnet, which is WireGuard-encrypted
already, so no certificates are needed). The server's own port (8000) never
appears in the service definition.

## 2. Wrap the pipeline

Write a pipeline module and run it under the generic server:

```python
# api/pipeline.py
REQUIRED_FILES = ("scene", "character")      # multipart file fields; 400 if missing
def boot():                                  # once, in the worker thread
    ...                                      # load models, start ComfyUI/llama-server
def run(job_dir, files, fields):             # one job at a time; files = {field: Path}
    ...                                      # write outputs into job_dir
    return {"result": "result.png", "seconds": 8.1}   # "result" is served at /jobs/{id}/result
```
```bash
python -u job_server.py --pipeline api/pipeline.py --port 8000 --proxy /v1=http://127.0.0.1:8189
```
This gives `GET /healthz` (503 until `boot()` returns), `GET /queue`,
`POST /jobs[?wait=N]`, `GET /jobs/{id}`, `GET /jobs/{id}/result`,
`DELETE /jobs/{id}`, and `idempotency_key` de-duplication. `--proxy` forwards a
path prefix unchanged to a local server; that's how body-swap exposes its VLM as
an OpenAI-compatible `/v1/chat/completions`. Copy `job_server.py` into the
project's repo (e.g. `api/`) so it gets pushed with the code.

**Reuse the project's real code path, not a reimplementation.** If the shipping
artifact is a notebook, execute its cells: body-swap runs the Install, Download
and Start cells in `boot()`, and Inputs and Run per job, in one namespace. That
guarantees API output equals notebook output. Two things break when you do this
(both hit on body-swap):
- `#@param` form lines (`scene_path = "" #@param`) reassign the values you set
  before exec. The cell then falls back to `google.colab.files.upload()` and dies with
  `'NoneType' object has no attribute 'kernel'`. Strip them:
  `re.sub(r"(?m)^\w+ = .*#@param.*$", "", src)`.
- Anything that needs a live kernel (`files.upload`, `eval_js`) fails in a
  detached process. Stubbing `IPython.display.display` is harmless, but it was not the fix.

## 3. Deploy (agent path)

From the project's repo root:

```bash
S=~/.claude/skills/colab-tailscale-api/scripts
$S/deploy.sh up myapi svc:my-api 8000 "python -u api/job_server.py --pipeline api/pipeline.py" \
    api/job_server.py api/pipeline.py <other repo files the pipeline needs>
$S/deploy.sh check http://my-api.<tailnet>.ts.net      # from the laptop, if it's on the tailnet
$S/deploy.sh down myapi
```
`up` creates an A100 through run-colab, or reuses the session if it exists. It
pushes the files, `.env` and `tailscale_up.sh` (sha256-verified), starts the
server detached, and blocks on `/healthz` for up to 20 min, because the first
boot downloads the models. Then it joins the tailnet and runs
`tailscale serve --service`. Steps can be re-run one at a time:
`deploy.sh ready myapi 8000`, `deploy.sh expose myapi svc:my-api 8000`.

A successful expose prints:
```
== hosting svc:body-swap -> 127.0.0.1:8000
This machine is configured as a service proxy for svc:body-swap, but approval from an admin is required.
http://body-swap.<tailnet>.ts.net/
```
The "approval required" line means step 1.3 wasn't done. Ask the user to approve
the host under Services, or to add the autoApprover.

**Smoke-test on the VM first, then over the tailnet.** On-VM tests
(`curl 127.0.0.1:8000`) separate app bugs from network ones. Write a
`smoke.sh BASE_URL` that hits every endpoint once, so the same script works for both.

## Gotchas (all hit for real)

- **Port 8080 is taken on Colab** (its Jupyter). `Address already in use`.
  Use 8000.
- **No TUN device and no CAP_NET_ADMIN on Colab.** `tailscaled` must run with
  `--tun=userspace-networking`. `tailscale_up.sh` does this. Inbound
  `serve --service` works in this mode. Verified 2026-09-27: the full smoke test
  passed from a laptop over the tailnet, including 2.4 MB image downloads.
- **Start `tailscaled` under `setsid`.** As a plain background child it was
  torn down when the launching job exited ("Client.Shutdown").
- **`backend error: invalid key: API key does not exist`** = the key in `.env` was
  revoked or expired. Only the user can mint a new one.
- **`tailscale serve --service` needs a tagged node** and Tailscale ≥ ~1.86;
  the install script gets 1.102.4, which has it. `--bg` is implied with `--service`.
- **Detached jobs don't see your env.** Source `.env` inside the command
  (`set -a; . ./.env; set +a`). `deploy.sh` does this for the Tailscale step;
  the server command needs it too if the pipeline reads keys.
- **`driver.py push` drops the exec bit.** Run scripts as `bash x.sh`.
- **`driver.py start` logs append.** A `watch --done` regex can match an
  old run's line. Give each attempt a new job name, or match on something unique.
- **Grep your own JSON carefully**: `*'"error"'*` matches the `"error": null`
  key in every job. Match `"status": "error"`.
- **Start fixed-allocation servers before adaptive ones.** llama-server allocates
  its KV cache up front and OOMs if a diffusion model (ComfyUI/FLUX) already holds
  the VRAM, so start it in `boot()` before the first job. The first
  VLM image call after boot took 41 s; after that 0.2–0.4 s.
- **A reclaimed VM means dead connections, not 503s.** The URL stays; nothing
  answers until the next `deploy.sh up`. Callers need retry with backoff and should
  check `/healthz`. If that's not acceptable, put a small always-on node in front
  that answers `503 Retry-After` while the GPU VM is away.
- **Handover (from Tailscale's docs, untested here):** a service can have several hosts,
  so bringing the new VM up before taking the old one `down` should avoid a gap.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `OSError: [Errno 98] Address already in use` | Port 8080: use 8000, or `pkill -f <server>` a previous copy |
| healthz stuck `starting` for >20 min | `driver.py tail SESSION server` - usually a model download; `BOOTFAIL` = boot() raised |
| every job `error` instantly, `'NoneType' ... 'kernel'` | notebook cell hit `files.upload`: strip `#@param` lines (above) |
| `invalid key: API key does not exist` | new auth key from the user into `.env`, re-run `deploy.sh expose` |
| expose prints "approval from an admin is required" | user approves the host or adds `autoApprovers.services` |
| `deploy.sh check` → `[000]` | host not approved yet, VM down, caller not on the tailnet, or the ACL doesn't grant `svc:NAME:80` |
