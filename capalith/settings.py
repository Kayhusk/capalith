"""User-owned settings, independent of the project being searched.

Packaged defaults are the single source for retrieval/model values. Settings
are never accepted from a skill, project file or tool argument. Callers treat
the cached result as immutable; optional ranking validates its own overrides.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache, wraps
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import string
import tomllib
from typing import Any

from capalith.textsafe import is_safe, visible


class SettingsError(ValueError):
    pass


RANKING_KEYS = frozenset({
    "model", "timeout_seconds", "shortlist", "body_excerpt_chars", "none_threshold",
    "cache_views", "cache_max_age_seconds", "cache_dir", "enable_env", "key_env",
    "transport", "instructions", "endpoint", "wide_candidates",
})


_active_settings: ContextVar[dict[str, Any] | None] = ContextVar(
    "capalith_settings", default=None
)


@contextmanager
def settings_snapshot():
    """One coherent user configuration for an entire search or index batch."""
    active = _active_settings.get()
    token = _active_settings.set(load_settings()) if active is None else None
    try:
        yield
    finally:
        if token is not None:
            _active_settings.reset(token)


def settings_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with settings_snapshot():
            return function(*args, **kwargs)

    return wrapped


@lru_cache(maxsize=1)
def defaults() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("settings-defaults.json").read_text())


@lru_cache(maxsize=1)
def profiles() -> dict[str, Any]:
    """Selectable embedding models. The default profile is the model in the packaged defaults."""
    return json.loads(Path(__file__).with_name("model-profiles.json").read_text())


def _apply_profile(config: dict[str, Any], supplied: dict[str, Any]) -> None:
    """A chosen profile replaces the model identity and its gate before the user's own keys apply."""
    semantic = supplied.get("semantic")
    name = semantic.get("profile") if isinstance(semantic, dict) else None
    if name is None or name == config["semantic"]["profile"]:
        return
    if type(name) is not str or name not in profiles():
        raise SettingsError("settings_error: unknown semantic.profile")
    entry = profiles()[name]
    model = config["semantic"]["model"]
    for key in ("id", "artifact", "revision", "dimensions"):
        model[key] = entry["model"][key]
    model["files"] = dict(entry["model"]["files"])
    config["search"]["minimum_similarity"] = entry["minimum_similarity"]
    config["semantic"]["download_bytes"] = entry["download_bytes"]


def settings_path() -> tuple[Path, bool]:
    explicit = os.environ.get("CAPALITH_CONFIG")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise SettingsError("settings_error: config path must be absolute")
        return path, True
    directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not directory.is_absolute():
        raise SettingsError("settings_error: XDG_CONFIG_HOME must be absolute")
    return directory / "capalith/settings.toml", False


def _untrusted(kind: str, path: Path, why: str, fix: str | None) -> SettingsError:
    """Which path, why it is refused, and the one command that fixes it (the check itself is never loosened).

    The path is shown with control characters as code points, the command is shell-quoted, and a path that
    holds control characters is offered no command at all.
    """
    if fix and not is_safe(path):
        hint = "The path holds control characters, so no command is offered: rename it, then repeat."
    elif fix:
        hint = f"Fix: {fix.format(path=shlex.quote(str(path)))}"
    else:
        hint = "Use a plain file or folder that you own, or point CAPALITH_CONFIG at one."
    return SettingsError(f"settings_untrusted: {kind}: {visible(path)} {why}. {hint}")


def load_settings_file(
    path: Path, *, required: bool = True, format: str = "toml"
) -> dict[str, Any]:
    # Reuse the store's descriptor-anchored no-follow traversal. The import is
    # deferred so the store can itself use settings without an import cycle.
    from capalith.store import Store, StoreError

    try:
        parent = Store(path)._open_parent()
        try:
            directory = os.fstat(parent)
            if directory.st_uid != os.geteuid():
                raise _untrusted("directory", path.parent, "belongs to another user", None)
            if directory.st_mode & 0o022:
                raise _untrusted("directory", path.parent, "can be written by group or others", "chmod go-w {path}")
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
        finally:
            os.close(parent)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise _untrusted("file", path, "is not a plain file", None)
            if info.st_nlink != 1:
                raise _untrusted("file", path, "has more than one hard link", None)
            if info.st_uid != os.geteuid():
                raise _untrusted("file", path, "belongs to another user", None)
            if info.st_mode & 0o022:
                raise _untrusted("file", path, "can be written by group or others", "chmod go-w {path}")
            raw = stream.read(65536 + 1)
        if len(raw) > 65536:
            raise SettingsError("settings_error: file too large")
        return (
            json.loads(raw.decode("utf-8"))
            if format == "json"
            else tomllib.loads(raw.decode("utf-8"))
        )
    except FileNotFoundError:
        if not required:
            return {}
        raise SettingsError("settings_error: file missing") from None
    except SettingsError:
        raise
    except (OSError, StoreError):
        raise _untrusted("file", path, "could not be opened safely (a link in its path, or it is unreadable)", None) from None
    except (UnicodeError, tomllib.TOMLDecodeError, json.JSONDecodeError) as error:
        # Parser diagnostics may echo source text; never put a value in errors.
        line = getattr(error, "lineno", None)
        suffix = f" at line {line}" if line is not None else ""
        raise SettingsError("settings_error: invalid TOML" + suffix) from None


def _merge(target: dict, supplied: dict, prefix: str = "") -> None:
    for key, value in supplied.items():
        address = prefix + key
        if key == "compatibility" and not prefix:
            raise SettingsError("settings_error: compatibility is a catalog contract")
        if key == "ranking" and not prefix:
            if not isinstance(value, dict):
                raise SettingsError("settings_error: ranking must be a table")
            if set(value) - RANKING_KEYS:
                raise SettingsError("settings_error: unknown ranking key")
            target[key] = value
            continue
        if key not in target:
            raise SettingsError(f"settings_error: unknown key {address}")
        current = target[key]
        if address == "semantic.model.files":
            if type(value) is not dict or not value:
                raise SettingsError(
                    f"settings_error: {address} must contain file hashes"
                )
            for name, digest in value.items():
                parts = PurePosixPath(name)
                if (
                    not name
                    or parts.is_absolute()
                    or ".." in parts.parts
                    or "\\" in name
                    or "\x00" in name
                    or type(digest) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", digest)
                ):
                    raise SettingsError(f"settings_error: invalid {address}")
            target[key] = value
        elif type(current) is dict:
            if type(value) is not dict:
                raise SettingsError(f"settings_error: {address} must be a table")
            _merge(current, value, address + ".")
        elif type(current) is float:
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise SettingsError(f"settings_error: invalid {address}")
            target[key] = float(value)
        elif type(value) is not type(current):
            raise SettingsError(f"settings_error: invalid type for {address}")
        else:
            target[key] = value


def checked_folder(value: object, address: str) -> Path:
    """A folder named in a user-owned file: absolute, no parent steps, no NUL byte, a known `~user`.

    The one rule for storage.data_dir, semantic.model.dir and ranking.cache_dir. Errors name the
    setting, never its value.
    """
    if type(value) is not str or not value or "\x00" in value:
        raise SettingsError(f"settings_error: invalid {address}")
    try:
        path = Path(value).expanduser()
    except RuntimeError:  # an unknown ~user
        raise SettingsError(f"settings_error: invalid {address}") from None
    if not path.is_absolute():
        raise SettingsError(f"settings_error: {address} must be absolute")
    if ".." in path.parts:
        raise SettingsError(f"settings_error: {address} must not contain ..")
    return path


def _validate(config: dict) -> None:
    if config["version"] != 1:
        raise SettingsError("settings_error: unsupported version")
    for table in ("search", "traversal"):
        values = config[table]
        for key in ("default_limit", "max_limit"):
            if values[key] < 1:
                raise SettingsError(f"settings_error: {table}.{key} must be positive")
        if values["default_limit"] > values["max_limit"]:
            raise SettingsError(
                f"settings_error: {table}.default_limit exceeds maximum"
            )
    search = config["search"]
    if not 0 <= search["minimum_similarity"] <= 1:
        raise SettingsError("settings_error: search.minimum_similarity outside range")
    for key in ("lexical_candidates", "rrf_k", "excerpt_chars"):
        if search[key] < 1:
            raise SettingsError(f"settings_error: search.{key} must be positive")
    for key in ("cached_catalogs", "cache_max_bytes"):
        if search[key] < 0:
            raise SettingsError(f"settings_error: search.{key} cannot be negative")
    if search["operator"] not in ("OR", "AND"):
        raise SettingsError("settings_error: search.operator must be OR or AND")
    if any(
        type(word) is not str or not re.fullmatch(r"\w+", word)
        for word in search["stopwords"]
    ):
        raise SettingsError("settings_error: search.stopwords must contain words")
    if not any(search["fusion_weights"].values()):
        raise SettingsError("settings_error: fusion weights cannot all be zero")
    semantic = config["semantic"]
    for key in ("threads", "batch_size"):
        if semantic[key] < 1:
            raise SettingsError(f"settings_error: semantic.{key} must be positive")
    if (
        semantic["projection"]["body_chars"] < 0
        or semantic["projection"]["version"] < 1
    ):
        raise SettingsError("settings_error: invalid semantic.projection")
    if type(semantic["download_bytes"]) is not int or semantic["download_bytes"] < 0:
        raise SettingsError("settings_error: semantic.download_bytes must be a whole number of bytes")
    idle = semantic["idle_release_seconds"]
    if type(idle) not in (int, float) or not math.isfinite(idle) or not 0 <= idle <= 86400:
        raise SettingsError("settings_error: semantic.idle_release_seconds must be 0 (never) or a number of seconds up to 86400")
    if semantic["model"]["dimensions"] < 1:
        raise SettingsError(
            "settings_error: semantic.model.dimensions must be positive"
        )
    if semantic["model"]["dir"]:
        checked_folder(semantic["model"]["dir"], "semantic.model.dir")
    if config["traversal"]["max_depth"] < 1:
        raise SettingsError("settings_error: traversal.max_depth must be positive")
    for key in ("id", "artifact", "revision"):
        value = semantic["model"][key]
        parts = value.split("/")
        if (not value or any(part in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts)
                or (key == "revision" and len(parts) != 1)):
            raise SettingsError(f"settings_error: invalid semantic.model.{key}")
    storage = config["storage"]
    if storage["data_dir"]:
        checked_folder(storage["data_dir"], "storage.data_dir")
    usage = config["usage"]
    if usage["dir"]:
        checked_folder(usage["dir"], "usage.dir")
    if not 1 <= usage["retention_days"] <= 3650:
        raise SettingsError("settings_error: usage.retention_days must be from 1 to 3650")
    if "cache_dir" in config["ranking"]:
        checked_folder(config["ranking"]["cache_dir"], "ranking.cache_dir")
    # SQLite's busy handler accepts milliseconds in a signed 32-bit integer.
    if storage["busy_timeout_seconds"] * 1000 > 2147483647:
        raise SettingsError("settings_error: storage.busy_timeout_seconds outside SQLite range")
    for key, value in config["git"].items():
        if value <= 0:
            raise SettingsError(f"settings_error: git.{key} must be positive")
    sources = config["sources"]
    if sources["hermes_config_max_bytes"] < 1:
        raise SettingsError("settings_error: sources.hermes_config_max_bytes must be positive")
    def environment_name(value, address):
        if value and not re.fullmatch(r"[A-Za-z_]\w*", value):
            raise SettingsError(f"settings_error: invalid {address}")
    def paths(values, address, *, relative=False):
        for value in values:
            if type(value) is not str or not value or "\x00" in value:
                raise SettingsError(f"settings_error: invalid {address}")
            try:
                path = Path(value).expanduser()
            except RuntimeError:  # an unknown ~user
                raise SettingsError(f"settings_error: invalid {address}") from None
            if (relative and (path.is_absolute() or ".." in path.parts)) or (not relative and not path.is_absolute()):
                raise SettingsError(f"settings_error: invalid path in {address}")
    environment_name(sources["workspace_env"], "sources.workspace_env")
    paths(sources["extra"], "sources.extra")
    paths(sources["shared_user_dirs"], "sources.shared_user_dirs")
    paths(sources["shared_project_dirs"], "sources.shared_project_dirs", relative=True)
    for name, host in sources["hosts"].items():
        prefix = f"sources.hosts.{name}"
        environment_name(host["home_env"], prefix + ".home_env")
        paths([host["home_default"]], prefix + ".home_default")
        for key in ("user_dirs", "project_dirs"):
            paths(host[key], prefix + "." + key, relative=True)
    _validate_answer(config["answer"])


_ANSWER_FIELDS = {"name", "nearest", "signal", "sent", "total"}


def _validate_answer(answer: dict) -> None:
    """The agent-facing answer: shape, wording and the redirect switch."""
    if answer["detail"] not in ("short", "full"):
        raise SettingsError("settings_error: answer.detail must be short or full")
    for key, minimum in (("default_limit", 1), ("summary_chars", 20), ("max_matched_words", 0), ("max_redirects", 0), ("max_chars", 0)):
        if answer[key] < minimum:
            raise SettingsError(f"settings_error: answer.{key} must be at least {minimum}")
    if not 0 <= answer["close_margin"] <= 1:
        raise SettingsError("settings_error: answer.close_margin outside range")
    for key in ("refuse_below", "good_from"):
        if type(answer[key]) is not float or not 0 <= answer[key] <= 1:
            raise SettingsError(f"settings_error: answer.{key} outside range")
    if any(type(word) is not str or not re.fullmatch(r"\w+", word) for word in answer["hide_words"]):
        raise SettingsError("settings_error: answer.hide_words must contain words")
    for key, text in answer["signals"].items():
        if type(text) is not str or not text:
            raise SettingsError(f"settings_error: answer.signals.{key} must be text")
        if "{" in text or "}" in text:   # a signal is a plain sentence: no placeholder, no braces
            raise SettingsError(f"settings_error: invalid answer.signals.{key}")
    for key, text in answer["messages"].items():
        if type(text) is not str or not text:
            raise SettingsError(f"settings_error: answer.messages.{key} must be text")
        try:
            fields = list(string.Formatter().parse(text))
        except ValueError:
            raise SettingsError(f"settings_error: invalid answer.messages.{key}") from None
        # Only plain {name}, {nearest} and {signal}: no attribute, index, conversion or format spec.
        if any(
            field is not None and (field not in _ANSWER_FIELDS or conversion or spec)
            for _, field, spec, conversion in fields
        ):
            raise SettingsError(f"settings_error: invalid answer.messages.{key}")


@lru_cache(maxsize=8)
def _load(path: str, required: bool, signature: tuple) -> dict[str, Any]:
    config = deepcopy(defaults())
    config["ranking"] = {}
    supplied = load_settings_file(Path(path), required=required)
    _apply_profile(config, supplied)
    _merge(config, supplied)
    _validate(config)
    return config


def load_settings() -> dict[str, Any]:
    active = _active_settings.get()
    if active is not None:
        return active
    path, required = settings_path()
    try:
        signature = tuple(
            (
                s.st_dev,
                s.st_ino,
                s.st_mtime_ns,
                s.st_ctime_ns,
                s.st_size,
                s.st_mode,
                s.st_uid,
                s.st_nlink,
            )
            for s in (path.lstat(), path.parent.lstat())
        )
    except FileNotFoundError:
        signature = ()
    except OSError:
        raise _untrusted("file", path, "could not be read safely (a link in its path, or it is unreadable)", None) from None
    return _load(str(path), required, signature)
