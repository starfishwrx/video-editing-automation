"""Read-only readiness check for the default script production backend."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check'])
    parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'doubao-jianying-orchestrator'
    sys.path.insert(0, str(root))
    from orchestrator.runtime import headless as module
    try:
        result=module.preflight()
    except (ValueError, RuntimeError, OSError, ImportError) as exc:
        print(json.dumps({'status':'ENV_ERROR','reason':str(exc)},ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
