from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import importlib
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

from capalith import __version__
from capalith.settings import SettingsError, defaults, load_settings, settings_operation

_DEFAULT = defaults()["semantic"]
MODEL_ID = _DEFAULT["model"]["id"]
MODEL_ARTIFACT = _DEFAULT["model"]["artifact"]
MODEL_REVISION = _DEFAULT["model"]["revision"]
MODEL_KEY = f"{MODEL_ARTIFACT}@{MODEL_REVISION}"
DIMENSIONS = _DEFAULT["model"]["dimensions"]
VECTOR_BYTES = DIMENSIONS * 4
MINIMUM_SIMILARITY = defaults()["search"]["minimum_similarity"]
MODEL_FILES = _DEFAULT["model"]["files"]


class SemanticUnavailable(Exception):
    pass


class VectorIndex:
    """Exact local dot products over the stored float32 vectors.

    Accumulate as float64, like the old scalar path, without a Python loop
    over vector components. The immutable matrix can be reused until indexing
    changes the catalog; this class owns no storage or refresh policy.
    """

    def __init__(self, vectors: Sequence[bytes]) -> None:
        import numpy as np

        size = dimensions()
        if any(len(vector) != size * 4 for vector in vectors):
            raise SemanticUnavailable("stored semantic vector has an invalid size")
        self._matrix = np.frombuffer(b"".join(vectors), dtype="<f4").reshape(
            len(vectors), size
        )
        if not np.isfinite(self._matrix).all():
            raise SemanticUnavailable("stored semantic vector is not finite")

    @property
    def nbytes(self) -> int:
        return self._matrix.nbytes

    def scores(self, query: bytes):
        import numpy as np

        if len(query) != self._matrix.shape[1] * 4:
            raise SemanticUnavailable("semantic query has an invalid size")
        vector = np.frombuffer(query, dtype="<f4")
        if not np.isfinite(vector).all():
            raise SemanticUnavailable("semantic query is not finite")
        return np.einsum("ij,j->i", self._matrix, vector, dtype=np.float64)


def dimensions() -> int:
    return load_settings()["semantic"]["model"]["dimensions"]


def model_key() -> str:
    config = load_settings()["semantic"]
    identity = {
        "model": {k: v for k, v in config["model"].items() if k != "dir"},
        "projection": config["projection"],
    }
    # Keep existing default vectors usable; every non-default projection/model
    # gets its own fingerprint and cannot be mistaken for the shipped index.
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    compatibility = defaults()["compatibility"]
    if fingerprint == compatibility["semantic_fingerprint"]:
        return compatibility["semantic_key"]
    return f"{config['model']['artifact']}@{config['model']['revision']}#{fingerprint}"


def model_directory() -> Path:
    config = load_settings()["semantic"]["model"]
    configured = os.environ.get("CAPALITH_SEMANTIC_MODEL") or config["dir"]
    if configured:
        return Path(os.path.abspath(Path(configured).expanduser()))
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return (
        cache
        / "capalith"
        / "models"
        / config["id"].rsplit("/", 1)[-1]
        / config["revision"]
    )


def projection(name: str, description: str, skill_text: str) -> str:
    from capalith.catalog import skill_markdown_body

    size = load_settings()["semantic"]["projection"]["body_chars"]
    return f"{name}\n{description}\n{skill_markdown_body(skill_text)[:size]}"


def projection_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _verify_model_directory(directory: Path) -> Path:
    for relative, expected in load_settings()["semantic"]["model"]["files"].items():
        path = directory / relative
        if not path.is_file():
            raise SemanticUnavailable(
                f"semantic model file failed verification: {relative}"
            )
        with path.open("rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        if actual != expected:
            raise SemanticUnavailable(
                f"semantic model file failed verification: {relative}"
            )
    return directory


def _build_model(directory: str, identity: str, name: str, threads: int):
    verified = _verify_model_directory(Path(directory))
    try:
        TextEmbedding = importlib.import_module("fastembed").TextEmbedding

        return TextEmbedding(
            model_name=name,
            specific_model_path=str(verified),
            local_files_only=True,
            providers=["CPUExecutionProvider"],
            threads=threads,
        )
    except Exception as error:
        raise SemanticUnavailable("local semantic model is unavailable") from error


# The loaded model, kept for `semantic.idle_release_seconds` after its last use and then dropped (about 115 MB per idle
# process). One lock covers the cache, a call's use of the model and the release, so a search never loses its model
# mid-call and a release never takes it from a call in progress; a search that arrives after a release builds it again.
_cache_lock = threading.RLock()
_cache: tuple[tuple[str, str, str, int], object] | None = None
_last_used = 0.0
_idle_seconds = 0.0            # the setting as the last call saw it: the only policy a release looks at
_timer: threading.Timer | None = None
_timer_generation = 0          # a timer that is not the current one (re-armed or cancelled since) does nothing


def _model_is_loaded() -> bool:
    return _cache is not None


def _cancel_timer() -> None:
    """Called with the lock held."""
    global _timer, _timer_generation
    _timer_generation += 1
    if _timer is not None:
        _timer.cancel()
    _timer = None


def _reset_model_cache() -> None:
    global _cache
    with _cache_lock:
        _cancel_timer()
        _cache = None


def _give_memory_back() -> None:
    """Best effort: a failure here never reaches a search (the model is already dropped)."""
    try:
        gc.collect()
        import ctypes          # glibc only: other platforms keep what the allocator holds

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:          # noqa: BLE001 - optional cleanup
        pass


def _release_if_idle(generation: int) -> None:
    global _cache, _timer
    if not _cache_lock.acquire(blocking=False):
        return                 # a call holds the model: its end starts the idle clock again
    try:
        if generation != _timer_generation or _idle_seconds <= 0:
            return             # stale (re-armed, cancelled, or the setting is now off): not this timer's decision
        remaining = _last_used + _idle_seconds - time.monotonic()
        if remaining > 0.001:
            _arm(remaining)
            return
        _timer, _cache = None, None
    finally:
        _cache_lock.release()
    _give_memory_back()


def _arm(delay: float) -> None:
    """Called with the lock held."""
    global _timer
    _cancel_timer()
    _timer = threading.Timer(delay, _release_if_idle, (_timer_generation,))
    _timer.daemon = True       # never keeps a command-line process alive
    _timer.start()


@contextlib.contextmanager
def _model_in_use():
    """The configured model, loaded on demand and held for the whole call; the idle clock restarts when the call ends."""
    global _cache, _last_used, _idle_seconds
    config = load_settings()["semantic"]
    if not config["enabled"]:
        raise SemanticUnavailable("local semantic search is disabled")
    key = (str(model_directory()), model_key(), config["model"]["id"], config["threads"])
    seconds = config["idle_release_seconds"]
    with _cache_lock:
        try:
            if _cache is None or _cache[0] != key:
                _cache = (key, _build_model(*key))
            yield _cache[1]
        finally:
            # Also when building a replacement failed: a timer that found this call busy relies on this end to start the
            # idle clock again, so the model still held (an older one) has an expiry.
            _last_used = time.monotonic()
            _idle_seconds = seconds
            if seconds > 0 and _cache is not None:
                _arm(seconds)
            else:
                _cancel_timer()    # off now (an earlier timer must not release the model), or nothing held


def _blob(values: Iterable[float]) -> bytes:
    vector = tuple(float(value) for value in values)
    size = dimensions()
    if len(vector) != size or not all(math.isfinite(value) for value in vector):
        raise SemanticUnavailable("semantic model returned an invalid vector")
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0:
        raise SemanticUnavailable("semantic model returned an invalid vector")
    return struct.pack(f"<{size}f", *(value / norm for value in vector))


@settings_operation
def embed_documents(documents: Sequence[str]) -> tuple[bytes, ...]:
    try:
        with _model_in_use() as model:
            values = tuple(
                _blob(vector)
                for vector in model.passage_embed(
                    documents, batch_size=load_settings()["semantic"]["batch_size"]
                )
            )
    except SemanticUnavailable:
        raise
    except Exception as error:
        raise SemanticUnavailable("local semantic indexing failed") from error
    if len(values) != len(documents):
        raise SemanticUnavailable(
            "local semantic indexing returned the wrong vector count"
        )
    return values


@settings_operation
def embed_query(query: str) -> bytes:
    try:
        with _model_in_use() as model:
            values = tuple(model.query_embed(query))
        if len(values) != 1:
            raise SemanticUnavailable(
                "local semantic query returned the wrong vector count"
            )
        return _blob(values[0])
    except SemanticUnavailable:
        raise
    except Exception as error:
        raise SemanticUnavailable("local semantic query failed") from error


def similarity(left: bytes, right: bytes) -> float:
    size = dimensions()
    if len(left) != size * 4 or len(right) != size * 4:
        raise SemanticUnavailable("stored semantic vector has an invalid size")
    left_values = struct.unpack(f"<{size}f", left)
    right_values = struct.unpack(f"<{size}f", right)
    return math.fsum(a * b for a, b in zip(left_values, right_values, strict=True))


def provision_notice() -> str:
    """What provisioning is about to download, how much, from where and to where (said before it starts)."""
    semantic = load_settings()["semantic"]
    size = f"about {round(semantic['download_bytes'] / 1e6)} MB" if semantic["download_bytes"] else "an unknown size"
    return (f"Downloading the search model {semantic['model']['id']} ({size}, {len(semantic['model']['files'])} files) "
            f"from Hugging Face ({semantic['model']['artifact']}) into {model_directory()}. "
            "It needs the network once; a message about unauthenticated requests is harmless.")


def provision_model(announce: Callable[[str], None] | None = None) -> Path:
    target = model_directory()
    try:
        return _verify_model_directory(target)
    except SemanticUnavailable:
        if target.exists():
            raise
    if announce is not None:
        announce(provision_notice())
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix="capalith-model-", dir=target.parent))
    try:
        snapshot_download = importlib.import_module("huggingface_hub").snapshot_download

        snapshot_download(
            repo_id=load_settings()["semantic"]["model"]["artifact"],
            revision=load_settings()["semantic"]["model"]["revision"],
            local_dir=staged,
            allow_patterns=list(load_settings()["semantic"]["model"]["files"]),
        )
        _verify_model_directory(staged)
        os.replace(staged, target)
        return target
    except SemanticUnavailable:
        raise
    except Exception as error:
        raise SemanticUnavailable("could not install semantic model") from error
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m capalith.semantic",
        description="Manage the local semantic-search model.",
    )
    parser.add_argument("--version", action="version", version=f"capalith-model {__version__}")
    parser.add_argument(
        "command",
        choices=("provision", "verify"),
        help="provision downloads and verifies the pinned model; verify checks local files",
    )
    arguments = parser.parse_args(argv)
    try:
        path = (
            provision_model(announce=lambda text: print(text, file=sys.stderr))
            if arguments.command == "provision"
            else _verify_model_directory(model_directory())
        )
    except SettingsError as error:
        print(str(error))
        return 2
    except SemanticUnavailable as error:
        print(str(error))
        return 1
    print(
        json.dumps(
            {"model": model_key(), "path": str(path)},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
