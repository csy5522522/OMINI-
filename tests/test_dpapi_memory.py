"""Keep repeated credential/history operations from retaining ctypes classes."""
import ctypes
import gc
import sys
import tempfile
import unittest
from pathlib import Path

from assistant_data import OperationHistory
from campus_network import protect_password, unprotect_password


@unittest.skipUnless(sys.platform == "win32", "Windows DPAPI regression")
class DpapiMemoryTest(unittest.TestCase):
    def warm_cache(self):
        self.assertEqual(unprotect_password(protect_password("warmup")), "warmup")
        gc.collect()
        return set(ctypes._pointer_type_cache)

    def test_repeated_password_roundtrips_keep_pointer_types_bounded(self):
        before = self.warm_cache()
        for index in range(300):
            value = f"fixture-password-{index}-中文"
            blob = protect_password(value)
            self.assertNotIn(value, blob)
            self.assertEqual(unprotect_password(blob), value)
        gc.collect()
        self.assertEqual(set(ctypes._pointer_type_cache), before)

    def test_repeated_history_reads_do_not_retain_per_record_types(self):
        with tempfile.TemporaryDirectory(prefix="omini-dpapi-memory-") as directory:
            history = OperationHistory(Path(directory))
            for index in range(40):
                history.append(f"fixture action {index}", "fixture")
            before = self.warm_cache()
            for _ in range(100):
                records = history.records(limit=None)
                self.assertEqual(len(records), 40)
                self.assertEqual(records[-1]["message"], "fixture action 39")
            gc.collect()
            self.assertEqual(set(ctypes._pointer_type_cache), before)


if __name__ == "__main__":
    unittest.main()
