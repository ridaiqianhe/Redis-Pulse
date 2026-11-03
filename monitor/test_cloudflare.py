import unittest
from monitor.cloudflare import resolve_client
from monitor.app import parse_line, Collector
import time


class CloudflareTests(unittest.TestCase):
    def test_trusted_edge_behind_local_proxy(self):
        self.assertEqual(resolve_client('127.0.0.1', '172.64.1.2', '203.0.113.8'),
                         ('203.0.113.8', '172.64.1.2', 'cloudflare'))

    def test_external_origin_request_cannot_spoof_forwarded_headers(self):
        self.assertEqual(resolve_client('198.51.100.7', '172.64.1.2', '203.0.113.8'),
                         ('198.51.100.7', '198.51.100.7', 'direct'))

    def test_non_cloudflare_proxy_peer_ignores_client_header(self):
        self.assertEqual(resolve_client('127.0.0.1', '198.51.100.7', '203.0.113.8'),
                         ('198.51.100.7', '198.51.100.7', 'direct'))

    def test_ipv6_and_pseudo_ipv4(self):
        self.assertEqual(resolve_client('127.0.0.1', '2606:4700::1234',
            '240.1.2.3', '2001:db8::8'), ('2001:db8::8', '2606:4700::1234', 'cloudflare'))
        self.assertEqual(resolve_client('127.0.0.1', '172.64.1.2',
            '2001:db8::9'), ('2001:db8::9', '172.64.1.2', 'cloudflare'))

    def test_invalid_and_missing_client_header(self):
        for value in ('-', None, 'not-an-ip', '203.0.113.1, 203.0.113.2'):
            self.assertEqual(resolve_client('127.0.0.1', '172.64.1.2', value),
                             ('172.64.1.2', '172.64.1.2', 'cloudflare_missing'))

    def test_extended_log_and_legacy_counts(self):
        stamp = time.strftime('%d/%b/%Y:%H:%M:%S %z')
        legacy = f'127.0.0.1 - - [{stamp}] "GET /api/counts HTTP/1.1" 200 10 "-" "test" "172.64.1.2"'
        extended = legacy + ' "203.0.113.8" "-" "abcd-HKG"'
        record = parse_line(extended)
        self.assertEqual(record[1], '203.0.113.8')
        self.assertEqual(record[7:], ('172.64.1.2', 'cloudflare', 'abcd-HKG'))
        collector = Collector()
        collector.ingest(parse_line(legacy))
        collector.ingest(record)
        snapshot = collector.snapshot(5)
        self.assertEqual(snapshot['summary']['total'], 2)
        self.assertEqual(snapshot['summary']['unique_ips'], 1)
        self.assertEqual(snapshot['summary']['unresolved_ips'], 1)
        self.assertEqual(snapshot['ips'], [{'ip': '203.0.113.8', 'count': 1}])


if __name__ == '__main__':
    unittest.main()
