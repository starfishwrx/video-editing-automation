"""Read-only readiness check for the default script production backend."""
import argparse
import importlib.util
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check'])
    parser.parse_args()
    path=Path(__file__).resolve().parents[1]/'doubao-jianying-orchestrator/orchestrator/runtime/headless.py'
    spec=importlib.util.spec_from_file_location('_headless_preflight',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        result=module.preflight()
    except (ValueError, RuntimeError, OSError, ImportError) as exc:
        print(json.dumps({'status':'ENV_ERROR','reason':str(exc)},ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
