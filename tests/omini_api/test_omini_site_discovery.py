from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omini_api import omini_account_service as accounts
from omini_api import omini_site_discovery as discovery
from omini_api import omini_sites as sites


class SiteDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_candidate_count_format_uniqueness_and_ratio(self) -> None:
        candidates = discovery.generate_candidates(1000, seed=12345)
        self.assertEqual(len(candidates), 1000)
        self.assertEqual(len(set(candidates)), 1000)
        self.assertTrue(all(discovery.HOST_RE.fullmatch(item) for item in candidates))
        word_pattern = re.compile(
            r"^api\.(?:" + "|".join(discovery.DISCOVERY_WORDS) + r")(?:\d{2,4})?\."
        )
        self.assertEqual(sum(bool(word_pattern.match(item)) for item in candidates[:600]), 600)
        self.assertTrue(all(item.rsplit(".", 1)[1] in discovery.DISCOVERY_TLDS for item in candidates))

    def test_positive_balance_promotes_and_zero_balance_quarantines(self) -> None:
        accounts_root = self.root / "accounts"
        store_path = self.root / "omini_sites.json"
        log_path = self.root / "omini_site_discovery.jsonl"
        sites.save_site_store(store_path, {"version": 1, "sites": [sites.default_xhhai_profile()]})

        def fake_probe(host: str, **_kwargs: object) -> discovery.ProbeResult:
            return discovery.ProbeResult(
                host=host, url=f"https://{host}/api/v1/settings/public", compatible=True,
                stage="compatible", settings={"registration_enabled": True, "email_verify_enabled": False},
            )

        def fake_provision(profile: dict[str, object], email: str, **kwargs: object) -> accounts.ProvisionResult:
            output_dir = Path(kwargs["output_dir"])
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{profile['id']}.json"
            balance = 4.0 if "good" in str(profile["api_base"]) else 0.0
            path.write_text(json.dumps({
                "schema_version": 2, "site": {"id": profile["id"], "name": profile["name"],
                "home_url": profile["home_url"], "api_base": profile["api_base"]},
                "account_kind": "codex", "route": "默认", "email": email,
                "password": accounts.FIXED_ACCOUNT_PASSWORD, "registration": {"access_token": "test"},
                "api_key": "", "key": {}, "group": {}, "key_history": [],
                "balance_initial": balance, "balance_cache": {"value": balance},
                "discovery": kwargs.get("discovery", {}),
            }, ensure_ascii=False), encoding="utf-8")
            return accounts.ProvisionResult(
                status="partial", path=path, email=email, balance=balance,
                balance_status="complete", key_status="failed", group_status="failed", errors=["key fixture"],
            )

        with mock.patch.object(discovery.account_service, "provision_account", side_effect=fake_provision):
            summary = discovery.discover_sites(
                count=2, probe_concurrency=2, provision_concurrency=2, email_domain="example.com",
                email_mode="direct", key_name="codex", receiver_config={}, accounts_root=accounts_root,
                site_store_path=store_path, log_path=log_path, use_proxy=False,
                candidates=["api.good.top", "api.zero.xyz"], probe_func=fake_probe,
            )

        self.assertEqual(summary["promoted"], 1)
        self.assertEqual(summary["zero_balance"], 1)
        saved = sites.load_site_store(store_path)
        self.assertTrue(any(item["api_base"] == "https://api.good.top/api/v1" for item in saved["sites"]))
        self.assertEqual(len(list((accounts_root / "codex").glob("*.json"))), 1)
        self.assertEqual(len(list((accounts_root / "discovery").glob("*.json"))), 1)
        log_text = log_path.read_text(encoding="utf-8")
        self.assertNotIn("1234578Cao", log_text)
        self.assertNotIn("access_token", log_text)


if __name__ == "__main__":
    unittest.main()
