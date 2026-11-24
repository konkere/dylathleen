import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock

from dylathleen.gems.debget import client, privileged as helper


class AptBoundaryTest(TestCase):
    def test_remove_and_purge_strip_autoremove(self):
        for verb in ('remove', 'purge'):
            with self.subTest(verb=verb):
                args = ['-q', '-y', '--autoremove', verb, 'fixture']
                self.assertEqual(helper.apt_plan(args, {'fixture'}, verb), ([verb, 'fixture'], {'fixture'}))

    def test_upgrade_with_empty_native_apt_registry_is_a_noop(self):
        self.assertEqual(helper.apt_plan(['-y', 'install', '--only-upgrade', ''], set(), 'upgrade'), ([], set()))

    def test_global_actions_and_unregistered_targets_fail_closed(self):
        for args in (['update'], ['-y', 'upgrade'], ['full-upgrade'], ['autoremove'],
                     ['remove', 'stray'], ['remove', 'fixture', 'stray'], ['-o', 'APT::Config=/tmp/other', 'remove', 'fixture']):
            with self.subTest(args=args):
                with self.assertRaises(helper.DebGetError):
                    helper.apt_plan(args, {'fixture'}, 'remove')
        with self.assertRaises(helper.DebGetError):
            helper.apt_plan(['install', 'fixture'], {'fixture'}, 'update')

    def test_simulation_blocks_removing_another_package(self):
        for verb, tag in (('remove', 'Remv'), ('purge', 'Purg')):
            with self.subTest(verb=verb), mock.patch.dict(os.environ, {
                'DG_ALLOWED': '["fixture"]', 'DG_ACTION': verb
            }), mock.patch.object(subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=f'{tag} fixture [1]\nRemv stray [1]\n'
            )), mock.patch.object(subprocess, 'call') as commit:
                with self.assertRaisesRegex(helper.DebGetError, 'outside'):
                    helper.apt(['--autoremove', verb, 'fixture'])
                commit.assert_not_called()

    def test_approved_removal_uses_exact_name(self):
        for verb in ('remove', 'purge'):
            with self.subTest(verb=verb), mock.patch.dict(os.environ, {
                'DG_ALLOWED': '["fixture"]', 'DG_ACTION': verb
            }), mock.patch.object(subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='Remv fixture [1]\n'
            )), mock.patch.object(helper, 'ask') as prompt, mock.patch.object(helper, 'emit'), \
                    mock.patch.object(subprocess, 'call', return_value=0) as commit:
                self.assertEqual(helper.apt(['--autoremove', verb, 'fixture']), 0)
                self.assertEqual(commit.call_args.args[0][-2:], [verb, 'fixture'])
                self.assertNotIn('--autoremove', commit.call_args.args[0])
                self.assertIn('APT::Get::AutomaticRemove=false', commit.call_args.args[0])
                prompt.assert_called_once()

    def test_native_commands_bypass_wrapper_and_keep_update_flags(self):
        for verb, expected in (('update', ['update']), ('upgrade', ['upgrade', '--dg-only'])):
            with (
                self.subTest(verb=verb),
                mock.patch.object(helper, 'secure'),
                mock.patch.object(helper, 'check_native_paths'),
                mock.patch.object(helper, 'registered', return_value={}),
                mock.patch.object(subprocess, 'check_output', return_value='0.4.7\n') as version,
                mock.patch.object(helper, 'TemporaryDirectory', return_value=TemporaryDirectory()),
                mock.patch.object(helper, 'run_native', return_value=0) as native,
            ):
                request = {'action': verb, 'token': 'fake_token', 'packages': []}
                self.assertEqual(helper.native_request(request), 0)
                args, env = native.call_args.args
                self.assertEqual(args, expected)
                self.assertEqual(env['DISABLE_APT'], 'y')
                if verb == 'update':
                    self.assertEqual(env['DEBGET_CACHE_RTN'], '0')
                self.assertNotIn('fake_token', str(env))
                self.assertEqual(version.call_args.args[0][0], '/usr/bin/deb-get')

    def test_final_plan_cannot_remove_unrelated_package(self):
        lines = ['VERSION 2\n', 'name=value\n', '\n', 'fixture 1 > - **REMOVE**\n']
        helper.check_apt_hook(iter(lines), {'fixture'})
        with self.assertRaisesRegex(helper.DebGetError, 'outside'):
            helper.check_apt_hook(iter(lines + ['stray 1 > - **REMOVE**\n']), {'fixture'})
        with self.assertRaisesRegex(helper.DebGetError, 'protocol'):
            helper.check_apt_hook(iter(['VERSION 1\n']), {'fixture'})

    def test_wget_uses_private_configuration_only_for_github_api(self):
        with (mock.patch.dict(os.environ, {'DG_WGET_CONFIG': '/run/private/wgetrc'}),
              mock.patch.object(subprocess, 'call', return_value=0) as command):
            helper.wget(['', '-q', 'https://api.github.com/repos/example/tool/releases'])
            self.assertIn('--config=/run/private/wgetrc', command.call_args.args[0])
            helper.wget(['-q', 'https://example.invalid/deb'])
            self.assertNotIn('--config=/run/private/wgetrc', command.call_args.args[0])

    def test_original_user_token_is_private_and_env_wins(self):
        with TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'HOME': directory, 'DEBGET_TOKEN': ''}):
            path = Path(directory) / '.config/deb-get/github-token'
            path.parent.mkdir(parents=True)
            path.write_text('sample_token\n')
            path.chmod(0o600)
            self.assertEqual(client.user_token(), 'sample_token')
            path.chmod(0o644)
            with self.assertRaises(helper.DebGetError):
                client.user_token()
            with mock.patch.dict(os.environ, {'DEBGET_TOKEN': 'override'}):
                self.assertEqual(client.user_token(), 'override')

    def test_parsers_distinguish_updates_from_errors(self):
        self.assertEqual(client.pending_updates(['[+] foo (1) has an update pending. 2 is available.',
                                                '[*] WARNING! repo failed']), {'foo': '2'})
        self.assertEqual(client.pending_updates(['[!] ERROR! GitHub API rate limit exceeded']), {})
