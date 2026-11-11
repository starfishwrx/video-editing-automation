"""Default worker recovery must reuse frozen audio and never restart a full build."""
import copy
import argparse
import contextlib
import io
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "doubao-jianying-orchestrator"))
import batch_worker as worker
from tools.workflow import selection_recovery, scheduled
from tools.queueing import contracts
from orchestrator.application import recovery_audio
from orchestrator.selection import timing_contract


class WorkerSelectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.report = self.root / "report"
        self.report.mkdir()
        self.drafts = self.root / "drafts"
        self.drafts.mkdir()
        self.audio = self.root / "original.wav"
        self.audio.write_bytes(b"retained audio fixture")
        self.manifest = {"task_id": "offline", "full_script": "保持原句。", "voice": "fixed-voice",
                         "delivery_mode": "formal", "draft_name": "original", "material_sources": ["source.mp4"]}
        segment = {"text": "保持原句。"}
        key = timing_contract.sentence_key(segment, 1)
        self.timing = {"rows": [{"key": key, "text": "保持原句。", "tts_path": str(self.audio),
                               "audio_duration_us": 1_000_000, "timing_certainty": "audio", "estimated": False}]}
        frozen = {**self.manifest, "segments": [segment]}
        stat = self.audio.stat()
        self.snapshot = {"input_manifest": self.manifest, "manifest": frozen, "timing": self.timing,
            "run_id": "current-attempt", "audio_files": [{"path": str(self.audio), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}]}
        self.write("selection_resume.json", self.snapshot)
        self.write("narration_timing.json", self.timing)
        self.write("shot_candidates.json", {"shots": [{"shot_id": "retained"}]})
        self.write("last_result.json", {"status": "VISION_CALL_FAILED", "retryable": True, "resume_stage": "semantic_match"})

    def test_worker_handoff_preserves_bound_exclusion_warning(self):
        cached = self.root / "shared.json"
        warning = {"type": "shot_skipped_unauditable", "shot_id": "bad", "reasks": 2}
        good = {"shot_id": "good", "description": "No action", "visual_tags": ["paper"],
                "evidence_tags": [], "analysis_confidence": .9, "visual_observation_complete": True,
                "action_complete": False, "status": "pending_analysis"}
        bad = {"shot_id": "bad", "status": "pending_analysis", "excluded_from_pool": True,
               "exclusion_cache_key": "bound-input", "exclusion_reason": "VISION_UNAUDITABLE_AFTER_REASK"}
        cached.write_text(json.dumps({"shots": [good, bad], "warnings": [warning]}))
        with patch.object(selection_recovery, "pending", return_value=False), \
             patch.object(selection_recovery, "draft_pending", return_value=False), \
             patch.object(worker, "SKILL", ROOT / "doubao-jianying-orchestrator"), \
             patch.object(worker, "log"), \
             patch.object(worker.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="")):
            result = worker.run_build(self.manifest, self.report, "handoff", timeout=60, reuse_analysis=str(cached))
        supplied = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["shot_analysis"]
        self.assertEqual(supplied["warnings"], [warning])
        self.assertEqual(supplied["shots"][1], bad)

    def write(self, name, value):
        path = self.report / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def recover(self, manifest=None, run=None):
        return selection_recovery.recover(manifest or self.manifest, self.report,
            prefix=[sys.executable, "-m", "orchestrator.cli"], run=run or Mock(), env={},
            cwd=str(ROOT), deadline=time.time() + 60, drafts_root=self.drafts)

    def successful_cli(self, command, **kwargs):
        stage = command[-1]
        if stage == "draft_write":
            draft = self.drafts / "unique-recovery"
            draft.mkdir()
            self.write("last_result.json", {"status": "SUCCESS", "draft_path": str(draft)})
        else:
            self.write("last_result.json", {"status": "PREFLIGHT_READY" if stage == "preflight" else "SHOT_MATCH_READY"})
        return SimpleNamespace(returncode=0, stdout=json.dumps({"stage": stage}), stderr="")

    def test_qc_refill_freezes_original_sentences_and_audio(self):
        from orchestrator.runtime.file_safety import content_identity
        self.snapshot['audio_files'] = [content_identity(self.audio)]
        self.write('selection_resume.json', self.snapshot)
        request = {**self.manifest, 'ai_refill_indices': [1], 'qc_rejected_shots': {}}
        result = selection_recovery.freeze_refill_input(request, self.report)
        self.assertEqual(result['segments'][0]['text'], '保持原句。')
        self.assertEqual(result['segments'][0]['audio'], str(self.audio))
        self.assertEqual(result['segments'][0]['audio_duration_us'], 1_000_000)
        self.assertEqual(result['ai_refill_indices'], [1])
        self.assertEqual(result['retained_tts_audio'], [str(self.audio)])
        self.audio.write_bytes(b'changed audio')
        with self.assertRaisesRegex(ValueError, 'REFILL_FROZEN_AUDIO_CHANGED'):
            selection_recovery.freeze_refill_input(request, self.report)

    def test_explicit_force_build_refill_reuses_frozen_audio_and_feedback(self):
        from orchestrator.runtime.file_safety import content_identity
        self.snapshot['audio_files'] = [content_identity(self.audio)]
        self.snapshot['manifest']['force_overwrite'] = True
        self.snapshot['manifest']['_selection_attempt'] = 'old-run'
        self.write('selection_resume.json', self.snapshot)
        self.write('selection_recovery_state.json', {'status': 'SUCCESS', 'source_run_id': 'current-attempt'})
        feedback = {'1': {'claim': '保持原句。', 'observed': 'prior display', 'reason': 'action missing',
                          'match_level': 'ACCEPTABLE_DEGRADED', 'prior_shot_ids': ['old-shot']}}
        request = {**self.manifest, 'force_overwrite': False, '_selection_attempt': 'new-run',
                   'ai_refill_indices': [1], 'ai_refill_feedback': feedback}
        def subprocess_build(command, **kwargs):
            input_path = Path(command[command.index('--input') + 1])
            actual = json.loads(input_path.read_text(encoding='utf-8'))
            self.assertEqual(actual['draft_name'], 'new-draft')
            self.assertFalse(actual['force_overwrite'])
            self.assertEqual(actual['_selection_attempt'], 'new-run')
            self.assertEqual(actual['segments'][0]['audio'], str(self.audio))
            self.assertEqual(actual['segments'][0]['audio_duration_us'], 1_000_000)
            self.assertEqual(actual['ai_refill_feedback'], feedback)
            return SimpleNamespace(returncode=0, stdout='fixture success', stderr='')
        with patch.object(worker, 'log'), patch.object(worker.subprocess, 'run', side_effect=subprocess_build) as run:
            result = worker.run_build(request, self.report, 'new-draft', timeout=60, force_rebuild=True)
        self.assertEqual(result['rc'], 0)
        run.assert_called_once()
        self.assertTrue(list(self.report.glob('selection_recovery_state.*.json')))
        self.assertEqual(self.audio.read_bytes(), b'retained audio fixture')

    def test_existing_dispatcher_resume_build_forwards_explicit_force(self):
        from tools import batch_dispatch
        queue = self.root / 'queue.json'
        queue.write_text(json.dumps([{'record_id': 'one', 'force_rebuild': True},
                                     {'record_id': 'other'}]), encoding='utf-8')
        with patch.object(batch_dispatch, 'dispatch', return_value=[]) as dispatched, \
                patch('builtins.print'):
            code = batch_dispatch.main(['resume', '--queue', str(queue), '--work-root', str(self.root),
                                        '--record-id', 'one', '--stage', 'build'])
        self.assertEqual(code, 0)
        self.assertEqual(dispatched.call_args.args[0], [{'record_id': 'one', 'force_rebuild': True}])
        self.assertEqual(dispatched.call_args.kwargs['resume_stage'], 'build')

    def test_default_worker_runs_recovery_stages_and_keeps_original_audio_and_timing(self):
        before = self.audio.read_bytes()
        with patch.object(worker, "DRAFTS", self.drafts), patch.object(worker, "log"), \
                patch.object(worker.subprocess, "run", side_effect=self.successful_cli) as run:
            result = worker.run_build(self.manifest, self.report, "requested-name", timeout=60)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(result["draft_name"], "unique-recovery")
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["preflight", "semantic_match", "draft_write"])
        self.assertTrue(all("build" not in c.args[0] for c in run.call_args_list))
        self.assertEqual(self.audio.read_bytes(), before)
        self.assertEqual(json.loads((self.report / "narration_timing.json").read_text(encoding="utf-8")), self.timing)
        frozen = json.loads((self.report / "selection_recovery_input.json").read_text(encoding="utf-8"))
        self.assertEqual(frozen["parent_run_id"], "current-attempt")
        bound = recovery_audio.bind(frozen)
        self.assertEqual(bound["voice"], "fixed-voice")
        self.assertEqual(bound["segments"][0]["audio"], str(self.audio.resolve()))
        self.assertEqual(bound["segments"][0]["audio_duration_us"], 1_000_000)
        self.assertEqual(bound["segments"][0]["audio_mode"], "file")
        self.assertFalse(selection_recovery.pending(self.report))

    def test_failed_preflight_preserves_the_single_selection_retry(self):
        before = self.audio.read_bytes()
        blocked = Mock(return_value=SimpleNamespace(returncode=1, stdout='missing font', stderr=''))
        first = self.recover(run=blocked)
        self.assertFalse(first['stage_retry_exhausted'])
        self.assertIn('SELECTION_RECOVERY_PREFLIGHT_FAILED', first['stdout'])
        self.assertEqual(json.loads((self.report / 'selection_recovery_state.json').read_text())['attempts'], 0)
        self.assertEqual(self.audio.read_bytes(), before)
        retry = Mock(side_effect=self.successful_cli)
        second = self.recover(run=retry)
        self.assertEqual(second['rc'], 0, second)
        self.assertEqual([call.args[0][-1] for call in retry.call_args_list],
                         ['preflight', 'semantic_match', 'draft_write'])
        self.assertEqual(self.audio.read_bytes(), before)

    def test_second_explicit_preflight_failure_preserves_budget_but_unknown_failure_does_not(self):
        def failed_inside_semantic(command, **kwargs):
            if command[-1] == 'preflight':
                return SimpleNamespace(returncode=0, stdout='{}', stderr='')
            return SimpleNamespace(returncode=1, stdout=json.dumps({'status': 'ENV_ERROR',
                'failure_class': 'environment', 'failed_stage': 'preflight'}), stderr='')
        result = self.recover(run=Mock(side_effect=failed_inside_semantic))
        self.assertTrue(result['preflight_only'])
        self.assertEqual(json.loads((self.report / 'selection_recovery_state.json').read_text())['attempts'], 0)

    def test_changed_audio_after_blocked_preflight_is_not_retryable(self):
        blocked = Mock(return_value=SimpleNamespace(returncode=1, stdout='missing font', stderr=''))
        self.recover(run=blocked)
        self.audio.write_bytes(b'changed audio bytes')
        result = self.recover(run=Mock())
        self.assertIn('SELECTION_RECOVERY_AUDIO_CHANGED', result['stdout'])
        self.assertFalse(result['preflight_only'])
        self.assertFalse(json.loads(result['stdout'])['retryable'])

    def test_same_metadata_changed_audio_cannot_resume_when_snapshot_has_hash(self):
        import os
        from orchestrator.runtime.file_safety import content_identity
        self.snapshot["audio_files"] = [content_identity(self.audio)]
        self.write("selection_resume.json", self.snapshot)
        before = self.audio.stat()
        contents = bytearray(self.audio.read_bytes())
        contents[0] ^= 1
        self.audio.write_bytes(contents)
        os.utime(self.audio, ns=(before.st_atime_ns, before.st_mtime_ns))
        run = Mock()
        result = self.recover(run=run)
        self.assertIn("SELECTION_RECOVERY_AUDIO_CHANGED", result["stdout"])
        run.assert_not_called()

    def test_audio_changed_during_recovered_draft_write_cannot_succeed(self):
        import os
        from orchestrator.runtime.file_safety import content_identity
        self.snapshot["audio_files"] = [content_identity(self.audio)]
        self.write("selection_resume.json", self.snapshot)

        def changed_during_write(command, **kwargs):
            result = self.successful_cli(command, **kwargs)
            if command[-1] == "draft_write":
                before = self.audio.stat()
                contents = bytearray(self.audio.read_bytes())
                contents[0] ^= 1
                self.audio.write_bytes(contents)
                os.utime(self.audio, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result

        result = self.recover(run=Mock(side_effect=changed_during_write))
        self.assertEqual(result["rc"], 1)
        self.assertIn("SELECTION_RECOVERY_AUDIO_CHANGED", result["stdout"])

    def test_fixed_bgm_must_be_frozen_and_unchanged_through_recovery(self):
        import os
        from orchestrator.runtime.file_safety import content_identity
        music = self.root / "music.wav"
        music.write_bytes(b"original-music-fixture")
        self.manifest["bgm"] = {"path": str(music)}
        self.snapshot["input_manifest"] = copy.deepcopy(self.manifest)
        self.snapshot["manifest"]["bgm"] = {"path": str(music)}
        self.write("selection_resume.json", self.snapshot)
        run = Mock()
        self.assertIn("SELECTION_RECOVERY_BGM_IDENTITY_REQUIRED", self.recover(run=run)["stdout"])
        run.assert_not_called()
        self.snapshot["bgm_file"] = content_identity(music)
        self.write("selection_resume.json", self.snapshot)

        def changed_during_write(command, **kwargs):
            result = self.successful_cli(command, **kwargs)
            if command[-1] == "draft_write":
                before = music.stat()
                contents = bytearray(music.read_bytes())
                contents[0] ^= 1
                music.write_bytes(contents)
                os.utime(music, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result

        result = self.recover(run=Mock(side_effect=changed_during_write))
        self.assertEqual(result["rc"], 1)
        self.assertIn("SELECTION_RECOVERY_BGM_CHANGED", result["stdout"])

    def test_audio_or_bgm_changed_during_preflight_is_not_a_retryable_preflight(self):
        import os
        from orchestrator.runtime.file_safety import content_identity
        for changed in ("audio", "bgm"):
            with self.subTest(changed=changed):
                music = self.root / (changed + "-music.wav")
                music.write_bytes(b"original-music-fixture")
                self.manifest["bgm"] = {"path": str(music)}
                self.snapshot["input_manifest"] = copy.deepcopy(self.manifest)
                self.snapshot["manifest"]["bgm"] = {"path": str(music)}
                self.snapshot["audio_files"] = [content_identity(self.audio)]
                self.snapshot["bgm_file"] = content_identity(music)
                self.write("selection_resume.json", self.snapshot)

                def changed_during_preflight(command, **kwargs):
                    result = self.successful_cli(command, **kwargs)
                    target = self.audio if changed == "audio" else music
                    before = target.stat()
                    data = bytearray(target.read_bytes())
                    data[0] ^= 1
                    target.write_bytes(data)
                    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
                    return result

                run = Mock(side_effect=changed_during_preflight)
                result = self.recover(run=run)
                self.assertEqual(result["rc"], 1)
                self.assertFalse(result["preflight_only"])
                self.assertFalse(json.loads(result["stdout"])["retryable"])
                run.assert_called_once()
                state = self.report / "selection_recovery_state.json"
                if state.exists():
                    state.unlink()

    def test_bgm_changed_when_preflight_child_fails_is_not_retryable(self):
        from orchestrator.runtime.file_safety import content_identity
        music = self.root / "music-preflight-failure.wav"
        music.write_bytes(b"original-music-fixture")
        self.manifest["bgm"] = {"path": str(music)}
        self.snapshot["input_manifest"] = copy.deepcopy(self.manifest)
        self.snapshot["manifest"]["bgm"] = {"path": str(music)}
        self.snapshot["bgm_file"] = content_identity(music)
        self.write("selection_resume.json", self.snapshot)

        def failed_preflight(command, **kwargs):
            music.write_bytes(b"changed-music-fixture!")
            return SimpleNamespace(returncode=1, stdout="preflight failed", stderr="")

        result = self.recover(run=Mock(side_effect=failed_preflight))
        self.assertIn("SELECTION_RECOVERY_BGM_CHANGED", result["stdout"])
        self.assertFalse(result["preflight_only"])
        self.assertFalse(json.loads(result["stdout"])["retryable"])

    def test_bgm_changed_during_preflight_timeout_is_not_retryable(self):
        from orchestrator.runtime.file_safety import content_identity
        music = self.root / "music-preflight-timeout.wav"
        music.write_bytes(b"original-music-fixture")
        self.manifest["bgm"] = {"path": str(music)}
        self.snapshot["input_manifest"] = copy.deepcopy(self.manifest)
        self.snapshot["manifest"]["bgm"] = {"path": str(music)}
        self.snapshot["bgm_file"] = content_identity(music)
        self.write("selection_resume.json", self.snapshot)

        def timeout(command, **kwargs):
            music.write_bytes(b"changed-music-fixture!")
            raise subprocess.TimeoutExpired(command, 1)

        result = self.recover(run=Mock(side_effect=timeout))
        self.assertIn("SELECTION_RECOVERY_BGM_CHANGED", result["stdout"])
        self.assertFalse(result["preflight_only"])
        self.assertFalse(json.loads(result["stdout"])["retryable"])

    def test_worker_propagates_preflight_block_to_done_without_a_second_attempt(self):
        blocked = self.recover(run=Mock(return_value=SimpleNamespace(
            returncode=1, stdout='missing font', stderr='')))
        task = {'record_id': 'record', 'title': 'draft', 'script': '一句',
                'material_dir': str(self.root), 'delivery_mode': 'preview'}
        source = self.root / 'source.mp4'
        source.write_bytes(b'fixture')
        (self.root / 'record' / 'eval').mkdir(parents=True)
        with patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts), \
             patch.object(worker, '_runtime_facts', return_value={}), \
             patch.object(worker, 'video_files', return_value=[str(source)]), \
             patch.object(worker, '_save_frozen_task'), \
             patch.object(worker, 'restore_qc_rejections', return_value=({}, {})), \
             patch.object(worker, 'build_progress_inputs', return_value={'fixture': True}), \
             patch.object(worker, 'publish_failure_notification', return_value={}), \
             patch.object(worker, 'progress'), patch.object(worker, 'log'), \
             patch.object(worker.production_eval, 'runtime_identity', return_value={}), \
             patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}), \
             patch.object(worker.production_eval, 'write_once'), \
             patch.object(worker, 'run_build', return_value=blocked) as build:
            worker.process_task(task, admitted=True)
        done = json.loads((self.root / 'record' / 'done.json').read_text(encoding='utf-8'))
        self.assertEqual(build.call_count, 1, done)
        self.assertEqual(done['failure_class'], 'environment')
        self.assertEqual(done['failure_code'], 'ENVIRONMENT_PRECHECK_FAILED')
        self.assertTrue(done['retryable'])
        self.assertEqual(done['resume_stage'], 'semantic_match')

    def test_worker_done_preserves_draft_write_resume_for_scheduler(self):
        task = {'record_id': 'record', 'title': 'draft', 'script': '一句',
                'material_dir': str(self.root), 'delivery_mode': 'formal'}
        source = self.root / 'source.mp4'
        source.write_bytes(b'fixture')
        (self.root / 'record' / 'eval').mkdir(parents=True)
        def interrupted(_manifest, report_dir, _name, **_kwargs):
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / 'last_result.json').write_text(json.dumps({
                'status': 'DRAFT_WRITE_TIMEOUT', 'retryable': True, 'resume_stage': 'draft_write',
                'failure_class': 'draft', 'failure_code': 'DRAFT_WRITE_TIMEOUT'}))
            return {'rc': 1, 'stdout': '', 'stderr': 'DRAFT_WRITE_TIMEOUT', 'combined': 'DRAFT_WRITE_TIMEOUT'}
        with patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts), \
             patch.object(worker, '_runtime_facts', return_value={}), patch.object(worker, 'video_files', return_value=[str(source)]), \
             patch.object(worker, '_save_frozen_task'), patch.object(worker, 'restore_qc_rejections', return_value=({}, {})), \
             patch.object(worker, 'build_progress_inputs', return_value={'fixture': True}), \
             patch.object(worker, 'publish_failure_notification', return_value={}), patch.object(worker, 'progress'), patch.object(worker, 'log'), \
             patch.object(worker.production_eval, 'runtime_identity', return_value={}), \
             patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}), \
             patch.object(worker.production_eval, 'write_once'), patch.object(worker, 'run_build', side_effect=interrupted) as build:
            worker.process_task(task, admitted=True)
            worker.process_task(task, admitted=True, retry_error=True)
            self.assertGreater(build.call_count, 3)
        done = json.loads((self.root / 'record' / 'done.json').read_text(encoding='utf-8'))
        self.assertTrue(done['retryable'], done)
        self.assertEqual(done['resume_stage'], 'draft_write')
        stage, _ = scheduled._plan(task, self.root.resolve(), {})
        self.assertEqual(stage, 'build')

    def test_worker_note_binding_survives_first_run_and_retry(self):
        task = {'record_id': 'record', 'title': 'draft', 'script': '第一句，继续。第二句',
                'material_dir': str(self.root), 'delivery_mode': 'formal', '画面备注': '第2句：展示包装'}
        source = self.root / 'source.mp4'
        source.write_bytes(b'fixture')
        (self.root / 'record' / 'eval').mkdir(parents=True)
        seen = []
        def save_task(directory, task_data, _frozen):
            (directory / 'task.json').write_text(json.dumps(task_data, ensure_ascii=False), encoding='utf8')
        def interrupted(manifest, report_dir, _name, **_kwargs):
            seen.append(copy.deepcopy(manifest))
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / 'last_result.json').write_text(json.dumps({
                'status': 'DRAFT_WRITE_TIMEOUT', 'retryable': True, 'resume_stage': 'draft_write',
                'failure_class': 'draft', 'failure_code': 'DRAFT_WRITE_TIMEOUT'}))
            return {'rc': 1, 'stdout': '', 'stderr': 'DRAFT_WRITE_TIMEOUT',
                    'combined': 'DRAFT_WRITE_TIMEOUT', 'stage_retry_exhausted': True}
        with patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts), \
             patch.object(worker, '_runtime_facts', return_value={}), patch.object(worker, 'video_files', return_value=[str(source)]), \
             patch.object(worker, '_save_frozen_task', side_effect=save_task), \
             patch.object(worker, 'restore_qc_rejections', return_value=({}, {})), \
             patch.object(worker, 'build_progress_inputs', return_value={'fixture': True}), \
             patch.object(worker, 'publish_failure_notification', return_value={}), \
             patch.object(worker, 'progress'), patch.object(worker, 'log'), \
             patch.object(worker.production_eval, 'runtime_identity', return_value={}), \
             patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}), \
             patch.object(worker.production_eval, 'write_once'), patch.object(worker, 'run_build', side_effect=interrupted):
            worker.process_task(task, admitted=True)
            worker.process_task(task, admitted=True, retry_error=True)
        self.assertGreaterEqual(len(seen), 2)
        for manifest in seen:
            self.assertEqual(manifest['shot_notes'], '第2句：展示包装')
            self.assertEqual(manifest['shot_notes_index_basis'], 'original_sentence')
            self.assertEqual(manifest['source_reuse_policy'], 'strict-v1')
        binding = json.loads((self.root / 'record' / 'shot_notes_binding.json').read_text(encoding='utf8'))
        self.assertEqual(binding['schema'], 'shot-notes-binding-v1')

    def test_note_binding_preflight_retry_and_force_rebuild_have_distinct_generations(self):
        directory = self.root / 'binding'
        directory.mkdir()
        task = {'record_id': 'binding', '画面备注': '第1句：展示包装'}
        # A dispatcher-created task.json and a failed preflight are not proof
        # that an older selection ever ran.
        (directory / 'task.json').write_text('{}', encoding='utf8')
        (directory / 'done.json').write_text('{"status":"ERROR","failures":[{"phase":"queue_preflight"}]}', encoding='utf8')
        first = worker._frozen_shot_note_overrides(directory, task, '画面备注')
        self.assertEqual(first['source_reuse_policy'], 'strict-v1')
        self.assertEqual(first['shot_notes'], '第1句：展示包装')
        self.assertEqual(worker._frozen_shot_note_overrides(directory, task, '画面备注'), first)
        changed = {**task, '画面备注': '第1句：展示实验'}
        with self.assertRaisesRegex(ValueError, 'SHOT_NOTES_INPUT_CHANGED'):
            worker._frozen_shot_note_overrides(directory, changed, '画面备注')
        rebuilt = worker._frozen_shot_note_overrides(directory, {**changed, 'force_rebuild': True}, '画面备注')
        self.assertEqual(rebuilt['shot_notes'], '第1句：展示实验')
        self.assertEqual(worker._frozen_shot_note_overrides(directory, changed, '画面备注'), rebuilt)
        history = list((directory / 'selection_policy_history').glob('*.json'))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text(encoding='utf8'))['overrides'], first)
        third = worker._frozen_shot_note_overrides(directory,
            {**task, '画面备注': '第1句：展示特写', 'force_rebuild': 1}, '画面备注')
        self.assertEqual(third['shot_notes'], '第1句：展示特写')

    def test_dispatcher_writes_task_before_worker_and_new_policy_still_reaches_build(self):
        from tools import batch_dispatch
        task = {'record_id': 'record', 'title': 'draft', 'script': '第一句。第二句',
                'material_dir': str(self.root), 'delivery_mode': 'formal', '画面备注': '第2句：展示包装'}
        source = self.root / 'source.mp4'
        source.write_bytes(b'fixture')
        (self.root / 'record' / 'eval').mkdir(parents=True)
        captured = []
        def save_task(directory, task_data, _frozen):
            (directory / 'task.json').write_text(json.dumps(task_data, ensure_ascii=False), encoding='utf8')
        def build(manifest, report_dir, _name, **_kwargs):
            captured.append(copy.deepcopy(manifest))
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / 'last_result.json').write_text(json.dumps({
                'status': 'DRAFT_WRITE_TIMEOUT', 'retryable': True, 'resume_stage': 'draft_write'}))
            return {'rc': 1, 'stdout': '', 'stderr': 'DRAFT_WRITE_TIMEOUT',
                    'combined': 'DRAFT_WRITE_TIMEOUT', 'stage_retry_exhausted': True}
        def launch(command, **_kwargs):
            path = Path(command[command.index('--task-file') + 1])
            self.assertTrue(path.is_file())
            worker.process_task(json.loads(path.read_text(encoding='utf8')), admitted=True)
            return SimpleNamespace(returncode=1, stdout='', stderr='')
        deadline = str(time.time() + 600)
        with patch.dict(worker.os.environ, {'JY_TASK_DEADLINE_EPOCH': deadline, 'JY_TASK_DEADLINE_TS': deadline}), \
             patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts), \
             patch.object(worker, '_runtime_facts', return_value={}), patch.object(worker, 'video_files', return_value=[str(source)]), \
             patch.object(worker, '_save_frozen_task', side_effect=save_task), \
             patch.object(worker, 'restore_qc_rejections', return_value=({}, {})), \
             patch.object(worker, 'build_progress_inputs', return_value={'fixture': True}), \
             patch.object(worker, 'publish_failure_notification', return_value={}), \
             patch.object(worker, 'progress'), patch.object(worker, 'log'), \
             patch.object(worker.production_eval, 'runtime_identity', return_value={}), \
             patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}), \
             patch.object(worker.production_eval, 'write_once'), patch.object(worker, 'run_build', side_effect=build), \
             patch.object(batch_dispatch.subprocess, 'run', side_effect=launch):
            batch_dispatch.run_task(task, 'build', {'JY_TASK_DEADLINE_EPOCH': deadline}, self.root)
        self.assertTrue(captured)
        self.assertEqual(captured[0]['source_reuse_policy'], 'strict-v1')
        self.assertEqual(captured[0]['shot_notes'], '第2句：展示包装')
        self.assertEqual(captured[0]['shot_notes_index_basis'], 'original_sentence')

    def test_old_generation_does_not_add_newly_forwarded_task_limits_on_retry(self):
        task = {'record_id': 'record', 'title': 'draft', 'script': '一句',
                'material_dir': str(self.root), 'delivery_mode': 'formal',
                'max_source_reuse': 1, 'max_absorb_demo_speed': 1.5}
        source = self.root / 'source.mp4'
        source.write_bytes(b'fixture')
        directory = self.root / 'record'
        (directory / 'eval').mkdir(parents=True)
        (directory / 'eval' / 'active_contract.json').write_text('{}', encoding='utf8')
        (directory / 'task.json').write_text(json.dumps(task), encoding='utf8')
        captured = []
        def build(manifest, report_dir, _name, **_kwargs):
            captured.append(copy.deepcopy(manifest))
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / 'last_result.json').write_text(json.dumps({
                'status': 'DRAFT_WRITE_TIMEOUT', 'retryable': True, 'resume_stage': 'draft_write'}))
            return {'rc': 1, 'stdout': '', 'stderr': 'DRAFT_WRITE_TIMEOUT',
                    'combined': 'DRAFT_WRITE_TIMEOUT', 'stage_retry_exhausted': True}
        with patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts), \
             patch.object(worker, '_runtime_facts', return_value={}), patch.object(worker, 'video_files', return_value=[str(source)]), \
             patch.object(worker, '_save_frozen_task'), patch.object(worker, 'restore_qc_rejections', return_value=({}, {})), \
             patch.object(worker, 'build_progress_inputs', return_value={'fixture': True}), \
             patch.object(worker, 'publish_failure_notification', return_value={}), \
             patch.object(worker, 'progress'), patch.object(worker, 'log'), \
             patch.object(worker.production_eval, 'runtime_identity', return_value={}), \
             patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}), \
             patch.object(worker.production_eval, 'write_once'), patch.object(worker, 'run_build', side_effect=build):
            worker.process_task(task, admitted=True)
            worker.process_task(task, admitted=True, retry_error=True)
        self.assertGreaterEqual(len(captured), 2)
        for manifest in captured:
            self.assertEqual(manifest['max_source_reuse'], 2)
            self.assertNotIn('max_absorb_demo_speed', manifest)
            self.assertNotIn('source_reuse_policy', manifest)

    def test_qc_target_without_bound_feedback_never_starts_a_weak_refill(self):
        task = {'record_id': 'record', 'title': 'draft', 'script': '一句',
                'material_dir': str(self.root), 'delivery_mode': 'formal', 'force_rebuild': True}
        source = self.root / 'source.mp4'; source.write_bytes(b'fixture')
        report = self.root / 'record' / 'report' / 'draft'
        report.mkdir(parents=True)
        (report / 'qc_rejected_shots.json').write_text(json.dumps({'repair_indices': [1], 'repair_feedback': {}}))
        (self.root / 'record' / 'eval').mkdir(parents=True)
        with patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts), \
             patch.object(worker, '_runtime_facts', return_value={}), patch.object(worker, 'video_files', return_value=[str(source)]), \
             patch.object(worker, '_save_frozen_task'), patch.object(worker, 'restore_qc_rejections', return_value=({}, {})), \
             patch.object(worker, 'publish_failure_notification', return_value={}), patch.object(worker, 'progress'), patch.object(worker, 'log'), \
             patch.object(worker.production_eval, 'runtime_identity', return_value={}), \
             patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}), \
             patch.object(worker.production_eval, 'write_once'), patch.object(worker, 'run_build') as build:
            worker.process_task(task, admitted=True)
        build.assert_not_called()
        done = json.loads((self.root / 'record' / 'done.json').read_text(encoding='utf-8'))
        self.assertIn('QC_FEEDBACK_BINDING_UNAVAILABLE', str(done['failures']))

    def test_refill_timeout_resumes_only_draft_write_and_rejects_changed_identity(self):
        for change_source in (False, True):
            with self.subTest(change_source=change_source):
                task = {'record_id': 'record', 'title': 'draft', 'script': '一句',
                        'material_dir': str(self.root), 'delivery_mode': 'formal'}
                source = self.root / 'source.mp4'
                source.write_bytes(b'original fixture')
                (self.root / 'record' / 'eval').mkdir(parents=True, exist_ok=True)
                report = self.root / 'record' / 'report' / 'draft'
                feedback = {'1': {'observed': 'old A', 'reason': 'missing action'}}
                original_build = worker.run_build
                calls = []
                def build(manifest, report_dir, name, **kwargs):
                    calls.append(name)
                    if len(calls) == 1:
                        report_dir.mkdir(parents=True, exist_ok=True)
                        worker.save_qc_rejections(report_dir, worker.qc_rejection_identity(manifest), {}, {1}, feedback)
                        # A was reviewed; the successful refill already replaced it by B.
                        (report_dir / 'shot_match_report.json').write_text(json.dumps({
                            'ok': True, 'input_manifest': manifest,
                            'segments': [{'index': 1, 'shot_id': 'source-B'}]}))
                        (report_dir / 'draft_visual_qc.json').write_text(json.dumps({'status': 'PASS_WITH_DEGRADED'}))
                        (report_dir / 'last_result.json').write_text(json.dumps({
                            'retryable': True, 'resume_stage': 'draft_write', 'status': 'DRAFT_WRITE_TIMEOUT'}))
                        return {'rc': 1, 'stdout': '', 'stderr': 'DRAFT_WRITE_TIMEOUT',
                                'combined': 'DRAFT_WRITE_TIMEOUT', 'stage_retry_exhausted': True}
                    self.assertNotIn('ai_refill_indices', manifest)
                    return original_build(manifest, report_dir, name, **kwargs)
                def cli(command, **kwargs):
                    self.assertEqual(command[-2:], ['--stage', 'draft_write'])
                    draft = self.drafts / 'recovered'
                    draft.mkdir(exist_ok=True)
                    (report / 'last_result.json').write_text(json.dumps({'status': 'SUCCESS', 'draft_path': str(draft)}))
                    return SimpleNamespace(returncode=0, stdout='', stderr='')
                with ExitStack() as stack:
                    patches = [patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts),
                        patch.object(worker, '_runtime_facts', return_value={}), patch.object(worker, 'video_files', return_value=[str(source)]),
                        patch.object(worker, '_save_frozen_task'), patch.object(worker, 'publish_failure_notification', return_value={}),
                        patch.object(worker, 'progress'), patch.object(worker, 'log'),
                        patch.object(worker.production_eval, 'runtime_identity', return_value={}),
                        patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}),
                        patch.object(worker.production_eval, 'write_once'),
                        patch.object(worker, 'qc_refill_feedback_from_report', return_value={}),
                        patch.object(worker, 'run_build', side_effect=build),
                        patch.object(worker, 'validate_visual_match', return_value=(True, '')),
                        patch.object(worker, 'run_post_draft_qc', return_value={'status': 'PASS', 'output': 'qc.json'}),
                        patch.object(worker, 'validate_draft_package', return_value={'ok': True}),
                        patch.object(worker, 'finalize_closed_loop', return_value={'ok': True}),
                        patch.object(worker, 'evaluate_task', return_value={})]
                    for item in patches:
                        stack.enter_context(item)
                    run = stack.enter_context(patch.object(worker.subprocess, 'run', side_effect=cli))
                    worker.process_task(task, admitted=True)
                    if change_source:
                        source.write_bytes(b'changed fixture of different size')
                    worker.process_task(task, admitted=True, retry_error=True)
                    state = json.loads((report / 'qc_rejected_shots.json').read_text())
                    self.assertEqual(state['repair_feedback'], feedback)
                    if change_source:
                        run.assert_not_called()
                        done = json.loads((self.root / 'record' / 'done.json').read_text(encoding='utf-8'))
                        self.assertIn('DRAFT_RECOVERY_IDENTITY_CHANGED', str(done['failures']))
                    else:
                        run.assert_called_once()
                        self.assertEqual(state['active_repair_feedback'], {})
                # Isolate the next subtest from the previous done/checkpoint.
                import shutil
                shutil.rmtree(self.root / 'record')

    def test_latest_qc_only_refills_current_failed_sentence(self):
        task = {'record_id': 'record', 'title': 'draft', 'script': '三句',
                'material_dir': str(self.root), 'delivery_mode': 'formal'}
        source = self.root / 'source.mp4'; source.write_bytes(b'fixture')
        (self.root / 'record' / 'eval').mkdir(parents=True)
        fixed = {'index': 2, 'text': 'second', 'shot_id': 'fixed-B', 'audio': 'frozen.wav',
                 'source_start': 1.2, 'source_end': 2.4}
        calls = []
        def build(manifest, report, name, **kwargs):
            calls.append(copy.deepcopy(manifest))
            report.mkdir(parents=True, exist_ok=True)
            segments = [{'index': 1, 'text': 'first', 'shot_id': 'one'}, copy.deepcopy(fixed),
                        {'index': 3, 'text': 'third', 'shot_id': 'three'}]
            (report / 'shot_match_report.json').write_text(json.dumps({'segments': segments}))
            return {'rc': 0, 'stdout': '', 'stderr': '', 'combined': ''}
        def qc(draft, report):
            round_no = len(calls)
            levels = {1: 'DIRECT', 2: 'ACCEPTABLE_DEGRADED' if round_no == 1 else 'DIRECT',
                      3: 'ACCEPTABLE_DEGRADED' if round_no == 2 else 'DIRECT'}
            rows = [{'index': i, 'claim': ('first', 'second', 'third')[i-1],
                     'match_level': levels[i], 'observed': 'pixels', 'reason': 'missing action'} for i in (1, 2, 3)]
            return {'status': 'PASS' if round_no == 3 else 'PASS_WITH_DEGRADED',
                    'message': 'DRAFT_VISUAL_QC_BLOCKED', 'report': {'model_review': rows}, 'output': 'qc.json'}
        def feedback(report_dir, report, targets, **kwargs):
            return {str(r['index']): {**r, 'prior_shot_ids': ['prior']}
                    for r in report.get('model_review', []) if r['index'] in targets}
        def rejected(report_dir, report, **kwargs):
            if not kwargs.get('include_non_direct'):
                return {}
            return {r['index']: {'prior'} for r in report.get('model_review', [])
                    if r['match_level'] == 'ACCEPTABLE_DEGRADED'}
        with ExitStack() as stack:
            patches = [patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts),
                patch.object(worker, '_runtime_facts', return_value={}), patch.object(worker, 'video_files', return_value=[str(source)]),
                patch.object(worker, '_save_frozen_task'), patch.object(worker, 'publish_failure_notification', return_value={}),
                patch.object(worker, 'progress'), patch.object(worker, 'log'),
                patch.object(worker.production_eval, 'runtime_identity', return_value={}),
                patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}),
                patch.object(worker.production_eval, 'write_once'), patch.object(worker, 'run_build', side_effect=build),
                patch.object(worker, 'validate_visual_match', return_value=(True, '')),
                patch.object(worker, 'run_post_draft_qc', side_effect=qc),
                patch.object(worker, 'qc_refill_feedback_from_report', side_effect=feedback),
                patch.object(worker, 'qc_rejected_shots_from_report', side_effect=rejected),
                patch.object(worker, 'validate_draft_package', return_value={'ok': True}),
                patch.object(worker, 'finalize_closed_loop', return_value={'ok': True}),
                patch.object(worker, 'evaluate_task', return_value={})]
            for item in patches: stack.enter_context(item)
            worker.process_task(task, admitted=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1]['ai_refill_indices'], [2])
        self.assertEqual(calls[2]['ai_refill_indices'], [3])
        self.assertEqual(set(calls[2]['ai_refill_feedback']), {'3'})
        report = self.root / 'record' / 'report' / 'draft' / 'shot_match_report.json'
        self.assertEqual(json.loads(report.read_text())['segments'][1], fixed)

    def test_cold_qc_refill_then_warm_force_rebuild_restores_feedback(self):
        from orchestrator.runtime.file_safety import content_identity
        task = {'record_id': 'record', 'title': 'draft', 'script': '一句',
                'material_dir': str(self.root), 'delivery_mode': 'formal'}
        source = self.root / 'source.mp4'; source.write_bytes(b'source')
        (self.root / 'record' / 'eval').mkdir(parents=True)
        calls = []
        observation = {'1': {'claim': '一句', 'match_level': 'ACCEPTABLE_DEGRADED',
                             'observed': 'package', 'reason': 'action absent', 'prior_shot_ids': ['A']}}
        def build(manifest, report, name, **kwargs):
            calls.append(copy.deepcopy(manifest))
            report.mkdir(parents=True, exist_ok=True)
            if len(calls) == 1:
                original = {**manifest, 'draft_name': name, 'shot_analysis': {'shots': []}}
                sentence = {'text': '一句'}
                timing = {'rows': [{'key': timing_contract.sentence_key(sentence, 1), 'text': '一句',
                         'tts_path': str(self.audio), 'audio_duration_us': 1_000_000,
                         'timing_certainty': 'audio', 'estimated': False}]}
                prepared = {**original, 'segments': [sentence], 'voice_tempo': 1.0,
                            'narration_timing_file': str(report / 'narration_timing.json')}
                (report / 'narration_timing.json').write_text(json.dumps(timing))
                (report / 'selection_resume.json').write_text(json.dumps({'input_manifest': original,
                    'manifest': prepared, 'timing': timing, 'audio_files': [content_identity(self.audio)], 'run_id': 'cold'}))
                (report / 'shot_match_report.json').write_text(json.dumps({'ok': True,
                    'input_manifest': original, 'segments': [{'text': '一句', 'shot_id': 'A'}]}))
                return {'rc': 0, 'stdout': '', 'stderr': '', 'combined': ''}
            frozen = selection_recovery.freeze_refill_input({**manifest, 'draft_name': name}, report)
            self.assertEqual(frozen['segments'][0]['audio'], str(self.audio))
            self.assertEqual(manifest['ai_refill_indices'], [1])
            if len(calls) == 2:
                (report / 'shot_match_report.json').write_text(json.dumps({'ok': True,
                    'input_manifest': frozen, 'segments': [{'text': '一句', 'shot_id': 'A'}]}))
                return {'rc': 1, 'stdout': '', 'stderr': 'HEADLESS_TARGET_EXISTS',
                        'combined': 'HEADLESS_TARGET_EXISTS', 'stage_retry_exhausted': True}
            self.assertEqual(manifest['ai_refill_feedback']['1']['reason'], 'action absent')
            for field, value in (('variant_count', 2), ('variant_index', 1)):
                with self.assertRaisesRegex(ValueError, 'REFILL_ORIGINAL_INPUT_CHANGED'):
                    selection_recovery.freeze_refill_input({**manifest, field: value}, report)
            return {'rc': 1, 'stdout': '', 'stderr': 'offline stop', 'combined': 'offline stop',
                    'stage_retry_exhausted': True}
        def qc(draft, report):
            payload = {'status': 'PASS_WITH_DEGRADED', 'model_review': [{'index': 1,
                'claim': '一句', 'match_level': 'ACCEPTABLE_DEGRADED'}]}
            (report / 'draft_visual_qc.json').write_text(json.dumps(payload))
            return {'status': 'PASS_WITH_DEGRADED', 'message': 'DRAFT_VISUAL_QC_BLOCKED',
                    'report': payload, 'output': 'qc.json'}
        def feedback(report, qc_report, targets, **kwargs):
            return {key: value for key, value in observation.items() if int(key) in targets}
        def rejected(report, qc_report, **kwargs):
            return {1: {'A'}} if kwargs.get('include_non_direct') else {}
        with ExitStack() as stack:
            patches = [patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts),
                patch.object(worker, '_runtime_facts', return_value={}), patch.object(worker, 'video_files', return_value=[str(source)]),
                patch.object(worker, '_save_frozen_task'), patch.object(worker, 'publish_failure_notification', return_value={}),
                patch.object(worker, 'progress'), patch.object(worker, 'log'),
                patch.object(worker.production_eval, 'runtime_identity', return_value={}),
                patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}),
                patch.object(worker.production_eval, 'write_once'), patch.object(worker, 'run_build', side_effect=build),
                patch.object(worker, 'validate_visual_match', return_value=(True, '')),
                patch.object(worker, 'run_post_draft_qc', side_effect=qc),
                patch.object(worker, 'qc_refill_feedback_from_report', side_effect=feedback),
                patch.object(worker, 'qc_rejected_shots_from_report', side_effect=rejected)]
            for item in patches: stack.enter_context(item)
            worker.process_task(task, admitted=True)
            self.assertEqual(len(calls), 2)
            worker.process_task({**task, 'force_rebuild': True}, admitted=True, retry_error=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2]['ai_refill_indices'], [1])

    def test_worker_done_resumes_delivery_after_table_failure(self):
        task = {'record_id': 'record', 'title': 'draft', 'script': '一句',
                'material_dir': str(self.root), 'delivery_mode': 'formal'}
        source = self.root / 'source.mp4'
        source.write_bytes(b'fixture')
        (self.root / 'record' / 'eval').mkdir(parents=True)
        with patch.object(worker, 'WORK', self.root), patch.object(worker, 'DRAFTS', self.drafts), \
             patch.object(worker, 'SKIP_SHIP', False), patch.object(worker, '_runtime_facts', return_value={}), \
             patch.object(worker, 'video_files', return_value=[str(source)]), patch.object(worker, '_save_frozen_task'), \
             patch.object(worker, 'restore_qc_rejections', return_value=({}, {})), \
             patch.object(worker, 'build_progress_inputs', return_value={'fixture': True}), \
             patch.object(worker, 'publish_failure_notification', return_value={}), patch.object(worker, 'progress'), patch.object(worker, 'log'), \
             patch.object(worker.production_eval, 'runtime_identity', return_value={}), \
             patch.object(worker.production_eval, 'freeze', return_value={'mode': 'enforce'}), \
             patch.object(worker.production_eval, 'write_once'), patch.object(worker, 'run_build', return_value={'rc': 0}), \
             patch.object(worker, 'validate_visual_match', return_value=(True, '')), \
             patch.object(worker, 'run_post_draft_qc', return_value={'status': 'PASS', 'output': 'qc.json'}), \
             patch.object(worker, 'validate_draft_package', return_value={'ok': True}), \
             patch.object(worker, 'finalize_closed_loop', return_value={'ok': False, 'reason': 'TABLE_READBACK_FAILED'}), \
             patch.object(worker, 'evaluate_task', return_value={}):
            worker.process_task(task, admitted=True)
        done = json.loads((self.root / 'record' / 'done.json').read_text(encoding='utf-8'))
        self.assertTrue(done['retryable'], done)
        self.assertEqual(done['resume_stage'], 'delivery')
        stage, _ = scheduled._plan(task, self.root.resolve(), {})
        self.assertEqual(stage, 'delivery')

    def test_changed_task_input_never_launches_cli_or_falls_back_to_build(self):
        run = Mock()
        for field, value in (("voice", "another-speaker"), ("full_script", "另一句"), ("material_sources", ["different.mp4"])):
            with self.subTest(field=field):
                result = self.recover({**self.manifest, field: value}, run=run)
                self.assertIn("SELECTION_RECOVERY_INPUT_CHANGED", result["stdout"])
        run.assert_not_called()

    def test_changed_audio_or_timing_is_not_regenerated_or_promoted(self):
        run = Mock()
        self.audio.write_bytes(b"changed recording")
        result = self.recover(run=run)
        self.assertIn("SELECTION_RECOVERY_AUDIO_CHANGED", result["stdout"])
        run.assert_not_called()
        self.snapshot["audio_files"][0].update(size=self.audio.stat().st_size, mtime_ns=self.audio.stat().st_mtime_ns)
        self.snapshot["timing"]["rows"][0]["timing_certainty"] = "estimated"
        self.write("selection_resume.json", self.snapshot)
        self.write("narration_timing.json", self.snapshot["timing"])
        self.assertIn("SELECTION_RECOVERY_AUDIO_REQUIRED", self.recover(run=run)["stdout"])
        frozen = {**self.snapshot["manifest"], "narration_timing_file": str(self.report / "narration_timing.json")}
        with self.assertRaisesRegex(ValueError, "TIMING_CHANGED"):
            recovery_audio.bind(frozen)
        run.assert_not_called()

    def test_interrupted_recovery_is_persisted_and_cannot_return_to_full_build(self):
        def interrupted(command, **kwargs):
            if command[-1] == "preflight":
                return self.successful_cli(command, **kwargs)
            raise subprocess.TimeoutExpired(command, 1)
        run = Mock(side_effect=interrupted)
        first = self.recover(run=run)
        self.assertTrue(first["stage_retry_exhausted"])
        self.assertTrue(selection_recovery.pending(self.report))
        with patch.object(worker, "DRAFTS", self.drafts), patch.object(worker, "log"), \
                patch.object(worker.subprocess, "run", side_effect=AssertionError("must not launch another process")):
            second = worker.run_build(self.manifest, self.report, "requested-name", timeout=60)
        self.assertIn("SELECTION_STAGE_RETRY_EXHAUSTED", second["stdout"])
        self.assertEqual(run.call_count, 2)

    def test_parent_timeout_retains_only_the_current_selection_attempt(self):
        self.write("last_result.json", {"status": "PREFLIGHT_READY"})
        self.write("shot_match_report.json", {"ok": True, "segments": ["stale success"]})
        def timed_out(command, **kwargs):
            self.snapshot["run_id"] = "new-selection-attempt"
            self.write("selection_resume.json", self.snapshot)
            self.write("ai_editor/plan.json", {"phase": "selection_edit", "attempt_run_id": "new-selection-attempt"})
            raise subprocess.TimeoutExpired(command, 1)
        with patch.object(worker, "log"), patch.object(worker.subprocess, "run", side_effect=timed_out):
            result = worker.run_build(self.manifest, self.report, "original", timeout=1)
        self.assertEqual(json.loads(result["stdout"])["resume_stage"], "semantic_match")
        self.assertFalse(json.loads((self.report / "shot_match_report.json").read_text(encoding="utf-8"))["ok"])
        archives = list((self.report / "match_history").glob("*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(json.loads(archives[0].read_text(encoding="utf-8"))["segments"], ["stale success"])
        self.write("ai_editor/plan.json", {"phase": "selection_edit", "attempt_run_id": "old-attempt"})
        self.assertIsNone(selection_recovery.selection_timeout(self.report))

    def test_late_draft_timeout_retains_completed_matching_instead_of_reselecting(self):
        self.write("last_result.json", {"status": "PREFLIGHT_READY"})
        def timed_out(command, **kwargs):
            self.snapshot["run_id"] = "completed-current-attempt"
            self.write("selection_resume.json", self.snapshot)
            self.write("ai_editor/plan.json", {"ok": True, "phase": "selection_complete", "attempt_run_id": "completed-current-attempt"})
            self.write("shot_match_report.json", {"ok": True, "input_manifest": self.manifest})
            raise subprocess.TimeoutExpired(command, 1)
        with patch.object(worker, "log"), patch.object(worker.subprocess, "run", side_effect=timed_out):
            result = worker.run_build(self.manifest, self.report, "original", timeout=1)
        self.assertEqual(json.loads(result["stdout"])["resume_stage"], "draft_write")
        self.assertTrue(json.loads(result["stdout"])["retryable"])
        self.assertTrue(json.loads((self.report / "shot_match_report.json").read_text(encoding="utf-8"))["ok"])

    def test_interrupted_gap_recovery_never_starts_full_build_with_missing_freeze(self):
        self.write("last_result.json", {"status": "NARRATION_GAP_EXCEEDED", "retryable": False,
                   "recovery_stage": "draft_write", "resume_stage": "semantic_match"})
        (self.report / "selection_resume.json").unlink()
        with patch.object(worker, "log"), patch.object(worker.subprocess, "run") as child:
            for _ in range(2):
                result = worker.run_build(self.manifest, self.report, "original", timeout=30)
                self.assertNotEqual(result["rc"], 0)
                self.assertTrue(result["stage_retry_exhausted"])
        child.assert_not_called()

    def test_missing_last_result_resumes_only_with_verified_frozen_inputs(self):
        self.write("last_result.json", {})
        self.assertTrue(selection_recovery.pending(self.report))
        (self.report / "last_result.json").unlink()
        self.assertTrue(selection_recovery.pending(self.report))
        self.audio.write_bytes(b"changed")
        with patch.object(worker, "log"), patch.object(worker.subprocess, "run") as child:
            self.assertNotEqual(worker.run_build(self.manifest, self.report, "original", timeout=30)["rc"], 0)
            child.assert_not_called()

    def test_policy_change_routes_once_through_frozen_recovery(self):
        self._check_frozen_rematch("AI_SELECTION_RULES_CHANGED")

    def test_legacy_narration_gap_routes_once_through_frozen_audio_recovery(self):
        self._check_frozen_rematch("NARRATION_GAP_EXCEEDED")

    def _check_frozen_rematch(self, reason):
        self.write("shot_match_report.json", {"ok": True, "input_manifest": self.manifest})
        updated_timing = {**self.timing, "tts_attempted": 0}
        self.write("narration_timing.json", updated_timing)
        self.snapshot["manifest"]["usage_ledger_path"] = "obsolete/task/.cache/usage.json"
        self.write("selection_resume.json", self.snapshot)
        stages = []
        def run(command, **kwargs):
            stages.append(command[-1])
            if command[-1] == "semantic_match":
                recovered = json.loads(Path(command[command.index("--input") + 1]).read_text(encoding="utf8"))
                self.assertNotIn("usage_ledger_path", recovered)
            if len(stages) == 1:
                return SimpleNamespace(returncode=1, stderr="", stdout=json.dumps({
                    "status": reason, "resume_stage": "semantic_match"}))
            return self.successful_cli(command, **kwargs)
        result = selection_recovery.recover_draft(self.manifest, self.report, prefix=["python"],
            run=run, env={}, cwd=str(ROOT), deadline=time.time()+60, drafts_root=self.drafts)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(stages, ["draft_write", "preflight", "semantic_match", "draft_write"])

    def test_draft_timeout_recovery_uses_only_matched_draft_write(self):
        self.write('last_result.json', {'retryable': True, 'resume_stage': 'draft_write'})
        self.write('shot_match_report.json', {'ok': True, 'input_manifest': self.manifest})
        self.drafts.mkdir(exist_ok=True)
        draft = self.drafts / 'recovered'
        draft.mkdir()
        def completed(command, **kwargs):
            self.assertEqual(command[-2:], ['--stage', 'draft_write'])
            self.write('last_result.json', {'status': 'SUCCESS', 'draft_path': str(draft)})
            return SimpleNamespace(returncode=0, stdout='ok', stderr='')
        with patch.object(worker, 'DRAFTS', self.drafts), patch.object(worker.subprocess, 'run', side_effect=completed) as run:
            result = worker.run_build(self.manifest, self.report, 'requested-name', timeout=60)
        self.assertEqual(result['draft_name'], 'recovered')
        self.assertEqual(run.call_count, 1)

    def test_frozen_refill_timeout_reaches_cli_writer_with_current_name(self):
        from orchestrator.application import cli
        from orchestrator.runtime.file_safety import content_identity
        texts = [f'句子{i}' for i in range(1, 12)]
        self.manifest = {'task_id': 'offline', 'full_script': '，'.join(texts), 'delivery_mode': 'formal',
                         'draft_name': 'first', 'material_sources': [str(self.root / 'source.mp4')]}
        Path(self.manifest['material_sources'][0]).write_bytes(b'source')
        sentences = [{'text': text} for text in texts]
        rows, files = [], []
        for index, sentence in enumerate(sentences, 1):
            audio = self.root / f'audio{index}.wav'; audio.write_bytes(f'audio-{index}'.encode())
            files.append(content_identity(audio))
            rows.append({'key': timing_contract.sentence_key(sentence, index), 'text': sentence['text'],
                         'tts_path': str(audio), 'audio_duration_us': 1_000_000 + index,
                         'timing_certainty': 'audio', 'estimated': False})
        timing = {'rows': rows}
        self.write('narration_timing.json', timing)
        prepared = {**self.manifest, 'segments': sentences, 'narration_timing_file': str(self.report / 'narration_timing.json'),
                    'voice_tempo': 1.0, 'sentence_plan_version': 'v1'}
        self.write('selection_resume.json', {'input_manifest': self.manifest, 'manifest': prepared,
                                             'timing': timing, 'audio_files': files, 'run_id': 'original'})
        enriched = selection_recovery.freeze_refill_input({**self.manifest, 'draft_name': 'old-a2',
                                                           'ai_refill_indices': [2]}, self.report)
        segments = [{**row, 'shot_id': f'source-{i}', 'video': str(self.root / 'source.mp4')}
                    for i, row in enumerate(enriched['segments'], 1)]
        segments[1]['shot_id'] = 'replacement-B'
        matched_manifest = {**enriched, 'segments': segments}
        self.write('shot_match_report.json', {'ok': True, 'input_manifest': enriched,
                                             'manifest': matched_manifest, 'segments': segments,
                                             'delivery_mode': 'formal'})
        self.write('last_result.json', {'retryable': True, 'resume_stage': 'draft_write'})
        captured = []
        class FakeEngine:
            def __init__(inner, manifest, *args, **kwargs): captured.append(copy.deepcopy(manifest))
            def run(inner):
                draft = self.drafts / captured[-1]['draft_name']
                draft.mkdir()
                return {'status': 'SUCCESS', 'draft_path': str(draft)}
        def run_cli(command, **kwargs):
            arg = lambda key: command[command.index(key) + 1]
            args = argparse.Namespace(input=arg('--input'), report_dir=arg('--report-dir'),
                                      stage=arg('--stage'), draft_name=arg('--draft-name'))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = cli.cmd_recover(args)
            return SimpleNamespace(returncode=code, stdout='', stderr='')
        with patch.object(worker, 'DRAFTS', self.drafts), \
             patch.object(worker.subprocess, 'run', side_effect=run_cli) as child, \
             patch.object(cli, '_vision_configuration_gate', return_value=None), \
             patch.object(cli, 'OrchestrationEngine', FakeEngine), \
             patch.object(cli, '_temporary_match') as selection, \
             patch.object(cli, '_pre_synthesize_narration') as tts:
            result = worker.run_build(self.manifest, self.report, 'new-a3', timeout=60)
        self.assertEqual(result['rc'], 0, result)
        child.assert_called_once(); selection.assert_not_called(); tts.assert_not_called()
        writer = captured[0]
        self.assertTrue(writer['draft_name'].startswith('new-a3_recovery_'))
        self.assertFalse(writer['force_overwrite'])
        self.assertFalse(writer['auto_match_shots'])
        self.assertEqual(writer['segments'][1]['shot_id'], 'replacement-B')
        self.assertEqual([r['audio'] for r in writer['segments']], [r['tts_path'] for r in rows])
        self.assertEqual([r['audio_duration_us'] for r in writer['segments']], [r['audio_duration_us'] for r in rows])
        # Frozen audio and original text are both mandatory for the same recovery.
        for mutation in ('text', 'audio'):
            request = copy.deepcopy(self.manifest)
            if mutation == 'text': request['full_script'] += '变化'
            else: Path(rows[0]['tts_path']).write_bytes(b'changed audio')
            blocked = selection_recovery.recover_draft(request, self.report, prefix=['python'], run=Mock(),
                env={}, cwd=str(ROOT), deadline=time.time()+60, drafts_root=self.drafts)
            self.assertNotEqual(blocked['rc'], 0)
            self.assertIn('REFILL_', blocked['stdout'])

    def test_retry_budget_belongs_to_the_selection_generation(self):
        self.write("selection_recovery_state.json", {"attempts": 1, "source_run_id": "old-generation", "status": "RUNNING"})
        self.assertTrue(selection_recovery.pending(self.report))
        run = Mock(side_effect=self.successful_cli)
        self.assertEqual(self.recover(run=run)["rc"], 0)
        self.assertEqual(run.call_count, 3)
        self.snapshot["run_id"] = "another-explicit-build"
        self.write("selection_resume.json", self.snapshot)
        self.write("last_result.json", {"status": "VISION_CALL_FAILED", "retryable": True, "resume_stage": "semantic_match"})
        self.assertTrue(selection_recovery.pending(self.report))
        failed = Mock(return_value=SimpleNamespace(returncode=1, stdout="preflight blocked", stderr=""))
        self.assertNotEqual(self.recover(run=failed)["rc"], 0)
        failed.assert_called_once()
        state = json.loads((self.report / "selection_recovery_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["source_run_id"], "another-explicit-build")

    def test_explicit_force_build_archives_previous_recovery_state(self):
        self.write("selection_recovery_state.json", {"attempts": 1, "source_run_id": "current-attempt", "status": "RUNNING"})
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout="{}", stderr=""))
        with patch.object(worker, "log"), patch.object(worker.subprocess, "run", run):
            worker.run_build(self.manifest, self.report, "requested-name", timeout=1, force_rebuild=True)
        self.assertIn("build", run.call_args.args[0])
        self.assertFalse((self.report / "selection_recovery_state.json").exists())
        self.assertEqual(len(list(self.report.glob("selection_recovery_state.*.json"))), 1)

    def test_exhausted_recovery_is_not_scheduled_as_another_build(self):
        base = {"message": "VISION_CALL_FAILED:DEADLINE_EXCEEDED", "resume_stage": "semantic_match"}
        def receipt(failure):
            return contracts.batch_result_contract("ERROR", task_id="offline", run_id="offline",
                parent_run_id=None, pipeline_version="test", manifest_sha256="test", runtime={}, failures=[failure])
        self.assertTrue(receipt(base)["retryable"])
        stopped = receipt({**base, "stage_retry_exhausted": True})
        self.assertFalse(stopped["retryable"])
        task_dir = self.root / "record"
        task_dir.mkdir()
        (task_dir / "done.json").write_text(json.dumps({"status": "ERROR", **stopped}), encoding="utf-8")
        stage, reason = scheduled._plan({"record_id": "record", "delivery_mode": "formal"}, self.root, {})
        self.assertIsNone(stage)
        self.assertEqual(reason, "FAILED_OR_ATTEMPTED_REQUIRES_RECOVERY")
