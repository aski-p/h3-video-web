import http.client
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import backend_proxy
import server


class VideoDeliveryTests(unittest.TestCase):
    ORIGIN_SECRET = "unit-test-origin-secret"

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

    def test_proxy_fails_closed_without_origin_secret(self):
        started = []
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/jobs", "QUERY_STRING": "", "wsgi.input": None}
        with patch.object(backend_proxy, "ORIGIN_SECRET", ""), \
             patch("urllib.request.urlopen") as urlopen:
            result = backend_proxy.handler(env, lambda status, headers: started.extend([status, dict(headers)]))
        self.assertEqual(started[0], "503")
        self.assertEqual(json.loads(b"".join(result))["error"], "origin authentication unavailable")
        urlopen.assert_not_called()

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

    def test_comfy_recovery_never_starts_failed_duplicate_user_unit(self):
        source = inspect.getsource(server.ensure_comfyui)
        self.assertIn("systemctl start comfyui-minimax-h3.service", source)
        self.assertNotIn("systemctl --user start minimax-h3-comfyui.service", source)

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

    def test_completed_history_marks_final_job_complete(self):
        job = {"id": "job", "started": 1, "segments": 1, "status": "running"}
        history = {"ours": {"status": {"completed": True, "status_str": "success"}}}
        with patch.dict(server.JOBS, {"job": job}, clear=True), patch.object(server, "_save_job"):
            self.assertEqual(server.reconcile_comfy_prompt("job", "ours", history, {}, final=True), "completed")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["progress"]["pct"], 100)

    def test_progress_transport_is_installed_and_reconnects_after_socket_loss(self):
        """A transient WebSocket loss must not make measured progress disappear forever."""
        root = Path(__file__).resolve().parents[1]
        requirements = (root / "requirements.txt").read_text()
        source = (root / "server.py").read_text()
        self.assertIn("websocket-client", requirements)
        self.assertIn("ws_retry_at", source)
        self.assertIn("ws = _comfy_ws(client_id)", source)
        self.assertIn('phase="ComfyUI 연결 복구 중"', source)

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
        self.assertIn("wrap.insertBefore(job,workspace)", html)
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
        self.assertIn("ExecStart=/usr/bin/python3 /home/aski/h3-web/server.py", unit)
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

    def test_save_buttons_use_explicit_mobile_safe_handler_and_visible_status(self):
        """Save cannot rely on an anchor's download attribute in iOS/PWA."""
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('<button class="dl" id="dl1" type="button">⬇ MP4 저장</button>', html)
        self.assertIn('id="saveStatus"', html)
        self.assertIn('async function saveVideo(', html)
        self.assertIn('navigator.share', html)
        self.assertIn("saveCompletedVideo(j.id, j.id+'.mp4', $('#dl1'))", html)
        self.assertIn("saveCompletedVideo(j.id, j.id+'.mp4', saveB)", html)
        self.assertIn("return saveVideo(media.download,filename||jid+'.mp4',button);", html)
        self.assertNotIn('id="dl1" download', html)
        self.assertNotIn('href="/api/download/${j.id}" download="${j.id}.mp4"', html)

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

    def test_recent_jobs_are_managed_in_a_full_screen_modal(self):
        """Recent work must not be trapped in the small inline dashboard card."""
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('id="recentModal"', html)
        self.assertIn('id="recentModalOpen"', html)
        self.assertIn('id="recentModalClose"', html)
        self.assertIn('id="recentModalList"', html)
        self.assertIn('function showRecentModal()', html)
        self.assertIn('function closeRecentModal()', html)
        self.assertIn("fetch('/api/job/'+jid)", html)
        self.assertIn('ComfyUI 원본 측정값', html)
        self.assertIn('리얼리즘 LoRA', html)

    def test_completed_video_ui_uses_same_origin_nas_range_routes(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn("function completedMedia(jid)", html)
        self.assertIn("return {view:'/api/view/'+encoded,download:'/api/download/'+encoded};", html)
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
        self.assertIn('href="/apple-redesign.css?v=20260908-nas"', html)
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
