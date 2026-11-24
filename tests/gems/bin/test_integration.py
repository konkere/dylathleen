"""Run the real bin against a local GitHub-compatible release API when installed."""
import io
import json
import logging
import os
import shutil
import subprocess
import tarfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import requests

from dylathleen.api.abstract.handler import ProcessWatcher
from dylathleen.api.abstract.view import SingleSelectComponent, TextInputComponent
from dylathleen.commons.app_launch import LaunchProfile, LaunchProfiles
from dylathleen.gems.bin import registry, releases
from dylathleen.gems.bin.client import BinCommand, BinError, ChoiceRequired
from dylathleen.gems.bin.controller import BinManager
from dylathleen.view.util.translation import get_locale_keys


def executable_archive(version, replacement=False):
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode='w:gz') as archive:
        for filename in ('other', 'replacement' if replacement else 'tool'):
            body = f"#!/bin/sh\nprintf '%s\\n' '{filename} {version}'\n".encode()
            entry = tarfile.TarInfo(f'tool-{version}/{filename}')
            entry.mode = 0o755
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
    return content.getvalue()


class ReleaseHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        endpoint = unquote(urlsplit(self.path).path)
        if endpoint.startswith('/asset/'):
            version, asset = endpoint.split('/')[-2:]
            if asset.endswith('.tar.gz'):
                body = executable_archive(version, replacement=version == 'v3.0.0')
            else:
                body = b'other asset'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        def release(version, prerelease=False):
            base = f'tool-{version}-linux' if version != 'v3.0.0' else 'replacement-linux'
            assets = []
            for name in (f'{base}.tar.gz', 'different-platform.zip', 'SHA256SUMS.txt', 'tool-source.zip'):
                assets.append({'name': name, 'url': f'{self.server.base}/asset/{version}/{name}',
                               'browser_download_url': f'https://github.com/example/tool/asset/{version}/{name}'})
            return {'tag_name': version, 'html_url': f'https://github.com/example/tool/releases/tag/{version}',
                    'assets': assets, 'prerelease': prerelease, 'draft': False}

        if self.server.fail_checks and endpoint.endswith('/latest'):
            self.send_error(503)
            return
        if endpoint.endswith('/releases'):
            data = [release('v4.0.0-rc1', True), release(self.server.latest), release('v1.0.0')]
        else:
            data = release(self.server.latest if endpoint.endswith('/latest') else endpoint.rsplit('/', 1)[-1])
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ReleaseSession:
    def __init__(self, base):
        self.base = base

    def get(self, url, **kwargs):
        path = urlsplit(url).path
        if path.startswith('/repos/'):
            path = '/api/v3' + path
        else:
            path = path.removeprefix('/example/tool')
        return requests.get(self.base + path, **kwargs)


class Watcher(ProcessWatcher):
    def __init__(self):
        self.fields = []
        self.errors = []

    def request_confirmation(self, title, body, components, **kwargs):
        for field in components[0].components:
            self.fields.append(field)
            if isinstance(field, TextInputComponent) and field.label == 'Repository URL':
                field.set_value('https://github.com/example/tool')
            elif isinstance(field, SingleSelectComponent):
                selections = {
                    'Release': lambda value: value.version == 'v1.0.0',
                    'Release file': lambda value: value.name.endswith('.tar.gz'),
                    'File inside archive': lambda value: value.endswith(('/tool', '/replacement')),
                }
                if field.label in selections:
                    select = selections[field.label]
                    field.value = next(option for option in field.options if option.value and select(option.value))
        return True

    def show_message(self, title, body, type_):
        self.errors.append(body)


class ExitCodeTest(unittest.TestCase):
    def test_update_errors_are_not_no_update(self):
        with BinCommand('bin') as command:
            with patch.object(command, 'run', return_value=(1, 'HTTP 503')):
                with self.assertRaisesRegex(BinError, 'HTTP 503'):
                    command.check()
            with patch.object(command, 'run', return_value=(3, 'unrecognized update output')):
                with self.assertRaisesRegex(BinError, 'unrecognized'):
                    command.check()

    def test_prompt_on_eof_is_rejected_even_if_bin_returns_success(self):
        with BinCommand('bin') as command:
            with patch.object(command, 'run', return_value=(0, 'Select an option [1]: ')):
                with self.assertRaises(ChoiceRequired):
                    command.update()


@unittest.skipUnless(shutil.which('bin'), 'marcosnils/bin is not installed')
class BinIntegrationTest(unittest.TestCase):
    def setUp(self):
        result = subprocess.run(['bin', '--version'], capture_output=True, text=True)
        if not result.stdout.startswith('bin version '):
            self.skipTest('marcosnils/bin is not installed')
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        server = ThreadingHTTPServer(('127.0.0.1', 0), ReleaseHandler)
        server.base = f'http://127.0.0.1:{server.server_port}'
        server.latest = 'v2.0.0'
        server.fail_checks = False
        self.server = server
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        self.config = self.root / 'config.json'
        self.enterContext(patch.dict(os.environ, {'HOME': str(self.root), 'BIN_CONFIG': str(self.config),
                                                 'GHES_BASE_URL': server.base + '/', 'GHES_UPLOAD_URL': server.base + '/',
                                                 'GHES_AUTH_TOKEN': 'test-fixture'}))
        self.enterContext(patch('dylathleen.gems.bin.controller.is_root', return_value=False))
        self.enterContext(patch('dylathleen.gems.bin.controller.Repository', side_effect=self.repository))
        locale = Path(releases.__file__).with_name('resources') / 'locale'
        self.manager = BinManager(SimpleNamespace(i18n={**get_locale_keys('en', str(locale))[1],
                                                        'proceed': 'Proceed', 'cancel': 'Cancel', 'error': 'Error'},
                                                  logger=logging.getLogger(__name__)))
        self.manager.settings_path = self.root / 'settings.json'
        self.manager.launcher.profiles = LaunchProfiles(self.root / 'launch.json')
        self.manager.registries[True] = SimpleNamespace(read=lambda: {'bins': {}})
        self.watcher = Watcher()
        self.binary = self.root / '.local/bin/tool'

    def enterContext(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def repository(self, url, **kwargs):
        return releases.Repository(url, session=ReleaseSession(self.server.base), **kwargs)

    def installed(self):
        return self.manager.read_installed(None, -1, True, None, True).installed[0]

    def run_binary(self):
        return subprocess.check_output([self.binary], text=True).strip()

    def upgrade(self, package):
        requirements = self.manager.get_upgrade_requirements([package], None, self.watcher)
        self.assertTrue(self.manager.upgrade(requirements, None, self.watcher), self.watcher.errors)

    def test_install_tag_detect_update_preserve_asset_and_archive_path(self):
        self.assertTrue(self.manager.install_url(None, self.watcher), self.watcher.errors)
        self.assertEqual(self.run_binary(), 'tool v1.0.0')
        tag = next(field for field in self.watcher.fields if field.label == 'Release')
        self.assertEqual(tag.init_value.value.version, 'v2.0.0')
        asset = next(field for field in self.watcher.fields if field.label == 'Release file')
        self.assertEqual([option.label for option in asset.options[1:]],
                         ['tool-v1.0.0-linux.tar.gz', 'different-platform.zip'])
        self.assertTrue(any(field.label == 'File inside archive' for field in self.watcher.fields))

        package = self.installed()
        self.assertFalse(package.pinned)
        self.assertEqual(package.version, 'v1.0.0')
        self.assertTrue(package.update)
        self.assertEqual(package.latest_version, 'v2.0.0')
        self.assertTrue(self.manager.pin(package, None, self.watcher))
        pinned = self.installed()
        self.assertTrue(pinned.pinned)
        self.assertFalse(pinned.update)
        self.assertTrue(self.manager.unpin(pinned, None, self.watcher))

        self.watcher.fields.clear()
        self.upgrade(self.installed())
        self.assertEqual(self.watcher.fields, [])
        self.assertEqual(self.run_binary(), 'tool v2.0.0')
        current = registry.read_registry(self.config)['bins'][str(self.binary)]
        self.assertEqual(current['version'], 'v2.0.0')
        self.assertEqual(current['url'], 'https://github.com/example/tool/releases/tag/v2.0.0')
        self.assertEqual(current['selected_asset'], 'tool-v2.0.0-linux.tar.gz')
        self.assertEqual(current['package_path'], 'tool-v2.0.0/tool')
        self.assertFalse(current['pinned'])
        self.assertFalse(self.installed().update)

        self.server.latest = 'v3.0.0'
        self.upgrade(self.installed())
        self.assertTrue(any(field.label == 'Release file' for field in self.watcher.fields))
        self.assertEqual(self.run_binary(), 'replacement v3.0.0')
        package = self.installed()
        self.assertEqual(package.entry['package_path'], 'tool-v3.0.0/replacement')
        self.assertTrue(self.manager.uninstall(package, None, self.watcher, None).success)
        self.assertFalse(self.binary.exists())
        self.assertEqual(registry.read_registry(self.config)['bins'], {})

    def test_api_failure_is_reported_with_installed_entry(self):
        self.assertTrue(self.manager.install_url(None, self.watcher), self.watcher.errors)
        self.server.fail_checks = True
        package = self.installed()
        self.assertFalse(package.update)
        self.assertIn('503', package.update_error)
        self.assertTrue(any('503' in message for message in self.manager.list_warnings(True)))
        self.assertEqual(self.run_binary(), 'tool v1.0.0')

    def test_change_user_directory_rejects_conflict_and_updates_registry_on_retry(self):
        self.assertTrue(self.manager.install_url(None, self.watcher), self.watcher.errors)
        installed = self.installed()
        self.manager.launcher.profiles.save(self.manager.launcher.key(installed),
                                            LaunchProfile('gui', str(self.binary), ('--example',)))
        self.assertEqual(self.installed().launch_state.command.argv, (str(self.binary), '--example'))
        settings = next(self.manager.get_settings())
        directory, _ = settings.component.components[0].components
        directory.set_value('~/commands')
        new_path = self.root / 'commands/tool'
        new_path.parent.mkdir()
        new_path.write_text('unrelated')
        self.assertFalse(settings.save()[0])
        self.assertEqual(self.manager._directories()['user'], '~/.local/bin')
        self.assertEqual(self.run_binary(), 'tool v1.0.0')
        self.assertEqual(self.installed().launch_state.command.argv, (str(self.binary), '--example'))
        self.assertEqual(new_path.read_text(), 'unrelated')
        new_path.unlink()
        self.assertEqual(settings.save(), (True, None))
        self.assertFalse(self.binary.exists())
        self.assertEqual(subprocess.check_output([new_path], text=True).strip(), 'tool v1.0.0')
        self.assertEqual(list(registry.read_registry(self.config)['bins']), [str(new_path)])
        moved = self.installed()
        self.assertEqual(moved.launch_profile.path, str(new_path))
        self.assertEqual(moved.launch_state.command.argv, (str(new_path), '--example'))
        self.upgrade(moved)
        self.assertEqual(self.installed().launch_state.command.argv, (str(new_path), '--example'))
