"""Pitch sidecar for the teammate HTTP agent.

Replay is the default (the public Atira run that scored 100). Live mode starts
the unchanged package in ../pitch-agent and streams parsed stdout to the desk.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent / "pitch-agent"
WEB = HERE / "web"
GOLDEN = WEB / "golden.json"
LIVE_OUT = HERE / "data" / "live-out"

LINE = re.compile(
    r"^\[(?P<site>[^\]]+)\]\s+(?P<elapsed>\d+)s\s+(?P<rest>.*)$"
)
TOOL = re.compile(
    r"^(?P<action>view|submit|record|gaps|CIRCLE|BLOCKED)(?: repeat of)?\s+(?P<body>.*)$"
)
ROUND = re.compile(
    r"After round (?P<round>\d+) at (?P<t>\d+)s: (?P<pos>\d+) positions priced, "
    r"(?P<gaps>\d+) gaps"
)
BID = re.compile(r"^Bid at (?P<t>\d+)s")
PREFLIGHT = re.compile(r"^Preflight ok")
TRACE_LINE = re.compile(r"^\s{2}(.+)$")

lock = threading.Lock()
state: dict = {
    "mode": "idle",
    "started": None,
    "events": [],
    "answer": None,
    "error": None,
    "pid": None,
}
subscribers: list = []


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def emit(event: dict) -> None:
    event = dict(event)
    event.setdefault("wall", time.time())
    with lock:
        state["events"].append(event)
        watchers = list(subscribers)
    for push in watchers:
        try:
            push(event)
        except Exception:
            pass


def parse_line(raw: str) -> list[dict]:
    text = raw.rstrip("\n")
    if not text.strip():
        return []
    events: list[dict] = []
    if PREFLIGHT.search(text):
        events.append({"type": "metric", "title": "Preflight", "detail": text, "site": None})
    m = ROUND.search(text)
    if m:
        events.append({
            "type": "phase",
            "title": "Kernel",
            "detail": f"{m['pos']} positions priced · {m['gaps']} gaps",
            "t": int(m["t"]),
        })
    if BID.search(text):
        events.append({"type": "bid", "title": "Bid ready", "detail": text})
    if text.startswith("Trace:"):
        events.append({"type": "phase", "title": "Trace", "detail": "Resolver applying the brief."})
    worker = LINE.match(text)
    if not worker:
        if text.startswith("  ") and not text.startswith("   model"):
            events.append({"type": "trace", "title": text.strip(), "detail": text.strip()})
        elif text.startswith("Workdir") or text.startswith("Run "):
            events.append({"type": "phase", "title": "Run", "detail": text})
        return events
    site = worker["site"]
    elapsed = int(worker["elapsed"])
    rest = worker["rest"]
    tool = TOOL.match(rest)
    if rest.startswith("says:"):
        events.append({
            "type": "say",
            "site": site,
            "t": elapsed,
            "title": f"{site} done" if "complete" in rest.lower() or rest.endswith("done") else site,
            "detail": rest[5:].strip()[:220],
        })
        return events
    if not tool:
        events.append({"type": "metric", "site": site, "t": elapsed, "title": rest[:80], "detail": rest[:220]})
        return events
    action = tool["action"]
    body = tool["body"]
    kind = None
    title = action
    try:
        payload = json.loads(body) if body.startswith("{") else {}
    except ValueError:
        payload = {}
    if action == "record":
        kind = payload.get("kind")
        n = len(payload.get("records") or [])
        title = f"record {kind or ''}".strip()
        detail = f"{n} record(s)" if n else body[:180]
        events.append({
            "type": "record", "site": site, "t": elapsed, "kind": kind,
            "title": title, "detail": detail, "records": n,
        })
    elif action in ("view", "submit"):
        url = payload.get("url") or (f"link {payload.get('link')}" if payload.get("link") else site)
        events.append({
            "type": action, "site": site, "t": elapsed,
            "title": f"{action} {str(url)[:72]}", "detail": body[:220],
        })
    elif action == "gaps":
        events.append({
            "type": "gaps", "site": site, "t": elapsed,
            "title": "gaps", "detail": body[:220],
        })
    else:
        events.append({
            "type": "metric", "site": site, "t": elapsed,
            "title": action, "detail": body[:220],
        })
    return events


def run_agent() -> None:
    LIVE_OUT.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("RFQ_SUBMIT", "0")
    env["RFQ_OUT"] = str(LIVE_OUT)
    cmd = ["uv", "run", "--no-sync", "python", "-u", "main.py"]
    emit({"type": "phase", "title": "Live desk", "detail": "Starting pitch-agent (unchanged package)."})
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(AGENT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError as error:
        with lock:
            state["mode"] = "error"
            state["error"] = f"Cannot start agent: {error}. Install uv and open a scout shell."
        emit({"type": "metric", "title": "Error", "detail": state["error"]})
        return
    with lock:
        state["pid"] = proc.pid
        state["mode"] = "live"
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        for event in parse_line(line):
            emit(event)
    code = proc.wait()
    answer = LIVE_OUT / "answer.json"
    payload = json.loads(answer.read_text(encoding="utf-8")) if answer.exists() else None
    with lock:
        state["answer"] = payload
        state["mode"] = "done" if code == 0 else "error"
        if code != 0 and not state.get("error"):
            state["error"] = f"Agent exited {code}"
    if payload:
        emit({"type": "bid", "title": "Live bid", "detail": json.dumps(payload)[:400], "payload": payload})
    emit({"type": "score", "title": "Live finished", "detail": f"exit {code}"})


def start_live() -> dict:
    with lock:
        if state["mode"] == "live":
            return {"ok": False, "error": "A live desk is already running."}
        if not os.environ.get("CHALLENGE_RUN_TOKEN") and not os.environ.get("CHALLENGE_SCOUTING"):
            return {
                "ok": False,
                "error": "No run token and no scouting logins. In this terminal: atira scout --env, then start the server again.",
            }
        state.update(mode="starting", started=time.time(), events=[], answer=None, error=None, pid=None)
    threading.Thread(target=run_agent, daemon=True).start()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/live":
            self._json(200, start_live())
            return
        self._json(404, {"error": "not found"})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/replay":
            self._json(200, golden())
            return
        if path == "/api/status":
            with lock:
                snap = {
                    "mode": state["mode"],
                    "started": state["started"],
                    "events": len(state["events"]),
                    "error": state["error"],
                    "pid": state["pid"],
                    "answer": state["answer"],
                    "hasScout": bool(os.environ.get("CHALLENGE_SCOUTING") or os.environ.get("CHALLENGE_RUN_TOKEN")),
                }
            self._json(200, snap)
            return
        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            queue: list[dict] = []

            def push(event: dict) -> None:
                queue.append(event)

            with lock:
                replay = list(state["events"])
                subscribers.append(push)
            try:
                for event in replay:
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    if queue:
                        event = queue.pop(0)
                        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    else:
                        time.sleep(0.15)
            except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
                return
            finally:
                with lock:
                    if push in subscribers:
                        subscribers.remove(push)
            return
        self._static(path)

    def _static(self, path: str) -> None:
        root = WEB
        if not root.exists():
            self._json(503, {"error": "pitch/web is missing"})
            return
        if path == "/":
            path = "/index.html"
        target = (root / path.lstrip("/")).resolve()
        if root.resolve() not in target.parents and target != root.resolve():
            self._json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            target = root / "index.html"
        data = target.read_bytes()
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
        self.send_response(200)
        self.send_header("Content-Type", types.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    host, port = "127.0.0.1", int(os.environ.get("PITCH_PORT", "8787"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Quorum pitch server http://{host}:{port}", flush=True)
    print(f"Replay {GOLDEN.name} · agent {AGENT}", flush=True)
    if not os.environ.get("CHALLENGE_SCOUTING") and not os.environ.get("CHALLENGE_RUN_TOKEN"):
        print("Live desk needs scouting: atira scout --env   (then restart this server)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
