from dataclasses import replace
import unittest

import test_experiment_pairs as pair_fixture
import test_weipin_experiment as single_fixture
from orchestrator import ai_editor_contract as identity


def add_outside_placeholder(base):
    path = base.root / 'diagnostic-black.mp4'
    path.write_bytes(b'generated diagnostic fixture')
    asset = replace(base.timeline.assets[0], asset_id='diagnostic', path=str(path))
    old = base.timeline.videos[-1]
    event = replace(old, asset_id='diagnostic', evidence=({'diagnostic_placeholder': True},))
    issue = {'sentence_id': old.sentence_ids[0],
        'reason': 'NO_CONFIRMED_FIT_WITHIN_SEARCH_BUDGET; black is diagnostic only'}
    base.timeline = replace(base.timeline, assets=base.timeline.assets + (asset,),
        videos=base.timeline.videos[:-1] + (event,), unresolved=base.timeline.unresolved + (issue,))
    origin = {'asset_id': asset.asset_id, 'original': identity.source_identity(path),
        'proxy': identity.source_identity(path), 'clock': 'same_source_clock'}
    return event, issue, origin


class ExperimentPlaceholderTests(unittest.TestCase):
    def single(self):
        fixture = single_fixture.BoundExperimentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def paired(self):
        fixture = pair_fixture.PairApplicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_single_source_repair_keeps_unrelated_placeholder_and_unresolved_status(self):
        for supplied_origin in (False, True):
            with self.subTest(supplied_origin=supplied_origin):
                fixture = self.single()
                event, issue, origin = add_outside_placeholder(fixture)
                if supplied_origin:
                    fixture.origins.append(origin)
                updated = fixture.apply()
                self.assertIn(event, updated.videos)
                self.assertIn(issue, updated.unresolved)
                self.assertEqual(updated.audios, fixture.timeline.audios)

    def test_paired_repair_accepts_omitted_or_generated_placeholder_origin(self):
        for supplied_origin in (False, True):
            with self.subTest(supplied_origin=supplied_origin):
                fixture = self.paired()
                event, issue, origin = add_outside_placeholder(fixture.base)
                if supplied_origin:
                    fixture.base.origins.append(origin)
                updated = fixture.apply()
                self.assertIn(event, updated.videos)
                self.assertIn(issue, updated.unresolved)
                self.assertEqual(updated.delivery_mode, 'preview')

    def test_unknown_footage_is_not_exempt_just_because_it_has_one_placeholder_hint(self):
        for missing in ('marker', 'unresolved'):
            with self.subTest(missing=missing):
                fixture = self.paired()
                event, issue, origin = add_outside_placeholder(fixture.base)
                if missing == 'marker':
                    fixture.base.timeline = replace(fixture.base.timeline,
                        videos=fixture.base.timeline.videos[:-1] + (replace(event, evidence=()),))
                else:
                    fixture.base.timeline = replace(fixture.base.timeline, unresolved=())
                with self.assertRaisesRegex(ValueError, 'EXISTING_ORIGIN_UNKNOWN'):
                    fixture.apply()

    def test_invalid_explicit_placeholder_identity_still_blocks(self):
        fixture = self.paired()
        event, issue, origin = add_outside_placeholder(fixture.base)
        origin['proxy']['size'] += 1
        fixture.base.origins.append(origin)
        with self.assertRaisesRegex(ValueError, 'PRIOR_ORIGIN_BINDING_INVALID'):
            fixture.apply()

    def test_one_marked_event_cannot_hide_unknown_shared_real_source(self):
        fixture = self.paired()
        old = fixture.base.timeline
        event = replace(old.videos[-1], evidence=({'diagnostic_placeholder': True},))
        issue = {'sentence_id': event.sentence_ids[0],
            'reason': 'NO_CONFIRMED_FIT_WITHIN_SEARCH_BUDGET; black is diagnostic only'}
        fixture.base.timeline = replace(old, videos=old.videos[:-1] + (event,), unresolved=(issue,))
        fixture.base.origins = []
        with self.assertRaisesRegex(ValueError, 'EXISTING_ORIGIN_UNKNOWN'):
            fixture.apply()


if __name__ == '__main__':
    unittest.main()
