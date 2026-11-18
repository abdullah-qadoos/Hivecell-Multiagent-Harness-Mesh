"""Path normalization (fix #1, decision 5) and repo discovery."""
import subprocess
from pathlib import Path, PurePosixPath


def git(cwd: Path, *args: str, strip: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip() if strip else r.stdout


def main_checkout(path: Path) -> Path:
    """Linked worktrees resolve to the main checkout, so every agent in a repo shares one workspace."""
    common = git(Path(path), "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common).resolve().parent


def ignore_case(repo: Path) -> bool:
    try:
        return git(repo, "config", "--get", "core.ignorecase").lower() == "true"
    except RuntimeError:
        return False  # git only writes the key when it is true


def normalize(path: str, root: Path, base: Path | None = None, fold: bool = False) -> str:
    """Absolute or relative (to `base`, default `root`) path -> repo-relative posix path.

    `base` may be a linked worktree: its files map onto the same repo-relative paths.
    """
    if not path or "\x00" in path:
        raise ValueError("empty or invalid path")
    base = Path(base or root).resolve()
    full = (base / path).resolve()
    if not full.is_relative_to(base):
        raise ValueError(f"{path!r} is outside the repository")
    rel = full.relative_to(base).as_posix()
    if rel in ("", "."):
        raise ValueError("cannot claim the whole repository")
    return rel.casefold() if fold else rel


def overlaps(a: str, b: str) -> bool:
    """Same file, or one is a folder containing the other."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def glob_prefix(glob: str) -> str:
    """'services/api/**' -> 'services/api': the folder an ownership glob covers."""
    parts = []
    for part in PurePosixPath(glob).parts:
        if any(c in part for c in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


def dirty_files(worktree: Path) -> list[str]:
    """Uncommitted changes in a worktree (fix #3: unreviewed work counts as an overlap)."""
    # not stripped: the leading space of " M file" is part of the format
    out = git(worktree, "status", "--porcelain", "-z", "--untracked-files=all", strip=False)
    files = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        if len(e) > 3:
            files.append(e[3:])
            if e[0] in "RC":  # renames carry the old path as the next entry
                i += 1
        i += 1
    return files


def worktrees(repo: Path) -> list[Path]:
    out = git(repo, "worktree", "list", "--porcelain")
    return [Path(line[9:]) for line in out.splitlines() if line.startswith("worktree ")]
