"""Short-lived, origin-authenticated links for completed H3 media."""
import hashlib
import hmac
import re
import time
from urllib.parse import parse_qs, quote, urlencode

MAX_LIFETIME_SECONDS = 3600
MEDIA_PATH = re.compile(r"/api/(view|download)/([A-Za-z0-9_-]{1,64})\Z")


def signature(secret, kind, job_id, expires):
    message = f"h3-media-v1:{kind}:{job_id}:{expires}".encode("ascii")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def issue(origin, secret, kind, job_id, now=None):
    if not secret or not MEDIA_PATH.fullmatch(f"/api/{kind}/{job_id}"):
        raise ValueError("invalid media ticket")
    expires = int(time.time() if now is None else now) + MAX_LIFETIME_SECONDS
    query = urlencode({"expires": expires, "signature": signature(secret, kind, job_id, expires)})
    return f"{origin}/api/{kind}/{quote(job_id)}?{query}"


def verify(secret, path, query, now=None):
    match = MEDIA_PATH.fullmatch(path)
    if not secret or not match:
        return False
    params = parse_qs(query, keep_blank_values=True)
    if set(params) != {"expires", "signature"} or any(len(values) != 1 for values in params.values()):
        return False
    raw_expires, supplied = params["expires"][0], params["signature"][0]
    if not re.fullmatch(r"[0-9]{1,12}", raw_expires) or not re.fullmatch(r"[a-f0-9]{64}", supplied):
        return False
    expires = int(raw_expires)
    current = int(time.time() if now is None else now)
    if not current <= expires <= current + MAX_LIFETIME_SECONDS:
        return False
    return hmac.compare_digest(supplied, signature(secret, match[1], match[2], expires))
