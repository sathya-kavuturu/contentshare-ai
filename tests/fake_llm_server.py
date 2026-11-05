"""A stand-in for llama-server, used by the tests: /health and /v1/chat/completions on this PC.

Run as ``python fake_llm_server.py --port N --api-key KEY``. The answer it gives is the JSON in
the environment variable FAKE_LLM_ANSWER, and the request it last received is written to the
file named by FAKE_LLM_LOG (one JSON line per request).
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--api-key", default="")
    args, _rest = parser.parse_known_args()
    answer = os.environ.get("FAKE_LLM_ANSWER", "{}")
    log_path = os.environ.get("FAKE_LLM_LOG", "")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:  # keep the test output quiet
            return

        def _send(self, status: int, payload: dict[str, object]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._send(200 if self.path == "/health" else 404, {"status": "ok"})

        def do_POST(self) -> None:
            if self.headers.get("Authorization") != f"Bearer {args.api_key}":
                self._send(401, {"error": "bad key"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
            if log_path:
                with Path(log_path).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(request) + "\n")
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": answer}}]})

    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
