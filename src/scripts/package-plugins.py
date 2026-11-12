"""Build a first-party plugin distribution without the unselected implementations."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from backend.plugins.catalog import CATALOG


def package_plugins(root: Path, selected: str) -> list[str]:
    ids = set(CATALOG) if selected == "all" else {item.strip() for item in selected.split(",") if item.strip()}
    unknown = ids - CATALOG.keys()
    if unknown:
        raise ValueError(f"Unknown plugins: {', '.join(sorted(unknown))}")
    root = root.resolve()
    for plugin_id, plugin in CATALOG.items():
        if plugin_id in ids:
            continue
        for resource in plugin.resources:
            target = root.joinpath(resource).resolve()
            if not target.is_relative_to(root) or target == root:
                raise ValueError("Plugin resource is outside the distribution root")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.is_file():
                target.unlink()
    (root / "plugin-bundle.json").write_text(json.dumps(sorted(ids)) + "\n", encoding="utf-8")
    return sorted(ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugins", help="all, an empty string for core only, or comma-separated plugin IDs")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    print(json.dumps(package_plugins(args.root, args.plugins)))
