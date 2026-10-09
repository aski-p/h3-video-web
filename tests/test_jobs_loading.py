import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import backend_proxy


class JobsLoadingTests(unittest.TestCase):
    def request(self, query, data):
        started = []
        response = io.BytesIO(json.dumps(data).encode())
        with patch.object(backend_proxy, 'ORIGIN_SECRET', 'test-secret'), \
             patch('urllib.request.urlopen', return_value=response) as opened:
            result = backend_proxy.handler({'REQUEST_METHOD': 'GET', 'PATH_INFO': '/api/jobs',
                                            'QUERY_STRING': query},
                                           lambda status, headers: started.extend([status, dict(headers)]))
            payload = b''.join(result)
        return started, payload, opened

    def test_recent_list_stops_reading_before_unused_history(self):
        data = {'ok': True, 'jobs': [{'id': str(n), 'prompt': 'x' * 10000} for n in range(400)],
                'today_completed_count': 12, 'workers': {'pgx': {'busy': True}}, 'queue_len': 3}
        started, payload, opened = self.request('limit=10', data)
        result = json.loads(payload)
        self.assertEqual(started[0], '200 OK')
        self.assertEqual(len(result['jobs']), 10)
        self.assertIsNone(result['jobs_total'])
        self.assertIsNone(result['today_completed_count'])
        self.assertTrue(result['partial'])
        self.assertLess(len(payload), 110000)
        self.assertIn('limit=10', opened.call_args.args[0].full_url)
        self.assertEqual(started[1]['Cache-Control'], 'private, no-store')

    def test_status_only_response_excludes_history(self):
        _, payload, _ = self.request('limit=0', {'ok': True, 'jobs': [{'id': 'a'}], 'active_job': 'a'})
        self.assertEqual(json.loads(payload)['jobs'], [])

    def test_unicode_braces_and_short_lists_are_parsed_as_json(self):
        for jobs in [[], [{'id': 'one', 'prompt': '한글 { \"quoted\" }'}]]:
            _, payload, _ = self.request('limit=10', {'ok': True, 'jobs': jobs})
            self.assertEqual(json.loads(payload)['jobs'], jobs)

    def test_remaining_history_is_never_consumed(self):
        class PrefixOnly(io.BytesIO):
            def read(self, size=-1):
                if self.tell() >= 8192:
                    raise AssertionError('Read unused history')
                return super().read(size)
        response = PrefixOnly(json.dumps({'ok': True, 'jobs': [{'id': 'a'}] +
                                         [{'id': 'b', 'prompt': 'x' * 1000000}]}).encode())
        result = backend_proxy._recent_jobs_prefix(response, 1)
        self.assertEqual(result['jobs'], [{'id': 'a'}])

    def test_invalid_limit_is_rejected_before_upstream(self):
        for query in ['limit=-1', 'limit=101', 'limit=', 'limit=1&limit=2', 'limit=abc']:
            started, _, opened = self.request(query, {})
            self.assertEqual(started[0], '400 Bad Request')
            opened.assert_not_called()

    def test_browser_uses_bounded_requests_and_recovers_from_failure(self):
        html = (Path(__file__).resolve().parents[1] / 'index.html').read_text()
        self.assertNotIn("fetch('/api/jobs')", html)
        self.assertNotIn("fetchJsonWithTimeout('/api/jobs',", html)
        self.assertIn("fetchJsonWithTimeout('/api/jobs?limit=10',{},45000)", html)
        self.assertIn("fetchJsonWithTimeout('/api/health',{},15000)", html)
        self.assertNotIn('/api/jobs?limit=0', html)
        self.assertIn("retry.onclick=loadRecent", html)
        self.assertIn("finally(()=>{recentLoading=false;})", html)


if __name__ == '__main__':
    unittest.main()
