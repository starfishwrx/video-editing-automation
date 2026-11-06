"""Default worker recovery must reuse frozen audio and never restart a full build."""
import copy
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
        self.audio.write_bytes(b'changed audio')
        with self.assertRaisesRegex(ValueError, 'REFILL_FROZEN_AUDIO_CHANGED'):
            selection_recovery.freeze_refill_input(request, self.report)

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
