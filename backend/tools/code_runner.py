"""Restricted execution of model-generated code.

The programmer agent used to return source text nobody ever ran, so a broken
snippet looked exactly like a working one.  This module gives the agent a
feedback loop: run the code, capture stdout/stderr, and let the agent repair
itself until the program actually works.

Isolation is layered but deliberately **not** a container:

* the program runs from a fresh temporary directory
* the environment is scrubbed of every credential-looking variable, so provider
  keys from ``API.json`` cannot be read out of ``os.environ``
* ``-I`` puts the interpreter in isolated mode (no user site-packages, no
  ``PYTHONPATH``, no ``sys.path`` injection from the caller)
* hard wall-clock timeout, output caps, and POSIX CPU/address-space limits
* the generated source is screened by :mod:`backend.safety.injection` *before*
  it runs; destructive or exfiltrating programs are refused and audited

Switch it off entirely with ``AGENT_CODE_EXEC=0`` (also exposed as a runtime
toggle in the safety panel).  Treat it as a guardrail for a trusted single-user
deployment, not as a security boundary against hostile code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.safety.injection import screen, severity_rank

logger = logging.getLogger("tools.code_runner")

BOOTSTRAP_PATH = Path(__file__).with_name("code_exec_bootstrap.py")

MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 25.0
MAX_TIMEOUT = 120.0
MAX_CODE_CHARS = 60_000

# Environment variables that must never reach generated code.
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
                 "SESSION", "COOKIE", "AUTH")
_ENV_ALLOW = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
              "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
              "LANG", "LC_ALL", "HOME")


@dataclass
class RunResult:
    """Outcome of one restricted execution."""

    ok: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    refused: bool = False
    refusal_reason: str = ""
    findings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 2),
            "timed_out": self.timed_out,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "findings": self.findings[:8],
        }

    def summary(self) -> str:
        if self.refused:
            return f"未执行（安全策略拒绝）：{self.refusal_reason}"
        if self.timed_out:
            return f"执行超时（>{self.duration:.0f}s）"
        if self.ok:
            first = self.stdout.strip().splitlines()
            head = first[0][:80] if first else "无输出"
            return f"运行通过（{self.duration:.1f}s，输出：{head}）"
        tail = (self.stderr or self.stdout).strip().splitlines()
        head = tail[-1][:120] if tail else "无错误输出"
        return f"运行失败（exit={self.exit_code}）：{head}"


def execution_enabled() -> bool:
    """Whether generated code may be executed at all.

    The runtime safety settings are the single source of truth (so the GUI
    toggle takes effect immediately); the env var only seeds the default.
    """
    try:
        from backend.safety.settings import get_safety_settings

        return bool(get_safety_settings().code_execution)
    except Exception:
        raw = os.environ.get("AGENT_CODE_EXEC")
        if raw is None:
            return True
        return raw.strip().lower() in ("1", "true", "yes", "on")


def _timeout_seconds() -> float:
    try:
        value = float(os.environ.get("AGENT_CODE_EXEC_TIMEOUT", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return max(1.0, min(MAX_TIMEOUT, value))


def build_env(workdir: Path) -> dict:
    """Minimal, credential-free environment for the child interpreter."""
    env: dict[str, str] = {}
    for name in _ENV_ALLOW:
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.setdefault("PATH", os.defpath)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = str(workdir)
    env["TMPDIR"] = str(workdir)
    env["AGENT_CODE_EXEC_CHILD"] = "1"
    return env


def contains_credentials(env: dict) -> bool:
    """True when any surviving variable looks like a secret (invariant check)."""
    return any(
        any(hint in name.upper() for hint in _SECRET_HINTS)
        for name in env
    )


def screen_code(code: str) -> list[dict]:
    """Screen generated source before it is allowed to run."""
    return [
        finding for finding in screen(code, source="generated_code")
        if finding.get("category") in (
            "destructive_action", "data_exfiltration", "credential_access",
            "remote_code_execution", "safety_bypass",
        )
    ]


def language_of(code: str) -> str:
    """Best-effort language detection for a fenced or bare snippet."""
    text = (code or "").strip()
    if text.startswith("```"):
        first = text.split("\n", 1)[0].strip("`").strip().lower()
        if first:
            return first.split()[0]
    for tag, names in (
        ("python", ("python", "py")),
        ("javascript", ("javascript", "js", "node", "typescript", "ts")),
        ("html", ("html",)),
        ("bash", ("bash", "sh", "shell")),
    ):
        for name in names:
            if name == "python" and text.startswith(("python", "py")):
                return tag
    return ""


def extract_code(code: str) -> tuple[str, str]:
    """Return ``(source, language)`` with markdown fences stripped."""
    text = (code or "").strip()
    if not text.startswith("```"):
        return text, ""
    lines = text.splitlines()
    lang = lines[0].strip("`").strip().split()[0].lower() if lines[0].strip("`").strip() else ""
    body = "\n".join(lines[1:])
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip(), lang


def is_runnable_python(code: str) -> bool:
    """True when the snippet is plain Python that can be executed directly."""
    source, lang = extract_code(code)
    if not source or len(source) > MAX_CODE_CHARS:
        return False
    if lang and lang not in ("python", "python3", "py"):
        return False
    if "<!doctype" in source[:200].lower() or "<html" in source[:200].lower():
        return False
    try:
        compile(source, "<generated>", "exec")
    except SyntaxError:
        return False
    return True


def _posix_limits():  # pragma: no cover - kept for reference
    """Deprecated: limits now live in ``code_exec_bootstrap.py``.

    ``preexec_fn`` cannot be used here because the runner executes on a worker
    thread, where it is documented as deadlock-prone.
    """
    return None


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[输出截断，共 {len(text)} 字符]"


def _read_capped(path: Path, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Read at most *limit* bytes so a chatty program cannot exhaust memory."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit + 1)
    except OSError:
        return ""
    text = raw[:limit].decode("utf-8", errors="replace")
    if len(raw) > limit:
        text += f"\n…[输出截断，超过 {limit} 字节]"
    return text


def _run_blocking(cmd: list[str], workdir: Path, env: dict,
                  stdin_path: Path, stdout_path: Path, stderr_path: Path,
                  timeout: float) -> tuple[int, bool]:
    """Run the child with file-backed stdio (no pipes) and return its exit code.

    Pipes are deliberately avoided: they are blocked in some hardened/sandboxed
    environments, and file redirection additionally bounds memory no matter how
    much the generated program prints.
    """
    result_code = -1
    timed_out = False
    with open(stdin_path, "rb") as stdin_fh, \
            open(stdout_path, "wb") as stdout_fh, \
            open(stderr_path, "wb") as stderr_fh:
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(workdir),
                env=env,
                stdin=stdin_fh,
                stdout=stdout_fh,
                stderr=stderr_fh,
                timeout=timeout,
                check=False,
            )
            result_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as exc:
            logger.debug("Child process could not start: %s", exc)
            raise
    return result_code, timed_out


async def run_python(
    code: str,
    *,
    timeout: Optional[float] = None,
    stdin: str = "",
    enabled: Optional[bool] = None,
) -> RunResult:
    """Execute *code* in an isolated interpreter and capture its output."""
    result = RunResult()
    started = time.monotonic()

    allow = execution_enabled() if enabled is None else enabled
    if not allow:
        result.refused = True
        result.refusal_reason = "代码执行已关闭（AGENT_CODE_EXEC=0）"
        return result

    source, _lang = extract_code(code)
    if not source:
        result.refused = True
        result.refusal_reason = "没有可执行的代码"
        return result
    if len(source) > MAX_CODE_CHARS:
        result.refused = True
        result.refusal_reason = f"代码过长（{len(source)} 字符）"
        return result

    result.findings = screen_code(source)
    blocked = [f for f in result.findings if severity_rank(f.get("severity", "low")) >= 2]
    if blocked:
        result.refused = True
        result.refusal_reason = "；".join(
            f["message"] for f in blocked[:3]
        ) or "代码包含被禁止的操作"
        logger.warning(
            "Refused to execute generated code: %s",
            [f.get("id") for f in blocked][:5],
        )
        return result

    limit = min(MAX_TIMEOUT, max(1.0, timeout if timeout is not None else _timeout_seconds()))

    with tempfile.TemporaryDirectory(prefix="agent_code_") as tmp:
        workdir = Path(tmp)
        script = workdir / "main.py"
        script.write_text(source, encoding="utf-8")
        stdin_path = workdir / "stdin.txt"
        stdin_path.write_text(stdin or "", encoding="utf-8")
        stdout_path = workdir / "stdout.txt"
        stderr_path = workdir / "stderr.txt"

        cmd = [
            sys.executable, "-I", "-X", "utf8",
            str(BOOTSTRAP_PATH), str(script),
        ]

        try:
            exit_code, timed_out = await asyncio.to_thread(
                _run_blocking, cmd, workdir, build_env(workdir),
                stdin_path, stdout_path, stderr_path, limit,
            )
        except OSError as exc:
            result.refused = True
            result.refusal_reason = f"无法启动子进程：{exc}"
            return result

        result.duration = time.monotonic() - started
        result.stdout = _read_capped(stdout_path)
        result.stderr = _read_capped(stderr_path)

        if timed_out:
            result.timed_out = True
            result.exit_code = -1
            result.stderr = (
                f"执行超过 {limit:.0f} 秒被强制终止\n" + result.stderr
            )
            return result

        result.exit_code = exit_code
        result.ok = exit_code == 0
        return result
