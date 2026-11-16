import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import ai_editor_contract as contract
from orchestrator.selection import ai_editor, evidence_recall
from orchestrator.vision import evidence_store as store, evidence_import, vision_analyzer as vision


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"JY_MATERIAL_EVIDENCE_DB":str(self.root/"db"),
                                          "JY_VISION_TRANSPORT":"qwen","JY_QWEN_MODEL":"mock",
                                          "JY_QWEN_BASE_URL":"https://unused.invalid"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path=self.root/"a.mov"
        self.path.write_bytes(b"test-content")
        self.proof=self.root/"qc.json"
        self.proof.write_bytes(b"verified fixture")
        self.manifest={"product":"paper","category":"zhipin","sku":"paper-1"}
        self.shot={"video":str(self.path),"shot_id":"one","source_start":0,"source_end":8}
        self.original="揉搓不掉絮"
        self.facts={"basis":"visible_result","observation":"双手拿取一张白色纸巾进行揉搓、挤压动作，随后将纸巾展开平铺展示，纸巾表面完整，未见明显毛絮或破损。",
                    "report_sha256":store.identity(self.proof),"same_product":True,"verdict":"matched","confidence":.95,
                    "claims":[{"id":"old","text":self.original}],"claim_context":self.original,
                    "scene_ids":[],"time_sensitive":False,"evidence_intervals_us":[{"start":2000000,"end":5000000}]}

    def add(self):
        with store.connect() as db:
            store.put(db,store.scope(self.manifest),store.identity(self.path),self.path,2000000,5000000,
                      "qc",store.protocol(self.manifest),self.facts,self.proof)

    def sentence(self,text):
        return {"index":1,"text":text,"selling_points":[{"id":"current","text":text}]}

    def test_new_wording_current_path_and_missing_condition(self):
        self.add()
        renamed=self.root/"b.mov"
        self.path.rename(renamed)
        shot={**self.shot,"video":str(renamed)}
        with patch.object(ai_editor,"_call",side_effect=AssertionError("unexpected model")):
            hit=ai_editor._first_look_review(self.manifest,self.sentence("揉过再展开，纸面还是完整的"),shot,self.root)
        self.assertEqual(hit["supported_point_ids"],["current"])
        self.assertEqual(hit["source_identity"],contract.source_identity(renamed))
        self.assertEqual(hit["review_window_us"],{"start":2000000,"end":5000000})
        result,hint=evidence_recall.inspect(self.manifest,self.sentence("湿水后揉搓不掉絮"),shot)
        self.assertIsNone(result)
        self.assertIn("湿水",hint[1])
        self.assertEqual(store.windows({**self.manifest,"sku":"different"},shot),[])
        renamed.write_bytes(b"different-content")
        self.assertEqual(store.windows(self.manifest,shot),[])

    def test_failed_draft_scope_rule_and_damaged_proof(self):
        self.add()
        self.assertIsNone(evidence_recall.inspect(self.manifest,
            {**self.sentence(self.original),"qc_feedback":{"failed_windows":[1]}},self.shot)[0])
        self.proof.write_bytes(b"changed")
        self.assertIsNone(evidence_recall.inspect(self.manifest,self.sentence(self.original),self.shot)[0])
        self.assertEqual(evidence_import.import_qc(self.manifest,{"status":"FAIL"},self.proof),0)

    def test_idempotence_with_concurrent_producers(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _:self.add(),range(12)))
        self.assertEqual(len(store.windows(self.manifest,self.shot)),1)

    def test_reversed_or_negated_actions_cannot_answer(self):
        self.facts["observation"]="先展开纸巾，接着揉搓成团，纸巾完整，未再展开"
        self.add()
        answer, _ = evidence_recall.inspect(self.manifest,self.sentence("揉过再展开，纸面还是完整的"), self.shot)
        self.assertIsNone(answer)

    def test_independent_ocr_version(self):
        with patch.dict(os.environ,{"JY_VISION_TEXT_PASS":"1"}):
            row={"action_complete":True,"text_pass":"hires","ocr_protocol":vision._ocr_protocol()}
            self.assertFalse(vision._needs_text_backfill(row))
            with patch.dict(os.environ,{"JY_QWEN_MODEL":"changed"}):
                self.assertTrue(vision._needs_text_backfill(row))

