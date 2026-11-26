import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from dylathleen.daemon import checks, client, inventory
from dylathleen.daemon.operations import destination
from dylathleen.daemon.service import Backend


class CacheTest(unittest.TestCase):
    def test_offline_service_retains_snapshot(self):
        data = {'bins': {'/opt/bin/tool': {'version': '1'}}}
        with patch.object(client, 'interface', side_effect=OSError), patch.object(client, 'load', return_value=data):
            result, warning = client.refresh(True)
        self.assertIs(result, data)
        self.assertTrue(warning)

    def test_external_removal_and_changed_version(self):
        data = {'bins': {}, 'debget': {'tool': ['1', 'installed', 'github'], 'removed': ['4', 'installed', 'direct']},
                'updates': {'debget:tool': '2', 'debget:removed': '5'}}
        with patch.object(inventory.registry, 'transact', return_value={'bins': {}}), patch.object(inventory, 'secure'), \
                patch.object(inventory.catalogue, 'registered', return_value={'tool': 'github'}), \
                patch.object(inventory.catalogue, 'installed_versions', return_value={'tool': ('3', 'installed')}):
            result = inventory.inventory(data)
        self.assertEqual(result['debget'], {'tool': ['3', 'installed', 'github']})
        self.assertEqual(result['updates'], {})
        self.assertEqual(result['stale'], ['debget:tool'])

    def test_registry_failure_preserves_entries(self):
        with patch.object(inventory.registry, 'transact', side_effect=ValueError):
            data = inventory.inventory({'bins': {'key': {'version': '1'}}})
        self.assertIn('key', data['bins'])
        self.assertIn('bin', data['errors'])

    def test_public_registry_omits_credentials_and_extensions(self):
        result = inventory.public_entry({'path': '/opt/bin/app', 'token': 'private',
                                         'url': 'https://example.org/file?token=secret'})
        self.assertEqual(result, {'path': '/opt/bin/app'})

    def test_disallowed_install_directory(self):
        with self.assertRaises(ValueError):
            destination('/etc')
        self.assertEqual(destination('/opt/apps/bin'), Path('/opt/apps/bin'))

    def test_github_seed_shared_without_forwarding_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            token = folder / 'token'
            token.write_text('fixture')
            token.chmod(0o600)
            response = Mock(status_code=200)
            response.iter_content.return_value = [json.dumps({'assets': [
                {'browser_download_url': 'https://github.com/example/app/app_2_all.deb'}]}).encode()]
            with patch.object(checks, 'secure', return_value=True), patch.object(checks, 'Path', return_value=token), \
                    patch.object(checks.requests.Session, 'get') as get:
                get.return_value.__enter__.return_value = response
                checks.seed(folder, {k: ('example/app', 'latest') for k in ('tool', 'tool2')})
                get.assert_called_once()
                self.assertFalse(get.call_args.kwargs['allow_redirects'])
                self.assertEqual(get.call_args.kwargs['headers']['Authorization'], 'Bearer fixture')
            for file in folder.glob('*.json_extract'):
                self.assertIn('app_2_all.deb', file.read_text())
                self.assertNotIn('fixture', file.read_text())


class OperationTest(unittest.TestCase):
    def test_overlapping_check_and_transaction(self):
        backend = Backend.__new__(Backend)
        backend.active = 1
        backend.jobs = {1: ('refresh', 'running', '')}
        backend.want_online = False
        self.assertEqual(backend.Refresh(True), 1)
        with self.assertRaisesRegex(ValueError, 'running'):
            backend.start('bin', Mock())
