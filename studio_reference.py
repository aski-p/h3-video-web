"""Private, signed Storage-to-NAS transfer for the Aski studio's large videos."""
import hashlib
import json
import re
import urllib.parse
import urllib.request

STORAGE_ORIGIN = "hyovtguangyykehxwnvp.supabase.co"
STORAGE_PREFIX = "/storage/v1/object/sign/aski-cast-transfer/"
MAX_VIDEO = 100 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect rejected")


def validate_request(body):
    if len(body) > 10000:
        raise ValueError("invalid transfer request")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("invalid transfer object")
    parts = data.get("parts") or [{"url":data.get("url", ""), "size":data.get("size")}]
    if not isinstance(parts, list) or not 1 <= len(parts) <= 3:
        raise ValueError("invalid transfer parts")
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("invalid part object")
        url = urllib.parse.urlsplit(part.get("url", ""))
        if (url.scheme != "https" or url.hostname != STORAGE_ORIGIN or url.port not in (None, 443)
                or url.username or url.password or url.fragment
                or not url.path.startswith(STORAGE_PREFIX)
                or not re.fullmatch(r"[a-f0-9-]{36}\.(?:[0-2]\.part|mp4|mov|webm)", url.path[len(STORAGE_PREFIX):])
                or not urllib.parse.parse_qs(url.query).get("token")
                or not isinstance(part.get("size"), int) or not 1 <= part["size"] <= MAX_VIDEO):
            raise ValueError("invalid signed storage URL or part size")
    if sum(p["size"] for p in parts) != data.get("size"):
        raise ValueError("part size mismatch")
    data["parts"] = parts
    digest = data.get("sha256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid file digest")
    if not isinstance(data.get("size"), int) or not 100 <= data["size"] <= MAX_VIDEO:
        raise ValueError("invalid file size")
    if not re.fullmatch(r"aski-" + digest + r"\.(mp4|mov|webm)", data.get("name", "")):
        raise ValueError("invalid reference filename")
    return data


def transfer(body, backend, origin_header, origin_secret, client_header, client_key, opener=None):
    data = validate_request(body)
    opener = opener or urllib.request.build_opener(NoRedirect())
    chunks = []
    for part in data["parts"]:
        with opener.open(part["url"], timeout=60) as source:
            declared = source.headers.get("Content-Length")
            if declared and int(declared) != part["size"]:
                raise ValueError("file size mismatch")
            chunk = source.read(part["size"] + 1)
            if len(chunk) != part["size"]:
                raise ValueError("incomplete transfer part")
            chunks.append(chunk)
    content = b"".join(chunks)
    if len(content) != data["size"] or hashlib.sha256(content).hexdigest() != data["sha256"]:
        raise ValueError("file integrity mismatch")
    # The upstream API performs ffprobe, writes to NAS and verifies SHA-256.
    req = urllib.request.Request(backend + "/api/refv/set", data=content, method="POST", headers={
        "Content-Type": "application/octet-stream",
        "Content-Disposition": 'attachment; filename="' + data["name"] + '"',
        origin_header: origin_secret, client_header: client_key,
    })
    with opener.open(req, timeout=150) as upstream:
        result = json.loads(upstream.read(100000))
    meta = result.get("refv") or {}
    if not result.get("ok") or meta.get("size") != len(content) or meta.get("sha256") != data["sha256"]:
        raise ValueError("NAS registration verification failed")
    return {"ok": True, "refv": meta, "sha256": data["sha256"]}


def handle(body, start_response, **kwargs):
    try:
        result = transfer(body, **kwargs)
        code = "200 OK"
    except (ValueError, TypeError, KeyError):
        result, code = {"ok": False, "error": "reference transfer validation failed"}, "400 Bad Request"
    except Exception:
        # Do not echo signed URLs, file data or origin credentials.
        result, code = {"ok": False, "error": "reference transfer unavailable"}, "502 Bad Gateway"
    start_response(code, [("Content-Type", "application/json"), ("Cache-Control", "private, no-store")])
    return [json.dumps(result).encode()]
