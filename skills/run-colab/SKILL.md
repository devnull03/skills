---
name: run-colab
description: Run, monitor and benchmark any project on a Colab GPU session from the terminal - create an A100 VM (G4 fallback), push code with sha256 verification, start detached jobs, watch a running job while pulling every output as it lands, tail logs, probe a VM that looks dead, build image contact sheets and video grids on the VM, sample video frames, stop the VM. Use it whenever work happens on a Colab VM (the `colab` CLI), and to delegate long log-watching to a cheap subagent model.
---

# Run on Colab

GPU work runs on a Colab VM driven by the `colab` CLI. **`driver.py` (next to
this file) is the one handle on it**: python3 stdlib only, cheap enough to hand
to a Haiku subagent. Project-specific setup (installing the model server,
downloading weights) belongs in the project's own wrapper scripts; everything
generic goes through the driver. If the project has notes on its Colab setup
(e.g. a `docs/` file named in its CLAUDE.md), read them too.

Run the driver from inside the project. Paths are relative to the project root,
which the driver takes from `git rev-parse` of the cwd. `push` and
`start --cwd` default to `/content/<project dir name>` on the VM (override:
`COLAB_REMOTE_REPO`). `D=~/.claude/skills/run-colab/driver.py`.

**Three rules that cost the most when broken:**
1. **The VM bills until `stop`. Only the main agent stops a VM, never a
   subagent, and never another agent's VM** (check `sessions`; a VM you did not
   create is not yours).
2. **Colab reclaims VMs** (A100s after 40-90 min, 6-10 lost per long session).
   Pull every result as it lands (`watch --sync`), never at the end. Reclaims
   have cost whole runs of finished outputs.
3. **Verify code landed before a run** (`push` does this). A silent missing
   file has cost two full runs.

## Prerequisites

```bash
colab --help                  # Colab CLI on PATH, already authenticated
set -a; . ./.env; set +a      # the project's keys, if it has any (values must be quoted)
```

## Run (agent path) - the whole lifecycle

```bash
D=~/.claude/skills/run-colab/driver.py
python3 $D sessions                 # what exists - the CLI's state file is the only truth
python3 $D new myexp                # A100; if all 3 A100 slots are taken, falls back to G4 (~14 s)
python3 $D probe myexp              # GPU/disk/RAM/procs + VM clock + age of each job log

# code/inputs -> VM, sha256-verified, exit 1 on any mismatch
python3 $D push myexp run.py configs/exp1.json inputs/a.png
python3 $D start myexp setup -- bash -lc 'bash setup.sh; echo SETUP DONE'   # the project's own setup

# start DETACHED (survives exec timeouts and websocket drops); log -> /content/jobs/NAME.log
python3 $D start myexp run1 --cwd /content/<project> -- \
  bash -lc 'python -u run.py ... ; echo RUN1 DONE'

# block until done, printing only new log bytes AND pulling each new output file as it appears
python3 $D watch myexp run1 --interval 60 --max-min 90 --done 'RUN1 DONE' \
  --sync /content/out results/run1

python3 $D tail myexp run1 2500                     # one-shot look
python3 $D sync myexp /content/out results/run1     # one-shot incremental pull
python3 $D sheet myexp results/run1/sheet.jpg '/content/out/*.png' --cols 4 --height 384
echo 'print(1+1)' | python3 $D py myexp             # a python snippet on the VM (--timeout N)
python3 $D stop myexp
```

Exit codes: **watch** `0` done regex matched · `2` stalled (no output for
`--stall` polls, process alive) · `3` `--max-min` hit · `4` process gone
without the done line. **probe** `0` up · `5` dead (repeated 404/401, VM
reclaimed: rebuild) · `6` unreachable but not proven dead (**do not
rebuild**; twice a "dead" VM was still generating).

## Contact sheets: build them on the VM

Pulling 40 x 2.5 MB PNGs to eyeball them is slow over the websocket and dies
with a reclaim. Build the sheet where the images are and pull one JPEG:

```bash
python3 $D sheet myexp out/sheet.jpg '/content/out/*.png' --cols 4 --height 384
```

Globs are expanded on the VM in order; each tile is labelled with its filename.
It re-saves at lower JPEG quality until the file is under 4.5 MB, small enough for
upload caps like Notion's 5 MiB. For custom layouts, push your own sheet script and
`start` it the same way, then `pull` the `.jpg`. Sheets are for spotting what moved;
pixel comparisons need the full-resolution files.

## Video: grid videos and frame sampling

ffmpeg 6.1 ships on the Colab image with `xstack` and `drawtext`, so this runs
on the VM and only small files come back.

```bash
# labelled side-by-side grid of clips, re-encoded under 5 MiB
python3 $D vsheet myexp out/grid.mp4 /content/out/armA.mp4 /content/out/armB.mp4 /content/out/armC.mp4 \
  --cols 2 --labels "arm A,arm B,arm C"
# -> VSHEET 3 clips 4.0s crf 23 497 KB

# N timestamped frames per clip (for a vision model or a person) + frames.json
python3 $D frames myexp /content/out/armA.mp4 results/vid/armA --n 8     # or --every 0.5
```

- `vsheet`: every tile takes the first clip's aspect ratio (others are
  letterboxed), the frame rate is normalised (`--fps`, default 16), the grid ends
  with the shortest clip, and empty cells are black. It steps CRF 23→40 until
  the file is under 4.8 MB; if even CRF 40 is over, it prints `OVER 5 MiB` (then
  `driver.py drive SESSION FILE` uploads it to the Colab account's Drive - needs a
  browser consent click per VM, see Gotchas).
- `frames`: evenly spaced mid-interval samples (skips the first/last frame),
  768 px wide JPEGs named `tSSSS.ss.jpg`, and `frames.json` with each frame's
  `t` and **`jump`**: mean |pixel diff| (0-255) from the previous sample. On a
  test clip, clean frames stayed under 10.2, an injected colour inversion scored
  108.7 and a cut 55. **More than ~40 between neighbouring samples is a global
  jump worth a look.** Read it alongside a vision-model or human check, not instead of one.

## Delegate: who does what

`watch` alone is the big win (one blocking call instead of N polling turns, ~10x).
A haiku subagent is a further ~2x and frees the main model, but costs ~23k
tokens before it does anything, so **don't delegate a 30-second check.**

| Work | Model |
|---|---|
| Research, reading images, deciding the next run, `new`/`stop`, code changes | main model |
| A long `watch` (with `--sync`), big logs, pass/fail counts | **haiku** |
| Publishing already-decided results (docs, uploads) | **sonnet** |

Watcher prompt (fill in the placeholders; keep the output shape):

```
Agent(model: "haiku", subagent_type: "general-purpose", description: "Watch colab job", prompt: """
Run exactly this one command (it blocks up to 90 min) and nothing else:

cd <repo root> && python3 ~/.claude/skills/run-colab/driver.py watch <SESSION> <JOB> --interval 60 --max-min 90 --done '<DONE REGEX>' --sync <REMOTE_OUT_DIR> <LOCAL_DIR>

Exit code: 0=done, 2=stalled, 3=timeout, 4=process died.
Reply with AT MOST 6 lines, exactly:
STATUS: <done|stalled|timeout|died>
SYNCED: <sum of the "synced N new file(s)" counts>
ERRORS: <traceback/OOM/CUDA line verbatim, max 2 lines, or "none">
LAST: <final non-empty log line, verbatim>
Do not interpret results, read other files, or start/stop/probe anything.
""")
```

## Waiting correctly

- **`run_in_background: true` returns immediately**; it is not a pause. A
  string of background `sleep`s has led to dozens of polls in a few minutes and
  misreading 30 s of silence as a stall. To block, run `watch` in the
  foreground (or in a subagent).
- **One watcher per job, and kill it (`TaskStop`) when the job or its VM
  ends.** Stale watchers each hold a queued exec on the single kernel and make
  every later command time out (~1 h lost once).
- Judge elapsed time from `probe`'s `clock` and `last write Ns ago` lines,
  never from your own turn count.

## Gotchas

- **colab CLI 0.6.0 prints an update banner on STDOUT**, with a blank line
  before it, ahead of the kernel's output. It broke `watch` (exit 4 in 2 s on
  a live job) until `strip_banner()` was added. Any hand-rolled
  `colab exec | parse` needs the same, or set
  `"enable_update_check": false` in `~/.config/colab-cli/settings.json`.
- **`colab status -s <nonexistent>` exits 0.** Existence = `driver.py sessions`
  (reads `~/.config/colab-cli/sessions.json`).
- **One `colab` caller at a time.** Two concurrent CLI calls can clobber
  `sessions.json` and drop a live session from it; the VM keeps billing,
  unreachable. Re-run `sessions` before acting; never cache it.
- **One kernel per VM: every exec serialises.** A trivial exec measured 20 s
  while a build saturated the CPUs; don't poll faster than ~60 s.
- **3 A100s per account** (`TooManyAssignmentsError ... Precondition Failed`).
  `new` falls back to G4 (RTX PRO 6000, 96 GB). Never stop another agent's VM
  for a slot.
- **Finished jobs linger as zombies** (the kernel never reaps them): liveness
  must read `/proc/PID/stat` state, which the driver does.
- **`start` needs `--` before the command**; always end the command with an
  `echo SOMETHING DONE` and pass that as `--done`.
- **Job logs append.** Re-using a job name keeps the old log, so a `--done`
  regex can match the previous run's line. Use a new name per attempt.
- **`push` drops the exec bit.** Run pushed scripts as `bash x.sh`.
- **Port 8080 on the VM is Colab's own Jupyter.** Servers you start need another port.
- **Env for detached jobs:** they do not inherit your laptop env. Source a
  pushed env file in the command (`set -a; . ./.env; set +a`) or pass `--env K=V`.
- **Setup is idempotent; a transient pip failure is normal.** Re-run setup
  before concluding anything is broken. Cache deterministic pre-passes locally
  and re-`push` them to the next VM.
- **Model loads print nothing for minutes.** Exit 2 during a load is normal;
  re-run `watch` or raise `--stall`.
- **`colab exec` has its own idle-output timeout, shorter than ours.** A silent
  ffmpeg encode died with `TimeoutError: Timeout waiting for output` - and a
  loop of encodes silently stopped after the first. `remote()` passes
  `--timeout`; `py` takes `--timeout N` (default 300) for long snippets.
- **Drive needs a browser consent click per VM.** `colab drivemount -s NAME`
  prints an accounts.google.com URL and waits for Enter; an agent cannot do it.
  The Drive is the **Colab account's**, and files there are private until shared.
- **A scrubbed env (`env -i`) makes the CLI hang** waiting to re-auth. Run the
  driver from your normal shell.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `watch` exits 4 within seconds on a job you know is running | Banner parsing (see Gotchas) if you edited `remote()`; otherwise `tail` - it really crashed. |
| `probe` exit 6 | Websocket drop. Wait a minute and probe again; check the job log grows. Don't rebuild. |
| `probe` exit 5 | Reclaimed. Whatever was not synced is gone. `new` + setup + re-push cached intermediates. |
| `[driver] no session 'X'` | Not in the state file (stopped, reclaimed, or clobbered). `sessions`. |
| `[push] MISMATCH` / exit 1 | Re-run `push`; do not start the job until it prints `N/N verified`. |
| `[push] not a repo-relative file` | `push` only takes paths inside the repo; copy scratch files in first. |
| `new`: `no A100 or G4 slot free` | All GPU slots held; ask the user rather than stopping anything. |

## Sharing

Tracked at github.com/devnull03/skills. Install by symlinking
`skills/run-colab` into `~/.claude/skills/`. Users need the `colab` CLI
authenticated with their own account. Scripts can find the driver at
`~/.claude/skills/run-colab/driver.py`, or at `$COLAB_DRIVER`.
