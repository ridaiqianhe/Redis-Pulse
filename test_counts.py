"""Integration tests using isolated, randomly named Redis keys only."""
import importlib.util
import os
import time
import unittest
import uuid
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'views_under_test', os.environ.get('VIEWS_TEST_PATH', 'views.py'))
views = importlib.util.module_from_spec(spec)
spec.loader.exec_module(views)


@unittest.skipUnless(os.environ.get('RUN_REDIS_TESTS') == '1',
                     'Set RUN_REDIS_TESTS=1 to run against Redis')
class CountTests(unittest.TestCase):
    def setUp(self):
        self.prefix = 'test:queue-counts:' + uuid.uuid4().hex + ':'
        self.names = [self.prefix + name for name in ('slides', 'yiduns')]
        self.original_count = views.count_active_data
        self.now = int(time.time())
        for name in self.names:
            values = {'expired': self.now - 901, 'boundary': self.now - 900,
                      'active': self.now - 899, 'fresh': self.now}
            views.r.zadd(name, values)
            views.r.zadd(name + ':seen', values)
            # A recent cleanup marker must not prevent accurate counts.
            views.r.set(name + ':cleanup', '1', ex=30)

    def tearDown(self):
        views.r.delete(*[key for name in self.names
                         for key in (name, name + ':seen', name + ':cleanup')])

    def count_test_keys(self, *names):
        result = self.original_count(*(self.prefix + name for name in names))
        return {name: result[self.prefix + name] for name in names}

    def test_atomic_cleanup_preserves_valid_data_and_seen(self):
        with patch.object(views.time, 'time', return_value=self.now):
            result = self.original_count(*self.names)
        self.assertEqual(result, dict.fromkeys(self.names, 2))
        for name in self.names:
            self.assertEqual(views.r.zrange(name, 0, -1), ['active', 'fresh'])
            self.assertEqual(views.r.zrange(name + ':seen', 0, -1),
                             ['active', 'fresh'])

    def test_both_routes_keep_response_contract_and_do_not_consume(self):
        with patch.object(views, 'count_active_data', self.count_test_keys), \
                patch.object(views.time, 'time', return_value=self.now):
            client = views.app.test_client()
            for _ in range(2):
                for name in ('slides', 'yiduns'):
                    self.assertEqual(client.get('/api/readnum',
                        query_string={'type': name}).get_json(),
                        {'code': 1, 'count': 2})
                self.assertEqual(client.get('/api/counts').get_json(),
                                 {'code': 1, 'slides': 2, 'yiduns': 2})

    def test_invalid_types_do_not_query_redis(self):
        with patch.object(views, 'count_active_data') as count:
            client = views.app.test_client()
            for query in ({}, {'type': 'other'}):
                self.assertEqual(client.get('/api/readnum',
                    query_string=query).get_json()['code'], 0)
            count.assert_not_called()

    def test_empty_and_all_expired_counts(self):
        for name in self.names:
            views.r.delete(name, name + ':seen')
        views.r.zadd(self.names[0], {'old': self.now - 1000})
        with patch.object(views.time, 'time', return_value=self.now):
            self.assertEqual(self.original_count(*self.names),
                             dict.fromkeys(self.names, 0))

    def test_write_deduplication_and_read_still_work(self):
        name = self.prefix + 'slides'
        views.r.delete(name, name + ':seen', name + ':cleanup')
        with views.app.test_request_context(method='POST',
                data={'token': 'test-token', 'validate': 'test-value'}):
            self.assertEqual(views.write_unique_data(name).get_json()['inserted'], 1)
            self.assertEqual(views.write_unique_data(name).get_json()['inserted'], 0)
        with views.app.test_request_context():
            self.assertEqual(views.read_unique_data(name, force=True).get_json(),
                {'token': 'test-token', 'validate': 'test-value', 'count': 0})


if __name__ == '__main__':
    unittest.main()
