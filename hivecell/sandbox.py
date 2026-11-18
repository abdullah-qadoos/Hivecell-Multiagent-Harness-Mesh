"""Git isolation: one worktree and branch per agent per run. Docker comes later."""
import subprocess
from pathlib import Path

# commits made by agents are authored by the harness, not by whoever runs it
IDENTITY = ["-c", "user.name=hivecell", "-c", "user.email=hivecell@localhost"]


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *IDENTITY, *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def check_repo(repo: Path):
    try:
        git(repo, "rev-parse", "HEAD")
    except RuntimeError:
        raise RuntimeError(f"{repo} must be a git repo with at least one commit") from None
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    if not exclude.exists() or ".hivecell/" not in exclude.read_text(encoding="utf-8"):
        with exclude.open("a", encoding="utf-8") as f:
            f.write("\n.hivecell/\n")


def make_worktree(repo: Path, run_id: str, agent: str) -> tuple[Path, str]:
    path = repo / ".hivecell" / "worktrees" / run_id / agent
    branch = f"hivecell/{run_id}/{agent}"
    git(repo, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
    return path, branch


def commit_all(worktree: Path, message: str) -> bool:
    git(worktree, "add", "-A")
    if not git(worktree, "status", "--porcelain"):
        return False
    git(worktree, "commit", "-q", "-m", message)
    return True


def merge(worktree: Path, branch: str):
    git(worktree, "merge", "-q", "--no-edit", branch)
