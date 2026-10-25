"""Public asset updates invalidate both entry documents and lazy URLs."""
import json
from pathlib import Path
import re
import tempfile
import unittest

from web_assets import StaticAssets


class StaticAssetTests(unittest.TestCase):
    def test_source_update_changes_entry_etag_and_lazy_asset_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'index.html').write_text('<script id="asset-manifest" type="application/json">{}</script><script src="/app.js"></script>', encoding='utf-8')
            (root / 'app.js').write_bytes(b'initial')
            (root / 'editor.js').write_bytes(b'version one')
            store = StaticAssets({'/': (root / 'index.html', 'text/html'),
                '/app.js': (root / 'app.js', 'text/javascript'),
                '/editor.js': (root / 'editor.js', 'text/javascript')})
            first = store.get('/')
            manifest = lambda asset: json.loads(re.search(r'application/json">(.*?)</script>', asset.data.decode()).group(1))
            old_version = manifest(first)['/editor.js']
            self.assertIn('immutable', store.get('/editor.js', old_version).cache_control)
            (root / 'editor.js').write_bytes(b'version two, deployed')
            second = store.get('/')
            self.assertNotEqual(first.etag, second.etag)
            self.assertNotEqual(old_version, manifest(second)['/editor.js'])
            self.assertFalse(second.matches(first.etag))
            self.assertNotIn('immutable', store.get('/editor.js', old_version).cache_control)
            self.assertEqual(store.get('/editor.js').data, b'version two, deployed')

    def test_conditional_requests_accept_weak_and_strong_validators(self):
        from web_assets import PublicAsset
        asset = PublicAsset(b'file', 'text/javascript', 'W/"current"', 'public')
        for supplied in ('W/"current"', '"current"', '"old", W/"current"', '*'):
            self.assertTrue(asset.matches(supplied), supplied)
        for supplied in ('', 'W/"other"', '"other"'):
            self.assertFalse(asset.matches(supplied), supplied)
