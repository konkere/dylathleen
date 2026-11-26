"""Run in a disposable, booted systemd VM. Arguments are the previous and candidate DEBs."""
import hashlib
import importlib.util
import json
import os
import socket
import shutil
import subprocess
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch, MagicMock

from dylathleen.daemon import client as backend
from dylathleen.gems.debget.client import Cancelled
from dylathleen.daemon.client import interface, refresh, wait
from dylathleen.gems.debget.privileged import MARKER


class Watcher:
    def print(self, line):
        pass

    def change_progress(self, value):
        pass


def version():
    return subprocess.check_output(['dpkg-query', '-Wf', '${Version}', 'dylathleen'], text=True)


def detached_upgrade():
    api = interface()
    job, descriptor = api.DebGet('upgrade-one', ['dylathleen'], signature='sas')
    with socket.socket(fileno=descriptor.take()) as peer, peer.makefile('r') as lines:
        peer.sendall(b'\n')
        for line in lines:
            if line.startswith(MARKER) and json.loads(line[len(MARKER):])['event'] == 'transaction':
                peer.sendall(b'yes\n')
                break
        else:
            raise AssertionError('Missing APT plan')
    # Drop the initiating connection while dpkg is running.
    del api
    wait(interface(), job)


def guard_failures():
    from dylathleen.gems.debget import privileged as helper
    # This mounted filesystem exercises execve denial even when the mode is 0755.
    with TemporaryDirectory() as root:
        mount = Path(root)
        subprocess.run(['mount', '-t', 'tmpfs', '-o', 'noexec', 'none', mount], check=True)
        try:
            with TemporaryDirectory(dir=mount) as folder:
                try:
                    helper.install_guards(Path(folder))
                except PermissionError:
                    pass
                else:
                    raise AssertionError('Guards on noexec were accepted')
        finally:
            subprocess.run(['umount', mount], check=True)
    # A missing installed guard must also fail through the public transaction endpoint.
    held = helper.GUARD.with_name('guard-test-backup')
    helper.GUARD.rename(held)
    try:
        try:
            backend.debget({'operation': 'native', 'action': 'reinstall', 'packages': ['dylathleen']},
                           '', Watcher(), lambda _: True)
        except OSError as error:
            assert 'debget-guard' in str(error), error
        else:
            raise AssertionError('Missing guard was accepted')
    finally:
        held.rename(helper.GUARD)
    with TemporaryDirectory(dir='/usr/lib/dylathleen') as root:
        folder = Path(root)
        helper.install_guards(folder)
        for name in ('apt', 'apt-get', 'aptitude'):
            (folder / name).unlink()
            assert shutil.which(name, path=root) is None


def authenticated_check(recipe, source):
    from dylathleen.daemon import checks, inventory
    recipe.write_text(source + '''
get_github_releases "example/test" "latest"
if [ ! -r /etc/dylathleen/github-token ] && [ ! -x /usr/bin/apt-get ]; then
    URL=$(grep -m1 browser_download_url "${CACHE_FILE}" | cut -d '"' -f4)
    VERSION_PUBLISHED=${URL##*/tool_}
    VERSION_PUBLISHED=${VERSION_PUBLISHED%_all.deb}
fi''')
    credential = Path('/etc/dylathleen/github-token')
    credential.parent.mkdir(exist_ok=True)
    with credential.open('x') as output:
        credential.chmod(0o600)
        output.write('test-credential')
    try:
        response = MagicMock(status_code=200)
        response.iter_content.return_value = [b'{"assets":[{"browser_download_url":"https://github.com/example/test/tool_98.0_all.deb"}]}']
        with patch.object(checks.requests.Session, 'get') as get:
            get.return_value.__enter__.return_value = response
            checks.refresh(inventory.inventory(inventory.load()))
            get.assert_called_once()
            call = get.call_args
            assert call.args == ('https://api.github.com/repos/example/test/releases/latest',)
            assert call.kwargs['headers']['Authorization'] == 'Bearer test-credential'
            assert not call.kwargs['allow_redirects']
        assert inventory.load()['updates']['debget:dylathleen'] == '98.0'
        assert 'test-credential' not in inventory.CACHE.read_text()
    finally:
        credential.unlink()


def check_lifecycle(candidate, settings, before):
    timer = 'dylathleen-refresh.timer'
    subprocess.run(['dpkg', '-i', candidate], check=True)
    assert subprocess.run(['systemctl', 'is-enabled', '--quiet', timer]).returncode != 0
    service = 'dylathleend.service'
    deadline = time.monotonic() + 90
    while subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0:
        if time.monotonic() > deadline:
            raise AssertionError('Daemon did not exit after the idle timeout')
        time.sleep(2)
    subprocess.run(['systemctl', 'mask', '--runtime', service], check=True)
    try:
        data, warning = refresh(False)
        assert warning and 'debget' in data
    finally:
        subprocess.run(['systemctl', 'unmask', '--runtime', service], check=True)
    subprocess.run(['dpkg', '--purge', 'dylathleen'], check=True)
    assert settings.read_bytes() == before
    assert not Path('/var/lib/dylathleen/inventory.json').exists()
    subprocess.run(['dpkg', '-i', candidate], check=True)
    assert settings.read_bytes() == before
    subprocess.run(['systemctl', 'is-enabled', '--quiet', timer], check=True)
    data, warning = refresh(False)
    assert not warning and 'debget' in data


def main(previous, candidate):
    assert os.geteuid() == 0 and os.environ['DYLATHLEEN_TEST_VM'] == '1'
    spec = importlib.util.spec_from_file_location('fixtures', Path(__file__).with_name('check-debget.py'))
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    expected = subprocess.check_output(['dpkg-deb', '-f', candidate, 'Version'], text=True).strip()
    settings = Path('/home/tester/.config/dylathleen/debget.json')
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"include_unsupported":true,"refresh_interval":15}')
    before = settings.read_bytes()
    subprocess.run(['dpkg', '-i', previous], check=True)
    assert version() != expected
    with TemporaryDirectory() as root:
        folder = Path(root)
        package = Path(candidate).read_bytes()
        link = folder / ('dylathleen-' + hashlib.sha256(package).hexdigest()[:16] + '.deb')
        link.write_bytes(package)
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=root))
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            fixtures.RECIPES.mkdir(parents=True, exist_ok=True)
            recipe = fixtures.RECIPES / 'dylathleen'
            source = fixtures.FIXTURE.replace('1.0', expected).replace('https://example.invalid/fixture.deb',
                                      f'http://127.0.0.1:{server.server_port}/{link.name}')
            recipe.write_text(source)
            fixtures.STATE.write_text('dylathleen 1 direct\n')
            subprocess.run(['dpkg', '-i', candidate], check=True)
            assert version() == expected and settings.read_bytes() == before
            # The candidate must have shown a real plan and honored a rejection
            # on a machine where /run may be mounted noexec.
            calls = []
            try:
                backend.debget({'operation': 'native', 'action': 'reinstall', 'packages': ['dylathleen']},
                               '', Watcher(), lambda plan: calls.append(plan) or False)
            except Cancelled:
                pass
            assert calls and 'dylathleen' in calls[0]
            # Use the candidate's actual maintainer scripts and payload for a
            # version-changing self-upgrade while the initiating client exits.
            tree = folder / 'upgrade'
            subprocess.run(['dpkg-deb', '-R', candidate, tree], check=True)
            control = tree / 'DEBIAN/control'
            next_version = expected + '+fixture1'
            control.write_text(control.read_text().replace('Version: ' + expected + '\n',
                                                            'Version: ' + next_version + '\n'))
            upgraded = folder / 'dylathleen-upgrade.deb'
            subprocess.run(['dpkg-deb', '-b', tree, upgraded], check=True)
            recipe.write_text(source.replace(expected, next_version).replace(link.name, upgraded.name))
            detached_upgrade()
            assert version() == next_version and settings.read_bytes() == before
        finally:
            server.shutdown()
            server.server_close()
    guard_failures()
    indexes = [p for p in Path('/var/lib/apt/lists').glob('*') if p.is_file()]
    baseline = {p: hashlib.sha256(p.read_bytes()).digest() for p in
                (fixtures.STATE, Path('/var/lib/dpkg/status'), *Path('/etc/apt').rglob('*.list'), *indexes)}
    recipe.write_text(source.replace('VERSION_PUBLISHED=' + expected, '''
if [ "$(id -u)" -gt 0 ] && [ ! -w /etc/deb-get/installed ] && [ ! -w /var/lib/dpkg/status ]; then
    VERSION_PUBLISHED=99.0
fi'''))
    data, warning = refresh(True)
    assert not warning and data['updates']['debget:dylathleen'] == '99.0', (data, warning)
    assert all(hashlib.sha256(p.read_bytes()).digest() == digest for p, digest in baseline.items())
    recipe.write_text(source + '\nVERSION_PUBLISHED=""\n')
    data, _ = refresh(True)
    assert data['updates']['debget:dylathleen'] == '99.0' and 'debget:dylathleen' in data['stale']
    authenticated_check(recipe, source)
    fixtures.STATE.write_text('')
    data, _ = refresh(False)
    assert 'dylathleen' not in data['debget'] and not data['updates']
    api = interface()
    for minutes in (15, 0):
        wait(api, api.ConfigureTimer(minutes, signature='u'))
        enabled = subprocess.run(['systemctl', 'is-active', '--quiet', 'dylathleen-refresh.timer']).returncode == 0
        assert enabled == bool(minutes)
    check_lifecycle(candidate, settings, before)
    print('Package purge/reinstall and idle exit verified.')
    print('Upgrade, detached version-changing self-upgrade, isolated native check, stale cache, CLI changes and timer verified.')


if __name__ == '__main__':
    main(*sys.argv[1:])
