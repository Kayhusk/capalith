"""The embedding model is released after an idle time (`semantic.idle_release_seconds`), and a search never loses it mid-call.

The model is a stand-in that counts how often it is built; nothing here loads a real model.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from capalith import semantic
from capalith.settings import SettingsError, defaults, load_settings


class FakeModel:
    """Deterministic vectors of the configured size; `slow` makes a document call take a while."""

    def __init__(self, counter: list[int], slow: float = 0.0):
        counter.append(1)
        self.slow = slow

    @staticmethod
    def vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [(digest[i % len(digest)] + 1) / 256 for i in range(semantic.dimensions())]

    def query_embed(self, query):
        yield self.vector(query)

    def passage_embed(self, documents, batch_size=1):
        for document in documents:
            time.sleep(self.slow)
            yield self.vector(document)


@contextlib.contextmanager
def configured(idle: float | None, slow: float = 0.0):
    """Settings with the given idle time and a counted fake model in place of the real build."""
    built: list[int] = []
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "settings.toml"
        config.write_text("version = 1\n" + ("" if idle is None else f"[semantic]\nidle_release_seconds = {idle}\n"))
        config.chmod(0o600)
        semantic._reset_model_cache()
        with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}), \
                patch.object(semantic, "_build_model", lambda *a, **k: FakeModel(built, slow)):
            try:
                yield built
            finally:
                semantic._reset_model_cache()


def set_idle(seconds: float) -> None:
    """The user edits the settings file while the server runs; the next search reads the new value."""
    config = Path(os.environ["CAPALITH_CONFIG"])
    config.write_text(f"version = 1\n[semantic]\nidle_release_seconds = {seconds}\n")
    config.chmod(0o600)


class IdleReleaseTests(unittest.TestCase):
    def test_the_default_is_five_minutes_and_zero_turns_it_off(self):
        self.assertEqual(300, defaults()["semantic"]["idle_release_seconds"])
        with configured(0) as built:
            semantic.embed_query("alpha")
            time.sleep(0.4)
            self.assertTrue(semantic._model_is_loaded())
            self.assertEqual(1, len(built))

    def test_the_model_is_dropped_after_the_idle_time_and_built_again_with_the_same_vectors(self):
        with configured(0.2) as built:
            before = semantic.embed_query("alpha")
            documents = semantic.embed_documents(["one", "two"])
            self.assertEqual(1, len(built))
            self.assertTrue(semantic._model_is_loaded())
            time.sleep(0.7)
            self.assertFalse(semantic._model_is_loaded(), "released after the idle time")
            self.assertEqual(before, semantic.embed_query("alpha"))
            self.assertEqual(documents, semantic.embed_documents(["one", "two"]))
            self.assertEqual(2, len(built), "one build before, one after the release")

    def test_use_keeps_the_model_loaded_past_the_idle_time(self):
        with configured(0.4) as built:
            for _ in range(5):
                semantic.embed_query("alpha")
                time.sleep(0.15)                     # each use is inside the idle time: the clock starts again
            self.assertTrue(semantic._model_is_loaded())
            self.assertEqual(1, len(built))

    def test_a_search_in_progress_never_loses_its_model(self):
        with configured(0.1, slow=0.35) as built:
            results: list[object] = []
            worker = threading.Thread(target=lambda: results.append(semantic.embed_documents(["a", "b", "c"])))
            worker.start()
            time.sleep(0.25)                         # the idle time has passed while the call is still running
            self.assertTrue(semantic._model_is_loaded(), "a release never takes the model from a call in progress")
            worker.join(timeout=10)
            self.assertEqual(1, len(results))
            self.assertEqual(3, len(results[0]))
            self.assertEqual(1, len(built))
            time.sleep(0.5)
            self.assertFalse(semantic._model_is_loaded(), "released once the call is over and the idle time has passed")

    def test_a_search_that_starts_during_the_release_waits_or_rebuilds_and_never_errors(self):
        with configured(0.05) as built:
            errors: list[BaseException] = []

            def search():
                try:
                    for _ in range(12):
                        semantic.embed_query("alpha")
                        time.sleep(0.12)      # longer than the idle time: the model is released between searches
                except BaseException as error:       # noqa: BLE001 - the point is that nothing is raised
                    errors.append(error)

            threads = [threading.Thread(target=search) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual([], errors)
            self.assertGreaterEqual(len(built), 2, "the model was released and rebuilt while searches ran")

    def test_a_setting_changed_to_zero_keeps_the_model_though_an_earlier_search_started_a_timer(self):
        with configured(0.15) as built:
            semantic.embed_query("alpha")           # arms a 0.15 s timer
            set_idle(0)
            semantic.embed_query("alpha")           # reads 0: the earlier timer must not release the model
            time.sleep(0.5)
            self.assertTrue(semantic._model_is_loaded())
            self.assertEqual(1, len(built))

    def test_an_old_timer_already_entering_its_callback_does_not_follow_its_old_time_after_a_longer_setting(self):
        with configured(0.1) as built:
            semantic.embed_query("alpha")
            stale = semantic._timer_generation      # what the first timer carries
            set_idle(10)
            semantic.embed_query("alpha")
            time.sleep(0.25)                        # past the first idle time, far from the new one
            semantic._release_if_idle(stale)        # a callback that was already running when it was replaced
            self.assertTrue(semantic._model_is_loaded())
            self.assertEqual(1, len(built))

    def test_a_shorter_setting_takes_effect_at_the_next_search(self):
        with configured(10) as built:
            semantic.embed_query("alpha")
            set_idle(0.1)
            semantic.embed_query("alpha")
            time.sleep(0.5)
            self.assertFalse(semantic._model_is_loaded())
            semantic.embed_query("alpha")
            self.assertEqual(2, len(built))

    def test_a_failed_replacement_build_still_lets_the_held_model_expire(self):
        # The settings change to another thread count, so the next search builds a replacement; the old model's timer fires
        # while that build runs (it finds the model busy and leaves the expiry to the end of the call), and the build fails.
        with configured(0.15) as built:
            semantic.embed_query("alpha")
            config = Path(os.environ["CAPALITH_CONFIG"])
            config.write_text("version = 1\n[semantic]\nthreads = 2\nidle_release_seconds = 0.15\n")
            config.chmod(0o600)

            def failing(*args, **kwargs):
                time.sleep(0.4)
                raise semantic.SemanticUnavailable("local semantic model is unavailable")

            with patch.object(semantic, "_build_model", failing), self.assertRaises(semantic.SemanticUnavailable):
                semantic.embed_query("alpha")
            self.assertTrue(semantic._model_is_loaded(), "the older model is still held right after the failure")
            time.sleep(0.6)
            self.assertFalse(semantic._model_is_loaded(), "and it expires")
            self.assertEqual(1, len(built))

    def test_a_failure_while_giving_memory_back_never_reaches_a_thread_or_a_search(self):
        raised: list[object] = []
        with configured(0.1) as built, patch.object(semantic.gc, "collect", side_effect=RuntimeError("injected")), \
                patch.object(threading, "excepthook", lambda args: raised.append(args)):
            semantic.embed_query("alpha")
            time.sleep(0.5)
            self.assertFalse(semantic._model_is_loaded(), "released although the cleanup failed")
            self.assertEqual([], raised, "no error escaped the timer thread")
            self.assertEqual(semantic.embed_query("alpha"), semantic.embed_query("alpha"))
            self.assertEqual(2, len(built))

    def test_the_setting_is_validated(self):
        for bad in ('"soon"', "-1", "true", "86401"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "settings.toml"
                config.write_text(f"version = 1\n[semantic]\nidle_release_seconds = {bad}\n")
                config.chmod(0o600)
                with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}), self.assertRaises(SettingsError):
                    load_settings()
        self.assertNotIn("idle_release_seconds", defaults()["semantic"]["model"], "not part of the index key")


if __name__ == "__main__":
    unittest.main()
