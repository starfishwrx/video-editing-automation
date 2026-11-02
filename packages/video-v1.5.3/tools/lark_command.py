"""Build an argv-safe official Lark CLI command, avoiding the Windows cmd shim."""
import os
from pathlib import Path
import shutil


def actor():
    value = os.environ.get('LARK_ACTOR', 'bot').strip().lower()
    if value not in {'bot', 'user'}:
        raise RuntimeError('LARK_ACTOR_INVALID')
    return value


def command(arguments, *, executable, profile=None):
    cli = Path(shutil.which(executable) or executable)
    if os.name == 'nt' and cli.suffix.casefold() in {'.cmd', '.bat'}:
        entry = cli.parent / 'node_modules/@larksuite/cli/scripts/run.js'
        node = shutil.which('node.exe') or shutil.which('node')
        if not entry.is_file() or not node:
            raise RuntimeError('LARK_NODE_ENTRY_REQUIRED')
        prefix = [node, str(entry)]
    else:
        prefix = [str(cli)]
    if profile:
        prefix += ['--profile', profile]
    return [*prefix, *arguments]
