"""Build a reproducible synthetic search catalog, without private skill text.

Vectors are seeded synthetic unit vectors, not model embeddings. This fixture
measures retrieval cost and memory, not model quality or indexing throughput.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import resource


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
    import numpy as np
    from capalith import semantic
    from capalith.catalog import CATALOG_FORMAT_VERSION
    from capalith.store import Store

    if args.database.exists():
        parser.error("database exists; preserve the fixture")
    store = Store(args.database.resolve())
    store.initialize()
    source_root = args.database.resolve().parent / "synthetic-source"
    source_root.mkdir(exist_ok=True)
    source = store.add_local_source(source_root)
    connection = store._connect()
    rng = np.random.default_rng(48271)
    topics = ["database migration indexes", "network retry deadlines", "layout browser screenshot", "security authentication review", "module interface design"]
    try:
        # Store connections use isolation_level=None; the context manager alone
        # does not start a transaction. Match native scan's explicit transaction
        # rather than fsyncing each synthetic row as its own transaction.
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            for start in range(0, args.count, 1000):
                vectors = rng.normal(size=(min(1000,args.count-start),semantic.DIMENSIONS)).astype("<f4")
                vectors /= np.linalg.norm(vectors, axis=1)[:,None]
                for offset, vector in enumerate(vectors):
                    index = start + offset
                    name = f"synthetic-{index:06d}"
                    description = f"Help with {topics[index % len(topics)]}; task family {index}."
                    text = f"---\nname: {name}\ndescription: {description}\n---\n# Instructions\n{description}\n"
                    artifact = "art_" + hashlib.sha256(name.encode()).hexdigest()
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    connection.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)", (artifact,source.id,name,digest,1,"synthetic"))
                    connection.execute("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?)", (artifact,digest,"{}",1,len(text),"synthetic"))
                    connection.execute("INSERT INTO catalog_entries VALUES(?,?,?,?,?,?,?,?,?)", (artifact,digest,CATALOG_FORMAT_VERSION,"ready",name,description,"",text,None))
                    connection.execute("INSERT INTO catalog_resources VALUES(?,?,?,?,?,?,?)", (artifact,digest,CATALOG_FORMAT_VERSION,"SKILL.md","text",None,text))
                    projection = semantic.projection(name,description,text)
                    connection.execute("INSERT INTO catalog_semantic_vectors VALUES(?,?,?,?,?,?,?)", (artifact,digest,CATALOG_FORMAT_VERSION,semantic.MODEL_KEY,semantic.projection_digest(projection),semantic.DIMENSIONS,vector.tobytes()))
                    connection.execute("INSERT INTO catalog_fts VALUES(?,?,?,?,?,?)", (artifact,digest,CATALOG_FORMAT_VERSION,"SKILL.md","SKILL.md",text))
            # This fixture has one enabled source and unique current names.
            # Its search projection is therefore exactly the complete file
            # projection. Avoid timing source-priority reconstruction as setup.
            connection.execute("INSERT INTO catalog_search_fts SELECT * FROM catalog_fts")
    finally:
        connection.close()
    print(json.dumps({"skills":args.count,"bytes":args.database.stat().st_size,"peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))


if __name__ == "__main__":
    main()
