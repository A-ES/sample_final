"""Automatically commit, push, and create PR to GitHub after the agent finishes."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from config.config import Config, GitHubConfig


def _truncate_for_commit(s: str, max_len: int = 200) -> str:
    """Truncate and sanitize string for use in commit message (single line)."""
    if not s:
        return ""
    one_line = " ".join(s.split())
    one_line = re.sub(r"[^\w\s\-.,;:!?()\"']", "", one_line)
    if len(one_line) > max_len:
        one_line = one_line[: max_len - 3] + "..."
    return one_line


async def _run_git(cwd: Path, *args: str) -> tuple[int, str, str]:
    """Run git command; return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    return proc.returncode or 0, stdout, stderr


async def _is_git_repo(cwd: Path) -> bool:
    code, _, _ = await _run_git(cwd, "rev-parse", "--is-inside-work-tree")
    return code == 0


async def _has_changes(cwd: Path) -> bool:
    code, out, _ = await _run_git(cwd, "status", "--porcelain")
    return code == 0 and bool(out.strip())


async def _current_branch(cwd: Path) -> str | None:
    code, out, _ = await _run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if code == 0 and out.strip() else None


async def _remote_url(cwd: Path) -> str | None:
    """Get origin URL (e.g. https://github.com/A-ES/demo123.git)."""
    code, out, _ = await _run_git(cwd, "remote", "get-url", "origin")
    if code != 0 or not out.strip():
        return None
    url = out.strip()
    if url.endswith(".git"):
        url = url[:-4]
    return url


async def run_after_agent(
    config: Config,
    user_message: str,
    agent_response: str | None,
) -> None:
    """
    After the agent finishes: optionally commit, push, and create a PR.
    Runs only if config.github.enabled and there are uncommitted changes.
    """
    gh = config.github
    if not gh.enabled:
        return

    cwd = Path(config.cwd)
    if not await _is_git_repo(cwd):
        return

    if not await _has_changes(cwd):
        return

    agent_response_safe = (agent_response or "")[:500]
    user_message_safe = _truncate_for_commit(user_message, 150)

    if gh.auto_push and gh.branch:
        current = await _current_branch(cwd)
        if current != gh.branch:
            code, out, err = await _run_git(cwd, "rev-parse", "--verify", gh.branch)
            if code == 0:
                await _run_git(cwd, "checkout", gh.branch)
            else:
                await _run_git(cwd, "checkout", "-b", gh.branch)

    if gh.auto_commit:
        message = gh.commit_message_template.format(
            user_message=user_message_safe or "agent run",
            agent_response=agent_response_safe,
        )
        message = _truncate_for_commit(message, 200) or "Agent updates"

        await _run_git(cwd, "add", "-A")
        code, out, err = await _run_git(cwd, "commit", "-m", message)
        if code != 0 and "nothing to commit" not in (out + err).lower():
            print(f"github automation: commit failed: {err or out}", file=sys.stderr)
            return
        if code == 0:
            print(f"github automation: committed: {message[:60]}...")

    if gh.auto_push:
        branch = gh.branch or await _current_branch(cwd)
        if not branch:
            print("github automation: could not determine branch", file=sys.stderr)
            return

        if gh.branch and branch != gh.branch:
            await _run_git(cwd, "checkout", "-b", gh.branch)
            branch = gh.branch

        code, out, err = await _run_git(cwd, "push", "-u", "origin", branch)
        if code != 0:
            print(f"github automation: push failed: {err or out}", file=sys.stderr)
            return
        print(f"github automation: pushed to origin/{branch}")
        remote_url = await _remote_url(cwd)
        if remote_url:
            print(f"github automation: view branch: {remote_url}/tree/{branch}")
        print("github automation: (commit is on branch 'agent-updates', not main—switch branch on GitHub to see it)")

    if gh.auto_pr:
        code, _, _ = await _run_git(cwd, "which", "gh")
        if code != 0:
            print(
                "github automation: 'gh' CLI not found. Install it to create PRs.",
                file=sys.stderr,
            )
            return

        branch = gh.branch or await _current_branch(cwd)
        if not branch:
            return

        title = gh.pr_title_template.format(
            user_message=user_message_safe or "agent run",
            agent_response=agent_response_safe,
        )
        title = _truncate_for_commit(title, 100) or "Agent updates"

        body = gh.pr_body_template.format(
            user_message=user_message or "—",
            agent_response=(agent_response or "—")[:2000],
        )

        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            print(
                f"github automation: pr create failed: {stderr_bytes.decode() or stdout_bytes.decode()}",
                file=sys.stderr,
            )
            return
        print(f"github automation: PR created: {stdout_bytes.decode().strip()}")
