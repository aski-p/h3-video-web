#!/usr/bin/env python3
"""MiniMax H3 영상 생성/다운로드 페이지 서버.

친구 페이지 스타일의 웹 UI + PGX ComfyUI(:8188) 미니맥스 H3 text-to-video+audio 연동.
- ComfyUI 없으면 systemd user 서비스(minimax-h3-comfyui) 자동 기동
- /api/generate      → 비동기 영상 생성 시작 (job)
- /api/job/{id}      → 진행 상태 폴링
- /api/jobs          → 전체 job 리스트
- /api/download/{id} → 완료 영상 mp4 서빙 (다운로드)
- /api/cancel/{id}   → 큐 대기 job 취소
- /                  → index.html 정적 서빙
"""
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = os.environ.get("H3_HOST", "0.0.0.0")
PORT = int(os.environ.get("H3_PORT", "8300"))
COMFY = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188")
ASUI = os.environ.get("ASUI", "aski")
OUT_DIR = os.environ.get("H3_OUT_DIR", os.path.expanduser("~/h3-web/output"))
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
COMFY_OUT = "/home/aski/minimax-h3/output"

JOBS = {}
LOCK = threading.Lock()


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def comfy_get(path, timeout=15):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return json.load(r)


def comfy_post(path, payload, timeout=60):
    req = urllib.request.Request(COMFY + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def comfy_up():
    try:
        comfy_get("/system_stats", timeout=5)
        return True
    except Exception:
        return False


def run_asu(cmd, timeout=300, check=True):
    full = ["sudo", "-n", "-u", ASUI, "bash", "-c", cmd]
    env = dict(os.environ)
    env.update({"XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"})
    p = subprocess.run(full, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"asu cmd failed: {cmd}\n{p.stderr.strip()[:400]}")
    return p


def ensure_comfyui():
    if comfy_up():
        return True
    log("ComfyUI down -> starting minimax-h3-comfyui.service")
    try:
        run_asu("systemctl --user start minimax-h3-comfyui.service", timeout=60, check=True)
    except Exception as e:
        log(f"systemctl start failed (maybe already starting): {e}")
    for _ in range(300):
        if comfy_up():
            log("ComfyUI ready")
            return True
        time.sleep(2)
    raise RuntimeError("ComfyUI 기동 실패 (300초 대기 초과)")


def snap_len(seconds):
    raw = max(5, round(seconds * 24))
    return raw + (5 - (raw % 17)) % 17


def make_prompt(text, width, height, length, steps, seed, prefix):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": text, "width": width, "height": height, "length": length}},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "7": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "8": {"class_type": "BasicScheduler", "inputs": {"model": ["1", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}},
        "9": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["5", 0]}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["6", 0], "guider": ["9", 0], "sampler": ["7", 0], "sigmas": ["8", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "audio": ["12", 0], "fps": 24.0, "bit_depth": 8}},
        "14": {"class_type": "SaveVideo", "inputs": {"video": ["13", 0], "filename_prefix": prefix, "format": "auto", "codec": "auto"}},
    }


def _resolve_output(fname):
    fname = fname.replace("\\", "/")
    parts = fname.split("/")
    cand = os.path.join(COMFY_OUT, *parts)
    if os.path.exists(cand):
        return cand
    import glob
    hits = glob.glob(os.path.join(COMFY_OUT, "**", parts[-1]), recursive=True)
    if hits:
        p = sorted(hits)[-1]
        if os.access(p, os.R_OK):
            return p
    raise RuntimeError(f"출력 파일 미발견 또는 읽기 권한 없음: {fname}")


def run_job(job_id, cfg):
    with LOCK:
        JOBS[job_id]["status"] = "starting"
    try:
        ensure_comfyui()
        seed = cfg["seed"] if cfg["seed"] >= 0 else int.from_bytes(os.urandom(6), "big")
        prefix = cfg["filename"] or "h3web/video"
        workflow = make_prompt(cfg["prompt"], cfg["width"], cfg["height"], cfg["length"], cfg["steps"], seed, prefix)
        cid = str(uuid.uuid4())
        queued = comfy_post("/prompt", {"prompt": workflow, "client_id": cid})
        if "error" in queued:
            raise RuntimeError(json.dumps(queued, ensure_ascii=False)[:600])
        pid = queued["prompt_id"]
        with LOCK:
            JOBS[job_id].update(status="running", seed=seed, prompt_id=pid, started=time.time())
        log(f"job {job_id} queued pid={pid} seed={seed}")
        while True:
            h = comfy_get(f"/history/{pid}", timeout=30)
            if pid in h:
                result = h[pid]
                status = result.get("status", {})
                if status.get("status_str") == "error" or not status.get("completed", False):
                    raise RuntimeError(json.dumps(result, ensure_ascii=False)[:800])
                files = []
                for out in result.get("outputs", {}).values():
                    for key in ("videos", "gifs", "images"):
                        for f in out.get(key, []):
                            files.append(f)
                mp4 = [f for f in files if str(f).lower().endswith(".mp4")] or files
                if not mp4:
                    raise RuntimeError("완료되었으나 mp4 파일 없음: " + str(files)[:300])
                fname = mp4[0]
                src = _resolve_output(fname)
                dst_dir = os.path.join(OUT_DIR, job_id)
                os.makedirs(dst_dir, exist_ok=True)
                dst = os.path.join(dst_dir, fname.split("/")[-1])
                if os.access(src, os.R_OK):
                    shutil.copy2(src, dst)
                else:
                    run_asu(f"cp '{src}' '{dst}' && chmod 644 '{dst}'", timeout=60)
                try:
                    run_asu(f"chmod 644 '{dst}'", timeout=15, check=False)
                except Exception:
                    pass
                with LOCK:
                    JOBS[job_id].update(status="done", file=fname, src=dst,
                                        elapsed=round(time.time() - JOBS[job_id].get("started", time.time()), 1))
                log(f"job {job_id} done -> {dst}")
                return
            q = comfy_get("/queue", timeout=30)
            with LOCK:
                JOBS[job_id]["progress"] = {
                    "elapsed": round(time.time() - JOBS[job_id].get("started", time.time()), 1),
                    "queue_running": len(q.get("queue_running", [])),
                    "queue_pending": len(q.get("queue_pending", [])),
                }
            time.sleep(8)
    except Exception as e:
        with LOCK:
            JOBS[job_id].update(status="error", error=str(e)[:800])
        log(f"job {job_id} ERROR: {str(e)[:200]}")


# ---------- HTTP ----------
def send_json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(fmt % args)

    def _cors(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        self._cors()

    def _static(self, path):
        if path in ("/", "/index.html"):
            fname, ctype = "index.html", "text/html; charset=utf-8"
        else:
            fname = path.lstrip("/")
            ctype = "application/octet-stream"
            if fname.endswith(".html"): ctype = "text/html; charset=utf-8"
            elif fname.endswith(".css"): ctype = "text/css"
            elif fname.endswith(".js"): ctype = "application/javascript"
        fpath = os.path.join(WEB_DIR, fname)
        if not os.path.exists(fpath):
            send_json(self, {"ok": False, "error": "not found"}, 404)
            return
        with open(fpath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/jobs":
            with LOCK:
                items = [dict(j, prompt=j.get("prompt", "")) for j in JOBS.values()]
            items.sort(key=lambda x: x.get("created", 0), reverse=True)
            send_json(self, {"ok": True, "jobs": items, "comfy_up": comfy_up()})
        elif p.startswith("/api/job/"):
            jid = p.split("/")[2]
            with LOCK:
                j = dict(JOBS.get(jid)) if JOBS.get(jid) else None
            send_json(self, {"ok": True, "job": j}, code=200 if j else 404)
        elif p.startswith("/api/download/"):
            jid = p.split("/")[2]
            with LOCK:
                j = JOBS.get(jid)
            if not j or j.get("status") != "done" or not j.get("src") or not os.path.exists(j["src"]):
                send_json(self, {"ok": False, "error": "다운로드 가능 영상 없음 (job 미완료)"}, 404)
                return
            fsize = os.path.getsize(j["src"])
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(fsize))
            self.send_header("Content-Disposition", f'attachment; filename="{jid}.mp4"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(j["src"], "rb") as f:
                shutil.copyfileobj(f, self.wfile, length=1024 * 256)
        else:
            self._static(p)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            send_json(self, {"ok": False, "error": f"bad request: {e}"}, 400)
            return
        if p == "/api/generate":
            prompt = (data.get("prompt") or "").strip()
            if len(prompt) < 3:
                send_json(self, {"ok": False, "error": "프롬프트가 너무 짧습니다"}, 400)
                return
            cfg = {
                "prompt": prompt,
                "width": int(data.get("width", 608)),
                "height": int(data.get("height", 352)),
                "length": snap_len(float(data.get("seconds", 5))),
                "steps": int(data.get("steps", 6)),
                "seed": int(data.get("seed", -1)),
                "filename": (data.get("filename") or "").strip() or "h3web/video",
            }
            jid = str(uuid.uuid4())[:8]
            with LOCK:
                JOBS[jid] = {"id": jid, "status": "queued", "created": time.time(),
                             "cfg": cfg, "prompt": prompt}
            threading.Thread(target=run_job, args=(jid, cfg), daemon=True).start()
            log(f"new job {jid}: {prompt[:50]}... {cfg['width']}x{cfg['height']} len={cfg['length']} steps={cfg['steps']}")
            send_json(self, {"ok": True, "job": jid})
        elif p.startswith("/api/cancel/"):
            jid = p.split("/")[2]
            with LOCK:
                j = JOBS.get(jid)
                if j and j["status"] in ("queued", "starting"):
                    j["status"] = "cancelled"
                    send_json(self, {"ok": True})
                else:
                    send_json(self, {"ok": False, "error": "이미 실행 중이라 취소 불가"}, 400)
        else:
            send_json(self, {"ok": False, "error": "not found"}, 404)


def main():
    os.makedirs(WEB_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"H3 웹 서버 시작 http://{HOST}:{PORT} (comfy={COMFY})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def _lambda_main(environ, start_response):
    """Vercel WSGI entry — request를 내부 HTTP 핸들러로 라우팅."""
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    body = b""
    if environ.get("CONTENT_LENGTH"):
        n = int(environ["CONTENT_LENGTH"])
        body = environ.get("wsgi.input", None)
        body = body.read(n) if body else b""

    # Create a fake socket pair to feed into BaseHTTPRequestHandler
    import io
    req_lines = [f"{method} {path} HTTP/1.1", "Host: vercel", "Content-Length: " + str(len(body))]
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            req_lines.append(f"{k[5:].replace('_', '-')}: {v}")
    req_data = "\r\n".join(req_lines).encode() + b"\r\n\r\n" + body

    class _Resp:
        def __init__(self):
            self.status_code = 200
            self.headers = {}
            self._buf = io.BytesIO()
        def send_response(self, code):
            self.status_code = code
        def send_header(self, k, v):
            self.headers[k] = v
        def end_headers(self):
            pass
        def wfile_write(self, data):
            self._buf.write(data)
        @property
        def wfile(self):
            self._wfile = self
            return self
        def write(self, data):
            self._buf.write(data)

    resp = _Resp()
    handler = Handler.__new__(Handler)
    handler.request_version = "HTTP/1.1"
    handler.command = method
    handler.path = path
    handler.rfile = io.BytesIO(req_data)
    handler.wfile = resp
    handler.headers = {}
    handler.server = None
    handler.client_address = ("vercel", 0)
    try:
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        else:
            handler.send_response(405); handler.end_headers()
    except Exception as e:
        resp.status_code = 500
        resp._buf = io.BytesIO(str(e).encode())
        resp.headers = {"Content-Type": "text/plain"}

    headers = [(k, v) for k, v in resp.headers.items()]
    start_response(f"{resp.status_code}", headers)
    return [resp._buf.getvalue()]


def handler(environ, start_response):
    return _lambda_main(environ, start_response)


if __name__ == "__main__":
    main()
