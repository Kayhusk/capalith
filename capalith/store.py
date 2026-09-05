from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from capalith import semantic
from capalith.catalog import (
    CATALOG_FORMAT_VERSION,
    CatalogObservation,
    build_discovery,
    fts_query,
    query_tokens,
    skill_markdown_body,
)
from capalith.identity import BundleManifest, artifact_id

SCHEMA_VERSION = 7
_DISCOVERY_LIMIT = 5
_DISCOVERY_MAX_LIMIT = 50
_LEXICAL_MATCH_LIMIT = 1000
_TRAVERSAL_LIMIT = 10
_TRAVERSAL_MAX_LIMIT = 100
_TRAVERSAL_MAX_DEPTH = 3
_RELATIONSHIP_TYPES = (
    "requires",
    "complements",
    "alternatives",
    "conflicts",
    "supersedes",
)
_RRF_RANK_CONSTANT = 60
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW

_SCHEMA = (
    """CREATE TABLE sources(
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        locator TEXT NOT NULL,
        requested_ref TEXT,
        status TEXT NOT NULL DEFAULT 'enabled'
            CHECK(status IN('enabled','disabled','removed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(
            (kind='local' AND requested_ref IS NULL) OR
            (kind='git' AND requested_ref IS NOT NULL)
        )
    )""",
    """CREATE TABLE artifacts(
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        bundle_path TEXT NOT NULL,
        current_digest TEXT NOT NULL,
        present INTEGER NOT NULL CHECK(present IN(0,1)),
        last_seen_at TEXT NOT NULL,
        UNIQUE(source_id,bundle_path)
    )""",
    """CREATE TABLE artifact_versions(
        artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
        digest TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        file_count INTEGER NOT NULL CHECK(file_count>=1),
        byte_count INTEGER NOT NULL CHECK(byte_count>=0),
        first_seen_at TEXT NOT NULL,
        PRIMARY KEY(artifact_id,digest)
    )""",
    """CREATE TABLE git_revisions(
        id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        requested_ref TEXT NOT NULL,
        resolved_commit TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE(source_id,requested_ref,resolved_commit)
    )""",
    """CREATE TABLE artifact_git_revisions(
        git_revision_id INTEGER NOT NULL REFERENCES git_revisions(id) ON DELETE CASCADE,
        artifact_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        PRIMARY KEY(git_revision_id,artifact_id,digest),
        FOREIGN KEY(artifact_id,digest)
            REFERENCES artifact_versions(artifact_id,digest) ON DELETE CASCADE
    )""",
    """CREATE TABLE current_git_revisions(
        source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
        requested_ref TEXT NOT NULL,
        resolved_commit TEXT NOT NULL,
        FOREIGN KEY(source_id,requested_ref,resolved_commit)
            REFERENCES git_revisions(source_id,requested_ref,resolved_commit)
            ON DELETE CASCADE
    )""",
)

_CATALOG_SCHEMA = (
    """CREATE TABLE catalog_entries(
        artifact_id TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        catalog_format_version INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN('ready','invalid')),
        name TEXT,
        description TEXT,
        raw_frontmatter TEXT,
        skill_text TEXT,
        reason TEXT,
        PRIMARY KEY(artifact_id,content_digest,catalog_format_version),
        FOREIGN KEY(artifact_id,content_digest)
            REFERENCES artifact_versions(artifact_id,digest) ON DELETE CASCADE
    )""",
    """CREATE TABLE catalog_resources(
        artifact_id TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        catalog_format_version INTEGER NOT NULL,
        path TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN('text','non_text')),
        reason TEXT,
        text TEXT,
        PRIMARY KEY(artifact_id,content_digest,catalog_format_version,path),
        FOREIGN KEY(artifact_id,content_digest,catalog_format_version)
            REFERENCES catalog_entries(
                artifact_id,content_digest,catalog_format_version
            ) ON DELETE CASCADE
    )""",
    """CREATE TABLE catalog_relationships(
        artifact_id TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        catalog_format_version INTEGER NOT NULL,
        relationship_type TEXT NOT NULL,
        target_name TEXT NOT NULL,
        metadata_key TEXT NOT NULL,
        evidence_path TEXT NOT NULL CHECK(evidence_path='SKILL.md'),
        evidence_line INTEGER NOT NULL CHECK(evidence_line>=1),
        PRIMARY KEY(
            artifact_id,content_digest,catalog_format_version,
            relationship_type,target_name
        ),
        FOREIGN KEY(artifact_id,content_digest,catalog_format_version)
            REFERENCES catalog_entries(
                artifact_id,content_digest,catalog_format_version
            ) ON DELETE CASCADE
    )""",
    """CREATE VIRTUAL TABLE catalog_fts USING fts5(
        artifact_id UNINDEXED,
        content_digest UNINDEXED,
        catalog_format_version UNINDEXED,
        path_key UNINDEXED,
        path,
        body,
        tokenize='unicode61 remove_diacritics 0 categories ''L* N*'' tokenchars ''_'''
    )""",
    "CREATE INDEX catalog_entries_name ON catalog_entries(name)",
    """CREATE TABLE catalog_semantic_vectors(
        artifact_id TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        catalog_format_version INTEGER NOT NULL,
        model_key TEXT NOT NULL,
        projection_digest TEXT NOT NULL CHECK(length(projection_digest)=64),
        dimensions INTEGER NOT NULL CHECK(dimensions=384),
        vector BLOB NOT NULL CHECK(typeof(vector)='blob' AND length(vector)=1536),
        PRIMARY KEY(artifact_id,content_digest,catalog_format_version,model_key),
        FOREIGN KEY(artifact_id,content_digest,catalog_format_version)
            REFERENCES catalog_entries(
                artifact_id,content_digest,catalog_format_version
            ) ON DELETE CASCADE
    )""",
)

_CATALOG_SEARCH_SCHEMA = """CREATE VIRTUAL TABLE catalog_search_fts USING fts5(
    artifact_id UNINDEXED,
    content_digest UNINDEXED,
    catalog_format_version UNINDEXED,
    path_key UNINDEXED,
    path,
    body,
    tokenize='unicode61 remove_diacritics 0 categories ''L* N*'' tokenchars ''_'''
)"""


@dataclass(frozen=True)
class Source:
    id: str
    kind: str
    locator: str
    requested_ref: str | None = None
    status: str = "enabled"


@dataclass(frozen=True)
class GitRevision:
    requested_ref: str
    resolved_commit: str


@dataclass(frozen=True)
class GitSourceSnapshot:
    source: Source
    revision: GitRevision | None
    artifacts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ArtifactObservation:
    artifact_id: str
    bundle_path: str
    manifest: BundleManifest
    catalog: CatalogObservation


def _semantic_document_vectors(
    observations: tuple[ArtifactObservation, ...],
) -> dict[tuple[str, str], bytes]:
    ready = tuple(
        observation
        for observation in observations
        if observation.catalog.status == "ready"
    )
    if not ready:
        return {}
    try:
        vectors = semantic.embed_documents(
            tuple(
                semantic.projection(
                    str(observation.catalog.name),
                    str(observation.catalog.description),
                    str(observation.catalog.skill_text),
                )
                for observation in ready
            )
        )
    except semantic.SemanticUnavailable:
        return {}
    return {
        (observation.artifact_id, observation.manifest.digest): vector
        for observation, vector in zip(ready, vectors, strict=True)
    }


def _semantic_query_vector(query: str) -> bytes | None:
    try:
        return semantic.embed_query(query)
    except semantic.SemanticUnavailable:
        return None


class StoreError(Exception):
    pass


def _page_request(
    limit: object,
    offset: object,
    view_id: object,
    *,
    maximum: int,
    view_prefix: str,
) -> tuple[int, int, str | None]:
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise StoreError(f"invalid_request: limit must be between 1 and {maximum}")
    if type(offset) is not int or offset < 0:
        raise StoreError("invalid_request: offset must be a non-negative integer")
    if view_id is not None and (
        type(view_id) is not str
        or re.fullmatch(rf"{re.escape(view_prefix)}:[0-9a-f]{{64}}", view_id) is None
    ):
        raise StoreError("invalid_request: malformed view_id")
    if offset and view_id is None:
        raise StoreError("invalid_request: view_id is required when offset is greater than 0")
    return limit, offset, view_id


def _view_id(prefix: str, value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def _exploration_state(
    connection: sqlite3.Connection, source_ids: list[str]
) -> list[dict[str, object]]:
    state = []
    for source_id in source_ids:
        source = connection.execute(
            "SELECT id,kind,locator,requested_ref,status,updated_at FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
        if source is None:
            state.append({"source_id": source_id, "status": "missing"})
            continue
        artifacts = [
            dict(row)
            for row in connection.execute(
                "SELECT a.id,a.bundle_path,a.current_digest,a.present,c.status AS catalog_status,"
                "c.name,c.description FROM artifacts AS a "
                "LEFT JOIN catalog_entries AS c ON c.artifact_id=a.id "
                "AND c.content_digest=a.current_digest AND c.catalog_format_version=? "
                "WHERE a.source_id=? ORDER BY a.id",
                (CATALOG_FORMAT_VERSION, source_id),
            )
        ]
        state.append({**dict(source), "artifacts": artifacts})
    return state


def _semantic_state(
    connection: sqlite3.Connection, source_ids: list[str]
) -> list[dict[str, object]]:
    if not source_ids:
        return []
    placeholders = ",".join("?" for _ in source_ids)
    return [
        dict(row)
        for row in connection.execute(
            "SELECT v.artifact_id,v.content_digest,v.model_key,v.projection_digest "
            "FROM catalog_semantic_vectors AS v JOIN artifacts AS a "
            "ON a.id=v.artifact_id AND a.current_digest=v.content_digest "
            f"WHERE a.source_id IN ({placeholders}) AND a.present=1 "
            "AND v.catalog_format_version=? AND v.model_key=? "
            "ORDER BY v.artifact_id,v.content_digest",
            (*source_ids, CATALOG_FORMAT_VERSION, semantic.MODEL_KEY),
        )
    ]


class _AnchoredConnection(sqlite3.Connection):
    _parent_descriptor: int | None = None
    _database_descriptor: int | None = None

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if (
            self._database_descriptor is not None
            and os.fstat(self._database_descriptor).st_nlink > 1
        ):
            raise StoreError("database file cannot have multiple hard links")
        return super().execute(sql, parameters)

    def close(self) -> None:
        parent_descriptor = self._parent_descriptor
        database_descriptor = self._database_descriptor
        self._parent_descriptor = None
        self._database_descriptor = None
        try:
            super().close()
        finally:
            if database_descriptor is not None:
                os.close(database_descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)


def _validate_relative_posix(
    value: object, label: str, *, allow_root: bool
) -> None:
    if type(value) is not str:
        raise StoreError(f"{label} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise StoreError(f"{label} must be valid UTF-8") from error
    if not value or "\0" in value:
        raise StoreError(f"{label} is not a canonical relative POSIX path")
    if allow_root and value == ".":
        return
    if (
        value == "."
        or posixpath.isabs(value)
        or posixpath.normpath(value) != value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise StoreError(f"{label} is not a canonical relative POSIX path")


def _validate_observations(
    source_id: str, observations: tuple[ArtifactObservation, ...]
) -> None:
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for observation in observations:
        if type(observation) is not ArtifactObservation:
            raise StoreError("scan entries must be ArtifactObservations")
        if type(observation.catalog) is not CatalogObservation:
            raise StoreError("scan entries must include catalog observations")
        _validate_relative_posix(
            observation.bundle_path, "bundle path", allow_root=True
        )
        expected_id = artifact_id(source_id, observation.bundle_path)
        if observation.artifact_id != expected_id:
            raise StoreError(
                "artifact_id does not match source_id and bundle_path: "
                f"{observation.artifact_id}"
            )
        if observation.artifact_id in seen_ids:
            raise StoreError(f"duplicate artifact_id: {observation.artifact_id}")
        if observation.bundle_path in seen_paths:
            raise StoreError(f"duplicate bundle_path: {observation.bundle_path}")
        seen_ids.add(observation.artifact_id)
        seen_paths.add(observation.bundle_path)


class Store:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(os.path.abspath(path))
        self.read_only = read_only

    def _open_parent(self) -> int:
        descriptor = os.open("/", _DIRECTORY_OPEN_FLAGS)
        try:
            for part in self.path.parent.parts[1:]:
                child = os.open(
                    part,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _connect(self) -> sqlite3.Connection:
        parent_descriptor: int | None = None
        database_descriptor: int | None = None
        connection: sqlite3.Connection | None = None
        try:
            parent_descriptor = self._open_parent()
            flags = os.O_RDONLY if self.read_only else os.O_RDWR | os.O_CREAT
            database_descriptor = os.open(
                self.path.name,
                flags | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_descriptor,
            )
            database_stat = os.fstat(database_descriptor)
            if database_stat.st_nlink > 1:
                raise StoreError("database file cannot have multiple hard links")
            target = f"/proc/self/fd/{parent_descriptor}/{self.path.name}"
            if self.read_only:
                target = f"file:{quote(target, safe='/')}?mode=ro"
            connection = sqlite3.connect(
                target,
                uri=self.read_only,
                isolation_level=None,
                factory=_AnchoredConnection,
            )
            try:
                final_database_stat = os.fstat(database_descriptor)
                if final_database_stat.st_nlink > 1:
                    raise StoreError("database file cannot have multiple hard links")
                path_stat = os.stat(
                    self.path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    database_stat.st_dev,
                    database_stat.st_ino,
                ) != (
                    path_stat.st_dev,
                    path_stat.st_ino,
                ):
                    raise StoreError("database file changed while being opened")
                verification_descriptor = self._open_parent()
                try:
                    if (
                        os.fstat(parent_descriptor).st_dev,
                        os.fstat(parent_descriptor).st_ino,
                    ) != (
                        os.fstat(verification_descriptor).st_dev,
                        os.fstat(verification_descriptor).st_ino,
                    ):
                        raise StoreError("database parent changed while being opened")
                finally:
                    os.close(verification_descriptor)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                if self.read_only:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version != SCHEMA_VERSION:
                        raise sqlite3.DatabaseError("unsupported store schema version")
            except BaseException:
                connection.close()
                raise
            # Keep the verified descriptors open while SQLite checks the file.
            connection._parent_descriptor = parent_descriptor
            parent_descriptor = None
            connection._database_descriptor = database_descriptor
            database_descriptor = None
            return connection
        finally:
            if database_descriptor is not None:
                os.close(database_descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    def validate_local_source(self, locator: Path) -> Path:
        supplied = Path(locator)
        if supplied.is_symlink():
            raise StoreError(f"local source directory cannot be a symlink: {supplied}")
        if not supplied.exists() or not supplied.is_dir():
            raise StoreError(f"local source directory must be an existing directory: {supplied}")
        try:
            root = supplied.resolve(strict=True)
            database = self.path.resolve(strict=False)
        except OSError as error:
            raise StoreError(f"could not resolve local source directory: {supplied}") from error
        if database == root or database.is_relative_to(root):
            raise StoreError("database must be outside the local source directory")
        return root

    def validate_git_source(self, locator: str, requested_ref: str) -> None:
        try:
            if type(locator) is not str:
                raise ValueError
            locator.encode("utf-8")
            if (
                not locator
                or "?" in locator
                or "#" in locator
                or "\\" in locator
                or any(character.isspace() for character in locator)
                or any(
                    unicodedata.category(character) == "Cc" for character in locator
                )
                or re.search(r"%(?![0-9A-Fa-f]{2})", locator)
            ):
                raise ValueError
            parsed = urlsplit(locator)
            hostname = parsed.hostname
            parsed.port
            if parsed.username is not None or parsed.password is not None:
                raise ValueError
            if parsed.scheme.lower() == "https":
                if not hostname:
                    raise ValueError
                hostname.encode("idna")
            elif parsed.scheme.lower() == "file":
                if parsed.netloc.lower() not in ("", "localhost"):
                    raise ValueError
                if not posixpath.isabs(parsed.path):
                    raise ValueError
            else:
                raise ValueError
        except (UnicodeError, ValueError):
            raise StoreError("invalid Git source URL") from None

        try:
            if type(requested_ref) is not str:
                raise ValueError
            requested_ref.encode("utf-8")
            if not requested_ref.startswith(("refs/heads/", "refs/tags/")):
                raise ValueError
            components = requested_ref.split("/")
            if (
                any(not component for component in components)
                or any(
                    component.startswith(".") or component.endswith(".lock")
                    for component in components
                )
                or requested_ref.endswith(".")
                or ".." in requested_ref
                or "@{" in requested_ref
                or "\\" in requested_ref
                or any(
                    unicodedata.category(character) == "Cc"
                    or character in " ~^:?*["
                    for character in requested_ref
                )
            ):
                raise ValueError
        except (UnicodeError, ValueError):
            raise StoreError("invalid Git branch or tag reference") from None

    def initialize(self) -> None:
        if self.read_only:
            raise StoreError("read-only stores cannot be initialized")
        connection = self._connect()
        foreign_keys_disabled = False
        try:
            _require_fts5(connection)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version not in (0, 1, 2, 3, 4, 5, 6):
                raise StoreError(f"unsupported store schema version: {version}")
            if version == 1:
                connection.execute("PRAGMA foreign_keys=OFF")
                if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                    raise StoreError("could not disable foreign keys for migration")
                foreign_keys_disabled = True

            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == SCHEMA_VERSION:
                    connection.execute("COMMIT")
                    return
                if version not in (0, 1, 2, 3, 4, 5, 6):
                    raise StoreError("store schema changed during migration")
                if version == 1:
                    connection.execute(
                        "ALTER TABLE artifact_versions "
                        "RENAME TO _artifact_versions_v1"
                    )
                    connection.execute(
                        "ALTER TABLE artifacts RENAME TO _artifacts_v1"
                    )
                    connection.execute("ALTER TABLE sources RENAME TO _sources_v1")
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO sources"
                        "(id,kind,locator,requested_ref,created_at,updated_at) "
                        "SELECT id,kind,locator,NULL,created_at,updated_at "
                        "FROM _sources_v1"
                    )
                    connection.execute(
                        "INSERT INTO artifacts"
                        "(id,source_id,bundle_path,current_digest,present,last_seen_at) "
                        "SELECT id,source_id,bundle_path,current_digest,present,last_seen_at "
                        "FROM _artifacts_v1"
                    )
                    connection.execute(
                        "INSERT INTO artifact_versions"
                        "(artifact_id,digest,manifest_json,file_count,byte_count,first_seen_at) "
                        "SELECT artifact_id,digest,manifest_json,file_count,byte_count,first_seen_at "
                        "FROM _artifact_versions_v1"
                    )
                    connection.execute("DROP TABLE _artifact_versions_v1")
                    connection.execute("DROP TABLE _artifacts_v1")
                    connection.execute("DROP TABLE _sources_v1")
                elif version == 0:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                elif version in (2, 3):
                    connection.execute(
                        "ALTER TABLE sources ADD COLUMN status TEXT NOT NULL "
                        "DEFAULT 'enabled' "
                        "CHECK(status IN('enabled','disabled','removed'))"
                    )

                if version in (2, 3, 4, 5):
                    connection.execute(_SCHEMA[4])
                if version in (2, 3, 4, 5, 6):
                    connection.execute(_SCHEMA[5])
                connection.execute(
                    "INSERT INTO current_git_revisions"
                    "(source_id,requested_ref,resolved_commit) "
                    "SELECT r.source_id,r.requested_ref,r.resolved_commit "
                    "FROM git_revisions AS r JOIN ("
                    "SELECT source_id FROM git_revisions GROUP BY source_id "
                    "HAVING COUNT(*)=1"
                    ") AS single ON single.source_id=r.source_id"
                )

                if version in (0, 1, 2):
                    for statement in _CATALOG_SCHEMA:
                        connection.execute(statement)
                elif version in (3, 4):
                    connection.execute(_CATALOG_SCHEMA[-1])
                if version in (0, 1, 2, 3, 4, 5):
                    connection.execute(_CATALOG_SEARCH_SCHEMA)
                    _rebuild_catalog_search_fts(connection)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise StoreError("store migration failed foreign key validation")
                connection.execute(
                    "INSERT INTO catalog_fts(catalog_fts) VALUES('integrity-check')"
                )
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if foreign_keys_disabled:
                connection.execute("PRAGMA foreign_keys=ON")
                if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise StoreError("could not restore foreign keys after migration")
            connection.close()

    def add_local_source(self, locator: Path) -> Source:
        root = self.validate_local_source(locator)
        source = Source(f"src_{uuid.uuid4().hex}", "local", str(root))
        timestamp = _utc_timestamp()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO sources(id,kind,locator,created_at,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (source.id, source.kind, source.locator, timestamp, timestamp),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return source

    def add_git_source(self, locator: str, requested_ref: str) -> Source:
        self.validate_git_source(locator, requested_ref)
        source = Source(f"src_{uuid.uuid4().hex}", "git", locator, requested_ref)
        timestamp = _utc_timestamp()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO sources"
                    "(id,kind,locator,requested_ref,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        source.id,
                        source.kind,
                        source.locator,
                        source.requested_ref,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return source

    def _get_stored_source(self, source_id: str) -> Source:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id,kind,locator,requested_ref,status FROM sources WHERE id=?",
                (source_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise StoreError(f"unknown source: {source_id}")
        return Source(
            row["id"],
            row["kind"],
            row["locator"],
            row["requested_ref"],
            row["status"],
        )

    def list_sources(self) -> list[Source]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id,kind,locator,requested_ref,status FROM sources ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        return [
            Source(
                row["id"],
                row["kind"],
                row["locator"],
                row["requested_ref"],
                row["status"],
            )
            for row in rows
        ]

    def set_local_source_path(self, source_id: str, locator: Path) -> Source:
        stored = self._get_stored_source(source_id)
        if stored.status == "removed":
            raise StoreError(f"source is removed: {source_id}")
        root = self.validate_local_source(locator)
        source = Source(source_id, "local", str(root), status=stored.status)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT kind,status FROM sources WHERE id=?", (source_id,)
                ).fetchone()
                if row is None:
                    raise StoreError(f"unknown source: {source_id}")
                if row["kind"] != "local":
                    raise StoreError(f"source is not local: {source_id}")
                if row["status"] == "removed":
                    raise StoreError(f"source is removed: {source_id}")
                connection.execute(
                    "UPDATE sources SET locator=?,updated_at=? WHERE id=?",
                    (source.locator, _utc_timestamp(), source_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return source

    def set_source_status(self, source_id: str, status: str) -> Source:
        if status not in ("enabled", "disabled", "removed"):
            raise StoreError("invalid source status")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT id,kind,locator,requested_ref,status FROM sources WHERE id=?",
                    (source_id,),
                ).fetchone()
                if row is None:
                    raise StoreError(f"unknown source: {source_id}")
                if row["status"] == "removed" and status != "removed":
                    raise StoreError(f"source is removed: {source_id}")
                if row["status"] != status:
                    connection.execute(
                        "UPDATE sources SET status=?,updated_at=? WHERE id=?",
                        (status, _utc_timestamp(), source_id),
                    )
                    _rebuild_catalog_search_fts(connection)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return Source(
            row["id"], row["kind"], row["locator"], row["requested_ref"], status
        )

    def get_source(self, source_id: str, *, require_enabled: bool = False) -> Source:
        source = self._get_stored_source(source_id)
        if require_enabled and source.status != "enabled":
            raise StoreError(f"source is {source.status}: {source_id}")
        if source.kind == "local":
            root = self.validate_local_source(Path(source.locator))
            return Source(source.id, source.kind, str(root), status=source.status)
        if source.kind == "git":
            return source
        raise StoreError(f"unsupported source kind: {source_id}")

    def git_source_snapshot(self, source_id: str) -> GitSourceSnapshot:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT id,kind,locator,requested_ref,status FROM sources WHERE id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown source: {source_id}")
            source = Source(
                row["id"],
                row["kind"],
                row["locator"],
                row["requested_ref"],
                row["status"],
            )
            if source.status != "enabled":
                raise StoreError(f"source is {source.status}: {source_id}")
            if source.kind != "git":
                raise StoreError(f"source is not Git: {source_id}")

            revision_row = connection.execute(
                "SELECT r.requested_ref,r.resolved_commit "
                "FROM current_git_revisions AS c JOIN git_revisions AS r "
                "ON r.source_id=c.source_id AND r.requested_ref=c.requested_ref "
                "AND r.resolved_commit=c.resolved_commit WHERE c.source_id=?",
                (source_id,),
            ).fetchone()
            if revision_row is None and connection.execute(
                "SELECT 1 FROM git_revisions WHERE source_id=?", (source_id,)
            ).fetchone() is not None:
                raise StoreError(f"Git source requires a fresh scan: {source_id}")
            revision = (
                GitRevision(
                    revision_row["requested_ref"], revision_row["resolved_commit"]
                )
                if revision_row is not None
                else None
            )
            artifacts = tuple(
                (artifact["bundle_path"], artifact["current_digest"])
                for artifact in connection.execute(
                    "SELECT bundle_path,current_digest FROM artifacts "
                    "WHERE source_id=? AND present=1 ORDER BY bundle_path",
                    (source_id,),
                )
            )
            connection.execute("COMMIT")
            return GitSourceSnapshot(source, revision, artifacts)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def apply_scan(
        self,
        source_id: str,
        observations: tuple[ArtifactObservation, ...],
        git_revision: GitRevision | None = None,
    ) -> int:
        staged = tuple(observations)
        _validate_observations(source_id, staged)
        missing_vectors: list[ArtifactObservation] = []
        existing_connection = self._connect()
        try:
            for observation in staged:
                catalog = observation.catalog
                if (
                    catalog.status != "ready"
                    or catalog.name is None
                    or catalog.description is None
                    or catalog.skill_text is None
                ):
                    continue
                expected_projection = semantic.projection_digest(
                    semantic.projection(
                        catalog.name, catalog.description, catalog.skill_text
                    )
                )
                exists = existing_connection.execute(
                    "SELECT 1 FROM catalog_semantic_vectors "
                    "WHERE artifact_id=? AND content_digest=? "
                    "AND catalog_format_version=? AND model_key=? "
                    "AND projection_digest=?",
                    (
                        observation.artifact_id,
                        observation.manifest.digest,
                        CATALOG_FORMAT_VERSION,
                        semantic.MODEL_KEY,
                        expected_projection,
                    ),
                ).fetchone()
                if exists is None:
                    missing_vectors.append(observation)
        finally:
            existing_connection.close()
        semantic_vectors = (
            _semantic_document_vectors(tuple(missing_vectors))
            if missing_vectors
            else {}
        )
        timestamp = _utc_timestamp()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                source = connection.execute(
                    "SELECT kind,requested_ref,status FROM sources WHERE id=?",
                    (source_id,),
                ).fetchone()
                if source is None:
                    raise StoreError(f"unknown source: {source_id}")
                if source["status"] != "enabled":
                    raise StoreError(f"source is {source['status']}: {source_id}")
                revision_id: int | None = None
                if source["kind"] == "git":
                    if type(git_revision) is not GitRevision:
                        raise StoreError(
                            f"Git revision required for Git source: {source_id}"
                        )
                    if git_revision.requested_ref != source["requested_ref"]:
                        raise StoreError(
                            f"Git revision does not match Git source: {source_id}"
                        )
                    connection.execute(
                        "INSERT INTO git_revisions"
                        "(source_id,requested_ref,resolved_commit,first_seen_at,last_seen_at) "
                        "VALUES(?,?,?,?,?) "
                        "ON CONFLICT(source_id,requested_ref,resolved_commit) "
                        "DO UPDATE SET last_seen_at=excluded.last_seen_at",
                        (
                            source_id,
                            git_revision.requested_ref,
                            git_revision.resolved_commit,
                            timestamp,
                            timestamp,
                        ),
                    )
                    revision_id = connection.execute(
                        "SELECT id FROM git_revisions "
                        "WHERE source_id=? AND requested_ref=? AND resolved_commit=?",
                        (
                            source_id,
                            git_revision.requested_ref,
                            git_revision.resolved_commit,
                        ),
                    ).fetchone()[0]
                    connection.execute(
                        "INSERT INTO current_git_revisions"
                        "(source_id,requested_ref,resolved_commit) VALUES(?,?,?) "
                        "ON CONFLICT(source_id) DO UPDATE SET "
                        "requested_ref=excluded.requested_ref,"
                        "resolved_commit=excluded.resolved_commit",
                        (
                            source_id,
                            git_revision.requested_ref,
                            git_revision.resolved_commit,
                        ),
                    )
                elif source["kind"] == "local":
                    if git_revision is not None:
                        raise StoreError(
                            f"Git revision forbidden for local source: {source_id}"
                        )
                else:
                    raise StoreError(f"unsupported source kind: {source_id}")

                existing_ids: set[str] = set()
                for observation in staged:
                    identity_row = connection.execute(
                        "SELECT source_id,bundle_path FROM artifacts WHERE id=?",
                        (observation.artifact_id,),
                    ).fetchone()
                    if identity_row is not None:
                        if (
                            identity_row["source_id"] != source_id
                            or identity_row["bundle_path"] != observation.bundle_path
                        ):
                            raise StoreError(
                                "artifact_id is already used by another source or bundle_path: "
                                f"{observation.artifact_id}"
                            )
                        existing_ids.add(observation.artifact_id)

                    path_row = connection.execute(
                        "SELECT id FROM artifacts WHERE source_id=? AND bundle_path=?",
                        (source_id, observation.bundle_path),
                    ).fetchone()
                    if (
                        path_row is not None
                        and path_row["id"] != observation.artifact_id
                    ):
                        raise StoreError(
                            f"artifact path collision: {observation.bundle_path}"
                        )

                connection.execute(
                    "UPDATE artifacts SET present=0 WHERE source_id=?", (source_id,)
                )
                for observation in staged:
                    manifest = observation.manifest
                    if observation.artifact_id in existing_ids:
                        updated = connection.execute(
                            "UPDATE artifacts SET current_digest=?,present=1,last_seen_at=? "
                            "WHERE id=? AND source_id=? AND bundle_path=?",
                            (
                                manifest.digest,
                                timestamp,
                                observation.artifact_id,
                                source_id,
                                observation.bundle_path,
                            ),
                        )
                        if updated.rowcount != 1:
                            raise StoreError(
                                f"artifact changed during scan: {observation.artifact_id}"
                            )
                    else:
                        connection.execute(
                            "INSERT INTO artifacts"
                            "(id,source_id,bundle_path,current_digest,present,last_seen_at) "
                            "VALUES(?,?,?,?,1,?)",
                            (
                                observation.artifact_id,
                                source_id,
                                observation.bundle_path,
                                manifest.digest,
                                timestamp,
                            ),
                        )

                    connection.execute(
                        "INSERT INTO artifact_versions"
                        "(artifact_id,digest,manifest_json,file_count,byte_count,first_seen_at) "
                        "VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(artifact_id,digest) DO NOTHING",
                        (
                            observation.artifact_id,
                            manifest.digest,
                            _canonical_manifest_json(manifest),
                            len(manifest.files),
                            manifest.byte_count,
                            timestamp,
                        ),
                    )
                    _publish_catalog(connection, observation)
                    vector = semantic_vectors.get(
                        (observation.artifact_id, manifest.digest)
                    )
                    if vector is not None:
                        catalog = observation.catalog
                        if (
                            catalog.status != "ready"
                            or catalog.name is None
                            or catalog.description is None
                            or catalog.skill_text is None
                        ):
                            raise StoreError("semantic index has no searchable skill")
                        projection = semantic.projection(
                            catalog.name, catalog.description, catalog.skill_text
                        )
                        connection.execute(
                            "INSERT INTO catalog_semantic_vectors("
                            "artifact_id,content_digest,catalog_format_version,model_key,"
                            "projection_digest,dimensions,vector) VALUES(?,?,?,?,?,?,?) "
                            "ON CONFLICT(artifact_id,content_digest,catalog_format_version,model_key) "
                            "DO UPDATE SET projection_digest=excluded.projection_digest,"
                            "dimensions=excluded.dimensions,vector=excluded.vector",
                            (
                                observation.artifact_id,
                                manifest.digest,
                                CATALOG_FORMAT_VERSION,
                                semantic.MODEL_KEY,
                                semantic.projection_digest(projection),
                                semantic.DIMENSIONS,
                                vector,
                            ),
                        )
                    if revision_id is not None:
                        connection.execute(
                            "INSERT INTO artifact_git_revisions"
                            "(git_revision_id,artifact_id,digest) VALUES(?,?,?) "
                            "ON CONFLICT(git_revision_id,artifact_id,digest) DO NOTHING",
                            (
                                revision_id,
                                observation.artifact_id,
                                manifest.digest,
                            ),
                        )

                connection.execute(
                    "INSERT INTO catalog_fts(catalog_fts) VALUES('integrity-check')"
                )
                _rebuild_catalog_search_fts(connection)
                absent = connection.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE source_id=? AND present=0",
                    (source_id,),
                ).fetchone()[0]
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return absent

    def list_artifacts(self) -> list[dict[str, object]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT a.id AS artifact_id,a.source_id,a.bundle_path,"
                "a.current_digest,a.present,v.file_count,v.byte_count "
                "FROM artifacts AS a JOIN artifact_versions AS v "
                "ON v.artifact_id=a.id AND v.digest=a.current_digest "
                "ORDER BY a.source_id,a.bundle_path,a.id"
            ).fetchall()
            return [_artifact_fields(row) for row in rows]
        finally:
            connection.close()

    def get_artifact(
        self,
        artifact_id: str,
        content_digest: str,
        resource_path: str | None = None,
    ) -> dict[str, object]:
        if type(artifact_id) is not str or not artifact_id:
            raise StoreError("invalid_request: artifact_id must be a non-empty string")
        if (
            type(content_digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", content_digest) is None
        ):
            raise StoreError("invalid_request: content_digest must be a SHA-256 digest")
        if resource_path is not None:
            try:
                _validate_relative_posix(resource_path, "resource path", allow_root=False)
            except StoreError as error:
                raise StoreError(f"invalid_request: {error}") from None
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT a.id AS artifact_id,a.source_id,a.bundle_path,"
                "a.current_digest,a.present,v.file_count,v.byte_count,"
                "v.manifest_json "
                "FROM artifacts AS a JOIN artifact_versions AS v "
                "ON v.artifact_id=a.id AND v.digest=a.current_digest "
                "WHERE a.id=?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise StoreError("artifact_not_found")
            if row["current_digest"] != content_digest:
                raise StoreError("stale_artifact")

            result = _artifact_fields(row)
            result["manifest"] = json.loads(row["manifest_json"])
            versions = connection.execute(
                "SELECT digest,first_seen_at,file_count,byte_count,manifest_json "
                "FROM artifact_versions WHERE artifact_id=? "
                "ORDER BY first_seen_at,digest",
                (artifact_id,),
            ).fetchall()
            result["versions"] = []
            for version in versions:
                value = {
                    "digest": version["digest"],
                    "first_seen_at": version["first_seen_at"],
                    "file_count": version["file_count"],
                    "byte_count": version["byte_count"],
                    "manifest": json.loads(version["manifest_json"]),
                }
                revisions = connection.execute(
                    "SELECT r.requested_ref,r.resolved_commit,"
                    "r.first_seen_at,r.last_seen_at "
                    "FROM artifact_git_revisions AS l "
                    "JOIN git_revisions AS r ON r.id=l.git_revision_id "
                    "WHERE l.artifact_id=? AND l.digest=? "
                    "ORDER BY r.requested_ref,r.resolved_commit",
                    (artifact_id, version["digest"]),
                ).fetchall()
                if revisions:
                    value["git_revisions"] = [
                        {
                            "first_seen_at": revision["first_seen_at"],
                            "last_seen_at": revision["last_seen_at"],
                            "requested_ref": revision["requested_ref"],
                            "resolved_commit": revision["resolved_commit"],
                        }
                        for revision in revisions
                    ]
                result["versions"].append(value)
            catalog = connection.execute(
                "SELECT status,name,description,reason FROM catalog_entries "
                "WHERE artifact_id=? AND content_digest=? AND catalog_format_version=?",
                (artifact_id, row["current_digest"], CATALOG_FORMAT_VERSION),
            ).fetchone()
            if catalog is None:
                result["catalog"] = {
                    "catalog_format_version": CATALOG_FORMAT_VERSION,
                    "status": "missing",
                    "name": None,
                    "description": None,
                    "reason": "this artifact version is not in the current search index",
                }
                result["resources"] = []
                result["relationships"] = []
                if resource_path is not None:
                    raise StoreError("resource_not_found")
            else:
                result["catalog"] = {
                    "catalog_format_version": CATALOG_FORMAT_VERSION,
                    "status": catalog["status"],
                    "name": catalog["name"],
                    "description": catalog["description"],
                    "reason": catalog["reason"],
                }
                result["resources"] = [
                    {
                        "path": resource["path"],
                        "status": resource["status"],
                        "reason": resource["reason"],
                    }
                    for resource in connection.execute(
                        "SELECT path,status,reason FROM catalog_resources "
                        "WHERE artifact_id=? AND content_digest=? "
                        "AND catalog_format_version=? ORDER BY path",
                        (artifact_id, row["current_digest"], CATALOG_FORMAT_VERSION),
                    )
                ]
                result["relationships"] = [
                    {
                        "type": relationship["relationship_type"],
                        "target_name": relationship["target_name"],
                        "metadata_key": relationship["metadata_key"],
                        "evidence": {
                            "path": relationship["evidence_path"],
                            "line": relationship["evidence_line"],
                        },
                    }
                    for relationship in connection.execute(
                        "SELECT relationship_type,target_name,metadata_key,"
                        "evidence_path,evidence_line FROM catalog_relationships "
                        "WHERE artifact_id=? AND content_digest=? "
                        "AND catalog_format_version=? "
                        "ORDER BY relationship_type,target_name,metadata_key",
                        (artifact_id, row["current_digest"], CATALOG_FORMAT_VERSION),
                    )
                ]
                if resource_path is not None:
                    resource = connection.execute(
                        "SELECT path,status,reason,text FROM catalog_resources "
                        "WHERE artifact_id=? AND content_digest=? "
                        "AND catalog_format_version=? AND path=?",
                        (
                            artifact_id,
                            row["current_digest"],
                            CATALOG_FORMAT_VERSION,
                            resource_path,
                        ),
                    ).fetchone()
                    if resource is None:
                        raise StoreError("resource_not_found")
                    result["resource"] = dict(resource)
            connection.execute("COMMIT")
            return result
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def traverse(
        self,
        artifact_id: str,
        content_digest: str,
        *,
        source_ids: tuple[str, ...] | None = None,
        relationship_types: tuple[str, ...] | None = None,
        direction: str = "outbound",
        depth: int = 1,
        limit: int = _TRAVERSAL_LIMIT,
        offset: int = 0,
        view_id: str | None = None,
    ) -> dict[str, object]:
        limit, offset, view_id = _page_request(
            limit,
            offset,
            view_id,
            maximum=_TRAVERSAL_MAX_LIMIT,
            view_prefix="t1",
        )
        if type(artifact_id) is not str or not artifact_id:
            raise StoreError("invalid_request: artifact_id must be a non-empty string")
        if (
            type(content_digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", content_digest) is None
        ):
            raise StoreError("invalid_request: content_digest must be a SHA-256 digest")
        if source_ids is not None and (
            type(source_ids) is not tuple
            or any(type(source_id) is not str or not source_id for source_id in source_ids)
        ):
            raise StoreError(
                "invalid_request: source_ids must be an ordered tuple of non-empty strings"
            )
        if source_ids is not None and len(set(source_ids)) != len(source_ids):
            raise StoreError("invalid_request: source_ids must not contain duplicates")
        if relationship_types is not None and (
            type(relationship_types) is not tuple
            or any(
                type(relationship_type) is not str
                or relationship_type not in _RELATIONSHIP_TYPES
                for relationship_type in relationship_types
            )
            or len(set(relationship_types)) != len(relationship_types)
        ):
            raise StoreError("invalid_request: invalid relationship types")
        selected_types = tuple(
            relationship_type
            for relationship_type in _RELATIONSHIP_TYPES
            if relationship_types is None or relationship_type in relationship_types
        )
        if direction not in ("outbound", "inbound", "both"):
            raise StoreError("invalid_request: invalid traversal direction")
        if type(depth) is not int or not 1 <= depth <= _TRAVERSAL_MAX_DEPTH:
            raise StoreError(
                f"invalid_request: depth must be between 1 and {_TRAVERSAL_MAX_DEPTH}"
            )

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            enabled_source_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM sources WHERE status='enabled' ORDER BY rowid DESC"
                )
            ]
            scope = enabled_source_ids if source_ids is None else list(source_ids)
            unavailable = next(
                (source_id for source_id in scope if source_id not in enabled_source_ids),
                None,
            )
            if unavailable is not None:
                if view_id is not None:
                    raise StoreError("stale_view")
                raise StoreError(f"source_unavailable: {unavailable}")

            artifact = connection.execute(
                "SELECT a.current_digest,a.present,a.source_id,s.status FROM artifacts AS a "
                "JOIN sources AS s ON s.id=a.source_id WHERE a.id=?",
                (artifact_id,),
            ).fetchone()
            if artifact is None or not artifact["present"]:
                raise StoreError("artifact_not_found")
            if artifact["current_digest"] != content_digest:
                raise StoreError("stale_artifact")
            if artifact["status"] != "enabled" or artifact["source_id"] not in scope:
                if view_id is not None:
                    raise StoreError("stale_view")
                raise StoreError(
                    "source_unavailable: starting artifact's source_id is not included in source_ids"
                )

            entries: list[dict[str, object]] = []
            relationships: list[dict[str, object]] = []
            if scope:
                scope_values = ",".join("(?,?)" for _ in scope)
                scope_parameters = tuple(
                    value
                    for priority, source_id in enumerate(scope)
                    for value in (source_id, priority)
                )
                preferred_entries = (
                    f"WITH source_scope(source_id,source_priority) AS (VALUES {scope_values}), "
                    "ranked_entries AS ("
                    "SELECT c.artifact_id,c.content_digest,c.name,a.source_id,a.bundle_path,"
                    "p.source_priority,MIN(p.source_priority) OVER (PARTITION BY c.name) "
                    "AS best_source_priority FROM catalog_entries AS c JOIN artifacts AS a "
                    "ON a.id=c.artifact_id AND a.current_digest=c.content_digest "
                    "JOIN source_scope AS p ON p.source_id=a.source_id WHERE a.present=1 "
                    "AND c.catalog_format_version=? AND c.status='ready'), "
                    "preferred_entries AS (SELECT * FROM ranked_entries "
                    "WHERE source_priority=best_source_priority) "
                )
                entries = [
                    dict(row)
                    for row in connection.execute(
                        preferred_entries
                        + "SELECT artifact_id,content_digest,name,source_id,bundle_path "
                        "FROM preferred_entries ORDER BY name,artifact_id",
                        (*scope_parameters, CATALOG_FORMAT_VERSION),
                    )
                ]
                relationships = [
                    dict(row)
                    for row in connection.execute(
                        preferred_entries
                        + "SELECT r.artifact_id,r.content_digest,r.relationship_type,"
                        "r.target_name,r.metadata_key,r.evidence_path,r.evidence_line "
                        "FROM catalog_relationships AS r JOIN preferred_entries AS p "
                        "ON p.artifact_id=r.artifact_id AND p.content_digest=r.content_digest "
                        "WHERE r.catalog_format_version=? ORDER BY r.artifact_id,"
                        "r.relationship_type,r.target_name,r.metadata_key,r.evidence_path,"
                        "r.evidence_line",
                        (
                            *scope_parameters,
                            CATALOG_FORMAT_VERSION,
                            CATALOG_FORMAT_VERSION,
                        ),
                    )
                ]

            entries_by_key = {
                (str(entry["artifact_id"]), str(entry["content_digest"])): entry
                for entry in entries
            }
            root_key = (artifact_id, content_digest)
            root = entries_by_key.get(root_key)
            if root is None:
                root_row = connection.execute(
                    "SELECT c.artifact_id,c.content_digest,c.name,a.source_id,a.bundle_path "
                    "FROM catalog_entries AS c JOIN artifacts AS a ON a.id=c.artifact_id "
                    "AND a.current_digest=c.content_digest WHERE c.artifact_id=? "
                    "AND c.content_digest=? AND c.catalog_format_version=? "
                    "AND c.status='ready' AND a.present=1",
                    (artifact_id, content_digest, CATALOG_FORMAT_VERSION),
                ).fetchone()
                if root_row is None:
                    if view_id is not None:
                        raise StoreError("stale_view")
                    raise StoreError("artifact_not_found")
                root = dict(root_row)
                entries_by_key[root_key] = root
                relationships.extend(
                    dict(row)
                    for row in connection.execute(
                        "SELECT artifact_id,content_digest,relationship_type,target_name,"
                        "metadata_key,evidence_path,evidence_line "
                        "FROM catalog_relationships WHERE artifact_id=? "
                        "AND content_digest=? AND catalog_format_version=? "
                        "ORDER BY relationship_type,target_name,metadata_key,"
                        "evidence_path,evidence_line",
                        (artifact_id, content_digest, CATALOG_FORMAT_VERSION),
                    )
                )
            entries_by_name: dict[str, list[dict[str, object]]] = {}
            for entry in entries:
                entries_by_name.setdefault(str(entry["name"]), []).append(entry)
            relationships_by_key: dict[
                tuple[str, str], list[dict[str, object]]
            ] = {}
            relationships_by_target: dict[str, list[dict[str, object]]] = {}
            for relationship in relationships:
                key = (
                    str(relationship["artifact_id"]),
                    str(relationship["content_digest"]),
                )
                relationships_by_key.setdefault(key, []).append(relationship)
                relationships_by_target.setdefault(
                    str(relationship["target_name"]), []
                ).append(relationship)

            def resolve(
                relationship: dict[str, object],
            ) -> tuple[tuple[str, str] | None, dict[str, Any]]:
                targets = entries_by_name.get(str(relationship["target_name"]), [])
                if len(targets) == 1:
                    target_key = (
                        str(targets[0]["artifact_id"]),
                        str(targets[0]["content_digest"]),
                    )
                    return target_key, {
                        "status": "resolved",
                        "artifact_id": target_key[0],
                        "content_digest": target_key[1],
                    }
                if targets:
                    return None, {
                        "status": "ambiguous",
                        "candidates": sorted(
                            (
                                {
                                    "artifact_id": str(target["artifact_id"]),
                                    "content_digest": str(target["content_digest"]),
                                }
                                for target in targets
                            ),
                            key=lambda target: (
                                target["artifact_id"],
                                target["content_digest"],
                            ),
                        ),
                    }
                return None, {"status": "missing"}

            ordered: list[dict[str, Any]] = []
            seen_edges: set[tuple[object, ...]] = set()
            visited = {root_key}
            queue = [(root_key, 0)]
            cursor = 0
            while cursor < len(queue):
                current_key, current_distance = queue[cursor]
                cursor += 1
                if current_distance >= depth:
                    continue
                current = entries_by_key[current_key]
                incident: list[tuple[dict[str, object], bool]] = []
                if direction in ("outbound", "both"):
                    incident.extend(
                        (relationship, False)
                        for relationship in relationships_by_key.get(current_key, [])
                    )
                if direction in ("inbound", "both"):
                    incident.extend(
                        (relationship, True)
                        for relationship in relationships_by_target.get(
                            str(current["name"]), []
                        )
                    )
                for relationship, inbound in incident:
                    relationship_type = str(relationship["relationship_type"])
                    if relationship_type not in selected_types:
                        continue
                    declaring_key = (
                        str(relationship["artifact_id"]),
                        str(relationship["content_digest"]),
                    )
                    target_key, resolution = resolve(relationship)
                    if inbound:
                        targets = entries_by_name.get(
                            str(relationship["target_name"]), []
                        )
                        target_keys = {
                            (
                                str(target["artifact_id"]),
                                str(target["content_digest"]),
                            )
                            for target in targets
                        }
                        if current_key not in target_keys:
                            continue
                        next_key = declaring_key if target_key == current_key else None
                    else:
                        next_key = target_key
                    edge_key = (
                        relationship["artifact_id"],
                        relationship["content_digest"],
                        relationship_type,
                        relationship["target_name"],
                    )
                    if edge_key in seen_edges:
                        continue
                    seen_edges.add(edge_key)
                    declaring = entries_by_key[declaring_key]
                    ordered.append(
                        {
                            "declared_by": {
                                "artifact_id": declaring["artifact_id"],
                                "content_digest": declaring["content_digest"],
                                "name": declaring["name"],
                            },
                            "distance": current_distance + 1,
                            "evidence": {
                                "line": int(str(relationship["evidence_line"])),
                                "path": str(relationship["evidence_path"]),
                            },
                            "metadata_key": str(relationship["metadata_key"]),
                            "resolution": resolution,
                            "target_name": str(relationship["target_name"]),
                            "type": relationship_type,
                        }
                    )
                    if next_key is not None and next_key not in visited:
                        visited.add(next_key)
                        queue.append((next_key, current_distance + 1))

            ordered.sort(
                key=lambda relationship: (
                    relationship["distance"],
                    relationship["declared_by"]["artifact_id"],
                    relationship["declared_by"]["content_digest"],
                    relationship["type"],
                    relationship["target_name"],
                    relationship["metadata_key"],
                    relationship["evidence"]["path"],
                    relationship["evidence"]["line"],
                    relationship["resolution"].get("artifact_id", ""),
                    relationship["resolution"].get("content_digest", ""),
                )
            )
            current_view_id = _view_id(
                "t1",
                {
                    "catalog_format_version": CATALOG_FORMAT_VERSION,
                    "depth": depth,
                    "direction": direction,
                    "kind": "traverse",
                    "relationship_types": selected_types,
                    "root": root_key,
                    "source_scope": scope,
                    "state": _exploration_state(connection, scope),
                },
            )
            if view_id is not None and view_id != current_view_id:
                raise StoreError("stale_view")
            page = ordered[offset : offset + limit]
            returned = len(page)
            has_more = offset + returned < len(ordered)
            result = {
                "catalog_format_version": CATALOG_FORMAT_VERSION,
                "depth": depth,
                "direction": direction,
                "has_more": has_more,
                "limit": limit,
                "next_offset": offset + returned if has_more else None,
                "offset": offset,
                "relationship_types": list(selected_types),
                "relationships": page,
                "returned": returned,
                "root": {
                    "artifact_id": root["artifact_id"],
                    "bundle_path": root["bundle_path"],
                    "content_digest": root["content_digest"],
                    "name": root["name"],
                    "source_id": root["source_id"],
                },
                "source_scope": scope,
                "view_id": current_view_id,
            }
            connection.execute("COMMIT")
            return result
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def discover(
        self,
        query: str,
        *,
        source_ids: tuple[str, ...] | None = None,
        limit: int = _DISCOVERY_LIMIT,
        offset: int = 0,
        view_id: str | None = None,
    ) -> dict[str, object]:
        tokens = query_tokens(query)
        normalized_query = " ".join(tokens)
        limit, offset, view_id = _page_request(
            limit,
            offset,
            view_id,
            maximum=_DISCOVERY_MAX_LIMIT,
            view_prefix="d1",
        )
        if source_ids is not None and (
            type(source_ids) is not tuple
            or any(type(source_id) is not str or not source_id for source_id in source_ids)
        ):
            raise StoreError(
                "invalid_request: source_ids must be an ordered tuple of non-empty strings"
            )
        if source_ids is not None and len(set(source_ids)) != len(source_ids):
            raise StoreError("invalid_request: source_ids must not contain duplicates")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            enabled_source_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM sources WHERE status='enabled' ORDER BY rowid DESC"
                )
            ]
            scope = enabled_source_ids if source_ids is None else list(source_ids)
            enabled = set(enabled_source_ids)
            unavailable = next(
                (source_id for source_id in scope if source_id not in enabled), None
            )
            if unavailable is not None:
                if view_id is not None:
                    raise StoreError("stale_view")
                raise StoreError(f"source_unavailable: {unavailable}")

            coverage_counts = {"ready": 0, "invalid": 0, "missing": 0}
            entries: list[dict[str, object]] = []
            resources: list[dict[str, object]] = []
            relationships: list[dict[str, object]] = []
            candidate_rows: list[sqlite3.Row | dict[str, object]] = []
            semantic_scores: dict[str, float] = {}
            fusion_scores: dict[str, dict[str, int | float | None]] = {}
            retrieval = {
                "method": "sqlite_fts5_bm25",
            }
            if scope:
                # Keep source priority in the query that uses it.
                scope_values = ",".join("(?,?)" for _ in scope)
                scope_parameters = tuple(
                    value
                    for priority, source_id in enumerate(scope)
                    for value in (source_id, priority)
                )
                coverage_rows = connection.execute(
                    f"WITH source_scope(source_id,source_priority) AS (VALUES {scope_values}) "
                    "SELECT c.status FROM artifacts AS a "
                    "JOIN source_scope AS p ON p.source_id=a.source_id "
                    "LEFT JOIN catalog_entries AS c "
                    "ON c.artifact_id=a.id AND c.content_digest=a.current_digest "
                    "AND c.catalog_format_version=? "
                    "WHERE a.present=1 ORDER BY a.id",
                    (*scope_parameters, CATALOG_FORMAT_VERSION),
                ).fetchall()
                for row in coverage_rows:
                    status = (
                        row["status"]
                        if row["status"] in ("ready", "invalid")
                        else "missing"
                    )
                    coverage_counts[status] += 1

                preferred_entries = (
                    f"WITH source_scope(source_id,source_priority) AS (VALUES {scope_values}), "
                    "ranked_entries AS ("
                    "SELECT c.artifact_id,c.content_digest,c.name,c.description,c.skill_text,"
                    "a.source_id,a.bundle_path,p.source_priority,"
                    "MIN(p.source_priority) OVER (PARTITION BY c.name) "
                    "AS best_source_priority "
                    "FROM catalog_entries AS c JOIN artifacts AS a "
                    "ON a.id=c.artifact_id AND a.current_digest=c.content_digest "
                    "JOIN source_scope AS p ON p.source_id=a.source_id "
                    "WHERE a.present=1 AND c.catalog_format_version=? "
                    "AND c.status='ready'), "
                    "preferred_entries AS (SELECT * FROM ranked_entries "
                    "WHERE source_priority=best_source_priority), "
                    "search_entries AS (SELECT * FROM (SELECT *,ROW_NUMBER() OVER ("
                    "PARTITION BY name ORDER BY bundle_path,artifact_id) AS name_row "
                    "FROM preferred_entries) WHERE name_row=1) "
                )
                entries = [
                    dict(row)
                    for row in connection.execute(
                        preferred_entries
                        + "SELECT artifact_id,content_digest,name,description,skill_text,"
                        "source_id,bundle_path,source_priority "
                        "FROM preferred_entries ORDER BY name,artifact_id",
                        (*scope_parameters, CATALOG_FORMAT_VERSION),
                    )
                ]
                semantic_rows = connection.execute(
                    preferred_entries
                    + "SELECT v.artifact_id,v.content_digest,v.projection_digest,v.vector,"
                    "p.name,p.description,p.skill_text,p.source_priority "
                    "FROM catalog_semantic_vectors AS v JOIN search_entries AS p "
                    "ON p.artifact_id=v.artifact_id AND p.content_digest=v.content_digest "
                    "WHERE v.catalog_format_version=? AND v.model_key=? "
                    "ORDER BY p.name,p.artifact_id",
                    (
                        *scope_parameters,
                        CATALOG_FORMAT_VERSION,
                        CATALOG_FORMAT_VERSION,
                        semantic.MODEL_KEY,
                    ),
                ).fetchall()
                semantic_complete = len(semantic_rows) == len(
                    {str(entry["name"]) for entry in entries}
                ) and all(
                    row["projection_digest"]
                    == semantic.projection_digest(
                        semantic.projection(
                            str(row["name"]),
                            str(row["description"]),
                            str(row["skill_text"]),
                        )
                    )
                    for row in semantic_rows
                )
                query_vector = (
                    _semantic_query_vector(normalized_query)
                    if semantic_rows and semantic_complete
                    else None
                )
                semantic_candidates = []
                if query_vector is not None:
                    try:
                        scored = []
                        for row in semantic_rows:
                            similarity = semantic.similarity(
                                query_vector, bytes(row["vector"])
                            )
                            if not math.isfinite(similarity):
                                raise semantic.SemanticUnavailable(
                                    "semantic scoring returned an invalid value"
                                )
                            scored.append(
                                (
                                    0
                                    if str(row["name"]).casefold()
                                    == normalized_query
                                    else 1,
                                    -similarity,
                                    int(row["source_priority"]),
                                    str(row["name"]),
                                    str(row["artifact_id"]),
                                    str(row["content_digest"]),
                                )
                            )
                    except semantic.SemanticUnavailable:
                        scored = []
                    scored = [
                        item
                        for item in scored
                        if item[0] == 0
                        or -item[1] >= semantic.MINIMUM_SIMILARITY
                    ]
                    if scored:
                        scored.sort()
                        selected = []
                        selected_names: set[str] = set()
                        for item in scored:
                            name = item[3]
                            if name in selected_names:
                                continue
                            selected.append(item)
                            selected_names.add(name)
                        semantic_candidates = selected
                search_entries_by_name: dict[str, dict[str, object]] = {}
                for entry in sorted(
                    entries,
                    key=lambda item: (
                        str(item["name"]),
                        str(item["bundle_path"]),
                        str(item["artifact_id"]),
                    ),
                ):
                    search_entries_by_name.setdefault(str(entry["name"]), entry)
                lexical_entries = {
                    str(entry["artifact_id"]): entry
                    for entry in search_entries_by_name.values()
                }
                lexical_rows: list[sqlite3.Row] = []
                if lexical_entries:
                    use_persistent_fts = tuple(scope) == tuple(enabled_source_ids)
                    fts_table = "catalog_search_fts"
                    if not use_persistent_fts:
                        fts_table = "scoped_catalog_fts"
                        connection.execute(
                            "CREATE VIRTUAL TABLE temp.scoped_catalog_fts USING fts5("
                            "artifact_id UNINDEXED,content_digest UNINDEXED,"
                            "catalog_format_version UNINDEXED,path_key UNINDEXED,"
                            "path,body,"
                            "tokenize='unicode61 remove_diacritics 0 categories "
                            "''L* N*'' tokenchars ''_''')"
                        )
                        connection.execute(
                            preferred_entries
                            + "INSERT INTO temp.scoped_catalog_fts("
                            "artifact_id,content_digest,catalog_format_version,"
                            "path_key,path,body) "
                            "SELECT f.artifact_id,f.content_digest,"
                            "f.catalog_format_version,f.path_key,f.path,f.body "
                            "FROM catalog_fts AS f JOIN search_entries AS p "
                            "ON p.artifact_id=f.artifact_id "
                            "AND p.content_digest=f.content_digest "
                            "WHERE CAST(f.catalog_format_version AS INTEGER)=?",
                            (
                                *scope_parameters,
                                CATALOG_FORMAT_VERSION,
                                CATALOG_FORMAT_VERSION,
                            ),
                        )
                    artifact_versions = tuple(
                        (artifact_id, str(entry["content_digest"]))
                        for artifact_id, entry in lexical_entries.items()
                    )
                    artifact_placeholders = ",".join(
                        "(?,?)" for _ in artifact_versions
                    )
                    lexical_sql = (
                        "SELECT artifact_id,content_digest,path_key,"
                        f"bm25({fts_table},0.0,0.0,0.0,0.0,3.0,1.0) AS rank "
                        f"FROM {fts_table} "
                        "WHERE CAST(catalog_format_version AS INTEGER)=? "
                        f"AND (artifact_id,content_digest) IN ({artifact_placeholders}) "
                        f"AND {fts_table} MATCH ? "
                        "ORDER BY rank,artifact_id,path_key"
                    )
                    lexical_parameters = (
                        CATALOG_FORMAT_VERSION,
                        *(
                            value
                            for artifact_version in artifact_versions
                            for value in artifact_version
                        ),
                        fts_query(tokens),
                    )
                    # Run the limited query first. If it exceeds _LEXICAL_MATCH_LIMIT,
                    # rerun without a limit so pagination cannot omit matches.
                    raw_lexical_rows = connection.execute(
                        lexical_sql + " LIMIT ?",
                        (*lexical_parameters, _LEXICAL_MATCH_LIMIT + 1),
                    ).fetchall()
                    if len(raw_lexical_rows) > _LEXICAL_MATCH_LIMIT:
                        raw_lexical_rows = connection.execute(
                            lexical_sql, lexical_parameters
                        ).fetchall()
                    best_lexical_rows: dict[str, sqlite3.Row] = {}
                    for row in raw_lexical_rows:
                        best_lexical_rows.setdefault(str(row["artifact_id"]), row)
                    lexical_rows = sorted(
                        best_lexical_rows.values(),
                        key=lambda row: (
                            0
                            if str(
                                lexical_entries[str(row["artifact_id"])]["name"]
                            )
                            == normalized_query
                            else 1,
                            float(row["rank"]),
                            int(
                                str(
                                    lexical_entries[str(row["artifact_id"])][
                                        "source_priority"
                                    ]
                                )
                            ),
                            str(
                                lexical_entries[str(row["artifact_id"])]["name"]
                            ),
                            str(row["artifact_id"]),
                        ),
                    )
                if semantic_candidates:
                    lexical_ranks = {
                        str(row["artifact_id"]): rank
                        for rank, row in enumerate(lexical_rows, 1)
                    }
                    semantic_ranks = {
                        artifact: rank
                        for rank, (_, _, _, _, artifact, _) in enumerate(
                            semantic_candidates, 1
                        )
                    }
                    lexical_by_artifact = {
                        str(row["artifact_id"]): row for row in lexical_rows
                    }
                    semantic_by_artifact = {
                        artifact: item
                        for item in semantic_candidates
                        for artifact in (item[4],)
                    }
                    entries_by_artifact = {
                        str(entry["artifact_id"]): entry for entry in entries
                    }
                    fused = []
                    for artifact in lexical_ranks.keys() | semantic_ranks.keys():
                        lexical_rank = lexical_ranks.get(artifact)
                        semantic_rank = semantic_ranks.get(artifact)
                        fused_score = sum(
                            1 / (_RRF_RANK_CONSTANT + rank)
                            for rank in (lexical_rank, semantic_rank)
                            if rank is not None
                        )
                        entry = entries_by_artifact[artifact]
                        row = lexical_by_artifact.get(artifact)
                        if row is None:
                            semantic_row = semantic_by_artifact[artifact]
                            row = {
                                "artifact_id": artifact,
                                "content_digest": semantic_row[5],
                                "path_key": "__semantic__",
                            }
                        fused.append(
                            (
                                0
                                if str(entry["name"]).casefold()
                                == normalized_query
                                else 1,
                                -fused_score,
                                int(str(entry["source_priority"])),
                                str(entry["name"]),
                                artifact,
                                row,
                                lexical_rank,
                                semantic_rank,
                                fused_score,
                            )
                        )
                    fused.sort(key=lambda item: item[:5])
                    selected_names: set[str] = set()
                    semantic_score_by_artifact = {
                        artifact: -negative_score
                        for _, negative_score, _, _, artifact, _ in semantic_candidates
                    }
                    for item in fused:
                        name, artifact, row = item[3], item[4], item[5]
                        if name in selected_names:
                            continue
                        selected_names.add(name)
                        candidate_rows.append(row)
                        fusion_scores[artifact] = {
                            "fused_score": round(item[8], 9),
                            "lexical_rank": item[6],
                            "semantic_rank": item[7],
                        }
                        if artifact in semantic_score_by_artifact:
                            semantic_scores[artifact] = semantic_score_by_artifact[
                                artifact
                            ]
                    retrieval = {
                        "method": "sqlite_fts5_bm25_local_semantic_rrf",
                        "model": semantic.MODEL_KEY,
                        "overfetch": max(len(lexical_rows), len(semantic_candidates)),
                        "rank_constant": _RRF_RANK_CONSTANT,
                    }
                else:
                    candidate_rows = list(lexical_rows)
                candidate_rows = candidate_rows[: offset + limit + 1]
                candidate_ids = [str(row["artifact_id"]) for row in candidate_rows]
                candidate_id_set = set(candidate_ids)
                semantic_scores = {
                    artifact: score
                    for artifact, score in semantic_scores.items()
                    if artifact in candidate_id_set
                }
                fusion_scores = {
                    artifact: score
                    for artifact, score in fusion_scores.items()
                    if artifact in candidate_id_set
                }
                if candidate_ids:
                    candidate_placeholders = ",".join("?" for _ in candidate_ids)
                    resources = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT r.artifact_id,r.content_digest,r.path,r.text "
                            "FROM catalog_resources AS r JOIN artifacts AS a "
                            "ON a.id=r.artifact_id AND a.current_digest=r.content_digest "
                            f"WHERE r.artifact_id IN ({candidate_placeholders}) "
                            "AND r.catalog_format_version=? AND r.status='text' "
                            "ORDER BY r.artifact_id,r.path",
                            (*candidate_ids, CATALOG_FORMAT_VERSION),
                        )
                    ]
                relationships = [
                    dict(row)
                    for row in connection.execute(
                        preferred_entries
                        + "SELECT r.artifact_id,r.content_digest,r.relationship_type,"
                        "r.target_name,r.metadata_key,r.evidence_path,r.evidence_line "
                        "FROM catalog_relationships AS r JOIN preferred_entries AS p "
                        "ON p.artifact_id=r.artifact_id AND p.content_digest=r.content_digest "
                        "WHERE r.catalog_format_version=? "
                        "ORDER BY r.artifact_id,r.relationship_type,r.target_name",
                        (
                            *scope_parameters,
                            CATALOG_FORMAT_VERSION,
                            CATALOG_FORMAT_VERSION,
                        ),
                    )
                ]
            coverage: dict[str, dict[str, object]] = {
                status: {"count": count} for status, count in coverage_counts.items()
            }
            matched_resources = {
                (str(row["artifact_id"]), str(row["content_digest"]), str(row["path_key"]))
                for row in candidate_rows
            }
            candidate_order = {
                str(row["artifact_id"]): index for index, row in enumerate(candidate_rows)
            }
            result = build_discovery(
                query,
                tokens,
                coverage,
                entries,
                resources,
                matched_resources,
                relationships,
                candidate_order,
                semantic_scores,
                fusion_scores,
            )
            current_view_id = _view_id(
                "d1",
                {
                    "catalog_format_version": CATALOG_FORMAT_VERSION,
                    "kind": "discover",
                    "query_terms": list(tokens),
                    "store_schema_version": SCHEMA_VERSION,
                    "ranking": {
                        "minimum_similarity": semantic.MINIMUM_SIMILARITY,
                        "model": semantic.MODEL_KEY,
                        "rank_constant": _RRF_RANK_CONSTANT,
                        "retrieval": retrieval,
                    },
                    "source_scope": scope,
                    "semantic_state": _semantic_state(connection, scope),
                    "state": _exploration_state(connection, scope),
                },
            )
            if view_id is not None and view_id != current_view_id:
                raise StoreError("stale_view")
            candidates = result["candidates"]
            assert isinstance(candidates, list)
            page = candidates[offset : offset + limit]
            returned = len(page)
            has_more = offset + returned < len(candidates)
            result["candidates"] = page
            result["retrieval"] = {
                **retrieval,
                "has_more": has_more,
                "limit": limit,
                "next_offset": offset + returned if has_more else None,
                "offset": offset,
                "returned": returned,
                "view_id": current_view_id,
            }
            result["source_scope"] = scope
            connection.execute("COMMIT")
            return result
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def _rebuild_catalog_search_fts(connection: sqlite3.Connection) -> None:
    # Rebuild after writes so search contains only current catalog entries.
    connection.execute("DELETE FROM catalog_search_fts")
    connection.execute(
        "WITH ranked_entries AS ("
        "SELECT c.artifact_id,c.content_digest,c.name,a.bundle_path,s.rowid AS source_rowid,"
        "MAX(s.rowid) OVER (PARTITION BY c.name) AS preferred_source_rowid "
        "FROM catalog_entries AS c JOIN artifacts AS a "
        "ON a.id=c.artifact_id AND a.current_digest=c.content_digest "
        "JOIN sources AS s ON s.id=a.source_id WHERE s.status='enabled' AND a.present=1 "
        "AND c.catalog_format_version=? AND c.status='ready'), "
        "preferred_entries AS (SELECT * FROM ranked_entries "
        "WHERE source_rowid=preferred_source_rowid), "
        "search_entries AS (SELECT * FROM (SELECT *,ROW_NUMBER() OVER ("
        "PARTITION BY name ORDER BY bundle_path,artifact_id) AS name_row "
        "FROM preferred_entries) WHERE name_row=1) "
        "INSERT INTO catalog_search_fts(artifact_id,content_digest,"
        "catalog_format_version,path_key,path,body) "
        "SELECT f.artifact_id,f.content_digest,f.catalog_format_version,"
        "f.path_key,f.path,f.body FROM catalog_fts AS f JOIN search_entries AS p "
        "ON p.artifact_id=f.artifact_id AND p.content_digest=f.content_digest "
        "WHERE CAST(f.catalog_format_version AS INTEGER)=?",
        (CATALOG_FORMAT_VERSION, CATALOG_FORMAT_VERSION),
    )
    connection.execute(
        "INSERT INTO catalog_search_fts(catalog_search_fts) VALUES('integrity-check')"
    )


def _publish_catalog(
    connection: sqlite3.Connection, observation: ArtifactObservation
) -> None:
    catalog = observation.catalog
    digest = observation.manifest.digest
    exists = connection.execute(
        "SELECT 1 FROM catalog_entries "
        "WHERE artifact_id=? AND content_digest=? AND catalog_format_version=?",
        (observation.artifact_id, digest, CATALOG_FORMAT_VERSION),
    ).fetchone()
    if exists is not None:
        return
    connection.execute(
        "INSERT INTO catalog_entries("
        "artifact_id,content_digest,catalog_format_version,status,name,description,"
        "raw_frontmatter,skill_text,reason) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            observation.artifact_id,
            digest,
            CATALOG_FORMAT_VERSION,
            catalog.status,
            catalog.name,
            catalog.description,
            catalog.raw_frontmatter,
            catalog.skill_text,
            catalog.reason,
        ),
    )
    for resource in catalog.resources:
        connection.execute(
            "INSERT INTO catalog_resources("
            "artifact_id,content_digest,catalog_format_version,path,status,reason,text) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                observation.artifact_id,
                digest,
                CATALOG_FORMAT_VERSION,
                resource.path,
                resource.status,
                resource.reason,
                resource.text,
            ),
        )
        if catalog.status == "ready" and resource.status == "text":
            assert resource.text is not None
            search_text = resource.text
            if resource.path == "SKILL.md":
                assert catalog.name is not None and catalog.description is not None
                search_text = (
                    f"{skill_markdown_body(resource.text)}\n"
                    f"{catalog.name}\n{catalog.description}"
                )
            connection.execute(
                "INSERT INTO catalog_fts("
                "artifact_id,content_digest,catalog_format_version,path_key,path,body) "
                "VALUES(?,?,?,?,?,?)",
                (
                    observation.artifact_id,
                    digest,
                    CATALOG_FORMAT_VERSION,
                    resource.path,
                    resource.path.casefold(),
                    search_text.casefold(),
                ),
            )
    for relationship in catalog.relationships:
        connection.execute(
            "INSERT INTO catalog_relationships("
            "artifact_id,content_digest,catalog_format_version,relationship_type,"
            "target_name,metadata_key,evidence_path,evidence_line) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                observation.artifact_id,
                digest,
                CATALOG_FORMAT_VERSION,
                relationship.kind,
                relationship.target_name,
                relationship.metadata_key,
                "SKILL.md",
                relationship.evidence_line,
            ),
        )


def _require_fts5(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.capalith_fts5_probe USING fts5(value)"
        )
        connection.execute("DROP TABLE temp.capalith_fts5_probe")
    except sqlite3.Error as error:
        raise StoreError("SQLite FTS5 is required") from error


def _canonical_manifest_json(manifest: BundleManifest) -> str:
    return json.dumps(
        {
            "digest": manifest.digest,
            "byte_count": manifest.byte_count,
            "files": [
                {
                    "path": file.path,
                    "executable": file.executable,
                    "size": file.size,
                    "sha256": file.sha256,
                }
                for file in manifest.files
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _artifact_fields(row: sqlite3.Row) -> dict[str, object]:
    return {
        "artifact_id": row["artifact_id"],
        "source_id": row["source_id"],
        "bundle_path": row["bundle_path"],
        "current_digest": row["current_digest"],
        "present": bool(row["present"]),
        "file_count": row["file_count"],
        "byte_count": row["byte_count"],
    }


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
