#!/usr/bin/env python3
"""MiniMax H3 영상 생성/다운로드 페이지 서버 v2.

- 30fps 출력 (H.264 리샘플링)
- 최대 60초 (4초 세그먼트 분할 + ffconcat 스티치)
- 생성 시간 추정
- NAS 저장 (원본 보존) + 로컬 다운로드 서빙
- Negative prompt 지원 (한방에 prompt에 병합)
- ComfyUI 자동 기동
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
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
COMFY_OUT = "/home/aski/minimax-h3/output"
NAS_DIR = "/mnt/comfyui_videos/comfyui/video/h3web"
OUT_DIR = os.environ.get("H3_OUT_DIR", os.path.expanduser("~/h3-web/output"))

# H3 모델: 24fps, 17k+5 프레임 그리드, 세그먼트당 ~4초
SEG_SECONDS = 4
SEG_FRAMES = snap_len = None  # set below
MAX_SECONDS = 60
SEGMENT_EST_SECONDS = 75  # segment당 예상 생성 시간 (초)

JOBS = {}
LOCK = threading.Lock()


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def snap_len(seconds):
    """seconds를 17k+5 프레임 그리드에 스냅 (24fps 기준)."""
    raw = max(124, round(seconds * 24))
    return raw + (5 - (raw % 17)) % 17


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
    env.update({"XDG_RUNTIME_DIR": "/run/user/1000",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"})
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


def make_prompt(text, width, height, length, steps, seed, prefix, negative=""):
    # H3 단일 prompt 노드 → negative를 한방에 병합
    full_prompt = text
    if negative:
        full_prompt = f"{text} (do NOT include: {negative})"
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": full_prompt, "width": width, "height": height, "length": length}},
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
    fname = str(fname).replace("\\", "/")
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


def _copy_to_nas(src_path):
    """원본 파일을 NAS에 저장. 실패해도 로컬은 유지."""
    try:
        os.makedirs(NAS_DIR, exist_ok=True)
        dst = os.path.join(NAS_DIR, os.path.basename(src_path))
        if os.access(src_path, os.R_OK):
            shutil.copy2(src_path, dst)
        else:
            run_asu(f"cp '{src_path}' '{dst}' && chmod 644 '{dst}'", timeout=60)
        log(f"  NAS 저장: {dst}")
    except Exception as e:
        log(f"  NAS 저장 실패 (로컬 유지): {e}")


def _remux_30fps(src_path, dst_path):
    """24fps mp4 → 30fps H.264 mp4로 리샘플링 (음성 포함)."""
    cmd = ["ffmpeg", "-y", "-i", src_path,
           "-c:v", "libx264", "-preset", "fast", "-crf", "18",
           "-c:a", "aac", "-b:a", "192k",
           "-r", "30",
           dst_path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg 리샘플링 실패: {p.stderr.strip()[:300]}")
    log(f"  30fps 변환: {os.path.basename(dst_path)}")


def _stitch_segments(seg_files, dst_path):
    """여러 세그먼트 mp4를 ffconcat으로 이어붙임 (동일 인코딩 → 무손실)."""
    concat_file = dst_path + ".concat.txt"
    with open(concat_file, "w") as f:
        for sf in seg_files:
            f.write(f"file '{sf}'\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
           "-c", "copy", dst_path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    os.remove(concat_file)
    if p.returncode != 0:
        raise RuntimeError(f"스티치 실패: {p.stderr.strip()[:300]}")
    log(f"  스티치 완료: {len(seg_files)}개 → {os.path.basename(dst_path)}")


def run_job(job_id, cfg):
    with LOCK:
        JOBS[job_id]["status"] = "starting"
    try:
        ensure_comfyui()
        total_seconds = min(cfg["seconds"], MAX_SECONDS)
        segments = max(1, round(total_seconds / SEG_SECONDS))
        seg_frames = snap_len(SEG_SECONDS)
        total_frames = seg_frames * segments
        # 생성 시간 추정: segment당 ~75초 (10스텝 기준), 스티치 ~10초
        est = segments * SEGMENT_EST_SECONDS + 15
        with LOCK:
            JOBS[job_id].update(
                segments=segments, total_seconds=total_seconds,
                estimated_seconds=est,
            )
        log(f"job {job_id}: {total_seconds}s {segments}개 세그먼트 "
            f"(각 {SEG_SECONDS}s, {seg_frames}프레임) 예상 {est}초")

        # 각 세그먼트 생성
        seg_files = []
        for i in range(segments):
            with LOCK:
                JOBS[job_id]["progress"] = {
                    "phase": f"세그먼트 {i+1}/{segments} 생성 중",
                    "elapsed": round(time.time() - JOBS[job_id].get("started", time.time()), 1),
                }
            seed = (cfg["seed"] if cfg["seed"] >= 0 else int.from_bytes(os.urandom(6), "big")) + i
            prefix = f"h3web/{job_id}_s{i:02d}"
            workflow = make_prompt(cfg["prompt"], cfg["width"], cfg["height"],
                                   seg_frames, cfg["steps"], seed, prefix,
                                   negative=cfg.get("negative", ""))
            cid = str(uuid.uuid4())
            queued = comfy_post("/prompt", {"prompt": workflow, "client_id": cid})
            if "error" in queued:
                raise RuntimeError(json.dumps(queued, ensure_ascii=False)[:600])
            pid = queued["prompt_id"]
            log(f"  seg {i+1}/{segments} queued pid={pid}")

            # 폴링
            while True:
                h = comfy_get(f"/history/{pid}", timeout=30)
                if pid in h:
                    result = h[pid]
                    status = result.get("status", {})
                    if status.get("status_str") == "error" or not status.get("completed", False):
                        raise RuntimeError(f"seg {i+1} 실패: " + json.dumps(result, ensure_ascii=False)[:800])
                    files = []
                    for out in result.get("outputs", {}).values():
                        for key in ("videos", "gifs", "images"):
                            for f in out.get(key, []):
                                if isinstance(f, dict):
                                    fn = f.get("filename", "")
                                    sub = f.get("subfolder", "")
                                    files.append(os.path.join(sub, fn) if sub else fn)
                                else:
                                    files.append(str(f))
                    mp4 = [f for f in files if str(f).lower().endswith(".mp4")] or files
                    if not mp4:
                        raise RuntimeError(f"seg {i+1} 완료되었으나 mp4 없음: {str(files)[:300]}")
                    fname = str(mp4[0])
                    src = _resolve_output(fname)
                    dst_dir = os.path.join(OUT_DIR, job_id)
                    os.makedirs(dst_dir, exist_ok=True)
                    dst = os.path.join(dst_dir, f"seg_{i:02d}.mp4")
                    if os.access(src, os.R_OK):
                        shutil.copy2(src, dst)
                    else:
                        run_asu(f"cp '{src}' '{dst}' && chmod 644 '{dst}'", timeout=60)
                    seg_files.append(dst)
                    log(f"  seg {i+1}/{segments} 완료 → {dst}")
                    break
                q = comfy_get("/queue", timeout=30)
                with LOCK:
                    JOBS[job_id]["progress"] = {
                        "phase": f"세그먼트 {i+1}/{segments} 대기",
                        "elapsed": round(time.time() - JOBS[job_id].get("started", time.time()), 1),
                        "queue_running": len(q.get("queue_running", [])),
                        "queue_pending": len(q.get("queue_pending", [])),
                    }
                time.sleep(8)

        # 최종 파일 경로
        dst_dir = os.path.join(OUT_DIR, job_id)
        final_local = os.path.join(dst_dir, f"{job_id}.mp4")

        if segments == 1:
            # 단일 세그먼트: 30fps 리샘플링
            with LOCK:
                JOBS[job_id]["progress"] = {"phase": "30fps 변환 중", "elapsed": round(time.time() - JOBS[job_id].get("started", time.time()), 1)}
            _remux_30fps(seg_files[0], final_local)
        else:
            # 다수 세그먼트: 먼저 스티치 → 30fps 변환
            with LOCK:
                JOBS[job_id]["progress"] = {"phase": "세그먼트 스티치 중", "elapsed": round(time.time() - JOBS[job_id].get("started", time.time()), 1)}
            _stitch_segments(seg_files, final_local)
            with LOCK:
                JOBS[job_id]["progress"] = {"phase": "30fps 변환 중", "elapsed": round(time.time() - JOBS[job_id].get("started", time.time()), 1)}
            _remux_30fps(final_local, final_local + ".tmp.mp4")
            os.replace(final_local + ".tmp.mp4", final_local)

        # NAS에 저장
        with LOCK:
            JOBS[job_id]["progress"] = {"phase": "NAS 저장 중", "elapsed": round(time.time() - JOBS[job_id].get("started", time.time()), 1)}
        _copy_to_nas(final_local)

        with LOCK:
            JOBS[job_id].update(
                status="done", file=os.path.basename(final_local), src=final_local,
                elapsed=round(time.time() - JOBS[job_id].get("started", time.time()), 1),
                segments=segments, total_seconds=total_seconds,
            )
        log(f"job {job_id} done → {final_local} ({segments}seg, {total_seconds}s, 30fps)")
        return
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
            jid = p.split("/")[3]
            with LOCK:
                j = dict(JOBS.get(jid)) if JOBS.get(jid) else None
            send_json(self, {"ok": True, "job": j}, code=200 if j else 404)
        elif p.startswith("/api/download/"):
            jid = p.split("/")[3]
            # 1) JOBS에서 src 확인
            with LOCK:
                j = JOBS.get(jid)
            src = j.get("src") if j and j.get("status") == "done" else None
            # 2) 폴백: JOBS에 없어도 파일 기반 탐색
            if not src or not os.path.exists(src):
                src = os.path.join(OUT_DIR, jid, f"{jid}.mp4")
            if not os.path.exists(src):
                send_json(self, {"ok": False, "error": f"다운로드 가능 영상 없음 ({jid})"}, 404)
                return
            fsize = os.path.getsize(src)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(fsize))
            self.send_header("Content-Disposition", f'attachment; filename="{jid}.mp4"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(src, "rb") as f:
                shutil.copyfileobj(f, self.wfile, length=1024 * 256)
        elif p == "/api/health":
            send_json(self, {"ok": True, "comfy_up": comfy_up(),
                             "nas_ok": os.path.isdir(NAS_DIR)})
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
            seconds = min(float(data.get("seconds", 5)), MAX_SECONDS)
            segments = max(1, round(seconds / SEG_SECONDS))
            est = segments * SEGMENT_EST_SECONDS + 15
            cfg = {
                "prompt": prompt,
                "negative": (data.get("negative") or "").strip(),
                "width": int(data.get("width", 608)),
                "height": int(data.get("height", 352)),
                "seconds": seconds,
                "steps": int(data.get("steps", 6)),
                "seed": int(data.get("seed", -1)),
                "filename": "h3web",
            }
            jid = str(uuid.uuid4())[:8]
            with LOCK:
                JOBS[jid] = {
                    "id": jid, "status": "queued", "created": time.time(),
                    "cfg": cfg, "prompt": prompt,
                    "segments": segments, "total_seconds": seconds,
                    "estimated_seconds": est,
                }
            threading.Thread(target=run_job, args=(jid, cfg), daemon=True).start()
            log(f"new job {jid}: {prompt[:50]}... {cfg['width']}x{cfg['height']} "
                f"{seconds}s {segments}seg steps={cfg['steps']}")
            send_json(self, {
                "ok": True, "job": jid,
                "segments": segments, "total_seconds": seconds,
                "estimated_seconds": est,
                "message": f"{segments}개 세그먼트, 예상 {est}초"
            })
        elif p.startswith("/api/cancel/"):
            jid = p.split("/")[3]
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
    os.makedirs(OUT_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"H3 웹 서버 v2 시작 http://{HOST}:{PORT} (comfy={COMFY}, nas={NAS_DIR})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

