#!/usr/bin/env python3
"""Drive a Colab GPU session from the terminal - one handle for every agent.

Wraps `colab exec/upload/download` the way session.sh / bench.sh / vlm.sh each
do by hand, and adds the one thing they don't have: `watch`, a bounded polling
loop cheap enough to hand to a Haiku subagent.

    driver.py sessions
    driver.py new     SESSION [--gpu A100]       # A100, falls back to G4 when all 3 A100s are taken
    driver.py probe   SESSION                    # retries; tells a websocket drop from a dead VM
    driver.py push    SESSION FILE...            # repo files -> VM, sha256-VERIFIED, exit 1 on mismatch
    driver.py py      SESSION [-f FILE]          # code on stdin otherwise
    driver.py start   SESSION NAME -- CMD...     # detached, log -> /content/jobs/NAME.log
    driver.py tail    SESSION NAME [CHARS]
    driver.py watch   SESSION NAME [opts]        # <- the monitoring loop; --sync makes it the puller too
    driver.py sync    SESSION REMOTE_DIR LOCAL_DIR   # fetch only files not already local
    driver.py sheet   SESSION OUT.jpg IMG...     # contact sheet built ON the VM, only the JPEG comes back
    driver.py pull    SESSION REMOTE LOCAL
    driver.py stop    SESSION

Exit codes: watch 0 done, 2 stalled, 3 timeout, 4 job died.
            probe 0 up, 5 dead (reclaimed), 6 unreachable but not proven dead.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("COLAB_STATE", Path.home() / ".config/colab-cli/sessions.json"))
JOBS = "/content/jobs"
def _repo() -> Path:
    # The skill lives in ~/.claude/skills, so the project is wherever the agent
    # is working: the git root of the cwd, else the cwd itself.
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(p.stdout.strip()) if p.returncode == 0 else Path.cwd()


REPO = _repo()
REMOTE_REPO = os.environ.get("COLAB_REMOTE_REPO", f"/content/{REPO.name}")


def colab(*args, stdin: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = ["colab", "--config", str(STATE), *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)


def remote(session: str, code: str, timeout: int = 300) -> str:
    """Run python on the VM, return stdout. Raises with stderr on failure."""
    # colab exec has its OWN idle-output timeout (short by default): a silent
    # ffmpeg encode dies with "TimeoutError: Timeout waiting for output" unless
    # it is raised to match ours.
    p = colab("exec", "-s", session, "--timeout", str(timeout), stdin=code, timeout=timeout + 30)
    if p.returncode != 0:
        raise SystemExit(f"[driver] exec failed on {session}:\n{p.stdout}\n{p.stderr}")
    return strip_banner(p.stdout)


def strip_banner(out: str) -> str:
    # colab CLI prints "[colab] A new version ..." plus a blank line on STDOUT
    # ahead of the kernel's output; left in, it corrupts every parsed reply.
    # (0.6.0 also puts a blank line BEFORE it.)
    lines = out.splitlines(keepends=True)
    hits = [i for i, l in enumerate(lines[:8]) if l.startswith("[colab] ")]
    if not hits:
        return out
    rest = lines[hits[-1] + 1:]
    if rest and not rest[0].strip():
        rest = rest[1:]
    return "".join(rest)


def known_sessions() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def require(session: str) -> None:
    # `colab status -s NAME` exits 0 for a session that does not exist, so the
    # CLI's own state file is the only reliable existence check.
    if session not in known_sessions():
        raise SystemExit(f"[driver] no session {session!r} in {STATE}. `driver.py sessions` lists them.")


# ---------------------------------------------------------------- commands

def cmd_sessions(a):
    s = known_sessions()
    if not s:
        print("(no sessions in %s)" % STATE)
        return
    for name, v in s.items():
        print(f"{name:<12} {v.get('accelerator','?'):<6} last_exec={(v.get('last_execution') or ['','',''])[2]}")


def cmd_new(a):
    if a.session in known_sessions():
        raise SystemExit(f"[driver] {a.session!r} already exists - pick another name")
    for gpu in [a.gpu] + (["G4"] if a.gpu == "A100" else []):
        p = colab("new", "-s", a.session, "--gpu", gpu, timeout=600)
        msg = p.stdout + p.stderr
        if a.session in known_sessions():
            print(f"[driver] {a.session} up on {gpu}")
            return 0
        if "TooManyAssignments" in msg or "Precondition Failed" in msg:
            # At most 3 A100s per account, often held by another agent in this
            # repo. Never stop someone else's VM to free a slot.
            print(f"[driver] no {gpu} slot free", flush=True)
            continue
        raise SystemExit(f"[driver] colab new failed:\n{msg[-2000:]}")
    raise SystemExit("[driver] no A100 or G4 slot free")


PROBE = r"""
import shutil, subprocess, os
try:
    gpu = subprocess.run(['nvidia-smi','--query-gpu=name,memory.used,memory.total,utilization.gpu',
                          '--format=csv,noheader'], capture_output=True, text=True).stdout.strip()
except Exception as e:
    gpu = 'no nvidia-smi: %s' % e
free = shutil.disk_usage('/content')
ram = subprocess.run(['free','-g'], capture_output=True, text=True).stdout.splitlines()[1]
print('gpu  ', gpu)
print('disk  %d GiB free of %d' % (free.free>>30, free.total>>30))
print('ram  ', ram)
# args truncated hard: a full nvcc command line is 600 chars of noise and this
# output is usually read by a small-model subagent.
ps = subprocess.run(['bash','-lc',"ps -eo pid,etime,rss,args --sort=-rss | sed -n '2,200p'"],
                    capture_output=True, text=True).stdout
# the heaviest processes (>100 MB resident) are the ones doing the work
keep = [l for l in ps.splitlines() if len(l.split(None, 3)) == 4 and l.split()[2].isdigit()
        and int(l.split()[2]) > 100 * 1024
        and 'colab_kernel' not in l and 'jupyter' not in l][:6]
print('procs')
for l in keep:
    pid, et, rss, rest = l.split(None, 3)
    print('  %-7s %-9s %5s MB  %s' % (pid, et, int(rss)//1024, rest[:90]))
if not keep:
    print('  (no job processes)')
for d in ('/content/jobs',):
    if os.path.isdir(d):
        print('jobs  ', ', '.join(sorted(os.listdir(d))) or '(empty)')
# The VM's clock and each log's age: elapsed time comes from here, never from
# counting your own turns.
import time
print('clock ', time.strftime('%H:%M:%S', time.gmtime()), 'UTC')
if os.path.isdir('/content/jobs'):
    for f in sorted(os.listdir('/content/jobs')):
        if f.endswith('.log'):
            print('  %-24s last write %5ds ago' % (f, time.time() - os.path.getmtime('/content/jobs/' + f)))
"""


def cmd_probe(a):
    """A websocket drop and a reclaimed VM look identical on one try. Retry;
    only a repeated 404/401 means the VM is gone."""
    require(a.session)
    last = ""
    for i in range(a.tries):
        try:
            p = colab("exec", "-s", a.session, stdin=PROBE, timeout=120)
            if p.returncode == 0 and "gpu" in p.stdout:
                print(p.stdout.rstrip())
                return 0
            last = (p.stdout + p.stderr).strip()
        except subprocess.TimeoutExpired:
            last = "exec timed out after 120s"
        print(f"[probe] try {i+1}/{a.tries} failed: {(last.splitlines() or ['?'])[-1][:200]}", flush=True)
        if i + 1 < a.tries:
            time.sleep(15)
    if any(k in last for k in ("404", "401", "Cleaning up")):
        print("[probe] VERDICT: DEAD (reclaimed). Unpulled results are gone; rebuild.")
        return 5
    print("[probe] VERDICT: UNREACHABLE, not proven dead. Retry later; do NOT rebuild.")
    return 6


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cmd_push(a):
    """Upload repo-relative files and verify each by sha256. A push that
    'succeeded' with a file missing has cost two full runs in this repo."""
    require(a.session)
    files = [Path(f) for f in a.files]
    for f in files:
        if f.is_absolute() or not (REPO / f).is_file():
            raise SystemExit(f"[push] not a repo-relative file: {f}")
    dirs = sorted({f"{a.remote_root}/{f.parent}" for f in files})
    remote(a.session, "import os\nfor d in %r: os.makedirs(d, exist_ok=True)\n" % dirs)
    for f in files:
        p = colab("upload", "-s", a.session, str(REPO / f), f"{a.remote_root}/{f}", timeout=900)
        if p.returncode != 0:
            print(f"[push] upload error {f}: {(p.stdout + p.stderr).strip()[-300:]}", flush=True)
    want = {str(f): _sha(REPO / f) for f in files}
    got = json.loads(remote(a.session, """
import hashlib, json, os
out = {}
for f in %r:
    p = os.path.join(%r, f)
    out[f] = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.exists(p) else None
print(json.dumps(out))
""" % (list(want), a.remote_root)).strip().splitlines()[-1])
    bad = [f for f in want if got.get(f) != want[f]]
    for f in bad:
        print(f"[push] MISMATCH {f}: {'missing on VM' if got.get(f) is None else 'hash differs'}")
    print(f"[push] {len(want) - len(bad)}/{len(want)} verified")
    return 1 if bad else 0


def _sync(session: str, rdir: str, ldir: Path) -> int:
    """Fetch every file under rdir that is missing locally or a different size."""
    listing = json.loads(remote(session, """
import json, os
out = {}
for root, _, fs in os.walk(%r):
    for f in fs:
        p = os.path.join(root, f)
        out[os.path.relpath(p, %r)] = os.path.getsize(p)
print(json.dumps(out))
""" % (rdir, rdir)).strip().splitlines()[-1])
    n = 0
    for rel, size in sorted(listing.items()):
        dst = ldir / rel
        if dst.exists() and dst.stat().st_size == size:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        p = colab("download", "-s", session, f"{rdir}/{rel}", str(dst), timeout=600)
        if p.returncode == 0:
            n += 1
        else:
            print(f"[sync] failed {rel}: {(p.stdout + p.stderr).strip()[-200:]}", flush=True)
    return n


def cmd_sync(a):
    require(a.session)
    n = _sync(a.session, a.remote_dir.rstrip("/"), Path(a.local_dir))
    print(f"[sync] fetched {n} new file(s) -> {a.local_dir}")


SHEET = r"""
import glob, json, os
from PIL import Image, ImageDraw, ImageFont
spec = json.loads(%r)
paths = [p for g in spec['imgs'] for p in (sorted(glob.glob(g)) or [g])]
cols, H = spec['cols'], spec['height']
font = ImageFont.load_default(size=max(12, H // 18))
tiles = []
for p in paths:
    try:
        im = Image.open(p).convert('RGB')
        im = im.resize((max(1, round(im.width * H / im.height)), H), Image.Resampling.LANCZOS)
    except Exception as e:
        im = Image.new('RGB', (H, H), 'gray'); p = '%%s (%%s)' %% (p, type(e).__name__)
    t = Image.new('RGB', (im.width, H + font.size + 8), 'white')
    t.paste(im, (0, font.size + 8))
    ImageDraw.Draw(t).text((4, 3), os.path.basename(p)[:60], fill='black', font=font)
    tiles.append(t)
rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
W = max(sum(t.width for t in r) for r in rows)
sheet = Image.new('RGB', (W, sum(max(t.height for t in r) for r in rows)), 'white')
y = 0
for r in rows:
    x = 0
    for t in r:
        sheet.paste(t, (x, y)); x += t.width
    y += max(t.height for t in r)
os.makedirs(os.path.dirname(spec['out']), exist_ok=True)
q = 88
while True:          # Notion rejects files over 5 MiB
    sheet.save(spec['out'], quality=q)
    if os.path.getsize(spec['out']) < 4.5 * 2**20 or q < 40:
        break
    q -= 12
print('SHEET', len(paths), 'images', sheet.size, os.path.getsize(spec['out']) >> 10, 'KB q', q)
"""


FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"   # present on Colab images

VSHEET = r"""
import json, os, subprocess, glob
spec = json.loads(__SPEC__)
vids = [p for g in spec['vids'] for p in (sorted(glob.glob(g)) or [g])]
H, cols, font = spec['height'], min(spec['cols'], len(vids)), __FONT__
def probe(p):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                        'stream=width,height:format=duration', '-of', 'json', p], capture_output=True, text=True)
    j = json.loads(r.stdout); s = j['streams'][0]
    return s['width'], s['height'], float(j['format']['duration'])
info = [probe(v) for v in vids]
W = int(round(info[0][0] * H / info[0][1] / 2) * 2)     # every tile gets the first clip's aspect
dur = min(d for _, _, d in info)                        # grid ends with the shortest clip
labels = spec['labels'] or [os.path.splitext(os.path.basename(v))[0] for v in vids]
esc = lambda t: t.replace('\\', '\\\\').replace(':', '\\:').replace("'", '')
chains = []
for i, lab in enumerate(labels):
    chains.append(f"[{i}:v]fps={spec['fps']},scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                  f"drawtext=fontfile={font}:text='{esc(lab)[:40]}':x=8:y=8:fontsize={max(14, H//18)}:"
                  f"fontcolor=white:box=1:boxcolor=black@0.6[v{i}]")
n = len(vids)
if n == 1:
    graph = chains[0].rsplit('[', 1)[0] + '[out]'
else:
    pad_to = -(-n // cols) * cols
    for j in range(n, pad_to):                          # blank tiles fill the last row
        chains.append(f"color=black:s={W}x{H}:r={spec['fps']}:d={dur}[v{j}]")
    layout = '|'.join(f"{(k % cols) * W}_{(k // cols) * H}" for k in range(pad_to))
    graph = ';'.join(chains) + ';' + ''.join(f'[v{k}]' for k in range(pad_to)) + \
            f'xstack=inputs={pad_to}:layout={layout}:shortest=1[out]'
os.makedirs(os.path.dirname(spec['out']), exist_ok=True)
for crf in (23, 28, 32, 36, 40):                        # Notion's upload cap is 5 MiB
    cmd = ['ffmpeg', '-y', '-v', 'error'] + sum((['-i', v] for v in vids), []) + [
           '-filter_complex', graph, '-map', '[out]', '-t', str(dur), '-an',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(crf), '-pix_fmt', 'yuv420p',
           '-movflags', '+faststart', spec['out']]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit('ffmpeg failed: ' + r.stderr[-1500:])
    if os.path.getsize(spec['out']) < 4.8 * 2**20:
        break
print('VSHEET', n, 'clips', '%.1fs' % dur, 'crf', crf, os.path.getsize(spec['out']) >> 10, 'KB')
if os.path.getsize(spec['out']) >= 5 * 2**20:
    print('OVER 5 MiB even at crf 40 - publish it with `driver.py drive` (Notion embeds the Drive link)')
"""

FRAMES = r"""
import json, os, subprocess
spec = json.loads(__SPEC__)
v, out, n = spec['video'], spec['out'], spec['n']
dur = float(json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
      '-of', 'json', v], capture_output=True, text=True).stdout)['format']['duration'])
os.makedirs(out, exist_ok=True)
# evenly spaced mid-interval samples skip the first/last frame (fades, encoder warm-up)
ts = ([t / 1000 for t in range(0, int(dur * 1000), int(spec['every'] * 1000))] if spec['every']
      else [dur * (i + 0.5) / n for i in range(n)])
rows = []
for t in ts:
    p = os.path.join(out, 't%07.2f.jpg' % t)
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', '%.3f' % t, '-i', v, '-frames:v', '1',
                    '-vf', 'scale=%d:-2' % spec['width'], '-q:v', '3', p], check=True)
    rows.append({'t': round(t, 2), 'path': os.path.basename(p)})
# jump = mean |pixel diff| (0-255) vs the previous sampled frame. The AI judge
# misses global jumps that still look plausible (an inverted test pattern);
# this number does not. Read it next to the judge, never instead of it.
try:
    from PIL import Image, ImageChops, ImageStat
    prev = None
    for r in rows:
        im = Image.open(os.path.join(out, r['path'])).convert('L').resize((128, 72))
        r['jump'] = None if prev is None else round(sum(ImageStat.Stat(ImageChops.difference(im, prev)).mean), 1)
        prev = im
except ImportError:
    pass
json.dump({'video': v, 'duration': dur, 'frames': rows}, open(os.path.join(out, 'frames.json'), 'w'), indent=1)
print('FRAMES', len(rows), 'from %.1fs' % dur)
"""

DRIVE = r"""
import os, shutil, time
src, sub = __SRC__, __SUB__
root = '/content/drive/MyDrive'
if not os.path.isdir(root):
    raise SystemExit('DRIVE NOT MOUNTED')
d = os.path.join(root, sub); os.makedirs(d, exist_ok=True)
dst = os.path.join(d, os.path.basename(src))
shutil.copy(src, dst)
# Colab's Drive FUSE exposes the file id as an xattr once the upload syncs
fid = None
for _ in range(60):
    try:
        fid = os.getxattr(dst, 'user.drive.id').decode(); break
    except OSError:
        time.sleep(2)
print('DRIVEID', fid)
"""


def cmd_vsheet(a):
    """Tile clips into one labelled grid video ON the VM (ffmpeg xstack),
    re-encoded under 5 MiB; pull only that."""
    require(a.session)
    rout = f"/content/sheets/{Path(a.out).name}"
    spec = json.dumps({"vids": a.vids, "cols": a.cols, "height": a.height, "fps": a.fps,
                       "labels": a.labels.split(",") if a.labels else None, "out": rout})
    print(remote(a.session, VSHEET.replace('__SPEC__', repr(spec)).replace('__FONT__', repr(FONT)), timeout=1800).strip().split('VSHEET', 1)[-1].join(['VSHEET', '']).strip())
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    p = colab("download", "-s", a.session, rout, a.out, timeout=900)
    if p.returncode != 0:
        raise SystemExit(p.stdout + p.stderr)
    print(f"<- {a.out}")


def cmd_frames(a):
    """Sample timestamped frames from a clip ON the VM for grading and pull them
    with frames.json. Grading reads a handful of frames, never every frame."""
    require(a.session)
    rdir = f"/content/frames/{Path(a.local_dir).name}"
    spec = json.dumps({"video": a.video, "out": rdir, "n": a.n, "every": a.every, "width": a.width})
    print(remote(a.session, FRAMES.replace('__SPEC__', repr(spec)), timeout=900).strip().splitlines()[-1])
    n = _sync(a.session, rdir, Path(a.local_dir))
    print(f"<- {a.local_dir} ({n} files)")


def cmd_drive(a):
    """Copy a VM file into the VM account's Google Drive and print a view link -
    for Notion media over the 5 MiB upload cap."""
    require(a.session)
    out = remote(a.session, DRIVE.replace('__SRC__', repr(a.remote)).replace('__SUB__', repr(a.folder)), timeout=900)
    if "DRIVE NOT MOUNTED" in out:
        raise SystemExit(f"[drive] Drive not mounted: run `colab drivemount -s {a.session}` "
                         "once (browser consent), then retry")
    fid = out.strip().splitlines()[-1].split()[-1]
    if fid in ("None", ""):
        raise SystemExit("[drive] copied, but Drive has not assigned an id yet - retry in a minute")
    print(f"https://drive.google.com/file/d/{fid}/view")


def cmd_sheet(a):
    """Build a labelled contact sheet from images already on the VM and pull
    only the JPEG - one ~1 MB file instead of N ~2.5 MB PNGs over the websocket."""
    require(a.session)
    rout = f"/content/sheets/{Path(a.out).name}"
    spec = json.dumps({"imgs": a.imgs, "cols": a.cols, "height": a.height, "out": rout})
    out = remote(a.session, SHEET % spec, timeout=600)
    print(out.strip().splitlines()[-1])
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    p = colab("download", "-s", a.session, rout, a.out, timeout=600)
    if p.returncode != 0:
        raise SystemExit(p.stdout + p.stderr)
    print(f"<- {a.out}")


def cmd_py(a):
    require(a.session)
    code = Path(a.file).read_text() if a.file else sys.stdin.read()
    t = time.time()
    out = remote(a.session, code, timeout=a.timeout)
    sys.stdout.write(out)
    print(f"[driver] {time.time()-t:.1f}s", file=sys.stderr)


def cmd_start(a):
    require(a.session)
    argv_b64 = base64.b64encode(json.dumps(a.cmd).encode()).decode()
    extra_env = dict(kv.split("=", 1) for kv in a.env)
    out = remote(a.session, f"""
import base64, json, os, subprocess, sys
os.makedirs({JOBS!r}, exist_ok=True)
argv = json.loads(base64.b64decode({argv_b64!r}))
if argv and argv[0] in ('python', 'python3'):
    argv[0] = sys.executable
log = os.path.join({JOBS!r}, {a.name!r} + '.log')
# start_new_session: the job must outlive this exec, or a 300s exec timeout
# kills a 40-minute benchmark.
p = subprocess.Popen(argv, cwd={a.cwd!r}, stdout=open(log, 'a'),
                     stderr=subprocess.STDOUT, start_new_session=True,
                     env=dict(os.environ, PYTHONUNBUFFERED='1', **{extra_env!r}))
open(os.path.join({JOBS!r}, {a.name!r} + '.pid'), 'w').write(str(p.pid))
print('pid', p.pid, '->', log)
""")
    print(out.strip())


def _tail(session: str, name: str, chars: int) -> str:
    return remote(session, f"""
import os
p = os.path.join({JOBS!r}, {name!r} + '.log')
print(open(p).read()[-{chars}:] if os.path.exists(p) else '(no log yet)')
""")


def _poll(session: str, name: str, offset: int, cap: int):
    """One exec returns: bytes appended since `offset`, the new offset, and
    whether the pid is still alive. One round trip, because every exec queues
    behind whatever else is talking to the same kernel and costs 2-3s."""
    out = remote(session, f"""
import os
d = {JOBS!r}
log, pidf = os.path.join(d, {name!r} + '.log'), os.path.join(d, {name!r} + '.pid')
pid = int(open(pidf).read()) if os.path.exists(pidf) else 0
# /proc/PID survives as a zombie because the job's parent is the long-lived
# Colab kernel, which never reaps it - state Z means finished, not running.
alive = 'GONE'
try:
    st = open('/proc/%d/stat' % pid).read().rsplit(')', 1)[1].split()[0]
    alive = 'GONE' if st == 'Z' else 'ALIVE'
except Exception:
    pass
off, new = {offset}, ''
if os.path.exists(log):
    size = os.path.getsize(log)
    if size < off:          # log was truncated or the job restarted
        off = 0
    with open(log, 'rb') as f:
        f.seek(off)
        new = f.read({cap}).decode('utf-8', 'replace')
    off = min(size, off + {cap})
print('%s %d' % (alive, off))
print(new, end='')
""")
    head, _, body = out.partition("\n")
    alive, _, off = head.strip().partition(" ")
    return body, int(off or 0), alive == "ALIVE"


def cmd_tail(a):
    require(a.session)
    sys.stdout.write(_tail(a.session, a.name, a.chars))


def cmd_watch(a):
    """Poll a detached job's log. Print only what is new. Stop on its own."""
    require(a.session)
    done = re.compile(a.done) if a.done else None
    deadline = time.time() + a.max_min * 60
    offset, stale, tail = 0, 0, ""
    while True:
        try:
            new, offset, alive = _poll(a.session, a.name, offset, a.chars)
        except SystemExit as e:                     # transient kernel hiccup
            print(f"[watch] exec error, retrying: {e}", flush=True)
            time.sleep(a.interval)
            continue
        if a.sync:              # the waiter is also the puller: a reclaim loses nothing already polled
            try:
                n = _sync(a.session, a.sync[0].rstrip("/"), Path(a.sync[1]))
                if n:
                    print(f"[watch] synced {n} new file(s) -> {a.sync[1]}", flush=True)
            except (SystemExit, subprocess.TimeoutExpired) as e:
                print(f"[watch] sync error, will retry: {str(e)[-200:]}", flush=True)
        if new.strip():
            sys.stdout.write(new if new.endswith("\n") else new + "\n")
            sys.stdout.flush()
            tail = (tail + new)[-4000:]
            stale = 0
        else:
            stale += 1
        if done and done.search(tail):
            print(f"[watch] DONE: /{a.done}/ matched", flush=True)
            return 0
        if not alive:
            print("[watch] EXIT: job process is gone (finished or crashed) - see the tail above", flush=True)
            return 4
        if time.time() > deadline:
            print(f"[watch] TIMEOUT after {a.max_min} min, job still running", flush=True)
            return 3
        if stale >= a.stall:
            print(f"[watch] STALLED: no new output for ~{stale * a.interval}s, job still running", flush=True)
            return 2
        time.sleep(a.interval)


def cmd_pull(a):
    require(a.session)
    Path(a.local).parent.mkdir(parents=True, exist_ok=True)
    p = colab("download", "-s", a.session, a.remote, a.local, timeout=600)
    if p.returncode != 0:
        raise SystemExit(p.stdout + p.stderr)
    print(f"<- {a.local}")


def cmd_stop(a):
    require(a.session)
    p = colab("stop", "-s", a.session, timeout=120)
    sys.stdout.write(p.stdout + p.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sessions").set_defaults(fn=cmd_sessions)

    p = sub.add_parser("new"); p.add_argument("session")
    p.add_argument("--gpu", default="A100"); p.set_defaults(fn=cmd_new)

    p = sub.add_parser("probe"); p.add_argument("session")
    p.add_argument("--tries", type=int, default=3); p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("push"); p.add_argument("session"); p.add_argument("files", nargs="+")
    p.add_argument("--remote-root", default=REMOTE_REPO); p.set_defaults(fn=cmd_push)

    p = sub.add_parser("sync"); p.add_argument("session"); p.add_argument("remote_dir")
    p.add_argument("local_dir"); p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("sheet"); p.add_argument("session"); p.add_argument("out")
    p.add_argument("imgs", nargs="+", help="remote paths or globs, in grid order")
    p.add_argument("--cols", type=int, default=4); p.add_argument("--height", type=int, default=384)
    p.set_defaults(fn=cmd_sheet)

    p = sub.add_parser("vsheet"); p.add_argument("session"); p.add_argument("out")
    p.add_argument("vids", nargs="+", help="remote clips or globs, in grid order")
    p.add_argument("--cols", type=int, default=2); p.add_argument("--height", type=int, default=360)
    p.add_argument("--fps", type=int, default=16); p.add_argument("--labels", help="comma-separated")
    p.set_defaults(fn=cmd_vsheet)

    p = sub.add_parser("frames"); p.add_argument("session"); p.add_argument("video")
    p.add_argument("local_dir"); p.add_argument("--n", type=int, default=8)
    p.add_argument("--every", type=float, help="seconds between frames (instead of --n)")
    p.add_argument("--width", type=int, default=768); p.set_defaults(fn=cmd_frames)

    p = sub.add_parser("drive"); p.add_argument("session"); p.add_argument("remote")
    p.add_argument("--folder", default="run-colab-media"); p.set_defaults(fn=cmd_drive)

    p = sub.add_parser("py"); p.add_argument("session"); p.add_argument("-f", "--file")
    p.add_argument("--timeout", type=int, default=300); p.set_defaults(fn=cmd_py)

    p = sub.add_parser("start"); p.add_argument("session"); p.add_argument("name")
    p.add_argument("--cwd", default=REMOTE_REPO)
    p.add_argument("--env", action="append", default=[], metavar="K=V",
                   help="extra env for the job, repeatable (e.g. --env TIER=fp8)")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("tail"); p.add_argument("session"); p.add_argument("name")
    p.add_argument("chars", nargs="?", type=int, default=2500); p.set_defaults(fn=cmd_tail)

    p = sub.add_parser("watch"); p.add_argument("session"); p.add_argument("name")
    p.add_argument("--interval", type=int, default=60, help="seconds between polls")
    p.add_argument("--max-min", type=int, default=60, help="give up after this many minutes")
    p.add_argument("--stall", type=int, default=5, help="polls with no new output before calling it stalled")
    p.add_argument("--done", default=r"\bDONE\b", help="regex that means finished")
    p.add_argument("--chars", type=int, default=4000)
    p.add_argument("--sync", nargs=2, metavar=("REMOTE_DIR", "LOCAL_DIR"),
                   help="each poll, also fetch new files - the waiter is the puller")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("pull"); p.add_argument("session"); p.add_argument("remote")
    p.add_argument("local"); p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("stop"); p.add_argument("session"); p.set_defaults(fn=cmd_stop)

    # Split the job command off by hand: argparse.REMAINDER would swallow --cwd.
    argv, job = sys.argv[1:], []
    if "--" in argv:
        i = argv.index("--")
        argv, job = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    if a.cmd == "start":
        if not job:
            raise SystemExit("[driver] start needs: ... start SESS NAME [--cwd D] -- python -u run.py ...")
        a.cmd = job
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
