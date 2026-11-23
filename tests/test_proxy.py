import os
import unittest
from unittest import mock

from monitor import http


class TestProxySwitch(unittest.TestCase):
    def test_proxy_comes_from_env(self):
        env = {"HTTPS_PROXY": "http://127.0.0.1:7890",
               "HTTP_PROXY": "http://127.0.0.1:7890"}
        with mock.patch.dict(os.environ, env, clear=True):
            handler = http._proxy_handler()
            self.assertEqual(handler.proxies.get("https"), "http://127.0.0.1:7890")
            self.assertEqual(handler.proxies.get("http"), "http://127.0.0.1:7890")

    def test_no_env_means_no_proxy(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            handler = http._proxy_handler()
            self.assertFalse(handler.proxies)

    def test_all_proxy_used_as_fallback(self):
        with mock.patch.dict(os.environ, {"ALL_PROXY": "http://127.0.0.1:7890"}, clear=True):
            handler = http._proxy_handler()
            self.assertEqual(handler.proxies.get("https"), "http://127.0.0.1:7890")

    def test_validate_url_skips_dns_when_proxy_set(self):
        # 不存在的域名：不走代理时会 dns_error，走代理时交给代理解析，不应报错
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=True):
            http.validate_url("https://definitely-not-exist.invalid/path")

    def test_validate_url_still_blocks_private_ip_with_proxy(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=True):
            with self.assertRaises(http.FetchFailure):
                http.validate_url("http://127.0.0.1/admin")

    def test_validate_url_dns_check_without_proxy(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(http.FetchFailure):
                http.validate_url("https://definitely-not-exist.invalid/path")


if __name__ == "__main__":
    unittest.main()
