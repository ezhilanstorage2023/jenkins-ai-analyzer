"""A tiny real SMTP server for tests: speaks enough of the protocol for
smtplib (EHLO, AUTH PLAIN/LOGIN, MAIL, RCPT, DATA, QUIT) over a local socket.

The point is to exercise the actual smtplib code path, including the failure
modes that decide "failed" versus "uncertain", not a mock of it.
"""

import base64
import socket
import socketserver
import threading
from email import message_from_bytes, policy


class FakeSmtp:
    def __init__(
        self,
        *,
        auth_ok=True,
        reject_rcpt=(),
        data_reply=b"250 2.0.0 queued",
        drop_after_data=False,
        advertise_auth=True,
    ):
        self.auth_ok = auth_ok
        self.reject_rcpt = {r.lower() for r in reject_rcpt}
        self.data_reply = data_reply
        self.drop_after_data = drop_after_data
        self.advertise_auth = advertise_auth

        self.messages = []        # accepted: {"mail_from", "rcpt_to", "raw"}
        self.data_attempts = 0    # DATA commands seen, accepted or not
        self.logins = []          # (username, password) the client offered
        self.commands = []

        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                outer._serve(self)

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.host, self.port = self._server.server_address
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # lifecycle
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    # protocol
    @staticmethod
    def _send(handler, line):
        handler.wfile.write(line + b"\r\n")
        handler.wfile.flush()

    def _serve(self, h):
        mail_from, rcpts = None, []
        self._send(h, b"220 fake.local ESMTP ready")

        while True:
            raw = h.rfile.readline()

            if not raw:
                return

            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            self.commands.append(verb)

            if verb in ("EHLO", "HELO"):
                lines = [b"250-fake.local", b"250-8BITMIME"]

                if self.advertise_auth:
                    lines.append(b"250-AUTH PLAIN LOGIN")

                lines.append(b"250 SIZE 10485760")
                for entry in lines:
                    self._send(h, entry)

            elif verb == "AUTH":
                self._auth(h, line)

            elif verb == "MAIL":
                mail_from, rcpts = line.split(":", 1)[1].strip().strip("<>").split()[0].strip("<>"), []
                self._send(h, b"250 2.1.0 OK")

            elif verb == "RCPT":
                rcpt = line.split(":", 1)[1].strip().strip("<>").split()[0].strip("<>")

                if rcpt.lower() in self.reject_rcpt:
                    self._send(h, b"550 5.1.1 mailbox unavailable")
                else:
                    rcpts.append(rcpt)
                    self._send(h, b"250 2.1.5 OK")

            elif verb == "DATA":
                self._send(h, b"354 end data with <CRLF>.<CRLF>")
                self.data_attempts += 1
                body = []

                while True:
                    chunk = h.rfile.readline()

                    if not chunk or chunk in (b".\r\n", b".\n"):
                        break

                    body.append(chunk[1:] if chunk.startswith(b"..") else chunk)

                if self.drop_after_data:
                    # The message arrived in full, but the client never hears
                    # the verdict: the classic "was it delivered?" case.
                    h.connection.shutdown(socket.SHUT_RDWR)
                    return

                if self.data_reply.startswith(b"250"):
                    self.messages.append(
                        {"mail_from": mail_from, "rcpt_to": list(rcpts), "raw": b"".join(body)}
                    )

                self._send(h, self.data_reply)

            elif verb in ("RSET", "NOOP"):
                self._send(h, b"250 OK")

            elif verb == "QUIT":
                self._send(h, b"221 bye")
                return

            else:
                self._send(h, b"502 5.5.2 not implemented")

    def _auth(self, h, line):
        parts = line.split(" ")
        mechanism = parts[1].upper() if len(parts) > 1 else ""

        if mechanism == "PLAIN":
            blob = parts[2] if len(parts) > 2 else None

            if blob is None:
                self._send(h, b"334 ")
                blob = h.rfile.readline().decode().strip()

            _, user, password = base64.b64decode(blob).decode().split("\0")

        elif mechanism == "LOGIN":
            self._send(h, b"334 " + base64.b64encode(b"Username:"))
            user = base64.b64decode(h.rfile.readline().strip()).decode()
            self._send(h, b"334 " + base64.b64encode(b"Password:"))
            password = base64.b64decode(h.rfile.readline().strip()).decode()

        else:
            self._send(h, b"504 5.5.4 unrecognized mechanism")
            return

        self.logins.append((user, password))

        if self.auth_ok:
            self._send(h, b"235 2.7.0 Accepted")
        else:
            self._send(h, b"535 5.7.8 Username and Password not accepted")

    # helpers for assertions
    def parsed(self, index=0):
        return message_from_bytes(self.messages[index]["raw"], policy=policy.default)


def closed_port() -> int:
    """A local port with nothing listening on it."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
