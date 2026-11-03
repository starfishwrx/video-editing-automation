"""Offline ready-candidate scheduling, backpressure and fail-closed behavior."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.ready_work import iter_ready
from orchestrator import ai_editor


class ReadyWorkTests(unittest.TestCase):
    def test_ready_call_does_not_wait_for_slow_peer_preparation(self):
        called = threading.Event()
        def prepare(job):
            if job == 'slow':
                self.assertTrue(called.wait(2))
        def consume(job):
            if job == 'fast':
                called.set()
            return job
        rows = dict(iter_ready(['slow', 'fast'], prepare, consume,
                    prep_workers=2, api_workers=1, on_error=lambda j, e: self.fail(str(e))))
        self.assertEqual(rows, {'slow': 'slow', 'fast': 'fast'})

    def test_bounded_admission_and_separate_worker_limits(self):
        lock = threading.Lock()
        state = dict(admitted=0, done=0, prep=0, api=0, peak=0)
        def jobs():
            for job in range(40):
                with lock:
                    state['admitted'] += 1
                    self.assertLessEqual(state['admitted'] - state['done'], 5)
                yield job
        def prepare(job):
            with lock:
                state['prep'] += 1
                self.assertLessEqual(state['prep'], 2)
            time.sleep(.002)
            with lock:
                state['prep'] -= 1
        def consume(job):
            with lock:
                state['api'] += 1
                self.assertLessEqual(state['api'], 3)
                state['peak'] = max(state['peak'], state['api'])
            time.sleep(.005)
            with lock:
                state['api'] -= 1
                state['done'] += 1
            return job
        result = dict(iter_ready(jobs(), prepare, consume, prep_workers=2, api_workers=3,
                                on_error=lambda j, e: self.fail(str(e))))
        self.assertEqual(len(result), 40)
        self.assertGreater(state['peak'], 1)

    def test_preparation_failure_never_consumes_or_becomes_pass(self):
        consume = mock.Mock(return_value='PASS')
        def prepare(job):
            raise ValueError('broken media')
        rows = dict(iter_ready(['bad'], prepare, consume, prep_workers=1, api_workers=1,
                               on_error=lambda j, e: {'verdict': 'uncertain', 'error': str(e)}))
        consume.assert_not_called()
        self.assertEqual(rows['bad']['verdict'], 'uncertain')

    def test_editor_preparation_failure_is_uncertain_and_no_api(self):
        review = mock.Mock()
        with mock.patch.object(ai_editor, '_review_media', side_effect=OSError('unreadable')):
            rows = dict(ai_editor._iter_review_round(['s'], {'s': {}}, Path('.'), review))
        review.assert_not_called()
        self.assertEqual(rows['s']['verdict'], 'uncertain')

    def test_completion_order_preserves_job_identity(self):
        result = dict(iter_ready([3, 2, 1], lambda j: None, lambda j: j * 10,
                                prep_workers=2, api_workers=2, on_error=lambda j, e: None))
        self.assertEqual(result, {1: 10, 2: 20, 3: 30})

    def test_invalid_workers_fail_and_empty_input_finishes(self):
        with self.assertRaises(ValueError):
            list(iter_ready([], None, None, prep_workers=0, api_workers=1, on_error=None))
        self.assertEqual(list(iter_ready([], None, None, prep_workers=1, api_workers=1,
                                       on_error=None)), [])
