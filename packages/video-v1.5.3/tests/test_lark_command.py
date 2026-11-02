"""Windows JSON argv is preserved without a cmd/bat command-string round trip."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import shutil

from tools.lark_command import command, actor


class LarkArgvTests(unittest.TestCase):
    def test_actor_can_use_the_verified_user_identity_without_changing_argv(self):
        with patch.dict(os.environ, {'LARK_ACTOR':'user'}):
            self.assertEqual(actor(),'user')
        with patch.dict(os.environ, {'LARK_ACTOR':'invalid'}):
            with self.assertRaisesRegex(RuntimeError,'LARK_ACTOR_INVALID'):
                actor()
    @unittest.skipUnless(os.name=='nt' and shutil.which('node.exe'),'Windows Node argv probe')
    def test_official_node_entry_keeps_unicode_quotes_and_newlines(self):
        with tempfile.TemporaryDirectory(prefix='lark argv 中文 ') as raw:
            root=Path(raw);shim=root/'lark-cli.cmd';shim.write_text('must never execute')
            entry=root/'node_modules/@larksuite/cli/scripts/run.js'
            entry.parent.mkdir(parents=True)
            entry.write_text('process.stdout.write(JSON.stringify(process.argv.slice(2)));',encoding='utf-8')
            payload={'update_records':{'fixture':{'BGM名称':'A "曲名"\n第二行','问题反馈':'literal $(x) `test`'}}}
            args=['base','+record-batch-update','--json',json.dumps(payload,ensure_ascii=False)]
            argv=command(args,executable=str(shim),profile='fixture')
            self.assertEqual(Path(argv[0]).name.lower(),'node.exe')
            result=subprocess.run(argv,capture_output=True,text=True,encoding='utf-8',check=True)
            actual=json.loads(result.stdout)
            self.assertEqual(actual[:2],['--profile','fixture'])
            self.assertEqual(actual[2:],args)
            self.assertEqual(json.loads(actual[-1]),payload)

    @unittest.skipUnless(os.name=='nt','Windows npm shim')
    def test_missing_node_entry_is_rejected_before_cli_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(RuntimeError,'LARK_NODE_ENTRY_REQUIRED'):
                command(['base'],executable=str(Path(raw)/'lark-cli.cmd'))
