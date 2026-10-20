"""Quota window classification, safe management probes and UI partial failures."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import ai_assistants as core
from codex_quota import USAGE_URL, parse_codex_quota, quota_cell


def payload(five=23, week=67):
    return {"plan_type": "plus", "rate_limit": {
        "primary_window": {"used_percent": five, "limit_window_seconds": 18000, "reset_after_seconds": 3600},
        "secondary_window": {"used_percent": week, "limit_window_seconds": 604800, "reset_at": 1894000000}}}


class QuotaParsingTests(unittest.TestCase):
    def test_remaining_percent_reset_and_reversed_windows(self):
        value = payload(0, 100)
        rate = value['rate_limit']
        rate['primary_window'], rate['secondary_window'] = rate['secondary_window'], rate['primary_window']
        result = parse_codex_quota(value, now=1800000000)
        self.assertEqual(result['five_hour']['remaining_percent'], 100)
        self.assertEqual(result['weekly']['remaining_percent'], 0)
        self.assertEqual(result['five_hour']['reset_at'], 1800003600)

    def test_week_only_free_account_and_month_are_not_mislabeled(self):
        value = {'plan_type': 'free', 'rate_limit': {'primary_window': payload()['rate_limit']['secondary_window']}}
        result = parse_codex_quota(value)
        self.assertIsNone(result['five_hour']); self.assertIsNotNone(result['weekly'])
        value['rate_limit']['primary_window']['limit_window_seconds'] = 30*86400
        result = parse_codex_quota(value)
        self.assertIsNone(result['five_hour']); self.assertIsNone(result['weekly'])
        self.assertEqual(len(result['other_windows']), 1)

    def test_unknown_invalid_percent_and_missing_window_stay_unknown(self):
        for used in (None, True, 'NaN', -1, 101):
            result = parse_codex_quota(payload(used))
            self.assertIsNone(result['five_hour']['remaining_percent'])
        result = parse_codex_quota({'plan_type': 'free', 'rate_limit': None})
        self.assertEqual(quota_cell(result, 'five_hour')[0], '未返回')
        for value in ({}, '<html>', {'rate_limit': []}):
            with self.assertRaises(ValueError): parse_codex_quota(value)

    def test_camel_case_numeric_strings_and_failure_cache_label(self):
        result = parse_codex_quota({'planType':'plus', 'rateLimit': {'primaryWindow': {
            'usedPercent':'12.5', 'limitWindowSeconds': '18000', 'resetAt': '2027-01-01T00:00:00Z'}}})
        self.assertEqual(result['five_hour']['remaining_percent'], 87.5)
        result['error'] = 'HTTP 403'
        text, tip = quota_cell(result, 'five_hour')
        self.assertIn('上次', text); self.assertIn('87.5%', text); self.assertIn('403', tip)


class QuotaRequestTests(unittest.TestCase):
    def test_probe_uses_credential_index_and_engine_token_placeholder(self):
        client = core.PoolClient(core.default_settings()['pool'])
        row = {'name':'member.json', 'auth_index':'idx-1', 'id_token':{'chatgpt_account_id':'account-id'}}
        with patch.object(client, 'request', return_value={'status_code':200, 'body':json.dumps(payload())}) as request:
            result = client.quota(row)
        self.assertEqual(result['weekly']['remaining_percent'], 33)
        body = request.call_args.kwargs['json']
        self.assertEqual(request.call_args.args, ('POST','/requests/api-call'))
        self.assertEqual(body['url'], USAGE_URL)
        self.assertEqual(body['auth_index'], 'idx-1')
        self.assertEqual(body['header']['Authorization'], 'Bearer $TOKEN$')
        self.assertEqual(body['header']['Chatgpt-Account-Id'], 'account-id')

    def test_http_failures_and_html_do_not_leak_raw_response(self):
        client = core.PoolClient(core.default_settings()['pool'])
        for status in (401, 403, 429, 503):
            with patch.object(client, 'request', return_value={'status_code':status, 'body':'PRIVATE_RESPONSE'}):
                with self.assertRaises(ValueError) as caught: client.quota({'auth_index':'idx'})
                self.assertIn(str(status), str(caught.exception)); self.assertNotIn('PRIVATE',str(caught.exception))
        with patch.object(client, 'request', return_value={'status_code':200, 'body':'<html>PRIVATE_RESPONSE</html>'}):
            with self.assertRaises(ValueError): client.quota({'auth_index':'idx'})


class QuotaPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qt = QApplication.instance() or QApplication([])

    def test_query_all_preserves_selection_and_one_failed_snapshot(self):
        from PySide6.QtTest import QTest
        from ai_assistants_page import AiAssistantsPage
        with tempfile.TemporaryDirectory() as directory:
            page = AiAssistantsPage(Path(directory))
            rows = [{'name':f'{i}.json','email':f'member{i}@example.com','status':'active','auth_index':str(i)} for i in range(2)]
            page.set_accounts(rows); page.table.selectRow(1)
            page.quotas['1.json'] = parse_codex_quota(payload(20, 40))
            client = core.PoolClient(core.default_settings()['pool'])
            def quota(row):
                if row['name']=='1.json': raise ValueError('HTTP 403')
                return parse_codex_quota(payload(10, 30))
            with patch.object(page, 'client', return_value=client), patch.object(client,'quota',side_effect=quota):
                page.query_quotas(True)
                deadline = time.monotonic()+5
                while page.worker is not None and time.monotonic()<deadline: QTest.qWait(10)
            self.assertIsNone(page.worker)
            self.assertEqual(page.selected_account()['name'],'1.json')
            self.assertIn('90%',page.table.item(0,2).text())
            self.assertIn('上次',page.table.item(1,2).text())
            self.assertIn('80%',page.table.item(1,2).text())
            self.assertIn('403',page.table.item(1,2).toolTip())
            self.assertIn('成功 1',page.status.text())
            self.assertEqual(page.strategy.count(),4)
            page.strategy.setCurrentIndex(page.strategy.findData('balanced'))
            self.assertFalse(page.affinity.isEnabled())
            self.assertEqual(page.pool_value()['strategy'],'balanced')
            page.stop(); page.deleteLater(); self.qt.processEvents()
