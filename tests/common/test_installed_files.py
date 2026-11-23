import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dylathleen.gems.appimage.controller import AppImageManager
from dylathleen.gems.appimage.model import AppImage
from dylathleen.gems.flatpak.inventory import application_files
from dylathleen.gems.snap.snap import launch_args


class InstalledFilesTest(unittest.TestCase):

    def test_appimage_command_and_file_list_with_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / 'My App'
            install.mkdir()
            image = install / 'Hello World.AppImage'
            image.touch()
            icon = install / 'icon.png'
            icon.touch()
            link = root / 'hello'
            link.symlink_to(image)
            loop = install / 'loop'
            loop.symlink_to(root, target_is_directory=True)
            app = AppImage(name='Hello', installed=True, install_dir=str(install),
                           symlink=str(link), license='MIT')
            manager = AppImageManager.__new__(AppImageManager)
            info = manager.get_info(app)
            self.assertEqual(shlex.split(info['launch_command']), [str(image)])
            self.assertEqual(info['app_files'].location, str(image))
            self.assertEqual(info['app_files'].paths, [str(image), *sorted(map(str, (icon, link, loop)))])

    def test_flatpak_uses_deployment_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'files/bin/start'
            binary.parent.mkdir(parents=True)
            binary.touch()
            (root / 'metadata').write_text('[Application]\ncommand=/app/bin/start\n')
            with patch('dylathleen.gems.flatpak.inventory._read', return_value=tmp) as query:
                files = application_files('user', 'app/org.example.App/x86_64/beta')
            query.assert_called_once_with('info', '--user', '--show-location', 'app/org.example.App/x86_64/beta')
            self.assertEqual(files.location, str(binary))
            self.assertEqual(files.paths, [str(binary), str(root / 'metadata')])

    def test_snap_selects_qualified_desktop_command(self):
        apps = [{'name': 'service', 'daemon': 'simple', 'desktop-file': 'service.desktop'},
                {'name': 'cli'}, {'name': 'gui', 'desktop-file': 'gui.desktop'}]
        self.assertEqual(launch_args('example', apps), ['snap', 'run', 'example.gui'])
        self.assertEqual(launch_args('example', [{'name': 'example'}]), ['snap', 'run', 'example'])
        self.assertIsNone(launch_args('example', []))
