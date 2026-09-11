#!/usr/bin/env python3
"""ASKI MiniMax H3 outbound worker for a Windows RTX 5080 desktop.

The worker opens outbound HTTPS connections only. It never exposes ComfyUI or a
local inbound port. A fresh exact-profile heartbeat is required before the web UI
can route a job here.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
MANIFEST_PATH = BASE / "model-manifest.json"
HASH_CACHE_PATH = BASE / "hash-cache.json"
GENERATION_MARKER_PATH = BASE / "generation-verified.json"
LOG_PATH = BASE / "worker.log"
WORK_ROOT = BASE / "work"
PROFILE = "minimax-h3-pgx-exact-v1"
WORKER_ID = "desktop-rtx5080"
HEARTBEAT_SECONDS = 5
RANGE_BYTES = 2 * 1024 * 1024
EXPECTED_MODEL_ASSETS = {
    ("diffusion_model", "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors", "96eb49aed36a069f97bc77efe0152a534da3d3b162861e977cd213c04a4fe481"),
    ("text_encoder", "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6"),
    ("video_vae", "vae/minimax_h3_video_vae_fp16.safetensors", "7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522"),
    ("audio_vae", "vae/minimax_h3_audio_vae_fp32.safetensors", "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48"),
    ("turbo_lora", "loras/minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors", "7098acf3ee75028fd9fcd948f50fcc8d995057fabb76f86bd3ca2c0ffc58e409"),
    ("realism_lora", "loras/h3-realism-people-t2v-i2v-r2v.safetensors", "acc529601d2da117fb81179e76c56e488a3beab1171659d305f04fa3655b787e"),
    ("camera_motion_1000", "loras/cam_motion_1000.safetensors", "c126738c887804ace3a4be4a3156fcca70517613c7a7ece4e5938d372419186b"),
    ("camera_motion_3000", "loras/cam_motion_3000.safetensors", "9d7d98d7377f56efed3aa7d507f767936112d208906f49908ec9a8ae912be88b"),
}
OPTIONAL_LORA_FILENAMES = {
    "h3-realism-people-t2v-i2v-r2v.safetensors",
    "better_motion_h3_lora_v1_500.safetensors",
    "ig_tiktok_aesthetic_h3_lora_v1_500.safetensors",
    "Motion_Repair.safetensors",
    "camera_motion_h3_lora_v1_1000_pruned.safetensors",
    "camera_motion_h3_lora_v1_3000_pruned.safetensors",
    "wushu_spatial_physics_clean_3000_pruned.safetensors",
    "wushu_spatial_physics_v2_1000_pruned.safetensors",
}
OPTIONAL_LORA_ALIASES = {
    "camera_motion_h3_lora_v1_1000_pruned.safetensors": "cam_motion_1000.safetensors",
    "camera_motion_h3_lora_v1_3000_pruned.safetensors": "cam_motion_3000.safetensors",
}
REQUIRED_COMFY_CLASSES = {
    "UNETLoader", "CLIPLoader", "VAELoader", "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo", "RandomNoise", "KSamplerSelect", "BasicScheduler",
    "BasicGuider", "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio",
    "CreateVideo", "SaveVideo", "LoraLoaderModelOnly", "LoadImage", "LoadVideo",
}


class WorkerJobCancelled(RuntimeError):
    """The coordinator fenced this execution after a user cancellation."""


def valid_job_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{8}", str(value or "")))


def strict_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeError(f"invalid {label}")
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"invalid {label}") from None


def validate_api_base(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise RuntimeError("api_base must be one HTTPS origin without path, query, or credentials")
    return urllib.parse.urlunsplit(("https", parsed.netloc, "", "", ""))


def validate_comfy_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != 8188
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise RuntimeError("ComfyUI must be bound to http://127.0.0.1:8188")
    return "http://127.0.0.1:8188"


def validate_comfy_args(values) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise RuntimeError("comfy_args must be a list of strings")
    result, index = [], 0
    while index < len(values):
        value = values[index]
        if value in ("--lowvram", "--disable-smart-memory"):
            result.append(value)
            index += 1
            continue
        if value == "--reserve-vram" and index + 1 < len(values):
            try:
                amount = float(values[index + 1])
            except ValueError:
                raise RuntimeError("invalid --reserve-vram value") from None
            if not math.isfinite(amount) or not 0 <= amount <= 8:
                raise RuntimeError("invalid --reserve-vram value")
            result.extend((value, values[index + 1]))
            index += 2
            continue
        raise RuntimeError(f"unsafe or unsupported ComfyUI argument: {value}")
    return result


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        oldurl = getattr(req, "full_url", "")
        raise urllib.error.HTTPError(oldurl, code, "redirect refused", headers, fp)


NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirectHandler())


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON object required: {path}")
    return value


def load_worker_token(config: dict) -> str:
    protected = str(config.get("worker_token_dpapi") or "").strip()
    if protected:
        if os.name != "nt":
            raise RuntimeError("DPAPI worker token requires Windows")
        import ctypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        try:
            encrypted = base64.b64decode(protected, validate=True)
        except ValueError:
            raise RuntimeError("invalid DPAPI worker token encoding") from None
        if not encrypted:
            raise RuntimeError("empty DPAPI worker token")
        buffer = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
        input_blob = DataBlob(len(encrypted), buffer)
        output_blob = DataBlob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DataBlob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_bool
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
        ):
            raise RuntimeError("CryptUnprotectData failed for worker token")
        try:
            token = ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
        finally:
            kernel32.LocalFree(output_blob.pbData)
    elif os.environ.get("H3_ALLOW_PLAINTEXT_WORKER_TOKEN") == "1":
        token = str(config.get("worker_token") or "").strip()
    else:
        raise RuntimeError("config.json requires a DPAPI-protected worker token")
    if len(token) < 32:
        raise RuntimeError("strong worker token required")
    return token


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def installed_optional_loras(models_dir: Path) -> list[str]:
    root = models_dir / "loras"
    return sorted(
        name for name in OPTIONAL_LORA_FILENAMES
        if (root / name).is_file()
        or (OPTIONAL_LORA_ALIASES.get(name) and (root / OPTIONAL_LORA_ALIASES[name]).is_file())
    )


def shared_server_path() -> Path:
    for candidate in (BASE / "server.py", BASE.parent / "server.py"):
        if candidate.is_file():
            return candidate
    raise RuntimeError("server.py missing from worker package")


def verification_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in (MANIFEST_PATH, shared_server_path(), Path(__file__).resolve()):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def generation_marker_valid() -> bool:
    try:
        marker = load_json(GENERATION_MARKER_PATH)
    except Exception:
        return False
    return bool(
        marker.get("verified") is True
        and marker.get("profile") == PROFILE
        and marker.get("fingerprint") == verification_fingerprint()
    )


def validate_package() -> None:
    manifest = load_json(MANIFEST_PATH)
    if manifest.get("profile") != PROFILE:
        raise RuntimeError("model manifest profile mismatch")
    models = manifest.get("models")
    if not isinstance(models, list) or len(models) != 8:
        raise RuntimeError("exact H3 manifest must contain 8 model assets")
    roles, paths, assets = set(), set(), set()
    for item in models:
        if not isinstance(item, dict):
            raise RuntimeError("invalid model manifest entry")
        role, relative = item.get("role"), item.get("relative_path")
        digest, size = item.get("sha256"), item.get("size")
        if not role or role in roles or not relative or relative in paths:
            raise RuntimeError("duplicate/empty model manifest entry")
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise RuntimeError("unsafe model relative path")
        if not isinstance(size, int) or size <= 0:
            raise RuntimeError("invalid model size")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RuntimeError("invalid model sha256")
        if item.get("required") is not True:
            raise RuntimeError("all exact H3 model assets must be required")
        roles.add(role)
        paths.add(relative)
        assets.add((role, relative, digest))
    if assets != EXPECTED_MODEL_ASSETS:
        raise RuntimeError("model manifest does not match the code-anchored exact H3 profile")
    server_path = shared_server_path()
    source = server_path.read_text(encoding="utf-8")
    if "def build_workflow(" not in source or "lora_dirs=None" not in source:
        raise RuntimeError("shared H3 workflow source is incompatible")


def _known_comfy_candidates() -> list[Path]:
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    candidates = [
        home / "ComfyUI", home / "Desktop/ComfyUI", home / "Documents/ComfyUI",
        local / "Programs/ComfyUI", local / "ComfyUI", roaming / "ComfyUI",
    ]
    for drive in "CDEFG":
        candidates += [Path(f"{drive}:/ComfyUI"), Path(f"{drive}:/AI/ComfyUI")]
    return candidates


def discover_models_dir(config: dict) -> Path:
    marker = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    candidates = []
    configured = str(config.get("models_dir") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    comfy_configured = str(config.get("comfy_dir") or "").strip()
    if comfy_configured:
        candidates.append(Path(comfy_configured).expanduser() / "models")
    candidates += [root / "models" for root in _known_comfy_candidates()]
    for candidate in candidates:
        if (candidate / "diffusion_models" / marker).is_file():
            return candidate.resolve()
    raise RuntimeError("exact H3 models directory not found; set models_dir in config.json")


def discover_comfy_root(config: dict, models_dir: Path) -> Path:
    configured = str(config.get("comfy_dir") or "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates += [models_dir.parent, *_known_comfy_candidates()]
    for candidate in candidates:
        if (candidate / "main.py").is_file():
            return candidate.resolve()
    raise RuntimeError("ComfyUI main.py root not found; set comfy_dir in config.json")


def verify_models(models_dir: Path, force: bool = False) -> tuple[bool, list[str]]:
    manifest = load_json(MANIFEST_PATH)
    try:
        cache = load_json(HASH_CACHE_PATH)
    except Exception:
        cache = {}
    cached_files = cache.get("files")
    entries = cached_files if isinstance(cached_files, dict) else {}
    updated, errors = {}, []
    for item in manifest["models"]:
        path = models_dir / item["relative_path"]
        if not path.is_file():
            errors.append(f"missing: {item['relative_path']}")
            continue
        stat = path.stat()
        key = str(path.resolve()).lower()
        cached = entries.get(key) or {}
        digest = cached.get("sha256")
        cache_valid = (
            not force and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and digest == item["sha256"]
        )
        if stat.st_size != item["size"]:
            errors.append(f"size mismatch: {item['relative_path']}")
            continue
        if not cache_valid:
            log(f"SHA-256 확인: {item['relative_path']}")
            digest = sha256_file(path)
        if digest != item["sha256"]:
            errors.append(f"sha256 mismatch: {item['relative_path']}")
            continue
        updated[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest}
    if not errors:
        atomic_json(HASH_CACHE_PATH, {"profile": PROFILE, "files": updated, "verified_at": time.time()})
    return not errors, errors


def model_files_unchanged(models_dir: Path) -> bool:
    try:
        manifest = load_json(MANIFEST_PATH)
        cache = load_json(HASH_CACHE_PATH)
        entries = cache.get("files")
        if not isinstance(entries, dict):
            return False
        for item in manifest["models"]:
            path = models_dir / item["relative_path"]
            stat = path.stat()
            cached = entries.get(str(path.resolve()).lower()) or {}
            if (stat.st_size != item["size"]
                    or cached.get("size") != stat.st_size
                    or cached.get("mtime_ns") != stat.st_mtime_ns
                    or cached.get("sha256") != item["sha256"]):
                return False
        return True
    except Exception:
        return False


def nvidia_info() -> tuple[str, int]:
    command = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
    first = result.stdout.strip().splitlines()[0]
    name, memory = [part.strip() for part in first.rsplit(",", 1)]
    return name, int(float(memory))


def assert_comfy_loopback_only(port: int = 8188) -> None:
    if os.name != "nt":
        return
    query = (
        f"@(Get-NetTCPConnection -State Listen -LocalPort {port} "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalAddress) -join \"`n\""
    )
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", query],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise RuntimeError("unable to verify the ComfyUI listener binding")
    addresses = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not addresses or not addresses.issubset({"127.0.0.1", "::1"}):
        rendered = ", ".join(sorted(addresses)) if addresses else "none"
        raise RuntimeError(f"ComfyUI port {port} is not loopback-only: {rendered}")


class ApiClient:
    def __init__(self, base_url: str, token: str):
        self.base = validate_api_base(base_url)
        if len(str(token or "")) < 32:
            raise RuntimeError("strong worker token required")
        self.token = token

    def request(self, path: str, payload: dict | None = None, timeout: int = 60) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers,
                                         method="POST" if data is not None else "GET")
        try:
            with NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"worker API {exc.code}: {body[:500]}") from exc
        value = json.loads(body or b"{}")
        if not value.get("ok", False):
            raise RuntimeError(str(value.get("error") or "worker API failed"))
        return value

    def download_input(self, job_id: str, kind: str, execution: str, lease: str,
                       destination: Path, expected_size: int, expected_hash: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, offset = hashlib.sha256(), 0
        with destination.open("wb") as target:
            while offset < expected_size:
                end = min(expected_size - 1, offset + RANGE_BYTES - 1)
                headers = {
                    "Authorization": f"Bearer {self.token}",
                    "X-H3-Execution-Id": execution,
                    "X-H3-Lease-Token": lease,
                    "Range": f"bytes={offset}-{end}",
                }
                request = urllib.request.Request(
                    f"{self.base}/api/worker/input/{urllib.parse.quote(job_id, safe='')}/{kind}", headers=headers
                )
                with NO_REDIRECT_OPENER.open(request, timeout=90) as response:
                    block = response.read()
                if len(block) != end - offset + 1:
                    raise RuntimeError("short worker input range")
                target.write(block)
                digest.update(block)
                offset += len(block)
        if offset != expected_size or digest.hexdigest() != expected_hash:
            destination.unlink(missing_ok=True)
            raise RuntimeError("worker input final SHA-256 mismatch")


class ComfyClient:
    def __init__(
        self, url: str, server_path: Path | None = None,
        targeted_interrupt_sha256: str = "",
    ):
        self.url = validate_comfy_url(url)
        self.server_path = Path(server_path) if server_path is not None else None
        self.targeted_interrupt_sha256 = str(targeted_interrupt_sha256 or "").strip().lower()

    def targeted_interrupt_contract_matches(self) -> bool:
        if self.server_path is None or not re.fullmatch(
            r"[0-9a-f]{64}", self.targeted_interrupt_sha256,
        ):
            return False
        try:
            return hmac.compare_digest(
                sha256_file(self.server_path), self.targeted_interrupt_sha256,
            )
        except OSError:
            return False

    def json(self, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url + path, data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method="POST" if body is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")

    def ready(self) -> bool:
        try:
            stats = self.json("/system_stats", timeout=4)
            devices = stats.get("devices") or []
            return bool(devices and "NVIDIA GeForce RTX 5080" in str(devices[0].get("name") or ""))
        except Exception:
            return False

    def assert_runtime(self) -> None:
        if not self.ready():
            raise RuntimeError("ComfyUI is not using the expected RTX 5080")
        assert_comfy_loopback_only()
        objects = self.json("/object_info", timeout=60)
        missing = sorted(REQUIRED_COMFY_CLASSES - set(objects))
        if missing:
            raise RuntimeError("ComfyUI required nodes missing: " + ", ".join(missing))

    def busy(self) -> bool:
        try:
            queue = self.json("/queue", timeout=4)
            return bool(queue.get("queue_running") or queue.get("queue_pending"))
        except Exception:
            return True

    def cancel_prompt(self, prompt_id: str) -> bool:
        """Delete pending work; interrupt running work only with a pinned contract."""
        prompt_id = str(prompt_id or "")
        if not prompt_id:
            return False
        errors = []
        try:
            self.json("/queue", {"delete": [prompt_id]}, timeout=10)
        except Exception as exc:
            errors.append(exc)
        try:
            queue = self.json("/queue", timeout=10)
            running = {
                str(item[1]) for item in queue.get("queue_running") or ()
                if isinstance(item, (list, tuple)) and len(item) > 1
            }
        except Exception as exc:
            errors.append(exc)
            running = set()
        if prompt_id in running:
            if not self.targeted_interrupt_contract_matches():
                errors.append(RuntimeError("ComfyUI targeted interrupt contract is not pinned"))
            else:
                try:
                    self.json("/interrupt", {"prompt_id": prompt_id}, timeout=10)
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise RuntimeError("ComfyUI prompt cancellation incomplete") from errors[0]
        return True

    def fetch_output(self, item: dict, destination: Path) -> None:
        query = urllib.parse.urlencode({
            "filename": item.get("filename", ""),
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(self.url + "/view?" + query, timeout=180) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
        if destination.stat().st_size <= 0:
            raise RuntimeError("empty ComfyUI output")


def comfy_python(comfy_root: Path) -> Path:
    candidates = [
        comfy_root / ".venv/Scripts/python.exe",
        comfy_root / "venv/Scripts/python.exe",
        comfy_root.parent / "python_embeded/python.exe",
        comfy_root / "python_embeded/python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("ComfyUI Python environment not found")


def ensure_comfy(comfy_root: Path, client: ComfyClient, config: dict) -> None:
    if client.ready():
        return
    python = comfy_python(comfy_root)
    main = comfy_root / "main.py"
    if not main.is_file():
        raise RuntimeError("ComfyUI main.py not found")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    configured_args = config.get("comfy_args")
    extra_args = validate_comfy_args(
        configured_args if configured_args is not None else ["--lowvram", "--reserve-vram", "1.5"]
    )
    child_env = dict(os.environ)
    child_env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    subprocess.Popen(
        [str(python), str(main), "--listen", "127.0.0.1", "--port", "8188", *extra_args],
        cwd=str(comfy_root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags, env=child_env,
    )
    deadline = time.time() + 180
    while time.time() < deadline:
        if client.ready():
            return
        time.sleep(3)
    raise RuntimeError("ComfyUI did not become ready within 180 seconds")


def import_shared_server():
    sys.path.insert(0, str(shared_server_path().parent))
    import server  # type: ignore
    return server


def ffmpeg_executable() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("ffmpeg unavailable") from exc


def run_ffmpeg(arguments: list[str], timeout: int = 600, cwd: str | None = None) -> None:
    result = subprocess.run([ffmpeg_executable(), "-y", *arguments], capture_output=True, text=True,
                            timeout=timeout, cwd=cwd)
    if result.returncode:
        raise RuntimeError("ffmpeg failed: " + result.stderr[-500:])


def finish_segments(segments: list[Path], output: Path, duration_seconds: int | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    trim = ["-t", str(int(duration_seconds))] if duration_seconds is not None else []
    if len(segments) == 1:
        run_ffmpeg(["-i", str(segments[0]), "-c:v", "libx264", "-preset", "medium", "-crf", "16",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-r", "24",
                    *trim, "-movflags", "+faststart", str(output)])
        return
    directory = output.parent.resolve()
    if any(path.parent.resolve() != directory or not re.fullmatch(r"segment_[0-9]{2}\.mp4", path.name)
           for path in segments):
        raise RuntimeError("unsafe generated segment path")
    concat = output.with_suffix(".concat.txt")
    concat.write_text("".join(f"file '{path.name}'\n" for path in segments), encoding="utf-8")
    stitched = output.with_suffix(".stitched.mp4")
    try:
        run_ffmpeg(["-f", "concat", "-safe", "1", "-i", concat.name, "-c", "copy", stitched.name],
                   cwd=str(directory))
        run_ffmpeg(["-i", str(stitched), "-c:v", "libx264", "-preset", "medium", "-crf", "16",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-r", "24",
                    *trim, "-movflags", "+faststart", str(output)])
    finally:
        concat.unlink(missing_ok=True)
        stitched.unlink(missing_ok=True)


def copy_job_inputs(api: ApiClient, claim: dict, comfy_root: Path, work: Path) -> dict:
    job, execution, lease = claim["job"], claim["execution_id"], claim["lease_token"]
    cfg = dict(job.get("cfg") or {})
    specs = (
        ("image", "image_source_size", "image_source_sha256", "image_source_name", "image_name"),
        ("video", "video_source_size", "video_source_sha256", "video_source_name", "video_name"),
    )
    for kind, size_key, hash_key, source_key, result_key in specs:
        if not cfg.get(hash_key):
            continue
        source_name = Path(str(cfg.get(source_key) or (kind + ".bin"))).name
        extension = Path(source_name).suffix or (".png" if kind == "image" else ".mp4")
        local = work / f"input_{kind}{extension}"
        api.download_input(job["id"], kind, execution, lease, local,
                           int(cfg[size_key]), str(cfg[hash_key]))
        comfy_name = f"h3_remote_{job['id']}_{kind}{extension}"
        comfy_input = comfy_root / "input" / comfy_name
        comfy_input.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, comfy_input)
        cfg[result_key] = comfy_name
    return cfg


def collect_output(result: dict) -> dict:
    files = []
    for output in (result.get("outputs") or {}).values():
        for key in ("videos", "gifs", "images"):
            for item in output.get(key, []):
                if isinstance(item, dict):
                    files.append(item)
    videos = [item for item in files if str(item.get("filename", "")).lower().endswith(".mp4")]
    if not videos:
        raise RuntimeError("ComfyUI completed without MP4 output")
    return videos[0]


def comfy_execution_failure(result: dict) -> str:
    """Extract a bounded execution error without serializing the prompt graph."""
    status = result.get("status") if isinstance(result, dict) else None
    messages = status.get("messages") if isinstance(status, dict) else None
    for event in messages or ():
        if not isinstance(event, (list, tuple)) or len(event) < 2 or event[0] != "execution_error":
            continue
        payload = event[1] if isinstance(event[1], dict) else {}
        exception_type = re.sub(
            r"[^A-Za-z0-9_.]", "", str(payload.get("exception_type") or "")
        )[:120]
        node_type = re.sub(
            r"[^A-Za-z0-9_.]", "", str(payload.get("node_type") or "")
        )[:120]
        detail = re.sub(
            r"\s+", " ", str(payload.get("exception_message") or "")
        ).strip()[:500]
        identity = " / ".join(part for part in (exception_type, node_type) if part)
        suffix = ": " + detail if detail else ""
        return "ComfyUI execution failed" + (f" [{identity}]" if identity else "") + suffix
    return "ComfyUI execution failed"


def generate_segment(api, comfy: ComfyClient, server, claim: dict, cfg: dict,
                     segment_index: int, segments: int, frames: int, seed: int,
                     destination: Path, comfy_root: Path, models_dir: Path, state=None) -> dict:
    try:
        import websocket  # type: ignore
    except Exception as exc:
        raise RuntimeError("websocket-client is not installed") from exc
    client_id = str(uuid.uuid4())
    workflow = server.build_workflow(
        cfg["prompt"], cfg.get("negative", ""), int(cfg["width"]), int(cfg["height"]),
        frames, int(cfg["steps"]), seed, image_name=cfg.get("image_name", ""),
        video_name=cfg.get("video_name", ""), prefix=f"h3remote/{claim['job']['id']}_s{segment_index:02d}",
        realism_lora=cfg.get("realism_lora", False), cam_motion=cfg.get("cam_motion", ""),
        realism_strength=cfg.get("realism_strength"), cam_strength=cfg.get("cam_strength"),
        lora_options=cfg.get("lora_options"),
        lora_dirs=[str(models_dir / "loras"), str(models_dir / "loras/split_files/loras")],
        strict_loras=True,
    )
    ws = None
    try:
        ws = websocket.create_connection(
            comfy.url.replace("http://", "ws://").replace("https://", "wss://") + "/ws?clientId=" + client_id,
            timeout=3,
        )
    except websocket.WebSocketException as exc:
        log(f"ComfyUI progress websocket unavailable; using measured history only: {exc}")
    queued = comfy.json("/prompt", {"prompt": workflow, "client_id": client_id})
    if queued.get("error") or not queued.get("prompt_id"):
        if ws is not None:
            ws.close()
        raise RuntimeError("ComfyUI queue rejected workflow: " + json.dumps(queued)[:800])
    prompt_id = queued["prompt_id"]
    last_history = 0.0
    deadline = time.monotonic() + 6 * 60 * 60
    completed = False
    try:
        while True:
            if state is not None and not state.lease_ok(claim):
                raise WorkerJobCancelled("coordinator cancelled or fenced this execution")
            if time.monotonic() >= deadline:
                raise RuntimeError("ComfyUI segment exceeded the 6 hour safety timeout")
            if ws is not None:
                try:
                    raw = ws.recv()
                    event = json.loads(raw) if isinstance(raw, str) else {}
                    data = event.get("data") or {}
                    if event.get("type") == "progress" and data.get("prompt_id") == prompt_id:
                        try:
                            api.request("/api/worker/progress", {
                                "job_id": claim["job"]["id"], "execution_id": claim["execution_id"],
                                "lease_token": claim["lease_token"],
                                "progress": {"phase": f"세그먼트 {segment_index + 1}/{segments} 생성 중",
                                             "value": data.get("value"), "max": data.get("max"),
                                             "segment_index": segment_index, "segments": segments},
                            }, timeout=30)
                        except Exception as exc:
                            log(f"measured progress report failed; generation continues under heartbeat lease: {exc}")
                except websocket.WebSocketTimeoutException:
                    pass
                except websocket.WebSocketException as exc:
                    log(f"ComfyUI progress websocket lost; continuing history polling: {exc}")
                    try:
                        ws.close()
                    except Exception:
                        pass
                    ws = None
            else:
                time.sleep(0.2)
            now = time.time()
            if now - last_history < 2:
                continue
            last_history = now
            history = comfy.json("/history/" + urllib.parse.quote(prompt_id), timeout=20)
            if prompt_id not in history:
                continue
            result = history[prompt_id]
            status = result.get("status") or {}
            if status.get("status_str") == "error" or not status.get("completed", False):
                raise RuntimeError(comfy_execution_failure(result))
            if state is not None and not state.lease_ok(claim):
                raise WorkerJobCancelled("coordinator cancelled or fenced this execution")
            item = collect_output(result)
            comfy.fetch_output(item, destination)
            completed = True
            return item
    finally:
        if ws is not None:
            ws.close()
        if not completed:
            try:
                comfy.cancel_prompt(prompt_id)
            except Exception as exc:
                log(f"ComfyUI prompt cancellation failed: {type(exc).__name__}")


class _SmokeApi:
    def request(self, path: str, payload: dict | None = None, timeout: int = 60) -> dict:
        return {"ok": True}


def run_local_generation_smoke(comfy: ComfyClient, server, comfy_root: Path, models_dir: Path) -> None:
    if generation_marker_valid():
        log("기존 exact-H3 generation verification marker 확인")
        return
    work = WORK_ROOT / ("smoke-" + uuid.uuid4().hex[:8])
    work.mkdir(parents=True, exist_ok=False)
    destination = work / "smoke.mp4"
    claim = {
        "job": {"id": "00000000"},
        "execution_id": "local-smoke",
        "lease_token": "local-smoke",
    }
    cfg = {
        "prompt": "A calm ocean horizon at sunrise, stable camera, natural light",
        "negative": "text, watermark, logo, low quality",
        "width": 768, "height": 432, "steps": 4,
        "image_name": "", "video_name": "", "realism_lora": False,
        "cam_motion": "", "realism_strength": None, "cam_strength": None,
    }
    output_item = None
    try:
        output_item = generate_segment(
            _SmokeApi(), comfy, server, claim, cfg, 0, 1,
            server.snap_len(1), 5080, destination, comfy_root, models_dir,
        )
        run_ffmpeg(["-v", "error", "-i", str(destination), "-f", "null", "-"], timeout=900)
        atomic_json(GENERATION_MARKER_PATH, {
            "verified": True,
            "profile": PROFILE,
            "fingerprint": verification_fingerprint(),
            "verified_at": time.time(),
            "smoke": {"width": 768, "height": 432, "seconds": 1, "steps": 4, "seed": 5080},
        })
    finally:
        if isinstance(output_item, dict):
            relative = Path(str(output_item.get("subfolder") or "")) / str(output_item.get("filename") or "")
            candidate = (comfy_root / "output" / relative).resolve()
            output_root = (comfy_root / "output").resolve()
            try:
                candidate.relative_to(output_root)
                candidate.unlink(missing_ok=True)
            except (ValueError, OSError):
                pass
        shutil.rmtree(work, ignore_errors=True)


def upload_final(api: ApiClient, claim: dict, path: Path) -> None:
    job_id, execution, lease = claim["job"]["id"], claim["execution_id"], claim["lease_token"]
    size, digest = path.stat().st_size, sha256_file(path)
    init = api.request("/api/worker/upload/init", {
        "job_id": job_id, "execution_id": execution, "lease_token": lease,
        "size": size, "sha256": digest,
    }, timeout=60)
    offset = strict_int(init.get("received"), "upload initialization acknowledgement")
    chunk_max = strict_int(init.get("chunk_max"), "upload initialization acknowledgement")
    if not 0 <= offset <= size or not 1 <= chunk_max <= RANGE_BYTES:
        raise RuntimeError("invalid upload initialization acknowledgement")
    with path.open("rb") as stream:
        stream.seek(offset)
        while offset < size:
            chunk = stream.read(min(chunk_max, size - offset))
            if not chunk:
                raise RuntimeError("local upload source ended unexpectedly")
            result = api.request("/api/worker/upload/chunk", {
                "job_id": job_id, "execution_id": execution, "lease_token": lease,
                "offset": offset, "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
                "data": base64.b64encode(chunk).decode("ascii"),
            }, timeout=120)
            acknowledged = strict_int(result.get("received"), "upload chunk acknowledgement")
            if acknowledged != offset + len(chunk) or acknowledged > size:
                raise RuntimeError("invalid upload chunk acknowledgement")
            offset = acknowledged
    completed = api.request("/api/worker/upload/complete", {
        "job_id": job_id, "execution_id": execution, "lease_token": lease,
    }, timeout=900)
    if (completed.get("job") or {}).get("status") != "done":
        raise RuntimeError("coordinator did not confirm completed upload")


class RuntimeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.claim: dict | None = None
        self.lease_deadline = 0.0
        self.stopping = False

    def try_set_claim(self, claim: dict) -> bool:
        """Atomically reject a late claim after shutdown won the local race."""
        with self.lock:
            if self.stopping or self.claim is not None:
                return False
            self.claim = claim
            self.lease_deadline = time.monotonic() + 45
            return True

    def clear_claim(self, claim: dict | None = None) -> None:
        with self.lock:
            if claim is not None and self.claim is not None:
                if self.claim.get("execution_id") != claim.get("execution_id"):
                    return
            self.claim = None
            self.lease_deadline = 0.0

    def mark_renewed(self, claim: dict) -> None:
        with self.lock:
            if (self.claim and self.claim.get("execution_id") == claim.get("execution_id")):
                self.lease_deadline = time.monotonic() + 45

    def invalidate(self, claim: dict) -> None:
        with self.lock:
            if (self.claim and self.claim.get("execution_id") == claim.get("execution_id")):
                self.lease_deadline = 0.0

    def lease_ok(self, claim: dict) -> bool:
        with self.lock:
            return bool(
                self.claim
                and self.claim.get("execution_id") == claim.get("execution_id")
                and time.monotonic() < self.lease_deadline
            )

    def snapshot(self) -> dict | None:
        with self.lock:
            return dict(self.claim) if self.claim else None

    def request_shutdown(self) -> bool:
        with self.lock:
            if self.claim or self.stopping:
                return False
            self.stopping = True
            return True


def handle_power_command(api: ApiClient, comfy: ComfyClient, state: RuntimeState,
                         command: dict | None) -> bool:
    if not isinstance(command, dict):
        return False
    command_id = str(command.get("id") or "")
    if (not re.fullmatch(r"[0-9a-f]{32}", command_id)
            or command.get("action") != "shutdown"
            or float(command.get("expires_at") or 0) < time.time()):
        return False
    if os.name != "nt" or comfy.busy() or not state.request_shutdown():
        api.request("/api/worker/power/ack", {
            "command_id": command_id, "status": "rejected",
        }, timeout=20)
        return False
    try:
        subprocess.run(
            ["shutdown.exe", "/s", "/t", "15", "/d", "p:0:0",
             "/c", "H3 웹에서 요청한 안전한 종료"],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except Exception:
        # Scheduling failed, so it is safe to reopen claim admission.  The
        # diagnostic ACK is best-effort and must not mask the real failure.
        try:
            api.request("/api/worker/power/ack", {
                "command_id": command_id, "status": "error",
            }, timeout=20)
        except Exception as ack_error:
            log(f"shutdown error acknowledgement failed: {type(ack_error).__name__}")
        with state.lock:
            state.stopping = False
        raise

    # From this point Windows will power off even if the network disappears.
    # Keep the worker permanently fenced and never downgrade the command or
    # resume claim polling merely because acknowledgement delivery failed.
    try:
        api.request("/api/worker/power/ack", {
            "command_id": command_id, "status": "scheduled",
        }, timeout=20)
    except Exception as ack_error:
        log(f"scheduled shutdown acknowledgement failed: {type(ack_error).__name__}")
    log("authenticated power-off scheduled")
    return True


def heartbeat_loop(api: ApiClient, comfy: ComfyClient, gpu: str, vram: int,
                   models_dir: Path, state: RuntimeState) -> None:
    while not state.stopping:
        claim = state.snapshot()
        current_models_ready = model_files_unchanged(models_dir)
        generation_verified = current_models_ready and generation_marker_valid()
        payload = {
            "worker_id": WORKER_ID, "gpu": gpu, "vram_mib": vram,
            "comfy_up": comfy.ready(), "model_ready": current_models_ready,
            "generation_verified": generation_verified,
            "busy": bool(claim) or comfy.busy(), "modes": ["t2v", "i2v"],
            "model_profile": PROFILE, "lora_files": installed_optional_loras(models_dir),
        }
        if claim:
            payload.update(job_id=claim["job"]["id"], execution_id=claim["execution_id"],
                           lease_token=claim["lease_token"])
        try:
            response = api.request("/api/worker/heartbeat", payload, timeout=20)
            handle_power_command(api, comfy, state, response.get("power_command"))
            if claim and response.get("lease_renewed"):
                state.mark_renewed(claim)
            elif claim:
                state.invalidate(claim)
                log("lease renewal rejected; stopping current execution safely")
        except Exception as exc:
            log(f"heartbeat failed: {exc}")
        time.sleep(HEARTBEAT_SECONDS)


def assert_claim_active(state: RuntimeState, claim: dict) -> None:
    if state is not None and not state.lease_ok(claim):
        raise WorkerJobCancelled("coordinator cancelled or fenced this execution")


def process_claim(api: ApiClient, comfy: ComfyClient, server, claim: dict,
                  comfy_root: Path, models_dir: Path, state: RuntimeState) -> None:
    job = claim["job"]
    if not valid_job_id(str(job.get("id") or "")):
        raise RuntimeError("unsafe coordinator job id")
    assert_claim_active(state, claim)
    work = WORK_ROOT / job["id"]
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    cfg = copy_job_inputs(api, claim, comfy_root, work)
    assert_claim_active(state, claim)
    total_seconds = min(int(cfg["seconds"]), int(server.MAX_SECONDS))
    strategy = cfg.get("strategy", server.STRATEGY_SPLIT)
    segment_seconds = int(cfg.get("seg_seconds", server.SEG_SECONDS))
    strategy, segment_seconds = server.normalize_worker_strategy(
        "rtx5080", total_seconds, strategy, segment_seconds,
    )
    segment_frames = server.worker_segment_frame_plan(
        "rtx5080", total_seconds, segment_seconds, strategy,
    )
    segments = len(segment_frames)
    seed_base = int(cfg.get("seed", -1))
    if seed_base < 0:
        seed_base = int.from_bytes(os.urandom(6), "big")
    outputs = []
    for index, frames in enumerate(segment_frames):
        assert_claim_active(state, claim)
        output = work / f"segment_{index:02d}.mp4"
        generate_segment(api, comfy, server, claim, cfg, index, segments, frames,
                         seed_base + index, output, comfy_root, models_dir, state)
        assert_claim_active(state, claim)
        outputs.append(output)
    final = work / f"{job['id']}.mp4"
    assert_claim_active(state, claim)
    finish_segments(outputs, final, duration_seconds=total_seconds)
    assert_claim_active(state, claim)
    upload_final(api, claim, final)
    shutil.rmtree(work, ignore_errors=True)


def cleanup_claim_files(comfy_root: Path, job_id: str) -> None:
    if not job_id or any(ch not in "0123456789abcdef" for ch in job_id.lower()):
        return
    for path in (comfy_root / "input").glob(f"h3_remote_{job_id}_*"):
        if path.is_file():
            path.unlink(missing_ok=True)
    shutil.rmtree(WORK_ROOT / job_id, ignore_errors=True)


def acquire_singleton():
    if os.name != "nt":
        return None
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Global\\ASKI_H3_RTX5080_WORKER_V1")
    if not handle:
        raise RuntimeError("unable to create the machine-wide H3 worker mutex")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise RuntimeError("another H3 worker process is already running")
    return handle


def run_worker(force_hash: bool = False) -> None:
    _singleton_lock = acquire_singleton()
    validate_package()
    config = load_json(CONFIG_PATH)
    api_base = validate_api_base(config.get("api_base") or "")
    token = load_worker_token(config)
    models_dir = discover_models_dir(config)
    comfy_root = discover_comfy_root(config, models_dir)
    gpu, vram = nvidia_info()
    if gpu != "NVIDIA GeForce RTX 5080" or vram < 15000:
        raise RuntimeError(f"unsupported GPU: {gpu} ({vram} MiB)")
    model_ready, errors = verify_models(models_dir, force=force_hash)
    if not model_ready:
        raise RuntimeError("exact H3 model validation failed: " + "; ".join(errors))
    if not generation_marker_valid():
        raise RuntimeError("exact H3 generation smoke is not verified; run --self-test first")
    comfy = ComfyClient(
        str(config.get("comfy_url") or "http://127.0.0.1:8188"),
        server_path=comfy_root / "server.py",
        targeted_interrupt_sha256=str(config.get("targeted_interrupt_server_sha256") or ""),
    )
    ensure_comfy(comfy_root, comfy, config)
    comfy.assert_runtime()
    server = import_shared_server()
    api = ApiClient(api_base, token)
    state = RuntimeState()
    heartbeat = threading.Thread(target=heartbeat_loop,
        args=(api, comfy, gpu, vram, models_dir, state), daemon=True)
    heartbeat.start()
    log(f"ready: {gpu} · {vram} MiB · {PROFILE} · {comfy_root}")
    while not state.stopping:
        if not comfy.ready():
            try:
                ensure_comfy(comfy_root, comfy, config)
                comfy.assert_runtime()
                log("ComfyUI recovered and revalidated")
            except Exception as exc:
                log(f"ComfyUI recovery failed: {exc}")
                time.sleep(5)
                continue
        if comfy.busy():
            time.sleep(3)
            continue
        try:
            claim = api.request("/api/worker/claim", {}, timeout=30)
            if not claim.get("job"):
                time.sleep(2)
                continue
            if not state.try_set_claim(claim):
                # The heartbeat thread may have accepted shutdown while this
                # claim response was in flight. Return the exact lease instead
                # of starting generation during shutdown.
                api.request("/api/worker/fail", {
                    "job_id": claim["job"]["id"], "execution_id": claim["execution_id"],
                    "lease_token": claim["lease_token"],
                    "error": "worker shutdown won before local claim assignment",
                    "retryable": True,
                }, timeout=30)
                continue
            log(f"claimed {claim['job']['id']}")
            try:
                process_claim(api, comfy, server, claim, comfy_root, models_dir, state)
                log(f"completed {claim['job']['id']}")
            except WorkerJobCancelled:
                log(f"cancelled {claim['job']['id']}")
            except Exception as exc:
                log(f"job {claim['job']['id']} failed: {exc}")
                try:
                    api.request("/api/worker/fail", {
                        "job_id": claim["job"]["id"], "execution_id": claim["execution_id"],
                        "lease_token": claim["lease_token"], "error": str(exc)[:800], "retryable": True,
                    }, timeout=30)
                except Exception as report_exc:
                    log(f"failure report rejected: {report_exc}")
            finally:
                cleanup_claim_files(comfy_root, str(claim["job"].get("id") or ""))
                state.clear_claim(claim)
        except Exception as exc:
            log(f"claim loop error: {exc}")
            time.sleep(5)


def self_test(force_hash: bool = False) -> None:
    validate_package()
    config = load_json(CONFIG_PATH)
    models_dir = discover_models_dir(config)
    comfy_root = discover_comfy_root(config, models_dir)
    gpu, vram = nvidia_info()
    if gpu != "NVIDIA GeForce RTX 5080" or vram < 15000:
        raise RuntimeError(f"RTX 5080 readiness failed: {gpu}, {vram} MiB")
    ok, errors = verify_models(models_dir, force=force_hash)
    if not ok:
        raise RuntimeError("model readiness failed: " + "; ".join(errors))
    comfy = ComfyClient(
        str(config.get("comfy_url") or "http://127.0.0.1:8188"),
        server_path=comfy_root / "server.py",
        targeted_interrupt_sha256=str(config.get("targeted_interrupt_server_sha256") or ""),
    )
    ensure_comfy(comfy_root, comfy, config)
    comfy.assert_runtime()
    server = import_shared_server()
    run_local_generation_smoke(comfy, server, comfy_root, models_dir)
    if not generation_marker_valid():
        raise RuntimeError("generation verification marker was not created")
    log(f"SELF-TEST OK · {gpu} · {vram} MiB · exact H3 hashes · actual generation/decode · ComfyUI ready")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-package", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--force-hash", action="store_true")
    args = parser.parse_args()
    try:
        if args.validate_package:
            validate_package()
            print("PACKAGE VALID")
        elif args.self_test:
            self_test(force_hash=args.force_hash)
        else:
            run_worker(force_hash=args.force_hash)
        return 0
    except Exception as exc:
        log(f"FATAL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
