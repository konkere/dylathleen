"""End-to-end checks inside the disposable Jammy CI guest with deb-get 0.4.7 installed."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from dylathleen.daemon import client
from dylathleen.gems.bin.client import BinRegistry
from dylathleen.gems.debget.privileged import MARKER


class Watcher:
    def print(self, text):
        print(text, flush=True)

    def change_progress(self, value):
        pass


def invoke(action, name=None, approval='yes', user=False, **kwargs):
    cmd = ['runuser', '-u', 'tester', '--', sys.executable, __file__, 'debget', action, name or '', approval]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=240)
    return proc.returncode, proc.stdout


def debget(action, name, approval):
    _, events = client.debget({'operation': 'native', 'action': action, 'packages': [name] if name else []},
                              '', Watcher(), lambda plan: approval == 'yes')
    for event in events:
        print(MARKER + json.dumps(event))


def bin_roundtrip():
    import hashlib
    from tests.gems.bin.test_integration import BinIntegrationTest
    from dylathleen.gems.bin.client import BinCommand
    test = BinIntegrationTest()
    test.setUp(live_backend=True)
    try:
        target = '/usr/local/bin/dylath-test-tool'
        registry = BinRegistry(True)
        repo = test.repository('https://github.com/example/tool')
        release = repo.release('v1.0.0')
        with BinCommand('bin') as cmd:
            entry = cmd.install(release.url, 'tool-v1.0.0-linux.tar.gz', 'tool-v1.0.0/tool')
            registry.save(target, None, entry, '/usr/local/bin', cmd.target)
        row = registry.read()['bins'][target]
        assert hashlib.sha256(Path(target).read_bytes()).hexdigest() == row['hash']
        assert subprocess.check_output([target], text=True).strip() == 'tool v1.0.0'
        for pinned in (True, False):
            registry.save(target, row, {**row, 'pinned': pinned}, '/usr/local/bin')
            row = registry.read()['bins'][target]
            assert row['pinned'] == pinned
        with BinCommand('bin', row) as cmd:
            upgraded = cmd.update()
            registry.save(target, row, upgraded, '/usr/local/bin', cmd.target)
        assert subprocess.check_output([target], text=True).strip() == 'tool v2.0.0'
        moved = registry.migrate('/opt/dylath-test/bin')['bins']
        key = '/opt/dylath-test/bin/dylath-test-tool'
        registry.save(key, moved[key], None, '/opt/dylath-test/bin')
        assert not Path(key).exists()
    finally:
        test.doCleanups()


def concurrent_operations():
    import dbus
    api = client.interface()
    try:
        api.Refresh('yes', signature='s')
    except dbus.DBusException as error:
        assert error.get_dbus_name() == 'org.freedesktop.DBus.Error.InvalidArgs', error
    else:
        raise AssertionError('Refresh accepted an invalid wire signature')
    job, descriptor = api.Recipe('backups', 'fixture', '', '', '', signature='sssss')
    with socket.socket(fileno=descriptor.take()) as peer:
        assert api.GetOperation(job, signature='u')[1] == 'running'
        try:
            api.Refresh(False, signature='b')
        except dbus.DBusException as error:
            assert 'Another system operation is running' in str(error)
        else:
            raise AssertionError('Refresh overlapped an active transaction')
        peer.sendall(b'\n')
        with peer.makefile('r') as result:
            assert any('backups' in line for line in result)
    client.wait(api, job)


def main():
    assert os.geteuid() == 0 and os.environ['DYLATHLEEN_TEST_CONTAINER'] == '1'
    base = Path(__file__).with_name('check-debget.py')
    spec = importlib.util.spec_from_file_location('fixtures', base)
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    result = subprocess.run(['runuser', '-u', 'tester', '--', sys.executable, __file__, 'debget', 'install',
                             'fixture', 'yes'], capture_output=True, text=True)
    assert result.returncode and 'Denied' in result.stderr, result.stderr
    policy = Path('/etc/polkit-1/localauthority/50-local.d/dylath-test.pkla')
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text('[fixture]\nIdentity=unix-user:tester\nAction=ru.konkere.dylathleen.manage\n'
                      'ResultAny=yes\nResultInactive=yes\nResultActive=yes\n')
    try:
        concurrent_operations()
        fixtures.invoke = invoke
        with TemporaryDirectory() as folder:
            fixtures.test_removal(Path(folder))
            fixtures.test_direct_package(Path(folder))
        subprocess.run(['runuser', '-u', 'tester', '--', sys.executable, __file__, 'bin'], check=True)
    finally:
        policy.unlink()


if __name__ == '__main__':
    if sys.argv[1:2] == ['debget']:
        debget(*sys.argv[2:])
    elif sys.argv[1:2] == ['bin']:
        bin_roundtrip()
    else:
        main()
