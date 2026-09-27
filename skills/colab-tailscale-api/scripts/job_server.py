#!/usr/bin/env python3
"""Generic GPU job API for a Colab VM: FIFO queue, one worker, stdlib only.

You write a pipeline module with two functions; this file does the HTTP side.

    # mypipe.py
    def boot():                        # once, in the worker thread: load models, start servers
        ...
    def run(job_dir, files, fields):   # per job; files = {field: Path}, fields = {name: str}
        ...                            # write outputs into job_dir
        return {"result": "result.png", "anything": "json-able"}   # "result" = file to serve

    python job_server.py --pipeline mypipe.py --port 8000 \
        [--proxy /v1=http://127.0.0.1:8189]      # pass a path prefix through to a local server

Endpoints: GET /healthz, GET /queue, POST /jobs[?wait=N] (multipart),
GET /jobs/{id}, GET /jobs/{id}/result, DELETE /jobs/{id}, plus any --proxy prefixes.
Listens on 127.0.0.1 only: `tailscale serve` is the way in, the tailnet ACL is the auth.
"""
from __future__ import annotations

import argparse
import email.parser
import email.policy
import importlib.util
import json
import mimetypes
import os
import queue
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

JOBS_DIR = Path(os.environ.get("API_JOBS_DIR", "/content/api_jobs"))
CAPACITY = int(os.environ.get("API_CAPACITY", "50"))
MAX_BODY = 60 * 2**20

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
Q: queue.Queue[str] = queue.Queue()
STATE = {"status": "starting", "detail": "", "median_s": 10.0}
DURATIONS: list[float] = []
PIPE = None
PROXIES: dict[str, str] = {}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def save_meta(job: dict) -> None:
    (JOBS_DIR / job["job_id"] / "meta.json").write_text(json.dumps(job, indent=2, default=str))


def public(job: dict) -> dict:
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    if job["status"] == "queued":
        with LOCK:
            ahead = sum(j["status"] == "queued" and j["_seq"] < job["_seq"] for j in JOBS.values())
        out.update(position=ahead, eta_seconds=round((ahead + 1) * STATE["median_s"]))
    return out


def worker() -> None:
    try:
        STATE["detail"] = "boot"
        PIPE.boot()
        STATE.update(status="ready", detail="")
    except Exception as e:
        STATE.update(status="error", detail=f"boot failed: {e!r}"[:500])
        traceback.print_exc()
        return
    while True:
        job = JOBS[Q.get()]
        if job["status"] != "queued":
            continue
        job.update(status="running", started_at=now())
        save_meta(job)
        t0 = time.time()
        d = JOBS_DIR / job["job_id"]
        try:
            out = PIPE.run(d, {k: d / v for k, v in job["_files"].items()}, job["fields"]) or {}
            job["_result"] = out.pop("result", None)
            job.update(status="done", output=out)
            DURATIONS.append(time.time() - t0)
            last = sorted(DURATIONS[-50:])
            STATE["median_s"] = last[len(last) // 2]
        except Exception as e:
            msg = str(e) or repr(e)
            code = "oom" if "out of memory" in msg.lower() else "job_failed"
            job.update(status="error", error={"code": code, "detail": msg.splitlines()[0][:300]})
            traceback.print_exc()
        job["finished_at"] = now()
        save_meta(job)


def parse_multipart(ctype: str, body: bytes) -> dict[str, tuple[str | None, bytes]]:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {ctype}\r\n\r\n".encode() + body)
    parts = {}
    for p in msg.iter_parts():
        name = p.get_param("name", header="content-disposition")
        if name:
            parts[name] = (p.get_filename(), p.get_payload(decode=True) or b"")
    return parts


class Handler(BaseHTTPRequestHandler):
    server_version = "colab-job-api/1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{now()} {fmt % args}\n")

    def send_bytes(self, code: int, data: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code: int, obj) -> None:
        self.send_bytes(code, json.dumps(obj, default=str).encode(), "application/json")

    def err(self, code: int, error: str, **extra) -> None:
        self.send_json(code, {"error": error, **extra})

    def route(self):
        u = urllib.parse.urlsplit(self.path)
        return [p for p in u.path.split("/") if p], urllib.parse.parse_qs(u.query)

    def proxied(self, method: str) -> bool:
        target = next((t for pre, t in PROXIES.items() if self.path.startswith(pre)), None)
        if target is None:
            return False
        n = int(self.headers.get("Content-Length") or 0)
        req = urllib.request.Request(target + self.path, data=self.rfile.read(n) if n else None, method=method,
                                     headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                self.send_bytes(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self.send_bytes(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            self.err(502, "upstream_unavailable", detail=repr(e)[:200])
        return True

    def do_GET(self):
        if self.proxied("GET"):
            return
        parts, _ = self.route()
        if parts == ["healthz"]:
            return self.send_json(200 if STATE["status"] == "ready" else 503,
                                  {**STATE, "median_s": round(STATE["median_s"], 1)})
        if parts == ["queue"]:
            with LOCK:
                depth = sum(j["status"] == "queued" for j in JOBS.values())
                running = next((j["job_id"] for j in JOBS.values() if j["status"] == "running"), None)
            return self.send_json(200, {"depth": depth, "running": running, "capacity": CAPACITY})
        if len(parts) >= 2 and parts[0] == "jobs":
            job = JOBS.get(parts[1])
            if not job:
                return self.err(404, "no_such_job")
            if len(parts) == 2:
                return self.send_json(200, public(job))
            if parts[2:] == ["result"]:
                if job["status"] != "done" or not job.get("_result"):
                    return self.err(404, "not_ready", status=job["status"])
                p = JOBS_DIR / job["job_id"] / job["_result"]
                return self.send_bytes(200, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        self.err(404, "not_found")

    def do_DELETE(self):
        parts, _ = self.route()
        job = JOBS.get(parts[1]) if len(parts) == 2 and parts[0] == "jobs" else None
        if not job:
            return self.err(404, "no_such_job")
        if job["status"] == "queued":
            job.update(status="canceled", finished_at=now())
            save_meta(job)
        self.send_json(200, public(job))

    def do_POST(self):
        if self.proxied("POST"):
            return
        parts, qs = self.route()
        if parts != ["jobs"]:
            return self.err(404, "not_found")
        if STATE["status"] == "error":
            return self.err(503, "not_ready", detail=STATE["detail"])
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self.err(400, "expected_multipart")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self.err(413, "too_large", limit_mb=MAX_BODY >> 20)
        form = parse_multipart(ctype, self.rfile.read(n))
        files = {k: v for k, v in form.items() if v[0] is not None}
        fields = {k: v[1].decode(errors="replace").strip() for k, v in form.items() if v[0] is None}
        missing = [f for f in getattr(PIPE, "REQUIRED_FILES", ()) if not files.get(f, (0, b""))[1]]
        if missing:
            return self.err(400, "missing_file", fields=missing)
        key = fields.pop("idempotency_key", "") or None
        with LOCK:
            prior = key and next((j for j in JOBS.values() if j["idempotency_key"] == key
                                  and j["status"] not in ("error", "canceled")), None)
            if prior:
                return self.send_json(200, public(prior))
            if sum(j["status"] == "queued" for j in JOBS.values()) >= CAPACITY:
                return self.err(429, "queue_full", capacity=CAPACITY)
            jid = "j_" + os.urandom(4).hex()
            d = JOBS_DIR / jid
            d.mkdir(parents=True)
            saved = {}
            for name, (fname, data) in files.items():
                saved[name] = f"in_{name}{Path(fname).suffix.lower()}"
                (d / saved[name]).write_bytes(data)
            job = {"job_id": jid, "status": "queued", "created_at": now(), "started_at": None,
                   "finished_at": None, "idempotency_key": key, "fields": fields, "error": None,
                   "_files": saved, "_seq": len(JOBS)}
            JOBS[jid] = job
        save_meta(job)
        Q.put(jid)
        wait = min(float((qs.get("wait") or ["0"])[0] or 0), 600)
        deadline = time.time() + wait
        while time.time() < deadline and job["status"] in ("queued", "running"):
            time.sleep(0.25)
        self.send_json(202 if job["status"] in ("queued", "running") else 200, public(job))


def main() -> None:
    global PIPE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pipeline", required=True, help="python file defining boot() and run()")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)  # 8080 is Colab's own Jupyter
    ap.add_argument("--proxy", action="append", default=[], metavar="/PREFIX=http://127.0.0.1:PORT")
    args = ap.parse_args()
    for p in args.proxy:
        pre, target = p.split("=", 1)
        PROXIES[pre] = target.rstrip("/")
    spec = importlib.util.spec_from_file_location("pipeline", args.pipeline)
    PIPE = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(args.pipeline).resolve().parent))
    spec.loader.exec_module(PIPE)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listening on http://{args.host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
