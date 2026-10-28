"""Studio-backed Google SSO and per-request ownership for the public H3 proxy.

No Google credential or PGX service key enters H3. Only Studio consumes OAuth;
H3 uses a one-time PKCE code, its own HttpOnly session cookie, and live approval
checks. The GPU server is deliberately left running during rollout.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie

H3 = "https://h3-video-web.vercel.app"
STUDIO = "https://reelradar-5.vercel.app"
COOKIE = "__Host-h3_session"
FLOW = "__Host-h3_login"

class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def result(start, status, data, headers=()):
    start(f"{status}", [("Content-Type", "application/json; charset=utf-8"),
                       ("Cache-Control", "private, no-store"), *headers])
    return [json.dumps(data, ensure_ascii=False).encode()]


def cookie(environ, name):
    try:
        c = SimpleCookie(environ.get("HTTP_COOKIE", ""))
        return c[name].value if name in c else ""
    except Exception:
        return ""


def set_cookie(name, value, age):
    return ("Set-Cookie", f"{name}={value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={age}")


def call(action, **body):
    token = os.environ.get("ORIGINAL_VIDEO_TOKEN", "")
    if not token:
        raise AuthError("Studio 로그인 연결을 사용할 수 없습니다.", 503)
    req = urllib.request.Request(STUDIO + "/api/h3-auth", method="POST",
        headers={"Content-Type": "application/json", "X-Aski-Original-Token": token},
        data=json.dumps({"action": action, **body}).encode())
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        raise AuthError("비밀번호가 올바르지 않거나 잠시 잠겼습니다." if exc.code in (401, 429) else "Aski Studio 계정을 확인할 수 없습니다.", exc.code if exc.code in (400, 401, 403, 409, 429) else 503) from None
    except (OSError, ValueError):
        raise AuthError("Studio 계정을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.", 503) from None


def seal(payload):
    key = os.environ.get("H3_ORIGIN_SECRET", "")
    if not key:
        raise AuthError("로그인 연결을 사용할 수 없습니다.", 503)
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return data + "." + hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def unseal(raw):
    try:
        data, sig = raw.split(".")
        expected = hmac.new(os.environ["H3_ORIGIN_SECRET"].encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError()
        p = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        if not 0 <= time.time() - p["at"] <= 600:
            raise ValueError()
        return p
    except (ValueError, KeyError, TypeError):
        raise AuthError("로그인 요청이 만료되었습니다. 다시 로그인해 주세요.") from None


def same_origin(environ):
    if environ.get("HTTP_ORIGIN") != H3:
        raise AuthError("요청 출처를 확인해 주세요.", 403)


def auth_route(environ, start):
    path, method = environ.get("PATH_INFO"), environ.get("REQUEST_METHOD", "GET")
    if path == "/api/auth/password" and method == "POST":
        same_origin(environ)
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if length < 1 or length > 1024:
                raise ValueError()
            body = json.loads(environ["wsgi.input"].read(length))
            key = body.get("key")
            if not isinstance(key, str) or not 4 <= len(key) <= 64:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise AuthError("비밀번호를 확인해 주세요.", 400) from None
        response = call("password", key=key)
        token = response.get("token", "")
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise AuthError("로그인을 완료할 수 없습니다.", 503)
        return result(start, 200, {"ok": True, "user": response.get("user")}, [set_cookie(COOKIE, token, 30*86400)])
    if path == "/api/auth/login" and method == "GET":
        p = {"state": secrets.token_hex(32), "verifier": secrets.token_urlsafe(32), "at": time.time()}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(p["verifier"].encode()).digest()).decode().rstrip("=")
        start("302 Found", [("Location", STUDIO + "/api/h3-auth?" + urllib.parse.urlencode({"state": p["state"], "challenge": challenge})),
                            ("Cache-Control", "no-store"), set_cookie(FLOW, seal(p), 600)])
        return [b""]
    if path == "/api/auth/callback" and method == "GET":
        q = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        p = unseal(cookie(environ, FLOW))
        if not hmac.compare_digest(q.get("state", [""])[0], p["state"]):
            raise AuthError("로그인 상태가 일치하지 않습니다.")
        response = call("exchange", code=q.get("code", [""])[0], verifier=p["verifier"])
        token = response.get("token", "")
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise AuthError("로그인을 완료할 수 없습니다.", 503)
        start("302 Found", [("Location", "/"), ("Cache-Control", "no-store"),
                            set_cookie(FLOW, "", 0), set_cookie(COOKIE, token, 28800)])
        return [b""]
    if path == "/api/auth/me" and method == "GET":
        return result(start, 200, {"ok": True, **call("introspect", session=cookie(environ, COOKIE))})
    if path == "/api/auth/logout" and method == "POST":
        same_origin(environ)
        try:
            call("logout", session=cookie(environ, COOKIE))
        except AuthError as exc:
            if exc.status not in (401, 403):
                raise
        return result(start, 200, {"ok": True}, [set_cookie(COOKIE, "", 0)])
    return result(start, 405, {"ok": False, "error": "method_not_allowed"})


def actor(environ):
    supplied = environ.get("HTTP_X_ASKI_ORIGINAL_TOKEN", "")
    secret = os.environ.get("ORIGINAL_VIDEO_TOKEN", "")
    if supplied:
        if secret and hmac.compare_digest(supplied, secret):
            return {"service": True}
        raise AuthError("서버 인증을 확인해 주세요.")
    token = cookie(environ, COOKIE)
    user = call("introspect", session=token).get("user")
    if not user or not user.get("id"):
        raise AuthError("Google 로그인이 필요합니다.")
    if environ.get("REQUEST_METHOD", "GET") not in ("GET", "HEAD"):
        same_origin(environ)
    return {"user": user, "session": token, "service": False}


def lookup(ids):
    records = []
    for offset in range(0, len(ids), 500):
        records.extend(call("lookup", ids=ids[offset:offset + 500]).get("jobs", []))
    return {r["job_id"]: r for r in records}


def owns(context, job_id):
    if context.get("service") or context["user"].get("admin"):
        return
    record = lookup([job_id]).get(job_id)
    if not record or record["owner_id"] != context["user"]["id"]:
        raise AuthError("내 영상만 변경하거나 열 수 있습니다.", 403)


def restrict(environ, context):
    if context.get("service"):
        return
    path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "GET")
    m = re.fullmatch(r"/api/(?:job|cancel|delete|delete-error)/([a-f0-9]{8})", path)
    if m:
        owns(context, m[1])
    if method not in ("GET", "HEAD") and not (m or path in ("/api/generate", "/api/upload")):
        if not context["user"].get("admin"):
            raise AuthError("공용 장비·고정 참조 변경은 관리자만 가능합니다.", 403)


def prepare(body, context):
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        raise AuthError("생성 요청을 확인해 주세요.", 400)
    if not isinstance(data, dict):
        raise AuthError("생성 요청을 확인해 주세요.", 400)
    response = call("prepare", session=context.get("session"),
        requestKey=data.get("client_request_id") or secrets.token_hex(16),
        title=data.get("title") or data.get("filename") or "H3 직접 생성")
    data["client_request_id"] = response["requestKey"]
    # Never trust caller-provided owner metadata.
    for name in ("owner", "owner_id", "studio_owner_id", "owner_email"):
        data.pop(name, None)
    return json.dumps(data).encode(), response


def bind(response, prepared):
    job = response.get("job_id") or response.get("job")
    job_id = job.get("id") if isinstance(job, dict) else job
    if not re.fullmatch(r"[a-f0-9]{8}", str(job_id or "")):
        raise AuthError("접수된 영상 번호를 확인할 수 없습니다.", 502)
    call("bind", requestKey=prepared["requestKey"], jobId=job_id)
    response["owner"] = prepared["user"]
    response["title"] = prepared["title"]
    return response


def jobs(response, context):
    source = response.get("jobs", [])
    ids = [j["id"] for j in source if re.fullmatch(r"[a-f0-9]{8}", str(j.get("id", "")))]
    records = lookup(ids)
    allowed = []
    for job in source:
        row = records.get(job.get("id"))
        if not context["user"].get("admin") and (not row or row["owner_id"] != context["user"]["id"]):
            continue
        if row:
            u = row["studio_users"]
            job["owner"] = {"id": u["id"], "name": u.get("display_name") or u["email"], "email": u["email"]}
            job["title"] = row["title"]
        allowed.append(job)
    response["jobs"] = allowed
    # Counts must reflect the visible job list, not other accounts' history.
    response["today_completed_count"] = sum(j.get("status") == "done" for j in allowed)
    return response


def detail(response, context):
    job = response.get("job")
    if not isinstance(job, dict) or not re.fullmatch(r"[a-f0-9]{8}", str(job.get("id", ""))):
        return response
    row = lookup([job["id"]]).get(job["id"])
    if row:
        u = row["studio_users"]
        job["owner"] = {"id": u["id"], "name": u.get("display_name") or u["email"], "email": u["email"]}
        job["title"] = row["title"]
    return response
