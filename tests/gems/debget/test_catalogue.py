from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from dylathleen.gems.debget import catalogue, privileged as helper

SOURCE = '''DEFVER=1
PRETTY_NAME="Example"
ARCHS_SUPPORTED="amd64 arm64"
CODENAMES_SUPPORTED="jammy noble"
WEBSITE="https://example.invalid"
SUMMARY="An example; no execution"
touch /untrusted/must-not-run
VERSION_PUBLISHED="$(unsafe-shell)"
get_github_releases "owner/project" latest
'''


class CatalogueTest(TestCase):
    def test_local_override_and_official_recipe_are_both_visible(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '01-main.d').mkdir()
            (root / '99-local.d').mkdir()
            (root / '01-main.repo').write_text('https://example.invalid/repo\nexample\n#disabled\n')
            (root / '01-main.d/example').write_text(SOURCE)
            (root / '01-main.d/disabled').write_text(SOURCE)
            (root / '99-local.d/example').write_text(SOURCE.replace('Example', 'Local Example'))
            (root / '99-local.d/external').symlink_to(root / '01-main.d/example')
            with mock.patch('subprocess.run', side_effect=AssertionError('catalogue tried to run code')):
                recipes = catalogue.read_catalogue(root)
            local, official = recipes['example']
            self.assertEqual([local.repo, official.repo], ['99-local', '01-main'])
            self.assertEqual(local.fields['PRETTY_NAME'], 'Local Example')
            self.assertNotIn('VERSION_PUBLISHED', local.fields)
            self.assertTrue(recipes['disabled'][0].deprecated)
            self.assertNotIn('external', recipes)
            self.assertTrue(local.compatible('arm64', 'jammy', 'ubuntu'))
            self.assertFalse(local.compatible('arm64', 'bookworm', 'debian'))

    def test_registry_only_contains_valid_native_entries(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'installed').write_text('good 1 github\nbad 1 unsupported\n../evil 1 direct\n')
            self.assertEqual(helper.registered(root), {'good': 'github'})

    def test_save_backup_restore_and_delete_does_not_source_or_remove_package(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'deb-get'
            recipe = root / '99-local.d/example'
            sentinel = Path(directory) / 'executed'
            previous = f'DEFVER=1\ntouch "{sentinel}"\n'
            revised = 'DEFVER=2\n'
            with mock.patch.object(helper, 'ROOT', root), mock.patch.object(helper, 'secure', side_effect=lambda path,
                            **kwargs: path.exists()):
                helper.change_recipe({'operation': 'save', 'name': 'example', 'source': previous, 'expected': None})
                ledger = root / 'installed'
                ledger.write_text('example 1 direct\n')
                helper.change_recipe({'operation': 'save', 'name': 'example', 'source': revised,
                                      'expected': helper.hash_text(previous)})
                helper.change_recipe({'operation': 'restore', 'name': 'example', 'revision': helper.hash_text(previous),
                                      'expected': helper.hash_text(revised)})
                self.assertEqual(recipe.read_text(), previous)
                helper.change_recipe({'operation': 'delete', 'name': 'example', 'expected': helper.hash_text(previous)})
                self.assertFalse(recipe.exists())
                self.assertFalse(sentinel.exists())
                self.assertEqual(ledger.read_text(), 'example 1 direct\n')

    def test_shell_syntax_error_leaves_old_recipe_in_place(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'deb-get'
            recipe = root / '99-local.d/example'
            recipe.parent.mkdir(parents=True)
            recipe.write_text(SOURCE)
            with mock.patch.object(helper, 'ROOT', root), mock.patch.object(helper, 'secure', side_effect=lambda path,
                            **kwargs: path.exists()):
                with self.assertRaises(helper.DebGetError):
                    helper.change_recipe({'operation': 'save', 'name': 'example', 'source': 'if [ broken\n',
                                          'expected': helper.hash_text(SOURCE)})
            self.assertEqual(recipe.read_text(), SOURCE)

    def test_privileged_path_rejects_symlink(self):
        for name in ('../bad', '/etc/passwd', '-leading', 'with spaces'):
            with self.subTest(name=name), self.assertRaises(helper.DebGetError):
                helper.package(name)
        with TemporaryDirectory() as directory:
            link = Path(directory) / 'link'
            link.symlink_to('/etc/passwd')
            with self.assertRaises(helper.DebGetError):
                helper.recipe_text(link)
