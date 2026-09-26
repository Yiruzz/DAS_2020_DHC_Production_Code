#!/usr/bin/env python3
"""Local stand-in for the Census dashboard endpoint.

Why this exists
---------------
`programs/dashboard.py:346 send_obj()` tries REST first and, when that fails,
falls through **unconditionally** to `SQS_Client().queue_message(...)`:

    r = send_url(surl)
    if r:
        return
    ...
    SQS_Client().queue_message(MessageBody=message)

`SQS_Client.__init__` calls `sqs_queue()` (`:187`), which calls
`boto3.resource('sqs', endpoint_url=...)`. On a host with no AWS region
configured that raises `botocore.exceptions.NoRegionError` -- inside the Spark
task. There is no config option that skips the fallback.

The call site that matters is `optimizer.py:797`, reached whenever
`report_reason` is non-empty, which `optimizer.py:760` sets as soon as
`model.NodeCount > 1` -- routine for the rounder MIP. So the optimisation stage
dies at the first node whose model branches.

Answering 200 makes `send_url` (`:330`, `return response.code == 200`) return
True, so `send_obj` returns at the `if r: return` above and boto3 is never
reached at all. No thread, no atexit handler, no SQS.

Side benefit: every message the DAS would have sent to the Census dashboard is
captured, which is evidence rather than noise.

Usage:  dashboard_sink.py [--port 8940] [--log FILE]
"""
import argparse
import datetime
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    logfile = None

    def _record(self, payload):
        row = {"t": datetime.datetime.now().isoformat(timespec="seconds"), **payload}
        line = json.dumps(row, default=str)
        if Handler.logfile:
            with open(Handler.logfile, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        else:
            print(line, flush=True)

    def _ok(self):
        body = b"OK\n"
        self.send_response(200)                  # dashboard.py:330 wants exactly 200
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        self._record({k: (v[0] if len(v) == 1 else v) for k, v in q.items()})
        self._ok()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        q = parse_qs(raw, keep_blank_values=True)
        self._record({k: (v[0] if len(v) == 1 else v) for k, v in q.items()} if q
                     else {"raw": raw})
        self._ok()

    def log_message(self, *args):
        pass                                      # keep it out of the run log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8940)
    ap.add_argument("--log", default=None, help="JSONL file; default stdout")
    args = ap.parse_args()

    Handler.logfile = args.log
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print(f"dashboard-sink: listening on http://127.0.0.1:{args.port} "
          f"-> {args.log or 'stdout'}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
