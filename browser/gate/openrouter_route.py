"""A local pass-through to OpenRouter that prefers the providers each model cites its evidence correctly on.

fastbrowse's reader must cite the page block behind each piece of evidence (`cite: {first, last}`), or fastbrowse
discards it and the run ends `stuck` on a page that shows the answer. The schema lets `cite` be null, so strict JSON
cannot force it, and the same open-weight model differs widely by OpenRouter provider. For MiniMax M3.1 Flash the
difference is reasoning: providers that let it reason cite correctly, InferenceNet (OpenRouter's usual pick) runs
it with no reasoning and does not. Replaying real fastbrowse reader requests:

    minimax/minimax-m3.1-flash  ModelRun, low effort 8/8 (median 3.5s); InferenceNet 3/8 at any effort;
                                SambaNova 12/12 but ~68s a read, past fastbrowse's 30s per call
    z-ai/glm-5.3                Wafer 3/3; InferenceNet, Morph, DeepInfra, Together 1/3; Fireworks, Parasail 0/3;
                                Z.AI, Baidu, Novita rejected the request

fastbrowse's OpenRouter preset sets `provider` itself and will not let it be overridden, so `run.py` points
fastbrowse's `custom` endpoint here. Each preferred provider gets a short deadline on its own, then OpenRouter's
routing takes over: a provider can accept a call and never answer, and OpenRouter only falls back on errors. Like
fastbrowse's preset, every attempt requires providers that support the request's parameters (the JSON schema), and
the reasoning effort fastbrowse would send itself (low) is added, since the custom endpoint sends none. It also asks
OpenRouter to report each call's cost (`usage.include`), which fastbrowse meters.
"""

import atexit
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

OPENROUTER = "https://openrouter.ai/api/v1"

PREFERRED = {
    "minimax/minimax-m3.1-flash": ["ModelRun"],
    "z-ai/glm-5.3": ["Wafer"],
}

REASONING = {"minimax/minimax-m3.1-flash": "low"}

_log_lock = threading.Lock()


PROVIDER_DEADLINE = 10
"""Seconds to wait on one preferred provider before trying the next. A read normally takes 2-5s, but a provider can
also accept a call and never answer (SambaNova did, intermittently), and OpenRouter only falls back on errors. Two
tries and the open-routed last resort must fit inside fastbrowse's own 30s per call."""
LAST_RESORT_DEADLINE = 25


class _Handler(BaseHTTPRequestHandler):
    client: httpx.Client

    def _body(self) -> dict:
        if length := self.headers.get("Content-Length"):
            return json.loads(self.rfile.read(int(length)))
        chunks = []
        while size := int(self.rfile.readline().split(b";")[0], 16):
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        self.rfile.readline()
        return json.loads(b"".join(chunks))

    def _post(self, body: dict, timeout: float) -> httpx.Response:
        return self.client.post(
            OPENROUTER + self.path.removeprefix("/v1"),
            json=body,
            headers={"Authorization": self.headers["Authorization"]},
            timeout=timeout,
        )

    def do_POST(self) -> None:
        body = self._body()
        body["usage"] = {"include": True}
        if (effort := REASONING.get(body.get("model"))) and "reasoning" not in body:
            body["reasoning"] = {"effort": effort}
        # Each preferred provider alone and briefly, then OpenRouter's own routing among the ones that support the
        # request's parameters (the JSON schema), as fastbrowse's OpenRouter preset asks for.
        attempts = [({"order": [p], "allow_fallbacks": False, "require_parameters": True}, PROVIDER_DEADLINE)
                    for p in PREFERRED.get(body.get("model"), [])]
        attempts.append(({"require_parameters": True, "allow_fallbacks": True}, LAST_RESORT_DEADLINE))
        upstream = None
        for provider, deadline in attempts:
            try:
                upstream = self._post({**body, "provider": provider}, deadline)
            except httpx.HTTPError:
                continue
            if upstream.status_code == 200:
                break
        if upstream is None:
            self.send_response(504)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if log := os.environ.get("OPENROUTER_ROUTE_LOG"):
            # Prompts carry page text and replies carry answers: keep this log local, and delete it after debugging.
            with _log_lock, open(log, "a") as f:
                f.write(json.dumps({"request": body, "status": upstream.status_code, "response": upstream.text}) + "\n")
        self.send_response(upstream.status_code)
        self.send_header("Content-Type", upstream.headers.get("content-type", "application/json"))
        self.send_header("Content-Length", str(len(upstream.content)))
        self.end_headers()
        self.wfile.write(upstream.content)

    def log_message(self, *args) -> None:
        pass


def start() -> str:
    """Serve in a child process on a free local port; returns the base URL to give fastbrowse.

    Not a thread: fastbrowse's Rust core makes its LLM requests from this same process, and a proxy thread that has to
    wait for the interpreter lock while the core is busy in Python answered too late. fastbrowse gives up on a call
    after 30s, and runs ended `unavailable` (bench round 8) or stalled for minutes (round 5).
    """
    child = subprocess.Popen([sys.executable, __file__], stdout=subprocess.PIPE, text=True)
    atexit.register(child.terminate)
    return child.stdout.readline().strip()


def _serve() -> None:
    _Handler.client = httpx.Client()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    print(f"http://127.0.0.1:{server.server_address[1]}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    _serve()
