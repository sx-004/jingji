import unittest
from unittest import mock

from monitor import telegram


class TestTelegramRetry(unittest.TestCase):
    def test_success_first_try(self):
        with mock.patch.object(telegram, "_send_once", return_value=True) as once:
            self.assertTrue(telegram.send_message("t", "c", "x"))
            self.assertEqual(once.call_count, 1)

    def test_transient_failure_is_retried(self):
        calls = []

        def fake(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise telegram.PushError("Telegram 请求失败: URLError")
            return True

        with mock.patch.object(telegram, "_send_once", side_effect=fake):
            with mock.patch.object(telegram.time, "sleep"):
                self.assertTrue(telegram.send_message("t", "c", "x"))
        self.assertEqual(len(calls), 2)

    def test_auth_error_is_not_retried(self):
        with mock.patch.object(telegram, "_send_once",
                               side_effect=telegram.PushError("Telegram 401：token 无效")) as once:
            with mock.patch.object(telegram.time, "sleep"):
                with self.assertRaises(telegram.PushError):
                    telegram.send_message("t", "c", "x")
        self.assertEqual(once.call_count, 1)

    def test_retry_limit_then_raise(self):
        with mock.patch.object(telegram, "_send_once",
                               side_effect=telegram.PushError("Telegram HTTP 500")) as once:
            with mock.patch.object(telegram.time, "sleep"):
                with self.assertRaises(telegram.PushError):
                    telegram.send_message("t", "c", "x")
        self.assertEqual(once.call_count, 3)


if __name__ == "__main__":
    unittest.main()
