import hashlib
import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from dylathleen.gems.bin import registry


def make_entry(path, payload=b'working binary'):
    return {'path': str(path), 'version': 'v1.0.0', 'url': 'https://github.com/example/tool',
            'hash': hashlib.sha256(payload).hexdigest(), 'package_path': 'archive/tool',
            'selected_asset': 'tool-linux.tar.gz', 'pinned': True}


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.source = self.root / 'old' / 'tool'
        self.source.parent.mkdir()
        self.source.write_bytes(b'working binary')
        self.source.chmod(0o751)
        self.unrelated = self.source.with_name('unrelated')
        self.unrelated.touch(mode=0o600)
        self.destination = self.root / 'new'
        self.entry = make_entry(self.source)
        self.previous = {'default_path': str(self.source.parent), 'bins': {str(self.source): self.entry}}
        registry.write_json(self.config, self.previous)

    def migrate(self):
        return registry.transact(self.config, {'operation': 'migrate', 'directory': str(self.destination)})

    def test_move_only_registered_file_and_preserve_metadata(self):
        result = self.migrate()
        installed = self.destination / self.source.name
        self.assertEqual(result, registry.read_registry(self.config))
        self.assertEqual({**self.entry, 'path': str(installed)}, result['bins'][str(installed)])
        self.assertEqual(result['default_path'], str(self.destination))
        self.assertEqual(installed.read_bytes(), b'working binary')
        self.assertEqual(installed.stat().st_mode & 0o777, 0o751)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.unrelated.stat().st_mode & 0o777, 0o600)

    def test_collision_with_dangling_symlink_leaves_source_and_registry(self):
        self.destination.mkdir()
        target = self.destination / self.source.name
        target.symlink_to(self.destination / 'absent')
        with self.assertRaises(registry.BinError):
            self.migrate()
        self.assertTrue(target.is_symlink())
        self.assertTrue(self.source.exists())
        self.assertEqual(self.previous, registry.read_registry(self.config))

    def test_registry_failure_rolls_back_copy(self):
        with patch.object(registry, 'write_json', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.migrate()
        self.assertTrue(self.source.exists())
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(self.previous, registry.read_registry(self.config))

    def test_unlink_failure_restores_registry_and_any_already_removed_file(self):
        second = self.source.with_name('tool2')
        second.write_bytes(self.source.read_bytes())
        self.previous['bins'][str(second)] = make_entry(second)
        registry.write_json(self.config, self.previous)
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == second:
                raise PermissionError('source unlink failed')
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', unlink):
            with self.assertRaisesRegex(PermissionError, 'source unlink failed'):
                self.migrate()
        self.assertTrue(self.source.exists())
        self.assertTrue(second.exists())
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(self.previous, registry.read_registry(self.config))

    def test_install_repairs_only_the_installed_file(self):
        target = self.source.with_name('new-tool')
        entry = make_entry(target, b'new binary')
        request = {'operation': 'entry', 'key': str(target), 'expected': None, 'entry': entry,
                   'directory': str(target.parent), 'mode': 0o644}
        registry.transact(self.config, request, io.BytesIO(b'new binary'))
        self.assertTrue(os.access(target, os.X_OK))
        self.assertEqual(self.unrelated.stat().st_mode & 0o777, 0o600)

    def test_failed_update_keeps_old_executable(self):
        entry = make_entry(self.source, b'new binary')
        request = {'operation': 'entry', 'key': str(self.source), 'expected': self.entry, 'entry': entry,
                   'directory': str(self.source.parent), 'mode': 0o644}
        with patch.object(registry, 'write_json', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                registry.transact(self.config, request, io.BytesIO(b'new binary'))
        self.assertEqual(self.source.read_bytes(), b'working binary')
        self.assertTrue(os.access(self.source, os.X_OK))
        self.assertEqual(self.previous, registry.read_registry(self.config))

    def test_refuses_to_remove_a_replaced_file(self):
        self.source.write_bytes(b'unrelated content')
        request = {'operation': 'entry', 'key': str(self.source), 'expected': self.entry, 'entry': None,
                   'directory': str(self.source.parent)}
        with self.assertRaisesRegex(registry.BinError, 'differs'):
            registry.transact(self.config, request)
        self.assertEqual(self.source.read_bytes(), b'unrelated content')
        self.assertEqual(self.previous, registry.read_registry(self.config))

    def test_refuses_stale_selection(self):
        stale = {**self.entry, 'pinned': False}
        request = {'operation': 'entry', 'key': str(self.source), 'expected': stale, 'entry': None,
                   'directory': str(self.source.parent)}
        with self.assertRaisesRegex(registry.BinError, 'changed'):
            registry.transact(self.config, request)
        self.assertTrue(self.source.exists())
