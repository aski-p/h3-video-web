"""Vercel 백엔드 프록시 — API 요청을 실제 서버로 전달."""
import json
import os
import urllib.request
import urllib.error

# 실제 백엔드 URL (네트워크 접근이 필요한 경우 환경변수로 지정)
# Tailscale MagicDNS: PGX 머신의 Tailscale IP (영구적, 터널 불필요)
BACKEND = os.environ.get("H3_BACKEND", "https://thinkstationpgx-11d3.tailccac79.ts.net:8300")

def _proxy_response(r, start_response, is_download=False):
    """HTTP response를 stream으로 반환 (다운로드 시 헤더 보존, 메모리 절감)."""
    headers = [("Content-Type", r.headers.get("Content-Type", "application/json")),
               ("Access-Control-Allow-Origin", "*"),
               ("Cache-Control", "no-store, no-cache, must-revalidate")]
    if is_download:
        cd = r.headers.get("Content-Disposition")
        if cd:
            headers.append(("Content-Disposition", cd))
        cl = r.headers.get("Content-Length")
        if cl:
            headers.append(("Content-Length", cl))
    start_response(f"{r.status}", headers)
    chunks = []
    while True:
        chunk = r.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return chunks


def proxy(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    body = b""
    if environ.get("CONTENT_LENGTH"):
        n = int(environ["CONTENT_LENGTH"])
        body = environ.get("wsgi.input", b"").read(n)

    # download 경로인지 확인 (대용량 파일 스트리밍 + 헤더 보존)
    is_download = path.startswith("/api/download/")

    url = BACKEND + path
    req = urllib.request.Request(url, data=body if method == "POST" else None,
                                  method=method,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return _proxy_response(r, start_response, is_download)
    except urllib.error.HTTPError as e:
        data = e.read()
        headers = [("Content-Type", "application/json"),
                   ("Access-Control-Allow-Origin", "*")]
        if is_download:
            cd = e.headers.get("Content-Disposition")
            if cd:
                headers.append(("Content-Disposition", cd))
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
