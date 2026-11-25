import gc
import os
from pathlib import Path
import unittest
from unittest.mock import patch


class ResourceLifetimeTests(unittest.TestCase):
    def test_completed_catalog_check_cannot_close_descriptors_during_next_setup(self):
        from tests.test_catalog import CatalogTests
        from tests.test_onboarding import OnboardingTests
        from capalith.store import _close_search_sessions

        _close_search_sessions()
        gc.collect()
        was_enabled = gc.isenabled()
        gc.disable()
        lost = []
        original = os.mkdir

        def mkdir(path, *args, **kwargs):
            if Path(path).name == "capalith":
                before = set(os.listdir("/proc/self/fd"))
                gc.collect()
                lost.extend(before - set(os.listdir("/proc/self/fd")))
            return original(path, *args, **kwargs)

        try:
            first = unittest.TestResult()
            CatalogTests("test_catalog_publication_failure_preserves_prior_results").run(first)
            self.assertTrue(first.wasSuccessful(), first.errors + first.failures)
            second = unittest.TestResult()
            with patch("os.mkdir", side_effect=mkdir):
                OnboardingTests("test_setup_directory_swaps_do_not_write_selected_sources").run(second)
            self.assertTrue(second.wasSuccessful(), second.errors + second.failures)
            self.assertEqual([], lost, "a completed check retained a connection until garbage collection")
        finally:
            if was_enabled:
                gc.enable()


if __name__ == "__main__":
    unittest.main()
