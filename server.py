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

try:
    import websocket  # websocket-client; ComfyUI의 실제 sampler progress 수신용
except ImportError:
    websocket = None
from urllib.parse import urlparse

HOST = os.environ.get("H3_HOST", "0.0.0.0")
PORT = int(os.environ.get("H3_PORT", "8300"))
COMFY = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188")
ASUI = os.environ.get("ASUI", "aski")
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
COMFY_OUT = "/home/aski/minimax-h3/output"
NAS_DIR = "/mnt/comfyui_videos/comfyui/h3_videos"
# CIFS automount 장애 때도 NAS로 직접 보관하는 SSH fallback. 키는 PGX의
# aski 계정 전용 비밀 파일이며 저장소에는 포함하지 않는다.
NAS_SSH_HOST = os.environ.get("H3_NAS_SSH_HOST", "admin@192.168.50.202")
NAS_SSH_KEY = os.environ.get("H3_NAS_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519_qnas"))
NAS_SSH_DIR = os.environ.get("H3_NAS_SSH_DIR", "/share/aski_main/comfyui/h3_videos")
OUT_DIR = os.environ.get("H3_OUT_DIR", os.path.expanduser("~/h3-web/output"))

# MiniMax H3 Eros E3 production profile. Override filenames with env vars when
# the PGX model directory uses a different revision.
H3_UNET = os.environ.get("H3_UNET", "minimax_h3_fl2va_pruned_int8_convrot.safetensors")
H3_CLIP = os.environ.get("H3_CLIP", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
H3_VIDEO_VAE = os.environ.get("H3_VIDEO_VAE", "minimax_h3_video_vae_fp16.safetensors")
H3_AUDIO_VAE = os.environ.get("H3_AUDIO_VAE", "minimax_h3_audio_vae_fp32.safetensors")
H3_LORA = os.environ.get("H3_LORA", "minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors")
REALISM_LORA = os.environ.get("REALISM_LORA", "h3-realism-people-t2v-i2v-r2v.safetensors")
REALISM_LORA_STRENGTH = float(os.environ.get("REALISM_LORA_STRENGTH", "0.8"))

# H3 model: 24fps, 17k+5 frame grid
MAX_SECONDS = 60

# 세그먼트 길이 (초)
SEG_CHOICES = (2, 4, 8)
SEG_SECONDS = 4  # 기본값

# 고정 참조 (고정 이미지): 한번 등록하면 서버가 영구 보관 — 삭제 전까지 자동 유지
REF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".h3-web", "ref")
REF_META = os.path.join(REF_DIR, "meta.json")

# 고정 동영상 참조 (인물 동영상): 추출된 프레임 + 원본 mp4를 영구 보관
REFV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".h3-web", "refv")
REFV_META = os.path.join(REFV_DIR, "meta.json")

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
MAX_PENDING_JOBS = 5  # 실행 중 작업은 제외하고, 대기열만 최대 5개
QUEUE_RESERVATIONS = 0  # 요청 처리 중인 admission slot; 동시 요청 우회 방지
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
    """Build an honest progress payload; never estimate percentage from time/queue.

    ``sampler_pct`` is accepted only when it originated in the matching
    ComfyUI WebSocket event.  Queue data remains informational and missing
    sampler data is intentionally represented as ``pct: null``.
    """
    j = JOBS.get(job_id) or {}
    now = time.time()
    elapsed = round(now - j.get("started", now), 1)
    segments = max(1, j.get("segments") or 1)
    seg_done = min(segments, int(extra.get("seg_done", 0)))
    sampler_pct = extra.get("sampler_pct")
    pct = None
    if sampler_pct is not None:
        # This is *only* the raw matching sampler event: 4/20 -> 20.
        # Do not blend segment count, elapsed time, or queue position into it.
        pct = round(100 * max(0, min(1, float(sampler_pct))))
    if extra.get("completed"):
        pct = 100
    out = {"phase": phase, "elapsed": elapsed, "pct": pct,
           "eta": None, "updated_at": now}
    out.update(extra)
    return out


def apply_comfy_event(job_id, prompt_id, event, seg_done=0, segments=1):
    """Apply one ComfyUI WebSocket event only when it belongs to ``prompt_id``.

    ComfyUI broadcasts events for all clients. Prompt-id equality is the sole
    correlation key, so unrelated work cannot affect an H3 job.
    """
    if not isinstance(event, dict):
        return False
    data = event.get("data") or {}
    if str(data.get("prompt_id") or "") != str(prompt_id):
        return False
    typ = event.get("type")
    if typ == "progress":
        value, maximum = data.get("value"), data.get("max")
        if (not isinstance(value, (int, float)) or not isinstance(maximum, (int, float))
                or maximum <= 0):
            return False
        ratio = max(0.0, min(1.0, value / maximum))
        update_job(job_id, status="running", comfy_status="running",
                   progress=_prog(job_id, "영상 생성 중", seg_done=seg_done,
                                  sampler_pct=ratio, node=data.get("node"),
                                  value=value, max=maximum,
                                  last_progress_at=time.time()))
        return True
    if typ == "executing":
        # node=None signals completion, but history is still authoritative for
        # output discovery.
        update_job(job_id, status="running", comfy_status="running",
                   progress=_prog(job_id, "영상 생성 중", seg_done=seg_done,
                                  node=data.get("node")))
        return True
    if typ == "execution_error":
        raise RuntimeError("ComfyUI 실행 오류: " + json.dumps(data, ensure_ascii=False)[:700])
    return False


def reconcile_comfy_prompt(job_id, prompt_id, history, queue, seg_done=0,
                           segments=1, final=False):
    """Reconcile one prompt's queue/history snapshot without inventing progress.

    Queue membership is lifecycle information only.  It never provides an
    execution percentage, and entries for other prompts are ignored.
    """
    history = history if isinstance(history, dict) else {}
    queue = queue if isinstance(queue, dict) else {}
    record = history.get(prompt_id)
    if isinstance(record, dict):
        status = record.get("status") or {}
        if status.get("status_str") == "error" or not status.get("completed", False):
            return "error"
        if final:
            update_job(job_id, status="done", comfy_status="completed",
                       progress=_prog(job_id, "생성 완료", completed=True,
                                      seg_done=segments))
        return "completed"

    running = {str(row[1]) for row in queue.get("queue_running", [])
               if isinstance(row, (list, tuple)) and len(row) > 1}
    pending = {str(row[1]) for row in queue.get("queue_pending", [])
               if isinstance(row, (list, tuple)) and len(row) > 1}
    if str(prompt_id) in running:
        comfy_status, phase, result = "running", "영상 생성 중", "running"
    elif str(prompt_id) in pending:
        comfy_status, phase, result = "pending", "ComfyUI 대기 중", "pending"
    else:
        return "unknown"
    update_job(job_id, status="running", comfy_status=comfy_status,
               progress=_prog(job_id, phase, seg_done=seg_done,
                              queue_running=len(queue.get("queue_running", [])),
                              queue_pending=len(queue.get("queue_pending", []))))
    return result


def _comfy_ws(client_id):
    """Open a short-lived ComfyUI event socket, or return None if unavailable."""
    if websocket is None:
        return None
    ws_url = re.sub(r"^http", "ws", COMFY, count=1).rstrip("/") + "/ws?clientId=" + client_id
    try:
        ws = websocket.create_connection(ws_url, timeout=2)
        ws.settimeout(1)
        return ws
    except Exception:
        return None


def snap_len(seconds):
    """seconds를 17k+5 프레임 그리드에 스냅 (24fps 기준)."""
    raw = max(124, round(seconds * 24))
    return raw + (5 - (raw % 17)) % 17


def estimate_seconds(total_seconds, seg_seconds, strategy, steps):
    """생성 시간 추정 (초). 6스텝 기준 75초/4초세그먼트."""
    # step당 시간 증감은 완만하게 반영한다. 기존 수식은 2-step에서 음수 예상시간을
    # 만들 수 있었으므로 최소 35%로 하한을 둔다.
    step_factor = max(0.35, 1.0 + 0.12 * (steps - STEPS_DEFAULT))
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


def comfy_upload_video(data: bytes, filename: str):
    """바이너리를 ComfyUI 입력 디렉터리에 저장 후 LoadVideo/LoadAnimatedPNG용 이름 반환.
    comfy_upload_image와 동일하게 직접 쓰기 → /upload/image 폴백 구조."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename))
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
        dst = os.path.join(target_dir, safe_name)
        with open(dst, "wb") as f:
            f.write(data)
        log(f"  동영상 업로드(직접): {dst}")
        return safe_name
    except Exception as e:
        log(f"  직접 쓰기 실패 ({e}) -> /upload 폴백")
    boundary = "----h3web" + uuid.uuid4().hex
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
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
    if not out.get("name"):
        raise RuntimeError(f"ComfyUI 업로드 실패: {out}")
    log(f"  동영상 업로드(/upload): {out.get('name')}")
    return out["name"]


def extract_ref_video_frame(video_bytes: bytes, ts_offset: float = 0.5) -> bytes:
    """동영상 mp4에서 ts_offset 초 지점의 인물 프레임을 추출.
    ffmpeg -ss → PNG 바이트 반환. 실패 시 RuntimeError."""
    tmp_dir = os.path.join(OUT_DIR, "refs")
    os.makedirs(tmp_dir, exist_ok=True)
    src = os.path.join(tmp_dir, f"refv_in_{uuid.uuid4().hex[:8]}.mp4")
    out_png = os.path.join(tmp_dir, f"refv_frame_{uuid.uuid4().hex[:8]}.png")
    ts = max(0.0, min(float(ts_offset), 4.0))
    try:
        with open(src, "wb") as f:
            f.write(video_bytes)
        cmd = ["ffmpeg", "-y", "-ss", str(ts), "-i", src,
               "-frames:v", "1", "-q:v", "2", out_png]
        p = subprocess.run(cmd, capture_output=True, timeout=90)
        if p.returncode != 0 or not os.path.isfile(out_png):
            raise RuntimeError(f"프레임 추출 실패: {p.stderr.decode(errors='replace')[:300]}")
        with open(out_png, "rb") as f:
            data = f.read()
        if not data:
            raise RuntimeError("추출된 프레임이 비어 있음")
        log(f"  고정 동영상 참조: {ts:.1f}s 지점 프레임 추출 ({len(data)}B)")
        return data
    finally:
        for pth in (src, out_png):
            try:
                if os.path.isfile(pth):
                    os.remove(pth)
            except Exception:
                pass


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


def build_workflow(text, negative, width, height, length, steps, seed, image_name=None, prefix="h3", video_name=None, realism_lora=False):
    """T2V/I2V 워크플로우 — H3 전용. Wan 폴백 제거 (사용자 지정).
    video_name: LoadVideo 노드를 통한 참조 동영상 (인물 동영상 모드)"""
    base_negative = "text, subtitles, captions, watermark, logo, script overlay, on-screen text, UI elements"
    if negative:
        full_prompt = f"{text} (do NOT include: {base_negative}, {negative})"
    else:
        full_prompt = f"{text} (do NOT include: {base_negative})"

    # 기본 Turbo 뒤에, 사용자가 토글을 켠 경우에만 리얼리즘 LoRA를 누적한다.
    lora_dirs = ["/home/aski/ComfyUI/models/loras",
                 "/home/aski/ComfyUI/models/loras/split_files/loras"]
    lora_avail = any(os.path.exists(os.path.join(d, H3_LORA)) for d in lora_dirs)
    realism_lora = realism_lora is True  # 문자열 "false" 등 truthy 값은 허용하지 않음
    realism_avail = realism_lora and any(
        os.path.exists(os.path.join(d, REALISM_LORA)) for d in lora_dirs
    )
    model_ref = ["1b", 0] if realism_avail else (["1a", 0] if lora_avail else ["1", 0])
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
        wf["1a"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": H3_LORA, "strength_model": 1.0}}
    if realism_avail:
        wf["1b"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1a", 0] if lora_avail else ["1", 0],
            "lora_name": REALISM_LORA,
            "strength_model": REALISM_LORA_STRENGTH,
        }}
    if image_name:
        wf["15"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        wf["5"]["inputs"]["first_frame"] = ["15", 0]
    if video_name:
        # LoadVideo → first_frame 입력 (인물 동영상 참조: 첫 프레임 기준)
        wf["16"] = {"class_type": "LoadVideo", "inputs": {"video": video_name, "force_rate": 24}}
        wf["5"]["inputs"]["first_frame"] = ["16", 0]
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
    """NAS에 완전 검증 저장 후 경로를 반환한다. 실패 시 None이고 로컬은 유지된다."""
    dst = os.path.join(NAS_DIR, os.path.basename(src_path))
    try:
        if not os.path.isdir(NAS_DIR):
            try:
                os.makedirs(NAS_DIR, exist_ok=True)
            except OSError as e:
                # CIFS automount 권한/장애는 SSH archive fallback으로 계속 진행한다.
                log(f"  CIFS NAS 디렉터리 준비 실패 ({e}) -> SSH archive 폴백")
        # 직접 쓰기 (NAS_DIR은 aski 소유 CIFS — uid 1000/1000)
        # CIFS + seccomp: chmod/chown이 EPERM → os.open(mode=0o644)로 생성 시점에 권한 지정
        try:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            with os.fdopen(fd, 'wb') as f:
                with open(src_path, 'rb') as s:
                    f.write(s.read())
            if os.path.getsize(dst) != os.path.getsize(src_path):
                raise RuntimeError("NAS 복사 크기 불일치")
            log(f"  NAS 저장: {dst}")
            return dst
        except Exception as e:
            log(f"  직접 NAS 쓰기 실패 ({e}) -> run_asu 폴백")
        # 직접 쓰기 실패 (CIFS seccomp 등) → run_asu 폴백
        try:
            p = run_asu(f"cp '{src_path}' '{dst}' && chmod 644 '{dst}'", timeout=60)
        except Exception as e:
            p = None
            log(f"  run_asu NAS 저장 실패 ({e}) -> SSH archive 폴백")
        if p and p.returncode == 0 and os.path.isfile(dst):
            if os.path.getsize(dst) == os.path.getsize(src_path):
                log(f"  NAS 저장(run_asu): {dst}")
                return dst
        # run_asu도 실패 (CIFS에서 로컬 파일 stat 불가) → 로컬에서 읽고 NAS에 쓰기
        try:
            with open(src_path, 'rb') as s:
                data = s.read()
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            if os.path.getsize(dst) != os.path.getsize(src_path):
                raise RuntimeError("NAS 복사 크기 불일치")
            log(f"  NAS 저장(로컬읽기→직접쓰기): {dst}")
            return dst
        except Exception as e2:
            log(f"  CIFS NAS 저장 실패: {e2}")
        # CIFS가 실패한 경우 NAS SSH archive를 쓰고 SHA256을 대조한다.
        if os.path.isfile(NAS_SSH_KEY):
            remote = f"{NAS_SSH_DIR.rstrip('/')}/{os.path.basename(src_path)}"
            ssh = ["ssh", "-i", NAS_SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", NAS_SSH_HOST]
            quoted_dir = NAS_SSH_DIR.replace("'", "'\\''")
            quoted_remote = remote.replace("'", "'\\''")
            mkdir = subprocess.run(ssh + [f"/bin/sh -c 'mkdir -p \\\"{quoted_dir}\\\"'"], capture_output=True, text=True, timeout=30)
            if mkdir.returncode == 0:
                put = subprocess.run(["scp", "-i", NAS_SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", src_path, f"{NAS_SSH_HOST}:{remote}"], capture_output=True, text=True, timeout=300)
                local_hash = subprocess.check_output(["sha256sum", src_path], text=True).split()[0]
                verify = subprocess.run(ssh + [f"/bin/sh -c 'sha256sum \\\"{quoted_remote}\\\"'"], capture_output=True, text=True, timeout=45)
                if put.returncode == 0 and verify.returncode == 0 and verify.stdout.split() and verify.stdout.split()[0] == local_hash:
                    log(f"  NAS 저장(SSH+SHA256): {remote}")
                    return remote
                log(f"  NAS SSH 저장/검증 실패: {put.stderr.strip()[:160] or verify.stderr.strip()[:160]}")
    except Exception as e:
        log(f"  NAS 저장 실패 (로컬 유지): {e}")
    return None


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
                                   image_name=cfg.get("image_name", ""),
                                   video_name=cfg.get("video_name", ""), prefix=prefix,
                                   realism_lora=cfg.get("realism_lora", False))
            cid = str(uuid.uuid4())
            # Subscribe before queueing so an immediately-started prompt cannot
            # emit its first real progress event before this client is listening.
            ws = _comfy_ws(cid)
            queued = comfy_post("/prompt", {"prompt": workflow, "client_id": cid})
            if "error" in queued:
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                err_msg = json.dumps(queued, ensure_ascii=False)
                raise RuntimeError(err_msg[:600])
            pid = queued["prompt_id"]
            update_job(job_id, status="running", comfy_prompt_id=pid, segment_started=time.time(),
                       comfy_status="pending",
                       progress=_prog(job_id, f"세그먼트 {i+1}/{segments} ComfyUI 대기 중" if segments > 1 else "ComfyUI 대기 중",
                                      seg_done=i, queue_pending=1))
            log(f"  seg {i+1}/{segments} queued pid={pid} (H3)")

            # A websocket supplies sampler measurements. Queue/history only
            # establish this prompt's lifecycle; neither can manufacture a pct.
            try:
                while True:
                    if ws:
                        try:
                            raw = ws.recv()
                            if isinstance(raw, str):
                                apply_comfy_event(job_id, pid, json.loads(raw), i, segments)
                        except Exception as e:
                            # A read timeout just means no event arrived yet;
                            # it is not a connection failure.
                            if websocket and isinstance(e, websocket.WebSocketTimeoutException):
                                pass
                            else:
                                # Socket loss is fail-closed, not a fabricated
                                # continuation. History polling below may recover.
                                try:
                                    ws.close()
                                except Exception:
                                    pass
                                ws = None
                                update_job(job_id, comfy_status="unavailable",
                                           progress=_prog(job_id, "ComfyUI 진행 정보 수신 대기", seg_done=i,
                                                          unavailable=True))
                    try:
                        h = comfy_get(f"/history/{pid}", timeout=30)
                    except Exception:
                        update_job(job_id, comfy_status="unavailable",
                                   progress=_prog(job_id, "ComfyUI 상태 확인 불가", seg_done=i,
                                                  unavailable=True))
                        time.sleep(2)
                        continue
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
                    # Without the event stream a queue position cannot be
                    # represented as generation progress. Remain fail-closed
                    # even if the prompt is visible in /queue.
                    if ws is None:
                        update_job(job_id, comfy_status="unavailable",
                                   progress=_prog(job_id, "ComfyUI 진행 정보 수신 대기", seg_done=i,
                                                  unavailable=True))
                        time.sleep(2)
                        continue
                    try:
                        q = comfy_get("/queue", timeout=30)
                    except Exception:
                        update_job(job_id, comfy_status="unavailable",
                                   progress=_prog(job_id, "ComfyUI 상태 확인 불가", seg_done=i,
                                                  unavailable=True))
                        time.sleep(2)
                        continue
                    reconcile_comfy_prompt(job_id, pid, {}, q, i, segments)
                    time.sleep(2)
            finally:
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass

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
        nas_path = _copy_to_nas(final_local)
        fsize = os.path.getsize(final_local)
        # NAS archive가 크기 또는 SHA256으로 검증됐을 때에만 PGX 원본/세그먼트를
        # 제거한다. archive 실패 시에는 복구를 위해 로컬을 유지한다.
        if nas_path:
            shutil.rmtree(dst_dir)
            final_src = nas_path
        else:
            final_src = final_local
        update_job(job_id,
            status="done", file=os.path.basename(final_local), src=final_src,
            progress=_prog(job_id, "생성 완료", completed=True, eta=0, seg_done=segments),
            elapsed=round(time.time() - JOBS[job_id].get("started", time.time()), 1),
            segments=segments, total_seconds=total_seconds,
            size=fsize,
            nas_saved=bool(nas_path),
            storage="nas" if nas_path else "pgx-local-recovery",
        )
        log(f"job {job_id} done → {final_src} ({segments}seg, {total_seconds}s, 24fps, {fsize//1048576}MB, nas={'OK' if nas_path else 'FAIL'})")
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


def _refv_path():
    return os.path.join(REFV_DIR, "ref_frame.png")


def _refv_video_path():
    return os.path.join(REFV_DIR, "ref_video.mp4")


def _load_refv():
    """고정 동영상 참조 meta 반환. 없으면 None."""
    meta_f = REFV_META
    if not os.path.isfile(meta_f) or not os.path.isfile(_refv_path()):
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
        "duration_s": m.get("duration_s", 0),
        "ts_offset": m.get("ts_offset", 0),
    }


def _save_refv(video_bytes: bytes, frame_bytes: bytes, w: int, h: int,
               name: str, duration_s: float, ts_offset: float):
    """고정 동영상 참조: 원본 mp4 + 추출 프레임을 영구 저장."""
    os.makedirs(REFV_DIR, exist_ok=True)
    with open(_refv_path(), "wb") as f:
        f.write(frame_bytes)
    with open(_refv_video_path(), "wb") as f:
        f.write(video_bytes)
    meta = {"name": name, "w": w, "h": h, "size": len(video_bytes),
            "ts": time.time(), "duration_s": duration_s, "ts_offset": ts_offset,
            "frame_size": len(frame_bytes)}
    with open(REFV_META, "w") as f:
        json.dump(meta, f, ensure_ascii=False)
    log(f"고정 동영상 참조 저장: {name} ({w}x{h}, {duration_s:.1f}s, {ts_offset:.1f}s 지점)")
    return meta


def _delete_refv():
    for pth in (_refv_path(), _refv_video_path(), REFV_META):
        try:
            if os.path.isfile(pth):
                os.remove(pth)
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


def parse_byte_range(header, size):
    """Return an inclusive single HTTP byte range or None for a full response."""
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or size <= 0:
        raise ValueError("invalid range")
    first, last = match.groups()
    if not first and not last:
        raise ValueError("invalid range")
    if first:
        start = int(first)
        end = int(last) if last else size - 1
    else:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("invalid range")
        start, end = max(0, size - suffix), size - 1
    if start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return start, min(end, size - 1)


def valid_job_id(jid):
    """Reject path-like identifiers before using them in an output path."""
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", jid or ""))


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

    def do_HEAD(self):
        self.do_GET()

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

    def _serve_video(self, src, jid, disposition):
        """Stream video with byte-range support for playback, seeking and resume."""
        remote = src.startswith(NAS_SSH_DIR.rstrip("/") + "/")
        if remote:
            if not os.path.isfile(NAS_SSH_KEY):
                send_json(self, {"ok": False, "error": "NAS archive 키가 없습니다"}, 503)
                return
            meta = subprocess.run(["ssh", "-i", NAS_SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", NAS_SSH_HOST,
                                   "stat", "-c", "%s", "--", src], capture_output=True, text=True, timeout=30)
            if meta.returncode != 0 or not meta.stdout.strip().isdigit():
                send_json(self, {"ok": False, "error": "NAS archive 영상을 찾을 수 없습니다"}, 404)
                return
            size = int(meta.stdout.strip())
        else:
            size = os.path.getsize(src)
        try:
            byte_range = parse_byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        if byte_range:
            start, end = byte_range
            length, code = end - start + 1, 206
        else:
            start, length, code = 0, size, 200
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f'{disposition}; filename="{jid}.mp4"')
        self.send_header("Accept-Ranges", "bytes")
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{start + length - 1}/{size}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            return
        if remote:
            # NAS에 복사본을 만들지 않고 dd로 필요한 Byte Range만 SSH stream한다.
            proc = subprocess.Popen(["ssh", "-i", NAS_SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", NAS_SSH_HOST,
                                     "dd", f"if={src}", "iflag=skip_bytes", f"skip={start}", f"count={length}", "status=none"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert proc.stdout is not None and proc.stderr is not None
            try:
                while True:
                    chunk = proc.stdout.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                if proc.wait(timeout=60) != 0:
                    log(f"NAS SSH video stream 실패: {proc.stderr.read().decode(errors='replace')[:160]}")
            finally:
                if proc.poll() is None:
                    proc.kill()
        else:
            with open(src, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining:
                    chunk = f.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

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
        elif p.startswith("/api/refv/frame"):
            # GET /api/refv/frame — 고정 동영상 참조에서 추출된 프레임 PNG (없으면 404)
            if not _load_refv():
                send_json(self, {"ok": False, "refv": None}, 404)
                return
            fsize = os.path.getsize(_refv_path())
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(fsize))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(_refv_path(), "rb") as f:
                self.wfile.write(f.read())
        elif p.startswith("/api/refv/status"):
            # GET /api/refv/status — 고정 동영상 참조 메타데이터
            send_json(self, {"ok": True, "refv": _load_refv()})
        elif p.startswith("/api/refv"):
            # GET /api/refv — 고정 동영상 원본 mp4 (없으면 404)
            if not os.path.isfile(_refv_video_path()):
                send_json(self, {"ok": False, "refv": None}, 404)
                return
            fsize = os.path.getsize(_refv_video_path())
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(fsize))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(_refv_video_path(), "rb") as f:
                shutil.copyfileobj(f, self.wfile, length=1024 * 256)
        elif p.startswith("/api/download/") or p.startswith("/api/view/"):
            jid = p.split("/")[3]
            if not valid_job_id(jid):
                send_json(self, {"ok": False, "error": "invalid job id"}, 400)
                return
            # 1) JOBS에서 src 확인
            with LOCK:
                j = JOBS.get(jid)
            src = j.get("src") if j and j.get("status") == "done" else None
            is_remote_archive = bool(src and src.startswith(NAS_SSH_DIR.rstrip("/") + "/"))
            # 2) 폴백: JOBS에 없어도 파일 기반 탐색
            if not src or (not is_remote_archive and not os.path.exists(src)):
                src = os.path.join(OUT_DIR, jid, f"{jid}.mp4")
                is_remote_archive = False
            if not is_remote_archive and not os.path.exists(src):
                send_json(self, {"ok": False, "error": f"다운로드 가능 영상 없음 ({jid})"}, 404)
                return
            self._serve_video(src, jid, "attachment" if p.startswith("/api/download/") else "inline")
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
        if p == "/api/refv/status":
            send_json(self, {"ok": True, "refv": _load_refv()})
            return
        if p == "/api/refv/delete":
            _delete_refv()
            log("고정 동영상 참조 삭제")
            send_json(self, {"ok": True})
            return
        if p == "/api/refv/set":
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
            fname = "refv.mp4"
            data = None
            if "multipart/form-data" in ctype:
                m = re.search(r"boundary=(\"?)([^\\s;\"']+)\\1", ctype)
                boundary = m.group(2).encode() if m else b"----h3web"
                parts = raw.split(b"--" + boundary)
                for seg in parts:
                    seg = seg.lstrip(b"\r\n")
                    if not seg:
                        continue
                    fm = re.search(rb'name="file";\s*filename="([^"]+)"', seg)
                    hm = re.search(rb'name="name";\s*content="([^"]*)"', seg)
                    if fm:
                        name = hm.group(1).decode() if hm else ""
                        if name == "file" and b"\r\n\r\n" in seg:
                            fname = fm.group(1).decode() or "refv.mp4"
                            data = seg.split(b"\r\n\r\n", 1)[1]
                            break
                if data is None:
                    send_json(self, {"ok": False, "error": "file 필드 없음"}, 400)
                    return
            else:
                data = raw
                disp = self.headers.get("Content-Disposition", "")
                fm2 = re.search(r'filename="?([^";]+)"?', disp)
                if fm2:
                    fname = fm2.group(1)
            ext = os.path.splitext(fname)[1].lower()
            if ext not in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
                send_json(self, {"ok": False, "error": "동영상 형식: mp4/mov/webm/mkv/avi"}, 400)
                return
            # 동영상 메타: ffmpeg로 길이/해상도 확인
            probe = {"w": 0, "h": 0, "duration_s": 0.0}
            tmp_probe = os.path.join(OUT_DIR, f"probe_{uuid.uuid4().hex[:8]}.mp4")
            try:
                import json as _json
                os.makedirs(OUT_DIR, exist_ok=True)
                with open(tmp_probe, "wb") as f:
                    f.write(data)
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height",
                     "-show_entries", "format=duration",
                     "-of", "json", tmp_probe],
                    capture_output=True, timeout=15)
                if out.returncode == 0:
                    pj = _json.loads(out.stdout.decode())
                    st = (pj.get("streams") or [{}])[0]
                    probe["w"] = int(st.get("width") or 0)
                    probe["h"] = int(st.get("height") or 0)
                    probe["duration_s"] = round(float(pj.get("format", {}).get("duration") or 0), 2)
            except Exception as e:
                log(f"  ffprobe 실패 ({e}) — 메타 없는 상태로 저장")
            finally:
                try:
                    if os.path.isfile(tmp_probe):
                        os.remove(tmp_probe)
                except Exception:
                    pass
            if probe["duration_s"] == 0.0:
                send_json(self, {"ok": False, "error": "동영상 길이를 읽을 수 없습니다"}, 400)
                return
            if probe["w"] == 0 or probe["h"] == 0:
                send_json(self, {"ok": False, "error": "해상도를 읽을 수 없습니다"}, 400)
                return
            # 프레임 추출 (0.5s 지점 — 인물 샷 기준, 4s 이내로 클램프)
            ts_offset = 0.5
            try:
                frame_data = extract_ref_video_frame(data, ts_offset)
            except RuntimeError as e:
                log(f"  프레임 추출 실패: {e}")
                send_json(self, {"ok": False, "error": f"프레임 추출 실패: {str(e)[:100]}"}, 400)
                return
            meta = _save_refv(data, frame_data, probe["w"], probe["h"],
                              os.path.basename(fname), probe["duration_s"], ts_offset)
            log(f"고정 동영상 참조 등록: {fname} ({probe['w']}x{probe['h']}, {probe['duration_s']:.1f}s)")
            send_json(self, {"ok": True, "refv": meta})
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
            video_name = ""
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
                    # 고정 동영상 참조 (인물 동영상) — ref_mode=video 또는 자동
                    refv = _load_refv()
                    if not refv or not os.path.isfile(_refv_path()):
                        send_json(self, {"ok": False, "error": "이미지를 먼저 업로드하거나 고정 참조(이미지/동영상)를 선택해 주세요 (I2V)"}, 400)
                        return
                    try:
                        with open(_refv_path(), "rb") as f:
                            refv_bytes = f.read()
                        video_name = comfy_upload_video(refv_bytes, f"h3web_refv_{uuid.uuid4().hex[:8]}.mp4")
                        log(f"  고정 동영상 참조 자동 사용: {refv.get('name')} ({refv.get('w')}x{refv.get('h')}, {refv.get('duration_s')}s) → {video_name}")
                    except Exception as e:
                        send_json(self, {"ok": False, "error": f"고정 동영상 참조 전송 실패: {e}"}, 500)
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
            # JSON true만 허용한다. 문자열 "false" 등으로 우회해 켜지지 않는다.
            realism_lora = data.get("realism_lora") is True
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
                "video_name": video_name,
                "realism_lora": realism_lora,
            }
            # admission slot을 먼저 예약한다. 따라서 동시에 여러 HTTP 요청이 와도
            # 대기열(예약 포함) 6번째는 이 시점에서 원자적으로 거절된다.
            global QUEUE_RESERVATIONS
            with QUEUE_LOCK:
                pending_total = len(QUEUE) + QUEUE_RESERVATIONS
                if pending_total >= MAX_PENDING_JOBS:
                    send_json(self, {"ok": False, "error": "대기열이 가득 찼습니다 (최대 5개). 실행 중인 작업이 끝난 뒤 다시 시도해 주세요.",
                                     "code": "QUEUE_FULL", "queue_pending": pending_total, "queue_limit": MAX_PENDING_JOBS}, 429)
                    return
                QUEUE_RESERVATIONS += 1
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
            # 서버에서 원자적으로 제한한다. 프런트엔드 체크를 우회해도 6번째
            # 대기 요청은 절대 enqueue되지 않는다.
            with QUEUE_LOCK:
                QUEUE_RESERVATIONS -= 1
                QUEUE.append(jid)
            log(f"new job {jid} [{mode}]: {prompt[:50]}... {cfg['width']}x{cfg['height']} "
                f"{seconds}s [{strategy}] {segments}seg steps={cfg['steps']}"
                + (f" img={image_name}" if image_name else "")
                + (f" vid={video_name}" if video_name else "")
                + (" realism_lora=on" if realism_lora else " realism_lora=off"))
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
        elif p.startswith("/api/delete-error/"):
            # 정상 완료 영상은 어떤 경우에도 이 API로 지우지 않는다. 오류/중단/취소
            # 작업의 job 전용 임시 디렉터리 안에서만, 실제로 깨진 mp4만 정리한다.
            jid = p.split("/")[3]
            with LOCK:
                j = JOBS.get(jid)
                if not j:
                    send_json(self, {"ok": False, "error": "job 없음"}, 404)
                    return
                if j.get("status") not in ("error", "interrupted", "cancelled"):
                    send_json(self, {"ok": False, "error": "오류/중단 작업만 정리할 수 있습니다. 정상 완료 영상은 보호됩니다."}, 400)
                    return
                job_dir = os.path.realpath(os.path.join(OUT_DIR, jid))
                root = os.path.realpath(OUT_DIR) + os.sep
                if not job_dir.startswith(root):
                    send_json(self, {"ok": False, "error": "안전하지 않은 출력 경로"}, 400)
                    return
            deleted, preserved = [], []
            if os.path.isdir(job_dir):
                for base, _, names in os.walk(job_dir):
                    for name in names:
                        path = os.path.realpath(os.path.join(base, name))
                        if not path.startswith(job_dir + os.sep):
                            continue
                        if not name.lower().endswith(".mp4"):
                            try:
                                os.remove(path)
                                deleted.append(os.path.basename(path))
                            except OSError:
                                pass
                            continue
                        # 최소 크기, 컨테이너 검사, 전체 디코드 중 하나라도 실패해야 삭제한다.
                        bad = not os.path.isfile(path) or os.path.getsize(path) < 1024 * 1024
                        if not bad:
                            probe = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", path],
                                                   capture_output=True, text=True, timeout=20)
                            bad = probe.returncode != 0
                        if not bad:
                            decode = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                                                    capture_output=True, text=True, timeout=180)
                            bad = decode.returncode != 0
                        if bad:
                            try:
                                os.remove(path)
                                deleted.append(os.path.basename(path))
                            except OSError as e:
                                preserved.append(f"{os.path.basename(path)} (삭제 실패: {e})")
                        else:
                            preserved.append(os.path.basename(path))
                # 빈 디렉터리만 제거. 유효 mp4는 보존한다.
                try:
                    if not any(os.scandir(job_dir)):
                        os.rmdir(job_dir)
                except OSError:
                    pass
            if not preserved:
                with LOCK:
                    JOBS.pop(jid, None)
                jf = _job_file(jid)
                if os.path.isfile(jf):
                    os.remove(jf)
            log(f"  오류 출력 정리: {jid}, 삭제 {len(deleted)}, 보존 {len(preserved)}")
            send_json(self, {"ok": True, "deleted": deleted, "preserved": preserved,
                             "message": "깨진 출력만 정리했습니다" if not preserved else "유효 MP4는 보호되어 삭제하지 않았습니다"})
        elif p.startswith("/api/delete/"):
            jid = p.split("/")[3]
            if not valid_job_id(jid):
                send_json(self, {"ok": False, "error": "invalid job id"}, 400)
                return
            with LOCK:
                j = JOBS.get(jid)
                if not j:
                    send_json(self, {"ok": False, "error": "job 없음"}, 404)
                    return
                if j.get("status") != "done" or not j.get("nas_saved"):
                    send_json(self, {"ok": False, "error": "NAS에 검증 저장된 완료 영상만 삭제할 수 있습니다"}, 400)
                    return
                name = os.path.basename(str(j.get("file", "")))
            try:
                archive = os.path.realpath(os.path.join(NAS_DIR, name))
                root = os.path.realpath(NAS_DIR) + os.sep
                if not name or not archive.startswith(root):
                    raise RuntimeError("안전하지 않은 NAS archive 경로")
                if os.path.isfile(archive):
                    os.remove(archive)
                else:
                    remote = f"{NAS_SSH_DIR.rstrip('/')}/{name}"
                    if not os.path.isfile(NAS_SSH_KEY):
                        raise RuntimeError("NAS archive 키가 없습니다")
                    quoted = remote.replace("'", "'\\''")
                    pdel = subprocess.run(["ssh", "-i", NAS_SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", NAS_SSH_HOST,
                                           f"/bin/sh -c 'rm -f \\\"{quoted}\\\"'"], capture_output=True, text=True, timeout=45)
                    if pdel.returncode != 0:
                        raise RuntimeError(pdel.stderr.strip()[:200] or "NAS SSH 삭제 실패")
                with LOCK:
                    JOBS.pop(jid, None)
                jf = _job_file(jid)
                if os.path.isfile(jf):
                    os.remove(jf)
                send_json(self, {"ok": True, "message": "NAS archive 영상과 작업 기록을 삭제했습니다"})
            except Exception as e:
                send_json(self, {"ok": False, "error": f"NAS 삭제 실패: {e}"}, 500)
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

