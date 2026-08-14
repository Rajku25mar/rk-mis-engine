from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BLOCKED_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519"}
BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".zip", ".pdf"}
BLOCKED_PREFIXES = {"data/raw/", "data/private/", "secrets/"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[=:]\s*['\"][^'\"]{8,}['\"]"),
]


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / p.decode() for p in output.split(b"\0") if p]


def main() -> None:
    violations: list[str] = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if path.name in BLOCKED_NAMES:
            violations.append(f"blocked filename: {rel}")
        if path.suffix.lower() in BLOCKED_SUFFIXES:
            violations.append(f"blocked binary/archive type: {rel}")
        if any(rel.startswith(prefix) for prefix in BLOCKED_PREFIXES):
            violations.append(f"blocked public data path: {rel}")
        try:
            if path.stat().st_size > 5_000_000:
                violations.append(f"file exceeds 5 MB public-repo limit: {rel}")
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                violations.append(f"possible secret pattern: {rel}")
                break

    if violations:
        print("RK_MIS_PUBLIC_SAFETY=FAIL")
        for item in sorted(set(violations)):
            print(" -", item)
        raise SystemExit(1)
    print("RK_MIS_PUBLIC_SAFETY=PASS")


if __name__ == "__main__":
    main()
