import json
import logging
import os
import shlex
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from dylathleen.commons.app_launch import ApplicationLauncher, LaunchProfile, LaunchProfiles, terminal_command
from dylathleen.commons.desktop_entry import DesktopCatalogue
from dylathleen.gems.bin.model import BinApplication
from dylathleen.gems.debget import catalogue
from dylathleen.gems.debget.controller import DebGetManager
from dylathleen.gems.debget.model import DebGetApplication
from dylathleen.view.util.translation import I18n, get_locale_keys


class AppLaunchTest(TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.user = self.root / 'user'
        self.system = self.root / 'system'
        self.user.mkdir()
        self.system.mkdir()
        self.i18n = I18n('en', get_locale_keys('en')[1], 'en', get_locale_keys('en')[1])
        self.launcher = self.new_launcher()
        self.binary = self.executable('fixture app')
        self.helper = self.executable('helper')
        self.enterContext(patch.dict(os.environ, {'PATH': str(self.root), 'XDG_CURRENT_DESKTOP': ''}))

    def enterContext(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def new_launcher(self):
        launcher = ApplicationLauncher(self.i18n)
        launcher.profiles = LaunchProfiles(self.root / 'config/launch.json')
        return launcher

    def executable(self, name, body='#!/bin/sh\nexit 0\n'):
        path = self.root / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def desktop(self, filename, fields='', root=None, exec_line=None, type_='Application'):
        path = (root or self.system) / filename
        path.write_text(f'[Desktop Entry]\nName={path.stem}\nType={type_}\n'
                        f'Exec={exec_line or shlex.quote(str(self.helper))}\n{fields}')
        return path

    def bin_app(self):
        return BinApplication(str(self.binary), {'version': '1'}, False, self.i18n)

    def package(self):
        return DebGetApplication('sample-app', None, ('1', 'installed'), 'github', None, {})

    def populate(self, pkg, files=(), managed=None, launcher=None):
        launcher = launcher or self.launcher
        launcher.populate(pkg, DesktopCatalogue((self.user, self.system)), launcher.saved_profiles(),
                          managed_path=managed, files=tuple(map(str, files)))

    def test_desktop_flags_try_exec_user_override_and_ambiguity(self):
        program = f'"{self.binary}"'
        gui = self.desktop('gui.desktop', exec_line=program + ' --open "two words" %U %c %% %i %k',
                           fields=f'TryExec={self.binary}\nIcon=sample\nPath={self.root}')
        tui = self.desktop('tui.desktop', exec_line=program, fields='Terminal=true')
        self.desktop('hidden.desktop', exec_line=program, fields='Hidden=true')
        self.desktop('no-display.desktop', exec_line=program, fields='NoDisplay=true')
        self.desktop('unavailable.desktop', exec_line=program, fields='TryExec=missing')
        self.desktop('link.desktop', exec_line=program, type_='Link')
        self.desktop('invalid.desktop', exec_line=program + ' %z')
        self.desktop('shadowed.desktop', exec_line=program)
        self.desktop('shadowed.desktop', exec_line=program, root=self.user, fields='Hidden=true')
        pkg = self.bin_app()
        self.populate(pkg, managed=str(self.binary))
        self.assertFalse(pkg.can_be_run())
        self.assertEqual(pkg.get_launch_disabled_reason(), 'launch.multiple')
        self.assertEqual([cmd.desktop for _, cmd in pkg.launch_choices], list(map(str, (gui, tui))))

        key = self.launcher.key(pkg)
        self.launcher.profiles.save(key, LaunchProfile(desktop=str(gui)))
        self.populate(pkg, managed=str(self.binary))
        self.assertTrue(pkg.can_be_run())
        command = pkg.launch_state.command
        self.assertEqual(command.argv, (str(self.binary), '--open', 'two words', 'gui', '%', '--icon', 'sample',
                                        str(gui)))
        self.assertEqual(command.cwd, str(self.root))
        self.assertFalse(command.terminal)

        self.launcher.profiles.save(key, LaunchProfile(desktop=str(tui)))
        self.populate(pkg, managed=str(self.binary))
        self.assertFalse(pkg.can_be_run())
        self.assertEqual(pkg.get_launch_disabled_reason(), 'launch.no_terminal')
        self.executable('xdg-terminal-exec', '#!/bin/sh\nexec "$@"\n')
        self.populate(pkg, managed=str(self.binary))
        self.assertTrue(pkg.can_be_run())
        self.assertTrue(pkg.launch_state.command.terminal)
        self.launcher.profiles.save(key, LaunchProfile(mode='gui', desktop=str(tui)))
        self.populate(pkg, managed=str(self.binary))
        self.assertFalse(pkg.launch_state.command.terminal)

    def test_no_desktop_requires_saved_mode_and_never_probes_executable(self):
        marker = self.root / 'ran'
        cli = self.executable('cli', f'#!/bin/sh\n/usr/bin/touch "{marker}"\n')
        pkg = self.package()
        self.populate(pkg, files=(cli,))
        self.assertFalse(pkg.can_be_run())
        self.assertEqual(pkg.get_launch_disabled_reason(), 'launch.unknown')
        self.assertEqual(self.launcher.information(pkg)['binary_path'], str(cli))
        self.assertEqual(self.launcher.information(pkg)['launch_command'], 'Unknown')
        self.launcher.profiles.save(self.launcher.key(pkg), LaunchProfile('gui', str(cli), ('--version',)))
        other = self.new_launcher()
        pkg = DebGetApplication('sample-app', None, ('2', 'installed'), 'github', None, {})
        self.populate(pkg, files=(cli,), launcher=other)
        self.assertTrue(pkg.can_be_run())
        self.assertEqual(pkg.launch_state.command.argv, (str(cli), '--version'))
        self.assertFalse(marker.exists())
        self.launcher.profiles.save(self.launcher.key(pkg), LaunchProfile('disabled', str(cli)))
        self.populate(pkg, files=(cli,))
        self.assertFalse(pkg.can_be_run())
        self.assertEqual(pkg.get_launch_disabled_reason(), 'launch.disabled')

    def test_start_gui_directly_and_cli_through_terminal_as_same_user(self):
        result = self.root / 'result'
        program = self.executable('run', f'#!{os.sys.executable}\n'
                                  'import json, os, sys\n'
                                  f'with open({str(result)!r}, "w") as out:\n'
                                  '    json.dump([os.geteuid(), os.environ.get("TEST_TERMINAL"), sys.argv[1:]], out)\n')
        self.executable('xdg-terminal-exec', '#!/bin/sh\nexport TEST_TERMINAL=used\nexec "$@"\n')
        desktop = self.desktop('gui.desktop', exec_line=f'{program} --desktop', fields='Terminal=false')
        pkg = self.package()
        self.populate(pkg, (desktop, program))
        uid = os.geteuid()
        modes = (('auto', ('--desktop',), None),
                 ('terminal', ('--tui', 'a b', '$(false)'), 'used'),
                 ('terminal', ('--version',), 'used'))
        for mode, args, expected_terminal in modes:
            with self.subTest(args=args):
                if mode != 'auto':
                    self.launcher.profiles.save(self.launcher.key(pkg), LaunchProfile(mode, str(program), args))
                    self.populate(pkg, (desktop, program))
                if uid == 0:
                    with self.assertRaisesRegex(RuntimeError, 'regular user'):
                        self.launcher.launch(pkg)
                else:
                    self.launcher.launch(pkg)
                    for _ in range(200):
                        try:
                            recorded = json.loads(result.read_text())
                            break
                        except (FileNotFoundError, json.JSONDecodeError):
                            time.sleep(0.01)
                    else:
                        self.fail('The application failed to record its arguments')
                    self.assertEqual(recorded, [uid, expected_terminal, list(args)])
                    result.unlink()

    def test_prefer_desktop_terminal_then_debian_fallback(self):
        alternate = self.executable('x-terminal-emulator')
        xfce = self.executable('xfce4-terminal')
        with patch.dict(os.environ, {'XDG_CURRENT_DESKTOP': 'XFCE'}):
            self.assertEqual(terminal_command(), (str(xfce), '--disable-server', '-x'))
        self.assertEqual(terminal_command(), (str(alternate), '-e'))

    def test_moved_bin_still_resolves_desktop_exec_and_try_exec(self):
        pkg = self.bin_app()
        desktop = self.desktop('example.desktop', exec_line=f'"{self.binary}" --example',
                               fields=f'TryExec={self.binary}')
        self.populate(pkg, managed=str(self.binary))
        self.assertTrue(pkg.can_be_run())
        destination = self.root / 'destination'
        destination.mkdir()
        moved = destination / self.binary.name
        with self.launcher.profiles.relocate_bin('user', [str(self.binary)], destination):
            self.binary.rename(moved)
        migrated = BinApplication(str(moved), {'version': '2'}, False, self.i18n)
        self.populate(migrated, managed=str(moved), launcher=self.new_launcher())
        self.assertTrue(migrated.can_be_run())
        self.assertEqual(migrated.launch_state.command.argv, (str(moved), '--example'))
        self.assertEqual(migrated.launch_state.command.desktop, str(desktop))

    def test_debget_info_uses_owned_files_without_executing_recipe_or_binary(self):
        marker = self.root / 'ran'
        recipe = catalogue.Recipe('sample-app', '99-local', f'touch "{marker}"', {})
        program = self.executable('ui', f'#!/bin/sh\n/usr/bin/touch "{marker}"\n')
        desktop = self.desktop('sample.desktop', exec_line=f'{program} "two words"')
        files = (desktop, program, self.helper)
        output = '\n'.join(map(str, (self.root, *files, self.root / 'missing', program, 'invalid')))
        manager = DebGetManager(SimpleNamespace(i18n=self.i18n, logger=logging.getLogger(__name__)))
        manager.launcher = self.launcher
        manager.settings_path = self.root / 'debget.json'
        with patch('dylathleen.gems.debget.controller.backend.refresh', return_value=({}, '')), \
             patch.object(catalogue, 'inventory', return_value=({'sample-app': [recipe]}, {'sample-app': 'github'},
                                                                 {'sample-app': ('1', 'installed')})), \
             patch.object(catalogue, 'read_catalogue', return_value={'sample-app': [recipe]}), \
             patch.object(catalogue, 'machine', return_value=('amd64', 'jammy', 'ubuntu')), \
             patch('dylathleen.gems.debget.controller.DesktopCatalogue',
                   return_value=DesktopCatalogue((self.user, self.system))), \
             patch.object(catalogue.subprocess, 'run',
                          return_value=SimpleNamespace(stdout=output, returncode=0)) as run:
            pkg = manager.read_installed(None, -1, True, None, False).installed[0]
            info = manager.get_info(pkg)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/dpkg-query', '-L', '--', 'sample-app'])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(info['desktop_file'], str(desktop))
        self.assertEqual(shlex.split(info['launch_command']), [str(program), 'two words'])
        self.assertEqual(info['binary_path'], str(program))
        self.assertEqual(info['app_files'].paths, list(map(str, files)))
        self.assertEqual(info['executables'].paths, [str(program), str(self.helper)])
        self.assertFalse(marker.exists())
