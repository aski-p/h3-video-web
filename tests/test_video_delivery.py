import http.client
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import backend_proxy
import server


class VideoDeliveryTests(unittest.TestCase):
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
        with patch("urllib.request.urlopen", fake_open):
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
    def test_video_ui_has_closeable_in_page_player_and_distinguishes_queue(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.assertIn('id="videoModal"', html)
        self.assertIn('aria-label="영상 닫기"', html)
        self.assertIn("function showVideo", html)
        self.assertIn("st==='queued'){ stTxt='대기열'", html)

    def test_remote_archive_stream_uses_busybox_compatible_byte_ranges(self):
        source = (Path(__file__).resolve().parents[1] / "server.py").read_text()
        self.assertIn('"bs=1", f"skip={start}", f"count={length}"', source)
        self.assertNotIn('"iflag=skip_bytes"', source)

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
        self.assertIn("fetch('/api/job/'+jid)", html)
        self.assertIn('ComfyUI 원본 측정값', html)
        self.assertIn('리얼리즘 LoRA', html)


if __name__ == "__main__":
    unittest.main()
