from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

SUSPICIOUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "non-empty Telegram session string",
        re.compile(r"^\s*TG_OUTREACH_TELEGRAM_SESSION_STRING\s*=\s*(?!\s*$)(?!\s*\"{0,1}\s*\"{0,1}\s*$).+$"),
    ),
    (
        "non-empty Telegram API hash",
        re.compile(r"^\s*TG_OUTREACH_TELEGRAM_API_HASH\s*=\s*(?!\s*$)(?!\s*\"{0,1}\s*\"{0,1}\s*$).+$"),
    ),
    (
        "non-empty SMTP password",
        re.compile(r"^\s*TG_OUTREACH_SMTP_PASSWORD\s*=\s*(?!\s*$)(?!\s*\"{0,1}\s*\"{0,1}\s*$).+$"),
    ),
    (
        "non-empty Astrixa gateway token",
        re.compile(r"^\s*ASTRIXA_GATEWAY_TOKEN\s*=\s*(?!\s*$)(?!\s*\"{0,1}\s*\"{0,1}\s*$).+$"),
    ),
    (
        "private key block",
        re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    ),
]


def git_tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def file_is_probably_text(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in sample


def main() -> int:
    tracked = git_tracked_files()
    failures: list[str] = []

    forbidden_tracked = {".env", ".env.local", ".env.production", ".env.development"}
    for path in tracked:
        if path in forbidden_tracked:
            failures.append(f"forbidden tracked file: {path}")

    for relative_path in tracked:
        path = REPO_ROOT / relative_path
        if not path.is_file() or not file_is_probably_text(path):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for description, pattern in SUSPICIOUS_PATTERNS:
            if pattern.search(content):
                failures.append(f"{description} in tracked file: {relative_path}")

    if failures:
        print("secret hygiene check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("secret hygiene check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
