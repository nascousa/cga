"""Convert a local ADC release package to a validated, reproducible CGA seed."""
import argparse
from pathlib import Path

from backend.adc.service import ReleaseCreate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.source.resolve()
    if not (root / ".adc").is_dir():
        parser.error("Source must contain a published .adc directory")
    docs = {}
    for path in sorted((root / ".adc").rglob("*")):
        if path.is_symlink():
            parser.error(f"Symlinks are not accepted: {path.name}")
        if path.is_file():
            docs[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8-sig")
    release = ReleaseCreate(
        version=args.version, documents=docs,
        reason="Import the complete locally available ADC release; no claim of upstream freshness.",
        source=f"ADC local release package {args.version}",
    )
    if args.output.exists():
        parser.error("Output exists; immutable seed must not be overwritten")
    args.output.write_text(release.model_dump_json(indent=2), encoding="utf-8")
    print(f"Validated {len(docs)} ADC documents for release {release.version}")


if __name__ == "__main__":
    main()
