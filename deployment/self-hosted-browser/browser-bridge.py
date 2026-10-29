"""Private Unix POST /browser-step: one bounded native CLI decision, no tools."""
import http.server
import json
import os
from pathlib import Path
import socketserver
import stat
import threading

from codex_browser import browser_step, validate_request

BUSY = threading.BoundedSemaphore(1)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log tasks, browser output, generated code or credentials.

    def reply(self, status, value):
        payload = json.dumps(value).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != '/browser-step':
            return self.reply(404, {'error': 'not_found'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.reply(400, {'error': 'invalid_length'})
        if not 0 < size <= 128000:
            return self.reply(413, {'error': 'input_limit'})
        if not BUSY.acquire(blocking=False):
            return self.reply(429, {'error': 'browser_cli_busy'})
        try:
            self.connection.settimeout(5)
            raw = self.rfile.read(size)
            if len(raw) != size:
                return self.reply(400, {'error': 'invalid_input'})
            try:
                value = validate_request(json.loads(raw))
            except (ValueError, UnicodeError):
                return self.reply(400, {'error': 'invalid_input'})
            self.reply(200, browser_step(value))
        except TimeoutError:
            self.reply(504, {'error': 'local_browser_cli_timeout'})
        except Exception:
            self.reply(502, {'error': 'local_browser_cli_failed'})
        finally:
            BUSY.release()


if hasattr(socketserver, 'UnixStreamServer'):
    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True


if __name__ == '__main__':
    path = Path(os.environ['SELF_HOSTED_BROWSER_LLM_SOCKET'])
    if not path.is_absolute():
        raise ValueError('absolute_private_socket_required')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise ValueError('refusing_to_unlink_non_socket')
        path.unlink()  # Single-instance service owns this private socket.
    with Server(str(path), Handler) as server:
        os.chmod(path, 0o600)
        server.serve_forever()
