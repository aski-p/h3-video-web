import io
import json
import os
import unittest
from unittest.mock import patch
import backend_proxy
import studio_auth as auth

class StudioAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"H3_ORIGIN_SECRET": "origin-test", "ORIGINAL_VIDEO_TOKEN": "service-test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, path, method="POST", body=None, **extra):
        raw = json.dumps(body or {}).encode()
        environ = {"PATH_INFO": path, "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw), **extra}
        seen = {}
        def start(status, headers):
            seen.update(status=status, headers=dict(headers))
        data = b"".join(backend_proxy.proxy(environ, start))
        return seen, data

    def test_password_login_requires_same_origin_and_sets_secure_session(self):
        token = "a" * 64
        with patch.object(backend_proxy, "ORIGIN_SECRET", "origin-test"), patch.object(auth, "call", return_value={"token": token, "user": {"id": "admin"}}) as rpc:
            response, body = self.invoke("/api/auth/password", body={"key": "1234"}, HTTP_ORIGIN=auth.H3)
            self.assertTrue(response["status"].startswith("200"))
            self.assertTrue(json.loads(body)["ok"])
            self.assertIn("HttpOnly", response["headers"]["Set-Cookie"])
            rpc.assert_called_once_with("password", key="1234")
        with patch.object(backend_proxy, "ORIGIN_SECRET", "origin-test"), patch.object(auth, "call") as rpc:
            response, body = self.invoke("/api/auth/password", body={"key": "1234"}, HTTP_ORIGIN="https://evil.invalid")
            self.assertTrue(response["status"].startswith("403"))
            rpc.assert_not_called()

    def test_recent_history_uses_session_scoped_store_without_slow_gpu_statistics(self):
        context = {"service": False, "session": "session", "user": {"id": "owner"}}
        with patch.object(backend_proxy, "ORIGIN_SECRET", "origin-test"), patch.object(auth, "actor", return_value=context), patch.object(auth, "call", return_value={"ok": True, "jobs": []}) as rpc, patch("urllib.request.urlopen") as gpu:
            response, body = self.invoke("/api/jobs", method="GET", QUERY_STRING="recent=1")
            self.assertEqual(response["status"], "200")
            self.assertTrue(json.loads(body)["ok"])
            rpc.assert_called_once_with("recent", session="session")
            gpu.assert_not_called()

    def test_anonymous_and_revoked_generation_never_reaches_gpu(self):
        with patch.object(backend_proxy, "ORIGIN_SECRET", "origin-test"), patch.object(auth, "call", side_effect=auth.AuthError("login", 401)), patch("urllib.request.urlopen") as upstream:
            r, _ = self.invoke("/api/generate", body={"prompt": "test"})
            self.assertEqual(r["status"], "401")
            upstream.assert_not_called()

    def test_forged_service_token_and_cross_origin_mutations_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.actor({"HTTP_X_ASKI_ORIGINAL_TOKEN": "forged"})
        with patch.object(auth, "call", return_value={"user": {"id": "owner", "admin": False}}):
            with self.assertRaises(auth.AuthError):
                auth.actor({"REQUEST_METHOD": "POST", "HTTP_ORIGIN": "https://evil.invalid"})
            self.assertEqual(auth.actor({"REQUEST_METHOD": "POST", "HTTP_ORIGIN": auth.H3})["user"]["id"], "owner")

    def test_ownership_checks_and_admin_controls(self):
        context = {"user": {"id": "owner", "admin": False}}
        with patch.object(auth, "lookup", return_value={"1234abcd": {"owner_id": "other"}}):
            with self.assertRaises(auth.AuthError):
                auth.restrict({"PATH_INFO": "/api/cancel/1234abcd", "REQUEST_METHOD": "POST"}, context)
        with patch.object(auth, "lookup", return_value={"1234abcd": {"owner_id": "owner"}}):
            auth.restrict({"PATH_INFO": "/api/cancel/1234abcd", "REQUEST_METHOD": "POST"}, context)
        with self.assertRaises(auth.AuthError):
            auth.restrict({"PATH_INFO": "/api/pgx-mode", "REQUEST_METHOD": "POST"}, context)
        auth.restrict({"PATH_INFO": "/api/upload", "REQUEST_METHOD": "POST"}, context)

    def test_prepare_namespaces_and_discards_forged_metadata_before_admission(self):
        response = {"requestKey": "h3-owner-server-id", "title": "real", "user": {"id": "owner"}}
        with patch.object(auth, "call", return_value=response) as rpc:
            body, prepared = auth.prepare(json.dumps({"prompt": "real", "owner_id": "other", "owner_email": "fake", "client_request_id": "stable"}).encode(), {"session": "session-token"})
            data = json.loads(body)
            self.assertEqual(data["client_request_id"], response["requestKey"])
            self.assertNotIn("owner_id", data)
            self.assertNotIn("owner_email", data)
            self.assertEqual(rpc.call_args.kwargs["session"], "session-token")
            auth.bind({"job": "1234abcd"}, prepared)
            self.assertEqual(rpc.call_args.args[0], "bind")

    def test_job_list_filters_other_owners_and_attaches_verified_titles(self):
        rows = {"1234abcd": {"owner_id": "owner", "title": "Rain", "studio_users": {"id": "owner", "email": "owner@test.invalid"}}, "5678abcd": {"owner_id": "other", "title": "PRIVATE", "studio_users": {"id": "other", "email": "other@test.invalid"}}}
        with patch.object(auth, "lookup", return_value=rows):
            out = auth.jobs({"jobs": [{"id": "1234abcd"}, {"id": "5678abcd"}]}, {"user": {"id": "owner", "admin": False}})
            self.assertEqual(len(out["jobs"]), 1)
            self.assertEqual(out["jobs"][0]["title"], "Rain")
            self.assertNotIn("PRIVATE", json.dumps(out))

    def test_login_flow_is_signed_state_bound_and_pkce_verified(self):
        seen = {}
        def start(status, headers):
            seen.update(status=status, headers=dict(headers))
        auth.auth_route({"PATH_INFO": "/api/auth/login", "REQUEST_METHOD": "GET"}, start)
        self.assertTrue(seen["headers"]["Location"].startswith(auth.STUDIO + "/api/h3-auth?"))
        self.assertIn("challenge=", seen["headers"]["Location"])
        self.assertIn("HttpOnly", seen["headers"]["Set-Cookie"])
        with self.assertRaises(auth.AuthError):
            auth.unseal("forged.bad")
        sealed = auth.seal({"state": "expected", "verifier": "v", "at": 0})
        with self.assertRaises(auth.AuthError):
            auth.unseal(sealed)

    def test_existing_signed_media_routes_do_not_require_browser_session(self):
        class Reply(io.BytesIO):
            status = 200
            headers = {"Content-Type": "video/mp4"}
        with patch.object(backend_proxy, "ORIGIN_SECRET", "origin-test"), patch.object(auth, "actor", side_effect=AssertionError("must preserve media ticket")), patch("urllib.request.urlopen", return_value=Reply(b"video")):
            r, payload = self.invoke("/api/media-ticket/view/1234abcd", method="GET")
            self.assertEqual(payload, b"video")

if __name__ == "__main__":
    unittest.main()
