import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from capalith.catalog import CatalogError, build_discovery, extract_catalog
from capalith.identity import artifact_id, hash_bundle
from capalith.intake import scan_local_source
from capalith.store import SCHEMA_VERSION, Store, StoreError
from tests.helpers import current_artifact


def make_catalog_skill(
    root: Path,
    name: str,
    *,
    bundle_path: str | None = None,
    body: str = "",
    description: str | None = None,
    metadata: dict[str, str] | None = None,
) -> Path:
    bundle = root / (bundle_path or name)
    bundle.mkdir(parents=True)
    metadata_text = ""
    if metadata:
        metadata_text = "metadata:\n" + "".join(
            f"  {key}: {value}\n" for key, value in metadata.items()
        )
    (bundle / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or f'{name.title()} skill.'}\n"
        f"{metadata_text}---\n# {name.title()}\n{body}",
        encoding="utf-8",
    )
    return bundle


def catalog_observation(bundle: Path):
    manifest = hash_bundle(bundle)
    descriptor = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return extract_catalog(descriptor, manifest)
    finally:
        os.close(descriptor)


@patch.dict(
    os.environ,
    {"CAPALITH_SEMANTIC_MODEL": "/missing-capalith-test-model"},
)
class CatalogTests(unittest.TestCase):
    def test_invalid_skill_metadata_is_not_admitted(self) -> None:
        cases = (
            (
                "null relationship",
                "---\nname: alpha\ndescription: Alpha.\nmetadata:\n"
                "  capalith.requires: null\n---\n",
                "invalid_relationship",
            ),
            (
                "unsafe yaml value",
                "---\nname: alpha\ndescription: 2026-13-01\n---\n",
                "invalid_frontmatter",
            ),
            (
                "duplicate relationship key",
                "---\nname: alpha\ndescription: Alpha.\nmetadata:\n"
                "  capalith.requires: beta\n  capalith.requires: gamma\n---\n",
                None,
            ),
        )
        for label, skill_text, reason in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory) / "alpha"
                bundle.mkdir()
                (bundle / "SKILL.md").write_text(skill_text, encoding="utf-8")

                observation = catalog_observation(bundle)

                self.assertEqual("invalid", observation.status)
                self.assertEqual((), observation.relationships)
                if reason is not None:
                    self.assertEqual(reason, observation.reason)

    def test_literal_block_description_is_normalized_and_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "humanizer"
            bundle.mkdir()
            bundle.joinpath("SKILL.md").write_text(
                "---\n"
                "name: humanizer\n"
                "description: |\n"
                "  Rewrite AI-sounding text without changing its meaning.\n"
                "  Use when editing prose for stock language or filler.\n"
                "---\n"
                "# Humanizer\n",
                encoding="utf-8",
            )

            observation = catalog_observation(bundle)

            self.assertEqual("ready", observation.status)
            self.assertEqual(
                "Rewrite AI-sounding text without changing its meaning.\n"
                "Use when editing prose for stock language or filler.",
                observation.description,
            )

    def test_catalog_publication_failure_preserves_prior_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source_root = temp / "source"
            bundle = make_catalog_skill(source_root, "alpha", body="nebula evidence\n")
            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(source_root)
            scan_local_source(store, source.id)
            identifier = artifact_id(source.id, "alpha")
            before_artifact = current_artifact(store, identifier)
            before_discovery = cast(dict[str, Any], store.discover("nebula"))

            bundle.joinpath("SKILL.md").write_text(
                "---\nname: alpha\ndescription: Alpha.\n---\nquasar evidence\n",
                encoding="utf-8",
            )
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_catalog_publication "
                    "BEFORE INSERT ON catalog_resources "
                    "BEGIN SELECT RAISE(ABORT,'catalog failure'); END"
                )

            with self.assertRaises(sqlite3.Error):
                scan_local_source(store, source.id)

            self.assertEqual(before_artifact, current_artifact(store, identifier))
            self.assertEqual(before_discovery, store.discover("nebula"))
            self.assertEqual([], store.discover("quasar")["candidates"])

    def test_discovery_treats_query_syntax_as_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source_root = temp / "source"
            make_catalog_skill(source_root, "alpha", body="nebula evidence\n")
            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(source_root)
            scan_local_source(store, source.id)

            result = cast(
                dict[str, Any],
                store.discover('nebula" OR "drop table catalog_entries'),
            )

            self.assertEqual("alpha", result["candidates"][0]["name"])
            self.assertEqual(["alpha"], [item["bundle_path"] for item in store.list_artifacts()])
            self.assertEqual("alpha", store.discover("nebula")["candidates"][0]["name"])

    def test_discovery_skips_nested_hidden_bundles_but_accepts_hidden_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            shared_root = temp / "shared"
            profile_root = temp / "profile"
            archive_root = profile_root / ".archive"
            make_catalog_skill(shared_root, "collision", body="live evidence\n")
            make_catalog_skill(
                profile_root,
                "collision",
                bundle_path=".archive/old",
                body="archived evidence\n",
            )

            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            shared = store.add_local_source(shared_root)
            profile = store.add_local_source(profile_root)
            explicit = store.add_local_source(archive_root)

            self.assertEqual(1, scan_local_source(store, shared.id).observed)
            self.assertEqual(0, scan_local_source(store, profile.id).observed)
            self.assertEqual(1, scan_local_source(store, explicit.id).observed)

            collision = cast(
                dict[str, Any],
                store.discover(
                    "collision",
                    source_ids=(profile.id, shared.id),
                ),
            )["candidates"][0]
            explicitly_selected = cast(
                dict[str, Any],
                store.discover(
                    "archived evidence",
                    source_ids=(explicit.id,),
                ),
            )["candidates"][0]

            self.assertEqual(shared.id, collision["source_id"])
            self.assertEqual("collision", collision["bundle_path"])
            self.assertEqual(explicit.id, explicitly_selected["source_id"])
            self.assertEqual("old", explicitly_selected["bundle_path"])

    def test_discovery_scope_precedence_and_continuation_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            base_root = temp / "base"
            overlay_root = temp / "overlay"
            excluded_root = temp / "excluded"
            changed = make_catalog_skill(base_root, "shadow", body="nebula base\n")
            for index in range(6):
                make_catalog_skill(base_root, f"skill-{index}", body="nebula evidence\n")
            make_catalog_skill(overlay_root, "shadow", body="nebula overlay\n")
            make_catalog_skill(excluded_root, "excluded", body="nebula evidence\n")

            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            base = store.add_local_source(base_root)
            overlay = store.add_local_source(overlay_root)
            excluded = store.add_local_source(excluded_root)
            for source in (base, overlay, excluded):
                scan_local_source(store, source.id)

            scope = (overlay.id, base.id)
            full = cast(
                dict[str, Any],
                store.discover("nebula", source_ids=scope, limit=20),
            )
            first = cast(
                dict[str, Any],
                store.discover("nebula", source_ids=scope, limit=3),
            )
            first_retrieval = cast(dict[str, Any], first["retrieval"])
            second = cast(
                dict[str, Any],
                store.discover(
                    "nebula",
                    source_ids=scope,
                    limit=20,
                    offset=3,
                    view_id=str(first_retrieval["view_id"]),
                ),
            )
            candidates = cast(list[dict[str, Any]], full["candidates"])

            self.assertEqual(
                candidates,
                first["candidates"] + second["candidates"],
            )
            self.assertEqual(
                first_retrieval["view_id"],
                cast(dict[str, Any], second["retrieval"])["view_id"],
            )
            self.assertEqual(overlay.id, next(item for item in candidates if item["name"] == "shadow")["source_id"])
            self.assertNotIn(excluded.id, {item["source_id"] for item in candidates})
            self.assertEqual(len(candidates), len({item["name"] for item in candidates}))

            changed.joinpath("SKILL.md").write_text(
                "---\nname: shadow\ndescription: Changed.\n---\nnebula changed\n",
                encoding="utf-8",
            )
            scan_local_source(store, base.id)
            with self.assertRaisesRegex(StoreError, "^stale_view$"):
                store.discover(
                    "nebula",
                    source_ids=scope,
                    view_id=str(first_retrieval["view_id"]),
                )

    def test_discovery_lexical_bound_does_not_cap_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source_root = temp / "source"
            make_catalog_skill(source_root, "alpha", body="nebula evidence\n")
            make_catalog_skill(source_root, "beta", body="nebula evidence\n")
            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(source_root)
            scan_local_source(store, source.id)

            with patch("capalith.store._LEXICAL_MATCH_LIMIT", 1):
                result = cast(dict[str, Any], store.discover("nebula", limit=20))

            self.assertEqual(
                ["alpha", "beta"],
                sorted(candidate["name"] for candidate in result["candidates"]),
            )

    def test_discovery_scope_isolated_from_excluded_source_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            included_root = temp / "included"
            excluded_root = temp / "excluded"
            make_catalog_skill(included_root, "alpha", body="rare rare\n")
            make_catalog_skill(included_root, "beta", body="common\n")
            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            included = store.add_local_source(included_root)
            scan_local_source(store, included.id)
            before = cast(
                dict[str, Any],
                store.discover("rare common", source_ids=(included.id,), limit=20),
            )

            for index in range(20):
                make_catalog_skill(
                    excluded_root,
                    f"excluded-{index}",
                    body="rare evidence\n",
                )
            excluded = store.add_local_source(excluded_root)
            scan_local_source(store, excluded.id)
            after = cast(
                dict[str, Any],
                store.discover("rare common", source_ids=(included.id,), limit=20),
            )

            self.assertEqual(before["candidates"], after["candidates"])
            self.assertEqual(
                cast(dict[str, Any], before["retrieval"])["view_id"],
                cast(dict[str, Any], after["retrieval"])["view_id"],
            )

    def test_discovery_schema_change_invalidates_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source_root = temp / "source"
            make_catalog_skill(source_root, "alpha", body="rare common\n")
            make_catalog_skill(source_root, "beta", body="rare common\n")
            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(source_root)
            scan_local_source(store, source.id)
            first = cast(dict[str, Any], store.discover("rare", limit=1))
            retrieval = cast(dict[str, Any], first["retrieval"])

            with patch("capalith.store.SCHEMA_VERSION", SCHEMA_VERSION + 1):
                with self.assertRaisesRegex(StoreError, "^stale_view$"):
                    store.discover(
                        "rare",
                        limit=1,
                        offset=1,
                        view_id=str(retrieval["view_id"]),
                    )

    def test_v5_migration_rebuilds_active_search_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            lower_root = temp / "lower"
            preferred_root = temp / "preferred"
            disabled_root = temp / "disabled"
            make_catalog_skill(lower_root, "shadow", body="rare shadowed\n")
            make_catalog_skill(preferred_root, "alpha", body="rare rare\n")
            make_catalog_skill(preferred_root, "beta", body="common\n")
            make_catalog_skill(preferred_root, "shadow", body="neutral\n")
            history = make_catalog_skill(preferred_root, "history", body="neutral\n")
            make_catalog_skill(disabled_root, "disabled-hit", body="rare disabled\n")
            database = temp / "catalog.sqlite3"
            store = Store(database)
            store.initialize()
            lower = store.add_local_source(lower_root)
            preferred = store.add_local_source(preferred_root)
            disabled = store.add_local_source(disabled_root)
            for source in (lower, preferred, disabled):
                scan_local_source(store, source.id)
            store.set_source_status(disabled.id, "disabled")

            history.joinpath("SKILL.md").write_text(
                "---\nname: history\ndescription: History skill.\n---\nrare stale\n",
                encoding="utf-8",
            )
            scan_local_source(store, preferred.id)
            history_id = artifact_id(preferred.id, "history")
            historical_digest = current_artifact(store, history_id)["current_digest"]
            history.joinpath("SKILL.md").write_text(
                "---\nname: history\ndescription: History skill.\n---\nneutral\n",
                encoding="utf-8",
            )
            scan_local_source(store, preferred.id)

            with sqlite3.connect(database) as connection:
                preserved_counts = tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("artifact_versions", "catalog_entries", "catalog_fts")
                )
                connection.execute("DROP TABLE catalog_search_fts")
                connection.execute("DROP TABLE artifact_git_revisions")
                connection.execute("DROP TABLE current_git_revisions")
                connection.execute("PRAGMA user_version=5")

            migrated = Store(database)
            migrated.initialize()
            first = cast(dict[str, Any], migrated.discover("rare common", limit=1))
            retrieval = cast(dict[str, Any], first["retrieval"])
            continued = cast(
                dict[str, Any],
                migrated.discover(
                    "rare common",
                    limit=20,
                    offset=1,
                    view_id=str(retrieval["view_id"]),
                ),
            )
            inspection = current_artifact(migrated, history_id)

            self.assertEqual(
                ["alpha", "beta"],
                [item["name"] for item in first["candidates"] + continued["candidates"]],
            )
            self.assertIn(
                historical_digest,
                {version["digest"] for version in inspection["versions"]},
            )
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    {"artifact_git_revisions", "current_git_revisions"},
                    {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name IN (?,?)",
                            ("artifact_git_revisions", "current_git_revisions"),
                        )
                    },
                )
                self.assertEqual(
                    SCHEMA_VERSION,
                    connection.execute("PRAGMA user_version").fetchone()[0],
                )
                self.assertEqual(
                    preserved_counts,
                    tuple(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        for table in ("artifact_versions", "catalog_entries", "catalog_fts")
                    ),
                )
                self.assertEqual(
                    {preferred.id},
                    {
                        row[0]
                        for row in connection.execute(
                            "SELECT DISTINCT a.source_id FROM catalog_search_fts AS f "
                            "JOIN artifacts AS a ON a.id=f.artifact_id"
                        )
                    },
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM catalog_search_fts "
                        "WHERE artifact_id=? AND content_digest=?",
                        (history_id, historical_digest),
                    ).fetchone()[0],
                )

    def test_discovery_history_does_not_change_frozen_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source_root = temp / "source"
            make_catalog_skill(source_root, "alpha", body="rare rare\n")
            make_catalog_skill(source_root, "beta", body="common\n")
            noise = make_catalog_skill(source_root, "noise", body="neutral\n")
            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(source_root)
            scan_local_source(store, source.id)
            first = cast(dict[str, Any], store.discover("rare common", limit=1))
            retrieval = cast(dict[str, Any], first["retrieval"])

            for index in range(40):
                noise.joinpath("SKILL.md").write_text(
                    "---\nname: noise\ndescription: Noise skill.\n---\n# Noise\n"
                    f"rare history {index}\n",
                    encoding="utf-8",
                )
                scan_local_source(store, source.id)
            noise.joinpath("SKILL.md").write_text(
                "---\nname: noise\ndescription: Noise skill.\n---\n# Noise\nneutral\n",
                encoding="utf-8",
            )
            scan_local_source(store, source.id)

            continued = cast(
                dict[str, Any],
                store.discover(
                    "rare common",
                    limit=20,
                    offset=1,
                    view_id=str(retrieval["view_id"]),
                ),
            )

            self.assertEqual(["alpha"], [item["name"] for item in first["candidates"]])
            self.assertEqual(
                ["beta"], [item["name"] for item in continued["candidates"]]
            )

    def test_discovery_full_scope_ranks_after_precedence_collapse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            lower_root = temp / "lower"
            preferred_root = temp / "preferred"
            make_catalog_skill(preferred_root, "alpha", body="rare rare\n")
            make_catalog_skill(preferred_root, "beta", body="common\n")
            for index in range(40):
                name = f"noise-{index}"
                make_catalog_skill(lower_root, name, body="rare shadowed\n")
                make_catalog_skill(preferred_root, name, body="neutral\n")

            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            lower = store.add_local_source(lower_root)
            scan_local_source(store, lower.id)
            preferred = store.add_local_source(preferred_root)
            scan_local_source(store, preferred.id)

            preferred_only = cast(
                dict[str, Any],
                store.discover(
                    "rare common", source_ids=(preferred.id,), limit=20
                ),
            )
            full_scope = cast(
                dict[str, Any], store.discover("rare common", limit=20)
            )

            self.assertEqual(
                ["alpha", "beta"],
                [item["name"] for item in preferred_only["candidates"]],
            )
            self.assertEqual(
                preferred_only["candidates"], full_scope["candidates"]
            )

    def test_discovery_uses_one_snapshot_during_source_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            alpha_root = temp / "alpha-source"
            beta_root = temp / "beta-source"
            make_catalog_skill(alpha_root, "alpha", body="nebula evidence\n")
            make_catalog_skill(beta_root, "beta", body="nebula evidence\n")
            database = temp / "catalog.sqlite3"
            store = Store(database)
            store.initialize()
            alpha_source = store.add_local_source(alpha_root)
            beta_source = store.add_local_source(beta_root)
            scan_local_source(store, alpha_source.id)
            scan_local_source(store, beta_source.id)
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
            before = cast(dict[str, Any], store.discover("nebula"))

            transitioned = False

            def disable_beta(*arguments: Any) -> dict[str, object]:
                nonlocal transitioned
                Store(database).set_source_status(beta_source.id, "disabled")
                transitioned = True
                return build_discovery(*arguments)

            with patch(
                "capalith.store.build_discovery",
                side_effect=disable_beta,
            ):
                during = cast(dict[str, Any], store.discover("nebula"))

            self.assertTrue(transitioned)
            self.assertEqual(before, during)
            after = cast(dict[str, Any], Store(database).discover("nebula"))
            self.assertEqual(
                ["alpha"],
                [candidate["name"] for candidate in after["candidates"]],
            )

    def test_relationships_explain_search_and_expand_only_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source_root = temp / "source"
            alpha = make_catalog_skill(
                source_root,
                "alpha",
                metadata={
                    "capalith.requires": "beta",
                    "capalith.complements": "delta",
                },
            )
            (alpha / "references").mkdir()
            (alpha / "references" / "guide.txt").write_text(
                "The nebula answer is here.\n", encoding="utf-8"
            )
            make_catalog_skill(
                source_root,
                "beta",
                metadata={"capalith.requires": "gamma"},
            )
            make_catalog_skill(source_root, "gamma")
            make_catalog_skill(source_root, "delta")
            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(source_root)
            scan_local_source(store, source.id)

            result = cast(dict[str, Any], store.discover("nebula"))
            candidate = cast(list[dict[str, Any]], result["candidates"])[0]
            recommendation = cast(dict[str, Any], result["recommendation"])

            self.assertEqual("alpha", candidate["name"])
            self.assertEqual(hash_bundle(alpha).digest, candidate["content_digest"])
            self.assertEqual("references/guide.txt", candidate["matches"][0]["path"])
            self.assertEqual("The nebula answer is here.", candidate["matches"][0]["excerpt"])
            self.assertEqual(["complements", "requires"], [item["type"] for item in candidate["relationships"]])
            self.assertTrue(all(item["resolution"]["status"] == "resolved" for item in candidate["relationships"]))
            self.assertTrue(all(item["evidence"]["path"] == "SKILL.md" for item in candidate["relationships"]))
            self.assertEqual("ready", recommendation["status"])
            self.assertEqual(["gamma", "beta", "alpha"], [item["name"] for item in recommendation["skills"]])
            self.assertTrue(
                all(
                    item["source_id"] == source.id and item["skill_path"] == "SKILL.md"
                    for item in recommendation["skills"]
                )
            )


    def test_traversal_is_cycle_safe_and_keeps_unresolved_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source_root = temp / "source"
            alpha = make_catalog_skill(
                source_root,
                "alpha",
                metadata={
                    "capalith.alternatives": "ghost",
                    "capalith.requires": "beta",
                },
            )
            beta = make_catalog_skill(
                source_root,
                "beta",
                metadata={"capalith.requires": "alpha"},
            )
            store = Store(temp / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(source_root)
            scan_local_source(store, source.id)

            result = cast(
                dict[str, Any],
                store.traverse(
                    artifact_id(source.id, "alpha"),
                    hash_bundle(alpha).digest,
                    depth=3,
                ),
            )
            relationships = cast(list[dict[str, Any]], result["relationships"])

            self.assertEqual(3, len(relationships))
            self.assertEqual(
                {
                    (1, "alpha", "ghost"),
                    (1, "alpha", "beta"),
                    (2, "beta", "alpha"),
                },
                {
                    (
                        item["distance"],
                        item["declared_by"]["name"],
                        item["target_name"],
                    )
                    for item in relationships
                },
            )
            ghost = next(item for item in relationships if item["target_name"] == "ghost")
            self.assertEqual("missing", ghost["resolution"]["status"])
            self.assertNotIn("artifact_id", ghost["resolution"])
            inbound = cast(
                dict[str, Any],
                store.traverse(
                    artifact_id(source.id, "beta"),
                    hash_bundle(beta).digest,
                    relationship_types=("requires",),
                    direction="inbound",
                    depth=1,
                ),
            )
            self.assertEqual(
                [("alpha", "beta")],
                [
                    (item["declared_by"]["name"], item["target_name"])
                    for item in cast(list[dict[str, Any]], inbound["relationships"])
                ],
            )

    def test_unresolved_requirements_and_conflicts_block_recommendation(self) -> None:
        cases = (
            (
                "missing",
                [("alpha", "alpha", {"capalith.requires": "ghost"}, "nebula")],
                ("ghost",),
                "missing",
            ),
            (
                "ambiguous",
                [
                    ("alpha", "alpha", {"capalith.requires": "shared"}, "nebula"),
                    ("one", "shared", {}, ""),
                    ("two", "shared", {}, ""),
                ],
                ("shared",),
                "ambiguous",
            ),
            (
                "selected conflict",
                [
                    (
                        "alpha",
                        "alpha",
                        {
                            "capalith.requires": "beta",
                            "capalith.conflicts": "beta",
                        },
                        "nebula",
                    ),
                    ("beta", "beta", {}, ""),
                ],
                ("alpha", "beta"),
                "resolved",
            ),
        )
        for label, skills, uncertainty_terms, resolution_status in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                source_root = temp / "source"
                for bundle_path, name, metadata, body in skills:
                    make_catalog_skill(
                        source_root,
                        name,
                        bundle_path=bundle_path,
                        metadata=metadata,
                        body=f"{body}\n" if body else "",
                    )
                store = Store(temp / "catalog.sqlite3")
                store.initialize()
                source = store.add_local_source(source_root)
                scan_local_source(store, source.id)

                result = cast(dict[str, Any], store.discover("nebula"))
                recommendation = cast(dict[str, Any], result["recommendation"])
                candidates = cast(list[dict[str, Any]], result["candidates"])

                self.assertEqual("blocked", recommendation["status"])
                self.assertTrue(
                    any(
                        all(term in message for term in uncertainty_terms)
                        for message in recommendation["uncertainty"]
                    )
                )
                relationship = next(
                    item
                    for item in candidates[0]["relationships"]
                    if item["resolution"]["status"] == resolution_status
                )
                if resolution_status != "resolved":
                    self.assertNotIn("artifact_id", relationship["resolution"])

    def test_catalog_rejects_bytes_changed_after_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "alpha"
            bundle.mkdir()
            skill = bundle / "SKILL.md"
            skill.write_text(
                "---\nname: alpha\ndescription: Alpha.\n---\n# Alpha\n",
                encoding="utf-8",
            )
            manifest = hash_bundle(bundle)
            skill.write_text("changed after hashing\n", encoding="utf-8")
            descriptor = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                with self.assertRaises(CatalogError):
                    extract_catalog(descriptor, manifest)
            finally:
                os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
