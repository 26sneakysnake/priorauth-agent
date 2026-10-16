"""Content hash of a directory tree, used to freeze the test split."""

import hashlib
from collections.abc import Mapping
from pathlib import Path


def tree_digest(files: Mapping[str, bytes]) -> str:
    """sha256 over the sorted list of "<sha256 of file>  <relative posix path>" lines."""
    lines = "".join(f"{hashlib.sha256(files[p]).hexdigest()}  {p}\n" for p in sorted(files))
    return hashlib.sha256(lines.encode()).hexdigest()


def read_tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def digest_dir(root: Path) -> str:
    return tree_digest(read_tree(root))


def read_digest_file(path: Path) -> str:
    """The digest file holds one line: "<digest>  <directory>"."""
    return path.read_text().split()[0]
