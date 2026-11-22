import json
import os
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch
from types import SimpleNamespace

from dylathleen.api.abstract.controller import UpgradeRequirement, UpgradeRequirements
from dylathleen.gems.appimage import controller
from dylathleen.gems.appimage.model import AppImage


class ReplacementTest(TestCase):
    def setUp(self):
        stack = self.stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(TemporaryDirectory()))
        self.live = root / 'installed' / 'demo-0'
        self.live.mkdir(parents=True)
        self.desktop = root / 'menu' / 'dylathleen_appimage_demo.desktop'
        self.desktop.parent.mkdir()
        self.desktop.write_text('original desktop')
        self.image = self.live / 'Demo-v1.AppImage'
        self.image.write_bytes(b'old image')
        self.image.chmod(0o755)
        self.link = root / 'demo'
        self.link.symlink_to(self.image)
        self.icon = self.live / 'logo.png'
        self.icon.write_bytes(b'original icon')
        self.ignored = root / 'updates_ignored.txt'
        self.ignored.write_text('Demo\nOther\n')
        self.source = root / 'Demo-v2.AppImage'
        self.source.write_bytes(b'incomplete download')
        self.pkg = AppImage(name='Demo', installed=True, install_dir=str(self.live), version='1',
                            symlink=str(self.link), icon_path=str(self.icon), updates_ignored=True,
                            url_download='file:///v1.AppImage', url_download_latest_version='file:///v2.AppImage')
        self.pkg.latest_version = '2'
        self.pkg.update = True
        self.metadata = self.live / 'data.json'
        self.metadata.write_text(json.dumps(self.pkg.get_data_to_cache()))
        self.original = {p: p.read_bytes() for p in (self.metadata, self.desktop, self.icon, self.image, self.ignored)}
        self.watcher = Mock()
        self.watcher.should_stop.return_value = False
        context = Mock()
        context.i18n = defaultdict(lambda: '{}')
        context.i18n['appimage.upgrade.failed'] = '{apps}'
        context.logger = Mock()
        self.manager = controller.AppImageManager(context)
        self.downloads = []

        def download(file_url, output_path, **kwargs):
            self.downloads.append(file_url)
            Path(output_path).write_bytes(self.source.read_bytes())
            return True

        self.manager.file_downloader.download.side_effect = download
        for name, value in dict(INSTALLATION_DIR=self.live.parent, DESKTOP_ENTRIES_DIR=self.desktop.parent,
                                DOWNLOAD_DIR=root / 'download', APPIMAGE_CONFIG_DIR=root,
                                DATABASE_APPS_FILE=root / 'apps.db', UPDATES_IGNORED_FILE=self.ignored).items():
            stack.enter_context(patch.object(controller, name, str(value)))

    def upgrade(self):
        return self.manager.upgrade(UpgradeRequirements([], [], [UpgradeRequirement(self.pkg)], []), None, self.watcher)

    def assert_original(self):
        for p, content in self.original.items():
            self.assertTrue(p.exists(), str(p))
            self.assertEqual(content, p.read_bytes())
        self.assertEqual(str(self.image), os.readlink(self.link))
        self.assertEqual('1', self.pkg.version)
        self.assertEqual('1', self.manager.read_installed(None).installed[0].version)

    def test_corrupt_upgrade_preserves_installation(self):
        self.assertFalse(self.upgrade())
        self.assert_original()

    def prepare_candidate(self):
        self.source.write_bytes(b'\x7fELF\x02\x01\x01\0AI\x02' + b'test payload')

        def extract(cmd, cwd):
            root = Path(cwd) / 'squashfs-root'
            root.mkdir()
            (root / 'AppRun').touch(mode=0o755)
            entry = root / 'usr' / 'share' / 'applications' / 'demo.desktop'
            entry.parent.mkdir(parents=True)
            entry.write_text('[Desktop Entry]\nType=Application\nName=Demo\nExec=demo %f\nIcon=demo\n')
            (root / 'demo.png').write_bytes(b'new icon')
            return MagicMock()

        self.stack.enter_context(patch.object(controller, 'SimpleProcess', side_effect=extract))
        return self.stack.enter_context(patch.object(controller.ProcessHandler, 'handle_simple',
                                                    return_value=(True, '')))

    def downgrade(self):
        self.manager.get_history = Mock(return_value=SimpleNamespace(pkg_status_idx=0, history=[
            {}, {'0_version': '0.9', '2_url_download': 'file:///older.AppImage'}]))
        return self.manager.downgrade(self.pkg, None, self.watcher)

    def manual_update(self):
        def choose(components, **kwargs):
            chooser, version = components[0].components
            chooser.file_path = str(self.source)
            version.value = 'manual'
            return True

        self.watcher.request_confirmation.side_effect = choose
        self.pkg.imported = True
        return self.manager.update_file(self.pkg, None, self.watcher)

    def assert_updated(self, version):
        self.assertEqual(version, self.pkg.version)
        self.assertEqual(version, self.manager.read_installed(None).installed[0].version)
        self.assertEqual(self.source.read_bytes(), self.image.read_bytes())
        self.assertEqual(str(self.image), os.readlink(self.link))
        self.assertEqual(self.image.read_bytes(), self.link.read_bytes())
        self.assertTrue(os.access(self.image, os.X_OK))
        self.assertEqual(b'new icon', self.icon.read_bytes())
        self.assertIn(f'Exec="{self.image}" %f', self.desktop.read_text())
        self.assertIn(f'Icon={self.icon}', self.desktop.read_text())
        self.assertEqual(self.original[self.ignored], self.ignored.read_bytes())
        self.assertFalse(list((self.live.parent / '.transactions').iterdir()))

    def test_upgrade(self):
        self.prepare_candidate()
        self.assertTrue(self.upgrade())
        self.assert_updated('2')

    def test_downgrade_with_newer_release_available(self):
        self.prepare_candidate()
        self.assertTrue(self.downgrade())
        self.assertEqual(['file:///older.AppImage'], self.downloads)
        self.assert_updated('0.9')

    def test_manual_update_preserves_source(self):
        self.prepare_candidate()
        source = self.source.read_bytes()
        self.assertTrue(self.manual_update())
        self.assertEqual([], self.downloads)
        self.assertEqual(source, self.source.read_bytes())
        self.assert_updated('manual')

    def test_failed_manual_extraction(self):
        extract = self.prepare_candidate()
        extract.return_value = False, 'incomplete extraction'
        source = self.source.read_bytes()
        self.assertFalse(self.manual_update())
        self.assert_original()
        self.assertEqual(source, self.source.read_bytes())
        self.assertFalse(self.pkg.manual_update)
        self.assertIsNone(self.pkg.local_file_path)

    def test_corrupt_downgrade_preserves_installation(self):
        self.assertFalse(self.downgrade())
        self.assert_original()

    def test_failed_download(self):
        self.manager.file_downloader.download.return_value = False
        self.manager.file_downloader.download.side_effect = None
        self.assertFalse(self.upgrade())
        self.assert_original()

    def test_cancel_before_publication(self):
        self.prepare_candidate()
        self.watcher.should_stop.return_value = True
        self.assertFalse(self.upgrade())
        self.assert_original()

    def test_publication_errors_restore_previous_files(self):
        self.prepare_candidate()
        replace = os.replace
        for target in (self.icon, self.desktop, self.metadata, self.image):
            def fail_once(src, dst):
                if dst == target and (src.suffix == '.new' or src.name == 'image'):
                    raise OSError('injected publication failure')
                replace(src, dst)

            with self.subTest(target=target), patch.object(os, 'replace', side_effect=fail_once):
                self.assertFalse(self.upgrade())
                self.assert_original()

    def test_missing_integration_files_stay_absent_on_failure(self):
        self.prepare_candidate()
        self.icon.unlink()
        self.desktop.unlink()
        replace = os.replace

        def fail_commit(src, dst):
            if dst == self.image:
                raise OSError('injected commit failure')
            replace(src, dst)

        with patch.object(os, 'replace', side_effect=fail_commit):
            self.assertFalse(self.upgrade())
        self.assertFalse(self.icon.exists())
        self.assertFalse(self.desktop.exists())
        self.assertEqual(self.original[self.metadata], self.metadata.read_bytes())

    def interrupt_publication(self, after_commit):
        self.prepare_candidate()
        child = os.fork()
        if child == 0:
            replace = os.replace

            def interrupt(src, dst):
                if dst == self.image and not after_commit:
                    os._exit(23)
                replace(src, dst)
                if dst == self.image:
                    os._exit(23)

            try:
                with patch.object(os, 'replace', side_effect=interrupt):
                    self.upgrade()
            finally:
                os._exit(1)
        _, status = os.waitpid(child, 0)
        self.assertEqual(23, os.waitstatus_to_exitcode(status))
        self.pkg, = self.manager.read_installed(None).installed

    def test_restart_before_commit_rolls_back(self):
        self.interrupt_publication(after_commit=False)
        self.assert_original()

    def test_restart_after_commit_keeps_complete_installation(self):
        self.interrupt_publication(after_commit=True)
        self.assert_updated('2')

    def test_unexpected_symlink_target_is_preserved(self):
        self.prepare_candidate()
        self.link.unlink()
        self.link.symlink_to(self.metadata)
        self.assertFalse(self.upgrade())
        self.assertEqual(self.metadata, self.link.resolve())
        self.assertEqual(self.original[self.metadata], self.metadata.read_bytes())
        self.assertEqual(self.original[self.image], self.image.read_bytes())

    def test_legacy_metadata_with_no_install_dir_or_symlink(self):
        self.prepare_candidate()
        previous = json.loads(self.metadata.read_text())
        del previous['install_dir'], previous['symlink']
        self.metadata.write_text(json.dumps(previous))
        self.link.unlink()
        self.pkg, = self.manager.read_installed(None).installed
        self.pkg.update = True
        self.pkg.latest_version = '2'
        self.pkg.url_download_latest_version = 'file:///v2.AppImage'
        self.assertTrue(self.upgrade())
        self.assertEqual('2', self.manager.read_installed(None).installed[0].version)
        self.assertEqual(self.source.read_bytes(), self.image.read_bytes())
        self.assertIsNone(self.pkg.symlink)
        self.assertFalse(self.link.exists())

    def test_delayed_cache_write_cannot_restore_old_version(self):
        self.prepare_candidate()
        stale, = self.manager.read_installed(None).installed
        self.assertTrue(self.upgrade())
        self.manager.cache_to_disk(stale, b'old icon', False)
        self.assert_updated('2')
