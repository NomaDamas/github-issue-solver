from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentResult:
    ok: bool
    output: str
    returncode: int


def run_agent(agent: str, repo_dir: Path, prompt: str, timeout: int) -> AgentResult:
    agent = (agent or "gjc").lower().strip()
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")

    input_text: str | None = prompt
    prompt_path: str | None = None

    if agent == "claude":
        # Pass the prompt via stdin so large GitHub issue bodies do not hit OS argv limits.
        cmd = ["claude", "-p", "--permission-mode", "bypassPermissions", "--output-format", "text"]
    elif agent == "omx":
        # omx/codex accepts '-' to read instructions from stdin.
        cmd = ["omx", "exec", "--dangerously-bypass-approvals-and-sandbox", "-C", str(repo_dir), "-"]
    elif agent == "gjc":
        # GJC uses @file for robust non-interactive prompt ingestion. Clear GitHub
        # token env vars because stale GH_TOKEN/GITHUB_TOKEN values make GJC prefer
        # broken token auth over its working keyring/OAuth setup.
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        env.setdefault("GJC_NO_PTY", "1")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", prefix="github-issue-solver-", delete=False) as f:
            f.write(prompt)
            prompt_path = f.name
        input_text = None
        cmd = ["gjc", "-p", "--mode", "text", "--no-session", f"@{prompt_path}"]
    else:
        raise ValueError(f"Unsupported agent: {agent}. Use 'gjc', 'omx', or 'claude'.")

    try:
        proc = subprocess.run(cmd, cwd=repo_dir, env=env, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return AgentResult(proc.returncode == 0, proc.stdout[-80_000:], proc.returncode)
    finally:
        if prompt_path:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass
