"""Committed path boundary tests with real git diffs, including both rename sides."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import check_repository_scope as scope


class ScopePolicyTests(unittest.TestCase):
    def test_active_experiments_and_docs_are_allowed(self):
        self.assertEqual(scope.check(['README.md', 'docs/history.md',
            'packages/video-v1.5.3/tools/x.py', 'packages/video-v1.5.4/index.py',
            'packages/video-v1.5.5/index.py']), [])

    def test_history_vendor_and_new_copies_are_blocked(self):
        for path, reason in [
            ('packages/video-v1.5.2/a.py', 'HISTORICAL_PACKAGE_READ_ONLY'),
            ('packages/video-v1.4.0/a.py', 'HISTORICAL_PACKAGE_READ_ONLY'),
            ('packages/video-v1.5.3/vendor/a.py', 'VENDOR_CHANGE_REQUIRES_POLICY_REVIEW'),
            ('packages/video-v2.0.0/a.py', 'NEW_PACKAGE_ROOT_REQUIRES_POLICY_REVIEW'),
            ('packages/current-copy/a.py', 'NEW_PACKAGE_ROOT_REQUIRES_POLICY_REVIEW'),
            ('PACKAGES/VIDEO-V1.5.2/a.py', 'HISTORICAL_PACKAGE_READ_ONLY'),
        ]:
            with self.subTest(path=path):
                self.assertTrue(any(item.startswith(reason + ':') for item in scope.check([path])))

    def test_case_aliases_of_allowed_package_roots_are_blocked(self):
        for path in ['Packages/video-v1.5.3/a.py', 'packages/VIDEO-V1.5.3/a.py',
                     'Packages/VIDEO-V1.5.3/a.py', 'packages/Video-v1.5.4/a.py',
                     'PACKAGES/video-v1.5.5/a.py']:
            with self.subTest(path=path):
                self.assertIn('NONCANONICAL_PACKAGE_PATH:' + path, scope.check([path]))

    def test_malformed_paths_fail(self):
        for path in ['/etc/test', '../test', 'packages\\video-v1.5.2\\a.py']:
            self.assertTrue(scope.check([path]))

    def test_bad_or_missing_commits_fail_closed(self):
        for sha in ['main', '--help', '0' * 40, 'f' * 40]:
            with self.subTest(sha=sha), patch.object(scope, 'REPO', Path(tempfile.gettempdir())):
                self.assertEqual(scope.main(['--base=' + sha, '--head', 'a' * 40]), 1)


class ScopeGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'scope-test@example.invalid')
        self.git('config', 'user.name', 'Scope fixture')
        self.write('packages/video-v1.5.2/old file.py', 'old')
        self.write('packages/video-v1.5.3/current.py', 'current')
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args]).decode().strip()

    def write(self, path, value):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding='utf-8')

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def test_no_changes_is_valid(self):
        self.assertEqual(scope.changed_paths(self.repo, self.base, self.base), [])

    def test_rename_out_of_history_includes_deleted_source(self):
        self.git('mv', 'packages/video-v1.5.2/old file.py', 'packages/video-v1.5.3/moved.py')
        paths = scope.changed_paths(self.repo, self.base, self.commit())
        self.assertIn('packages/video-v1.5.2/old file.py', paths)
        self.assertTrue(scope.check(paths))

    def test_rename_into_vendor_includes_destination(self):
        self.write('packages/video-v1.5.3/vendor/moved.py', 'current')
        (self.repo / 'packages/video-v1.5.3/current.py').unlink()
        self.assertTrue(scope.check(scope.changed_paths(self.repo, self.base, self.commit())))

    def test_utf8_and_spaces_are_not_split(self):
        path = 'packages/video-v1.5.2/中文 文件.py'
        self.write(path, 'new')
        self.assertEqual(scope.changed_paths(self.repo, self.base, self.commit()), [path])

    def test_pr_uses_merge_base_and_does_not_charge_target_only_changes(self):
        self.git('checkout', '-qb', 'target')
        self.write('packages/video-v1.5.2/old file.py', 'target-only')
        target = self.commit()
        self.git('checkout', '-qb', 'topic', self.base)
        self.write('README.md', 'topic')
        head = self.commit()
        paths = scope.changed_paths(self.repo, target, head, pull_request=True)
        self.assertEqual(paths, ['README.md'])
        self.assertEqual(scope.check(paths), [])

    def test_ci_merge_head_excludes_target_only_history(self):
        self.git('checkout', '-qb', 'target')
        self.write('packages/video-v1.5.2/old file.py', 'target-only')
        target = self.commit()
        self.git('checkout', '-qb', 'topic', self.base)
        self.write('README.md', 'topic')
        self.commit()
        self.git('checkout', 'target')
        self.git('merge', '--no-ff', '-m', 'synthetic PR merge', 'topic')
        merge = self.git('rev-parse', 'HEAD')
        self.assertEqual(scope.changed_paths(self.repo, target, merge, pull_request=True), ['README.md'])

    def test_missing_full_sha_in_existing_repository_fails(self):
        with self.assertRaises(ValueError):
            scope.changed_paths(self.repo, 'f' * 40, self.base)
        with patch.object(scope, 'REPO', self.repo):
            self.assertEqual(scope.main(['--base', self.base, '--head', 'f' * 40]), 1)

    def test_push_and_manual_explicit_base_cover_all_commits(self):
        self.write('packages/video-v1.5.2/old file.py', 'forbidden first commit')
        self.commit()
        self.write('README.md', 'allowed later commit')
        head = self.commit()
        paths = scope.changed_paths(self.repo, self.base, head)
        self.assertIn('packages/video-v1.5.2/old file.py', paths)
        with patch.object(scope, 'REPO', self.repo):
            self.assertEqual(scope.main(['--base', self.base, '--head', head]), 1)
            self.assertEqual(scope.main(['--base', head, '--head', head]), 0)

    def test_unrelated_history_is_an_error(self):
        self.git('checkout', '--orphan', 'unrelated')
        self.git('rm', '-rf', '.')
        self.write('other.txt', 'other')
        head = self.commit()
        with self.assertRaises(ValueError):
            scope.changed_paths(self.repo, self.base, head, pull_request=True)


if __name__ == '__main__':
    unittest.main()
