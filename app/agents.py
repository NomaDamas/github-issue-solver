from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentResult:
    ok: bool
    output: str
    returncode: int


def run_agent(agent: str, repo_dir: Path, prompt: str, timeout: int) -> AgentResult:
    agent = (agent or "omx").lower().strip()
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    if agent == "claude":
        # Pass the prompt via stdin so large GitHub issue bodies do not hit OS argv limits.
        cmd = ["claude", "-p", "--permission-mode", "bypassPermissions", "--output-format", "text"]
    elif agent == "omx":
        # omx/codex accepts '-' to read instructions from stdin.
        cmd = ["omx", "exec", "--dangerously-bypass-approvals-and-sandbox", "-C", str(repo_dir), "-"]
    else:
        raise ValueError(f"Unsupported agent: {agent}. Use 'omx' or 'claude'.")
    proc = subprocess.run(cmd, cwd=repo_dir, env=env, input=prompt, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return AgentResult(proc.returncode == 0, proc.stdout[-80_000:], proc.returncode)
