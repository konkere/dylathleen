import shlex
import shutil
import subprocess
from io import StringIO
from pathlib import Path
from typing import Tuple, Optional

import yaml

from dylathleen.commons.installed_files import InstalledFiles
from dylathleen.commons.system import SimpleProcess


def is_installed() -> bool:
    return bool(shutil.which('snap'))


def uninstall_and_stream(app_name: str, root_password: Optional[str]) -> SimpleProcess:
    return SimpleProcess(cmd=('snap', 'remove', app_name),
                         root_password=root_password,
                         lang=None,
                         shell=True)


def install_and_stream(app_name: str, confinement: str, root_password: Optional[str], channel: Optional[str] = None) -> SimpleProcess:

    install_cmd = ['snap', 'install', app_name]  # default

    if confinement == 'classic':
        install_cmd.append('--classic')

    if channel:
        install_cmd.append(f'--channel={channel}')

    return SimpleProcess(install_cmd, root_password=root_password, shell=True, lang=None)


def downgrade_and_stream(app_name: str, root_password: Optional[str]) -> SimpleProcess:
    return SimpleProcess(cmd=('snap', 'revert', app_name),
                         root_password=root_password,
                         shell=True,
                         lang=None)


def refresh_and_stream(app_name: str, root_password: Optional[str], channel: Optional[str] = None) -> SimpleProcess:
    cmd = ['snap', 'refresh', app_name]

    if channel:
        cmd.append(f'--channel={channel}')

    return SimpleProcess(cmd=cmd,
                         root_password=root_password,
                         lang=None,
                         shell=True)


def launch_args(name, commands):
    apps = [app for app in commands if not app.get('daemon')]
    if apps:
        selected = next((app for app in apps if app.get('desktop-file')), apps[0])
        command = selected['name']
        # snapd returns the app name, while snap run expects the qualified name.
        return ['snap', 'run', name if command == name else f'{name}.{command}']


def application_files(name, command, apps):
    for mount in (Path('/snap'), Path('/var/lib/snapd/snap')):
        directory = mount / name / 'current'
        if directory.is_dir():
            wrapper = mount / 'bin' / command
            launcher = wrapper
            try:
                with (directory / 'meta/snap.yaml').open(encoding='utf-8') as metadata:
                    app = yaml.safe_load(metadata)['apps'][command.removeprefix(name + '.')]
                executable = Path(shlex.split(app['command'])[0])
                if not executable.is_absolute() and (directory / executable).is_file():
                    launcher = directory / executable
            except (OSError, UnicodeError, yaml.YAMLError, KeyError, ValueError, TypeError, IndexError):
                pass  # The public snap launcher also works with incomplete metadata.
            entries = [app.get('desktop-file') for app in apps]
            return InstalledFiles.collect(directory, launcher, wrapper, *entries)


def run(args):
    subprocess.Popen(args)


def is_api_available() -> Tuple[bool, str]:
    output = StringIO()
    for o in SimpleProcess(('snap', 'search'), lang=None).instance.stdout:
        if o:
            output.write(o.decode())

    output.seek(0)
    output = output.read()
    return 'error:' not in output, output
