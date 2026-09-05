from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import shutil
import struct
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

MODEL_ID = "BAAI/bge-small-en-v1.5"
MODEL_ARTIFACT = "Qdrant/bge-small-en-v1.5-onnx-Q"
MODEL_REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"
MODEL_KEY = f"{MODEL_ARTIFACT}@{MODEL_REVISION}"
DIMENSIONS = 384
VECTOR_BYTES = DIMENSIONS * 4
MINIMUM_SIMILARITY = 0.66
MODEL_FILES = {
    "README.md": "3bf78bc7cb0def03b30a901331fa50ace3ddf7d6e411d7089a8a2db64f066f03",
    "config.json": "13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd",
    "model_optimized.onnx": "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431",
    "ort_config.json": "99881c45e073696289224931dd48694398bc6bcd1fe7cb7018bca1e0cc00e1fc",
    "special_tokens_map.json": "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    "tokenizer_config.json": "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53",
    "vocab.txt": "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
}


class SemanticUnavailable(Exception):
    pass


def model_directory() -> Path:
    configured = os.environ.get("CAPALITH_SEMANTIC_MODEL")
    if configured:
        return Path(os.path.abspath(Path(configured).expanduser()))
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "capalith" / "models" / "bge-small-en-v1.5" / MODEL_REVISION


def projection(name: str, description: str, skill_text: str) -> str:
    from capalith.catalog import skill_markdown_body

    return f"{name}\n{description}\n{skill_markdown_body(skill_text)[:64]}"


def projection_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _verify_model_directory(directory: Path) -> Path:
    for relative, expected in MODEL_FILES.items():
        path = directory / relative
        if not path.is_file():
            raise SemanticUnavailable(f"semantic model file failed verification: {relative}")
        with path.open("rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        if actual != expected:
            raise SemanticUnavailable(f"semantic model file failed verification: {relative}")
    return directory


@lru_cache(maxsize=1)
def _model(directory: str):
    verified = _verify_model_directory(Path(directory))
    try:
        TextEmbedding = importlib.import_module("fastembed").TextEmbedding

        return TextEmbedding(
            model_name=MODEL_ID,
            specific_model_path=str(verified),
            local_files_only=True,
            providers=["CPUExecutionProvider"],
            threads=1,
        )
    except Exception as error:
        raise SemanticUnavailable("local semantic model is unavailable") from error


def _blob(values: Iterable[float]) -> bytes:
    vector = tuple(float(value) for value in values)
    if len(vector) != DIMENSIONS or not all(math.isfinite(value) for value in vector):
        raise SemanticUnavailable("semantic model returned an invalid vector")
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0:
        raise SemanticUnavailable("semantic model returned an invalid vector")
    return struct.pack(f"<{DIMENSIONS}f", *(value / norm for value in vector))


def embed_documents(documents: Sequence[str]) -> tuple[bytes, ...]:
    try:
        values = tuple(
            _blob(vector)
            for vector in _model(str(model_directory())).passage_embed(
                documents, batch_size=1
            )
        )
    except SemanticUnavailable:
        raise
    except Exception as error:
        raise SemanticUnavailable("local semantic indexing failed") from error
    if len(values) != len(documents):
        raise SemanticUnavailable("local semantic indexing returned the wrong vector count")
    return values


def embed_query(query: str) -> bytes:
    try:
        values = tuple(_model(str(model_directory())).query_embed(query))
        if len(values) != 1:
            raise SemanticUnavailable("local semantic query returned the wrong vector count")
        return _blob(values[0])
    except SemanticUnavailable:
        raise
    except Exception as error:
        raise SemanticUnavailable("local semantic query failed") from error


def similarity(left: bytes, right: bytes) -> float:
    if len(left) != VECTOR_BYTES or len(right) != VECTOR_BYTES:
        raise SemanticUnavailable("stored semantic vector has an invalid size")
    left_values = struct.unpack(f"<{DIMENSIONS}f", left)
    right_values = struct.unpack(f"<{DIMENSIONS}f", right)
    return math.fsum(a * b for a, b in zip(left_values, right_values, strict=True))


def provision_model() -> Path:
    target = model_directory()
    try:
        return _verify_model_directory(target)
    except SemanticUnavailable:
        if target.exists():
            raise
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix="capalith-model-", dir=target.parent))
    try:
        snapshot_download = importlib.import_module(
            "huggingface_hub"
        ).snapshot_download

        snapshot_download(
            repo_id=MODEL_ARTIFACT,
            revision=MODEL_REVISION,
            local_dir=staged,
            allow_patterns=list(MODEL_FILES),
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
    parser.add_argument(
        "command",
        choices=("provision", "verify"),
        help="provision downloads and verifies the pinned model; verify checks local files",
    )
    arguments = parser.parse_args(argv)
    try:
        path = provision_model() if arguments.command == "provision" else _verify_model_directory(model_directory())
    except SemanticUnavailable as error:
        print(str(error))
        return 1
    print(json.dumps({"model": MODEL_KEY, "path": str(path)}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
