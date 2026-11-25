"""Load only the parent's Capalith package without adding its containing folder.

Run by absolute filename under -P. Explicit absolute host PYTHONPATH roots stay
trusted inputs; merely locating this package grants no sibling import access.
"""
import importlib.util
import runpy
import sys
from pathlib import Path


if __name__ == "__main__":
    package = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "capalith", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["capalith"] = module
    spec.loader.exec_module(module)
    runpy.run_module("capalith.ranking", run_name="__main__")
