#!/usr/bin/env python3
"""MiniMax H3 영상 생성/다운로드 페이지 서버 v2.

- 24fps 출력 (H.264 고품질)
- 최대 60초 (세그먼트 분할 + ffconcat 스티치 / 연속 단일 생성 선택 가능)
- 생성 시간 추정
- NAS 저장 (원본 보존) + 로컬 다운로드 서빙
- Negative prompt 지원 (한방에 prompt에 병합)
- ComfyUI 자동 기동
- 샘플링 스텝 조절 가능 (기본 6스텝)
- 생성 방식: 연속 단일 생성 / 세그먼트 분할 선택
"""
import json
import os
import re
import time
import shutil
import threading
import subprocess
import urllib.request
import urllib.error
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HOST = os.environ.get("H3_HOST", "0.0.0.0")
PORT = int(os.environ.get("H3_PORT", "8300"))
COMFY = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188")
ASUI = os.environ.get("ASUI", "aski")
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
COMFY_OUT = "/home/aski/minimax-h3/output"
NAS_DIR = "/mnt/comfyui_videos/comfyui/h3_videos"
OUT_DIR = os.environ.get("H3_OUT_DIR", os.path.expanduser("~/h3-web/output"))

# MiniMax H3 Eros E3 production profile. Override filenames with env vars when
# the PGX model directory uses a different revision.
H3_UNET = os.environ.get("H3_UNET", "minimax_h3_fl2va_pruned_int8_convrot.safetensors")
H3_CLIP = os.environ.get("H3_CLIP", "qwen3vl_32b_minimax_h3_bf16.safetensors")
H3_VIDEO_VAE = os.environ.get("H3_VIDEO_VAE", "minimax_h3_video_vae_fp16.safetensors")
H3_AUDIO_VAE = os.environ.get("H3_AUDIO_VAE", "minimax_h3_audio_vae_fp32.safetensors")
H3_LORA = os.environ.get("H3_LORA", "minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors")

# H3 model: 24fps, 17k+5 frame grid
MAX_SECONDS = 60

# 세그먼트 길이 (초)
SEG_CHOICES = (2, 4, 8)
SEG_SECONDS = 4  # 기본값

# 고정 참조 (고정 이미지): 한번 등록하면 서버가 영구 보관 — 삭제 전까지 자동 유지
REF_DIR = os.path.join(os.path.expanduser("~"), "h3-web", "ref")
REF_META = os.path.join(REF_DIR, "meta.json")

# 생성 방식
STRATEGY_CHOICES = ("single", "split")
STRATEGY_SINGLE = "single"  # 연속 단일 생성 (장면 연속성 우선)
STRATEGY_SPLIT = "split"   # 세그먼트 분할 (정확한 길이 우선)

# 샘플링 스텝
STEPS_MIN, STEPS_MAX, STEPS_DEFAULT = 2, 30, 6

# 예상 시간 계수 (초/4초세그먼트, 6스텝 기준)
EST_BASE_SECONDS = 75
EST_STEP_COEF = 8.0  # 스텝당 추가 (6스텝 대비)

JOBS = {}
LOCK = threading.Lock()
JOBS_DIR = os.path.join(os.path.expanduser("~"), "h3-web", "jobs")
QUEUE = []            # FIFO: 대기 중인 job_id
QUEUE_LOCK = threading.Lock()
ACTIVE = [None]       # 실행 중인 job_id (동시 1개)


def _job_file(jid):
    return os.path.join(JOBS_DIR, f"{jid}.json")


def _save_job(jid):
    """job 상태를 JSON 파일에 적음 (LOCK 밖에서 호출)."""
    with LOCK:
        j = JOBS.get(jid)
        if not j:
            return
        snapshot = dict(j)
    try:
        os.makedirs(JOBS_DIR, exist_ok=True)
        tmp = _job_file(jid) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, _job_file(jid))
    except Exception as e:
        log(f"  job 파일 저장 실패: {e}")


def update_job(jid, **kw):
    """LOCK 내부에서 호출: JOBS 갱신 + 디스크 영속화 트리거."""
    with LOCK:
        j = JOBS.get(jid)
        if not j:
            return
        j.update(kw)
    _save_job(jid)


def _restore_jobs():
    """기존 job JSON 복원. 재시작 중이던 건 interrupted 처리."""
    try:
        names = os.listdir(JOBS_DIR)
    except FileNotFoundError:
        return
    now = time.time()
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOBS_DIR, n)) as f:
                j = json.load(f)
        except Exception:
            continue
        jid = j.get("id") or n[:-5]
        j.setdefault("created", now)
        j.setdefault("status", "unknown")
        # started가 없으면 created로 fallback (타임라인 계산용)
        j.setdefault("started", j.get("created", now))
        if j["status"] in ("queued", "starting", "running"):
            j["status"] = "interrupted"
            j["error"] = j.get("error") or "서버 재시작으로 중단됨 — 다시 생성해 주세요"
        with LOCK:
            JOBS[jid] = j
    log(f"  job {len(JOBS)}개 복원 ({JOBS_DIR})")


def queue_worker():
    """FIFO 워커: 대기열에서 하나 꺼내 실행 (동시 1개)."""
    while True:
        with QUEUE_LOCK:
            jid = QUEUE.pop(0) if QUEUE else None
        if jid is None:
            with LOCK:
                ACTIVE[0] = None
            time.sleep(0.5)
            continue
        with LOCK:
            ACTIVE[0] = jid
        try:
            with LOCK:
                cfg = dict(JOBS[jid]["cfg"])
                started = time.time()
                JOBS[jid]["started"] = started
                JOBS[jid]["status"] = "starting"
            _save_job(jid)
            log(f"job {jid} 실행 시작 (worker)")
            run_job(jid, cfg)
        except Exception as e:
            import traceback
            update_job(jid, status="error", error=str(e)[:800])
            log(f"job {jid} worker ERROR: {str(e)[:200]}")
            traceback.print_exc()
        finally:
            with LOCK:
                ACTIVE[0] = None


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def _prog(job_id, phase, **extra):
    """progress 객체: phase + elapsed + pct + eta + (추가 필드)."""
    j = JOBS.get(job_id) or {}
    est = j.get("estimated_seconds") or 75
    now = time.time()
    elapsed = round(now - j.get("started", now), 1)
    total = (j.get("segments") or 1) + 3  # 생성 + 스티치 + 24fps + NAS
    done = extra.get("seg_done", 0) + extra.get("done_phase", 0)
    pct = min(95.0, round(5 + 85 * done / total, 1))
    remain = round(est - elapsed, 0)
    out = {"phase": phase, "elapsed": elapsed, "pct": pct, "eta": max(0, int(remain))}
    out.update(extra)
    return out


def snap_len(seconds):
    """seconds를 17k+5 프레임 그리드에 스냅 (24fps 기준)."""
    raw = max(124, round(seconds * 24))
    return raw + (5 - (raw % 17)) % 17


def estimate_seconds(total_seconds, seg_seconds, strategy, steps):
    """생성 시간 추정 (초). 6스텝 기준 75초/4초세그먼트."""
    step_factor = 1.0 + EST_STEP_COEF * (steps - STEPS_DEFAULT) / max(STEPS_DEFAULT, 1)
    if strategy == STRATEGY_SINGLE:
        # 단일 세그먼트 (길이 그대로, 1회 생성)
        n = max(1, total_seconds / 4.0)
    else:
        n = max(1, round(total_seconds / max(1, seg_seconds)))
    return int(round(n * EST_BASE_SECONDS * step_factor)) + 15


def comfy_get(path, timeout=15):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return json.load(r)


def comfy_post(path, payload, timeout=60):
    req = urllib.request.Request(COMFY + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # 400 바디에 node_errors 검증 상세가 들어있음 → 그대로 노출
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"raw": e.read()[:600].decode(errors="replace")}
        log(f"  ComfyUI {path} HTTP {e.code}: {json.dumps(body, ensure_ascii=False)[:600]}")
        raise RuntimeError(f"ComfyUI {e.code}: {json.dumps(body, ensure_ascii=False)[:600]}")


def comfy_upload_image(data: bytes, filename: str, subfolder: str = "", overwrite=True):
    """바이너리를 ComfyUI 입력 디렉터리에 저장 후 LoadImage용 이름 반환.
    LoadImage는 input/ 아래 평탄한 파일명만 인식하므로 서브폴더를 쓰지 않는다.
    로컬 파일시스템 접근 실패 시 /upload 엔드포인트로 폴백."""
    # 1) 직접 파일 쓰기 (ComfyUI가 로컬에서 돌 때 가장 빠름)
    try:
        base = os.environ.get("COMFY_INPUT_DIR")
        target_dir = base
        if not target_dir:
            for cand in ("/home/aski/ComfyUI/input", "/home/aski/minimax-h3/ComfyUI/input"):
                if os.path.isdir(cand):
                    target_dir = cand
                    break
            if target_dir is None:
                raise RuntimeError("input dir not found")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename))
        dst = os.path.join(target_dir, safe_name)
        with open(dst, "wb") as f:
            f.write(data)
        log(f"  이미지 업로드(직접): {dst}")
        return safe_name
    except Exception as e:
        log(f"  직접 쓰기 실패 ({e}) -> /upload 폴백")
    # 2) /upload 엔드포인트 (멀티파트) — subfolder 비어 있음 (LoadImage가 input/ 평탄 파일만 인식)
    boundary = "----h3web" + uuid.uuid4().hex
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename))
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="image"; filename="{safe_name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        data,
        b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="subfolder"\r\n\r\n',
        b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="overwrite"\r\n\r\n',
        b"true\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    payload = b"".join(parts)
    req = urllib.request.Request(COMFY + "/upload/image", data=payload,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    if not out.get("name"):
        raise RuntimeError(f"ComfyUI 업로드 실패: {out}")
    log(f"  이미지 업로드(/upload): {out.get('name')}")
    return out["name"]


def comfy_up():
    try:
        comfy_get("/system_stats", timeout=8)
        return True
    except Exception as e:
        log(f"  comfy_up probe failed: {e}")
        return False

def run_asu(cmd, timeout=300, check=True):
    """aski 권한으로 명령 실행 (NOPASSWD sudo, CIFS home 충돌 방지)."""
    full = ["sudo", "-n", "-u", ASUI, "bash", "-c", cmd]
    env = dict(os.environ)
    env.update({"XDG_RUNTIME_DIR": "/run/user/1000",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "HOME": "/home/aski",
                "PWD": "/tmp"})
    p = subprocess.run(full, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"asu cmd failed: {cmd}\n{p.stderr.strip()[:400]}")
    return p


def ensure_comfyui():
    if comfy_up():
        return True
    log("ComfyUI down -> reactivating always-on service (comfyui-minimax-h3)")
    # host root unit (systemd, aski) → user unit (minimax-h3-comfyui.service) 순서로 재기동
    # host unit: docker nsenter 우회 (h3ctl 패턴). 실패 시 user unit 폴백.
    started = False
    for cmd in (
        "docker run --rm --privileged --pid=host -v /:/host python:3.12-alpine "
        "chroot /host /usr/bin/nsenter -t 1 -m -i -n -p /usr/bin/systemctl start comfyui-minimax-h3.service",
        "systemctl --user start minimax-h3-comfyui.service",
    ):
        try:
            run_asu(cmd, timeout=60, check=True)
            log(f"  start ok via: {cmd.split('chroot /host')[-1].strip()[:40] if 'chroot' in cmd else cmd}")
            started = True
            break
        except Exception as e:
            log(f"  start attempt failed ({cmd[:50]}...): {e}")
    if not started:
        # 이미 starting 상태면 대기만 (실패가 아니므로)
        log("  no start path succeeded — waiting for existing/startup process")
    for _ in range(300):
        if comfy_up():
            log("ComfyUI ready")
            return True
        time.sleep(2)
    raise RuntimeError("ComfyUI 기동 실패 (300초 대기 초과)")


def build_workflow(text, negative, width, height, length, steps, seed, image_name=None, prefix="h3"):
    """T2V/I2V 워크플로우 — H3 전용. Wan 폴백 제거 (사용자 지정)."""
    base_negative = "text, subtitles, captions, watermark, logo, script overlay, on-screen text, UI elements"
    if negative:
        full_prompt = f"{text} (do NOT include: {base_negative}, {negative})"
    else:
        full_prompt = f"{text} (do NOT include: {base_negative})"

    # ---- H3 워크플로우 (원본) ----
    lora_name = H3_LORA
    lora_avail = any(
        os.path.exists(os.path.join(d, lora_name))
        for d in ["/home/aski/ComfyUI/models/loras",
                  "/home/aski/ComfyUI/models/loras/split_files/loras"]
    )
    model_ref = ["1a", 0] if lora_avail else ["1", 0]
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": H3_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": H3_CLIP, "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VIDEO_VAE}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": H3_AUDIO_VAE}},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": full_prompt, "width": width, "height": height, "length": length}},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "7": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "8": {"class_type": "BasicScheduler", "inputs": {"model": model_ref, "scheduler": "simple", "steps": steps, "denoise": 1.0}},
        "9": {"class_type": "BasicGuider", "inputs": {"model": model_ref, "conditioning": ["5", 0]}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["6", 0], "guider": ["9", 0], "sampler": ["7", 0], "sigmas": ["8", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "audio": ["12", 0], "fps": 24.0, "bit_depth": 8}},
        "14": {"class_type": "SaveVideo", "inputs": {"video": ["13", 0], "filename_prefix": prefix, "format": "mp4", "codec": "h264", "encoding": "re-encode", "crf": 18.0}},
    }
    if lora_avail:
        wf["1a"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": lora_name, "strength_model": 1.0}}
    if image_name:
        wf["15"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        wf["5"]["inputs"]["first_frame"] = ["15", 0]
    return wf


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


def nas_ok():
    """NAS 마운트 + 쓰기 권한 검증 (직접 쓰기 기준)."""
    try:
        if not os.path.isdir(NAS_DIR):
            return False
        probe = os.path.join(NAS_DIR, ".h3web_write_test")
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _copy_to_nas(src_path):
    """원본 파일을 NAS에 저장. 실패해도 로컬은 유지.
    CIFS uid 제한 → run_asu(aski)로 우회."""
    dst = os.path.join(NAS_DIR, os.path.basename(src_path))
    try:
        if not os.path.isdir(NAS_DIR):
            os.makedirs(NAS_DIR, exist_ok=True)
        # 직접 쓰기 (NAS_DIR은 aski 소유 CIFS — uid 1000/1000)
        # CIFS + seccomp: chmod/chown이 EPERM → os.open(mode=0o644)로 생성 시점에 권한 지정
        try:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            with os.fdopen(fd, 'wb') as f:
                with open(src_path, 'rb') as s:
                    f.write(s.read())
            log(f"  NAS 저장: {dst}")
            return True
        except Exception as e:
            log(f"  직접 NAS 쓰기 실패 ({e}) -> run_asu 폴백")
        # 직접 쓰기 실패 (CIFS seccomp 등) → run_asu 폴백
        p = run_asu(f"cp '{src_path}' '{dst}' && chmod 644 '{dst}'", timeout=60)
        if p.returncode == 0 and os.path.isfile(dst):
            log(f"  NAS 저장(run_asu): {dst}")
            return True
        # run_asu도 실패 (CIFS에서 로컬 파일 stat 불가) → 로컬에서 읽고 NAS에 쓰기
        try:
            with open(src_path, 'rb') as s:
                data = s.read()
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            log(f"  NAS 저장(로컬읽기→직접쓰기): {dst}")
            return True
        except Exception as e2:
            raise RuntimeError(f"NAS 저장 전 경로 실패: {e}")
    except Exception as e:
        log(f"  NAS 저장 실패 (로컬 유지): {e}")
        return False


def _remux_24fps(src_path, dst_path):
    """24fps H.264 고품질 mp4 (CRF 16, 음성 포함)."""
    cmd = ["ffmpeg", "-y", "-i", src_path,
           "-c:v", "libx264", "-preset", "medium", "-crf", "16",
           "-c:a", "aac", "-b:a", "192k",
           "-r", "24",
           dst_path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg 리인코딩 실패: {p.stderr.strip()[:300]}")
    log(f"  24fps 변환: {os.path.basename(dst_path)}")


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
    try:
        ensure_comfyui()
        total_seconds = min(cfg["seconds"], MAX_SECONDS)
        strategy = cfg.get("strategy", STRATEGY_SPLIT)
        seg_seconds = int(cfg.get("seg_seconds", SEG_SECONDS))
        if strategy == STRATEGY_SINGLE:
            # 연속 단일 생성: 길이 그대로 1회 생성 (정확한 길이)
            segments = 1
            seg_len = total_seconds
        else:
            # 세그먼트 분할: seg_seconds씩 분할 (세그먼트 경계에서 스티치)
            segments = max(1, round(total_seconds / seg_seconds))
            seg_len = seg_seconds
        seg_frames = snap_len(seg_len)
        total_frames = seg_frames * segments
        est = estimate_seconds(total_seconds, seg_seconds, strategy, cfg["steps"])
        update_job(job_id, segments=segments, total_seconds=total_seconds,
                   estimated_seconds=est)
        log(f"job {job_id}: {total_seconds}s [{strategy}] {segments}개 세그먼트 "
            f"(각 {seg_len}s, {seg_frames}프레임) steps={cfg['steps']} 예상 {est}초")

        # 각 세그먼트 생성
        seg_files = []
        for i in range(segments):
            update_job(job_id, progress=_prog(job_id,
                f"세그먼트 {i+1}/{segments} 생성 중" if segments > 1 else "영상 생성 중",
                seg_done=i))
            seed = (cfg["seed"] if cfg["seed"] >= 0 else int.from_bytes(os.urandom(6), "big")) + i
            update_job(job_id, seed=seed)
            prefix = f"h3web/{job_id}_s{i:02d}"
            workflow = build_workflow(cfg["prompt"], cfg.get("negative", ""), cfg["width"], cfg["height"],
                                   seg_frames, cfg["steps"], seed,
                                   image_name=cfg.get("image_name", ""), prefix=prefix)
            cid = str(uuid.uuid4())
            queued = comfy_post("/prompt", {"prompt": workflow, "client_id": cid})
            if "error" in queued:
                err_msg = json.dumps(queued, ensure_ascii=False)
                raise RuntimeError(err_msg[:600])
            pid = queued["prompt_id"]
            log(f"  seg {i+1}/{segments} queued pid={pid} (H3)")

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
                update_job(job_id, progress=_prog(job_id,
                    f"세그먼트 {i+1}/{segments} 대기", seg_done=i,
                    queue_running=len(q.get("queue_running", [])),
                    queue_pending=len(q.get("queue_pending", []))))
                time.sleep(8)

        # 최종 파일 경로
        dst_dir = os.path.join(OUT_DIR, job_id)
        final_local = os.path.join(dst_dir, f"{job_id}.mp4")

        if segments == 1:
            # 단일 세그먼트: 24fps 고품질 리인코딩
            update_job(job_id, progress=_prog(job_id, "24fps 변환 중", seg_done=segments, done_phase=1))
            _remux_24fps(seg_files[0], final_local)
        else:
            # 다수 세그먼트: 먼저 스티치 → 24fps 변환
            update_job(job_id, progress=_prog(job_id, "세그먼트 스티치 중", seg_done=segments, done_phase=1))
            _stitch_segments(seg_files, final_local)
            update_job(job_id, progress=_prog(job_id, "24fps 변환 중", seg_done=segments, done_phase=2))
            _remux_24fps(final_local, final_local + ".tmp.mp4")
            os.replace(final_local + ".tmp.mp4", final_local)

        # NAS에 저장
        update_job(job_id, progress=_prog(job_id, "NAS 저장 중", seg_done=segments, done_phase=3))
        nas_success = _copy_to_nas(final_local)

        fsize = os.path.getsize(final_local)
        update_job(job_id,
            status="done", file=os.path.basename(final_local), src=final_local,
            elapsed=round(time.time() - JOBS[job_id].get("started", time.time()), 1),
            segments=segments, total_seconds=total_seconds,
            size=fsize,
            nas_saved=nas_success,
        )
        log(f"job {job_id} done → {final_local} ({segments}seg, {total_seconds}s, 24fps, {fsize//1048576}MB, nas={'OK' if nas_success else 'FAIL'})")
        return
    except Exception as e:
        update_job(job_id, status="error", error=str(e)[:800])
        log(f"job {job_id} ERROR: {str(e)[:200]}")


# ---------- HTTP ----------
UPLOADED = {}   # nonce -> {"path": str, "w": int, "h": int, "ts": float}
UPLOAD_LOCK = threading.Lock()
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def _gc_uploads(keep=None):
    """10분 이상 지난 업로드 정지 + 50개 초과 시 정리."""
    now = time.time()
    with UPLOAD_LOCK:
        for k in list(UPLOADED):
            if k != keep and now - UPLOADED[k]["ts"] > 600:
                try:
                    os.remove(UPLOADED[k]["path"])
                except Exception:
                    pass
                del UPLOADED[k]
        for k in list(UPLOADED):
            if len(UPLOADED) > 50:
                try:
                    os.remove(UPLOADED[k]["path"])
                except Exception:
                    pass
                del UPLOADED[k]


def _ref_path():
    return os.path.join(REF_DIR, "ref.png")


def _load_ref():
    """고정 참조 이미지 존재 여부 + meta 반환. 없으면 None."""
    meta_f = REF_META
    if not os.path.isfile(meta_f) or not os.path.isfile(_ref_path()):
        return None
    try:
        with open(meta_f) as f:
            m = json.load(f)
    except Exception:
        return None
    return {
        "name": m.get("name", ""),
        "w": m.get("w", 0), "h": m.get("h", 0),
        "size": m.get("size", 0), "ts": m.get("ts", 0),
    }


def _save_ref(data: bytes, w: int, h: int, name: str):
    """고정 참조 이미지 영구 저장 (삭제 전까지 유지)."""
    os.makedirs(REF_DIR, exist_ok=True)
    with open(_ref_path(), "wb") as f:
        f.write(data)
    meta = {"name": name, "w": w, "h": h, "size": len(data), "ts": time.time()}
    with open(REF_META, "w") as f:
        json.dump(meta, f, ensure_ascii=False)
    return meta


def _delete_ref():
    try:
        if os.path.isfile(_ref_path()):
            os.remove(_ref_path())
    except Exception:
        pass
    try:
        if os.path.isfile(REF_META):
            os.remove(REF_META)
    except Exception:
        pass


def send_json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
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
            with QUEUE_LOCK:
                q_len = len(QUEUE)
            with LOCK:
                active_id = ACTIVE[0]
            # 상세 상태: ComfyUI 버전/GPU, NAS, 활성 job
            cstats = comfy_get("/system_stats", timeout=3) if comfy_up() else {}
            send_json(self, {
                "ok": True, "jobs": items,
                "comfy_up": comfy_up(),
                "comfy_info": {
                    "version": cstats.get("system", {}).get("comfyui_version", ""),
                    "gpu": (cstats.get("devices") or [{}])[0].get("name", ""),
                    "gpu_vram_free_gb": round((cstats.get("devices") or [{}])[0].get("vram_free", 0) / 1e9, 1),
                    "gpu_vram_total_gb": round((cstats.get("devices") or [{}])[0].get("vram_total", 0) / 1e9, 1),
                } if cstats else None,
                "nas_ok": nas_ok(),
                "queue_len": q_len,
                "active_job": active_id,
            })
        elif p.startswith("/api/job/"):
            jid = p.split("/")[3]
            with LOCK:
                j = dict(JOBS.get(jid)) if JOBS.get(jid) else None
            send_json(self, {"ok": True, "job": j}, code=200 if j else 404)
        elif p.startswith("/api/ref/status"):
            # GET /api/ref/status — 고정 참조 메타데이터만
            ref = _load_ref()
            send_json(self, {"ok": True, "ref": ref})
        elif p.startswith("/api/ref"):
            # GET /api/ref — 고정 참조 이미지 byte 반환 (없으면 404)
            ref = _load_ref()
            if not ref:
                send_json(self, {"ok": False, "ref": None}, 404)
                return
            fsize = os.path.getsize(_ref_path())
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(fsize))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(_ref_path(), "rb") as f:
                self.wfile.write(f.read())
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
            with QUEUE_LOCK:
                q_len = len(QUEUE)
            with LOCK:
                active_id = ACTIVE[0]
            send_json(self, {"ok": True, "comfy_up": comfy_up(),
                             "nas_ok": nas_ok(),
                             "queue_len": q_len,
                             "active_job": active_id})
        else:
            self._static(p)

    def _handle_upload(self):
        """I2V용 이미지 업로드 (multipart/form-data 또는 raw binary).
        성공 시 {ok, nonce, width, height, size} 반환."""
        ctype = self.headers.get("Content-Type", "")
        clen = int(self.headers.get("Content-Length", 0))
        if clen > MAX_UPLOAD_BYTES:
            self.rfile.read(clen)
            send_json(self, {"ok": False, "error": f"파일 초과 (최대 {MAX_UPLOAD_BYTES//1048576}MB)"}, 400)
            return
        raw = self.rfile.read(clen) if clen else b""
        if not raw:
            send_json(self, {"ok": False, "error": "빈 요청"}, 400)
            return

        data = None
        fname = "upload.png"
        if ctype.startswith("multipart/form-data"):
            m = re.search(r"boundary=(\"?)([^\";]+)\1", ctype)
            if not m:
                send_json(self, {"ok": False, "error": "boundary 없음"}, 400)
                return
            boundary = ("--" + m.group(2)).encode()
            parts = raw.split(boundary)
            for part in parts:
                if b"Content-Disposition" not in part:
                    continue
                head, _, body = part.partition(b"\r\n\r\n")
                body = body.rstrip(b"\r\n")
                hm = re.search(rb'name="([^"]*)"', head)
                fm = re.search(rb'filename="([^"]*)"', head)
                name = hm.group(1).decode() if hm else ""
                if name == "image" and body:
                    fname = fm.group(1).decode() if fm else "upload.png"
                    data = body
                    break
            if data is None:
                send_json(self, {"ok": False, "error": "image 필드 없음"}, 400)
                return
        else:
            data = raw
            disp = self.headers.get("Content-Disposition", "")
            fm = re.search(r'filename="?([^";]+)"?', disp)
            if fm:
                fname = fm.group(1)

        ext = os.path.splitext(fname)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            send_json(self, {"ok": False, "error": "지원 형식: png/jpg/webp/bmp (15MB 이하)"}, 400)
            return

        # PIL로 차원 확인 (PIL 없으면 0x0)
        w = h = 0
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
        except Exception:
            pass
        if w and h and (w < 128 or h < 128):
            send_json(self, {"ok": False, "error": f"이미지가 너무 작습니다 ({w}x{h})"}, 400)
            return

        nonce = uuid.uuid4().hex[:10]
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(fname))[:40] or "upload.png"
        tmpdir = os.path.join(OUT_DIR, "uploads")
        os.makedirs(tmpdir, exist_ok=True)
        dst = os.path.join(tmpdir, f"{nonce}_{safe}")
        with open(dst, "wb") as f:
            f.write(data)
        with UPLOAD_LOCK:
            UPLOADED[nonce] = {"path": dst, "w": w, "h": h, "ts": time.time()}
        _gc_uploads(keep=nonce)
        log(f"upload {nonce}: {safe} {w}x{h} {len(data)}B")
        send_json(self, {"ok": True, "nonce": nonce, "width": w, "height": h,
                         "size": len(data), "name": safe})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/ref/set":
            # 고정 참조 등록 (multipart/form-data: file=이미지)
            ctype = self.headers.get("Content-Type", "")
            clen = int(self.headers.get("Content-Length", 0))
            if clen > MAX_UPLOAD_BYTES:
                self.rfile.read(clen)
                send_json(self, {"ok": False, "error": f"파일 초과 (최대 {MAX_UPLOAD_BYTES//1048576}MB)"}, 400)
                return
            raw = self.rfile.read(clen) if clen else b""
            if not raw:
                send_json(self, {"ok": False, "error": "빈 요청"}, 400)
                return
            data = None
            fname = "ref.png"
            if ctype.startswith("multipart/form-data"):
                m = re.search(r"boundary=(\"?)([^\";]+)\1", ctype)
                if not m:
                    send_json(self, {"ok": False, "error": "boundary 없음"}, 400)
                    return
                boundary = ("--" + m.group(2)).encode()
                for part in raw.split(boundary):
                    if b"Content-Disposition" not in part:
                        continue
                    head, _, body = part.partition(b"\r\n\r\n")
                    body = body.rstrip(b"\r\n")
                    hm = re.search(rb'name="([^"]*)"', head)
                    fm = re.search(rb'filename="([^"]*)"', head)
                    name = hm.group(1).decode() if hm else ""
                    if name == "file" and body:
                        fname = fm.group(1).decode() if fm else "ref.png"
                        data = body
                        break
                if data is None:
                    send_json(self, {"ok": False, "error": "file 필드 없음"}, 400)
                    return
            else:
                data = raw
                disp = self.headers.get("Content-Disposition", "")
                fm = re.search(r'filename="?([^";]+)"?', disp)
                if fm:
                    fname = fm.group(1)
            ext = os.path.splitext(fname)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                send_json(self, {"ok": False, "error": "지원 형식: png/jpg/webp/bmp"}, 400)
                return
            w = h = 0
            try:
                import io
                from PIL import Image
                with Image.open(io.BytesIO(data)) as im:
                    w, h = im.size
            except Exception:
                pass
            meta = _save_ref(data, w, h, fname)
            log(f"고정 참조 등록: {fname} ({w}x{h})")
            send_json(self, {"ok": True, "ref": meta})
            return
        if p == "/api/ref/delete":
            _delete_ref()
            log("고정 참조 삭제")
            send_json(self, {"ok": True})
            return
        if p == "/api/upload":
            self._handle_upload()
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            send_json(self, {"ok": False, "error": f"bad request: {e}"}, 400)
            return
        if p == "/api/generate":
            mode = (data.get("mode") or "t2v").strip().lower()
            if mode not in ("t2v", "i2v"):
                send_json(self, {"ok": False, "error": "mode는 t2v 또는 i2v여야 합니다"}, 400)
                return
            prompt = (data.get("prompt") or "").strip()
            if len(prompt) < 3:
                send_json(self, {"ok": False, "error": "프롬프트가 너무 짧습니다"}, 400)
                return
            # 이미지 업로드 nonce (I2V 전용)
            image_name = ""
            upload_nonce = (data.get("image") or "").strip()
            ref_mode = str(data.get("ref_mode") or "").strip()
            if mode == "i2v":
                if not upload_nonce and ref_mode == "fixed":
                    # 고정 참조 모드: 서버가 영구 보관한 참조 이미지를 자동 사용
                    ref = _load_ref()
                    if not ref:
                        send_json(self, {"ok": False, "error": "고정 참조가 등록되지 않았습니다 — 참조 이미지를 먼저 등록해 주세요"}, 400)
                        return
                    try:
                        with open(_ref_path(), "rb") as f:
                            ref_bytes = f.read()
                        image_name = comfy_upload_image(ref_bytes, f"h3web_ref_{uuid.uuid4().hex[:8]}.png")
                        log(f"  고정 참조 자동 사용: {ref['name']} ({ref['w']}x{ref['h']}) → {image_name}")
                    except Exception as e:
                        send_json(self, {"ok": False, "error": f"고정 참조 전송 실패: {e}"}, 500)
                        return
                elif not upload_nonce:
                    send_json(self, {"ok": False, "error": "이미지를 먼저 업로드하거나 고정 참조를 선택해 주세요 (I2V)"}, 400)
                    return
                else:
                    with UPLOAD_LOCK:
                        up = UPLOADED.get(upload_nonce)
                    if not up:
                        send_json(self, {"ok": False, "error": "이미지가 만료되었습니다 — 다시 업로드해 주세요"}, 400)
                        return
                    # 즉시 ComfyUI 입력 디렉터리로 전송
                    try:
                        with open(up["path"], "rb") as f:
                            img_bytes = f.read()
                        safe_fname = f"h3web_{upload_nonce}.png"
                        image_name = comfy_upload_image(img_bytes, safe_fname)
                        log(f"  I2V 이미지: {image_name} ({up['w']}x{up['h']})")
                    except Exception as e:
                        send_json(self, {"ok": False, "error": f"이미지 전송 실패: {e}"}, 500)
                        return
            seconds = min(float(data.get("seconds", 5)), MAX_SECONDS)
            strategy = (data.get("strategy") or STRATEGY_SPLIT).strip().lower()
            if strategy not in STRATEGY_CHOICES:
                strategy = STRATEGY_SPLIT
            try:
                seg_seconds = int(data.get("seg_seconds", SEG_SECONDS))
            except Exception:
                seg_seconds = SEG_SECONDS
            if seg_seconds not in SEG_CHOICES:
                seg_seconds = SEG_SECONDS
            if strategy == STRATEGY_SINGLE:
                segments = 1
            else:
                segments = max(1, round(seconds / seg_seconds))
            try:
                steps = int(data.get("steps", STEPS_DEFAULT))
            except Exception:
                steps = STEPS_DEFAULT
            steps = max(STEPS_MIN, min(STEPS_MAX, steps))
            est = estimate_seconds(seconds, seg_seconds, strategy, steps)
            fname = re.sub(r'[^\w\-]', '_', (data.get("filename") or "video")).strip()[:40] or "video"
            cfg = {
                "mode": mode,
                "prompt": prompt,
                "negative": (data.get("negative") or "").strip(),
                "width": int(data.get("width", 1344)),
                "height": int(data.get("height", 768)),
                "seconds": seconds,
                "strategy": strategy,
                "seg_seconds": seg_seconds,
                "steps": steps,
                "seed": int(data.get("seed", -1)),
                "filename": fname,
                "image_name": image_name,
            }
            jid = str(uuid.uuid4())[:8]
            with LOCK:
                JOBS[jid] = {
                    "id": jid, "status": "queued", "created": time.time(),
                    "cfg": cfg, "prompt": prompt,
                    "mode": mode,
                    "segments": segments, "total_seconds": seconds,
                    "estimated_seconds": est,
                }
            _save_job(jid)
            with QUEUE_LOCK:
                QUEUE.append(jid)
            log(f"new job {jid} [{mode}]: {prompt[:50]}... {cfg['width']}x{cfg['height']} "
                f"{seconds}s [{strategy}] {segments}seg steps={cfg['steps']}"
                + (f" img={image_name}" if image_name else ""))
            send_json(self, {
                "ok": True, "job": jid,
                "segments": segments, "total_seconds": seconds,
                "strategy": strategy, "seg_seconds": seg_seconds,
                "steps": steps,
                "estimated_seconds": est,
                "message": f"{segments}개 세그먼트, 예상 {est}초"
            })
        elif p.startswith("/api/cancel/"):
            jid = p.split("/")[3]
            with LOCK:
                j = JOBS.get(jid)
                if j and j["status"] in ("queued", "starting"):
                    j["status"] = "cancelled"
                    _save_job(jid)
                    # 대기열에서 제거
                    with QUEUE_LOCK:
                        if jid in QUEUE:
                            QUEUE.remove(jid)
                    send_json(self, {"ok": True})
                else:
                    send_json(self, {"ok": False, "error": "이미 실행 중이라 취소 불가"}, 400)
        elif p.startswith("/api/delete/"):
            jid = p.split("/")[3]
            with LOCK:
                j = JOBS.get(jid)
                if not j:
                    send_json(self, {"ok": False, "error": "job 없음"}, 404)
                    return
                if j["status"] in ("queued", "starting", "running"):
                    send_json(self, {"ok": False, "error": "실행 중이라 삭제 불가"}, 400)
                    return
                # 파일 삭제
                src = j.get("src", "")
                try:
                    if src and os.path.isfile(src):
                        os.remove(src)
                    dst_dir = os.path.join(OUT_DIR, jid)
                    if os.path.isdir(dst_dir):
                        shutil.rmtree(dst_dir)
                except OSError as e:
                    log(f"  delete 파일 실패: {e}")
                # NAS에서도 삭제 (run_asu로, CIFS uid 제한 우회)
                try:
                    base = os.path.basename(src) if src else f"{jid}.mp4"
                    nas_f = os.path.join(NAS_DIR, base)
                    if os.path.isfile(nas_f):
                        p = run_asu(f"rm -f '{nas_f}'", timeout=10)
                        if p.returncode == 0:
                            log(f"  NAS 삭제: {base}")
                    elif os.path.isfile(os.path.join(NAS_DIR, f"{jid}.mp4")):
                        p = run_asu(f"rm -f '{os.path.join(NAS_DIR, f'{jid}.mp4')}'", timeout=10)
                        if p.returncode == 0:
                            log(f"  NAS 삭제: {jid}.mp4")
                except Exception as e:
                    log(f"  NAS 삭제 실패: {e}")
                # job 제거
                del JOBS[jid]
                jf = os.path.join(JOBS_DIR, f"{jid}.json")
                if os.path.isfile(jf):
                    os.remove(jf)
                log(f"  삭제: {jid}")
                send_json(self, {"ok": True})
        else:
            send_json(self, {"ok": False, "error": "not found"}, 404)


def main():
    os.makedirs(WEB_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    _restore_jobs()

    # 기존 done job에 size回填 (파일에서 감지)
    for jid, j in list(JOBS.items()):
        if j.get("status") == "done" and not j.get("size") and j.get("src"):
            try:
                j["size"] = os.path.getsize(j["src"])
                _save_job(jid)
            except OSError:
                pass

    # FIFO 큐 워커 시작 (동시 1개)
    threading.Thread(target=queue_worker, daemon=True, name="queue-worker").start()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"H3 웹 서버 v2.1 시작 http://{HOST}:{PORT} (comfy={COMFY}, nas={NAS_DIR})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

