import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from tripweave.identity import WORKSPACE_ID
from tripweave.launcher import probe_port


def test_launcher_identifies_same_workspace_and_leaves_other_services_alone():
    class Handler(BaseHTTPRequestHandler):
        workspace = WORKSPACE_ID

        def do_GET(self):
            body = json.dumps({"application": "tripweave", "workspace_id": self.workspace}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert probe_port(port) == "same"
        Handler.workspace = "another-workspace"
        assert probe_port(port) == "occupied"
        assert thread.is_alive()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert probe_port(port) == "free"
