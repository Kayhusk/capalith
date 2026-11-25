from __future__ import annotations

import copy
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from capalith import answer, semantic
from capalith.intake import scan_local_source
from capalith.settings import SettingsError, _validate_answer, defaults, load_settings
from capalith.store import Store

VECTOR = struct.pack("<384f", 1.0, *([0.0] * 383))
ORTHOGONAL = struct.pack("<384f", 0.0, 1.0, *([0.0] * 382))
CONFIG = defaults()["answer"]


def candidate(name, *, similarity=0.9, description_hits=1):
    score = {"name": 0, "description": description_hits, "distinct_terms": 1}
    if similarity is not None:
        score["semantic_similarity"] = similarity
    return {
        "name": name, "description": "Does a task.", "matched_terms": ["task"], "score": score,
        "source_id": "s1", "bundle_path": name, "artifact_id": "art_" + name,
        "content_digest": "sha256:" + "0" * 64, "relationships": [],
    }


def result(candidates, best, *, query="a task"):
    semantic_report = {"status": "used"}
    if best is not None:
        semantic_report["best_similarity"] = best
    return {"candidates": candidates, "query_terms": query.split(), "semantic": semantic_report}


def refusing(threshold):
    return {**CONFIG, "refuse_below": threshold}


def write_settings(root: Path, text: str) -> None:
    config = root / "config/capalith/settings.toml"
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.write_text(text)
    config.chmod(0o600)


class JudgeRefusalTests(unittest.TestCase):
    def test_best_meaning_score_below_the_setting_is_no_match(self):
        status, signal = answer.judge(result([candidate("pdf-filler")], 0.70), refusing(0.768))
        self.assertEqual("no_match", status)
        self.assertIn("0.70", signal)
        self.assertIn("0.768", signal)

    def test_a_score_at_or_above_the_setting_is_not_refused(self):
        self.assertEqual("possible", answer.judge(result([candidate("pdf-filler")], 0.80), refusing(0.768))[0])
        self.assertEqual("possible", answer.judge(result([candidate("pdf-filler")], 0.768), refusing(0.768))[0])

    def test_off_by_default_so_today_s_rule_is_unchanged(self):
        self.assertEqual(0.0, CONFIG["refuse_below"])
        self.assertEqual("possible", answer.judge(result([candidate("pdf-filler")], 0.01), CONFIG)[0])

    def test_an_exact_name_is_never_refused_and_a_missing_score_never_refuses(self):
        exact = result([candidate("pdf-filler")], 0.10, query="pdf filler")
        self.assertEqual("exact", answer.judge(exact, refusing(0.768))[0])
        self.assertEqual("possible", answer.judge(result([candidate("pdf-filler")], None), refusing(0.768))[0])


class SettingTests(unittest.TestCase):
    def test_wrong_values_are_refused(self):
        for bad in (-0.1, 1.5, True, "0.768", None):
            config = copy.deepcopy(CONFIG)
            config["refuse_below"] = bad
            with self.assertRaises(SettingsError, msg=repr(bad)):
                _validate_answer(config)

    def test_a_bad_value_or_unknown_profile_in_the_file_fails_loudly(self):
        for text, expected in (
            ('[answer]\nrefuse_below = 2.0\n', "refuse_below"),
            ('[answer]\nrefuse_below = "high"\n', "refuse_below"),
            ('[semantic]\nprofile = "no-such-model"\n', "profile"),
            ('[semantic]\nprofile = 3\n', "profile"),
        ):
            with tempfile.TemporaryDirectory() as directory, self.subTest(text=text):
                root = Path(directory)
                write_settings(root, text)
                with patch.dict(os.environ, {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                    with self.assertRaisesRegex(SettingsError, expected):
                        load_settings()

    def test_the_default_profile_keeps_today_s_model_and_gate(self):
        shipped = defaults()
        self.assertEqual("bge-small-en-v1.5", shipped["semantic"]["profile"])
        self.assertEqual("BAAI/bge-small-en-v1.5", shipped["semantic"]["model"]["id"])
        self.assertEqual(0.66, shipped["search"]["minimum_similarity"])

    def test_the_arctic_profile_pins_its_model_and_drops_the_bge_gate_and_a_user_key_still_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, '[semantic]\nprofile = "arctic-embed-s"\n')
            with patch.dict(os.environ, {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                config = load_settings()
                model = config["semantic"]["model"]
                self.assertEqual("snowflake/snowflake-arctic-embed-s", model["id"])
                self.assertEqual("snowflake/snowflake-arctic-embed-s", model["artifact"])
                self.assertRegex(model["revision"], r"^[0-9a-f]{40}$")
                self.assertEqual(384, model["dimensions"])
                self.assertIn("onnx/model.onnx", model["files"])
                self.assertTrue(all(len(digest) == 64 for digest in model["files"].values()))
                self.assertEqual(0.0, config["search"]["minimum_similarity"])
                self.assertEqual(0.0, config["answer"]["refuse_below"])  # the rule stays off until the holdout passes
                self.assertNotEqual(semantic.model_key(), defaults()["compatibility"]["semantic_key"])
                write_settings(root, '[semantic]\nprofile = "arctic-embed-s"\n[search]\nminimum_similarity = 0.5\n')
                self.assertEqual(0.5, load_settings()["search"]["minimum_similarity"])


    def test_model_id_and_revision_cannot_climb_out_of_the_cache_folder_with_or_without_a_profile(self):
        # A model id, revision or artifact name that climbs out of the cache folder is refused, with or without a profile
        # (the profile code once ran before any check of the model's name parts).
        fixtures = (("id", "../../escape"), ("revision", "../x"), ("id", "a/../../b"), ("artifact", "../x"), ("revision", "a/b"))
        for profile in ("", '[semantic]\nprofile = "arctic-embed-s"\n'):
            for key, value in fixtures:
                with tempfile.TemporaryDirectory() as directory, self.subTest(profile=bool(profile), key=key, value=value):
                    root = Path(directory)
                    write_settings(root, profile + ("[semantic.model]" if "[semantic]" not in profile else "\n[semantic.model]") + f'\n{key} = "{value}"\n')
                    environment = {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
                    with patch.dict(os.environ, environment, clear=True):
                        with self.assertRaisesRegex(SettingsError, "invalid semantic.model"):
                            load_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, '[semantic.model]\nid = "owner/model"\nrevision = "abc123"\n')
            environment = {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(root / "cache/capalith/models/model/abc123", semantic.model_directory())


def skill(root: Path, name: str, description: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n# Instructions\n{description}\n")


class RefusalThroughTheStoreTests(unittest.TestCase):
    def build(self, root: Path):
        source_root = root / "skills"
        skill(source_root, "pdf-form-filler", "Fill in and flatten PDF forms.")
        store = Store(root / "catalog.sqlite3")
        store.initialize()
        scan_local_source(store, store.add_local_source(source_root).id)
        return store

    def test_the_best_cosine_reaches_the_result_and_the_rule_refuses_below_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, "[answer]\nrefuse_below = 0.5\n")
            environment = {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")}
            with patch.dict(os.environ, environment, clear=True), patch(
                "capalith.semantic.embed_documents", side_effect=lambda texts: tuple(VECTOR for _ in texts)
            ):
                store = self.build(root)
                with patch("capalith.store._semantic_query_vector", return_value=ORTHOGONAL):
                    far = store.discover("quarterly zebra ledger")
                    self.assertAlmostEqual(0.0, far["semantic"]["best_similarity"], places=6)
                    shaped = answer.shape(store, far, "short")
                    self.assertEqual("no_match", shaped["status"])
                    self.assertTrue(shaped["_text"].startswith("no_match: No skill in the catalog fits"))
                with patch("capalith.store._semantic_query_vector", return_value=VECTOR):
                    near = store.discover("pdf forms")
                    self.assertAlmostEqual(1.0, near["semantic"]["best_similarity"], places=6)
                    self.assertNotEqual("no_match", answer.shape(store, near, "short")["status"])

    def test_a_model_change_is_stale_until_reindexed_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")}
            with patch.dict(os.environ, environment, clear=True), patch(
                "capalith.semantic.embed_documents", side_effect=lambda texts: tuple(VECTOR for _ in texts)
            ), patch("capalith.store._semantic_query_vector", return_value=VECTOR):
                store = self.build(root)
                self.assertIn("semantic", store.discover("pdf forms")["retrieval"]["method"])
                write_settings(root, '[semantic]\nprofile = "arctic-embed-s"\n')
                stale = store.discover("pdf forms")
                self.assertEqual("stale", stale["semantic"]["status"])
                self.assertEqual("index_skills", stale["semantic"]["action"])
                self.assertNotIn("best_similarity", stale["semantic"])
                scan_local_source(store, store.list_sources()[0].id)
                self.assertIn("semantic", store.discover("pdf forms")["retrieval"]["method"])

    def test_hostile_query_text_with_the_rule_on_never_breaks_the_answer(self):
        queries = ["\x00", "'; DROP TABLE artifacts; --", '"', "NEAR(a b) AND OR NOT", "‮fill‬", "e" + "́" * 5000,
                   "a " * 20000, "{name.__class__} {nearest}"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, "[answer]\nrefuse_below = 0.9\n")
            environment = {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")}
            with patch.dict(os.environ, environment, clear=True), patch(
                "capalith.semantic.embed_documents", side_effect=lambda texts: tuple(VECTOR for _ in texts)
            ):
                store = self.build(root)
                with patch("capalith.store._semantic_query_vector", return_value=ORTHOGONAL):
                    for query in queries:
                        try:
                            record = store.discover(query)
                        except Exception as error:  # a clean refusal of the query is fine, a crash is not
                            self.assertEqual("CatalogError", type(error).__name__, repr(query[:20]))
                            continue
                        shaped = answer.shape(store, record, "short")
                        self.assertIn(shaped["status"], {"no_match", "weak", "possible", "good", "close", "exact"})
                        self.assertNotIn("__class__", shaped["message"])


if __name__ == "__main__":
    unittest.main()
