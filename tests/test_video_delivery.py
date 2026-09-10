import http.client
import base64
import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import call, patch

import backend_proxy
import server


class VideoDeliveryTests(unittest.TestCase):
    ORIGIN_SECRET = "unit-test-origin-secret"

    def load_windows_worker(self):
        path = Path(__file__).resolve().parents[1] / "windows-worker" / "h3_worker.py"
        spec = importlib.util.spec_from_file_location("h3_worker_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_backend_rejects_direct_api_access_without_origin_secret(self):
        with patch.object(server, "ORIGIN_SECRET", self.ORIGIN_SECRET):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                conn.request("GET", "/api/health")
                response = conn.getresponse()
                self.assertEqual(response.status, 401)
                self.assertEqual(json.loads(response.read())["error"], "unauthorized origin")
                conn.close()

                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                conn.request("GET", "/api/health", headers={
                    server.ORIGIN_HEADER: self.ORIGIN_SECRET,
                })
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())["ok"])
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_proxy_uses_authoritative_stable_funnel_even_with_stale_environment_override(self):
        proxy_path = Path(__file__).resolve().parents[1] / "backend_proxy.py"
        spec = importlib.util.spec_from_file_location("backend_proxy_stale_env_probe", proxy_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"H3_BACKEND": "https://retired-quick-tunnel.invalid"}):
            assert spec.loader is not None
            spec.loader.exec_module(module)
        self.assertEqual(module.BACKEND, "https://thinkstationpgx-11d3.tailccac79.ts.net")

    def test_proxy_fails_closed_without_origin_secret(self):
        started = []
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/jobs", "QUERY_STRING": "", "wsgi.input": None}
        with patch.object(backend_proxy, "ORIGIN_SECRET", ""), \
             patch("urllib.request.urlopen") as urlopen:
            result = backend_proxy.handler(env, lambda status, headers: started.extend([status, dict(headers)]))
        self.assertEqual(started[0], "503")
        self.assertEqual(json.loads(b"".join(result))["error"], "origin authentication unavailable")
        urlopen.assert_not_called()

    def test_proxy_logs_only_the_upstream_exception_class_for_502_diagnosis(self):
        started = []
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/jobs", "QUERY_STRING": "", "wsgi.input": None}
        with patch.object(backend_proxy, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch("urllib.request.urlopen", side_effect=urllib.error.URLError("private detail")), \
             patch("builtins.print") as logged:
            result = backend_proxy.handler(env, lambda status, headers: started.extend([status, dict(headers)]))
        self.assertEqual(started[0], "502")
        self.assertEqual(json.loads(b"".join(result))["error"], "backend unavailable")
        logged.assert_called_once_with("H3 upstream error: URLError/str", flush=True)

    def test_proxy_injects_server_side_origin_secret(self):
        seen = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}
            def read(self, _size):
                if getattr(self, "done", False):
                    return b""
                self.done = True
                return b'{"ok":true}'
            def close(self):
                pass

        def fake_open(request, timeout):
            seen["secret"] = dict((key.lower(), value) for key, value in request.header_items()).get(
                "x-h3-origin-token"
            )
            return FakeResponse()

        started = []
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/jobs", "QUERY_STRING": "", "wsgi.input": None,
               "HTTP_X_H3_ORIGIN_TOKEN": "attacker-controlled"}
        with patch.object(backend_proxy, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch("urllib.request.urlopen", fake_open):
            result = backend_proxy.handler(env, lambda status, headers: started.extend([status, dict(headers)]))
            self.assertEqual(b"".join(result), b'{"ok":true}')
        self.assertEqual(started[0], "200")
        self.assertEqual(seen["secret"], self.ORIGIN_SECRET)

    def test_proxy_authenticates_worker_bearer_and_uses_separate_upstream_secret(self):
        seen = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}
            def read(self, _size):
                if getattr(self, "done", False):
                    return b""
                self.done = True
                return b'{"ok":true}'
            def close(self):
                pass

        def fake_open(request, timeout):
            seen.update(dict((key.lower(), value) for key, value in request.header_items()))
            return FakeResponse()

        base = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/worker/input/job/image",
                "QUERY_STRING": "", "wsgi.input": None,
                "HTTP_X_H3_WORKER_TOKEN": "attacker-controlled"}
        started = []
        with patch.object(backend_proxy, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch.object(backend_proxy, "WORKER_SECRET", "worker-secret"), \
             patch("urllib.request.urlopen") as urlopen:
            result = backend_proxy.handler(base, lambda status, headers: started.extend([status, dict(headers)]))
        self.assertEqual(started[0], "401")
        self.assertFalse(json.loads(b"".join(result))["ok"])
        urlopen.assert_not_called()

        started = []
        allowed = dict(base, HTTP_AUTHORIZATION="Bearer worker-secret")
        with patch.object(backend_proxy, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch.object(backend_proxy, "WORKER_SECRET", "worker-secret"), \
             patch("urllib.request.urlopen", fake_open):
            result = backend_proxy.handler(allowed, lambda status, headers: started.extend([status, dict(headers)]))
            self.assertEqual(b"".join(result), b'{"ok":true}')
        self.assertEqual(started[0], "200")
        self.assertEqual(seen["x-h3-origin-token"], self.ORIGIN_SECRET)
        self.assertEqual(seen["x-h3-worker-token"], self.ORIGIN_SECRET)
        self.assertNotEqual(seen["x-h3-worker-token"], "worker-secret")
        self.assertNotIn("authorization", seen)

        class ProxyToServerRequest:
            headers = {
                server.ORIGIN_HEADER: seen["x-h3-origin-token"],
                server.WORKER_HEADER: seen["x-h3-worker-token"],
            }

        request = ProxyToServerRequest()
        with patch.object(server, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch.object(server, "WORKER_PROXY_SECRET", self.ORIGIN_SECRET):
            self.assertTrue(server.Handler._origin_authorized(request))
            self.assertTrue(server.Handler._worker_authorized(request))

    def test_pgx_direct_worker_bearer_is_independent_and_constant_time_checked(self):
        class DirectWorkerRequest:
            headers = {"Authorization": "Bearer direct-worker-secret-0123456789abcdef"}

        request = DirectWorkerRequest()
        with patch.object(server, "DIRECT_WORKER_SECRET", "direct-worker-secret-0123456789abcdef"):
            self.assertTrue(server.Handler._worker_authorized(request))
            request.headers["Authorization"] = "Bearer wrong-worker-secret-0123456789abcdef"
            self.assertFalse(server.Handler._worker_authorized(request))

        root = Path(__file__).resolve().parents[1]
        installer = (root / "windows-worker" / "Install-H3Worker.ps1").read_text(encoding="utf-8")
        unit = (root / "h3-web-backend.service").read_text(encoding="utf-8")
        self.assertIn("api_base = 'https://thinkstationpgx-11d3.tailccac79.ts.net'", installer)
        self.assertIn('test -n "$H3_DIRECT_WORKER_TOKEN"', unit)

    def test_prompt_clear_button_erases_positive_and_negative_in_one_action(self):
        source = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="promptClear"', source)
        self.assertIn("$('#prompt').value='';", source)
        self.assertIn("$('#negative').value='';", source)
        self.assertIn("$('#promptClear').onclick", source)

    def test_rtx5080_worker_is_eligible_only_while_fresh_and_ready(self):
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080",
            "vram_mib": 16303,
            "comfy_up": True,
            "model_ready": True,
            "generation_verified": True,
            "busy": False,
            "modes": ["t2v", "i2v"],
            "model_profile": "minimax-h3-pgx-exact-v1",
        }
        with patch.dict(server.WORKERS, {}, clear=True):
            public = server.record_worker_heartbeat(
                server.RTX5080_WORKER_ID, heartbeat, now=100.0
            )
            self.assertTrue(public["online"])
            self.assertTrue(public["eligible"])
            self.assertTrue(public["generation_verified"])
            self.assertNotIn("token", public)
            unverified = dict(heartbeat, generation_verified=False)
            public = server.record_worker_heartbeat(
                server.RTX5080_WORKER_ID, unverified, now=101.0
            )
            self.assertTrue(public["online"])
            self.assertFalse(public["eligible"])
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=100.0)
            self.assertTrue(server.rtx5080_worker_status(now=114.9)["eligible"])
            self.assertFalse(server.rtx5080_worker_status(now=115.1)["online"])
            self.assertFalse(server.rtx5080_worker_status(now=115.1)["eligible"])

            not_ready = dict(heartbeat, comfy_up=False)
            public = server.record_worker_heartbeat(
                server.RTX5080_WORKER_ID, not_ready, now=200.0
            )
            self.assertTrue(public["online"])
            self.assertFalse(public["eligible"])

    def test_pgx_queue_skips_rtx5080_jobs_and_remote_claim_is_lease_fenced(self):
        jobs = {
            "remote": {"id": "remote", "status": "queued", "cfg": {"worker_target": "rtx5080", "mode": "t2v"}},
            "pgx": {"id": "pgx", "status": "queued", "cfg": {"worker_target": "pgx", "mode": "t2v"}},
        }
        queue = ["remote", "pgx"]
        self.assertEqual(server.pop_next_pgx_job(jobs, queue), "pgx")
        self.assertEqual(queue, ["remote"])

        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        tokens = iter(["first-execution", "first-secret", "second-execution", "second-secret"])
        with patch.dict(server.JOBS, {"remote": jobs["remote"]}, clear=True), \
             patch.object(server, "QUEUE", ["remote"]), \
             patch.dict(server.WORKERS, {}, clear=True), \
             patch.object(server, "_save_job"):
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=100.0)
            first = server.claim_rtx5080_job(now=100.0, token_factory=lambda: next(tokens))
            self.assertEqual(first["job"]["id"], "remote")
            self.assertNotIn("lease_sha256", first["job"])
            self.assertTrue(server.validate_rtx5080_lease(
                "remote", first["execution_id"], first["lease_token"], now=114.0
            ))
            self.assertFalse(server.renew_rtx5080_lease(
                "remote", first["execution_id"], first["lease_token"], now=161.0
            ))

            self.assertEqual(server.requeue_expired_rtx5080_jobs(now=161.0), ["remote"])
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=162.0)
            second = server.claim_rtx5080_job(now=162.0, token_factory=lambda: next(tokens))
            self.assertFalse(server.validate_rtx5080_lease(
                "remote", first["execution_id"], first["lease_token"], now=162.0
            ))
            self.assertTrue(server.validate_rtx5080_lease(
                "remote", second["execution_id"], second["lease_token"], now=162.0
            ))

    def test_heartbeat_renews_lease_without_progress_and_worker_failure_retries_once(self):
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        job = {"id": "remote", "status": "queued", "segments": 1,
               "cfg": {"worker_target": "rtx5080", "mode": "t2v", "steps": 6}}
        tokens = iter(["execution-1", "secret-1", "execution-2", "secret-2"])
        with patch.dict(server.JOBS, {"remote": job}, clear=True), \
             patch.object(server, "QUEUE", ["remote"]), \
             patch.dict(server.WORKERS, {}, clear=True), \
             patch.object(server, "_save_job"):
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=10.0)
            first = server.claim_rtx5080_job(now=10.0, token_factory=lambda: next(tokens))
            server.update_rtx5080_progress(
                "remote", first["execution_id"], first["lease_token"],
                {"value": 2, "max": 6, "phase": "영상 생성 중", "segment_index": 0, "segments": 1},
                now=20.0,
            )
            before = dict(server.JOBS["remote"]["progress"])
            self.assertTrue(server.renew_rtx5080_lease(
                "remote", first["execution_id"], first["lease_token"], now=50.0
            ))
            self.assertEqual(server.JOBS["remote"]["progress"], before)
            self.assertTrue(server.validate_rtx5080_lease(
                "remote", first["execution_id"], first["lease_token"], now=100.0
            ))
            retried = server.fail_rtx5080_job(
                "remote", first["execution_id"], first["lease_token"], "first failure", now=51.0
            )
            self.assertTrue(retried["retrying"])
            self.assertEqual(server.JOBS["remote"]["status"], "queued")
            self.assertEqual(server.QUEUE, ["remote"])

            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=52.0)
            second = server.claim_rtx5080_job(now=52.0, token_factory=lambda: next(tokens))
            terminal = server.fail_rtx5080_job(
                "remote", second["execution_id"], second["lease_token"], "second failure", now=53.0
            )
            self.assertFalse(terminal["retrying"])
            self.assertEqual(server.JOBS["remote"]["status"], "error")
            self.assertEqual(server.JOBS["remote"]["error"], "RTX 5080 worker generation failed")
            self.assertEqual(server.QUEUE, [])

    def test_live_server_lease_blocks_second_claim_even_if_heartbeat_reports_idle(self):
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        jobs = {
            "first": {"id": "first", "status": "queued", "cfg": {"worker_target": "rtx5080", "mode": "t2v"}},
            "second": {"id": "second", "status": "queued", "cfg": {"worker_target": "rtx5080", "mode": "t2v"}},
        }
        tokens = iter(["exec1", "lease1", "exec2", "lease2"])
        with patch.dict(server.WORKERS, {}, clear=True), patch.dict(server.JOBS, jobs, clear=True), \
             patch.object(server, "QUEUE", ["first", "second"]), patch.object(server, "_save_job"):
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=10.0)
            first = server.claim_rtx5080_job(now=10.0, token_factory=lambda: next(tokens))
            self.assertEqual(first["job"]["id"], "first")
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=11.0)
            self.assertIsNone(server.claim_rtx5080_job(now=11.0, token_factory=lambda: next(tokens)))
            self.assertEqual(server.JOBS["second"]["status"], "queued")
            self.assertEqual(server.QUEUE, ["second"])

    def test_rtx_readiness_uses_same_vram_threshold_as_claim(self):
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 14999,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        with patch.dict(server.WORKERS, {}, clear=True):
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=10.0)
            self.assertFalse(server.rtx5080_worker_status(now=10.1)["eligible"])
            heartbeat["vram_mib"] = 15000
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=11.0)
            self.assertTrue(server.rtx5080_worker_status(now=11.1)["eligible"])

    def test_non_expiring_lease_sweep_does_not_clear_worker_busy(self):
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": True,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        running = {"id": "remote", "status": "running", "worker_id": server.RTX5080_WORKER_ID,
                   "lease_expires_at": 100.0, "cfg": {"worker_target": "rtx5080"}}
        with patch.dict(server.WORKERS, {}, clear=True), \
             patch.dict(server.JOBS, {"remote": running}, clear=True):
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=10.0)
            self.assertEqual(server.requeue_expired_rtx5080_jobs(now=20.0), [])
            self.assertTrue(server.WORKERS[server.RTX5080_WORKER_ID]["busy"])

    def test_archive_failure_does_not_strand_remote_job_in_finalizing(self):
        payload = b"valid bytes" * 200
        digest = hashlib.sha256(payload).hexdigest()
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        tokens = iter(["execution", "secret"])
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            out = os.path.join(nas, ".h3-web", "work")
            os.makedirs(out)
            job = {"id": "remote", "status": "queued", "cfg": {"worker_target": "rtx5080", "mode": "t2v"}}
            with patch.object(server, "NAS_DIR", nas), patch.object(server, "OUT_DIR", out), \
                 patch.dict(server.JOBS, {"remote": job}, clear=True), patch.object(server, "QUEUE", ["remote"]), \
                 patch.dict(server.WORKERS, {}, clear=True), patch.object(server, "_save_job"), \
                 patch.object(server, "_validate_remote_mp4"), \
                 patch.object(server, "archive_final_to_nas", side_effect=RuntimeError("NAS unavailable")):
                server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=10.0)
                claim = server.claim_rtx5080_job(now=10.0, token_factory=lambda: next(tokens))
                server.begin_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"], len(payload), digest, now=11.0)
                server.append_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"], 0, payload, digest, now=12.0)
                with self.assertRaisesRegex(RuntimeError, "NAS unavailable"):
                    server.complete_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"], now=13.0)
                self.assertEqual(server.JOBS["remote"]["status"], "running")
                self.assertTrue(server.validate_rtx5080_lease("remote", claim["execution_id"], claim["lease_token"], now=14.0))

    def test_remote_completion_retry_returns_existing_done_job(self):
        payload = b"valid bytes" * 200
        digest = hashlib.sha256(payload).hexdigest()
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        tokens = iter(["execution", "secret"])
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            out = os.path.join(nas, ".h3-web", "work")
            os.makedirs(out)
            job = {"id": "remote", "status": "queued", "cfg": {"worker_target": "rtx5080", "mode": "t2v"}}
            with patch.object(server, "NAS_DIR", nas), patch.object(server, "OUT_DIR", out), \
                 patch.dict(server.JOBS, {"remote": job}, clear=True), patch.object(server, "QUEUE", ["remote"]), \
                 patch.dict(server.WORKERS, {}, clear=True), patch.object(server, "_save_job"), \
                 patch.object(server, "_validate_remote_mp4"):
                server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=10.0)
                claim = server.claim_rtx5080_job(now=10.0, token_factory=lambda: next(tokens))
                server.begin_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"], len(payload), digest, now=11.0)
                server.append_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"], 0, payload, digest, now=12.0)
                first = server.complete_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"], now=13.0)
                second = server.complete_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"], now=14.0)
                self.assertEqual(second["status"], "done")
                self.assertEqual(second["sha256"], first["sha256"])

    def test_rtx_progress_is_monotonic_and_stale_lease_cannot_publish(self):
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        job = {"id": "remote", "status": "queued", "segments": 1,
               "cfg": {"worker_target": "rtx5080", "mode": "t2v", "steps": 6}}
        tokens = iter(["execution", "secret"])
        with patch.dict(server.JOBS, {"remote": job}, clear=True), \
             patch.object(server, "QUEUE", ["remote"]), \
             patch.dict(server.WORKERS, {}, clear=True), \
             patch.object(server, "_save_job"):
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=10.0)
            claim = server.claim_rtx5080_job(now=10.0, token_factory=lambda: next(tokens))
            updated = server.update_rtx5080_progress(
                "remote", claim["execution_id"], claim["lease_token"],
                {"value": 2, "max": 6, "phase": "영상 생성 중 <img onerror=alert(1)>", "segment_index": 0, "segments": 1},
                now=20.0,
            )
            self.assertEqual(updated["progress"]["value"], 2)
            self.assertNotIn("<", updated["progress"]["phase"])
            self.assertNotIn(">", updated["progress"]["phase"])
            repeated_sampler = server.update_rtx5080_progress(
                "remote", claim["execution_id"], claim["lease_token"],
                {"value": 1, "max": 6, "phase": "영상 생성 중", "segment_index": 0, "segments": 1},
                now=21.0,
            )
            self.assertEqual(repeated_sampler["progress"]["value"], 2)
            self.assertEqual(repeated_sampler["progress"]["pct"], updated["progress"]["pct"])
            self.assertEqual(server.JOBS["remote"]["lease_expires_at"], 21.0 + server.RTX5080_LEASE_SECONDS)
            with self.assertRaises(PermissionError):
                server.update_rtx5080_progress(
                    "remote", "stale", "wrong",
                    {"value": 3, "max": 6, "phase": "영상 생성 중", "segment_index": 0, "segments": 1},
                    now=22.0,
                )

    def test_remote_mp4_validation_requires_probeable_fully_decodable_video(self):
        with tempfile.TemporaryDirectory() as root:
            valid = os.path.join(root, "valid.mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.2:r=10",
                 "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", valid],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
            server._validate_remote_mp4(valid)
            junk = os.path.join(root, "junk.mp4")
            Path(junk).write_bytes(b"not video" * 200)
            with self.assertRaisesRegex(ValueError, "probing"):
                server._validate_remote_mp4(junk)

    def test_rtx_chunk_upload_is_idempotent_and_publishes_only_after_hash_check(self):
        payload = b"verified remote mp4 bytes"
        digest = hashlib.sha256(payload).hexdigest()
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        tokens = iter(["execution", "secret"])
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            out = os.path.join(nas, ".h3-web", "work")
            os.makedirs(out)
            job = {"id": "remote", "status": "queued", "segments": 1,
                   "total_seconds": 2, "cfg": {"worker_target": "rtx5080", "mode": "t2v"}}
            with patch.object(server, "NAS_DIR", nas), patch.object(server, "OUT_DIR", out), \
                 patch.dict(server.JOBS, {"remote": job}, clear=True), \
                 patch.object(server, "QUEUE", ["remote"]), \
                 patch.dict(server.WORKERS, {}, clear=True), \
                 patch.object(server, "_save_job"), \
                 patch.object(server, "_validate_remote_mp4") as validate_remote_mp4:
                server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=10.0)
                claim = server.claim_rtx5080_job(now=10.0, token_factory=lambda: next(tokens))
                server.begin_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"],
                                            len(payload), digest, now=11.0)
                first = payload[:10]
                server.append_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"],
                                             0, first, hashlib.sha256(first).hexdigest(), now=12.0)
                # Network retry of an already committed range is accepted but not appended twice.
                server.append_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"],
                                             0, first, hashlib.sha256(first).hexdigest(), now=13.0)
                rest = payload[10:]
                server.append_rtx5080_upload("remote", claim["execution_id"], claim["lease_token"],
                                             10, rest, hashlib.sha256(rest).hexdigest(), now=14.0)
                done = server.complete_rtx5080_upload(
                    "remote", claim["execution_id"], claim["lease_token"], now=15.0
                )
                validate_remote_mp4.assert_called_once()
                self.assertEqual(done["status"], "done")
                self.assertEqual(done["sha256"], digest)
                self.assertEqual(Path(done["src"]).read_bytes(), payload)
                self.assertEqual(Path(done["src"]).parent, Path(nas))

    def test_expired_rtx_lease_cannot_append_and_requeue_removes_partial_upload(self):
        payload = b"stale bytes"
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        tokens = iter(["execution", "secret"])
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            out = os.path.join(nas, ".h3-web", "work")
            os.makedirs(out)
            job = {"id": "remote", "status": "queued", "segments": 1,
                   "cfg": {"worker_target": "rtx5080", "mode": "t2v"}}
            with patch.object(server, "NAS_DIR", nas), patch.object(server, "OUT_DIR", out), \
                 patch.dict(server.JOBS, {"remote": job}, clear=True), \
                 patch.object(server, "QUEUE", ["remote"]), \
                 patch.dict(server.WORKERS, {}, clear=True), \
                 patch.object(server, "_save_job"):
                server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat, now=10.0)
                claim = server.claim_rtx5080_job(now=10.0, token_factory=lambda: next(tokens))
                server.begin_rtx5080_upload(
                    "remote", claim["execution_id"], claim["lease_token"],
                    len(payload), hashlib.sha256(payload).hexdigest(), now=11.0,
                )
                part = Path(server.JOBS["remote"]["upload_path"])
                self.assertTrue(part.exists())
                with self.assertRaises(PermissionError):
                    server.append_rtx5080_upload(
                        "remote", claim["execution_id"], claim["lease_token"], 0,
                        payload, hashlib.sha256(payload).hexdigest(), now=72.0,
                    )
                self.assertEqual(part.stat().st_size, 0)
                self.assertEqual(server.requeue_expired_rtx5080_jobs(now=72.0), ["remote"])
                self.assertFalse(part.exists())
                self.assertIsNone(server.JOBS["remote"]["upload_path"])

    def test_worker_http_api_accepts_proxy_or_direct_auth_and_rejects_missing_secret(self):
        heartbeat = {
            "worker_id": server.RTX5080_WORKER_ID,
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
            "modes": ["t2v", "i2v"], "model_profile": "minimax-h3-pgx-exact-v1",
        }
        job = {"id": "remote", "status": "queued", "created": 1,
               "cfg": {"worker_target": "rtx5080", "mode": "t2v"}}

        def post(port, path, body, include_worker):
            headers = {"Content-Type": "application/json", server.ORIGIN_HEADER: self.ORIGIN_SECRET}
            if include_worker:
                headers[server.WORKER_HEADER] = "worker-secret"
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("POST", path, body=json.dumps(body).encode(), headers=headers)
            response = conn.getresponse()
            data = json.loads(response.read())
            conn.close()
            return response.status, data

        with patch.object(server, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch.object(server, "WORKER_PROXY_SECRET", "worker-secret"), \
             patch.dict(server.JOBS, {"remote": job}, clear=True), \
             patch.object(server, "QUEUE", ["remote"]), \
             patch.dict(server.WORKERS, {}, clear=True), \
             patch.object(server, "_save_job"):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                status, denied = post(httpd.server_port, "/api/worker/heartbeat", heartbeat, False)
                self.assertEqual(status, 401)
                self.assertFalse(denied["ok"])

                direct_headers = {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer direct-worker-secret-0123456789abcdef",
                }
                with patch.object(server, "DIRECT_WORKER_SECRET", "direct-worker-secret-0123456789abcdef"):
                    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                    conn.request("POST", "/api/worker/heartbeat", json.dumps(heartbeat), direct_headers)
                    direct_response = conn.getresponse()
                    direct_payload = json.loads(direct_response.read())
                    conn.close()
                self.assertEqual(direct_response.status, 200)
                self.assertTrue(direct_payload["worker"]["eligible"])

                status, ready = post(httpd.server_port, "/api/worker/heartbeat", heartbeat, True)
                self.assertEqual(status, 200)
                self.assertTrue(ready["worker"]["eligible"])
                self.assertNotIn("token", json.dumps(ready).lower())

                status, claimed = post(httpd.server_port, "/api/worker/claim", {}, True)
                self.assertEqual(status, 200)
                self.assertEqual(claimed["job"]["id"], "remote")
                self.assertIn("lease_token", claimed)
                self.assertNotIn("lease_sha256", json.dumps(claimed))
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_full_queue_rejection_removes_new_job_input_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            upload = Path(root) / "upload.png"
            upload.write_bytes(b"private reference bytes")
            existing = {
                f"q{i}": {"id": f"q{i}", "status": "queued", "cfg": {"worker_target": "pgx"}}
                for i in range(server.MAX_PENDING_JOBS)
            }
            queue = list(existing)
            body = json.dumps({
                "prompt": "queue snapshot cleanup test", "mode": "i2v", "seconds": 2,
                "worker_target": "pgx", "image": "nonce",
                "image_size": upload.stat().st_size,
                "image_sha256": hashlib.sha256(upload.read_bytes()).hexdigest(),
            }).encode()
            with patch.object(server, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
                 patch.object(server, "NAS_DIR", root), \
                 patch.dict(server.JOBS, existing, clear=True), \
                 patch.object(server, "QUEUE", queue), \
                 patch.dict(server.UPLOADED, {"nonce": {"path": str(upload), "name": "upload.png"}}, clear=True), \
                 patch.object(server, "_save_job"):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                    conn.request("POST", "/api/generate", body=body, headers={
                        "Content-Type": "application/json", server.ORIGIN_HEADER: self.ORIGIN_SECRET,
                    })
                    response = conn.getresponse()
                    payload = json.loads(response.read())
                    conn.close()
                    self.assertEqual(response.status, 429)
                    self.assertEqual(payload["code"], "QUEUE_FULL")
                    inputs = Path(root) / ".h3-web" / "inputs"
                    self.assertEqual(list(inputs.glob("*")) if inputs.exists() else [], [])
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=2)

    def test_generate_routes_one_job_to_selected_worker_and_rejects_offline_rtx(self):
        def post(port, worker_target):
            body = json.dumps({
                "prompt": "worker routing test prompt",
                "mode": "t2v",
                "seconds": 2,
                "worker_target": worker_target,
            }).encode()
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/api/generate", body=body, headers={
                "Content-Type": "application/json",
                server.ORIGIN_HEADER: self.ORIGIN_SECRET,
            })
            response = conn.getresponse()
            payload = json.loads(response.read())
            conn.close()
            return response.status, payload

        with patch.object(server, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch.dict(server.JOBS, {}, clear=True), \
             patch.object(server, "QUEUE", []), \
             patch.object(server, "QUEUE_RESERVATIONS", {"pgx": 0, "rtx5080": 0}), \
             patch.dict(server.WORKERS, {}, clear=True), \
             patch.object(server, "_save_job"):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                status, pgx = post(httpd.server_port, "pgx")
                self.assertEqual(status, 200)
                self.assertEqual(server.JOBS[pgx["job"]]["cfg"]["worker_target"], "pgx")
                self.assertEqual(len(server.JOBS), 1)

                status, offline = post(httpd.server_port, "rtx5080")
                self.assertEqual(status, 409)
                self.assertEqual(offline["code"], "RTX5080_OFFLINE")
                self.assertEqual(len(server.JOBS), 1)

                server.record_worker_heartbeat(server.RTX5080_WORKER_ID, {
                    "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
                    "comfy_up": True, "model_ready": True, "generation_verified": True, "busy": False,
                    "modes": ["t2v", "i2v"],
                    "model_profile": "minimax-h3-pgx-exact-v1",
                })
                status, rtx = post(httpd.server_port, "rtx5080")
                self.assertEqual(status, 200)
                self.assertEqual(server.JOBS[rtx["job"]]["cfg"]["worker_target"], "rtx5080")
                self.assertEqual(len(server.JOBS), 2)
                self.assertNotEqual(pgx["job"], rtx["job"])

                for index in range(4):
                    jid = f"pgx-fill-{index}"
                    server.JOBS[jid] = {
                        "id": jid, "status": "queued",
                        "cfg": {"worker_target": "pgx", "mode": "t2v"},
                    }
                    server.QUEUE.append(jid)
                status, full = post(httpd.server_port, "pgx")
                self.assertEqual(status, 429)
                self.assertEqual(full["code"], "QUEUE_FULL")
                self.assertEqual(full["worker_target"], "pgx")

                status, independent = post(httpd.server_port, "rtx5080")
                self.assertEqual(status, 200)
                self.assertEqual(server.JOBS[independent["job"]]["cfg"]["worker_target"], "rtx5080")
                self.assertEqual(len(server.JOBS), 7)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_generate_client_request_id_is_idempotent_and_does_not_grow_queue(self):
        def post(port):
            body = json.dumps({
                "prompt": "daily ReelRadar idempotency test prompt",
                "mode": "t2v",
                "seconds": 10,
                "worker_target": "pgx",
                "client_request_id": "8d2160cb-53d2-4f90-8d8d-a35b7f4e6d51",
            }).encode()
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/api/generate", body=body, headers={
                "Content-Type": "application/json",
                server.ORIGIN_HEADER: self.ORIGIN_SECRET,
            })
            response = conn.getresponse()
            payload = json.loads(response.read())
            conn.close()
            return response.status, payload

        with patch.object(server, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch.dict(server.JOBS, {}, clear=True), \
             patch.object(server, "QUEUE", []), \
             patch.object(server, "QUEUE_RESERVATIONS", {"pgx": 0, "rtx5080": 0}), \
             patch.object(server, "_save_job"):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                first_status, first = post(httpd.server_port)
                second_status, second = post(httpd.server_port)
                self.assertEqual(first_status, 200)
                self.assertEqual(second_status, 200)
                self.assertFalse(first.get("duplicate", False))
                self.assertTrue(second["duplicate"])
                self.assertEqual(second["job"], first["job"])
                self.assertEqual(len(server.JOBS), 1)
                self.assertEqual(server.QUEUE, [first["job"]])
                self.assertEqual(server.JOBS[first["job"]]["cfg"]["client_request_id"],
                                 "8d2160cb-53d2-4f90-8d8d-a35b7f4e6d51")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_comfy_recovery_never_starts_failed_duplicate_user_unit(self):
        source = inspect.getsource(server.ensure_comfyui)
        self.assertIn("systemctl start comfyui-minimax-h3.service", source)
        self.assertNotIn("systemctl --user start minimax-h3-comfyui.service", source)

    def test_comfy_recovery_retries_transient_system_service_start_failure(self):
        with patch.object(server, "comfy_up", side_effect=[False, False, False, True]), \
             patch.object(server, "run_asu", side_effect=[
                 RuntimeError("transient"),
                 subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
             ]) as start, \
             patch.object(server.time, "sleep"):
            self.assertTrue(server.ensure_comfyui())
        self.assertEqual(start.call_count, 2)

    def test_run_asu_executes_directly_when_already_running_as_target_user(self):
        completed = subprocess.CompletedProcess(
            args=["bash", "-c", "fixed-control-command"],
            returncode=0,
            stdout="",
            stderr="",
        )
        target_user = type("TargetUser", (), {"pw_uid": 1000})()
        with patch.object(server.os, "geteuid", return_value=1000), \
             patch("pwd.getpwnam", return_value=target_user), \
             patch.object(server.subprocess, "run", return_value=completed) as run:
            server.run_asu("fixed-control-command")
        self.assertEqual(run.call_args.args[0], ["bash", "-c", "fixed-control-command"])

    def test_run_asu_surfaces_stdout_when_failed_command_has_whitespace_stderr(self):
        failed = subprocess.CompletedProcess(
            args=["fixed-control-command"], returncode=1,
            stdout="systemd control transport failed", stderr="  \n",
        )
        with patch.object(server.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "systemd control transport failed"):
                server.run_asu("fixed-control-command")

    def test_run_asu_redacts_full_command_when_subprocess_times_out(self):
        secret_command = "cp /private/customer-secret.mov /safe/output.mp4"
        expired = subprocess.TimeoutExpired(secret_command, 7)
        with patch.object(server.subprocess, "run", side_effect=expired):
            with self.assertRaisesRegex(RuntimeError, "timed out after 7") as raised:
                server.run_asu(secret_command, timeout=7)
        self.assertNotIn("customer-secret.mov", str(raised.exception))
        self.assertNotIn(secret_command, str(raised.exception))

    def test_comfy_recovery_never_calls_past_its_300_second_deadline(self):
        clock = {"now": 0.0}
        probe_timeouts = []
        start_timeouts = []

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            clock["now"] += seconds

        def unavailable(*, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 8)
            probe_timeouts.append(timeout)
            clock["now"] += timeout
            return False

        def failed_start(_cmd, *, timeout, check):
            self.assertTrue(check)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 60)
            start_timeouts.append(timeout)
            clock["now"] += timeout
            raise subprocess.TimeoutExpired("systemctl start", timeout)

        with patch.object(server.time, "monotonic", side_effect=monotonic), \
             patch.object(server.time, "sleep", side_effect=sleep), \
             patch.object(server, "comfy_up", side_effect=unavailable), \
             patch.object(server, "run_asu", side_effect=failed_start):
            with self.assertRaisesRegex(RuntimeError, "300초"):
                server.ensure_comfyui()

        self.assertEqual(len(start_timeouts), 3)
        self.assertTrue(probe_timeouts)
        self.assertLessEqual(clock["now"], 300.0)

    def test_cancel_queued_job_releases_lock_before_persisting(self):
        job = {"id": "queued-job", "status": "queued"}
        with patch.dict(server.JOBS, {"queued-job": job}, clear=True), \
             patch.object(server, "QUEUE", ["queued-job"]), \
             patch.object(server, "_save_job") as save:
            self.assertTrue(server.cancel_queued_job("queued-job"))
            self.assertEqual(job["status"], "cancelled")
            self.assertNotIn("queued-job", server.QUEUE)
            self.assertTrue(server.LOCK.acquire(timeout=0.1))
            server.LOCK.release()
            save.assert_called_once_with("queued-job")

    def test_cancel_running_pgx_job_persists_before_interrupting_its_prompt(self):
        job = {
            "id": "running-job", "status": "running", "comfy_prompt_id": "prompt-owned",
            "cfg": {"worker_target": "pgx"},
        }
        events = []
        with patch.dict(server.JOBS, {"running-job": job}, clear=True), \
             patch.object(server, "QUEUE", []), \
             patch.object(server, "_save_job", side_effect=lambda jid: events.append(("saved", jid))), \
             patch.object(server, "cancel_comfy_prompt", side_effect=lambda pid: events.append(("interrupted", pid))):
            result = server.cancel_job("running-job", now=123.0)
        self.assertTrue(result["ok"])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["cancelled_at"], 123.0)
        self.assertEqual(job["progress"]["phase"], "사용자가 생성을 중단했습니다")
        self.assertEqual(events, [("saved", "running-job"), ("interrupted", "prompt-owned")])

    def test_pgx_cancel_deletes_owned_pending_prompt_without_interrupting_other_running_prompt(self):
        queue = {
            "queue_running": [[1, "other-running", {}, {}]],
            "queue_pending": [[2, "owned-pending", {}, {}]],
        }
        with patch.object(server, "comfy_get", return_value=queue), \
             patch.object(server, "comfy_post") as request:
            self.assertTrue(server.cancel_comfy_prompt("owned-pending"))
        request.assert_called_once_with(
            "/queue", {"delete": ["owned-pending"]}, timeout=10,
        )

    def test_cancel_endpoint_accepts_running_job(self):
        job = {"id": "running-job", "status": "running", "cfg": {"worker_target": "rtx5080"}}
        with patch.dict(server.JOBS, {"running-job": job}, clear=True), \
             patch.object(server, "QUEUE", []), \
             patch.object(server, "_save_job"), \
             patch.object(server, "ORIGIN_SECRET", self.ORIGIN_SECRET):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                conn.request("POST", "/api/cancel/running-job", body=b"{}", headers={
                    "Content-Type": "application/json",
                    server.ORIGIN_HEADER: self.ORIGIN_SECRET,
                })
                response = conn.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["status"], "cancelled")
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_concurrent_job_saves_never_overwrite_newer_snapshot(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(server, "JOBS_DIR", root), \
             patch.dict(server.JOBS, {"race": {"id": "race", "status": "old"}}, clear=True):
            old_blocked = threading.Event()
            release_old = threading.Event()
            original_dump = json.dump

            def controlled_dump(value, stream, *args, **kwargs):
                if value.get("status") == "old":
                    old_blocked.set()
                    self.assertTrue(release_old.wait(timeout=2))
                return original_dump(value, stream, *args, **kwargs)

            with patch.object(server.json, "dump", side_effect=controlled_dump), \
                 patch.object(server, "log") as save_log:
                old_thread = threading.Thread(target=server._save_job, args=("race",))
                old_thread.start()
                self.assertTrue(old_blocked.wait(timeout=2))
                with server.LOCK:
                    server.JOBS["race"]["status"] = "new"
                new_thread = threading.Thread(target=server._save_job, args=("race",))
                new_thread.start()
                release_old.set()
                old_thread.join(timeout=3)
                new_thread.join(timeout=3)
            self.assertFalse(old_thread.is_alive())
            self.assertFalse(new_thread.is_alive())
            self.assertFalse(any("저장 실패" in str(call) for call in save_log.call_args_list))
            saved = json.loads(Path(server._job_file("race")).read_text())
            self.assertEqual(saved["status"], "new")
            self.assertEqual(list(Path(root).glob("*.tmp*")), [])

    def test_pgx_start_transition_cannot_resurrect_cancelled_job(self):
        with patch.dict(server.JOBS, {
            "cancelled": {"id": "cancelled", "status": "cancelled", "cfg": {"worker_target": "pgx"}},
            "queued": {"id": "queued", "status": "queued", "cfg": {"worker_target": "pgx"}},
        }, clear=True), patch.object(server, "ACTIVE", [None]):
            self.assertIsNone(server.begin_pgx_job("cancelled", now=10.0))
            cfg = server.begin_pgx_job("queued", now=11.0)
            self.assertEqual(cfg["worker_target"], "pgx")
            self.assertEqual(server.JOBS["queued"]["status"], "starting")
            self.assertEqual(server.ACTIVE[0], "queued")

    def test_active_worker_aborts_when_its_job_is_cancelled_or_deleted(self):
        for jobs in ({"job": {"id": "job", "status": "cancelled"}}, {}):
            with self.subTest(jobs=jobs), patch.dict(server.JOBS, jobs, clear=True):
                with self.assertRaises(server.JobCancelled):
                    server.assert_job_active("job")

        source = inspect.getsource(server.run_job)
        wait_loop = source[source.index("while True:"):source.index("finally:", source.index("while True:"))]
        self.assertIn("assert_job_active(job_id)", wait_loop)

    def test_windows_worker_interrupts_only_its_running_comfy_prompt(self):
        worker = self.load_windows_worker()
        comfy = worker.ComfyClient("http://127.0.0.1:8188")
        queue = {
            "queue_running": [[1, "owned-prompt", {}, {}]],
            "queue_pending": [[2, "other-prompt", {}, {}]],
        }
        with patch.object(comfy, "json", side_effect=[queue, {}]) as request:
            self.assertTrue(comfy.cancel_prompt("owned-prompt"))
        self.assertEqual(request.call_args_list, [
            call("/queue", timeout=10),
            call("/interrupt", {}, timeout=10),
        ])

    def test_windows_worker_lease_cancellation_interrupts_submitted_comfy_prompt(self):
        worker = self.load_windows_worker()

        class FakeComfy:
            url = "http://127.0.0.1:8188"
            cancelled = []
            def json(self, path, payload=None, timeout=30):
                if path == "/prompt":
                    return {"prompt_id": "owned-prompt"}
                raise AssertionError(path)
            def cancel_prompt(self, prompt_id):
                self.cancelled.append(prompt_id)
                return True

        class FakeServer:
            @staticmethod
            def build_workflow(*args, **kwargs):
                return {}

        class CancelledState:
            @staticmethod
            def lease_ok(claim):
                return False

        class FakeWebSocket:
            def close(self):
                pass

        class FakeWebSocketModule:
            class WebSocketException(Exception):
                pass
            class WebSocketTimeoutException(Exception):
                pass
            @staticmethod
            def create_connection(*args, **kwargs):
                return FakeWebSocket()

        comfy = FakeComfy()
        claim = {"job": {"id": "deadbeef"}, "execution_id": "exec", "lease_token": "lease"}
        cfg = {"prompt": "prompt", "negative": "", "width": 768, "height": 432, "steps": 4}
        with tempfile.TemporaryDirectory() as root, \
             patch.dict(sys.modules, {"websocket": FakeWebSocketModule()}):
            with self.assertRaises(worker.WorkerJobCancelled):
                worker.generate_segment(
                    object(), comfy, FakeServer(), claim, cfg, 0, 1, 9, 1,
                    Path(root) / "segment.mp4", Path(root), Path(root), CancelledState(),
                )
        self.assertEqual(comfy.cancelled, ["owned-prompt"])

    def test_windows_worker_fences_cancelled_claim_before_generation_and_failure_reporting(self):
        worker = self.load_windows_worker()

        class CancelledState:
            @staticmethod
            def lease_ok(claim):
                return False

        claim = {
            "job": {"id": "deadbeef", "cfg": {}},
            "execution_id": "exec", "lease_token": "lease",
        }
        with tempfile.TemporaryDirectory() as root, \
             patch.object(worker, "copy_job_inputs") as copy_inputs, \
             patch.object(worker, "generate_segment", side_effect=AssertionError("generation must not start")):
            with self.assertRaises(worker.WorkerJobCancelled):
                worker.process_claim(
                    object(), object(), object(), claim, Path(root), Path(root), CancelledState(),
                )
        copy_inputs.assert_not_called()
        run_source = inspect.getsource(worker.run_worker)
        cancelled = run_source.index("except WorkerJobCancelled")
        generic = run_source.index("except Exception as exc", cancelled)
        self.assertLess(cancelled, generic)
        self.assertNotIn('"/api/worker/fail"', run_source[cancelled:generic])

    def test_prompt_missing_from_comfy_queue_and_history_is_bounded(self):
        job = {"id": "job", "status": "queued"}
        with patch.dict(server.JOBS, {"job": job}, clear=True):
            unknown_since = server.guard_prompt_presence(
                "job", "unknown", None, now=10.0, grace_seconds=5.0
            )
            self.assertEqual(unknown_since, 10.0)
            self.assertEqual(
                server.guard_prompt_presence(
                    "job", "unknown", unknown_since, now=14.9, grace_seconds=5.0
                ),
                unknown_since,
            )
            with self.assertRaisesRegex(RuntimeError, "큐/기록에서 사라졌습니다"):
                server.guard_prompt_presence(
                    "job", "unknown", unknown_since, now=15.0, grace_seconds=5.0
                )
            self.assertIsNone(
                server.guard_prompt_presence(
                    "job", "running", unknown_since, now=16.0, grace_seconds=5.0
                )
            )

    def test_progress_update_cannot_resurrect_a_cancelled_job(self):
        job = {"id": "job", "status": "cancelled", "progress": {"phase": "중단됨"}}
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job") as save:
            server.update_job(
                "job", status="running", comfy_status="running",
                progress={"phase": "영상 생성 중", "pct": 20},
            )
            server.update_job(
                "job", comfy_status="connected",
                progress={"phase": "ComfyUI 진행 정보 연결됨", "pct": None},
            )
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["progress"], {"phase": "중단됨"})
        save.assert_not_called()
    def test_job_created_time_normalizes_seconds_milliseconds_and_iso(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        start = html.index("function normalizeTimestampMs")
        end = html.index("function loadRecent", start)
        functions = html[start:end]
        script = functions + r"""
const now = 1700003600000;
console.log(JSON.stringify({
  seconds: normalizeTimestampMs(1700000000),
  milliseconds: normalizeTimestampMs(1700000000000),
  iso: normalizeTimestampMs('2023-11-14T22:13:20.000Z'),
  secondsAgo: fmtAgo(1700000000, now),
  millisecondsAgo: fmtAgo(1700000000000, now),
  exact: fmtCreatedAt(1700000000, now),
  missing: fmtCreatedAt(null, now)
}));
"""
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
            env={**os.environ, "TZ": "Asia/Seoul"},
        )
        values = json.loads(result.stdout)
        self.assertEqual(values["seconds"], 1700000000000)
        self.assertEqual(values["milliseconds"], 1700000000000)
        self.assertEqual(values["iso"], 1700000000000)
        self.assertEqual(values["secondsAgo"], "1시간 전")
        self.assertEqual(values["millisecondsAgo"], "1시간 전")
        self.assertIn("2023", values["exact"])
        self.assertIn("1시간 전", values["exact"])
        self.assertEqual(values["missing"], "시간 미상")

    def test_running_job_cancel_ui_targets_exact_job_and_rejects_stale_poll(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        running = html[html.index("} else if(j.status==='running'"):html.index("} else if(j.status==='done'")]
        self.assertIn("$('#stop').disabled=false", running)
        handler = html[html.index("$('#stop').onclick=async()=>{"):html.index("// 스크립트 팝업")]
        self.assertIn("const jobId=currentJob", handler)
        self.assertIn("'/api/cancel/'+encodeURIComponent(jobId)", handler)
        self.assertIn("if(!response.ok||!data.ok)", handler)
        self.assertNotIn("'/api/job/'", handler)
        poll = html[html.index("async function pollJob(){"):html.index("// 모드 (t2v / i2v)")]
        self.assertIn("const jobId=currentJob", poll)
        self.assertIn("if(currentJob!==jobId)return", poll)

    def test_completed_video_has_server_generated_jpeg_thumbnail(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output")
            jobs_dir = os.path.join(root, "jobs")
            jid = "thumbjob"
            video_dir = os.path.join(out_dir, jid)
            os.makedirs(video_dir)
            video_path = os.path.join(video_dir, jid + ".mp4")
            subprocess.run([
                "ffmpeg", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=320x240:d=1",
                "-pix_fmt", "yuv420p", video_path,
            ], check=True)
            job = {"id": jid, "status": "done", "src": video_path}
            with patch.object(server, "OUT_DIR", out_dir), \
                 patch.object(server, "JOBS_DIR", jobs_dir), \
                 patch.dict(server.JOBS, {jid: job}, clear=True):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                    conn.request("GET", f"/api/thumbnail/{jid}")
                    response = conn.getresponse()
                    body = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
                    self.assertTrue(body.startswith(b"\xff\xd8"))
                    self.assertGreater(len(body), 500)
                    self.assertTrue(os.path.isfile(os.path.join(jobs_dir, "thumbnails", jid + ".jpg")))
                    conn.close()
                finally:
                    httpd.shutdown()
                    httpd.server_close()

    def test_completed_job_card_uses_jpeg_poster_instead_of_bare_video(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn("const poster='/api/thumbnail/'+encodeURIComponent(j.id);", html)
        self.assertIn('<img class="rthumb" src="${poster}"', html)
        self.assertNotIn('<video class="rthumb" src="${view}"', html)

    def test_generate_defers_reference_transfer_to_background_worker(self):
        source = (Path(__file__).resolve().parents[1] / "server.py").read_text()
        post_start = source.index('if p == "/api/generate":')
        post_end = source.index('elif p.startswith("/api/cancel/")', post_start)
        post_block = source[post_start:post_end]
        self.assertIn('"image_source_path": image_source_path', post_block)
        self.assertIn('"video_source_path": video_source_path', post_block)
        self.assertNotIn("comfy_upload_image(", post_block)
        self.assertNotIn("comfy_upload_video(", post_block)

        worker_start = source.index("def _upload_job_references(job_id, cfg):")
        worker_end = source.index("def run_job(job_id, cfg):", worker_start)
        worker_block = source[worker_start:worker_end]
        self.assertIn('("image_source_path", "image_source_name"', worker_block)
        self.assertIn('("video_source_path", "video_source_name"', worker_block)
        self.assertIn("source_path = cfg.get(path_key)", worker_block)
        self.assertIn("comfy_upload_image),", worker_block)
        self.assertIn("comfy_upload_video),", worker_block)

    def test_frontend_polling_recovers_from_transient_proxy_timeouts(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn("async function fetchJsonWithTimeout", html)
        self.assertIn("AbortController", html)
        self.assertIn("scheduleJobPoll", html)
        self.assertIn("Math.min(30000", html)
        self.assertIn("localStorage.setItem('h3-active-job'", html)
        self.assertIn("localStorage.removeItem('h3-active-job')", html)

    def test_latest_selected_image_cannot_be_overwritten_by_stale_upload_response(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn("let imgUploadSeq=0, imgUploading=false, imgUploadController=null;", html)
        self.assertIn("const uploadSeq=++imgUploadSeq;", html)
        self.assertIn("imgUploadController.abort();", html)
        self.assertIn("if(uploadSeq!==imgUploadSeq || window.__imgBlob!==f) return;", html)
        self.assertIn("imgUploading=true;", html)
        self.assertIn("imgUploading=false;", html)

    def test_generate_never_falls_back_to_fixed_reference_while_selected_image_uploads(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        generate = html[html.index("$('#go').onclick=async()=>{"):]
        self.assertIn("if(MODE==='i2v' && window.__imgBlob && (imgUploading || !imgNonce))", generate)
        self.assertIn("body.image_sha256=imgSha256;", generate)
        self.assertIn("body.image_size=imgSize;", generate)
        self.assertIn("body.image_last_modified=imgLastModified;", generate)

    def test_reference_snapshot_is_immutable_and_hash_verified_before_worker_upload(self):
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            source = os.path.join(root, "shared.png")
            destination = os.path.join(nas, ".h3-web", "inputs", "job.png")
            with open(source, "wb") as f:
                f.write(b"selected-image")
            meta = server.snapshot_reference_input(source, destination, "selected.png")
            with open(source, "wb") as f:
                f.write(b"different-later-image")
            self.assertEqual(Path(destination).read_bytes(), b"selected-image")
            self.assertEqual(meta["source_name"], "selected.png")
            self.assertEqual(meta["size"], len(b"selected-image"))
            self.assertEqual(meta["sha256"], server.file_sha256(destination))

            uploaded = []
            cfg = {
                "image_source_path": destination,
                "image_source_name": "job.png",
                "image_source_sha256": meta["sha256"],
                "image_source_size": meta["size"],
            }
            with patch.object(server, "NAS_DIR", nas), \
                 patch.object(server, "comfy_upload_image", side_effect=lambda data, name: uploaded.append((data, name)) or name), \
                 patch.object(server, "_save_job"), patch.dict(server.JOBS, {"job": {"cfg": cfg}}, clear=True):
                server._upload_job_references("job", cfg)
            self.assertEqual(uploaded, [(b"selected-image", "job.png")])
            self.assertFalse(os.path.exists(destination))

    def test_multipart_image_upload_preserves_exact_trailing_bytes_and_reports_hash(self):
        # Pillow is optional in production. The byte-integrity contract must
        # hold whether or not image dimension probing is installed.
        selected_bytes = b"\x89PNG\r\n\x1a\nselected-image-bytes\r\n"
        boundary = "----h3-reference-byte-test"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="image"; filename="chosen.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + selected_bytes + f"\r\n--{boundary}--\r\n".encode()

        with tempfile.TemporaryDirectory() as root, \
             patch.object(server, "OUT_DIR", root), \
             patch.dict(server.UPLOADED, {}, clear=True):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                conn.request("POST", "/api/upload", body=body, headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                })
                response = conn.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["size"], len(selected_bytes))
                self.assertEqual(payload["sha256"], hashlib.sha256(selected_bytes).hexdigest())
                with open(server.UPLOADED[payload["nonce"]]["path"], "rb") as uploaded_file:
                    self.assertEqual(uploaded_file.read(), selected_bytes)
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_server_honours_railway_standard_port_environment_variable(self):
        source = inspect.getsource(server)
        self.assertIn(
            'PORT = int(os.environ.get("H3_PORT") or os.environ.get("PORT") or "8300")',
            source,
        )

    def test_camera_motion_strength_is_percent_ui_mapped_to_lora_scale(self):
        source = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('id="camStrength" min="0" max="200" step="5" value="100"', source)
        self.assertIn('id="camStrengthVal"', source)
        self.assertIn('>100%</span>', source)
        self.assertIn('CAM_STRENGTH=percentToLoraStrength(sl.value);', source)
        self.assertIn("v.textContent=Math.round(+sl.value)+'%';", source)
        self.assertIn('body.cam_strength=CAM_STRENGTH;', source)
        self.assertNotIn('>경도 <span', source)
        self.assertNotIn('>강도 <span', source)

    def test_realism_strength_uses_exact_point_zero_five_steps(self):
        source = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('id="realismStrength" min="0" max="2" step="0.05" value="1"', source)

    def test_camera_motion_workflow_receives_percent_mapped_strength(self):
        with patch.object(server.os.path, "exists", return_value=True):
            workflow = server.build_workflow(
                "camera test", "", 768, 1344, 121, 6, 1,
                cam_motion="3000", cam_strength=1.35,
            )
        self.assertEqual(workflow["1c"]["inputs"]["lora_name"], server.CAM_LORA_3000)
        self.assertEqual(workflow["1c"]["inputs"]["strength_model"], 1.35)
        self.assertEqual(workflow["8"]["inputs"]["model"], ["1c", 0])

    def test_fixed_reference_state_survives_kind_switch_and_reload(self):
        source = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        switch_handler = source[source.index("document.querySelectorAll('#refkind"):
                                source.index("// 고정 참조 영상 업로드")]
        self.assertNotIn("refvActive=false", switch_handler)
        self.assertNotIn("refvMeta=null", switch_handler)
        self.assertIn("refreshReferenceState().then(renderSelectedReference);", switch_handler)
        self.assertIn("refvActive=!!refvD.refv;", source)
        self.assertIn("refvMeta=refvD.refv||null;", source)
        self.assertIn("const refv=refvD.refv||null;", source)

    def test_fixed_reference_picker_opens_once_and_allows_same_file_retry(self):
        source = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        picker_block = source[source.index("function openReferencePicker()"):
                              source.index("// 동영상 고정 참조 클릭")]
        self.assertEqual(picker_block.count(".click();"), 1)
        self.assertIn("input.value='';", picker_block)
        self.assertIn("document.getElementById('refzone_inner').onclick=openReferencePicker;", picker_block)
        self.assertNotIn("zv.addEventListener('click'", source)

    def test_fixed_video_generation_uploads_the_saved_mp4_not_png_frame(self):
        source = inspect.getsource(server.Handler.do_POST)
        expected = 'snapshot_reference_input(_refv_video_path(), video_source_path,'
        self.assertIn(expected, source)
        self.assertNotIn('snapshot_reference_input(_ref_path(), video_source_path,', source)

    def test_byte_range_parser(self):
        self.assertEqual(server.parse_byte_range("bytes=4-7", 20), (4, 7))
        self.assertEqual(server.parse_byte_range("bytes=4-", 20), (4, 19))
        self.assertEqual(server.parse_byte_range("bytes=-4", 20), (16, 19))
        self.assertIsNone(server.parse_byte_range(None, 20))
        with self.assertRaises(ValueError):
            server.parse_byte_range("bytes=20-21", 20)

    def test_progress_never_uses_elapsed_time(self):
        with patch.dict(server.JOBS, {"job": {"started": 1, "segments": 1}}, clear=True):
            progress = server._prog("job", "generating")
        self.assertIsNone(progress["pct"])
        self.assertIsNone(progress["eta"])

    def test_measured_sampler_eta_uses_observed_step_rate_after_second_sample(self):
        job = {"id": "job", "started": 100.0, "segment_started": 110.0,
               "segments": 1, "status": "running"}
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server.time, "time", side_effect=[120.0, 120.0, 130.0, 130.0]):
            server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {
                    "prompt_id": "ours", "value": 1, "max": 20, "node": "sampler"
                }
            })
            self.assertEqual(job["progress"]["pct"], 5)
            self.assertIsNone(job["progress"]["eta"])

            server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {
                    "prompt_id": "ours", "value": 2, "max": 20, "node": "sampler"
                }
            })
        self.assertEqual(job["progress"]["pct"], 10)
        self.assertEqual(job["progress"]["sampler_step_seconds"], 10.0)
        self.assertEqual(job["progress"]["eta"], 180)

    def test_duplicate_and_out_of_order_sampler_events_cannot_regress_progress(self):
        job = {"id": "job", "started": 100.0, "segments": 1, "status": "running"}
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server.time, "time", side_effect=[120.0, 120.0, 130.0, 130.0]):
            self.assertTrue(server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {
                    "prompt_id": "ours", "value": 1, "max": 20, "node": "sampler"
                }
            }))
            self.assertTrue(server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {
                    "prompt_id": "ours", "value": 2, "max": 20, "node": "sampler"
                }
            }))
            stable = dict(job["progress"])
            for stale_value in (2, 1):
                self.assertFalse(server.apply_comfy_event("job", "ours", {
                    "type": "progress", "data": {
                        "prompt_id": "ours", "value": stale_value,
                        "max": 20, "node": "sampler",
                    }
                }))
                self.assertEqual(job["progress"], stable)

    def test_sampler_progress_rejects_bool_negative_and_non_finite_numbers(self):
        job = {"id": "job", "started": 100.0, "segments": 1,
               "status": "running", "progress": {"pct": 25, "value": 5, "max": 20}}
        invalid = [
            (True, 20), (-1, 20), (float("nan"), 20), (float("inf"), 20),
            (1, True), (1, 0), (1, float("nan")), (1, float("inf")),
            (10 ** 310, 10 ** 311),
        ]
        with patch.dict(server.JOBS, {"job": job}, clear=True), patch.object(server, "_save_job"):
            stable = dict(job["progress"])
            for value, maximum in invalid:
                accepted = server.apply_comfy_event("job", "ours", {
                    "type": "progress", "data": {
                        "prompt_id": "ours", "value": value,
                        "max": maximum, "node": "sampler",
                    }
                })
                self.assertFalse(accepted, (value, maximum))
                self.assertEqual(job["progress"], stable)

    def test_extreme_finite_sampler_values_never_overflow_derived_eta(self):
        job = {"id": "job", "started": 100.0, "segments": 1,
               "status": "running", "progress": {}}
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server.time, "time", side_effect=[120.0, 120.0, 130.0, 130.0]):
            self.assertTrue(server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {
                    "prompt_id": "ours", "value": 1, "max": 1e308, "node": "sampler"
                }
            }))
            self.assertTrue(server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {
                    "prompt_id": "ours", "value": 2, "max": 1e308, "node": "sampler"
                }
            }))
        self.assertEqual(job["progress"]["value"], 2)
        self.assertEqual(job["progress"]["sampler_step_seconds"], 10.0)
        self.assertIsNone(job["progress"]["eta"])

    def test_tiny_positive_sampler_rate_is_not_rounded_to_zero_before_eta(self):
        job = {"id": "job", "started": 100.0, "segments": 1,
               "status": "running", "progress": {}}
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server.time, "time", side_effect=[120.0, 120.0, 120.0001, 120.0001]):
            for value in (1, 2):
                self.assertTrue(server.apply_comfy_event("job", "ours", {
                    "type": "progress", "data": {
                        "prompt_id": "ours", "value": value,
                        "max": 10_000_000_000, "node": "sampler",
                    }
                }))
        self.assertGreater(job["progress"]["sampler_step_seconds"], 0)
        self.assertEqual(job["progress"]["eta"], 1_000_000)

    def test_unavailable_then_reconnect_keeps_raw_sampler_measurement(self):
        measured = {
            "phase": "영상 생성 중", "pct": 10, "eta": 900,
            "value": 2, "max": 20, "node": "sampler", "sampler_node": "sampler",
            "sampler_pct": 0.1, "updated_at": 120.0, "last_progress_at": 120.0,
            "sampler_step_seconds": 50.0, "unavailable": False,
        }
        job = {"id": "job", "started": 1.0, "segments": 1,
               "status": "running", "progress": dict(measured)}
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server.time, "time", side_effect=[200.0, 201.0]):
            update = server._prog("job", "ComfyUI 상태 확인 불가", unavailable=True)
            server.update_job("job", progress=update)
            reconnected = server._running_lifecycle_progress(
                "job", "ComfyUI 진행 정보 연결됨", unavailable=False
            )
            server.update_job("job", progress=reconnected)
        for key in (
            "value", "max", "node", "sampler_node", "sampler_pct",
            "last_progress_at", "sampler_step_seconds",
        ):
            self.assertEqual(job["progress"][key], measured[key])
        self.assertIsNone(job["progress"]["pct"])
        self.assertIsNone(job["progress"]["eta"])
        self.assertFalse(job["progress"]["unavailable"])
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server.time, "time", side_effect=[211.0, 211.0]):
            self.assertTrue(server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {
                    "prompt_id": "ours", "value": 3, "max": 20, "node": "sampler"
                }
            }))
        self.assertEqual(job["progress"]["pct"], 15)
        self.assertFalse(job["progress"]["unavailable"])
        self.assertTrue(server._finite_real(job["progress"]["sampler_step_seconds"]))

    def test_running_ui_validates_numeric_progress_and_honours_reduced_motion(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        start = html.index("function finiteProgressNumber(value")
        end = html.index("\n}", start) + 2
        helper = html[start:end]
        probe = helper + """
const inputs=[null,undefined,'',true,false,NaN,Infinity,-1,0,25,100,101,'25'];
console.log(JSON.stringify(inputs.map(v=>finiteProgressNumber(v,0,100))));
"""
        result = subprocess.run(["node", "-e", probe], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), [
            None, None, None, None, None, None, None, None,
            0, 25, 100, None, 25,
        ])
        self.assertIn("@media(prefers-reduced-motion:reduce)", html)
        self.assertIn(".track-progress.pending>i{animation:none!important", html)

    def test_running_ui_uses_indeterminate_bar_until_real_progress_and_no_static_eta(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        running = html[html.index("} else if(j.status==='running'"):
                       html.index("} else if(j.status==='done')")]
        self.assertIn("progress-pending", running)
        self.assertIn("실측 속도 산정 중", running)
        self.assertIn("measuredEta!==null", running)
        self.assertNotIn("예상 총 '+fmtEta(est)", running)
        self.assertIn(".progress-pending .bar i", html)
        self.assertIn("@keyframes progressPending", html)

    def test_queued_ui_waits_for_measured_runtime_instead_of_static_eta(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        queued = html[html.index("if(j.status==='queued')"):
                      html.index("} else if(j.status==='starting')")]
        self.assertNotIn("fmtEta(j.estimated_seconds)", queued)
        self.assertIn("작업 시작 후 실측", queued)
        self.assertIn("$('#pct').textContent='—'", queued)

    def test_default_video_work_and_reference_paths_are_nas_backed(self):
        root = os.path.realpath(server.NAS_DIR) + os.sep
        for path in (server.OUT_DIR, server.REF_DIR, server.REFV_DIR):
            self.assertTrue(os.path.realpath(path).startswith(root), path)

    def test_video_storage_gate_rejects_non_nas_output_path(self):
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            local_output = os.path.join(root, "pgx-local-output")
            os.makedirs(nas)
            os.makedirs(local_output)
            with patch.object(server, "NAS_DIR", nas), \
                 patch.object(server, "COMFY_OUT", local_output), \
                 patch.object(server, "OUT_DIR", os.path.join(nas, "work")), \
                 patch.object(server, "REF_DIR", os.path.join(nas, "ref")), \
                 patch.object(server, "REFV_DIR", os.path.join(nas, "refv")):
                with self.assertRaisesRegex(RuntimeError, "NAS"):
                    server.require_nas_video_storage()

    def test_job_input_path_is_private_nas_storage(self):
        with tempfile.TemporaryDirectory() as root, patch.object(server, "NAS_DIR", os.path.join(root, "nas")):
            path = server.job_input_path("job_123", ".mp4")
            self.assertEqual(path, os.path.join(root, "nas", ".h3-web", "inputs", "job_123.mp4"))
            self.assertTrue(server._is_under_nas(path))

    def test_generate_snapshots_media_inputs_under_nas(self):
        source = inspect.getsource(server.Handler.do_POST)
        self.assertIn('job_input_path(jid, ".png")', source)
        self.assertIn('job_input_path(jid, ".mp4")', source)
        self.assertNotIn('os.path.join(JOBS_DIR, "inputs")', source)

    def test_run_job_checks_nas_storage_before_comfy_request(self):
        source = inspect.getsource(server.run_job)
        self.assertLess(source.index("require_nas_video_storage()"), source.index("ensure_comfyui()"))

    def test_host_memory_stats_reports_kernel_measured_used_total_and_available(self):
        """Host RAM values must come from /proc/meminfo, never browser estimates."""
        measured = server.host_memory_stats("""MemTotal:       131072000 kB
MemAvailable:    20971520 kB
MemFree:          1048576 kB
Buffers:           524288 kB
Cached:          18874368 kB
""")
        self.assertEqual(measured, {
            "total_gb": 134.2,
            "used_gb": 112.7,
            "available_gb": 21.5,
        })

    def test_jobs_status_includes_measured_host_and_gpu_memory(self):
        source = (Path(__file__).resolve().parents[1] / "server.py").read_text()
        self.assertIn('"host_memory": host_memory_stats()', source)
        self.assertIn('"gpu_vram_used_gb":', source)

    def test_comfy_events_require_matching_prompt_and_keep_raw_measurement(self):
        job = {"id": "job", "started": 1, "segments": 1, "status": "queued"}
        with patch.dict(server.JOBS, {"job": job}, clear=True), patch.object(server, "_save_job"):
            ignored = server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {"prompt_id": "someone-else", "value": 19, "max": 20, "node": "9"}
            })
            self.assertFalse(ignored)
            self.assertEqual(job["status"], "queued")
            accepted = server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {"prompt_id": "ours", "value": 4, "max": 20, "node": "9"}
            })
        self.assertTrue(accepted)
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["progress"]["pct"], 20)
        self.assertEqual(job["progress"]["value"], 4)
        self.assertEqual(job["progress"]["max"], 20)
        self.assertEqual(job["progress"]["node"], "9")
        self.assertIsNotNone(job["progress"]["last_progress_at"])

    def test_invalid_comfy_progress_remains_unknown(self):
        job = {"id": "job", "started": 1, "segments": 1, "status": "running"}
        with patch.dict(server.JOBS, {"job": job}, clear=True), patch.object(server, "_save_job"):
            accepted = server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {"prompt_id": "ours", "value": 4, "max": 0}
            })
        self.assertFalse(accepted)
        self.assertNotIn("progress", job)

    def test_comfy_unavailable_clears_measured_percent(self):
        job = {"id": "job", "started": 1, "segments": 1, "status": "running"}
        with patch.dict(server.JOBS, {"job": job}, clear=True), patch.object(server, "_save_job"):
            server.apply_comfy_event("job", "ours", {
                "type": "progress", "data": {"prompt_id": "ours", "value": 4, "max": 20}
            })
            server.update_job("job", comfy_status="unavailable",
                              progress=server._prog("job", "ComfyUI 상태 확인 불가",
                                                    unavailable=True))
        self.assertTrue(job["progress"]["unavailable"])
        self.assertIsNone(job["progress"]["pct"])
        self.assertEqual(job["progress"]["value"], 4)
        self.assertEqual(job["progress"]["max"], 20)

    def test_restore_turns_unavailable_job_into_recoverable_interrupted_state(self):
        """A server restart must not leave an old lost ComfyUI prompt indefinitely unavailable."""
        with tempfile.TemporaryDirectory() as root, patch.object(server, "JOBS_DIR", root), patch.dict(server.JOBS, {}, clear=True):
            with open(os.path.join(root, "lost.json"), "w") as f:
                json.dump({"id": "lost", "status": "unavailable", "created": 1,
                           "progress": {"unavailable": True}}, f)
            server._restore_jobs()
            self.assertEqual(server.JOBS["lost"]["status"], "interrupted")
            self.assertIn("다시 생성", server.JOBS["lost"]["error"])

    def test_queue_lifecycle_is_prompt_scoped_and_never_invents_percent(self):
        job = {"id": "job", "started": 1, "segments": 1, "status": "queued"}
        with patch.dict(server.JOBS, {"job": job}, clear=True), patch.object(server, "_save_job"):
            self.assertEqual(server.reconcile_comfy_prompt("job", "ours", {}, {
                "queue_running": [[0, "someone-else"]], "queue_pending": []
            }), "unknown")
            self.assertEqual(job["status"], "queued")
            self.assertEqual(server.reconcile_comfy_prompt("job", "ours", {}, {
                "queue_running": [], "queue_pending": [[0, "ours"]]
            }), "pending")
            self.assertIsNone(job["progress"]["pct"])
            self.assertEqual(job["progress"]["phase"], "ComfyUI 대기 중")
            # A ComfyUI queue entry is waiting, not generating.  Keep the
            # public lifecycle separate so the UI cannot label it "in progress".
            self.assertEqual(job["status"], "queued")
            self.assertEqual(server.reconcile_comfy_prompt("job", "ours", {}, {
                "queue_running": [[0, "ours"]], "queue_pending": []
            }), "running")
            self.assertIsNone(job["progress"]["pct"])
            self.assertEqual(job["progress"]["phase"], "영상 생성 중")

    def test_queue_poll_does_not_erase_latest_measured_sampler_progress(self):
        measured = {
            "phase": "영상 생성 중", "pct": 10, "eta": 900,
            "value": 2, "max": 20, "node": "9", "sampler_node": "9",
            "updated_at": 120.0, "last_progress_at": 120.0,
            "sampler_step_seconds": 50.0,
        }
        job = {
            "id": "job", "started": 1, "segments": 1,
            "status": "running", "progress": dict(measured),
        }
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server.time, "time", return_value=200.0):
            state = server.reconcile_comfy_prompt("job", "ours", {}, {
                "queue_running": [[0, "ours"]], "queue_pending": []
            })
        self.assertEqual(state, "running")
        for key in (
            "pct", "eta", "value", "max", "node", "sampler_node",
            "last_progress_at", "sampler_step_seconds",
        ):
            self.assertEqual(job["progress"][key], measured[key])
        self.assertEqual(job["progress"]["updated_at"], 200.0)
        self.assertEqual(job["progress"]["queue_running"], 1)

    def test_stale_pending_snapshot_cannot_regress_a_measured_running_prompt(self):
        measured = {
            "phase": "영상 생성 중", "pct": 40, "eta": 60,
            "value": 8, "max": 20, "node": "sampler",
            "sampler_node": "sampler", "sampler_pct": 0.4,
            "updated_at": 120.0, "last_progress_at": 120.0,
            "sampler_step_seconds": 5.0, "unavailable": False,
        }
        job = {
            "id": "job", "started": 1, "segments": 1,
            "status": "running", "comfy_status": "running",
            "progress": dict(measured),
        }
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server.time, "time", return_value=200.0):
            state = server.reconcile_comfy_prompt("job", "ours", {}, {
                "queue_running": [], "queue_pending": [[0, "ours"]]
            })
        self.assertEqual(state, "running")
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["comfy_status"], "running")
        for key in (
            "pct", "eta", "value", "max", "node", "sampler_node",
            "sampler_pct", "last_progress_at", "sampler_step_seconds",
        ):
            self.assertEqual(job["progress"][key], measured[key])
        self.assertEqual(job["progress"]["updated_at"], 200.0)
        self.assertEqual(job["progress"]["queue_pending"], 1)

    def test_executing_event_keeps_sampler_measurement_but_uses_latest_lifecycle_node(self):
        measured = {
            "phase": "영상 생성 중", "pct": 10, "eta": 900,
            "value": 2, "max": 20, "node": "old-sampler", "sampler_node": "old-sampler",
            "updated_at": 120.0, "last_progress_at": 120.0,
        }
        job = {
            "id": "job", "started": 1, "segments": 1,
            "status": "running", "progress": dict(measured),
        }
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server.time, "time", side_effect=[200.0, 201.0]):
            self.assertTrue(server.apply_comfy_event("job", "ours", {
                "type": "executing", "data": {"prompt_id": "ours", "node": "decoder"}
            }))
            self.assertEqual(job["progress"]["node"], "decoder")
            self.assertEqual(job["progress"]["sampler_node"], "old-sampler")
            self.assertEqual(job["progress"]["pct"], 10)
            self.assertEqual(job["progress"]["updated_at"], 200.0)

            self.assertTrue(server.apply_comfy_event("job", "ours", {
                "type": "executing", "data": {"prompt_id": "ours", "node": None}
            }))
            self.assertIsNone(job["progress"]["node"])
            self.assertEqual(job["progress"]["sampler_node"], "old-sampler")
            self.assertEqual(job["progress"]["pct"], 10)
            self.assertEqual(job["progress"]["updated_at"], 201.0)

    def test_completed_history_marks_final_job_complete(self):
        job = {"id": "job", "started": 1, "segments": 1, "status": "running"}
        history = {"ours": {"status": {"completed": True, "status_str": "success"}}}
        with patch.dict(server.JOBS, {"job": job}, clear=True), patch.object(server, "_save_job"):
            self.assertEqual(server.reconcile_comfy_prompt("job", "ours", history, {}, final=True), "completed")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["progress"]["pct"], 100)

    def test_progress_transport_is_installed_and_reconnects_after_socket_loss(self):
        """The service installs WebSocket support and queue polling survives socket loss."""
        root = Path(__file__).resolve().parents[1]
        requirements = (root / "requirements.txt").read_text()
        source = (root / "server.py").read_text()
        unit = (root / "h3-web-backend.service").read_text()
        self.assertIn("websocket-client", requirements)
        self.assertIn("ws_retry_at", source)
        self.assertIn("ws = _comfy_ws(client_id)", source)
        self.assertIn("poll_comfy_queue_state(", inspect.getsource(server.run_job))
        self.assertIn(
            "ExecStart=/home/aski/h3-web/.venv/bin/python /home/aski/h3-web/server.py",
            unit,
        )
        self.assertIn(
            "ExecStartPre=/home/aski/h3-web/.venv/bin/python -c 'import websocket'",
            unit,
        )
        self.assertNotIn("H3_WORKER_TOKEN", unit)
        self.assertIn("H3_WORKER_PROXY_TOKEN", source)
        reconnect = inspect.getsource(server.run_job)
        reconnect_start = reconnect.index("if ws is None:")
        reconnect = reconnect[reconnect_start:reconnect.index("if ws:", reconnect_start + 1)]
        self.assertIn("_running_lifecycle_progress", reconnect)
        self.assertNotIn("progress=_prog", reconnect)

        job = {"id": "job", "started": 1, "segments": 1, "status": "queued"}
        with patch.dict(server.JOBS, {"job": job}, clear=True), \
             patch.object(server, "_save_job"), \
             patch.object(server, "comfy_get", return_value={
                 "queue_running": [], "queue_pending": [[0, "ours"]]
             }):
            state = server.poll_comfy_queue_state("job", "ours", 0, 1)
        self.assertEqual(state, "pending")
        self.assertEqual(job["progress"]["phase"], "ComfyUI 대기 중")
        self.assertIsNone(job["progress"]["pct"])

    def test_comfy_socket_reconnect_honors_cooldown_and_reuses_client_identity(self):
        """A lost event socket reconnects on a bounded cadence for the same prompt client."""
        recovered = object()
        with patch.object(server, "_comfy_ws", return_value=recovered) as connect:
            ws, retry_at = server.reconnect_comfy_ws("same-client", 0.0, now=10.0)
            self.assertIs(ws, recovered)
            self.assertEqual(retry_at, 15.0)
            connect.assert_called_once_with("same-client")

            # A tight polling loop must not spin connection attempts after loss.
            ws, next_retry_at = server.reconnect_comfy_ws("same-client", retry_at, now=12.0)
            self.assertIsNone(ws)
            self.assertEqual(next_retry_at, retry_at)
            connect.assert_called_once()

    def test_comfy_socket_empty_frame_is_treated_as_a_lost_connection(self):
        """websocket-client signals a peer close with an empty payload on some versions."""
        class ClosedSocket:
            def recv(self):
                return ""

        with self.assertRaisesRegex(ConnectionError, "closed"):
            server.recv_comfy_event(ClosedSocket())

    def test_live_job_card_is_promoted_above_workspace_and_mobile_recent_rows_do_not_overlap(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "index.html").read_text()
        css = (root / "apple-redesign.css").read_text()
        workspace = html[html.index("(function buildAppleWorkspace()") : html.index("})();", html.index("(function buildAppleWorkspace()"))]
        self.assertIn("wrap.insertBefore(job,workspace)", workspace)
        self.assertIn("if(!wrap||!tabs||!job) return;", workspace)
        self.assertNotIn("||!footer", workspace)
        self.assertNotIn("output.appendChild(job)", html)
        self.assertIn("live-job-card", html)
        self.assertIn(".live-job-card", css)
        self.assertIn(".recent-modal .ritem", css)
        self.assertIn("overflow-wrap:anywhere", css)

    def test_download_attachment_and_view_inline_support_ranges(self):
        with tempfile.TemporaryDirectory() as root, patch.object(server, "OUT_DIR", root), patch.dict(server.JOBS, {}, clear=True):
            jid = "testjob"
            os.makedirs(os.path.join(root, jid))
            with open(os.path.join(root, jid, jid + ".mp4"), "wb") as f:
                f.write(b"0123456789")
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                conn.request("GET", f"/api/download/{jid}", headers={"Range": "bytes=2-5"})
                response = conn.getresponse()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"2345")
                self.assertEqual(response.getheader("Content-Range"), "bytes 2-5/10")
                self.assertEqual(response.getheader("Accept-Ranges"), "bytes")
                self.assertTrue((response.getheader("Content-Disposition") or "").startswith("attachment"))
                conn.close()

                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                conn.request("HEAD", f"/api/view/{jid}")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Length"), "10")
                self.assertTrue((response.getheader("Content-Disposition") or "").startswith("inline"))
                self.assertEqual(response.read(), b"")
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_reference_video_is_inline_range_streamed_without_public_cache(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(server, "REFV_DIR", root), \
             patch.object(server, "REFV_META", os.path.join(root, "meta.json")):
            with open(os.path.join(root, "ref_video.mp4"), "wb") as f:
                f.write(b"0123456789")
            with open(os.path.join(root, "ref_frame.png"), "wb") as f:
                f.write(b"png")
            with open(server.REFV_META, "w") as f:
                json.dump({"name": "reference.mp4", "size": 10}, f)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                conn.request("GET", "/api/refv", headers={"Range": "bytes=2-5"})
                response = conn.getresponse()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"2345")
                self.assertEqual(response.getheader("Content-Type"), "video/mp4")
                self.assertEqual(response.getheader("Content-Range"), "bytes 2-5/10")
                self.assertEqual(response.getheader("Accept-Ranges"), "bytes")
                self.assertTrue((response.getheader("Content-Disposition") or "").startswith("inline"))
                self.assertEqual(response.getheader("Cache-Control"), "private, no-store")
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_external_media_origin_endpoints_are_not_exposed(self):
        """Completed media stays on the same Vercel/API proxy contract only."""
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ("/api/media-origin", "/api/media-url/missing"):
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                conn.request("GET", path)
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                self.assertEqual(json.loads(response.read()), {"ok": False, "error": "not found"})
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_static_build_and_vercel_routing_keep_css_out_of_spa_fallback(self):
        root = Path(__file__).resolve().parents[1]
        build = (root / "build.sh").read_text()
        config = json.loads((root / "vercel.json").read_text())
        self.assertIn("rm -rf dist", build)
        self.assertIn("cp index.html dist/index.html", build)
        self.assertIn("cp apple-redesign.css dist/apple-redesign.css", build)
        routes = config["routes"]
        filesystem_index = next(i for i, route in enumerate(routes) if route == {"handle": "filesystem"})
        api_index = next(i for i, route in enumerate(routes) if route.get("src") == "/api/(.*)")
        fallback_index = next(i for i, route in enumerate(routes) if route.get("src") == "/(.*)")
        self.assertLess(api_index, fallback_index)
        self.assertLess(filesystem_index, fallback_index)
        unit = (root / "h3-web-backend.service").read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=/home/aski/h3-web/.venv/bin/python /home/aski/h3-web/server.py",
            unit,
        )
        self.assertNotIn("cloudflared", unit.lower())
        self.assertIn("Environment=H3_HOST=127.0.0.1", unit)
        self.assertIn("EnvironmentFile=/home/aski/h3-web/.env", unit)
        self.assertIn('test -n "$H3_ORIGIN_SECRET"', unit)
        self.assertNotIn("minimax-h3-comfyui.service", unit)

    def test_proxy_forwards_range_and_preserves_partial_headers(self):
        seen = {}

        class FakeResponse:
            status = 206
            headers = {"Content-Type": "video/mp4", "Content-Length": "4", "Content-Range": "bytes 2-5/10", "Accept-Ranges": "bytes", "Content-Disposition": "inline; filename=job.mp4"}
            def read(self, _size):
                seen["reads"] = seen.get("reads", 0) + 1
                if getattr(self, "done", False):
                    return b""
                self.done = True
                return b"2345"
            def close(self):
                seen["closed"] = True
            def __enter__(self): return self
            def __exit__(self, *_): return False

        def fake_open(request, timeout):
            seen["range"] = request.get_header("Range")
            return FakeResponse()

        started = []
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/view/job", "QUERY_STRING": "", "HTTP_RANGE": "bytes=2-5", "wsgi.input": None}
        with patch.object(backend_proxy, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch("urllib.request.urlopen", fake_open):
            result = backend_proxy.handler(env, lambda status, headers: started.extend([status, dict(headers)]))
        self.assertEqual(seen["range"], "bytes=2-5")
        self.assertEqual(started[0], "206")
        self.assertEqual(started[1]["Content-Range"], "bytes 2-5/10")
        # The endpoint must return before consuming the video body; Vercel can
        # then pass chunks through instead of buffering an entire MP4.
        self.assertNotIn("reads", seen)
        self.assertEqual(b"".join(result), b"2345")
        self.assertEqual(seen["reads"], 2)
        self.assertTrue(seen["closed"])
        self.assertEqual(started[1]["Cache-Control"], backend_proxy.PUBLIC_VIDEO_CACHE_CONTROL)

    def test_proxy_keeps_reference_video_range_private(self):
        seen = {}

        class FakeResponse:
            status = 206
            headers = {"Content-Type": "video/mp4", "Content-Length": "4", "Content-Range": "bytes 2-5/10", "Accept-Ranges": "bytes", "Content-Disposition": "inline; filename=reference.mp4"}
            def read(self, _size):
                if getattr(self, "done", False): return b""
                self.done = True; return b"2345"
            def close(self): seen["closed"] = True

        started = []
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/refv", "QUERY_STRING": "", "HTTP_RANGE": "bytes=2-5", "wsgi.input": None}
        with patch.object(backend_proxy, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch("urllib.request.urlopen", lambda *args, **kwargs: FakeResponse()):
            result = backend_proxy.handler(env, lambda status, headers: started.extend([status, dict(headers)]))
        self.assertEqual(started[0], "206")
        self.assertEqual(started[1]["Content-Range"], "bytes 2-5/10")
        self.assertEqual(started[1]["Cache-Control"], "private, no-store")
        self.assertEqual(b"".join(result), b"2345")
        self.assertTrue(seen["closed"])
    def test_video_ui_has_closeable_in_page_player_and_distinguishes_queue(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('id="videoModal"', html)
        self.assertIn('aria-label="영상 닫기"', html)
        self.assertIn("function showVideo", html)
        self.assertIn("st==='queued'){ stTxt='대기열'", html)

    def test_save_buttons_use_direct_attachment_without_buffering_mp4_in_javascript(self):
        """Saving must preserve the tap and let the attachment endpoint stream the MP4."""
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('<button class="dl" id="dl1" type="button">⬇ MP4 저장</button>', html)
        self.assertIn('id="saveStatus"', html)
        self.assertIn('function saveVideo(', html)
        self.assertIn('const appleDownload=shouldUseNativeShare()', html)
        self.assertIn('triggerNativeDownload(url,filename,appleDownload)', html)
        self.assertNotIn('response.blob()', html)
        self.assertNotIn('navigator.share', html)
        self.assertIn("saveCompletedVideo(j.id, j.id+'.mp4', $('#dl1'))", html)
        self.assertIn("saveCompletedVideo(j.id, j.id+'.mp4', saveB)", html)
        self.assertIn("return saveVideo(media.download,filename||jid+'.mp4',button);", html)
        self.assertNotIn('id="dl1" download', html)

    def test_today_completed_count_uses_completed_at_in_kst_and_ignores_non_done_jobs(self):
        kst = timezone(timedelta(hours=9))
        now = datetime(2026, 9, 9, 18, 0, tzinfo=kst).timestamp()
        jobs = {
            "today": {"status": "done", "completed_at": datetime(2026, 9, 9, 0, 1, tzinfo=kst).timestamp()},
            "old": {"status": "done", "completed_at": datetime(2026, 9, 8, 23, 59, tzinfo=kst).timestamp()},
            "running": {"status": "running", "completed_at": datetime(2026, 9, 9, 12, 0, tzinfo=kst).timestamp()},
            "missing": {"status": "done"},
        }
        self.assertEqual(server.count_completed_videos_today(jobs, now=now), 1)

    def test_legacy_completed_job_backfills_from_actual_video_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "legacy.mp4"
            video.write_bytes(b"mp4")
            expected = 1_788_900_000
            os.utime(video, (expected, expected))
            with patch.object(server, "_job_video_source", return_value=(str(video), False)):
                self.assertEqual(server.infer_completed_at_from_video("legacy"), expected)

    def test_remote_archive_stream_uses_block_ranges_and_keeps_exact_byte_windows(self):
        self.assertEqual(server.remote_range_dd_plan(0, 2), (0, 0, 1))
        self.assertEqual(server.remote_range_dd_plan(65535, 2), (0, 65535, 2))
        self.assertEqual(server.remote_range_dd_plan(65536, 65536), (1, 0, 1))
        source = (Path(__file__).resolve().parents[1] / "server.py").read_text()
        self.assertIn('f"bs={REMOTE_RANGE_BLOCK_BYTES}"', source)
        self.assertNotIn('"bs=1", f"skip={start}", f"count={length}"', source)
        self.assertNotIn('"iflag=skip_bytes"', source)

    def test_completed_videos_are_faststart_optimized_before_nas_archive(self):
        source = (Path(__file__).resolve().parents[1] / "server.py").read_text()
        remux = source[source.index('def _remux_24fps'):source.index('def _stitch_segments')]
        self.assertIn('"-movflags", "+faststart"', remux)
        self.assertIn('"-pix_fmt", "yuv420p"', remux)

    def test_ref_video_multipart_parser_accepts_browser_formdata(self):
        boundary = '----WebKitFormBoundaryXyZ123'
        body = (
            f'--{boundary}\r\n'
            'Content-Disposition: form-data; name="file"; filename="person clip.mp4"\r\n'
            'Content-Type: video/mp4\r\n\r\n'
        ).encode() + b'\x00video\r\n' + b'\r\n' + f'--{boundary}--\r\n'.encode()
        name, data = server.extract_multipart_file_field(
            body, f'multipart/form-data; boundary={boundary}', 'file'
        )
        self.assertEqual(name, 'person clip.mp4')
        self.assertEqual(data, b'\x00video\r\n')

    def test_recent_video_open_does_not_escape_to_an_unclosable_tab(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertNotIn('class="rbtn open" href="${view}" target="_blank"', html)

    def test_mobile_recent_jobs_group_actions_below_metadata(self):
        """A 390px viewport must not squeeze the title into one character columns.

        Completed-job controls are grouped so CSS can place them on a dedicated,
        full-width action row instead of keeping five flex children in one row.
        """
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('class="ractions"', html)
        self.assertIn('.ractions{', html)
        mobile_css = html.split('@media(max-width:480px){', 1)[1].split('</style>', 1)[0]
        self.assertIn('.ritem{display:grid;grid-template-columns:64px minmax(0,1fr)', mobile_css)
        self.assertIn('.ractions{grid-column:1 / -1', mobile_css)

    def test_active_job_tracking_has_a_detail_modal_and_refreshes_live_data(self):
        """Active jobs expose their measured progress and saved generation config."""
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('id="trackingModal"', html)
        self.assertIn('id="trackingModalClose"', html)
        self.assertIn('function showJobTracking(j)', html)
        self.assertIn('data-track-job="${j.id}"', html)

    def test_elapsed_time_uses_padded_hours_minutes_and_seconds(self):
        """Elapsed time rejects missing values and floors exact HH/MM/SS boundaries."""
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        start = html.index("function fmtElapsed(s){")
        end = html.index("\n}", start) + 2
        function_source = html[start:end]
        probe = function_source + """
const inputs=[null,undefined,'',0,59.5,3599.5,3600,360001];
console.log(JSON.stringify(inputs.map(value=>fmtElapsed(value))));
"""
        result = subprocess.run(
            ["node", "-e", probe], check=True, capture_output=True, text=True
        )
        self.assertEqual(json.loads(result.stdout), [
            "—", "—", "—", "00시간 00분 00초", "00시간 00분 59초",
            "00시간 59분 59초", "01시간 00분 00초", "100시간 00분 01초",
        ])
        self.assertIn("trackValue('경과 시간',fmtElapsed(", html)
        self.assertNotIn("trackValue('경과 시간',p.elapsed!=null?p.elapsed+'초'", html)
        self.assertIn("'<b>'+fmtElapsed(p.elapsed)+'</b> 경과", html)
        self.assertNotIn("fmtElapsed(p.elapsed||0)", html)

    def test_mobile_tracking_modal_scrolls_body_without_clipping_rows(self):
        """The iOS tracking sheet keeps its header visible and scrolls every detail row."""
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('<div class="tracking-modal-head">', html)
        self.assertIn('#trackingModal .tracking-modal{width:min(680px,100%);display:flex;flex-direction:column;overflow:hidden}', html)
        self.assertIn('#trackingModalBody{min-height:0;overflow-y:auto;overscroll-behavior:contain', html)
        self.assertIn('height:100dvh;max-height:100dvh;border-radius:0', html)

    def test_windows_installer_uses_dpapi_acl_and_staged_upgrade(self):
        root = Path(__file__).resolve().parents[1] / "windows-worker"
        installer = (root / "Install-H3Worker.ps1").read_text()
        source = (root / "h3_worker.py").read_text()
        config = json.loads((root / "config.example.json").read_text())
        self.assertIn("ProtectedData]::Protect", installer)
        self.assertIn("DataProtectionScope]::CurrentUser", installer)
        self.assertIn("icacls", installer)
        self.assertIn("WindowsIdentity]::GetCurrent().User.Value", installer)
        self.assertIn("$StageRoot", installer)
        self.assertIn("$BackupRoot", installer)
        self.assertIn("Stop-InstalledWorker", installer)
        self.assertIn("Uninstall-H3Worker.ps1", installer)
        self.assertIn("Remove-Item -LiteralPath $tokenFile", installer)
        self.assertNotIn("worker_token = $token", installer)
        self.assertIn("worker_token_dpapi", config)
        self.assertNotIn("worker_token", config)
        self.assertIn("CryptUnprotectData", source)
        self.assertIn("load_worker_token", source)

    def test_windows_worker_comfy_runtime_requires_active_5080_and_all_nodes(self):
        worker = self.load_windows_worker()
        client = worker.ComfyClient("http://127.0.0.1:8188")
        complete = {name: {} for name in worker.REQUIRED_COMFY_CLASSES}
        with patch.object(client, "json", side_effect=[
            {"devices": [{"name": "cuda:0 NVIDIA GeForce RTX 5080"}]}, complete,
        ]):
            client.assert_runtime()
        with patch.object(client, "json", return_value={"devices": [{"name": "cuda:0 RTX 4090"}]}):
            with self.assertRaises(RuntimeError):
                client.assert_runtime()
        missing = dict(complete)
        missing.pop("MiniMaxH3ImageToVideo")
        with patch.object(client, "json", side_effect=[
            {"devices": [{"name": "cuda:0 NVIDIA GeForce RTX 5080"}]}, missing,
        ]):
            with self.assertRaises(RuntimeError):
                client.assert_runtime()

    def test_windows_worker_manifest_roles_paths_and_hashes_are_code_anchored(self):
        worker = self.load_windows_worker()
        manifest = json.loads(worker.MANIFEST_PATH.read_text())
        manifest["models"][0]["role"] = "attacker_substitute"
        with tempfile.TemporaryDirectory() as root:
            tampered = Path(root) / "model-manifest.json"
            tampered.write_text(json.dumps(manifest))
            with patch.object(worker, "MANIFEST_PATH", tampered):
                with self.assertRaises(RuntimeError):
                    worker.validate_package()

    def test_shared_segment_plan_preserves_requested_duration_within_h3_frame_grid(self):
        for seconds in (5, 8, 10, 12, 14, 15, 20, 30, 60):
            for segment_seconds in (6, 8, 10):
                frames = server.segment_frame_plan(seconds, segment_seconds, server.STRATEGY_SPLIT)
                self.assertGreaterEqual(len(frames), 1)
                self.assertLessEqual(len(frames), math.ceil(seconds / segment_seconds))
                self.assertTrue(all(frame >= 124 and (frame - 5) % 17 == 0 for frame in frames))
                self.assertLessEqual(abs(sum(frames) - round(seconds * 24)), 9)
        self.assertEqual(server.segment_frame_plan(5, 8, server.STRATEGY_SINGLE),
                         [server.snap_len(5)])
        source = (Path(__file__).resolve().parents[1] / "windows-worker" / "h3_worker.py").read_text()
        self.assertIn("server.worker_segment_frame_plan", source)

    def test_rtx_long_generation_is_forced_to_vram_safe_h3_segments(self):
        self.assertEqual(
            server.normalize_worker_strategy("rtx5080", 20, server.STRATEGY_SINGLE, 8),
            (server.STRATEGY_SPLIT, 4),
        )
        self.assertEqual(
            server.normalize_worker_strategy("rtx5080", 5, server.STRATEGY_SINGLE, 8),
            (server.STRATEGY_SINGLE, 8),
        )
        self.assertEqual(
            server.normalize_worker_strategy("rtx5080", 20, server.STRATEGY_SPLIT, 8),
            (server.STRATEGY_SPLIT, 4),
        )
        self.assertEqual(
            server.normalize_worker_strategy("pgx", 20, server.STRATEGY_SINGLE, 8),
            (server.STRATEGY_SINGLE, 8),
        )
        worker_source = (
            Path(__file__).resolve().parents[1] / "windows-worker" / "h3_worker.py"
        ).read_text()
        self.assertIn('server.normalize_worker_strategy(', worker_source)
        frames = server.worker_segment_frame_plan(
            "rtx5080", 20, 4, server.STRATEGY_SPLIT,
        )
        self.assertEqual(frames, [server.snap_len(4)] * 4)
        self.assertTrue(all(frame <= server.snap_len(4) for frame in frames))
        self.assertGreaterEqual(sum(frames), round(20 * 24))
        self.assertIn('server.worker_segment_frame_plan(', worker_source)
        self.assertIn('duration_seconds=total_seconds', worker_source)

    def test_rtx_generate_api_persists_vram_safe_split_instead_of_long_single_graph(self):
        heartbeat = {
            "gpu": "NVIDIA GeForce RTX 5080", "vram_mib": 16303,
            "comfy_up": True, "model_ready": True, "generation_verified": True,
            "busy": False, "modes": ["t2v", "i2v"],
            "model_profile": "minimax-h3-pgx-exact-v1",
        }
        body = json.dumps({
            "prompt": "portrait RTX OOM regression",
            "mode": "t2v", "worker_target": "rtx5080",
            "width": 768, "height": 1344, "seconds": 20,
            "steps": 20, "strategy": "single", "seg_seconds": 8,
        }).encode()
        with patch.object(server, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch.dict(server.JOBS, {}, clear=True), \
             patch.object(server, "QUEUE", []), \
             patch.object(server, "QUEUE_RESERVATIONS", {"pgx": 0, "rtx5080": 0}), \
             patch.dict(server.WORKERS, {}, clear=True), \
             patch.object(server, "_save_job"):
            server.record_worker_heartbeat(server.RTX5080_WORKER_ID, heartbeat)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                conn.request("POST", "/api/generate", body=body, headers={
                    "Content-Type": "application/json",
                    server.ORIGIN_HEADER: self.ORIGIN_SECRET,
                })
                response = conn.getresponse()
                receipt = json.loads(response.read())
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
            stored = dict(server.JOBS[receipt["job"]])
            stored["cfg"] = dict(stored["cfg"])
        self.assertEqual(response.status, 200)
        self.assertEqual(receipt["strategy"], server.STRATEGY_SPLIT)
        self.assertEqual(receipt["seg_seconds"], 4)
        self.assertEqual(receipt["segments"], 4)
        self.assertEqual(stored["cfg"]["strategy"], server.STRATEGY_SPLIT)
        self.assertEqual(stored["cfg"]["seg_seconds"], 4)

    def test_windows_worker_extracts_comfy_error_without_logging_the_prompt_graph(self):
        worker = self.load_windows_worker()
        result = {
            "prompt": [0, "prompt-id", {"5": {"inputs": {"prompt": "PRIVATE PROMPT"}}}],
            "status": {"status_str": "error", "completed": False, "messages": [[
                "execution_error", {
                    "node_id": "10", "node_type": "SamplerCustomAdvanced",
                    "exception_type": "torch.OutOfMemoryError",
                    "exception_message": "Allocation would exceed allowed memory",
                },
            ]]},
        }
        message = worker.comfy_execution_failure(result)
        self.assertIn("torch.OutOfMemoryError", message)
        self.assertIn("SamplerCustomAdvanced", message)
        self.assertIn("Allocation would exceed allowed memory", message)
        self.assertNotIn("PRIVATE PROMPT", message)
        self.assertNotIn('"prompt"', message)

    def test_exact_worker_workflow_never_silently_omits_requested_loras(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError):
                server.build_workflow("p", "", 768, 432, 25, 4, 1,
                                      lora_dirs=[root], strict_loras=True)
            Path(root, server.H3_LORA).write_bytes(b"x")
            server.build_workflow("p", "", 768, 432, 25, 4, 1,
                                  lora_dirs=[root], strict_loras=True)
            with self.assertRaises(RuntimeError):
                server.build_workflow("p", "", 768, 432, 25, 4, 1,
                                      realism_lora=True, lora_dirs=[root], strict_loras=True)
            with self.assertRaises(RuntimeError):
                server.build_workflow("p", "", 768, 432, 25, 4, 1,
                                      cam_motion="1000", lora_dirs=[root], strict_loras=True)
        source = (Path(__file__).resolve().parents[1] / "windows-worker" / "h3_worker.py").read_text()
        self.assertIn("strict_loras=True", source)

    def test_windows_worker_concat_manifest_uses_safe_relative_generated_names(self):
        worker = self.load_windows_worker()
        with tempfile.TemporaryDirectory(prefix="o'brien-") as root:
            directory = Path(root)
            segments = [directory / "segment_00.mp4", directory / "segment_01.mp4"]
            for segment in segments:
                segment.write_bytes(b"x")
            output = directory / "final.mp4"
            calls, manifests = [], []

            def fake_ffmpeg(arguments, timeout=600, cwd=None):
                calls.append((arguments, cwd))
                if "concat" in arguments:
                    manifest = Path(cwd) / arguments[arguments.index("-i") + 1]
                    manifests.append(manifest.read_text())
                target = Path(arguments[-1])
                if not target.is_absolute():
                    target = Path(cwd) / target
                target.write_bytes(b"video")

            with patch.object(worker, "run_ffmpeg", side_effect=fake_ffmpeg):
                worker.finish_segments(segments, output, duration_seconds=20)
            self.assertEqual(manifests, ["file 'segment_00.mp4'\nfile 'segment_01.mp4'\n"])
            self.assertEqual(calls[0][1], str(directory))
            self.assertIn("1", calls[0][0][calls[0][0].index("-safe") + 1:calls[0][0].index("-safe") + 2])
            self.assertEqual(calls[1][0][calls[1][0].index("-t") + 1], "20")

    def test_windows_worker_rejects_invalid_upload_acknowledgements(self):
        worker = self.load_windows_worker()
        claim = {"job": {"id": "deadbeef"}, "execution_id": "exec", "lease_token": "lease"}
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "result.mp4"
            video.write_bytes(b"x" * 64)

            class FakeApi:
                def __init__(self, initial, chunk_ack):
                    self.initial, self.chunk_ack, self.completed, self.chunks = initial, chunk_ack, False, 0
                def request(self, path, payload, timeout=60):
                    if path.endswith("/init"):
                        return {"received": self.initial, "chunk_max": 16}
                    if path.endswith("/chunk"):
                        self.chunks += 1
                        if self.chunks > 1:
                            raise AssertionError("worker retried a non-advancing upload acknowledgement")
                        return {"received": self.chunk_ack}
                    self.completed = True
                    return {"ok": True}

            for initial, chunk_ack in ((65, 65), (0, 999), (0, 0)):
                api = FakeApi(initial, chunk_ack)
                with self.assertRaises(RuntimeError):
                    worker.upload_final(api, claim, video)
                self.assertFalse(api.completed)

            class ValidApi:
                def __init__(self):
                    self.received = 0
                def request(self, path, payload, timeout=60):
                    if path.endswith("/init"):
                        return {"received": 0, "chunk_max": 16}
                    if path.endswith("/chunk"):
                        self.assert_offset = payload["offset"]
                        self.received += len(base64.b64decode(payload["data"]))
                        return {"received": self.received}
                    return {"job": {"status": "done"}}

            valid = ValidApi()
            worker.upload_final(valid, claim, video)
            self.assertEqual(valid.received, 64)

    def test_windows_worker_rejects_unsafe_ids_urls_redirects_and_comfy_overrides(self):
        worker = self.load_windows_worker()
        self.assertTrue(worker.valid_job_id("deadbeef"))
        for value in ("", "abc", "../escape", "deadbeef/../../x", "g0000000", "deadbeef00"):
            self.assertFalse(worker.valid_job_id(value), value)
        self.assertEqual(worker.validate_api_base("https://h3-video-web.vercel.app"),
                         "https://h3-video-web.vercel.app")
        for value in ("http://h3-video-web.vercel.app", "https://x.test/path", "https://x.test/?q=1"):
            with self.assertRaises(RuntimeError):
                worker.validate_api_base(value)
        self.assertEqual(worker.validate_comfy_url("http://127.0.0.1:8188"),
                         "http://127.0.0.1:8188")
        for value in ("http://0.0.0.0:8188", "http://localhost:8188", "https://127.0.0.1:8188", "http://127.0.0.1:9999"):
            with self.assertRaises(RuntimeError):
                worker.validate_comfy_url(value)
        self.assertEqual(worker.validate_comfy_args(["--lowvram", "--reserve-vram", "1.5"]),
                         ["--lowvram", "--reserve-vram", "1.5"])
        for args in (["--listen", "0.0.0.0"], ["--port", "9999"], ["--cuda-device", "1"], ["--reserve-vram", "oops"]):
            with self.assertRaises(RuntimeError):
                worker.validate_comfy_args(args)
        with self.assertRaises(urllib.error.HTTPError):
            worker.NoRedirectHandler().redirect_request(
                urllib.request.Request("https://origin.test"), None, 307,
                "redirect", {}, "https://other.test"
            )

    def test_windows_worker_package_is_exact_outbound_and_non_admin(self):
        root = Path(__file__).resolve().parents[1]
        worker = root / "windows-worker"
        result = subprocess.run(
            [sys.executable, str(worker / "h3_worker.py"), "--validate-package"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PACKAGE VALID", result.stdout)
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            for name in ("h3_worker.py", "model-manifest.json"):
                (package / name).write_bytes((worker / name).read_bytes())
            (package / "server.py").write_bytes((root / "server.py").read_bytes())
            isolated = subprocess.run(
                [sys.executable, str(package / "h3_worker.py"), "--validate-package"],
                cwd=package, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(isolated.returncode, 0, isolated.stdout + isolated.stderr)
            self.assertIn("PACKAGE VALID", isolated.stdout)
        manifest = json.loads((worker / "model-manifest.json").read_text())
        self.assertEqual(manifest["profile"], "minimax-h3-pgx-exact-v1")
        self.assertEqual(len(manifest["models"]), 8)
        live_pgx_sizes = {
            "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors": 20975924960,
            "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": 15687142551,
            "vae/minimax_h3_video_vae_fp16.safetensors": 5207808496,
            "vae/minimax_h3_audio_vae_fp32.safetensors": 605254808,
            "loras/minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors": 620285592,
            "loras/h3-realism-people-t2v-i2v-r2v.safetensors": 131229656,
            "loras/cam_motion_1000.safetensors": 155109488,
            "loras/cam_motion_3000.safetensors": 155109680,
        }
        self.assertEqual({item["relative_path"]: item["size"] for item in manifest["models"]}, live_pgx_sizes)
        self.assertTrue(all(item["required"] for item in manifest["models"]))
        self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["models"]))
        source = (worker / "h3_worker.py").read_text()
        installer = (worker / "Install-H3Worker.ps1").read_text()
        self.assertIn('http://127.0.0.1:8188', source)
        self.assertNotIn('--listen", "0.0.0.0', source)
        self.assertIn("def assert_comfy_loopback_only", source)
        self.assertIn("Get-NetTCPConnection", source)
        self.assertIn("if sys.stdout is not None:", source)
        self.assertIn("Register-ScheduledTask", installer)
        self.assertIn("New-ScheduledTaskPrincipal", installer)
        self.assertIn("-LogonType Interactive", installer)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", installer)
        self.assertIn("Start-ScheduledTask", installer)
        self.assertIn("ExecutionTimeLimit", installer)
        self.assertIn("Remove-ItemProperty -Path $LegacyRunKey -Name $LegacyRunName", installer)
        uninstaller = (worker / "Uninstall-H3Worker.ps1").read_text()
        self.assertIn("Unregister-ScheduledTask", uninstaller)
        self.assertIn("Remove-ItemProperty -Path $LegacyRunKey -Name $LegacyRunName", uninstaller)
        token_delete = "Remove-Item -LiteralPath $tokenFile -Force"
        self.assertLess(installer.index(token_delete), installer.index("Locating exact H3 models and ComfyUI"))
        self.assertIn("Worker activation failed; restoring previous installation", installer)
        self.assertIn("Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction SilentlyContinue", installer)
        self.assertGreater(installer.index("Stop-InstalledWorker $InstallRoot"), installer.index("try {", installer.index("$activated = $false")))
        self.assertGreater(installer.rindex("Remove-Item -LiteralPath $BackupRoot"), installer.index("Get-ScheduledTaskInfo"))
        self.assertIn("--lowvram", installer)
        self.assertIn("--reserve-vram", installer)
        self.assertIn("System.Text.UTF8Encoding($false)", installer)
        self.assertIn("Add-Type -AssemblyName System.Security", installer)
        self.assertNotIn("Set-Content -LiteralPath (Join-Path $InstallRoot 'config.json')", installer)
        self.assertIn("run_local_generation_smoke", source)
        self.assertIn("ComfyUI recovery failed", source)
        self.assertIn("ensure_comfy(comfy_root, comfy, config)", source)
        self.assertIn("generation_marker_valid", source)
        self.assertIn("for path in (MANIFEST_PATH, shared_server_path(), Path(__file__).resolve()):", source)
        self.assertIn('"generation_verified": generation_verified', source)
        self.assertIn("server.worker_segment_frame_plan", source)
        self.assertNotIn("RunAs", installer)
        self.assertFalse((worker / "worker-token.txt").exists())

    def test_worker_ui_escapes_phase_preserves_selection_and_single_job_has_queue_position(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn("esc(p.phase||'영상 생성 중…')", html)
        self.assertNotIn("if(!rtx.eligible&&WORKER_TARGET==='rtx5080') WORKER_TARGET='pgx'", html)
        self.assertNotIn("WORKER_TARGET='pgx';\n    $('#workerToggle').disabled=true", html)

        job = {"id": "deadbeef", "status": "queued", "cfg": {"worker_target": "rtx5080"}}
        with patch.object(server, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch.dict(server.JOBS, {"deadbeef": job}, clear=True), \
             patch.object(server, "QUEUE", ["deadbeef"]):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                conn.request("GET", "/api/job/deadbeef", headers={server.ORIGIN_HEADER: self.ORIGIN_SECRET})
                response = conn.getresponse()
                payload = json.loads(response.read())
                conn.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["job"]["queue_position"], 1)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_worker_toggle_routes_one_target_and_surfaces_independent_queues(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('id="workerToggle"', html)
        self.assertIn("let WORKER_TARGET='pgx'", html)
        self.assertIn("body.worker_target=WORKER_TARGET;", html)
        self.assertIn("workerToggle.disabled=!rtx.eligible", html)
        self.assertIn("RTX 5080 실제 H3 생성 검증 대기", html)
        self.assertIn("const workerLabel=", html)
        self.assertIn("j.queue_position", html)
        self.assertNotIn("fanout", html.lower())

    def test_recent_handle_is_svg_icon_and_tracking_identity_is_first(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        start = html.index('<button class="recent-drawer-handle"')
        end = html.index('</button>', start)
        handle = html[start:end]
        self.assertIn("<svg", handle)
        self.assertIn('id="recentCountBadge"', handle)
        self.assertNotIn('recent-handle-label', handle)
        self.assertIn("today_completed_count", html)
        self.assertIn("if(!r.ok||!d.ok)throw", html)
        self.assertNotIn("badge.textContent=jobs.length", html)
        render = html[html.index("function renderTracking(j)"):html.index("function showJobTracking", html.index("function renderTracking(j)"))]
        self.assertIn("track-identity", render)
        self.assertLess(render.index("track-identity"), render.index("track-status"))
        self.assertNotIn("trackValue('작업 ID'", render)

    def test_recent_jobs_open_as_a_right_edge_swipe_drawer(self):
        """Recent work is reachable without scrolling to the bottom of the page."""
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('id="recentModal"', html)
        self.assertIn('id="recentModalOpen"', html)
        self.assertIn('class="recent-drawer-handle"', html)
        self.assertIn('id="recentModalClose"', html)
        self.assertIn('id="recentModalList"', html)
        self.assertNotIn('id="recentcard"', html)
        self.assertIn('function bindRecentSwipeGestures()', html)
        self.assertIn("function recentEdgeWidth()", html)
        self.assertIn("edgeStartX>=window.innerWidth-recentEdgeWidth()", html)
        self.assertIn("edgeDeltaX<=-56", html)
        self.assertIn("drawerDeltaX>=70", html)
        self.assertIn("touchmove", html)
        self.assertIn("touchcancel", html)
        edge_touchstart = html.index("document.addEventListener('touchstart',event=>{")
        edge_touchstart_end = html.index("},{passive:true});", edge_touchstart)
        edge_touchstart_source = html[edge_touchstart:edge_touchstart_end]
        self.assertIn("clearTimeout(settleTimer); settleTimer=null;", edge_touchstart_source)
        self.assertLess(edge_touchstart_source.index("clearTimeout(settleTimer);"), edge_touchstart_source.index("edgeStartX=touch.clientX"))
        self.assertIn("resetEdgeSwipe", html)
        self.assertIn("resetDrawerSwipe", html)
        self.assertIn("passive:false", html)
        self.assertIn("setRecentBackgroundInert(true)", html)
        self.assertIn("setRecentBackgroundInert(false)", html)
        self.assertIn("function trapRecentFocus(event)", html)
        self.assertIn("if(event.key==='Tab') trapRecentFocus(event);", html)
        self.assertIn("setTimeout(()=>{ if(modal.classList.contains('show'))", html)
        self.assertIn("function beginRecentEdgePreview()", html)
        self.assertIn("function updateRecentEdgePreview(deltaX)", html)
        self.assertIn("function settleRecentEdgePreview(shouldOpen)", html)
        self.assertIn("modal.classList.add('swipe-preview')", html)
        self.assertIn("requestAnimationFrame(()=>requestAnimationFrame", html)
        self.assertIn('.modal-overlay#recentModal{display:flex;visibility:hidden;pointer-events:none;justify-content:flex-end', html)
        self.assertIn('#recentModal.swipe-preview{visibility:visible;pointer-events:none;transition-delay:0s}', html)
        self.assertIn('transition:background-color .28s', html)
        self.assertIn('will-change:transform', html)
        self.assertIn('@media(prefers-reduced-motion:reduce){#recentModal,#recentModal .recent-modal{transition:none!important}}', html)
        self.assertIn('transform:translateX(100%)', html)
        self.assertIn('#recentModal.show .recent-modal{transform:translateX(0)', html)
        self.assertIn('right:env(safe-area-inset-right,0px)', html)
        self.assertIn("fetch('/api/job/'+jid)", html)
        self.assertIn('ComfyUI 원본 측정값', html)
        self.assertIn('리얼리즘 LoRA', html)

    def test_completed_video_ui_uses_same_origin_nas_range_routes(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn("function completedMedia(jid)", html)
        self.assertIn("return {view:'/api/view/'+encoded+'?v='+MEDIA_STREAM_VERSION,download:'/api/download/'+encoded};", html)
        self.assertIn("async function openCompletedVideo", html)
        self.assertIn("async function saveCompletedVideo", html)
        self.assertNotIn("/api/media-url/", html)
        self.assertNotIn("media-origin", html)
        for forbidden in ("r2 signed url", "r2_media_", "r2://", "cloudflare", "trycloudflare"):
            self.assertNotIn(forbidden, html.lower())

    def test_apple_watch_viewer_is_loaded_and_reports_buffering_state(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "index.html").read_text()
        css = (root / "apple-redesign.css").read_text()
        self.assertIn('href="/apple-redesign.css?v=20260909-swipe1"', html)
        self.assertIn('id="videoStatus"', html)
        self.assertIn('id="modalPlaybackRate"', html)
        self.assertIn('id="modalPip"', html)
        self.assertIn('id="modalFullscreen"', html)
        self.assertIn('id="modalSave"', html)
        self.assertIn("function bindModalPlayerTelemetry()", html)
        self.assertIn("'waiting'", html)
        self.assertIn("'stalled'", html)
        self.assertIn('.video-watch', css)
        self.assertIn('.watch-actions', css)

    def test_completed_video_stream_is_uncached_versioned_and_reloaded_for_ios(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "index.html").read_text()
        self.assertEqual(backend_proxy.PUBLIC_VIDEO_CACHE_CONTROL, "private, no-store")
        self.assertIn('("Vary", "Range")', (root / "backend_proxy.py").read_text())
        self.assertIn('("CDN-Cache-Control", "no-store")', (root / "backend_proxy.py").read_text())
        self.assertIn('("Vercel-CDN-Cache-Control", "no-store")', (root / "backend_proxy.py").read_text())
        self.assertIn("const MEDIA_STREAM_VERSION='20260909-playback2';", html)
        self.assertIn("view:'/api/view/'+encoded+'?v='+MEDIA_STREAM_VERSION", html)
        open_video = html[html.index("async function openCompletedVideo"):html.index("async function saveCompletedVideo")]
        self.assertIn("if($('#recentModal').classList.contains('show')) closeRecentModal();", open_video)
        self.assertLess(open_video.index("closeRecentModal()"), open_video.index("showVideo("))
        self.assertIn('id="player" controls playsinline preload="metadata"', html)
        done = html[html.index("} else if(j.status==='done')"):html.index("} else if(j.status==='error')")]
        self.assertIn("resultPlayer.poster='/api/thumbnail/'+encodeURIComponent(j.id);", done)
        self.assertIn("resultPlayer.load();", done)

    def test_top_progress_card_uses_many_job_stable_running_and_completion_characters(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "index.html").read_text()
        css = (root / "apple-redesign.css").read_text()
        self.assertIn("wrap.insertBefore(job,workspace)", html)
        self.assertIn('id="progressCharacter"', html)
        self.assertIn("function progressCharacterForJob", html)
        self.assertIn("function setProgressCharacter", html)
        running_match = re.search(r"const RUNNING_CHARACTERS=(\[[^;]+\]);", html)
        complete_match = re.search(r"const COMPLETE_CHARACTERS=(\[[^;]+\]);", html)
        self.assertIsNotNone(running_match)
        self.assertIsNotNone(complete_match)
        running = json.loads(running_match.group(1))
        complete = json.loads(complete_match.group(1))
        self.assertGreaterEqual(len(running), 32)
        self.assertGreaterEqual(len(complete), 16)
        self.assertTrue(set(running).isdisjoint(set(complete)))
        self.assertIn("setProgressCharacter(j.id,'running',measuredPct)", html)
        self.assertIn("setProgressCharacter(j.id,'complete',100)", html)
        self.assertIn(".progress-character", css)
        self.assertIn("@keyframes characterRun", css)

    def test_recent_work_handle_is_a_crisp_vertical_edge_tab(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "index.html").read_text()
        css = (root / "apple-redesign.css").read_text()
        start = html.index('<button class="recent-drawer-handle"')
        end = html.index('</button>', start)
        handle = html[start:end]
        self.assertNotIn('recent-handle-label', handle)
        self.assertIn('aria-label="최근 작업 열기"', handle)
        self.assertIn('viewBox="0 0 32 32"', handle)
        handle_css = css[css.index(".recent-drawer-handle"):]
        self.assertIn("position:fixed", handle_css)
        self.assertIn("width:48px", handle_css)
        self.assertIn("height:78px", handle_css)
        self.assertNotIn("writing-mode:vertical-rl", handle_css)
        self.assertIn("vector-effect:non-scaling-stroke", handle_css)
        self.assertIn("width:44px;min-width:0;height:72px", handle_css)

    def test_header_uses_a_layered_inline_svg_app_icon(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "index.html").read_text()
        css = (root / "apple-redesign.css").read_text()
        start = html.index('<div class="logo"')
        end = html.index('</div>', start)
        logo = html[start:end]
        self.assertIn('<svg viewBox="0 0 48 48"', logo)
        self.assertIn("<linearGradient", logo)
        self.assertIn("<path", logo)
        self.assertNotIn(">▶<", logo)
        self.assertIn(".logo svg", css)
        self.assertNotIn('.logo::after{content:"▶"', css)

    def test_sampling_steps_max_at_twenty_in_ui_and_api(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertEqual(server.STEPS_MAX, 20)
        self.assertIn('id="steps" min="2" max="20" step="1" value="6"', html)
        self.assertIn('11–20: 최고 품질', html)

    def test_photo_i2v_uses_reference_to_video_and_preserves_original_prompt(self):
        """A person reference must not be reduced to a stretched first frame."""
        original = "a woman calmly walks through a sunlit garden"
        with patch.object(server.os.path, "exists", return_value=True):
            workflow = server.build_workflow(
                original, "", 768, 1344, 361, 6, 1,
                image_name="immutable-reference.png",
            )
        node = workflow["5"]
        self.assertEqual(node["class_type"], "MiniMaxH3ReferenceToVideo")
        inputs = node["inputs"]
        self.assertEqual(inputs["audio_vae"], ["4", 0])
        self.assertEqual(inputs["ref_images.ref_image_1"], ["15", 0])
        self.assertEqual(inputs["ref_image_size"], "match")
        self.assertNotIn("first_frame", inputs)
        self.assertIn("<Picture 1>", inputs["prompt"])
        self.assertIn(original, inputs["prompt"])

    def test_text_to_video_keeps_the_plain_h3_workflow(self):
        with patch.object(server.os.path, "exists", return_value=True):
            workflow = server.build_workflow("a landscape", "", 768, 1344, 121, 6, 1)
        self.assertEqual(workflow["5"]["class_type"], "MiniMaxH3ImageToVideo")
        self.assertNotIn("ref_images.ref_image_1", workflow["5"]["inputs"])
        self.assertNotIn("<Picture 1>", workflow["5"]["inputs"]["prompt"])

    def test_photo_i2v_duration_is_limited_to_h3s_supported_range(self):
        self.assertEqual(server.normalize_generation_seconds(15, "i2v"), 15.0)
        self.assertEqual(server.normalize_generation_seconds(20, "t2v"), 20.0)
        with self.assertRaisesRegex(ValueError, "15초"):
            server.normalize_generation_seconds(15.01, "i2v")
        with self.assertRaisesRegex(ValueError, "15초"):
            server.normalize_generation_seconds(20, "i2v")

    def test_photo_i2v_limit_is_enforced_server_side_and_disclosed_in_the_ui(self):
        handler_source = inspect.getsource(server.Handler.do_POST)
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('normalize_generation_seconds(data.get("seconds"), mode)', handler_source)
        self.assertIn("I2V_REFERENCE_MAX_SECONDS=15", html)
        self.assertIn("syncI2VDurationLimit()", html)
        self.assertIn("ReferenceToVideo", html)

    def test_nas_completed_video_routes_stream_without_external_redirect(self):
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            source = os.path.join(nas, "completed", "nasjob.mp4")
            os.makedirs(os.path.dirname(source))
            Path(source).write_bytes(b"0123456789")
            job = {"id": "nasjob", "status": "done", "src": source, "file": "nasjob.mp4"}
            with patch.object(server, "NAS_DIR", nas), \
                 patch.dict(server.JOBS, {"nasjob": job}, clear=True):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                    conn.request("GET", "/api/view/nasjob", headers={"Range": "bytes=2-5"})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), b"2345")
                    self.assertEqual(response.getheader("Content-Range"), "bytes 2-5/10")
                    self.assertIsNone(response.getheader("Location"))
                    conn.close()
                    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
                    conn.request("GET", "/api/download/nasjob")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"0123456789")
                    self.assertTrue((response.getheader("Content-Disposition") or "").startswith("attachment"))
                    self.assertIsNone(response.getheader("Location"))
                    conn.close()
                finally:
                    httpd.shutdown()
                    httpd.server_close()

    def test_nas_archive_verifies_bytes_before_releasing_nas_worker_intermediates(self):
        """Completed MP4s stay on NAS; only verified NAS worker copies are removed."""
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            work = os.path.join(nas, ".h3-work", "job123")
            comfy = os.path.join(nas, "comfy-output", "h3web")
            os.makedirs(work)
            os.makedirs(comfy)
            final = os.path.join(work, "job123.mp4")
            segment = os.path.join(work, "seg_00.mp4")
            comfy_source = os.path.join(comfy, "job123_source.mp4")
            Path(final).write_bytes(b"verified-final-video")
            Path(segment).write_bytes(b"worker-segment")
            Path(comfy_source).write_bytes(b"comfy-source")
            with patch.object(server, "NAS_DIR", nas):
                archived = server.archive_final_to_nas(
                    "job123", final, cleanup_paths=(segment, comfy_source)
                )

            expected = os.path.join(nas, "job123.mp4")
            self.assertEqual(archived["src"], expected)
            self.assertTrue(archived["nas_saved"])
            self.assertEqual(Path(expected).read_bytes(), b"verified-final-video")
            self.assertEqual(archived["sha256"], hashlib.sha256(b"verified-final-video").hexdigest())
            self.assertFalse(os.path.exists(final))
            self.assertFalse(os.path.exists(segment))
            self.assertFalse(os.path.exists(comfy_source))

    def test_removed_external_media_url_route_returns_standard_not_found(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
            conn.request("GET", "/api/media-url/nasjob")
            response = conn.getresponse()
            self.assertEqual(response.status, 404)
            self.assertEqual(json.loads(response.read()), {"ok": False, "error": "not found"})
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_fixed_video_reference_is_nas_backed_without_pgx_mp4(self):
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            refv_dir = os.path.join(nas, ".h3-web", "refv")
            with patch.object(server, "NAS_DIR", nas), \
                 patch.object(server, "REFV_DIR", refv_dir), \
                 patch.object(server, "REFV_META", os.path.join(refv_dir, "meta.json")):
                meta = server._save_refv(b"video-data", b"png-frame", 640, 360, "person.mp4", 2.0, 0.5)
                self.assertTrue(server._refv_video_path().startswith(nas + os.sep))
                self.assertEqual(Path(server._refv_video_path()).read_bytes(), b"video-data")
                self.assertEqual(meta["sha256"], hashlib.sha256(b"video-data").hexdigest())
                self.assertTrue(os.path.isfile(server._refv_path()))
                public = server._load_refv()
                self.assertEqual(public["name"], "person.mp4")
                self.assertNotIn("video_path", public)
                self.assertNotIn("r2_key", public)

    def test_reference_worker_retries_nas_snapshot_and_cleans_only_after_success(self):
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            source = os.path.join(nas, ".h3-web", "inputs", "retry.mp4")
            os.makedirs(os.path.dirname(source), exist_ok=True)
            Path(source).write_bytes(b"reference-video")
            cfg = {
                "video_source_path": source,
                "video_source_name": "h3web_refv_retry.mp4",
                "video_source_sha256": hashlib.sha256(b"reference-video").hexdigest(),
                "video_source_size": len(b"reference-video"), "video_name": "",
            }
            with patch.object(server, "NAS_DIR", nas), \
                 patch.object(server, "comfy_upload_video", side_effect=[RuntimeError("retry"), "comfy-ref.mp4"]) as upload, \
                 patch.object(server.time, "sleep"), patch.object(server, "_save_job"):
                server._upload_job_references("nasretry", cfg)
            self.assertEqual(upload.call_count, 2)
            self.assertFalse(os.path.exists(source))
            self.assertEqual(cfg["video_name"], "comfy-ref.mp4")
            self.assertEqual(cfg["video_source_path"], "")

    def test_worker_uploads_nas_reference_and_removes_only_job_owned_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            source = os.path.join(nas, ".h3-web", "inputs", "naswork.mp4")
            os.makedirs(os.path.dirname(source), exist_ok=True)
            Path(source).write_bytes(b"reference-video")
            cfg = {
                "video_source_path": source,
                "video_source_name": "h3web_refv_naswork.mp4",
                "video_source_sha256": hashlib.sha256(b"reference-video").hexdigest(),
                "video_source_size": len(b"reference-video"), "video_name": "",
            }
            with patch.object(server, "NAS_DIR", nas), \
                 patch.object(server, "comfy_upload_video", return_value="comfy-ref.mp4") as upload, \
                 patch.object(server, "_save_job"):
                server._upload_job_references("naswork", cfg)
            upload.assert_called_once_with(b"reference-video", "h3web_refv_naswork.mp4")
            self.assertFalse(os.path.exists(source))
            self.assertEqual(cfg["video_name"], "comfy-ref.mp4")
            self.assertEqual(cfg["video_source_path"], "")

    def test_reference_worker_rejects_non_nas_snapshot_before_upload_or_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            nas = os.path.join(root, "nas")
            source = os.path.join(root, "pgx-local-reference.mp4")
            Path(source).write_bytes(b"must-not-leave-pgx")
            cfg = {
                "video_source_path": source,
                "video_source_name": "pgx-local-reference.mp4",
                "video_source_sha256": hashlib.sha256(b"must-not-leave-pgx").hexdigest(),
                "video_source_size": len(b"must-not-leave-pgx"),
                "video_name": "",
            }
            with patch.object(server, "NAS_DIR", nas), \
                 patch.object(server, "comfy_upload_video") as upload:
                with self.assertRaisesRegex(RuntimeError, "NAS"):
                    server._upload_job_references("localonly", cfg)
            upload.assert_not_called()
            self.assertTrue(os.path.isfile(source))

    def test_backend_proxy_hides_upstream_connection_details(self):
        started = []
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/jobs", "QUERY_STRING": "", "wsgi.input": None}
        with patch.object(backend_proxy, "ORIGIN_SECRET", self.ORIGIN_SECRET), \
             patch("urllib.request.urlopen", side_effect=OSError("private-pgx-host is unavailable")):
            result = backend_proxy.handler(env, lambda status, headers: started.extend([status, dict(headers)]))
        self.assertEqual(started[0], "502")
        self.assertEqual(started[1]["Content-Type"], "application/json")
        body = b"".join(result)
        self.assertEqual(json.loads(body), {"ok": False, "error": "backend unavailable"})
        self.assertNotIn(b"private-pgx-host", body)

    def test_completed_generation_archives_to_nas_before_marking_done(self):
        source = inspect.getsource(server.run_job)
        full_source = (Path(__file__).resolve().parents[1] / "server.py").read_text()
        self.assertIn("archive_final_to_nas(", source)
        self.assertIn("job_id, final_local", source)
        self.assertIn('storage="nas"', source)
        self.assertIn('nas_saved=bool(archive.get("nas_saved"))', source)
        self.assertNotIn("archive_final_to_r2(", full_source)
        self.assertNotIn("r2_key=", full_source)
        self.assertNotIn("video_source_r2_key", full_source)


if __name__ == "__main__":
    unittest.main()
