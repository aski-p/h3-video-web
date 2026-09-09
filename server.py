#!/usr/bin/env python3
"""MiniMax H3 영상 생성/다운로드 페이지 서버 v2.

- 24fps 출력 (H.264 고품질)
- 최대 60초 (세그먼트 분할 + ffconcat 스티치 / 연속 단일 생성 선택 가능)
- 생성 시간 추정
- NAS 저장 (원본 보존) + NAS Range 스트리밍
- Negative prompt 지원 (한방에 prompt에 병합)
- ComfyUI 자동 기동
- 샘플링 스텝 조절 가능 (기본 6스텝)
- 생성 방식: 연속 단일 생성 / 세그먼트 분할 선택
"""
import json
import base64
import copy
import hashlib
import hmac
import math
import os
import re
import time
import shutil
import shlex
import threading
import socket
import subprocess
import urllib.request
import urllib.error
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any, TypeGuard


try:
    import websocket  # websocket-client; ComfyUI의 실제 sampler progress 수신용
except ImportError:
    websocket = None
from urllib.parse import urlparse

HOST = os.environ.get("H3_HOST", "0.0.0.0")
PORT = int(os.environ.get("H3_PORT") or os.environ.get("PORT") or "8300")
ORIGIN_HEADER = "X-H3-Origin-Token"
ORIGIN_SECRET = os.environ.get("H3_ORIGIN_SECRET", "")
COMFY = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188")
COMFY_WS_RETRY_SECONDS = 5.0
COMFY_PROMPT_MISSING_GRACE_SECONDS = float(
    os.environ.get("COMFY_PROMPT_MISSING_GRACE_SECONDS", "60")
)
ASUI = os.environ.get("ASUI", "aski")
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
COMFY_OUT = "/home/aski/minimax-h3/output"
NAS_DIR = "/mnt/comfyui_videos/comfyui/h3_videos"
# CIFS automount 장애 때도 NAS로 직접 보관하는 SSH fallback. 키는 PGX의
# aski 계정 전용 비밀 파일이며 저장소에는 포함하지 않는다.
NAS_SSH_HOST = os.environ.get("H3_NAS_SSH_HOST", "admin@192.168.50.202")
NAS_SSH_KEY = os.environ.get("H3_NAS_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519_qnas"))
NAS_SSH_DIR = os.environ.get("H3_NAS_SSH_DIR", "/share/aski_main/comfyui/h3_videos")
# SSH archive는 QNAP BusyBox의 byte-flag 비호환성을 피하기 위해 고정 블록을
# 읽고, 웹 서버가 정확한 HTTP Range 창만 내보낸다.
REMOTE_RANGE_BLOCK_BYTES = 64 * 1024
OUT_DIR = os.environ.get("H3_OUT_DIR", os.path.join(NAS_DIR, ".h3-web", "work"))

# MiniMax H3 Eros E3 production profile. Override filenames with env vars when
# the PGX model directory uses a different revision.
H3_UNET = os.environ.get("H3_UNET", "minimax_h3_fl2va_pruned_int8_convrot.safetensors")
H3_CLIP = os.environ.get("H3_CLIP", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
H3_VIDEO_VAE = os.environ.get("H3_VIDEO_VAE", "minimax_h3_video_vae_fp16.safetensors")
H3_AUDIO_VAE = os.environ.get("H3_AUDIO_VAE", "minimax_h3_audio_vae_fp32.safetensors")
H3_LORA = os.environ.get("H3_LORA", "minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors")
REALISM_LORA = os.environ.get("REALISM_LORA", "h3-realism-people-t2v-i2v-r2v.safetensors")
REALISM_LORA_STRENGTH = float(os.environ.get("REALISM_LORA_STRENGTH", "1.0"))

# H3 model: 24fps, 17k+5 frame grid
MAX_SECONDS = 60
# H3의 ImageToVideo keyframe은 시작 구도에는 강하지만, 장시간 인물 정체성을
# 별도 reference conditioning으로 보장하지는 않는다. 사진 I2V는 H3의
# ReferenceToVideo 경로와 모델 검증 범위 안에서만 받는다.
I2V_REFERENCE_MAX_SECONDS = 15
REFERENCE_TO_VIDEO_INSTRUCTION = (
    "<Picture 1> is the exact visual reference. Preserve the main subject's "
    "identity, facial features, hairstyle, clothing, and overall appearance "
    "from <Picture 1> unless the user's prompt explicitly requests a change."
)


def normalize_generation_seconds(value, mode):
    """Validate duration before accepting or snapshotting a generation request.

    T2V retains the historical 60-second clamp. Reference I2V is rejected
    rather than silently truncated above the H3 model's documented 15-second
    trained range, so the request and the delivered result always agree.
    """
    try:
        seconds = float(5 if value is None else value)
    except (TypeError, ValueError):
        raise ValueError("생성 길이는 숫자로 입력해 주세요")
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds <= 0:
        raise ValueError("생성 길이는 0보다 큰 유한한 숫자여야 합니다")
    if mode == "i2v" and seconds > I2V_REFERENCE_MAX_SECONDS:
        raise ValueError(
            f"사진/동영상 참조 I2V는 H3 안정 구간인 {I2V_REFERENCE_MAX_SECONDS}초 이하로 생성해 주세요"
        )
    return min(seconds, MAX_SECONDS)

# 세그먼트 길이 (초)
SEG_CHOICES = (2, 4, 8)
SEG_SECONDS = 4  # 기본값

# Fixed references are media assets, so they live beside completed media on NAS.
NAS_STATE_DIR = os.environ.get("H3_NAS_STATE_DIR", os.path.join(NAS_DIR, ".h3-web"))
REF_DIR = os.path.join(NAS_STATE_DIR, "ref")
REF_META = os.path.join(REF_DIR, "meta.json")

# Fixed video references: both MP4 and extracted frame stay on private NAS storage.
REFV_DIR = os.path.join(NAS_STATE_DIR, "refv")
REFV_META = os.path.join(REFV_DIR, "meta.json")


# 생성 방식
STRATEGY_CHOICES = ("single", "split")
STRATEGY_SINGLE = "single"  # 연속 단일 생성 (장면 연속성 우선)
STRATEGY_SPLIT = "split"   # 세그먼트 분할 (정확한 길이 우선)

# 샘플링 스텝
STEPS_MIN, STEPS_MAX, STEPS_DEFAULT = 2, 20, 6

# 예상 시간 계수 (초/4초세그먼트, 6스텝 기준)
EST_BASE_SECONDS = 75
EST_STEP_COEF = 8.0  # 스텝당 추가 (6스텝 대비)

JOBS = {}
LOCK = threading.Lock()
JOBS_DIR = os.path.join(os.path.expanduser("~"), "h3-web", "jobs")
QUEUE = []            # FIFO: 대기 중인 job_id
MAX_PENDING_JOBS = 5  # 실행 중 작업은 제외하고, 대기열만 최대 5개
QUEUE_RESERVATIONS = {"pgx": 0, "rtx5080": 0}  # worker별 admission slot
QUEUE_LOCK = threading.Lock()
ACTIVE = [None]       # 실행 중인 PGX job_id (동시 1개)

# Optional Windows RTX 5080 outbound worker.  The worker never exposes
# ComfyUI/SSH inbound; it polls the public coordinator and is eligible only
# while server-observed heartbeats prove that both ComfyUI and the pinned model
# are ready.
WORKER_HEADER = "X-H3-Worker-Token"
WORKER_EXECUTION_HEADER = "X-H3-Execution-Id"
WORKER_LEASE_HEADER = "X-H3-Lease-Token"
WORKER_SECRET = os.environ.get("H3_WORKER_TOKEN", "")
WORKER_UPLOAD_CHUNK_MAX = 2 * 1024 * 1024
REMOTE_UPLOAD_LOCK = threading.Lock()
JOB_SAVE_LOCK = threading.Lock()
RTX5080_WORKER_ID = "desktop-rtx5080"
RTX5080_MODEL_PROFILE = "minimax-h3-pgx-exact-v1"
WORKER_HEARTBEAT_TTL_SECONDS = 15.0
RTX5080_LEASE_SECONDS = 60.0
RTX5080_MAX_ATTEMPTS = 2
WORKERS = {}


def rtx5080_worker_status(now=None):
    now = time.time() if now is None else float(now)
    with LOCK:
        record = dict(WORKERS.get(RTX5080_WORKER_ID) or {})
    last_seen = record.get("last_seen")
    age = None if last_seen is None else max(0.0, now - float(last_seen))
    online = age is not None and age <= WORKER_HEARTBEAT_TTL_SECONDS
    eligible = bool(
        online
        and record.get("gpu") == "NVIDIA GeForce RTX 5080"
        and int(record.get("vram_mib") or 0) >= 15000
        and record.get("comfy_up") is True
        and record.get("model_ready") is True
        and record.get("generation_verified") is True
        and record.get("model_profile") == RTX5080_MODEL_PROFILE
        and {"t2v", "i2v"}.issubset(set(record.get("modes") or ()))
    )
    return {
        "id": RTX5080_WORKER_ID,
        "label": "RTX 5080 · MiniMax H3",
        "online": online,
        "eligible": eligible,
        "busy": bool(record.get("busy")) if online else False,
        "gpu": record.get("gpu") or "NVIDIA GeForce RTX 5080",
        "vram_mib": record.get("vram_mib"),
        "generation_verified": bool(record.get("generation_verified")),
        "model_profile": RTX5080_MODEL_PROFILE,
        "modes": list(record.get("modes") or ()),
        "last_seen_age_seconds": round(age, 1) if age is not None else None,
    }


def record_worker_heartbeat(worker_id, payload, now=None):
    if worker_id != RTX5080_WORKER_ID or not isinstance(payload, dict):
        raise ValueError("unknown worker")
    gpu = payload.get("gpu")
    profile = payload.get("model_profile")
    modes = payload.get("modes")
    if gpu != "NVIDIA GeForce RTX 5080" or profile != RTX5080_MODEL_PROFILE:
        raise ValueError("worker capability mismatch")
    if not isinstance(modes, list) or any(mode not in ("t2v", "i2v") for mode in modes):
        raise ValueError("invalid worker modes")
    if (payload.get("comfy_up") not in (True, False)
            or payload.get("model_ready") not in (True, False)
            or payload.get("generation_verified") not in (True, False)):
        raise ValueError("invalid worker readiness")
    if payload.get("busy") not in (True, False):
        raise ValueError("invalid worker busy state")
    vram_mib = payload.get("vram_mib")
    if isinstance(vram_mib, bool) or not isinstance(vram_mib, int) or not 8192 <= vram_mib <= 65536:
        raise ValueError("invalid worker VRAM")
    record = {
        "last_seen": time.time() if now is None else float(now),
        "gpu": gpu,
        "vram_mib": vram_mib,
        "comfy_up": payload["comfy_up"],
        "model_ready": payload["model_ready"],
        "generation_verified": payload["generation_verified"],
        "busy": payload["busy"],
        "modes": sorted(set(modes)),
        "model_profile": profile,
    }
    with LOCK:
        WORKERS[worker_id] = record
    return rtx5080_worker_status(now=record["last_seen"])


def pop_next_pgx_job(jobs, queue):
    """Remove the oldest queued PGX job while leaving RTX work untouched."""
    for index, jid in enumerate(tuple(queue)):
        job = jobs.get(jid) or {}
        if job.get("status") != "queued":
            continue
        if (job.get("cfg") or {}).get("worker_target", "pgx") == "pgx":
            queue.pop(index)
            return jid
    return None


def queued_jobs_for_target(jobs, queue, worker_target):
    return sum(
        1 for jid in queue
        if (jobs.get(jid) or {}).get("status") == "queued"
        and (jobs.get(jid, {}).get("cfg") or {}).get("worker_target", "pgx") == worker_target
    )


def worker_queue_snapshot(jobs=None, queue=None):
    jobs = JOBS if jobs is None else jobs
    if queue is None:
        with QUEUE_LOCK:
            queue = tuple(QUEUE)
    else:
        queue = tuple(queue)
    counters = {"pgx": 0, "rtx5080": 0}
    positions = {}
    for jid in queue:
        job = jobs.get(jid) or {}
        target = (job.get("cfg") or {}).get("worker_target", "pgx")
        if target not in counters or job.get("status") != "queued":
            continue
        counters[target] += 1
        positions[jid] = counters[target]
    rtx_active = next((
        jid for jid, job in jobs.items()
        if job.get("status") == "running"
        and (job.get("cfg") or {}).get("worker_target") == "rtx5080"
    ), None)
    return {
        "positions": positions,
        "pgx": {"pending": counters["pgx"], "active_job": ACTIVE[0]},
        "rtx5080": {"pending": counters["rtx5080"], "active_job": rtx_active},
    }


def _rtx5080_lease_hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _public_rtx5080_claim(job):
    private_keys = {
        "lease_sha256", "lease_expires_at", "execution_id",
        "completed_execution_id", "completed_lease_sha256", "_persist_version",
        "upload_path", "upload_expected_sha256", "upload_expected_size",
        "upload_received", "upload_started_at",
    }
    public = {k: v for k, v in job.items() if k not in private_keys}
    cfg = dict(public.get("cfg") or {})
    for key in tuple(cfg):
        if key.endswith("_source_path"):
            cfg.pop(key, None)
    public["cfg"] = cfg
    return public


def _active_rtx5080_lease_locked(now):
    return next((
        job for job in JOBS.values()
        if job.get("status") == "running"
        and job.get("worker_id") == RTX5080_WORKER_ID
        and float(job.get("lease_expires_at") or 0) >= now
    ), None)


def claim_rtx5080_job(now=None, token_factory=None):
    """Atomically claim one RTX job only when no live server lease exists."""
    now = time.time() if now is None else float(now)
    token_factory = token_factory or (lambda: uuid.uuid4().hex + uuid.uuid4().hex)
    execution_id = token_factory()
    lease_token = token_factory()
    with QUEUE_LOCK:
        with LOCK:
            worker = WORKERS.get(RTX5080_WORKER_ID) or {}
            fresh = now - float(worker.get("last_seen", 0)) <= WORKER_HEARTBEAT_TTL_SECONDS
            ready = (
                fresh and not worker.get("busy")
                and _active_rtx5080_lease_locked(now) is None
                and worker.get("gpu") == "NVIDIA GeForce RTX 5080"
                and int(worker.get("vram_mib") or 0) >= 15000
                and worker.get("comfy_up") is True
                and worker.get("model_ready") is True
                and worker.get("generation_verified") is True
                and worker.get("model_profile") == RTX5080_MODEL_PROFILE
                and set(worker.get("modes") or ()) >= {"t2v", "i2v"}
            )
            if not ready:
                return None
            candidate_index = None
            jid = None
            for index, candidate in enumerate(QUEUE):
                job = JOBS.get(candidate) or {}
                if (job.get("status") == "queued"
                        and (job.get("cfg") or {}).get("worker_target") == "rtx5080"):
                    candidate_index, jid = index, candidate
                    break
            if jid is None:
                return None
            assert candidate_index is not None
            job = JOBS[jid]
            job.update({
                "status": "running",
                "worker_id": RTX5080_WORKER_ID,
                "execution_id": execution_id,
                "lease_sha256": _rtx5080_lease_hash(lease_token),
                "lease_expires_at": now + RTX5080_LEASE_SECONDS,
                "attempts": int(job.get("attempts") or 0) + 1,
                "started": now,
            })
            QUEUE.pop(candidate_index)
            worker["busy"] = True
            public_job = _public_rtx5080_claim(job)
    _save_job(jid)
    return {"job": public_job, "execution_id": execution_id, "lease_token": lease_token}


def _valid_rtx5080_lease_locked(job, execution_id, lease_token, now):
    expected = str((job or {}).get("lease_sha256") or "")
    return bool(
        expected and job.get("status") == "running"
        and job.get("worker_id") == RTX5080_WORKER_ID
        and job.get("execution_id") == execution_id
        and hmac.compare_digest(expected, _rtx5080_lease_hash(lease_token))
        and float(job.get("lease_expires_at") or 0) >= now
    )


def validate_rtx5080_lease(jid, execution_id, lease_token, now=None):
    now = time.time() if now is None else float(now)
    with LOCK:
        return _valid_rtx5080_lease_locked(
            JOBS.get(jid) or {}, execution_id, lease_token, now
        )


def renew_rtx5080_lease(jid, execution_id, lease_token, now=None):
    now = time.time() if now is None else float(now)
    with LOCK:
        job = JOBS.get(jid) or {}
        if not _valid_rtx5080_lease_locked(job, execution_id, lease_token, now):
            return False
        job["lease_expires_at"] = now + RTX5080_LEASE_SECONDS
    _save_job(jid)
    return True


def update_rtx5080_progress(jid, execution_id, lease_token, payload, now=None):
    now = time.time() if now is None else float(now)
    if not validate_rtx5080_lease(jid, execution_id, lease_token, now=now):
        raise PermissionError("stale or invalid RTX 5080 lease")
    if not isinstance(payload, dict):
        raise ValueError("invalid progress payload")
    value, maximum = payload.get("value"), payload.get("max")
    segment_index = payload.get("segment_index", 0)
    segments = payload.get("segments", 1)
    if (not _finite_real(value) or not _finite_real(maximum)
            or float(maximum) <= 0 or float(value) < 0 or float(value) > float(maximum)):
        raise ValueError("invalid sampler measurement")
    if (isinstance(segment_index, bool) or not isinstance(segment_index, int)
            or isinstance(segments, bool) or not isinstance(segments, int)
            or segments < 1 or not 0 <= segment_index < segments):
        raise ValueError("invalid segment measurement")
    value, maximum = float(value), float(maximum)
    overall = ((segment_index + value / maximum) / segments) * 100.0
    with LOCK:
        job = JOBS.get(jid) or {}
        if not _valid_rtx5080_lease_locked(job, execution_id, lease_token, now):
            raise PermissionError("stale RTX 5080 execution")
        previous = job.get("progress") or {}
        previous_segment = int(previous.get("segment_index") or 0)
        previous_value = previous.get("value")
        if segment_index < previous_segment or (
            segment_index == previous_segment and _finite_real(previous_value)
            and value < float(previous_value)
        ):
            raise ValueError("sampler progress cannot regress")
        started = float(job.get("started") or now)
        phase = re.sub(r"[^0-9A-Za-z가-힣\s./:_()\-]", "", str(payload.get("phase") or ""))[:80].strip()
        job["progress"] = _prog(
            jid, phase or "영상 생성 중",
            pct=round(overall, 2), value=value, max=maximum,
            segment_index=segment_index, segments=segments,
            elapsed=max(0.0, now - started), last_progress_at=now,
        )
        job["lease_expires_at"] = now + RTX5080_LEASE_SECONDS
        public = _public_rtx5080_claim(job)
    _save_job(jid)
    return public


def _rtx5080_upload_path(jid):
    if not valid_job_id(jid):
        raise ValueError("invalid job id")
    path = os.path.join(OUT_DIR, jid, f"{jid}.remote.part")
    if not _is_under_nas(path):
        raise RuntimeError("remote upload path is outside NAS")
    return path


def begin_rtx5080_upload(jid, execution_id, lease_token, size, sha256, now=None):
    now = time.time() if now is None else float(now)
    if not validate_rtx5080_lease(jid, execution_id, lease_token, now=now):
        raise PermissionError("stale or invalid RTX 5080 lease")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 8 * 1024 ** 3:
        raise ValueError("invalid upload size")
    if not re.fullmatch(r"[0-9a-f]{64}", str(sha256 or "")):
        raise ValueError("invalid upload sha256")
    path = _rtx5080_upload_path(jid)
    with REMOTE_UPLOAD_LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with LOCK:
            job = JOBS.get(jid) or {}
            if not _valid_rtx5080_lease_locked(job, execution_id, lease_token, now):
                raise PermissionError("stale RTX 5080 execution")
            with open(path, "wb"):
                pass
            job.update(upload_path=path, upload_expected_size=size,
                       upload_expected_sha256=sha256, upload_received=0,
                       lease_expires_at=now + RTX5080_LEASE_SECONDS)
        _save_job(jid)
    return {"received": 0, "chunk_max": WORKER_UPLOAD_CHUNK_MAX}


def append_rtx5080_upload(jid, execution_id, lease_token, offset, data, chunk_sha256, now=None):
    fixed_now = now is not None
    now = time.time() if now is None else float(now)
    if not validate_rtx5080_lease(jid, execution_id, lease_token, now=now):
        raise PermissionError("stale or invalid RTX 5080 lease")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("invalid upload offset")
    if not isinstance(data, bytes) or not data or len(data) > WORKER_UPLOAD_CHUNK_MAX:
        raise ValueError("invalid upload chunk")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), str(chunk_sha256 or "")):
        raise ValueError("upload chunk sha256 mismatch")
    with REMOTE_UPLOAD_LOCK:
        with LOCK:
            job = JOBS.get(jid) or {}
            if not _valid_rtx5080_lease_locked(job, execution_id, lease_token, now):
                raise PermissionError("stale RTX 5080 execution")
            path = job.get("upload_path")
            received = int(job.get("upload_received") or 0)
            expected_size = int(job.get("upload_expected_size") or 0)
        if not path or not _is_under_nas(path) or offset > received or offset + len(data) > expected_size:
            raise ValueError("invalid upload range")
        if offset < received:
            if offset + len(data) > received:
                raise ValueError("overlapping upload retry")
            with open(path, "rb") as existing:
                existing.seek(offset)
                if existing.read(len(data)) != data:
                    raise ValueError("upload retry bytes mismatch")
            return {"received": received}
        with open(path, "ab") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        received += len(data)
        commit_now = now if fixed_now else time.time()
        with LOCK:
            job = JOBS.get(jid) or {}
            if not _valid_rtx5080_lease_locked(job, execution_id, lease_token, commit_now):
                raise PermissionError("stale RTX 5080 execution")
            job["upload_received"] = received
            job["lease_expires_at"] = commit_now + RTX5080_LEASE_SECONDS
        _save_job(jid)
    return {"received": received}


def _validate_remote_mp4(path):
    if not os.path.isfile(path) or os.path.getsize(path) < 1024:
        raise ValueError("remote result is not a valid MP4")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height:format=duration",
         "-of", "json", path],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if probe.returncode != 0:
        raise ValueError("remote result failed MP4 probing")
    try:
        metadata = json.loads(probe.stdout or "{}")
        stream = (metadata.get("streams") or [])[0]
        duration = float((metadata.get("format") or {}).get("duration") or 0)
        if not stream.get("codec_name") or int(stream.get("width") or 0) <= 0 or int(stream.get("height") or 0) <= 0 or duration <= 0:
            raise ValueError
    except (IndexError, KeyError, TypeError, ValueError):
        raise ValueError("remote result has invalid video metadata") from None
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        timeout=600, check=False,
    )
    if decode.returncode != 0:
        raise ValueError("remote result failed full video decode")


def complete_rtx5080_upload(jid, execution_id, lease_token, now=None):
    fixed_now = now is not None
    now = time.time() if now is None else float(now)
    lease_hash = _rtx5080_lease_hash(lease_token)
    with LOCK:
        existing = JOBS.get(jid) or {}
        if (existing.get("status") == "done"
                and existing.get("completed_execution_id") == execution_id
                and hmac.compare_digest(str(existing.get("completed_lease_sha256") or ""), lease_hash)):
            return _public_rtx5080_claim(existing)
    if not validate_rtx5080_lease(jid, execution_id, lease_token, now=now):
        raise PermissionError("stale or invalid RTX 5080 lease")
    with REMOTE_UPLOAD_LOCK:
        with LOCK:
            job = JOBS.get(jid) or {}
            if (job.get("status") == "done"
                    and job.get("completed_execution_id") == execution_id
                    and hmac.compare_digest(str(job.get("completed_lease_sha256") or ""), lease_hash)):
                return _public_rtx5080_claim(job)
            if not _valid_rtx5080_lease_locked(job, execution_id, lease_token, now):
                raise PermissionError("stale RTX 5080 execution")
            path = job.get("upload_path")
            expected_size = int(job.get("upload_expected_size") or 0)
            expected_hash = str(job.get("upload_expected_sha256") or "")
            received = int(job.get("upload_received") or 0)
            started = float(job.get("started") or now)
        if (not path or not os.path.isfile(path) or received != expected_size
                or os.path.getsize(path) != expected_size or file_sha256(path) != expected_hash):
            raise ValueError("remote upload final verification failed")
        _validate_remote_mp4(path)
        commit_now = now if fixed_now else time.time()
        with LOCK:
            job = JOBS.get(jid) or {}
            if not _valid_rtx5080_lease_locked(job, execution_id, lease_token, commit_now):
                raise PermissionError("stale RTX 5080 execution")
        archive = archive_final_to_nas(jid, path)
        with LOCK:
            job = JOBS[jid]
            job.update(
                status="done", file=os.path.basename(archive["src"]), src=archive["src"],
                size=archive["size"], sha256=archive["sha256"], nas_saved=True,
                storage="nas", elapsed=round(max(0.0, now - started), 1),
                progress=_prog(jid, "생성 완료", completed=True, eta=0),
                completed_execution_id=execution_id, completed_lease_sha256=lease_hash,
                lease_sha256=None, lease_expires_at=None, execution_id=None,
                upload_path=None, upload_expected_sha256=None,
                upload_expected_size=None, upload_received=None,
            )
            worker = WORKERS.get(RTX5080_WORKER_ID)
            if worker:
                worker["busy"] = False
            public = _public_rtx5080_claim(job)
        _save_job(jid)
    return public


def fail_rtx5080_job(jid, execution_id, lease_token, error, retryable=True, now=None):
    now = time.time() if now is None else float(now)
    if not validate_rtx5080_lease(jid, execution_id, lease_token, now=now):
        raise PermissionError("stale or invalid RTX 5080 lease")
    message = "RTX 5080 worker generation failed"
    with LOCK:
        job = JOBS.get(jid) or {}
        if not _valid_rtx5080_lease_locked(job, execution_id, lease_token, now):
            raise PermissionError("stale RTX 5080 execution")
        upload_path = job.get("upload_path")
        should_retry = bool(retryable) and int(job.get("attempts") or 0) < RTX5080_MAX_ATTEMPTS
        job.update(
            status="queued" if should_retry else "error",
            error=None if should_retry else message,
            progress=_prog(jid, "RTX 5080 재시도 대기" if should_retry else "RTX 5080 생성 오류"),
            lease_sha256=None, lease_expires_at=None, execution_id=None,
            upload_path=None, upload_expected_sha256=None,
            upload_expected_size=None, upload_received=None,
        )
        worker = WORKERS.get(RTX5080_WORKER_ID)
        if worker:
            worker["busy"] = False
    if upload_path:
        with REMOTE_UPLOAD_LOCK:
            if _is_under_nas(upload_path):
                try:
                    os.remove(upload_path)
                except FileNotFoundError:
                    pass
    if should_retry:
        with QUEUE_LOCK:
            if jid not in QUEUE:
                QUEUE.append(jid)
    _save_job(jid)
    return {"retrying": should_retry, "job": _public_rtx5080_claim(job)}


def requeue_expired_rtx5080_jobs(now=None):
    now = time.time() if now is None else float(now)
    requeued = []
    terminal = []
    expired_paths = []
    with LOCK:
        for jid, job in JOBS.items():
            if (job.get("status") != "running"
                    or job.get("worker_id") != RTX5080_WORKER_ID
                    or float(job.get("lease_expires_at") or 0) >= now):
                continue
            if job.get("upload_path"):
                expired_paths.append(job["upload_path"])
            is_terminal = int(job.get("attempts") or 0) >= RTX5080_MAX_ATTEMPTS
            job.update(
                status="error" if is_terminal else "queued",
                error="RTX 5080 worker 연결이 만료됐습니다" if is_terminal else None,
                worker_id=None, execution_id=None, lease_sha256=None, lease_expires_at=None,
                upload_path=None, upload_expected_sha256=None,
                upload_expected_size=None, upload_received=None,
            )
            (terminal if is_terminal else requeued).append(jid)
        if requeued or terminal:
            worker = WORKERS.get(RTX5080_WORKER_ID)
            if worker:
                worker["busy"] = any(
                    other.get("status") == "running"
                    and other.get("worker_id") == RTX5080_WORKER_ID
                    and float(other.get("lease_expires_at") or 0) >= now
                    for other in JOBS.values()
                )
    if expired_paths:
        with REMOTE_UPLOAD_LOCK:
            for path in expired_paths:
                if _is_under_nas(path):
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        pass
    if requeued:
        with QUEUE_LOCK:
            for jid in requeued:
                if jid not in QUEUE:
                    QUEUE.append(jid)
    for jid in requeued + terminal:
        _save_job(jid)
    return requeued


class JobCancelled(RuntimeError):
    """Stop a queue worker without turning an intentional cancellation into an error."""


def host_memory_stats(meminfo_text=None):
    """Return kernel-measured host RAM, using MemAvailable when available."""
    if meminfo_text is None:
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                meminfo_text = f.read()
        except OSError:
            return None
    values = {}
    for line in meminfo_text.splitlines():
        match = re.match(r"^(MemTotal|MemAvailable):\s+(\d+)\s+kB$", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return {
        "total_gb": round(total / 1e9, 1),
        "used_gb": round((total - available) / 1e9, 1),
        "available_gb": round(available / 1e9, 1),
    }


def _job_file(jid):
    return os.path.join(JOBS_DIR, f"{jid}.json")


def _save_job(jid):
    """Persist only the newest version; stale concurrent writers discard themselves."""
    tmp = None
    try:
        with LOCK:
            job = JOBS.get(jid)
            if not job:
                return
            version = int(job.get("_persist_version") or 0) + 1
            job["_persist_version"] = version
            snapshot = copy.deepcopy(job)
        os.makedirs(JOBS_DIR, exist_ok=True)
        tmp = _job_file(jid) + f".{uuid.uuid4().hex}.tmp"
        with open(tmp, "x", encoding="utf-8") as stream:
            json.dump(snapshot, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        with JOB_SAVE_LOCK:
            with LOCK:
                current = JOBS.get(jid)
                is_current = bool(
                    current
                    and int(current.get("_persist_version") or 0) == version
                    and current == snapshot
                )
            if not is_current:
                os.remove(tmp)
                return
            os.replace(tmp, _job_file(jid))
            tmp = None
    except Exception as exc:
        log(f"  job 파일 저장 실패: {exc}")
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass


def update_job(jid, **kw):
    """LOCK 내부에서 호출: JOBS 갱신 + 디스크 영속화 트리거."""
    with LOCK:
        j = JOBS.get(jid)
        if not j:
            return
        # A delayed WebSocket/HTTP response must never mutate a job after the
        # user cancelled it. Otherwise the sole worker can remain trapped on an
        # already-forgotten ComfyUI prompt and block every later job.
        if j.get("status") == "cancelled":
            return
        j.update(kw)
    _save_job(jid)


def assert_job_active(job_id):
    """Abort the worker when its durable job was cancelled or removed."""
    with LOCK:
        job = JOBS.get(job_id)
        cancelled = not job or job.get("status") == "cancelled"
    if cancelled:
        raise JobCancelled(f"job {job_id} cancelled or removed")


def guard_prompt_presence(job_id, state, unknown_since, now=None,
                          grace_seconds=COMFY_PROMPT_MISSING_GRACE_SECONDS):
    """Bound the time a submitted prompt may be absent from queue and history."""
    assert_job_active(job_id)
    if state != "unknown":
        return None
    now = time.monotonic() if now is None else now
    if unknown_since is None:
        return now
    if now - unknown_since >= grace_seconds:
        raise RuntimeError(
            "ComfyUI 작업이 큐/기록에서 사라졌습니다 — 다시 생성해 주세요"
        )
    return unknown_since


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
        # A transient ComfyUI lookup failure cannot survive a backend restart:
        # no worker remains attached to that prompt.  Make the recovery action
        # explicit instead of displaying an endlessly-empty progress bar.
        if j["status"] in ("queued", "starting", "running", "unavailable"):
            j["status"] = "interrupted"
            j["error"] = j.get("error") or "서버 재시작으로 중단됨 — 다시 생성해 주세요"
            j["progress"] = _prog(jid, "생성 중단됨 — 다시 생성해 주세요", unavailable=True)
        with LOCK:
            JOBS[jid] = j
    log(f"  job {len(JOBS)}개 복원 ({JOBS_DIR})")


def begin_pgx_job(jid, now=None):
    now = time.time() if now is None else float(now)
    with LOCK:
        job = JOBS.get(jid)
        if not job or job.get("status") != "queued":
            return None
        cfg = dict(job["cfg"])
        ACTIVE[0] = jid
        job["started"] = now
        job["status"] = "starting"
        return cfg


def queue_worker():
    """FIFO PGX 워커: RTX 작업을 건드리지 않고 PGX 작업만 실행."""
    while True:
        with QUEUE_LOCK:
            jid = pop_next_pgx_job(JOBS, QUEUE)
        if jid is None:
            with LOCK:
                ACTIVE[0] = None
            time.sleep(0.5)
            continue
        cfg = begin_pgx_job(jid)
        if cfg is None:
            continue
        try:
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


def _finite_real(value: Any) -> TypeGuard[int | float]:
    """Accept bounded JSON numbers; bool and overflow/non-finite values fail closed."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _prog(job_id: str, phase: str, **extra: Any) -> dict[str, Any]:
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
    eta = None
    if _finite_real(sampler_pct):
        # This is *only* the raw matching sampler event: 4/20 -> 20.
        # Do not blend segment count, elapsed time, or queue position into it.
        ratio = max(0, min(1, float(sampler_pct)))
        pct = round(100 * ratio)
        # A single sample cannot reveal sampler speed. ``apply_comfy_event``
        # supplies ETA only after two real progress points establish an observed
        # seconds-per-step rate, excluding queue and model-loading time.
    if extra.get("completed"):
        pct = 100
        eta = 0
    out = {"phase": phase, "elapsed": elapsed, "pct": pct,
           "eta": eta, "updated_at": now}
    out.update(extra)
    # A connectivity failure invalidates the displayed percentage, but must not
    # discard the last raw measurement received from the matching prompt.
    # Keeping it makes the unavailable state auditable without turning old data
    # into a current progress estimate.
    if extra.get("unavailable"):
        raw_previous = j.get("progress")
        previous: dict[str, Any] = raw_previous if isinstance(raw_previous, dict) else {}
        for key in (
            "value", "max", "node", "sampler_node", "last_progress_at",
            "sampler_pct", "sampler_step_seconds",
        ):
            if key not in out and key in previous:
                out[key] = previous[key]
    return out


def _running_lifecycle_progress(job_id: str, phase: str, **extra: Any) -> dict[str, Any]:
    """Refresh lifecycle metadata without erasing the last sampler sample."""
    progress = _prog(job_id, phase, **extra)
    with LOCK:
        job = JOBS.get(job_id) or {}
        raw_previous = job.get("progress")
        previous: dict[str, Any] = dict(raw_previous) if isinstance(raw_previous, dict) else {}
    # Raw sampler observations remain auditable across an unavailable state and
    # provide the baseline for the next real WebSocket progress event. They do
    # not by themselves revive the display percentage or ETA.
    for key in (
        "value", "max", "sampler_node", "sampler_pct",
        "last_progress_at", "sampler_step_seconds",
    ):
        if key in previous:
            progress[key] = previous[key]
    if previous.get("pct") is not None and not previous.get("unavailable"):
        for key in (
            "pct", "eta",
        ):
            if key in previous:
                progress[key] = previous[key]
        # Queue polling does not know the current node, so retain the prior one.
        # An executing event always supplies the key (including node=None) and
        # must remain authoritative for the latest lifecycle node.
    if "node" not in extra and "node" in previous:
        progress["node"] = previous["node"]
    return progress


def apply_comfy_event(job_id: str, prompt_id: str, event: Any, seg_done: int = 0, segments: int = 1) -> bool:
    """Apply one ComfyUI WebSocket event only when it belongs to ``prompt_id``.

    ComfyUI broadcasts events for all clients. Prompt-id equality is the sole
    correlation key, so unrelated work cannot affect an H3 job.
    """
    if not isinstance(event, dict):
        return False
    raw_data = event.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    if str(data.get("prompt_id") or "") != str(prompt_id):
        return False
    typ = event.get("type")
    if typ == "progress":
        value, maximum = data.get("value"), data.get("max")
        if (not _finite_real(value) or not _finite_real(maximum)
                or value < 0 or maximum <= 0 or value > maximum):
            return False
        node = data.get("node")
        with LOCK:
            job = JOBS.get(job_id) or {}
            raw_previous = job.get("progress")
            previous: dict[str, Any] = dict(raw_previous) if isinstance(raw_previous, dict) else {}
        previous_value = previous.get("value")
        previous_maximum = previous.get("max")
        previous_sampler_node = previous.get("sampler_node", previous.get("node"))
        same_sampler = previous_maximum == maximum and previous_sampler_node == node
        # Duplicate and out-of-order events must not regress the latest real
        # measurement or erase its ETA/rate/timestamp. A new sampler node or
        # maximum begins a distinct measured stream and resets the rate.
        if same_sampler and _finite_real(previous_value) and value <= previous_value:
            return False
        ratio = max(0.0, min(1.0, value / maximum))
        now = time.time()
        eta = None
        step_seconds = None
        previous_at = previous.get("last_progress_at")
        if value >= maximum:
            eta = 0
            step_seconds = previous.get("sampler_step_seconds") if same_sampler else None
        elif (same_sampler and _finite_real(previous_value)
              and value > previous_value and _finite_real(previous_at)
              and now >= previous_at):
            current_rate = (now - previous_at) / (value - previous_value)
            old_rate = previous.get("sampler_step_seconds")
            # Smooth later samples enough to avoid a jumping ETA while keeping
            # the first observed interval fully measured and immediately useful.
            if _finite_real(old_rate) and old_rate >= 0:
                current_rate = old_rate * 0.65 + current_rate * 0.35
            if _finite_real(current_rate) and current_rate >= 0:
                # Keep the measured rate unrounded for both smoothing and ETA.
                # Rounding a valid sub-millisecond rate to 0.000 would falsely
                # report zero remaining time for a large sampler range.
                step_seconds = current_rate
                projected_eta = current_rate * max(0, maximum - value)
                if _finite_real(projected_eta) and projected_eta >= 0:
                    eta = round(projected_eta)
        update_job(job_id, status="running", comfy_status="running",
                   progress=_prog(job_id, "영상 생성 중", seg_done=seg_done,
                                  sampler_pct=ratio, node=data.get("node"),
                                  sampler_node=data.get("node"),
                                  value=value, max=maximum,
                                  eta=eta,
                                  sampler_step_seconds=step_seconds,
                                  last_progress_at=now,
                                  unavailable=False))
        return True
    if typ == "executing":
        # node=None signals completion, but history is still authoritative for
        # output discovery. Keep the last sampler measurement while only the
        # current lifecycle node changes.
        update_job(job_id, status="running", comfy_status="running",
                   progress=_running_lifecycle_progress(
                       job_id, "영상 생성 중", seg_done=seg_done,
                       node=data.get("node")
                   ))
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
    with LOCK:
        current_job = JOBS.get(job_id) or {}
        already_running = current_job.get("status") == "running"
    if str(prompt_id) in running:
        comfy_status, phase, result, job_status = "running", "영상 생성 중", "running", "running"
    elif str(prompt_id) in pending and already_running:
        # Queue/history are separate HTTP snapshots. A delayed pending snapshot
        # must not rewind a prompt after an executing/progress event proved that
        # it was already running, nor erase its measured sampler state.
        comfy_status, phase, result, job_status = "running", "영상 생성 중", "running", "running"
    elif str(prompt_id) in pending:
        # ComfyUI exposes queued and executing prompts separately.  Do not
        # label a waiting prompt as generating merely because H3 accepted it.
        comfy_status, phase, result, job_status = "pending", "ComfyUI 대기 중", "pending", "queued"
    else:
        return "unknown"
    progress_builder = _running_lifecycle_progress if result == "running" else _prog
    update_job(job_id, status=job_status, comfy_status=comfy_status,
               progress=progress_builder(
                   job_id, phase, seg_done=seg_done,
                   queue_running=len(queue.get("queue_running", [])),
                   queue_pending=len(queue.get("queue_pending", []))
               ))
    return result


def poll_comfy_queue_state(job_id, prompt_id, seg_done=0, segments=1):
    """Keep lifecycle state accurate while the sampler socket reconnects.

    ComfyUI queue membership can prove waiting/running, but it cannot provide a
    sampler percentage. Reconciliation therefore remains useful and honest even
    when the optional WebSocket transport is unavailable.
    """
    queue = comfy_get("/queue", timeout=30)
    return reconcile_comfy_prompt(
        job_id, prompt_id, {}, queue, seg_done=seg_done, segments=segments
    )


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


def reconnect_comfy_ws(client_id, ws_retry_at, now=None):
    """Reconnect the event stream at a bounded cadence for one client ID.

    ComfyUI associates queued prompt events with ``client_id``. Reusing the
    original identifier after a transient socket failure lets the same prompt
    resume emitting genuine sampler measurements without inventing a percent.
    """
    now = time.monotonic() if now is None else now
    if now < ws_retry_at:
        return None, ws_retry_at
    ws = _comfy_ws(client_id)
    return ws, now + COMFY_WS_RETRY_SECONDS


def recv_comfy_event(ws):
    """Read one ComfyUI JSON event and normalize peer-close signals to an error."""
    raw = ws.recv()
    if raw is None or raw == "":
        raise ConnectionError("ComfyUI event socket closed")
    return json.loads(raw)


def snap_len(seconds):
    """seconds를 17k+5 프레임 그리드에 스냅 (24fps 기준)."""
    raw = max(124, round(seconds * 24))
    return raw + (5 - (raw % 17)) % 17


def segment_frame_plan(total_seconds, segment_seconds, strategy):
    """Choose H3-compatible segment frames while minimizing duration and split overflow."""
    if strategy == STRATEGY_SINGLE:
        return [snap_len(total_seconds)]
    target = max(1, round(float(total_seconds) * 24))
    maximum_segments = max(1, math.ceil(float(total_seconds) / max(1, float(segment_seconds))))
    segment_cap = snap_len(segment_seconds)
    candidates = []
    for count in range(1, maximum_segments + 1):
        minimum_units = 7 * count  # 5 + 17*7 == 124, the H3 minimum.
        units = max(minimum_units, round((target - 5 * count) / 17))
        base, extra = divmod(units, count)
        frames = [5 + 17 * (base + (1 if index < extra else 0)) for index in range(count)]
        duration_error = abs(sum(frames) - target)
        overflow = sum(max(0, frame - segment_cap) for frame in frames)
        candidates.append((duration_error > 9, duration_error + overflow, duration_error, -count, frames))
    return min(candidates, key=lambda item: item[:4])[4]


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
    log("ComfyUI down -> reactivating primary system service (comfyui-minimax-h3)")
    # Only the system-owned :8188 service is authoritative. The similarly named
    # user unit is a failed duplicate that collides on both the port and DB lock.
    cmd = (
        "docker run --rm --privileged --pid=host -v /:/host python:3.12-alpine "
        "chroot /host /usr/bin/nsenter -t 1 -m -i -n -p /usr/bin/systemctl start comfyui-minimax-h3.service"
    )
    try:
        run_asu(cmd, timeout=60, check=True)
        log("  primary system ComfyUI start requested")
    except Exception as e:
        # It may already be starting. Wait for the authoritative listener, but
        # never fall back to the duplicate user service.
        log(f"  primary system ComfyUI start attempt failed: {e}")
    for _ in range(300):
        if comfy_up():
            log("ComfyUI ready")
            return True
        time.sleep(2)
    raise RuntimeError("ComfyUI 기동 실패 (300초 대기 초과)")


CAM_LORA_1000 = "cam_motion_1000.safetensors"
CAM_LORA_3000 = "cam_motion_3000.safetensors"
CAM_LORA_STRENGTH = 1.0


def build_workflow(text, negative, width, height, length, steps, seed, image_name=None, prefix="h3", video_name=None, realism_lora=False, cam_motion="", realism_strength=None, cam_strength=None, lora_dirs=None, strict_loras=False):
    """T2V/I2V 워크플로우 — H3 전용. Wan 폴백 제거 (사용자 지정).
    video_name: LoadVideo 노드를 통한 참조 동영상 (인물 동영상 모드)
    realism_strength/cam_strength: None이면 기본값, 실수면 0.0~2.0으로 클램프"""
    def _clamp(v, default):
        if v is None:
            return default
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return max(0.0, min(2.0, f))
    r_strength = _clamp(realism_strength, REALISM_LORA_STRENGTH)
    c_strength = _clamp(cam_strength, CAM_LORA_STRENGTH)
    base_negative = "text, subtitles, captions, watermark, logo, script overlay, on-screen text, UI elements"
    if negative:
        full_prompt = f"{text} (do NOT include: {base_negative}, {negative})"
    else:
        full_prompt = f"{text} (do NOT include: {base_negative})"
    # ReferenceToVideo has a separate multimodal reference-conditioning path.
    # Keep the user's prompt intact and add the documented explicit image tag
    # rather than hiding a prompt rewrite or stretching the photo into a keyframe.
    reference_image = bool(image_name)
    if reference_image:
        full_prompt = f"{REFERENCE_TO_VIDEO_INSTRUCTION}\n\n{full_prompt}"

    # 기본 Turbo 뒤에, 사용자가 토글을 켠 경우에만 리얼리즘 LoRA를 누적한다.
    if lora_dirs is None:
        lora_dirs = ["/home/aski/ComfyUI/models/loras",
                     "/home/aski/ComfyUI/models/loras/split_files/loras"]
    lora_avail = any(os.path.exists(os.path.join(d, H3_LORA)) for d in lora_dirs)
    realism_lora = realism_lora is True  # 문자열 "false" 등 truthy 값은 허용하지 않음
    realism_avail = realism_lora and any(
        os.path.exists(os.path.join(d, REALISM_LORA)) for d in lora_dirs
    )
    if strict_loras and not lora_avail:
        raise RuntimeError(f"exact H3 Turbo LoRA missing: {H3_LORA}")
    if strict_loras and realism_lora and not realism_avail:
        raise RuntimeError(f"requested realism LoRA missing: {REALISM_LORA}")
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
    if reference_image:
        # Dynamic input name follows ComfyUI's official ReferenceToVideo
        # template (`ref_images.ref_image_1`). `match` avoids warping a 4:5
        # portrait into the 4:7 output canvas while retaining enough detail.
        wf["5"] = {
            "class_type": "MiniMaxH3ReferenceToVideo",
            "inputs": {
                "clip": ["2", 0],
                "vae": ["3", 0],
                "audio_vae": ["4", 0],
                "prompt": full_prompt,
                "ref_image_size": "match",
                "ref_images.ref_image_1": ["15", 0],
                "width": width,
                "height": height,
                "length": length,
            },
        }
    if lora_avail:
        wf["1a"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": H3_LORA, "strength_model": 1.0}}
    if realism_avail:
        wf["1b"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1a", 0] if lora_avail else ["1", 0],
            "lora_name": REALISM_LORA,
            "strength_model": r_strength,
        }}
    # 카메라 모션 LoRA (H3 전용): 토글 시 마지막에 누적
    cam_lo = None
    if cam_motion == "1000":
        cam_lo = CAM_LORA_1000
    elif cam_motion == "3000":
        cam_lo = CAM_LORA_3000
    if cam_lo:
        cam_avail = any(os.path.exists(os.path.join(d, cam_lo)) for d in lora_dirs)
        if strict_loras and not cam_avail:
            raise RuntimeError(f"requested camera LoRA missing: {cam_lo}")
        if cam_avail:
            wf["1c"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                "model": model_ref, "lora_name": cam_lo, "strength_model": c_strength,
            }}
            model_ref = ["1c", 0]
            # 8/9의 model 참조 갱신
            wf["8"]["inputs"]["model"] = model_ref
            wf["9"]["inputs"]["model"] = model_ref
    if image_name:
        wf["15"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        # ReferenceToVideo consumes the image through its documented dynamic
        # ``ref_images.ref_image_1`` input.  Supplying it again as a
        # ``first_frame`` turns the reference path back into a stretched
        # keyframe workflow and causes incompatible node contracts.
        if not reference_image:
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
            mkdir_cmd = "/bin/sh -c " + shlex.quote(f"mkdir -p -- {NAS_SSH_DIR}")
            mkdir = subprocess.run(ssh + [mkdir_cmd], capture_output=True, text=True, timeout=30)
            if mkdir.returncode == 0:
                put = subprocess.run(["scp", "-i", NAS_SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", src_path, f"{NAS_SSH_HOST}:{remote}"], capture_output=True, text=True, timeout=300)
                local_hash = subprocess.check_output(["sha256sum", src_path], text=True).split()[0]
                verify_cmd = "/bin/sh -c " + shlex.quote(f"sha256sum -- {remote}")
                verify = subprocess.run(ssh + [verify_cmd], capture_output=True, text=True, timeout=45)
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
           "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k",
           "-r", "24", "-movflags", "+faststart",
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


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_under_nas(path):
    """Resolve symlinks and accept only paths rooted in the active NAS mount."""
    try:
        root = os.path.realpath(NAS_DIR)
        resolved = os.path.realpath(path)
    except (OSError, TypeError):
        return False
    return resolved == root or resolved.startswith(root + os.sep)


def require_nas_video_storage():
    """Fail closed before a worker can write any media to a PGX-local path."""
    paths = {
        "ComfyUI 출력": COMFY_OUT,
        "H3 작업": OUT_DIR,
        "고정 이미지 참조": REF_DIR,
        "고정 동영상 참조": REFV_DIR,
    }
    if not os.path.isdir(NAS_DIR):
        raise RuntimeError("NAS 저장소가 준비되지 않았습니다")
    for label, path in paths.items():
        if not _is_under_nas(path):
            raise RuntimeError(f"{label} 경로가 NAS 밖입니다: {path}")
    for path in (OUT_DIR, REF_DIR, REFV_DIR):
        os.makedirs(path, exist_ok=True)


def job_input_path(job_id, suffix):
    """Return a job-owned immutable media snapshot path under NAS only."""
    if not valid_job_id(job_id) or not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or ""):
        raise RuntimeError("안전하지 않은 작업 입력 경로")
    path = os.path.join(NAS_DIR, ".h3-web", "inputs", f"{job_id}{suffix}")
    if not _is_under_nas(path):
        raise RuntimeError("작업 입력 NAS 경로가 아닙니다")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def cleanup_job_input_snapshots(*paths):
    for path in paths:
        if not path or not _is_under_nas(path):
            continue
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def archive_final_to_nas(job_id, final_path, cleanup_paths=()):
    """Atomically publish a finished MP4 within NAS before dropping NAS work files."""
    if not valid_job_id(job_id):
        raise RuntimeError("안전하지 않은 작업 ID")
    if not os.path.isfile(final_path) or not _is_under_nas(final_path):
        raise RuntimeError("NAS 작업 경로의 최종 영상을 찾을 수 없습니다")
    for path in cleanup_paths:
        if path and not _is_under_nas(path):
            raise RuntimeError("NAS 밖의 임시 영상은 정리하지 않습니다")

    destination = os.path.join(NAS_DIR, f"{job_id}.mp4")
    source_size = os.path.getsize(final_path)
    source_hash = file_sha256(final_path)
    temporary = destination + f".{uuid.uuid4().hex}.tmp"
    try:
        with open(final_path, "rb") as source, open(temporary, "xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if os.path.getsize(temporary) != source_size or file_sha256(temporary) != source_hash:
            raise RuntimeError("NAS 임시 아카이브 검증 실패")
        os.replace(temporary, destination)
        if os.path.getsize(destination) != source_size or file_sha256(destination) != source_hash:
            raise RuntimeError("NAS 완료본 검증 실패")
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass

    # Do not release any input until the published NAS object passed both checks.
    for path in (final_path, *cleanup_paths):
        if path and os.path.isfile(path) and os.path.realpath(path) != os.path.realpath(destination):
            os.remove(path)
    return {
        "src": destination,
        "nas_saved": True,
        "size": source_size,
        "sha256": source_hash,
    }



def snapshot_reference_input(source_path, destination_path, source_name=""):
    """Copy the exact selected bytes into immutable job-owned storage."""
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    tmp_path = destination_path + f".{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    size = 0
    try:
        with open(source_path, "rb") as source, open(tmp_path, "xb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp_path, destination_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    stat = os.stat(destination_path)
    return {
        "source_name": os.path.basename(source_name or source_path),
        "size": size,
        "sha256": digest.hexdigest(),
        "snapshot_mtime_ns": stat.st_mtime_ns,
    }



def _upload_job_references(job_id, cfg):
    """Transfer only immutable NAS job snapshots outside the browser request."""
    specs = (
        ("image_source_path", "image_source_name", "image_name", "image_source_sha256", "image_source_size", comfy_upload_image),
        ("video_source_path", "video_source_name", "video_name", "video_source_sha256", "video_source_size", comfy_upload_video),
    )
    input_root = os.path.realpath(os.path.join(NAS_DIR, ".h3-web", "inputs"))
    for path_key, source_name_key, result_key, hash_key, size_key, uploader in specs:
        source_path = cfg.get(path_key)
        if cfg.get(result_key) or not source_path:
            continue
        source_path = os.fspath(source_path)
        if not _is_under_nas(source_path):
            raise RuntimeError("작업 참조 파일이 NAS 밖입니다")
        resolved_path = os.path.realpath(source_path)
        if not resolved_path.startswith(input_root + os.sep):
            raise RuntimeError("작업 참조 파일이 NAS 입력 저장소 밖입니다")
        if not os.path.isfile(source_path):
            raise RuntimeError(f"작업 참조 파일을 찾을 수 없습니다: {os.path.basename(source_path)}")
        last_error = None
        for attempt in range(1, 4):
            try:
                update_job(job_id, status="starting",
                           progress=_prog(job_id, f"참조 파일 전송 중 ({attempt}/3)"))
                with open(source_path, "rb") as f:
                    payload = f.read()
                source_label = cfg.get(source_name_key) or os.path.basename(source_path)
                actual_size = len(payload)
                actual_hash = hashlib.sha256(payload).hexdigest()
                expected_size = cfg.get(size_key)
                expected_hash = cfg.get(hash_key)
                if expected_size is not None and int(expected_size) != actual_size:
                    raise RuntimeError(f"작업 참조 크기 불일치: expected={expected_size}, actual={actual_size}")
                if expected_hash and expected_hash != actual_hash:
                    raise RuntimeError(f"작업 참조 SHA-256 불일치: expected={expected_hash}, actual={actual_hash}")
                uploaded = uploader(payload, source_label)
                cfg[result_key] = uploaded
                cfg[hash_key] = actual_hash
                cfg[size_key] = actual_size
                try:
                    os.remove(source_path)
                    cfg[path_key] = ""
                except OSError as cleanup_exc:
                    log(f"  job {job_id} NAS 참조 삭제 보류: {cleanup_exc}")
                with LOCK:
                    if job_id in JOBS:
                        JOBS[job_id]["cfg"] = dict(cfg)
                _save_job(job_id)
                log(f"  job {job_id} 참조 전송 완료: {uploaded}")
                break
            except Exception as exc:
                last_error = exc
                if attempt == 3:
                    raise RuntimeError(f"참조 파일 전송 실패 (3회): {exc}") from exc
                time.sleep(2 ** attempt)
        if last_error and not cfg.get(result_key):
            raise last_error


def run_job(job_id, cfg):
    try:
        require_nas_video_storage()
        ensure_comfyui()
        # Potentially slow reference transfer belongs to the durable queue worker,
        # never to the browser/Vercel request that only admits the job.
        _upload_job_references(job_id, cfg)
        total_seconds = min(cfg["seconds"], MAX_SECONDS)
        strategy = cfg.get("strategy", STRATEGY_SPLIT)
        seg_seconds = int(cfg.get("seg_seconds", SEG_SECONDS))
        segment_frames = segment_frame_plan(total_seconds, seg_seconds, strategy)
        segments = len(segment_frames)
        total_frames = sum(segment_frames)
        est = estimate_seconds(total_seconds, seg_seconds, strategy, cfg["steps"])
        update_job(job_id, segments=segments, total_seconds=total_seconds,
                   estimated_seconds=est)
        log(f"job {job_id}: {total_seconds}s [{strategy}] {segments}개 세그먼트 "
            f"(프레임 {segment_frames}) steps={cfg['steps']} 예상 {est}초")

        # 각 세그먼트 생성
        seg_files = []
        comfy_source_files = []
        for i in range(segments):
            seg_frames = segment_frames[i]
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
                                   realism_lora=cfg.get("realism_lora", False),
                                   cam_motion=cfg.get("cam_motion", ""),
                                   realism_strength=cfg.get("realism_strength"),
                                   cam_strength=cfg.get("cam_strength"))
            client_id = str(uuid.uuid4())
            # Subscribe before queueing so an immediately-started prompt cannot
            # emit its first real progress event before this client is listening.
            ws = _comfy_ws(client_id)
            # A failed initial subscription is not permanent: the polling loop
            # below reconnects with this same identity on a bounded cadence.
            ws_retry_at = 0.0
            queued = comfy_post("/prompt", {"prompt": workflow, "client_id": client_id})
            if "error" in queued:
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                err_msg = json.dumps(queued, ensure_ascii=False)
                raise RuntimeError(err_msg[:600])
            pid = queued["prompt_id"]
            unknown_since = None
            update_job(job_id, status="queued", comfy_prompt_id=pid, segment_started=time.time(),
                       comfy_status="pending",
                       progress=_prog(job_id, f"세그먼트 {i+1}/{segments} ComfyUI 대기 중" if segments > 1 else "ComfyUI 대기 중",
                                      seg_done=i, queue_pending=1))
            log(f"  seg {i+1}/{segments} queued pid={pid} (H3)")

            # A websocket supplies sampler measurements. Queue/history only
            # establish this prompt's lifecycle; neither can manufacture a pct.
            try:
                while True:
                    assert_job_active(job_id)
                    if ws is None:
                        ws, ws_retry_at = reconnect_comfy_ws(client_id, ws_retry_at)
                        if ws is not None:
                            update_job(job_id, comfy_status="connected",
                                       progress=_running_lifecycle_progress(
                                           job_id, phase="ComfyUI 진행 정보 연결됨",
                                           seg_done=i, unavailable=False
                                       ))
                    if ws:
                        try:
                            event = recv_comfy_event(ws)
                            apply_comfy_event(job_id, pid, event, i, segments)
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
                                ws_retry_at = 0.0
                    try:
                        h = comfy_get(f"/history/{pid}", timeout=30)
                    except Exception:
                        update_job(job_id, comfy_status="unavailable",
                                   progress=_prog(job_id, "ComfyUI 상태 확인 불가", seg_done=i,
                                                  unavailable=True))
                        time.sleep(2)
                        continue
                    assert_job_active(job_id)
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
                        comfy_source_files.append(src)
                        log(f"  seg {i+1}/{segments} 완료 → {dst}")
                        break
                    try:
                        prompt_state = poll_comfy_queue_state(job_id, pid, i, segments)
                    except JobCancelled:
                        raise
                    except Exception:
                        update_job(job_id, comfy_status="unavailable",
                                   progress=_prog(job_id, "ComfyUI 상태 확인 불가", seg_done=i,
                                                  unavailable=True))
                        time.sleep(2)
                        continue
                    unknown_since = guard_prompt_presence(
                        job_id, prompt_state, unknown_since
                    )
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

        # NAS 내부 atomic publish가 byte-size/SHA-256 검증을 통과한 뒤에만
        # NAS work/ComfyUI의 작업 전용 중간 영상들을 정리한다.
        update_job(job_id, progress=_prog(job_id, "NAS 저장·검증 중", seg_done=segments, done_phase=3))
        archive = archive_final_to_nas(
            job_id, final_local, cleanup_paths=tuple(seg_files + comfy_source_files)
        )
        try:
            os.rmdir(dst_dir)  # archive function removed its NAS video children already
        except OSError:
            pass
        final_src = archive["src"]
        fsize = int(archive["size"])
        update_job(job_id,
            status="done", file=os.path.basename(final_src), src=final_src,
            progress=_prog(job_id, "생성 완료", completed=True, eta=0, seg_done=segments),
            elapsed=round(time.time() - JOBS[job_id].get("started", time.time()), 1),
            segments=segments, total_seconds=total_seconds,
            size=fsize,
            nas_saved=bool(archive.get("nas_saved")),
            sha256=archive.get("sha256", ""),
            storage="nas",
        )
        log(f"job {job_id} done → {final_src} ({segments}seg, {total_seconds}s, 24fps, {fsize//1048576}MB, nas=OK)")
        return
    except JobCancelled:
        log(f"job {job_id} 취소 확인 → worker 해제")
        return
    except Exception as e:
        update_job(job_id, status="error", error=str(e)[:800])
        log(f"job {job_id} ERROR: {str(e)[:200]}")


# ---------- HTTP ----------
UPLOADED = {}   # nonce -> {"path": str, "w": int, "h": int, "ts": float}
UPLOAD_LOCK = threading.Lock()
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_REF_VIDEO_BYTES = 100 * 1024 * 1024


def extract_multipart_file_field(raw, content_type, field_name):
    """Return a browser FormData file field without corrupting trailing bytes.

    The standard library's historic ``cgi`` parser is removed in newer Python,
    so this deliberately handles only the bounded single-file shape used here.
    It strips multipart framing only, preserving an actual CRLF at EOF.
    """
    match = re.search(r'(?:^|;)\s*boundary=(?:"([^"]+)"|([^;\s]+))', content_type or '', re.I)
    if not match:
        raise ValueError("multipart boundary 없음")
    boundary = (match.group(1) or match.group(2)).encode('utf-8')
    delimiter = b"--" + boundary
    for part in raw.split(delimiter):
        if not part or part.startswith(b"--"):
            continue
        part = part.lstrip(b"\r\n")
        headers, sep, payload = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        disposition = re.search(rb'content-disposition:\s*form-data;[^\r\n]*', headers, re.I)
        if not disposition:
            continue
        name_match = re.search(rb'name="([^"]+)"', disposition.group(0), re.I)
        if not name_match or name_match.group(1).decode('utf-8', 'replace') != field_name:
            continue
        filename_match = re.search(rb'filename="([^"]*)"', disposition.group(0), re.I)
        if not filename_match:
            continue
        # Every multipart boundary is preceded by one framing CRLF. If the
        # original file itself ends CRLF, it appears twice and one remains.
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        filename = filename_match.group(1).decode('utf-8', 'replace')
        return os.path.basename(filename) or "upload.bin", payload
    raise ValueError(f"{field_name} 파일 필드 없음")


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


def _read_refv_meta() -> dict[str, Any] | None:
    try:
        with open(REFV_META, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_refv() -> dict[str, Any] | None:
    """Return fixed-video metadata only when its private NAS MP4 is present."""
    if not os.path.isfile(_refv_video_path()) or not os.path.isfile(_refv_path()):
        return None
    m = _read_refv_meta()
    if not m or not _is_under_nas(_refv_video_path()):
        return None
    expected_size = m.get("size")
    expected_hash = str(m.get("sha256") or "")
    if expected_size is None:
        return None
    try:
        if int(expected_size) != os.path.getsize(_refv_video_path()):
            return None
    except (TypeError, ValueError, OSError):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash.lower()):
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
    """Atomically save the private fixed reference MP4 and frame on NAS only."""
    if not video_bytes or not frame_bytes:
        raise RuntimeError("고정 동영상 참조 데이터가 비어 있습니다")
    if not _is_under_nas(REFV_DIR):
        raise RuntimeError("고정 동영상 참조 NAS 경로가 아닙니다")
    os.makedirs(REFV_DIR, exist_ok=True)
    video_path = _refv_video_path()
    frame_path = _refv_path()
    video_tmp = video_path + f".{uuid.uuid4().hex}.tmp"
    frame_tmp = frame_path + f".{uuid.uuid4().hex}.tmp"
    meta_tmp = REFV_META + f".{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256(video_bytes).hexdigest()
    meta = {
        "name": name, "w": w, "h": h, "size": len(video_bytes),
        "ts": time.time(), "duration_s": duration_s, "ts_offset": ts_offset,
        "frame_size": len(frame_bytes), "sha256": digest,
    }
    try:
        for path, payload in ((video_tmp, video_bytes), (frame_tmp, frame_bytes)):
            with open(path, "xb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        with open(meta_tmp, "x", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if os.path.getsize(video_tmp) != len(video_bytes) or file_sha256(video_tmp) != digest:
            raise RuntimeError("고정 동영상 참조 NAS 저장 검증 실패")
        os.replace(video_tmp, video_path)
        os.replace(frame_tmp, frame_path)
        os.replace(meta_tmp, REFV_META)
    finally:
        for temp_path in (video_tmp, frame_tmp, meta_tmp):
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
    log(f"고정 동영상 참조 NAS 저장: {name} ({w}x{h}, {duration_s:.1f}s, {ts_offset:.1f}s 지점)")
    return meta


def _delete_refv():
    for pth in (_refv_path(), _refv_video_path(), REFV_META):
        try:
            if os.path.isfile(pth):
                os.remove(pth)
        except OSError:
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


def remote_range_dd_plan(start, length):
    """Plan a BusyBox-compatible block read for one exact HTTP byte window.

    ``dd`` only skips whole blocks portably on QNAP. The caller discards the
    leading partial block and emits exactly ``length`` bytes to the client.
    """
    if start < 0 or length <= 0:
        raise ValueError("invalid remote range")
    first_block, prefix = divmod(start, REMOTE_RANGE_BLOCK_BYTES)
    blocks = (prefix + length + REMOTE_RANGE_BLOCK_BYTES - 1) // REMOTE_RANGE_BLOCK_BYTES
    return first_block, prefix, blocks


def _job_video_source(jid):
    """Resolve a completed job video without downloading it into the web tier."""
    with LOCK:
        job = JOBS.get(jid)
        src = job.get("src") if job and job.get("status") == "done" else None
    remote = bool(src and src.startswith(NAS_SSH_DIR.rstrip("/") + "/"))
    if src and (remote or os.path.isfile(src)):
        return src, remote
    fallback = os.path.join(OUT_DIR, jid, f"{jid}.mp4")
    return (fallback, False) if os.path.isfile(fallback) else (None, False)


def ensure_job_thumbnail(jid):
    """Create and cache a small JPEG poster from the finished MP4."""
    thumb_dir = os.path.join(JOBS_DIR, "thumbnails")
    thumb = os.path.join(thumb_dir, f"{jid}.jpg")
    if os.path.isfile(thumb) and os.path.getsize(thumb) > 0:
        return thumb
    src, remote = _job_video_source(jid)
    if not src:
        return None
    os.makedirs(thumb_dir, exist_ok=True)
    tmp = os.path.join(thumb_dir, f".{jid}-{uuid.uuid4().hex}.jpg")
    ffmpeg_cmd = [
        "ffmpeg", "-loglevel", "error", "-y", "-ss", "0.1", "-i",
        "pipe:0" if remote else src, "-frames:v", "1", "-vf",
        "scale=640:-2:force_original_aspect_ratio=decrease", "-q:v", "3", tmp,
    ]
    ssh_proc = None
    try:
        if remote:
            if not os.path.isfile(NAS_SSH_KEY):
                return None
            ssh_proc = subprocess.Popen([
                "ssh", "-i", NAS_SSH_KEY, "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", NAS_SSH_HOST,
                "cat", "--", src,
            ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            result = subprocess.run(ffmpeg_cmd, stdin=ssh_proc.stdout,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=120)
        else:
            result = subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=120)
        if result.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            log(f"썸네일 생성 실패 {jid}: {result.stderr.decode(errors='replace')[:200]}")
            return None
        os.replace(tmp, thumb)
        return thumb
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"썸네일 생성 오류 {jid}: {exc}")
        return None
    finally:
        if ssh_proc is not None and ssh_proc.poll() is None:
            ssh_proc.kill()
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def cancel_queued_job(jid):
    """Cancel a not-yet-running job without persisting under ``LOCK``.

    ``_save_job`` acquires ``LOCK`` internally, so calling it while already
    holding the non-reentrant lock permanently deadlocks every job API.
    """
    with LOCK:
        job = JOBS.get(jid)
        if not job or job.get("status") not in ("queued", "starting"):
            return False
        job["status"] = "cancelled"
    with QUEUE_LOCK:
        if jid in QUEUE:
            QUEUE.remove(jid)
    _save_job(jid)
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        log(format % args)

    def _origin_authorized(self):
        """Require the Vercel-only secret whenever production configured one.

        Development/test servers may omit the secret. The production systemd
        unit has a fail-closed ExecStartPre so that mode cannot start on PGX.
        """
        if not ORIGIN_SECRET:
            return True
        supplied = self.headers.get(ORIGIN_HEADER, "")
        return hmac.compare_digest(supplied, ORIGIN_SECRET)

    def _require_origin(self):
        if self._origin_authorized():
            return True
        send_json(self, {"ok": False, "error": "unauthorized origin"}, 401)
        return False

    def _worker_authorized(self):
        if not WORKER_SECRET:
            return False
        supplied = self.headers.get(WORKER_HEADER, "")
        return hmac.compare_digest(supplied, WORKER_SECRET)

    def _require_worker(self):
        if self._worker_authorized():
            return True
        send_json(self, {"ok": False, "error": "unauthorized worker"}, 401)
        return False

    def _cors(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        if self.path.startswith("/api/") and not self._require_origin():
            return
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

    def _serve_video(self, src, jid, disposition, cache_control="no-store, no-cache, must-revalidate"):
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
        self.send_header("Cache-Control", cache_control)
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{start + length - 1}/{size}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            return
        if remote:
            # QNAP BusyBox cannot skip byte offsets natively. Read aligned 64KiB
            # blocks instead, discard the prefix in this process, and never send
            # any extra padding bytes beyond the HTTP Content-Length window.
            first_block, discard, block_count = remote_range_dd_plan(start, length)
            proc = subprocess.Popen(["ssh", "-i", NAS_SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", NAS_SSH_HOST,
                                     "dd", f"if={src}", f"bs={REMOTE_RANGE_BLOCK_BYTES}",
                                     f"skip={first_block}", f"count={block_count}"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert proc.stdout is not None and proc.stderr is not None
            remaining = length
            try:
                while True:
                    chunk = proc.stdout.read(1024 * 256)
                    if not chunk:
                        break
                    if discard:
                        cut = min(discard, len(chunk))
                        chunk = chunk[cut:]
                        discard -= cut
                    if chunk and remaining:
                        payload = chunk[:remaining]
                        self.wfile.write(payload)
                        remaining -= len(payload)
                    # Drain aligned trailing bytes too; otherwise ``dd`` can
                    # block on its pipe before it exits on a short final range.
                status = proc.wait(timeout=60)
                if status != 0 or remaining:
                    log(f"NAS SSH video stream 불완전: exit={status}, remaining={remaining}, stderr={proc.stderr.read().decode(errors='replace')[:160]}")
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
        if p.startswith("/api/") and not self._require_origin():
            return
        if p.startswith("/api/worker/") and not self._require_worker():
            return
        if p.startswith("/api/worker/input/"):
            parts = p.split("/")
            if len(parts) != 6 or parts[5] not in ("image", "video"):
                send_json(self, {"ok": False, "error": "invalid worker input route"}, 404)
                return
            jid, kind = parts[4], parts[5]
            execution_id = self.headers.get(WORKER_EXECUTION_HEADER, "")
            lease_token = self.headers.get(WORKER_LEASE_HEADER, "")
            now = time.time()
            if not validate_rtx5080_lease(jid, execution_id, lease_token, now=now):
                send_json(self, {"ok": False, "error": "stale or invalid RTX 5080 lease"}, 409)
                return
            key = "image_source_path" if kind == "image" else "video_source_path"
            size_key = "image_source_size" if kind == "image" else "video_source_size"
            hash_key = "image_source_sha256" if kind == "image" else "video_source_sha256"
            with LOCK:
                job = JOBS.get(jid) or {}
                cfg = job.get("cfg") or {}
                source = cfg.get(key)
                expected_size = cfg.get(size_key)
                expected_hash = cfg.get(hash_key)
                if job.get("execution_id") == execution_id:
                    job["lease_expires_at"] = now + RTX5080_LEASE_SECONDS
            if not source or not os.path.isfile(source) or not _is_under_nas(source):
                send_json(self, {"ok": False, "error": "worker input not found"}, 404)
                return
            size = os.path.getsize(source)
            if expected_size is not None and int(expected_size) != size:
                send_json(self, {"ok": False, "error": "worker input size mismatch"}, 409)
                return
            try:
                byte_range = parse_byte_range(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            start, end = byte_range if byte_range else (0, size - 1)
            length = end - start + 1
            self.send_response(206 if byte_range else 200)
            self.send_header("Content-Type", "image/png" if kind == "image" else "video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, no-store")
            if expected_hash:
                self.send_header("X-Content-SHA256", expected_hash)
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command != "HEAD":
                with open(source, "rb") as payload:
                    payload.seek(start)
                    remaining = length
                    while remaining:
                        chunk = payload.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            _save_job(jid)
            return
        if p == "/api/jobs":
            requeue_expired_rtx5080_jobs()
            with LOCK:
                items = [_public_rtx5080_claim(dict(j, prompt=j.get("prompt", ""))) for j in JOBS.values()]
                jobs_snapshot = {jid: dict(job) for jid, job in JOBS.items()}
            items.sort(key=lambda x: x.get("created", 0), reverse=True)
            queues = worker_queue_snapshot(jobs_snapshot)
            for item in items:
                target = item.get("worker_target") or (item.get("cfg") or {}).get("worker_target", "pgx")
                item["worker_target"] = target
                item.setdefault("worker_label", "RTX 5080" if target == "rtx5080" else "PGX Spark")
                item["queue_position"] = queues["positions"].get(item.get("id"))
            q_len = queues["pgx"]["pending"] + queues["rtx5080"]["pending"]
            active_id = queues["pgx"]["active_job"]
            # 상세 상태: ComfyUI 버전/GPU, NAS, 활성 job
            cstats = comfy_get("/system_stats", timeout=3) if comfy_up() else {}
            device = (cstats.get("devices") or [{}])[0]
            vram_free = device.get("vram_free", 0)
            vram_total = device.get("vram_total", 0)
            send_json(self, {
                "ok": True, "jobs": items,
                "comfy_up": comfy_up(),
                "comfy_info": {
                    "version": cstats.get("system", {}).get("comfyui_version", ""),
                    "gpu": device.get("name", ""),
                    "gpu_vram_free_gb": round(vram_free / 1e9, 1),
                    "gpu_vram_used_gb": round(max(vram_total - vram_free, 0) / 1e9, 1),
                    "gpu_vram_total_gb": round(vram_total / 1e9, 1),
                } if cstats else None,
                "host_memory": host_memory_stats(),
                "nas_ok": nas_ok(),
                "queue_len": q_len,
                "active_job": active_id,
                "queues": {"pgx": queues["pgx"], "rtx5080": queues["rtx5080"]},
                "workers": {
                    "pgx": {"id": "pgx", "label": "PGX Spark", "online": comfy_up(),
                            "eligible": comfy_up(), "busy": bool(active_id)},
                    "rtx5080": rtx5080_worker_status(),
                },
            })
        elif p == "/api/workers":
            with LOCK:
                jobs_snapshot = {jid: dict(job) for jid, job in JOBS.items()}
            queues = worker_queue_snapshot(jobs_snapshot)
            pgx_online = comfy_up()
            send_json(self, {
                "ok": True,
                "workers": {
                    "pgx": {"id": "pgx", "label": "PGX Spark", "online": pgx_online,
                            "eligible": pgx_online, "busy": bool(queues["pgx"]["active_job"]),
                            **queues["pgx"]},
                    "rtx5080": {**rtx5080_worker_status(), **queues["rtx5080"]},
                },
            })
        elif p.startswith("/api/job/"):
            jid = p.split("/")[3]
            if not valid_job_id(jid):
                send_json(self, {"ok": False, "error": "invalid job id"}, 400)
                return
            with QUEUE_LOCK:
                queue_snapshot = tuple(QUEUE)
                with LOCK:
                    jobs_snapshot = {job_id: dict(job) for job_id, job in JOBS.items()}
            source = jobs_snapshot.get(jid)
            j = _public_rtx5080_claim(source) if source else None
            if j:
                queues = worker_queue_snapshot(jobs_snapshot, queue_snapshot)
                j["queue_position"] = queues["positions"].get(jid)
            send_json(self, {"ok": True, "job": j}, code=200 if j else 404)
        elif p.startswith("/api/ref/status"):
            # GET /api/ref/status — 고정 참조 메타데이터만
            ref = _load_ref()
            send_json(self, {"ok": True, "ref": ref})
        elif p == "/api/ref":
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
        elif p == "/api/refv":
            # 고정 참조 동영상은 개인 업로드이므로 shared edge cache 없이,
            # 완료본과 동일한 Range/seek semantics로만 반환한다.
            if not os.path.isfile(_refv_video_path()):
                send_json(self, {"ok": False, "refv": None}, 404)
                return
            self._serve_video(_refv_video_path(), "reference", "inline", cache_control="private, no-store")
        elif p.startswith("/api/thumbnail/"):
            jid = p.split("/")[3]
            if not valid_job_id(jid):
                send_json(self, {"ok": False, "error": "invalid job id"}, 400)
                return
            thumb = ensure_job_thumbnail(jid)
            if not thumb:
                send_json(self, {"ok": False, "error": f"썸네일 생성 가능 영상 없음 ({jid})"}, 404)
                return
            fsize = os.path.getsize(thumb)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(fsize))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command != "HEAD":
                with open(thumb, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, length=64 * 1024)
        elif p.startswith("/api/download/") or p.startswith("/api/view/"):
            jid = p.split("/")[3]
            if not valid_job_id(jid):
                send_json(self, {"ok": False, "error": "invalid job id"}, 400)
                return
            src, _remote = _job_video_source(jid)
            if not src:
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
                # Remove only multipart framing, never valid trailing file bytes.
                if body.endswith(b"\r\n"):
                    body = body[:-2]
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
            sha256 = hashlib.sha256(data).hexdigest()
            UPLOADED[nonce] = {"path": dst, "w": w, "h": h, "ts": time.time(),
                               "name": safe, "size": len(data), "sha256": sha256}
        _gc_uploads(keep=nonce)
        log(f"upload {nonce}: {safe} {w}x{h} {len(data)}B")
        send_json(self, {"ok": True, "nonce": nonce, "width": w, "height": h,
                         "size": len(data), "name": safe, "sha256": sha256})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        if p.startswith("/api/") and not self._require_origin():
            return
        if p.startswith("/api/worker/") and not self._require_worker():
            return
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
                    # Remove only multipart framing, never valid trailing file bytes.
                    if body.endswith(b"\r\n"):
                        body = body[:-2]
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
            if clen > MAX_REF_VIDEO_BYTES:
                # Avoid buffering a rejected large body just to return an error.
                self.rfile.read(min(clen, 64 * 1024))
                send_json(self, {"ok": False, "error": f"파일 초과 (최대 {MAX_REF_VIDEO_BYTES//1048576}MB)"}, 400)
                return
            raw = self.rfile.read(clen) if clen else b""
            if not raw:
                send_json(self, {"ok": False, "error": "빈 요청"}, 400)
                return
            fname = "refv.mp4"
            if "multipart/form-data" in ctype:
                try:
                    fname, data = extract_multipart_file_field(raw, ctype, "file")
                except ValueError as exc:
                    send_json(self, {"ok": False, "error": str(exc)}, 400)
                    return
            else:
                data = raw
                disp = self.headers.get("Content-Disposition", "")
                fm2 = re.search(r'filename="?([^";]+)"?', disp)
                if fm2:
                    fname = os.path.basename(fm2.group(1))
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
        if p.startswith("/api/worker/"):
            try:
                if p == "/api/worker/heartbeat":
                    worker_id = str(data.get("worker_id") or "")
                    lease_job = str(data.get("job_id") or "")
                    lease_execution = str(data.get("execution_id") or "")
                    lease_token = str(data.get("lease_token") or "")
                    private = {"worker_id", "job_id", "execution_id", "lease_token"}
                    payload = {k: v for k, v in data.items() if k not in private}
                    worker = record_worker_heartbeat(worker_id, payload)
                    renewed = bool(lease_job and renew_rtx5080_lease(
                        lease_job, lease_execution, lease_token
                    ))
                    send_json(self, {"ok": True, "worker": worker, "lease_renewed": renewed})
                    return
                if p == "/api/worker/claim":
                    requeue_expired_rtx5080_jobs()
                    claimed = claim_rtx5080_job()
                    send_json(self, {"ok": True, **(claimed or {"job": None})})
                    return
                jid = str(data.get("job_id") or "")
                execution_id = str(data.get("execution_id") or "")
                lease_token = str(data.get("lease_token") or "")
                if p == "/api/worker/progress":
                    progress = data.get("progress")
                    job = update_rtx5080_progress(jid, execution_id, lease_token, progress)
                    send_json(self, {"ok": True, "job": job})
                    return
                if p == "/api/worker/upload/init":
                    result = begin_rtx5080_upload(
                        jid, execution_id, lease_token, data.get("size"),
                        str(data.get("sha256") or "").lower(),
                    )
                    send_json(self, {"ok": True, **result})
                    return
                if p == "/api/worker/upload/chunk":
                    try:
                        chunk = base64.b64decode(str(data.get("data") or ""), validate=True)
                    except Exception as exc:
                        raise ValueError("invalid base64 chunk") from exc
                    result = append_rtx5080_upload(
                        jid, execution_id, lease_token, data.get("offset"), chunk,
                        str(data.get("chunk_sha256") or "").lower(),
                    )
                    send_json(self, {"ok": True, **result})
                    return
                if p == "/api/worker/upload/complete":
                    job = complete_rtx5080_upload(jid, execution_id, lease_token)
                    send_json(self, {"ok": True, "job": job})
                    return
                if p == "/api/worker/fail":
                    result = fail_rtx5080_job(
                        jid, execution_id, lease_token, data.get("error"),
                        retryable=data.get("retryable", True),
                    )
                    send_json(self, {"ok": True, **result})
                    return
                send_json(self, {"ok": False, "error": "worker route not found"}, 404)
            except PermissionError as exc:
                send_json(self, {"ok": False, "error": str(exc), "code": "STALE_LEASE"}, 409)
            except ValueError as exc:
                send_json(self, {"ok": False, "error": str(exc), "code": "INVALID_WORKER_PAYLOAD"}, 400)
            except Exception as exc:
                log(f"worker API error: {type(exc).__name__}: {str(exc)[:180]}")
                send_json(self, {"ok": False, "error": "worker operation failed"}, 500)
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
            worker_target = str(data.get("worker_target") or "pgx").strip().lower()
            if worker_target not in ("pgx", "rtx5080"):
                send_json(self, {"ok": False, "error": "worker_target은 pgx 또는 rtx5080이어야 합니다"}, 400)
                return
            if worker_target == "rtx5080" and not rtx5080_worker_status()["eligible"]:
                send_json(self, {
                    "ok": False,
                    "error": "RTX 5080이 오프라인이거나 MiniMax H3 준비가 완료되지 않았습니다",
                    "code": "RTX5080_OFFLINE",
                    "worker": rtx5080_worker_status(),
                }, 409)
                return
            # 참조 파일은 이 짧은 접수 요청에서 ComfyUI로 전송하지 않는다.
            # 대기열 워커가 작업을 시작할 때 전송해야 Vercel/Railway HTTP
            # 연결 제한과 실제 장시간 생성 수명이 완전히 분리된다.
            image_name = ""
            video_name = ""
            image_source_path = ""
            video_source_path = ""
            image_source_name = ""
            video_source_name = ""
            image_source_sha256 = ""
            video_source_sha256 = ""
            image_source_size = None
            video_source_size = None
            jid = str(uuid.uuid4())[:8]
            upload_nonce = (data.get("image") or "").strip()
            ref_mode = str(data.get("ref_mode") or "").strip()
            if mode == "i2v":
                if not upload_nonce and ref_mode == "fixed":
                    ref = _load_ref()
                    if not ref or not os.path.isfile(_ref_path()):
                        send_json(self, {"ok": False, "error": "고정 참조가 등록되지 않았습니다 — 참조 이미지를 먼저 등록해 주세요"}, 400)
                        return
                    image_source_path = job_input_path(jid, ".png")
                    image_source_name = f"h3web_ref_{jid}.png"
                    meta = snapshot_reference_input(_ref_path(), image_source_path, ref.get("name") or "fixed-reference.png")
                    image_source_sha256 = meta["sha256"]
                    image_source_size = meta["size"]
                elif not upload_nonce:
                    refv = _load_refv()
                    if not refv or not os.path.isfile(_refv_video_path()):
                        send_json(self, {"ok": False, "error": "이미지를 먼저 업로드하거나 고정 참조(이미지/동영상)를 선택해 주세요 (I2V)"}, 400)
                        return
                    video_source_path = job_input_path(jid, ".mp4")
                    video_source_name = f"h3web_refv_{jid}.mp4"
                    meta = snapshot_reference_input(_refv_video_path(), video_source_path, refv.get("name") or "fixed-reference.mp4")
                    video_source_sha256 = meta["sha256"]
                    video_source_size = meta["size"]
                else:
                    with UPLOAD_LOCK:
                        up = UPLOADED.get(upload_nonce)
                    if not up or not os.path.isfile(up.get("path", "")):
                        send_json(self, {"ok": False, "error": "이미지가 만료되었습니다 — 다시 업로드해 주세요"}, 400)
                        return
                    # 업로드 임시파일은 GC와 긴 대기열의 영향을 받지 않도록
                    # job 소유 입력으로 빠르게 복사한다 (네트워크 작업 없음).
                    image_source_path = job_input_path(jid, ".png")
                    meta = snapshot_reference_input(up["path"], image_source_path, up.get("name") or "selected-image.png")
                    image_source_name = f"h3web_{jid}.png"
                    image_source_sha256 = meta["sha256"]
                    image_source_size = meta["size"]
                    requested_hash = str(data.get("image_sha256") or "").lower()
                    requested_size = data.get("image_size")
                    if requested_hash and requested_hash != image_source_sha256:
                        os.remove(image_source_path)
                        send_json(self, {"ok": False, "error": "선택 이미지 검증 실패(SHA-256 불일치) — 다시 선택해 주세요"}, 409)
                        return
                    if requested_size is not None:
                        try:
                            size_matches = int(requested_size) == image_source_size
                        except (TypeError, ValueError):
                            size_matches = False
                        if not size_matches:
                            os.remove(image_source_path)
                            send_json(self, {"ok": False, "error": "선택 이미지 검증 실패(크기 불일치) — 다시 선택해 주세요"}, 409)
                            return
            try:
                seconds = normalize_generation_seconds(data.get("seconds"), mode)
            except ValueError as exc:
                cleanup_job_input_snapshots(image_source_path, video_source_path)
                send_json(self, {"ok": False, "error": str(exc)}, 400)
                return
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
            # 카메라 모션 LoRA: "off"|"1000"|"3000" — 다른 값은 off로 처리
            cam_motion = data.get("cam_motion")
            cam_motion = cam_motion if cam_motion in ("1000", "3000") else ""
            # LoRA 강도: 실수만 허용, 부동/문자열/bool은 None → 기본값
            def _num(v):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return None
                f = float(v)
                if f != f or f in (float("inf"), float("-inf")):
                    return None
                return max(0.0, min(2.0, f))
            realism_strength = _num(data.get("realism_strength"))
            cam_strength = _num(data.get("cam_strength"))
            try:
                width = int(data.get("width", 1344))
                height = int(data.get("height", 768))
                seed = int(data.get("seed", -1))
            except (TypeError, ValueError):
                cleanup_job_input_snapshots(image_source_path, video_source_path)
                send_json(self, {"ok": False, "error": "해상도와 시드는 정수여야 합니다"}, 400)
                return
            cfg = {
                "worker_target": worker_target,
                "mode": mode,
                "prompt": prompt,
                "negative": (data.get("negative") or "").strip(),
                "width": width,
                "height": height,
                "seconds": seconds,
                "strategy": strategy,
                "seg_seconds": seg_seconds,
                "steps": steps,
                "seed": seed,
                "filename": fname,
                "image_name": image_name,
                "video_name": video_name,
                "image_source_path": image_source_path,
                "video_source_path": video_source_path,
                "image_source_name": image_source_name,
                "video_source_name": video_source_name,
                "image_source_sha256": image_source_sha256,
                "video_source_sha256": video_source_sha256,
                "image_source_size": image_source_size,
                "video_source_size": video_source_size,
                "image_source_client_name": str(data.get("image_name") or "")[:255],
                "image_source_client_last_modified": data.get("image_last_modified"),
                "realism_lora": realism_lora,
                "cam_motion": cam_motion,
                "realism_strength": realism_strength,
                "cam_strength": cam_strength,
            }
            # worker별 admission slot을 먼저 예약한다. 따라서 PGX와 RTX 큐는
            # 각각 최대 5개이며 동시에 들어온 요청도 서로 용량을 침범하지 않는다.
            global QUEUE_RESERVATIONS
            queue_full = False
            with QUEUE_LOCK:
                pending_total = (
                    queued_jobs_for_target(JOBS, QUEUE, worker_target)
                    + QUEUE_RESERVATIONS[worker_target]
                )
                if pending_total >= MAX_PENDING_JOBS:
                    queue_full = True
                else:
                    QUEUE_RESERVATIONS[worker_target] += 1
            if queue_full:
                cleanup_job_input_snapshots(image_source_path, video_source_path)
                send_json(self, {"ok": False, "error": f"{worker_target} 대기열이 가득 찼습니다 (최대 5개). 실행 중인 작업이 끝난 뒤 다시 시도해 주세요.",
                                 "code": "QUEUE_FULL", "worker_target": worker_target,
                                 "queue_pending": pending_total, "queue_limit": MAX_PENDING_JOBS}, 429)
                return
            with LOCK:
                JOBS[jid] = {
                    "id": jid, "status": "queued", "created": time.time(),
                    "cfg": cfg, "prompt": prompt,
                    "mode": mode, "worker_target": worker_target,
                    "worker_label": "RTX 5080" if worker_target == "rtx5080" else "PGX Spark",
                    "segments": segments, "total_seconds": seconds,
                    "estimated_seconds": est,
                }
            _save_job(jid)
            # 예약한 worker 큐에 정확히 한 번만 enqueue한다.
            with QUEUE_LOCK:
                QUEUE_RESERVATIONS[worker_target] -= 1
                QUEUE.append(jid)
            log(f"new job {jid} [{mode}]: {prompt[:50]}... {cfg['width']}x{cfg['height']} "
                f"{seconds}s [{strategy}] {segments}seg steps={cfg['steps']}"
                + (f" img={image_name}" if image_name else "")
                + (f" vid={video_name}" if video_name else "")
                + (" realism_lora=on" if realism_lora else " realism_lora=off")
                + (f" (x{cfg['realism_strength']})" if cfg.get("realism_strength") is not None else "")
                + (f" cam_motion={cam_motion}" if cam_motion else "")
                + (f" (x{cfg['cam_strength']})" if cfg.get("cam_strength") is not None else ""))
            send_json(self, {
                "ok": True, "job": jid, "worker_target": worker_target,
                "worker_label": "RTX 5080" if worker_target == "rtx5080" else "PGX Spark",
                "segments": segments, "total_seconds": seconds,
                "strategy": strategy, "seg_seconds": seg_seconds,
                "steps": steps,
                "estimated_seconds": est,
                "message": f"{segments}개 세그먼트, 예상 {est}초"
            })
        elif p.startswith("/api/cancel/"):
            jid = p.split("/")[3]
            if cancel_queued_job(jid):
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

