from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from capalith.store import Store


def make_skill(root: Path, name: str = "demo", resource: bytes = b"v1") -> Path:
    bundle = root / name
    references = bundle / "references"
    references.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill.\n---\n\n# Test\n",
        encoding="utf-8",
    )
    (references / "data.bin").write_bytes(resource)
    return bundle


def current_artifact(
    store: "Store", artifact_id: object, resource_path: str | None = None
) -> dict[str, Any]:
    identifier = str(artifact_id)
    artifact = next(
        item for item in store.list_artifacts() if item["artifact_id"] == identifier
    )
    return store.get_artifact(
        identifier,
        str(artifact["current_digest"]),
        resource_path,
    )
