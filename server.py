#!/usr/bin/env python3
"""Profitell Find & Replace: local web app. Binds to 127.0.0.1 only; every API call needs the per-run token embedded in the page.
usage: python3 server.py [--port 5731] [--no-open]"""
import argparse, json, os, secrets, subprocess, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import replace_core as rc

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.Lock()   # one write operation at a time


def opts(d):
    return dict(regex=bool(d.get("regex")), case=bool(d.get("case")), whole_word=bool(d.get("whole_word")))


def pick_folder():
    if sys.platform != "darwin":
        raise rc.ReplaceError("The folder chooser is only available on macOS. Type or browse to the folder instead.")
    r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose the folder to search")'], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise rc.ReplaceError("No folder chosen.")
    return r.stdout.strip().rstrip("/") or "/"


def list_dirs(path):
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        raise rc.ReplaceError("That folder does not exist.")
    dirs = []
    try:
        for n in sorted(os.listdir(path), key=str.lower):
            if not n.startswith(".") and os.path.isdir(os.path.join(path, n)):
                dirs.append(n)
    except PermissionError:
        raise rc.ReplaceError("No permission to read that folder.")
    return {"path": path, "parent": os.path.dirname(path) if path != os.sep else None, "dirs": dirs}


class H(BaseHTTPRequestHandler):
    server_version = "ProfitellReplace/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def _auth(self):
        return self._host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        u = urlparse(self.path)
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
                page = f.read().replace("__TOKEN__", TOKEN)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if not self._auth():
            return self._send(403, {"error": "forbidden"})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/info":
                return self._send(200, {"version": "1.1", "features": ["exclusions"]})
            if u.path == "/api/ls":
                return self._send(200, list_dirs(q.get("path", "")))
            if u.path == "/api/history":
                return self._send(200, rc.history(q.get("root", "")))
            if u.path == "/api/diff":
                return self._send(200, rc.diff(q.get("root", ""), int(q.get("n", "0"))))
        except rc.ReplaceError as e:
            return self._send(400, {"error": str(e)})
        except (ValueError, OSError) as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._auth():
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            d = json.loads(self.rfile.read(min(n, 50_000_000)) or b"{}")
            if u.path == "/api/pick":
                return self._send(200, {"path": pick_folder()})
            if u.path == "/api/scan":
                return self._send(200, rc.scan(d.get("root"), d.get("find", ""), d.get("replace", ""), include=d.get("include", ""), exclude=d.get("exclude", ""), protect=d.get("protect", ""), skip_lines=d.get("skip_lines", ""), **opts(d)))
            with LOCK:
                if u.path == "/api/apply":
                    return self._send(200, rc.apply(d.get("root"), d.get("find", ""), d.get("replace", ""), d.get("selection") or {}, note=str(d.get("note", ""))[:200], protect=d.get("protect", ""), skip_lines=d.get("skip_lines", ""), **opts(d)))
                if u.path == "/api/goto":
                    return self._send(200, rc.goto(d.get("root"), int(d.get("n", 0)), force=bool(d.get("force"))))
        except rc.ReplaceError as e:
            return self._send(400, {"error": str(e)})
        except (ValueError, OSError, KeyError) as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PFR_PORT", "5731")))
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    srv, port = None, a.port
    for port in range(a.port, a.port + 20):   # if the port is busy, use the next free one
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"No free port found between {a.port} and {a.port + 19}.")
    url = f"http://localhost:{port}/"
    print(f"Profitell Find & Replace running at {url}  (history is kept in {rc.HOME})", flush=True)
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
