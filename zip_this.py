"""PLAIN ENGLISH: Packs the project into a clean zip you can safely send to someone.

It leaves out everything that should never travel: your private `.env` settings, the `.venv` folder (which is
specific to your computer and huge), local databases, caches, old zip files and macOS clutter. `.env.example`
(the harmless template) IS included.
Usage:  python zip_this.py            -> writes ../org_automation_project.zip
"""
import sys
import zipfile
from pathlib import Path

SKIP_DIRS = {".venv", "venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
SKIP_NAMES = {".env", ".DS_Store"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".sqlite3", ".zip"}


def wanted(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
        return False
    return path.name not in SKIP_NAMES and path.suffix not in SKIP_SUFFIXES


def main() -> int:
    root = Path(__file__).resolve().parent
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else root.parent / "org_automation_project.zip"
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(root.rglob("*")):
            if path.is_file() and wanted(path, root):
                z.write(path, path.relative_to(root).as_posix())
                count += 1
    print(f"Wrote {count} files to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
