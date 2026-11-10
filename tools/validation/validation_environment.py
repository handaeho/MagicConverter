"""Verify owned child-process localhost access before expensive validation."""
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def check_environment():
    nonce = secrets.token_hex(16)
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/' + nonce:
                self.send_error(404)
                return
            requests.append(self.path)
            data = nonce.encode('ascii')
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_):
            pass
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        try:
            command = [sys.executable, '-X', 'utf8', '-B', '-c',
                       'import sys,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); '
                       'sys.exit(o.open(sys.argv[1],timeout=5).read()!=sys.argv[2].encode("ascii"))',
                       f'http://127.0.0.1:{server.server_port}/{nonce}', nonce]
            child = subprocess.run(command, capture_output=True, timeout=10,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if child.returncode or len(requests) != 1:
                detail = child.stderr.decode('utf-8', errors='replace')[-1500:]
                raise RuntimeError('Validation child cannot reach its owned localhost HTTP server. '
                                   'Run validation in an environment permitting local HTTP; no test is skipped. ' + detail)
            return {'status': 'passed', 'child_exit': child.returncode, 'owned_local_requests': len(requests),
                    'scope': 'Random-token HTTP response from an owned loopback server to a child process; no external network'}
        finally:
            server.shutdown()
            thread.join(timeout=2)
