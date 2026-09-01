"""Vercel 백엔드 프록시 — API 요청을 실제 서버로 전달."""
import json
import os
import urllib.request
import urllib.error

# 실제 백엔드 URL (네트워크 접근이 필요한 경우 환경변수로 지정)
# Tailscale MagicDNS: PGX 머신의 Tailscale IP (영구적, 터널 불필요)
BACKEND = os.environ.get("H3_BACKEND", "https://thinkstationpgx-11d3.tailccac79.ts.net:8300")

def _proxy_response(r, start_response, is_video=False):
    """HTTP response를 stream으로 반환 (다운로드 시 헤더 보존, 메모리 절감)."""
    headers = [("Content-Type", r.headers.get("Content-Type", "application/json")),
               ("Access-Control-Allow-Origin", "*"),
               ("Cache-Control", "no-store, no-cache, must-revalidate")]
    if is_video:
        for name in ("Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges"):
            value = r.headers.get(name)
            if value:
                headers.append((name, value))
    start_response(f"{r.status}", headers)

    # WSGI consumes an iterable lazily.  Returning a list here buffered the
    # entire MP4 before sending its first byte, which can exhaust a serverless
    # proxy or turn a healthy upstream connection into a 520/timeout.
    def stream():
        try:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            r.close()
    return stream()


def proxy(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    body = b""
    if environ.get("CONTENT_LENGTH"):
        n = int(environ["CONTENT_LENGTH"])
        body = environ.get("wsgi.input", b"").read(n)

    # Playback and download must retain range semantics through Vercel.
    is_video = path.startswith("/api/download/") or path.startswith("/api/view/")

    url = BACKEND + path
    headers = {"Content-Type": environ.get("CONTENT_TYPE", "application/json")}
    if environ.get("HTTP_RANGE"):
        headers["Range"] = environ["HTTP_RANGE"]
    req = urllib.request.Request(url, data=body if method == "POST" else None,
                                  method=method, headers=headers)
    try:
        # Do not use a context manager here: _proxy_response returns a lazy
        # WSGI iterator that must keep the upstream socket open while bytes
        # are sent to the browser.
        r = urllib.request.urlopen(req, timeout=300)
        return _proxy_response(r, start_response, is_video)
    except urllib.error.HTTPError as e:
        data = e.read()
        headers = [("Content-Type", "application/json"),
                   ("Access-Control-Allow-Origin", "*")]
        if is_video:
            for name in ("Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges"):
                value = e.headers.get(name)
                if value:
                    headers.append((name, value))
        start_response(f"{e.code}", headers)
        return [data]
    except Exception as e:
        err = json.dumps({"ok": False, "error": str(e)}).encode()
        start_response("502", [("Content-Type", "application/json"),
                               ("Access-Control-Allow-Origin", "*")])
        return [err]

def handler(environ, start_response):
    return proxy(environ, start_response)

# Vercel Python 빌더는 모듈 최상위 app/application/handler 변수를 요구함
app = proxy
