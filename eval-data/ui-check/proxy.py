"""Local test proxy: forwards to the backend and injects a Basic auth header.

    python proxy.py <listen_port> <backend_port> <user> <password>
"""

import base64
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

listen, backend, user, password = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
AUTH = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
PASS = ("content-type", "x-total-count")


class Handler(BaseHTTPRequestHandler):
    def _forward(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{backend}{self.path}", data=body, method=self.command,
            headers={"Authorization": AUTH,
                     "Content-Type": self.headers.get("Content-Type", "application/json")},
        )

        try:
            response = urllib.request.urlopen(request, timeout=120)
        except urllib.error.HTTPError as err:
            response = err

        payload = response.read()
        self.send_response(response.status if hasattr(response, "status") else response.code)

        for name in PASS:
            if response.headers.get(name):
                self.send_header(name, response.headers[name])

        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _forward

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", listen), Handler).serve_forever()
