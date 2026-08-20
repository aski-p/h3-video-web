"""Vercel 백엔드 프록시 — API 요청을 실제 서버로 전달."""
import json
import urllib.request
import urllib.error

BACKEND = "http://192.168.50.213:8300"

def proxy(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    body = b""
    if environ.get("CONTENT_LENGTH"):
        n = int(environ["CONTENT_LENGTH"])
        body = environ.get("wsgi.input", b"").read(n)

    url = BACKEND + path
    req = urllib.request.Request(url, data=body if method == "POST" else None,
                                  method=method,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
            status = r.status
            headers = {"Content-Type": r.headers.get("Content-Type", "application/json"),
                        "Access-Control-Allow-Origin": "*"}
            start_response(f"{status}", list(headers.items()))
            return [data]
    except urllib.error.HTTPError as e:
        data = e.read()
        start_response(f"{e.code}", {"Content-Type": "application/json",
                                      "Access-Control-Allow-Origin": "*"})
        return [data]
    except Exception as e:
        err = json.dumps({"ok": False, "error": str(e)}).encode()
        start_response("502", {"Content-Type": "application/json",
                                "Access-Control-Allow-Origin": "*"})
        return [err]

def handler(environ, start_response):
    return proxy(environ, start_response)
