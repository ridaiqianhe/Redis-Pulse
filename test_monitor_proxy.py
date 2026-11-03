import unittest
from unittest.mock import patch, MagicMock
import views


class ProxyTests(unittest.TestCase):
    def test_monitor_routes_forward_without_touching_queue(self):
        upstream = MagicMock()
        upstream.getresponse.return_value.status = 200
        upstream.getresponse.return_value.getheaders.return_value = [('Content-Type', 'text/html'), ('Set-Cookie', 'example=value; Secure; HttpOnly')]
        upstream.getresponse.return_value.read.return_value = b'protected dashboard'
        with patch.object(views.http.client, 'HTTPConnection', return_value=upstream):
            client = views.app.test_client()
            for path in ('/monitor', '/monitor/', '/monitor/login', '/monitor/api/snapshot?minutes=15'):
                response = client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200, path)
                self.assertEqual(response.data, b'protected dashboard')
                self.assertIn('Secure', response.headers['Set-Cookie'])
            self.assertEqual(client.post('/monitor/login', data='x'*4097).status_code, 413)

    def test_unavailable_monitor_is_503(self):
        upstream = MagicMock()
        upstream.request.side_effect = OSError('unavailable')
        with patch.object(views.http.client, 'HTTPConnection', return_value=upstream):
            self.assertEqual(views.app.test_client().get('/monitor/').status_code, 503)
            upstream.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
