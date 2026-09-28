"""A stand-in for `claude auth login` on the wire, run as a subprocess by `test_relayed_sign_in.py`.

It listens on a loopback port, runs `$BROWSER` with an authorize URL whose `redirect_uri` is that
port, and prints "Login successful" once the callback carrying its `state` arrives. It talks to
the flow the way the real CLI does, through its terminal output, hence the prints.
"""

import http.server
import os
import subprocess
import sys
import urllib.parse

server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
port = server.server_address[1]
state = "fake-state"
redirect = urllib.parse.quote(f"http://localhost:{port}/callback", safe="")
url = f"https://claude.ai/oauth/authorize?code=true&client_id=c&redirect_uri={redirect}&state={state}"


class Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        ok = query.get("state") == [state] and query.get("code") == ["the-code"]
        self.send_response(302 if ok else 400)
        self.send_header("Location", "https://platform.claude.com/oauth/code/success")
        self.send_header("Content-Length", "0")
        self.end_headers()
        server.succeeded = ok

    def log_message(self, *args):
        pass


server.RequestHandlerClass = Callback
server.succeeded = False
subprocess.run([os.environ["BROWSER"], url], check=True)
print("Browser didn't open? Use the url below to sign in:", flush=True)
print(url.replace("http%3A%2F%2Flocalhost", "https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode"), flush=True)
server.handle_request()
if server.succeeded:
    print("Login successful.", flush=True)
    sys.exit(0)
print("Login failed: the callback did not match", flush=True)
sys.exit(1)
