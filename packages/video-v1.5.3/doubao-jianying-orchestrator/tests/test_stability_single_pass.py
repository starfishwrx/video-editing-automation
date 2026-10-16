import os
import unittest
from unittest import mock

from orchestrator import stability


def _stderr(points, freeze=(), black=()):
    lines = []
    for i, (t, v) in enumerate(points):
        lines.append(f"[Parsed_metadata_5 @ 0x1] frame:{i} pts:{i} pts_time:{t}")
        lines.append(f"[Parsed_metadata_5 @ 0x1] lavfi.signalstats.YAVG={v}")
    for a, b in freeze:
        lines.append(f"[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: {a}")
        lines.append(f"[freezedetect @ 0x2] lavfi.freezedetect.freeze_duration: {b - a}")
        lines.append(f"[freezedetect @ 0x2] lavfi.freezedetect.freeze_end: {b}")
    for a, b in black:
        lines.append(f"[blackdetect @ 0x3] black_start:{a} black_end:{b} black_duration:{b - a}")
    return "\n".join(lines)


class SinglePassTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"JY_STABILITY_SINGLE_PASS": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def _fake(self, stderr, returncode=0):
        def run(cmd, **kwargs):
            self.calls.append(list(cmd))
            return mock.Mock(returncode=returncode, stdout="", stderr=stderr)
        return mock.patch.object(stability, "run_ffmpeg_decode", side_effect=run)

    def test_analyze_span_offsets_all_ranges(self):
        text = _stderr([(0, 0.0), (0.25, 3.5)], freeze=[(0.5, 1.0)], black=[(1.2, 1.5)])
        with self._fake(text):
            result = stability.analyze_span("a.mov", 2.0, 3.0, ffmpeg="ffmpeg")
        self.assertEqual(result["points"], [(2.0, 0.0), (2.25, 3.5)])
        self.assertEqual(result["frozen"], [(2.5, 3.0)])
        self.assertEqual(result["black"], [(3.2, 3.5)])
        cmd = self.calls[0]
        self.assertEqual(cmd.count("-map"), 3)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("split=3", graph)
        self.assertIn("blackdetect=d=0.1:pix_th=0.10:pic_th=0.985", graph)
        self.assertIn("freezedetect=n=-60dB:d=0.35", graph)

    def test_unterminated_freeze_runs_to_span_end(self):
        text = _stderr([(0, 0.0)], freeze=[])
        text += "\n[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 1.5"
        with self._fake(text):
            result = stability.analyze_span("a.mov", 1.0, 2.0)
        self.assertEqual(result["frozen"], [(2.5, 3.0)])

    def test_failed_decode_returns_none(self):
        with self._fake("boom", returncode=1):
            self.assertIsNone(stability.analyze_span("a.mov", 0.0, 2.0))

    def test_window_black_needs_one_tenth_second_overlap(self):
        black = [(3.0, 3.5)]
        self.assertTrue(stability.window_has_black(black, 3.4, 2.0))
        self.assertFalse(stability.window_has_black(black, 3.45, 2.0))
        self.assertFalse(stability.window_has_black(black, 1.0, 1.95))
        self.assertTrue(stability.window_has_black(black, 1.0, 2.1))

    def _points(self, lo, span):
        steps = int(span / 0.25) + 1
        return [(round(i * 0.25, 2), 0.0 if i == 0 else 1.0) for i in range(steps)]

    def test_find_window_uses_one_decode_and_avoids_black(self):
        lo, span = 2.2, 3.6
        text = _stderr(self._points(lo, span), black=[(0.8, 1.3)])
        with self._fake(text):
            window = stability.find_stable_window("a.mov", 2.0, requested_start=3.0,
                                                  video_duration=10.0, ffmpeg="ffmpeg")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(stability.window_has_black([(3.0, 3.5)], window.source_start, 2.0))
        self.assertIn("黑帧", window.reason)

    def test_falls_back_to_separate_passes_when_single_pass_fails(self):
        def run(cmd, **kwargs):
            self.calls.append(list(cmd))
            if "-filter_complex" in cmd:
                return mock.Mock(returncode=1, stdout="", stderr="split failed")
            return mock.Mock(returncode=0, stdout="",
                             stderr=_stderr(self._points(2.2, 3.6)))
        with mock.patch.object(stability, "run_ffmpeg_decode", side_effect=run):
            stability.find_stable_window("a.mov", 2.0, requested_start=3.0,
                                         video_duration=10.0, ffmpeg="ffmpeg")
        self.assertGreaterEqual(len(self.calls), 4)
        self.assertTrue(any("blackdetect" in " ".join(c) and "-filter_complex" not in c
                            for c in self.calls))

    def test_switch_off_keeps_legacy_path(self):
        with mock.patch.dict(os.environ, {"JY_STABILITY_SINGLE_PASS": "0"}), \
                self._fake(_stderr(self._points(2.2, 3.6))):
            stability.find_stable_window("a.mov", 2.0, requested_start=3.0,
                                         video_duration=10.0, ffmpeg="ffmpeg")
        self.assertFalse(any("-filter_complex" in c for c in self.calls))


if __name__ == "__main__":
    unittest.main()
