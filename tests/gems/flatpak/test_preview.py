from unittest import TestCase
from unittest.mock import patch
import subprocess

from dylathleen.gems.flatpak.inventory import inventory
from dylathleen.gems.flatpak.model import FlatpakApplication


class PreviewTest(TestCase):
    local = 'org.example.App/x86_64/stable\tstore\taaaaaa\t3.2.4\tApp\tDescription\torg.example.Platform/x86_64/stable\tuser,current\n'
    ref = 'app/org.example.App/x86_64/stable'

    def preview(self, remote, refresh=''):
        with patch('dylathleen.gems.flatpak.inventory._read',
                   side_effect=[self.local, remote, refresh, remote]):
            return FlatpakApplication(**inventory('user', True)[0])

    def test_unknown_version_survives_stale_cache(self):
        for version in ('', '3.0.6'):
            with self.subTest(version=version):
                app = self.preview(f'{self.ref}\tstore\tbbbbbb\t{version}\n')
                app.fill_cached_data({'version': '3.0.6', 'latest_version': '3.0.6'})
                self.assertTrue(app.update)
                self.assertEqual('3.2.4', app.version)
                self.assertIsNone(app.latest_version)
                self.assertIn('unknown', app.update_version_label({'flatpak.update.unknown': 'unknown'}))

    def test_scope_ref_and_origin(self):
        for ref, origin in [(self.ref, 'other'), (self.ref.replace('stable', 'beta'), 'store'),
                            (self.ref.replace('x86_64', 'aarch64'), 'store'),
                            (self.ref.replace('app/', 'runtime/'), 'store')]:
            self.assertFalse(self.preview(f'{ref}\t{origin}\tbbbbbb\t4').update)

    def test_catalogue_unavailable(self):
        app = self.preview(f'{self.ref}\tstore\tbbbbbb\t3.2.4', subprocess.TimeoutExpired('flatpak', 60))
        self.assertTrue(app.update)
        self.assertIsNone(app.latest_version)

    def test_same_commit(self):
        self.assertFalse(self.preview(f'{self.ref}\tstore\taaaaaa\t4').update)
