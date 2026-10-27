"""Safe names for local output files."""

import re
import unicodedata
from pathlib import Path


def safe_stem(filename: str) -> str:
    leaf = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(leaf).stem if Path(leaf).suffix else leaf
    stem = unicodedata.normalize("NFKC", stem)
    stem = "".join(c for c in stem if c.isprintable() and c not in "/\\")
    stem = re.sub(r"[^\w .-]+", "_", stem, flags=re.UNICODE).strip(" ._-")
    return stem[:80] or "media"
