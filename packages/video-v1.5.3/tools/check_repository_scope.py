"""Check committed change scope; no runtime imports, labels, or self-approved exceptions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

REPO = Path(__file__).resolve().parents[3]
# Existing writable package roots: one active package and two explicit experiments.
WRITABLE_PACKAGES = frozenset({'video-v1.5.3', 'video-v1.5.4', 'video-v1.5.5'})
HISTORICAL_PACKAGES = frozenset({'video-v1.4.0', 'video-v1.5.0', 'video-v1.5.1', 'video-v1.5.2'})


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError('Git comparison failed: ' + result.stderr.decode('utf-8', errors='replace').strip())
    return result.stdout


def _commit(repo: Path, sha: str) -> str:
    if not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', sha) or set(sha) == {'0'}:
        raise ValueError('base/head must be a nonzero full commit SHA')
    return _git(repo, 'rev-parse', '--verify', sha + '^{commit}').decode().strip()


def changed_paths(repo: Path, base: str, head: str, *, pull_request: bool = False) -> list[str]:
    base, head = _commit(repo, base), _commit(repo, head)
    if pull_request:
        # Exclude changes made only on the target branch since the PR forked.
        base = _git(repo, 'merge-base', base, head).decode().strip()
    # Disable rename folding so both deleted source and added destination are checked.
    raw = _git(repo, 'diff', '--no-ext-diff', '--no-renames', '--name-only', '-z', base, head, '--')
    return [value.decode('utf-8', errors='surrogateescape') for value in raw.split(b'\0') if value]


def check(paths: list[str]) -> list[str]:
    issues = []
    for path in paths:
        parts = PurePosixPath(path).parts
        lower = tuple(part.casefold() for part in parts)
        if not parts or path.startswith('/') or '..' in parts or '\\' in path:
            issues.append(f'INVALID_REPOSITORY_PATH:{path}')
            continue
        if 'vendor' in lower:
            issues.append(f'VENDOR_CHANGE_REQUIRES_POLICY_REVIEW:{path}')
        if len(lower) >= 2 and lower[0] == 'packages':
            package = lower[1]
            # Windows aliases must not create parallel package roots on Linux.
            if parts[0] != 'packages' or parts[1] != package:
                issues.append(f'NONCANONICAL_PACKAGE_PATH:{path}')
            if package in HISTORICAL_PACKAGES:
                issues.append(f'HISTORICAL_PACKAGE_READ_ONLY:{path}')
            elif package not in WRITABLE_PACKAGES:
                issues.append(f'NEW_PACKAGE_ROOT_REQUIRES_POLICY_REVIEW:{path}')
    return sorted(set(issues))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, help='Full target/before commit SHA, available locally')
    parser.add_argument('--head', required=True, help='Full proposed/after commit SHA, available locally')
    parser.add_argument('--pull-request', action='store_true', help='Compare from merge-base, not target tip')
    args = parser.parse_args(argv)
    try:
        paths = changed_paths(REPO, args.base, args.head, pull_request=args.pull_request)
        issues = check(paths)
        report = {'status': 'FAIL' if issues else 'PASS', 'changed_paths': len(paths), 'issues': issues}
    except (OSError, ValueError) as error:
        report = {'status': 'ERROR', 'issues': [str(error)]}
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
