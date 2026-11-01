"""The release workflow's two checks on the CHANGELOG and the version.

    python scripts/release.py check-tag v0.5.0   # exit 1 unless it names the package version
    python scripts/release.py notes [0.5.0]      # print that version's CHANGELOG section

`check-tag` keeps a tag from publishing one version under another's name;
`notes` is the GitHub release's text, the section as written, so the
CHANGELOG stays the one place release notes are kept.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "src" / "matterwave_tui" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"


def package_version(path: Path = VERSION_FILE) -> str:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    if match is None:
        raise SystemExit(f"{path} sets no __version__")
    return match.group(1)


def check_tag(tag: str, version: str) -> str | None:
    """None when `tag` is `v` plus `version`; otherwise what is wrong."""
    if tag == f"v{version}":
        return None
    return f"tag {tag} does not name the package version {version} (expected v{version})"


def notes(changelog: str, version: str) -> str:
    """The body of the `## [version]` section, up to the next `## [` or the
    link definitions at the foot."""
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.M)
    found = heading.search(changelog)
    if found is None:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    following = re.compile(r"^(## \[|\[[^\]]+\]: )", re.M).search(changelog, found.end())
    body = changelog[found.end() : following.start() if following else len(changelog)]
    body = body.strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md's section for {version} is empty")
    return body + "\n"


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "check-tag":
        problem = check_tag(argv[1], package_version())
        if problem:
            print(problem, file=sys.stderr)
            return 1
        return 0
    if argv[:1] == ["notes"] and len(argv) <= 2:
        version = argv[1] if len(argv) == 2 else package_version()
        # Bytes, so the CHANGELOG's dashes survive a console that is not UTF-8.
        sys.stdout.buffer.write(notes(CHANGELOG.read_text(encoding="utf-8"), version).encode())
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
