"""blog_context.py — Find published write-up posts from the Astro blog by artist name."""

import re
from pathlib import Path

BLOG_DIR = Path.home() / "reginerd-fm" / "src" / "content" / "blog"


def find_writeup(artist: str) -> str | None:
    """Return body text of a published write-up for the given artist, or None.

    Scans all .md/.mdx files in the Astro blog dir for a matching `artist:` frontmatter
    field. Skips draft posts. Returns up to 2000 chars of the post body.
    """
    if not artist or not BLOG_DIR.exists():
        return None

    artist_norm = _normalize(artist)

    for pattern in ("**/*.md", "**/*.mdx"):
        for md_file in BLOG_DIR.glob(pattern):
            result = _check_file(md_file, artist_norm)
            if result is not None:
                return result

    return None


def _normalize(name: str) -> str:
    return name.strip().lower()


def _check_file(path: Path, artist_norm: str) -> str | None:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    if not content.startswith("---"):
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    fm, body = parts[1], parts[2].strip()

    if re.search(r"^\s*draft\s*:\s*true", fm, re.MULTILINE):
        return None

    m = re.search(r"""^\s*artist\s*:\s*['""]?(.+?)['""]?\s*$""", fm, re.MULTILINE)
    if not m:
        return None

    if _normalize(m.group(1)) == artist_norm:
        return body[:2000]

    return None
