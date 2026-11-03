"""User-visible wrapping, centering and safe bounds without changing cue timing."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.media.caption_layout import layout_subtitle
from orchestrator.media.timeline_render import subtitle_ass
from orchestrator.domain.timeline import Canvas, EditTimeline, SubtitleEvent


class CaptionLayoutTests(unittest.TestCase):
    def assert_safe(self, layout):
        left, top, right, bottom = layout['bounds']
        sl, st, sr, sb = layout['safe_rect']
        self.assertGreaterEqual(left, sl)
        self.assertGreaterEqual(top, st)
        self.assertLessEqual(right, sr)
        self.assertLessEqual(bottom, sb)

    def test_long_chinese_caption_wraps_balanced_and_centered_without_text_loss(self):
        text = '但心相印的新品东方纸上抽纸真的很难不爱啊'
        row = layout_subtitle(text, 1080, 1920)
        self.assertGreater(len(row['lines']), 1)
        self.assertEqual(row['text'].replace('\n', ''), text)
        self.assertEqual(row['alignment'], 1)
        self.assertAlmostEqual(row['x'], 0)
        self.assertAlmostEqual(row['y'], -.55)
        self.assert_safe(row)

    def test_wide_latin_and_cjk_are_measured_differently(self):
        narrow = layout_subtitle('i' * 28, 1080, 1920)
        wide = layout_subtitle('W' * 28, 1080, 1920)
        self.assertLess(len(narrow['lines']), len(wide['lines']))
        self.assert_safe(wide)

    def test_complete_subject_and_predicate_beat_balanced_widths(self):
        row = layout_subtitle('但心相印的新品东方纸上抽纸真的很难不爱啊', 1080, 1920)
        self.assertEqual(row['lines'], ['但心相印的新品东方纸上抽纸', '真的很难不爱啊'])
        self.assert_safe(row)

    def test_unseen_product_names_are_not_a_one_example_exception(self):
        for name in ('晨曦柔云', '星河竹韵'):
            row = layout_subtitle(f'但心相印的新品{name}抽纸真的很难不爱啊', 1080, 1920)
            self.assertIn(name + '抽纸', row['text'])
            self.assertEqual(row['lines'][-1], '真的很难不爱啊')
            self.assert_safe(row)

    def test_english_brand_and_effect_phrase_stay_intact(self):
        row = layout_subtitle('Hello Kitty联名的云感洗脸巾用起来柔软又舒服', 1080, 1920)
        self.assertEqual(row['lines'], ['Hello Kitty联名的云感洗脸巾', '用起来柔软又舒服'])
        row = layout_subtitle('一次抽取一张不会连抽用起来特别省心', 1080, 1920)
        self.assertIn('用起来特别省心', row['text'])
        self.assert_safe(row)

    def test_relative_clause_stays_with_its_product_noun(self):
        text = '是哪个大冤种还在用那种擦完脸就泛红的杂牌洗脸巾啊'
        row = layout_subtitle(text, 1080, 1920)
        self.assertIn('擦完脸就泛红的杂牌洗脸巾啊', row['lines'])
        self.assertEqual(row['text'].replace('\n', ''), text)
        self.assert_safe(row)

    def test_numeric_specification_and_supplied_name_are_never_split_when_they_fit(self):
        text = '新上市星辉御柔洗脸巾足足200抽用起来特别省心'
        row = layout_subtitle(text, 720, 1280, protected_terms=('星辉御柔',))
        for term in ('星辉御柔', '洗脸巾', '200抽', '用起来特别省心'):
            self.assertIn(term, row['text'])
        self.assertEqual(row['text'].replace('\n', ''), text)
        self.assert_safe(row)

    def test_existing_punctuation_is_a_preferred_sentence_boundary(self):
        text = '这款洗脸巾柔软亲肤，吸水性也很好'
        row = layout_subtitle(text, 1080, 1920)
        self.assertEqual(row['lines'], ['这款洗脸巾柔软亲肤，', '吸水性也很好'])
        self.assert_safe(row)

    def test_intro_quantified_feature_and_action_result_are_complete(self):
        cases = [('关键是没有荧光增白剂给宝宝用也放心', ['关键是没有荧光增白剂', '给宝宝用也放心']),
                 ('一包有120抽四层加厚一箱足足有24包', ['一包有120抽四层加厚', '一箱足足有24包']),
                 ('把水倒上去很快就吸收了按压也不反渗', ['把水倒上去很快就吸收了', '按压也不反渗']),
                 ('这种柔软又厚实的抽纸擦完脸也不掉絮', ['这种柔软又厚实的抽纸', '擦完脸也不掉絮'])]
        for text, lines in cases:
            with self.subTest(text=text):
                row = layout_subtitle(text, 1080, 1920)
                self.assertEqual(row['lines'], lines)
                self.assert_safe(row)

    def test_emoji_modifiers_flags_and_joined_sequences_are_not_split(self):
        for emoji in ('👍🏻', '🇨🇳', '👩\u200d👩\u200d👧\u200d👦'):
            row = layout_subtitle('柔软' * 5 + emoji + '亲肤不掉絮亲肤不掉絮', 1080, 1920)
            self.assertIn(emoji, row['text'])
            self.assert_safe(row)

    def test_multiline_near_edge_is_clamped_in_both_orientations(self):
        for width, height in ((1080, 1920), (1920, 1080)):
            row = layout_subtitle('测试字幕\n第二行字幕\n第三行字幕', width, height, x=1, y=-1)
            self.assert_safe(row)
            self.assertEqual(row['text'], '测试字幕\n第二行字幕\n第三行字幕')

    def test_english_words_and_combining_characters_are_preserved(self):
        text = 'Hello Kitty 联名洗脸巾 A\u0301 natural cotton soft texture'
        row = layout_subtitle(text, 1080, 1920)
        self.assertEqual(row['text'].replace('\n', ''), text)
        self.assertNotIn('Hell\no', row['text'])
        self.assertNotIn('A\n\u0301', row['text'])
        self.assert_safe(row)

    def test_ass_has_explicit_wrapping_and_keeps_exact_original_timestamps(self):
        text = '但心相印的新品东方纸上抽纸真的很难不爱啊'
        cue = SubtitleEvent('caption', 's1', text, 1230000, 2500000)
        timeline = EditTimeline('layout', 1, Canvas(1080, 1920), 4000000, (), (), (), (), (cue,))
        ass = subtitle_ass(timeline, 540, 960)
        self.assertIn('0:00:01.23,0:00:03.73', ass)
        self.assertIn('\\N', ass)
        self.assertIn('\\pos(270.0000,744.0000)', ass)
        self.assertEqual(timeline.subtitles[0], cue)

    def test_invalid_font_and_unreadably_large_text_do_not_silently_overflow(self):
        with self.assertRaisesRegex(ValueError, 'FONT_UNAVAILABLE'):
            layout_subtitle('test', 1080, 1920, font_path='missing.ttf')
        with self.assertRaisesRegex(ValueError, 'TOO_LONG'):
            layout_subtitle('字' * 80, 300, 100, font_size_px=12)



class LegacyCaptionLayoutTests(unittest.TestCase):
    def test_long_plain_and_large_rich_captions_roundtrip_inside_safe_bounds(self):
        import tempfile
        import json
        from copy import deepcopy
        editor = str(Path(__file__).resolve().parents[2] / 'jianying-editor/scripts')
        sys.path.insert(0, editor)
        from jy_wrapper import JyProject
        from orchestrator.quality.draft_safety import _subtitle_style_issues
        for rich in (False, True):
            with self.subTest(rich=rich), tempfile.TemporaryDirectory() as raw:
                project = JyProject('layout', width=1080, height=1920, drafts_root=raw)
                text = '中' * 100 + '高亮' + '文' * 100
                if rich:
                    # Use the supported lower-level explicit span style.
                    import pyJianYingDraft as draft
                    seg = project.add_text_simple(text, rich_spans=[draft.RichTextSpan(100,102,size=20)])
                else:
                    seg = project.add_text_simple(text)
                material = seg.export_material()
                content = json.loads(material['content'])
                self.assertEqual(content['text'].replace('\n',''), text)
                self.assertIn('\n', content['text'])
                self.assertEqual(material['alignment'], 1)
                self.assertGreater(seg.clip_settings.transform_y, -.55)
                expected = {'subtitle_style_required': True, 'subtitle_layout': project._subtitle_layouts}
                segment = {'clip': {'transform': {'x': seg.clip_settings.transform_x,
                                                   'y': seg.clip_settings.transform_y}}}
                self.assertEqual(_subtitle_style_issues(material,segment,1,expected), [])
                if rich:
                    a,b = content['styles'][0]['range']
                    self.assertEqual(content['text'][a:b].replace('\n',''), '高亮')
                bad = deepcopy(material)
                bad['alignment'] = 0
                self.assertTrue(_subtitle_style_issues(bad,segment,1,expected))
                project.save()

if __name__ == '__main__':
    unittest.main()
