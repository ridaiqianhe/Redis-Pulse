import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash
from monitor.app import app, Collector, parse_line


class MonitorTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SECRET_KEY='test-only-secret', SESSION_COOKIE_SECURE=False)
        self.client = app.test_client()

    def test_log_parser_keeps_ip_and_strips_query(self):
        event = parse_line('127.0.0.1 - - [03/Nov/2026:20:21:04 +0800] "GET /api/counts?secret=x HTTP/1.1" 200 37 "-" "test-agent" "192.0.2.10"')
        self.assertEqual(event[1:7], ('192.0.2.10', 'GET', '/api/counts', 200, 37, 'test-agent'))
        self.assertIsNone(parse_line('invalid'))
        self.assertIsNone(parse_line('127.0.0.1 - - [03/Nov/2026:20:21:04 +0800] "GET /monitor/api/snapshot HTTP/1.1" 200 37 "-" "test-agent" "192.0.2.10"'))

    def test_protected_routes_and_login(self):
        self.assertEqual(self.client.get('/monitor/').status_code, 302)
        self.assertEqual(self.client.get('/monitor/api/snapshot').status_code, 401)
        with patch.dict(os.environ, {'MONITOR_PASSWORD_HASH': generate_password_hash('test-password')}):
            self.assertEqual(self.client.post('/monitor/login', data={'password': 'incorrect'}).status_code, 401)
            response = self.client.post('/monitor/login', data={'password': 'test-password'})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get('/monitor/').status_code, 200)
        self.assertEqual(self.client.get('/monitor/api/snapshot?minutes=999').status_code, 400)
        self.assertEqual(self.client.post('/monitor/logout').status_code, 302)
        self.assertEqual(self.client.get('/monitor/api/snapshot').status_code, 401)

    def test_aggregation_errors_and_expiry(self):
        c = Collector()
        now = int(time.time())
        c.ingest((now, '192.0.2.1', 'GET', '/api/readnum', 200, 50, 'agent', '192.0.2.1', 'direct', ''))
        c.ingest((now, '192.0.2.1', 'GET', '/api/readnum', 500, 50, 'agent', '192.0.2.1', 'direct', ''))
        c.ingest((now - 4000, '192.0.2.2', 'GET', '/old', 200, 50, 'agent', '192.0.2.1', 'direct', ''))
        result = c.snapshot(15)
        self.assertEqual(result['summary']['total'], 2)
        self.assertEqual(result['summary']['errors'], 1)
        self.assertEqual(result['summary']['unique_ips'], 1)
        self.assertEqual(len(result['series']), 15)
        self.assertEqual(result['summary']['success'], 50)

    def test_incremental_log_and_partial_line(self):
        stamp = time.strftime('%d/%b/%Y:%H:%M:%S %z')
        line = f'127.0.0.1 - - [{stamp}] "GET /api/counts HTTP/1.1" 200 37 "-" "test" "192.0.2.1"'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'access.log'
            path.write_text(line)
            c = Collector()
            with patch('monitor.app.LOG', path):
                c.read_log()
                self.assertEqual(c.snapshot(5)['summary']['total'], 0)
                with path.open('a') as handle:
                    handle.write('\n')
                c.read_log()
                c.read_log()
                self.assertEqual(c.snapshot(5)['summary']['total'], 1)
                path.rename(path.with_suffix('.old'))
                path.write_text(line + '\n')
                c.read_log()
                self.assertEqual(c.snapshot(5)['summary']['total'], 2)


if __name__ == '__main__':
    unittest.main()
