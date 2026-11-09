"""Opt-in host facts for logic tests; real admission and process tests stay unpatched."""
from functools import wraps
from unittest.mock import patch


def offline_host(test):
    """Keep unrelated editor/GPU detection and priority changes out of one test."""
    @wraps(test)
    def run(*args, **kwargs):
        with patch("orchestrator.runtime.platform_env._jianying_process_count", return_value=0), \
             patch("orchestrator.runtime.resource_profile.gpu_decode_available", return_value=False), \
             patch("orchestrator.application.engine.apply_process_priority", return_value=False):
            return test(*args, **kwargs)
    return run
