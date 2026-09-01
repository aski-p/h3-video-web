import http.client
import os
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
                if getattr(self, "done", False):
                    return b""
                self.done = True
                return b"2345"
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
        self.assertEqual(b"".join(result), b"2345")


if __name__ == "__main__":
    unittest.main()
