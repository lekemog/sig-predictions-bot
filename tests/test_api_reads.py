import http.client
import io
import json
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import Mock, patch

from trading_bot import API, Halt


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.api = API('test-key-never-log')
        self.api.opener = Mock()
        self.output = patch('builtins.print').start()
        self.sleep = patch('trading_bot.time.sleep').start()
        self.addCleanup(patch.stopall)

    def response(self, body=b'{"ok": true}'):
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(body))
        response.__exit__ = Mock(return_value=False)
        return response

    def test_read_recovers_after_connection_failure(self):
        self.api.opener.open.side_effect = [urllib.error.URLError('private details'), self.response()]
        self.assertEqual(self.api.get('/portfolio'), {'ok': True})
        self.assertEqual(self.api.opener.open.call_count, 2)
        self.sleep.assert_any_call(1)

    def test_read_exhausts_bounded_attempts_without_secret(self):
        self.api.opener.open.side_effect = urllib.error.URLError('test-key-never-log')
        with self.assertRaisesRegex(Halt, 'GET connection failed; read failed after 3 attempt') as error:
            self.api.get('/portfolio')
        self.assertNotIn('test-key-never-log', str(error.exception))
        self.assertEqual(self.api.opener.open.call_count, 3)
        self.sleep.assert_any_call(2)

    def test_retryable_http_codes_recover(self):
        for code in (429, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.api.opener.open.reset_mock()
                self.api.opener.open.side_effect = [urllib.error.HTTPError('url', code, 'private', {}, None), self.response()]
                self.assertEqual(self.api.get('/portfolio'), {'ok': True})
                self.assertEqual(self.api.opener.open.call_count, 2)

    def test_auth_redirect_and_other_http_errors_never_retry(self):
        for code in (301, 400, 401, 403, 404):
            with self.subTest(code=code):
                self.api.opener.open.reset_mock()
                self.api.opener.open.side_effect = urllib.error.HTTPError('url', code, 'private', {}, None)
                with self.assertRaisesRegex(Halt, f'GET HTTP {code}'):
                    self.api.get('/portfolio')
                self.assertEqual(self.api.opener.open.call_count, 1)

    def test_write_failures_never_replay(self):
        failures = [urllib.error.URLError('private'), TimeoutError(), ConnectionResetError(),
                    http.client.RemoteDisconnected(), urllib.error.HTTPError('url', 503, 'private', {}, None)]
        for method in ('POST', 'DELETE'):
            for failure in failures:
                with self.subTest(method=method, failure=type(failure).__name__):
                    self.api.opener.open.reset_mock()
                    self.api.opener.open.side_effect = failure
                    with self.assertRaisesRegex(Halt, 'write outcome may be unknown; reconcile before retry'):
                        self.api.request(method, '/orders', {'idempotencyKey': 'same-key'})
                    self.assertEqual(self.api.opener.open.call_count, 1)

    def test_disconnected_read_recovers(self):
        self.api.opener.open.side_effect = [http.client.RemoteDisconnected(), self.response()]
        self.assertEqual(self.api.get('/portfolio'), {'ok': True})

    def test_malformed_response_fails_closed_without_retry(self):
        self.api.opener.open.side_effect = [self.response(b'private invalid body')]
        with self.assertRaisesRegex(Halt, 'GET invalid JSON response'):
            self.api.get('/portfolio')
        self.assertEqual(self.api.opener.open.call_count, 1)

    def test_successful_write_sent_once(self):
        self.api.opener.open.side_effect = [self.response()]
        self.assertEqual(self.api.request('POST', '/orders', {'idempotencyKey': 'same-key'}), {'ok': True})
        self.assertEqual(self.api.opener.open.call_count, 1)

    def test_connection_diagnostics_use_only_safe_categories(self):
        failures = [(TimeoutError('SECRET'), 'timeout'),
                    (urllib.error.URLError(socket.gaierror(-2, 'SECRET')), 'dns'),
                    (urllib.error.URLError(ssl.SSLError('SECRET')), 'tls'),
                    (ConnectionResetError('SECRET'), 'disconnected'),
                    (http.client.IncompleteRead(b'SECRET'), 'http_transport'),
                    (urllib.error.URLError('SECRET'), 'transport')]
        for error, category in failures:
            with self.subTest(category=category):
                self.output.reset_mock()
                self.api.opener.open.side_effect = error
                with self.assertRaises(Halt):
                    self.api.get('/SECRET?credential=SECRET')
                record = json.loads(self.output.call_args.args[0])
                self.assertEqual(record['category'], category)
                self.assertEqual(record['phase'], 'connect_or_headers')
                self.assertNotIn('SECRET', str(self.output.call_args_list))

    def test_body_timeout_is_distinguished_from_connection(self):
        response = self.response()
        body = Mock()
        body.read.side_effect = TimeoutError('SECRET')
        response.__enter__.return_value = body
        self.api.opener.open.side_effect = [response, response, response]
        with self.assertRaises(Halt):
            self.api.get('/portfolio')
        record = json.loads(self.output.call_args.args[0])
        self.assertEqual(record['phase'], 'response_body')
        self.assertEqual(record['category'], 'timeout')
        self.assertNotIn('SECRET', str(self.output.call_args_list))
