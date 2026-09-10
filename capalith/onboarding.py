from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Sequence

import yaml

from capalith.intake import scan_git_source, scan_local_source
from capalith.store import Store, StoreError


def discover_sources(
    workspace: Path | None = None, host_homes: dict[str, Path] | None = None,
    extra_paths: Sequence[Path] = (),
) -> dict[str, object]:
    """Probe host skill locations, not the disk or other Hermes profiles."""
    home = Path.home()
    homes = {
        host: Path((host_homes or {}).get(host) or os.environ.get(variable) or home / f".{host}").expanduser()
        for host, variable in (("hermes", "HERMES_HOME"), ("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"))
    }
    hermes, claude, codex = homes["hermes"], homes["claude"], homes["codex"]
    current = (workspace or Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())).expanduser().resolve(strict=True)
    candidates: list[tuple[Path, str]] = []
    warnings: list[str] = []
    repository = next((p for p in (current, *current.parents) if (p / ".git").exists()), None)
    directory = current
    while True:
        candidates.extend([(directory / ".agents/skills", "workspace:agents"),
                           (directory / ".claude/skills", "workspace:claude")])
        if repository is None or directory == repository:
            break
        directory = directory.parent
    if repository is not None:
        candidates.append((repository / ".hermes/skills", "workspace:hermes"))
    candidates.extend([(home / ".agents/skills", "user:agents"),
                       (codex / "skills", "user:codex-legacy"),
                       (claude / "skills", "user:claude"), (hermes / "skills", "profile:hermes")])
    config = hermes / "config.yaml"
    try:
        # Read only skill-location settings, never credentials or other profiles.
        parent = Store(config)._open_parent()
        try:
            descriptor = os.open(config.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("configuration must be a regular file")
            # ponytail: bounded config read; raise explicitly if larger configs are needed.
            data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("configuration too large")
        settings = yaml.safe_load(data) or {}
        skills = settings.get("skills", {})
        external = skills.get("external_dirs", [])
        external = [external] if isinstance(external, str) else external
        if not isinstance(external, list) or any(not isinstance(p, str) for p in external):
            raise ValueError("invalid external directories")
        create = skills.get("create_dir") or ""
        if not isinstance(create, str):
            raise ValueError("invalid creation directory")
        for value, origin in [(create, "profile:hermes-create"),
                              *((p, "profile:hermes-external") for p in external)]:
            if value.strip():
                path = Path(os.path.expandvars(value.strip())).expanduser()
                candidates.append((path if path.is_absolute() else hermes / path, origin))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        warnings.append("hermes_config_unavailable")
    candidates.extend((path, "extra") for path in extra_paths)
    sources: dict[Path, dict[str, object]] = {}
    for supplied, origin in candidates:
        try:
            if not supplied.is_absolute():
                raise ValueError("host directories must be absolute")
            root = supplied.resolve(strict=True)
            if not root.is_dir():
                raise ValueError("skill source must be a directory")
            roots = [root]
            # Resolve installed category/skill links, never links inside a bundle.
            # Intake still owns no-follow observation and rejects bundle links.
            visited: set[Path] = set()
            for directory, dirs, filenames in os.walk(root, followlinks=True):
                path = Path(directory)
                resolved = path.resolve(strict=True)
                if resolved in visited:
                    dirs.clear()
                    continue
                visited.add(resolved)
                if path.is_symlink():
                    roots.append(resolved)
                dirs[:] = [] if "SKILL.md" in filenames or "SKILL.md" in dirs else sorted(
                    name for name in dirs if not name.startswith(".")
                )
            for resolved in roots:
                entry = sources.setdefault(resolved, {"path": str(resolved), "origins": []})
                origins = entry["origins"]
                assert isinstance(origins, list)
                if origin not in origins:
                    origins.append(origin)
        except FileNotFoundError:
            if origin == "extra":
                warnings.append("source_unavailable:extra")
            continue
        except (OSError, ValueError, RuntimeError):
            warnings.append(f"source_unavailable:{origin}")
    return {"sources": list(sources.values()), "warnings": sorted(set(warnings)),
            "workspace": str(current), "host_homes": {host: str(path) for host, path in homes.items()},
            "scope": "host_directories_not_active_host_inventory"}


def prepare_catalog(
    roots: Sequence[Path] = (), git_sources: Sequence[Sequence[str]] = (),
) -> Path:
    """Prepare only explicitly selected sources before serving read-only calls."""
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    if not data_home.is_absolute():
        raise StoreError("XDG_DATA_HOME must be an absolute path")
    catalogs = data_home / "capalith" / "catalogs"
    validator = Store(catalogs / "catalog.sqlite3")
    selected: list[tuple[str, str, str | None]] = [
        ("local", str(validator.validate_local_source(root.expanduser())), None)
        for root in roots
    ]
    for locator, requested_ref in git_sources:
        validator.validate_git_source(locator, requested_ref)
        selected.append(("git", locator, requested_ref))
    selected = list(dict.fromkeys(selected))
    if not selected:
        raise StoreError("select at least one skill source")
    identity = hashlib.sha256(json.dumps(selected).encode()).hexdigest()
    database = catalogs / f"{identity}.sqlite3"
    store = Store(database)
    # Validate the final destination before creating any directories or files.
    for kind, locator, _ in selected:
        if kind == "local":
            store.validate_local_source(Path(locator))
    parent_descriptor = store._open_parent(create=True)
    try:
        descriptor = os.open(
            database.with_suffix(".lock").name,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600,
            dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)
    try:
        # The same selected sources share a catalog across clients, not profiles.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        store.initialize()
        existing = store.list_sources()
        registered = {(s.kind, s.locator, s.requested_ref): s for s in existing}
        if (
            len(registered) != len(existing)
            or not registered.keys() <= set(selected)
            or any(s.status != "enabled" for s in existing)
        ):
            raise StoreError("managed sources changed; use --db for manual catalog control")
        # Existing precedence is newest registration first. First source wins.
        for kind, locator, requested_ref in reversed(selected):
            source = registered.get((kind, locator, requested_ref))
            if kind == "local":
                source = source or store.add_local_source(Path(locator))
                scan_local_source(store, source.id)
            else:
                assert requested_ref is not None
                source = source or store.add_git_source(locator, requested_ref)
                scan_git_source(store, source.id)
    finally:
        os.close(descriptor)
    return database
