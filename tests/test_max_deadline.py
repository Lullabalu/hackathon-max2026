import io
import signal
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ip_bot.budget import time_limit
from ip_bot.max_api import APIError, MaxAPI


class MaxDeadlineTests(unittest.TestCase):
    def test_network_operations_keep_transport_timeouts(self):
        api = MaxAPI('example-not-a-real-token')
        timeouts = []

        class Opener:
            def open(self, request, timeout):
                timeouts.append(timeout)
                return io.BytesIO(b'{"success": true}')

        api.opener = Opener()
        with patch('ip_bot.max_api.time.sleep'):
            self.assertTrue(api.send_message(1, 'Ответ')['success'])
            api.updates(None)
        self.assertGreater(timeouts[0], 0)
        self.assertLessEqual(timeouts[0], 45)
        self.assertGreater(timeouts[1], 44)
        self.assertLessEqual(timeouts[1], 45)

    def test_failed_delivery_does_not_leak_budget_into_polling(self):
        api = MaxAPI('example-not-a-real-token')
        with patch.object(api.opener, 'open', side_effect=TimeoutError):
            with self.assertRaises(APIError) as caught:
                api.send_message(1, 'Готовый ответ')
        self.assertTrue(caught.exception.retryable)
        with patch.object(api.opener, 'open', return_value=io.BytesIO(b'{}')) as open_request:
            api.updates(None)
        self.assertGreater(open_request.call_args.kwargs['timeout'], 44)
        self.assertLessEqual(open_request.call_args.kwargs['timeout'], 45)

    def test_too_long_reply_is_still_a_validation_error(self):
        api = MaxAPI('example-not-a-real-token')
        with patch.object(api.opener, 'open') as open_request:
            with self.assertRaises(ValueError):
                api.send_message(1, 'x' * 4001)
            open_request.assert_not_called()

    @unittest.skipUnless(hasattr(signal, 'setitimer') and threading.current_thread() is threading.main_thread(),
                         'Требуется POSIX timer в основном потоке')
    def test_budget_interrupts_rate_limits_before_network(self):
        for rate in ('dialog', 'global'):
            with self.subTest(rate=rate):
                api = MaxAPI('example-not-a-real-token')
                if rate == 'dialog':
                    api.last_message[1] = time.monotonic()
                else:
                    api.last_request = time.monotonic()
                api.opener = Mock()
                started = time.monotonic()
                with self.assertRaises(APIError) as caught:
                    with time_limit(0.02):
                        api.send_message(1, 'Ответ')
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn(api.token, str(caught.exception))
                self.assertLess(time.monotonic() - started, 0.4)
                api.opener.open.assert_not_called()

    @unittest.skipUnless(hasattr(signal, 'setitimer') and threading.current_thread() is threading.main_thread(),
                         'Требуется POSIX timer в основном потоке')
    def test_budget_interrupts_connect_and_body_read(self):
        sleep = time.sleep
        for stage in ('connect', 'read'):
            with self.subTest(stage=stage):
                api = MaxAPI('example-not-a-real-token')

                class Response(io.BytesIO):
                    def read(self, *args):
                        if stage == 'read':
                            sleep(0.5)
                        return super().read(*args)

                class Opener:
                    def open(self, request, timeout):
                        if stage == 'connect':
                            sleep(0.5)
                        return Response(b'{"success": true}')

                api.opener = Opener()
                started = time.monotonic()
                with self.assertRaises(APIError) as caught:
                    with time_limit(0.04):
                        api.send_message(1, 'Ответ')
                self.assertTrue(caught.exception.retryable)
                self.assertLess(time.monotonic() - started, 0.4)


if __name__ == '__main__':
    unittest.main()
