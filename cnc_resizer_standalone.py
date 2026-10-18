#!/usr/bin/env python3
"""Run the tool WITHOUT Django (handy for a quick local test):
        python standalone_server.py          ->  http://127.0.0.1:8000/
It serves the very same page and uses the very same cnc_resizer.core.handle_request()
that the Django view uses."""
import os
import sys
from wsgiref.simple_server import make_server

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cnc_resizer import core  # noqa: E402

TEMPLATE = os.path.join(HERE, "cnc_resizer", "templates", "cnc_resizer", "index.html")


def page():
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    return html.replace("{% csrf_token %}", "").replace("{{ api_url }}", "/api/process/").encode("utf-8")


def app(environ, start_response):
    path, method = environ["PATH_INFO"], environ["REQUEST_METHOD"]
    if path == "/" and method == "GET":
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [page()]
    if path == "/api/process/" and method == "POST":
        n = int(environ.get("CONTENT_LENGTH") or 0)
        if n > 3 * 1024 * 1024:
            status, data = 413, dict(ok=False, error="فایل بیش از حد بزرگ است")
        else:
            status, data = core.handle_request(environ["wsgi.input"].read(n))
        import json
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        start_response(f"{status} X", [("Content-Type", "application/json; charset=utf-8")])
        return [body]
    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"http://127.0.0.1:{port}/")
    make_server("127.0.0.1", port, app).serve_forever()
