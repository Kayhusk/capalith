from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass

import yaml
from yaml.nodes import MappingNode, ScalarNode

from capalith.identity import BundleManifest, FileRecord

CATALOG_FORMAT_VERSION = 1
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RELATIONSHIP_KEYS = {
    "capalith.requires": "requires",
    "capalith.complements": "complements",
    "capalith.alternatives": "alternatives",
    "capalith.conflicts": "conflicts",
    "capalith.supersedes": "supersedes",
}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW


class CatalogError(Exception):
    pass


@dataclass(frozen=True)
class CatalogResource:
    path: str
    status: str
    reason: str | None
    text: str | None


@dataclass(frozen=True)
class CatalogRelationship:
    kind: str
    target_name: str
    metadata_key: str
    evidence_line: int


@dataclass(frozen=True)
class CatalogObservation:
    status: str
    name: str | None
    description: str | None
    raw_frontmatter: str | None
    skill_text: str | None
    resources: tuple[CatalogResource, ...]
    relationships: tuple[CatalogRelationship, ...]
    reason: str | None = None


def extract_catalog(
    bundle_descriptor: int, manifest: BundleManifest
) -> CatalogObservation:
    resources = tuple(
        _resource(file, _read_manifest_file(bundle_descriptor, file))
        for file in manifest.files
    )
    skill = next((resource for resource in resources if resource.path == "SKILL.md"), None)
    if skill is None or skill.text is None:
        return _invalid(resources, "invalid_skill_text")

    parsed = _parse_frontmatter(skill.text)
    if parsed is None:
        return _invalid(resources, "invalid_frontmatter", skill.text)
    frontmatter, raw, evidence_lines = parsed
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    metadata = frontmatter.get("metadata", {})
    if (
        type(name) is not str
        or not _NAME.fullmatch(name)
        or len(name) > 64
        or type(description) is not str
        or not description.strip()
        or type(metadata) is not dict
    ):
        return _invalid(resources, "invalid_frontmatter", skill.text, raw)
    description = description.strip()

    relationships: list[CatalogRelationship] = []
    for metadata_key, kind in _RELATIONSHIP_KEYS.items():
        if metadata_key not in metadata:
            continue
        value = metadata[metadata_key]
        if type(value) is not str:
            return _invalid(resources, "invalid_relationship", skill.text, raw)
        targets = [target.strip() for target in value.split(",")]
        if any(not target or not _NAME.fullmatch(target) or len(target) > 64 for target in targets):
            return _invalid(resources, "invalid_relationship", skill.text, raw)
        line = evidence_lines.get(metadata_key)
        if line is None:
            return _invalid(resources, "invalid_relationship", skill.text, raw)
        for target in sorted(set(targets)):
            relationships.append(
                CatalogRelationship(kind, target, metadata_key, line)
            )

    relationships.sort(
        key=lambda item: (item.kind, item.target_name, item.metadata_key)
    )
    return CatalogObservation(
        "ready",
        name,
        description,
        raw,
        skill.text,
        resources,
        tuple(relationships),
    )


def query_tokens(query: str) -> tuple[str, ...]:
    if type(query) is not str:
        raise CatalogError("discovery query must contain a word")
    tokens = tuple(dict.fromkeys(re.findall(r"\w+", query.casefold())))
    if not tokens:
        raise CatalogError("discovery query must contain a word")
    return tokens


def fts_query(tokens: tuple[str, ...]) -> str:
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def build_discovery(
    query: str,
    tokens: tuple[str, ...],
    coverage: dict[str, dict[str, object]],
    entries: list[dict[str, object]],
    resources: list[dict[str, object]],
    matched_resources: set[tuple[str, str, str]],
    relationships: list[dict[str, object]],
    candidate_order: dict[str, int],
    semantic_scores: dict[str, float] | None = None,
    fusion_scores: dict[str, dict[str, int | float | None]] | None = None,
) -> dict[str, object]:
    semantic_scores = semantic_scores or {}
    fusion_scores = fusion_scores or {}
    entries_by_name: dict[str, list[dict[str, object]]] = {}
    entries_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for entry in entries:
        key = (str(entry["artifact_id"]), str(entry["content_digest"]))
        entries_by_key[key] = entry
        entries_by_name.setdefault(str(entry["name"]), []).append(entry)
    relationships_by_key: dict[tuple[str, str], list[dict[str, object]]] = {}
    for relationship in relationships:
        key = (
            str(relationship["artifact_id"]),
            str(relationship["content_digest"]),
        )
        relationships_by_key.setdefault(key, []).append(relationship)

    resources_by_artifact: dict[tuple[str, str], list[dict[str, object]]] = {}
    for resource in resources:
        key = (str(resource["artifact_id"]), str(resource["content_digest"]))
        resources_by_artifact.setdefault(key, []).append(resource)

    candidates: list[dict[str, object]] = []
    for entry in entries:
        artifact = str(entry["artifact_id"])
        digest = str(entry["content_digest"])
        key = (artifact, digest)
        semantic_score = semantic_scores.get(artifact)
        if semantic_score is None and not any(
            item[:2] == key for item in matched_resources
        ):
            continue
        name = str(entry["name"])
        description = str(entry["description"])
        token_set = set(tokens)
        name_hits = token_set & _word_set(name)
        description_hits = token_set & _word_set(description)
        skill_hits: set[str] = set()
        resource_path_hits: set[str] = set()
        resource_body_hits: set[str] = set()
        matches: list[dict[str, object]] = []
        for resource in sorted(
            resources_by_artifact.get(key, []), key=lambda item: str(item["path"])
        ):
            path = str(resource["path"])
            text = str(resource["text"])
            path_hits = token_set & _word_set(path)
            resource_path_hits.update(path_hits)
            if path == "SKILL.md":
                body_hits = token_set & _word_set(skill_markdown_body(text))
                skill_hits.update(body_hits)
            else:
                body_hits = token_set & _word_set(text)
                resource_body_hits.update(body_hits)
            if (artifact, digest, path) not in matched_resources:
                continue
            matched = [token for token in tokens if token in path_hits | body_hits]
            if body_hits:
                if path == "SKILL.md":
                    line, excerpt = _first_skill_body_matching_line(text, body_hits)
                else:
                    line, excerpt = _first_matching_line(text, body_hits)
                field = "body"
            elif path == "SKILL.md" and name_hits:
                line = _frontmatter_key_line(text, "name")
                excerpt, field = name, "name"
                matched = [token for token in tokens if token in name_hits]
            elif path == "SKILL.md" and description_hits:
                line = _frontmatter_key_line(text, "description")
                excerpt, field = description, "description"
                matched = [token for token in tokens if token in description_hits]
            else:
                line, excerpt, field = 1, path[:240], "path"
            matches.append(
                {
                    "excerpt": excerpt,
                    "field": field,
                    "line": line,
                    "matched_terms": matched,
                    "path": path,
                }
            )
        all_hits = (
            name_hits
            | description_hits
            | skill_hits
            | resource_path_hits
            | resource_body_hits
        )
        if not all_hits and semantic_score is None:
            continue
        if semantic_score is not None and not matches:
            skill_text = str(entry.get("skill_text", ""))
            matches.append(
                {
                    "excerpt": description,
                    "field": "semantic_projection",
                    "line": _frontmatter_key_line(skill_text, "description"),
                    "matched_terms": [],
                    "path": "SKILL.md",
                }
            )
        score: dict[str, int | float | None] = {
            "distinct_terms": len(all_hits),
            "name": len(name_hits),
            "description": len(description_hits),
            "skill_body": len(skill_hits),
            "resource_path": len(resource_path_hits),
            "resource_body": len(resource_body_hits),
        }
        if semantic_score is not None:
            score["semantic_similarity"] = round(semantic_score, 9)
        score.update(fusion_scores.get(artifact, {}))
        candidates.append(
            {
                "artifact_id": artifact,
                "catalog_format_version": CATALOG_FORMAT_VERSION,
                "content_digest": digest,
                "description": description,
                "matched_terms": [token for token in tokens if token in all_hits],
                "matches": matches,
                "name": name,
                "source_id": entry["source_id"],
                "bundle_path": entry["bundle_path"],
                "relationships": [
                    _relationship_evidence(relationship, entries_by_name)
                    for relationship in relationships_by_key.get(key, [])
                ],
                "score": score,
            }
        )

    candidates.sort(key=lambda item: candidate_order[str(item["artifact_id"])])
    if candidates:
        primary_key = (
            str(candidates[0]["artifact_id"]),
            str(candidates[0]["content_digest"]),
        )
        recommendation = _recommend(
            primary_key,
            entries_by_key,
            entries_by_name,
            relationships_by_key,
            coverage,
        )
    else:
        recommendation = {
            "status": "blocked" if coverage["missing"]["count"] else "not_found",
            "skills": [],
            "uncertainty": _coverage_uncertainty(coverage),
        }
    return {
        "candidates": candidates,
        "catalog_format_version": CATALOG_FORMAT_VERSION,
        "coverage": coverage,
        "query": query,
        "query_terms": list(tokens),
        "recommendation": recommendation,
    }


def _relationship_evidence(
    relationship: dict[str, object],
    entries_by_name: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    target_name = str(relationship["target_name"])
    targets = entries_by_name.get(target_name, [])
    if len(targets) == 1:
        resolution: dict[str, object] = {
            "status": "resolved",
            "artifact_id": targets[0]["artifact_id"],
            "content_digest": targets[0]["content_digest"],
        }
    elif targets:
        resolution = {
            "status": "ambiguous",
            "artifact_ids": sorted(str(target["artifact_id"]) for target in targets),
        }
    else:
        resolution = {"status": "missing"}
    return {
        "evidence": {
            "line": int(relationship["evidence_line"]),
            "path": str(relationship["evidence_path"]),
        },
        "metadata_key": str(relationship["metadata_key"]),
        "resolution": resolution,
        "target_name": target_name,
        "type": str(relationship["relationship_type"]),
    }


def _recommend(
    primary_key: tuple[str, str],
    entries_by_key: dict[tuple[str, str], dict[str, object]],
    entries_by_name: dict[str, list[dict[str, object]]],
    relationships_by_key: dict[tuple[str, str], list[dict[str, object]]],
    coverage: dict[str, dict[str, object]],
) -> dict[str, object]:
    order: list[tuple[str, str]] = []
    state: dict[tuple[str, str], int] = {}
    reasons = {primary_key: "primary search match"}
    requirements: dict[tuple[str, str], list[dict[str, object]]] = {}
    uncertainty = _coverage_uncertainty(coverage)
    blocked = bool(coverage["missing"]["count"])

    def add_uncertainty(message: str) -> None:
        nonlocal blocked
        blocked = True
        if message not in uncertainty:
            uncertainty.append(message)

    def visit(key: tuple[str, str], path: list[tuple[str, str]]) -> None:
        current_state = state.get(key, 0)
        if current_state == 2:
            return
        if current_state == 1:
            names = [str(entries_by_key[item]["name"]) for item in [*path, key]]
            add_uncertainty("requires cycle: " + " -> ".join(names))
            return
        state[key] = 1
        entry = entries_by_key[key]
        declaring_name = str(entry["name"])
        for relationship in relationships_by_key.get(key, []):
            if relationship["relationship_type"] != "requires":
                continue
            target_name = str(relationship["target_name"])
            targets = entries_by_name.get(target_name, [])
            if not targets:
                add_uncertainty(
                    f"missing required skill '{target_name}' declared by '{declaring_name}'"
                )
                continue
            if len(targets) != 1:
                add_uncertainty(
                    f"ambiguous required skill '{target_name}' declared by '{declaring_name}'"
                )
                continue
            target = targets[0]
            target_key = (
                str(target["artifact_id"]),
                str(target["content_digest"]),
            )
            requirement = {
                "declared_by": {
                    "artifact_id": entry["artifact_id"],
                    "content_digest": entry["content_digest"],
                    "name": entry["name"],
                },
                "evidence": {
                    "line": relationship["evidence_line"],
                    "path": relationship["evidence_path"],
                },
                "metadata_key": relationship["metadata_key"],
                "target_name": relationship["target_name"],
            }
            requirements.setdefault(target_key, []).append(requirement)
            if target_key not in reasons:
                reasons[target_key] = (
                    f"required by {declaring_name} via capalith.requires"
                )
            visit(target_key, [*path, key])
        state[key] = 2
        if key not in order:
            order.append(key)

    visit(primary_key, [])
    selected = set(order)
    for key in order:
        declaring_name = str(entries_by_key[key]["name"])
        for relationship in relationships_by_key.get(key, []):
            if relationship["relationship_type"] != "conflicts":
                continue
            target_name = str(relationship["target_name"])
            targets = entries_by_name.get(target_name, [])
            if len(targets) != 1:
                add_uncertainty(
                    f"unresolved conflict '{target_name}' declared by '{declaring_name}'"
                )
                continue
            target_key = (
                str(targets[0]["artifact_id"]),
                str(targets[0]["content_digest"]),
            )
            if target_key in selected:
                names = sorted((declaring_name, target_name))
                add_uncertainty(
                    f"conflict between selected skills '{names[0]}' and '{names[1]}'"
                )

    skills = []
    for key in order:
        entry = entries_by_key[key]
        skill = {
            "artifact_id": entry["artifact_id"],
            "content_digest": entry["content_digest"],
            "name": entry["name"],
            "reason": reasons[key],
            "source_id": entry["source_id"],
            "skill_path": "SKILL.md",
        }
        if key in requirements:
            skill["requirements"] = requirements[key]
        skills.append(skill)
    return {
        "status": "blocked" if blocked else "ready",
        "skills": skills,
        "uncertainty": uncertainty,
    }


def _word_set(value: str) -> set[str]:
    return set(re.findall(r"\w+", value.casefold()))


def _first_matching_line(text: str, terms: set[str]) -> tuple[int, str]:
    for line_number, line in enumerate(text.splitlines(), 1):
        if terms & _word_set(line):
            return line_number, line[:240]
    return 1, text[:240]


def _first_skill_body_matching_line(text: str, terms: set[str]) -> tuple[int, str]:
    lines = text.splitlines()
    end = next((index for index, line in enumerate(lines[1:], 1) if line == "---"), 0)
    for line_number, line in enumerate(lines[end + 1 :], end + 2):
        if terms & _word_set(line):
            return line_number, line[:240]
    return 1, ""


def _coverage_uncertainty(
    coverage: dict[str, dict[str, object]],
) -> list[str]:
    uncertainty: list[str] = []
    if coverage["missing"]["count"]:
        uncertainty.append("catalog coverage is incomplete; rescan missing artifacts")
    if coverage["invalid"]["count"]:
        uncertainty.append("invalid catalog artifacts are not searchable")
    return uncertainty


def _invalid(
    resources: tuple[CatalogResource, ...],
    reason: str,
    skill_text: str | None = None,
    raw_frontmatter: str | None = None,
) -> CatalogObservation:
    return CatalogObservation(
        "invalid",
        None,
        None,
        raw_frontmatter,
        skill_text,
        resources,
        (),
        reason,
    )


def _parse_frontmatter(
    text: str,
) -> tuple[dict[object, object], str, dict[str, int]] | None:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    end = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if end is None:
        return None
    raw = "".join(lines[1:end])
    try:
        value = yaml.safe_load(raw)
    except Exception:
        return None
    if type(value) is not dict:
        return None
    evidence_lines = _relationship_evidence_lines(raw)
    if evidence_lines is None:
        return None
    return value, raw, evidence_lines


def skill_markdown_body(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return ""
    end = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"),
        None,
    )
    return "" if end is None else "".join(lines[end + 1 :])


def _frontmatter_key_line(text: str, wanted: str) -> int:
    lines = text.splitlines(keepends=True)
    end = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if end is None:
        return 1
    try:
        root = yaml.compose("".join(lines[1:end]), Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError):
        return 1
    if isinstance(root, MappingNode):
        for key, _value in root.value:
            if isinstance(key, ScalarNode) and key.value == wanted:
                return key.start_mark.line + 2
    return 1


def _relationship_evidence_lines(raw_frontmatter: str) -> dict[str, int] | None:
    try:
        root = yaml.compose(raw_frontmatter, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError):
        return None
    if not isinstance(root, MappingNode):
        return None
    root_keys = [key.value for key, _value in root.value if isinstance(key, ScalarNode)]
    if len(root_keys) != len(set(root_keys)):
        return None
    metadata_nodes = [
        value
        for key, value in root.value
        if isinstance(key, ScalarNode) and key.value == "metadata"
    ]
    if not metadata_nodes:
        return {}
    if len(metadata_nodes) != 1 or not isinstance(metadata_nodes[0], MappingNode):
        return None
    lines: dict[str, int] = {}
    metadata_keys: set[str] = set()
    for key, _value in metadata_nodes[0].value:
        if not isinstance(key, ScalarNode):
            continue
        if key.value in metadata_keys:
            return None
        metadata_keys.add(key.value)
        if key.value in _RELATIONSHIP_KEYS:
            lines[key.value] = key.start_mark.line + 2
    return lines


def _resource(file: FileRecord, content: bytes) -> CatalogResource:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return CatalogResource(file.path, "non_text", "not_utf8_text", None)
    if "\0" in text:
        return CatalogResource(file.path, "non_text", "not_utf8_text", None)
    return CatalogResource(file.path, "text", None, text)


def _read_manifest_file(bundle_descriptor: int, file: FileRecord) -> bytes:
    descriptors: list[int] = []
    parent = bundle_descriptor
    try:
        parts = file.path.split("/")
        for part in parts[:-1]:
            parent = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
            descriptors.append(parent)
        before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise CatalogError("catalog bytes changed during scan")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        final = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        content = b"".join(chunks)
        if (
            _metadata(before) != _metadata(opened)
            or _metadata(opened) != _metadata(after)
            or _metadata(after) != _metadata(final)
            or file.size != len(content)
            or file.executable != bool(after.st_mode & 0o111)
            or file.sha256 != f"sha256:{hashlib.sha256(content).hexdigest()}"
        ):
            raise CatalogError("catalog bytes changed during scan")
        return content
    except CatalogError:
        raise
    except OSError as error:
        raise CatalogError("catalog bytes changed during scan") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _metadata(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode & 0o111,
    )
