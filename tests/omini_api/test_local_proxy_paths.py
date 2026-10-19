from __future__ import annotations

import unittest

from omini_api import xhhai_local_proxy as proxy


class LocalProxyPathTests(unittest.TestCase):
    def test_codex_provider_test_path_is_supported(self) -> None:
        self.assertTrue(proxy.is_supported_openai_path("/responses"))
        self.assertEqual(proxy.translate_local_path_to_upstream("/responses"), "/v1/responses")

    def test_openai_sdk_prefixed_path_is_preserved(self) -> None:
        self.assertTrue(proxy.is_supported_openai_path("/api/v1/responses"))
        self.assertEqual(proxy.translate_local_path_to_upstream("/api/v1/responses"), "/v1/responses")


if __name__ == "__main__":
    unittest.main()
