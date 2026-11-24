"""Exercise real deb-get in a disposable container with APT and sudo available."""
import json
import os
import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from dylathleen.gems.debget.privileged import MARKER

ROOT = Path('/etc/deb-get')
STATE = ROOT / 'installed'
RECIPES = ROOT / '99-local.d'
DOWNLOADED = ROOT / '01-main.d'
WRAPPER = Path('/usr/local/bin/deb-get')
HELPER = Path('/usr/lib/dylathleen/dylathleen/gems/debget/privileged.py')
FIXTURE = '\n'.join((
    'DEFVER=1', 'PRETTY_NAME="Fixture"', 'SUMMARY="Fixture package"', 'WEBSITE=https://example.invalid',
    'ARCHS_SUPPORTED="amd64 arm64"', 'VERSION_PUBLISHED=1.0', 'URL=https://example.invalid/fixture.deb', '',
))


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def build(directory, name, version='1.0'):
    tree = directory / (name + '-' + version)
    (tree / 'DEBIAN').mkdir(parents=True)
    (tree / 'DEBIAN/control').write_text(f'Package: {name}\nVersion: {version}\nArchitecture: all\n'
                                      f'Maintainer: Fixture <test@example.invalid>\nDescription: {name}\n')
    conffile = f'/etc/{name}.conf'
    (tree / 'etc').mkdir()
    (tree / conffile[1:]).write_text('fixture\n')
    (tree / 'DEBIAN/conffiles').write_text(conffile + '\n')
    destination = directory / (name + '-' + version + '.deb')
    command('dpkg-deb', '--build', str(tree), str(destination), stdout=subprocess.DEVNULL)
    return destination


def invoke(operation, name=None, approval='yes', user=False, **extra):
    request = {'operation': 'native', 'action': operation, 'packages': [name] if name else [], **extra}
    executable = ['/usr/bin/python3', '-I', str(HELPER)]
    if user:
        executable = ['runuser', '-u', 'tester', '--', 'sudo', '-n', '-H', '--', *executable]
    result = subprocess.Popen(executable, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
    result.stdin.write(json.dumps(request) + '\n')
    result.stdin.flush()
    output = []
    for line in result.stdout:
        output.append(line)
        if line.startswith(MARKER) and json.loads(line[len(MARKER):])['event'] == 'transaction':
            result.stdin.write(approval + '\n')
            result.stdin.flush()
    return result.wait(timeout=30), ''.join(output)


def status(name):
    return subprocess.run(['dpkg-query', '-W', '-f=${db:Status-Status}', name],
                          text=True, capture_output=True).stdout


def version(name):
    return command('dpkg-query', '-W', '-f=${Version}', name, capture_output=True).stdout


def test_removal(directory):
    keep = 'dylath-test-unrelated'
    first = 'dylath-test-remove'
    second = 'dylath-test-purge'
    apt_fixture = 'dylath-test-apt'
    files = [build(directory, name) for name in (keep, first, second, apt_fixture)]
    command('dpkg', '-i', *map(str, files), stdout=subprocess.DEVNULL)
    command('apt-mark', 'auto', keep, stdout=subprocess.DEVNULL)
    assert f'Remv {keep}' in command('apt-get', '-s', 'autoremove', capture_output=True).stdout
    RECIPES.mkdir(parents=True, exist_ok=True)
    for name in (first, second):
        (RECIPES / name).write_text(FIXTURE)
    (RECIPES / apt_fixture).write_text(FIXTURE + 'APT_REPO_URL="https://example.invalid stable main"\n'
                                     'ASC_KEY_URL=https://example.invalid/example.asc\n')
    STATE.write_text(f'{apt_fixture} 1 apt\n{first} 1 direct\n{second} 1 direct\n')
    before = STATE.read_bytes()
    # Keep catalogue refresh offline while exercising the real update path.
    (ROOT / '01-main.repo').write_text('file:///unused\n#cawbird\n')
    DOWNLOADED.mkdir()
    archived = DOWNLOADED / 'cawbird'
    archived.write_text(FIXTURE)
    archived.chmod(0o664)
    (RECIPES / first).write_text(FIXTURE.replace('VERSION_PUBLISHED=1.0', 'VERSION_PUBLISHED=2.0'))
    code, output = invoke('update', user=True)
    assert code == 0 and 'has an update pending. 2.0 is available' in output, output
    assert STATE.read_bytes() == before and status(first) == 'installed'
    (RECIPES / first).write_text(FIXTURE)
    code, output = invoke('upgrade', user=True)
    assert code == 0 and status(keep) == 'installed' and '"event": "transaction"' in output, output
    code, output = invoke('remove', first, 'no')
    assert code and status(first) == 'installed' and STATE.read_bytes() == before, output
    code, output = invoke('remove', keep)
    assert code and status(keep) == 'installed' and STATE.read_bytes() == before, output
    code, output = invoke('remove', first, user=True)
    assert code == 0 and status(first) == 'config-files' and status(keep) == 'installed', output
    assert FIRST_LINE not in STATE.read_text() and SECOND_LINE in STATE.read_text(), output

    # A PATH wrapper must not be called. This marker
    # exposes the attempt and the intentionally failing wrapper stops the test.
    WRAPPER.write_text('#!/bin/bash\necho WRAPPER_CALLED >&2\nexit 42\n')
    WRAPPER.chmod(0o755)
    code, output = invoke('purge', second, user=True)
    assert code == 0 and status(second) != 'installed' and status(second) != 'config-files', output
    assert 'WRAPPER_CALLED' not in output and status(keep) == 'installed', output
    assert SECOND_LINE not in STATE.read_text(), output
    assert not Path(f'/etc/{second}.conf').exists()
    assert (RECIPES / first).exists() and (RECIPES / second).exists()
    print('Native update and upgrade, scoped remove/purge, retained orphan, registry rollback and wrapper bypass verified.')


def test_direct_package(directory):
    name = 'dylath-test-direct'
    manifest = ROOT / '01-main.repo'
    manifest.write_text(manifest.read_text() + name + '\n')
    build(directory, name, '1.0')
    build(directory, name, '2.0')
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(directory)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for published in ('1.0', '2.0'):
            recipe = FIXTURE.replace('VERSION_PUBLISHED=1.0', f'VERSION_PUBLISHED={published}').replace(
                'https://example.invalid/fixture.deb', f'http://127.0.0.1:{server.server_port}/{name}-{published}.deb')
            path = DOWNLOADED / name
            path.write_text(recipe)
            path.chmod(0o664)
            if published == '2.0':
                show_code, shown = invoke('show', name, user=True)
                assert show_code == 0 and '2.0.deb' in shown, shown
                code, output = invoke('update', user=True)
                assert code == 0 and f'{name} (1.0) has an update pending. 2.0 is available.' in output, (
                    output, shown, STATE.read_text(), version(name), status(name))
            code, output = invoke('install', name, user=True)
            assert (code == 0 and version(name) == published and status(name) == 'installed'
                    and f'{name} 1 direct' in STATE.read_text()), output
        code, output = invoke('reinstall', name, user=True)
        assert code == 0 and version(name) == '2.0' and status(name) == 'installed', output
        code, output = invoke('show', name, user=True)
        assert code == 0 and 'Published:' in output and '2.0.deb' in output, output
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    print('Native catalogue install, update, reinstall and version resolution verified with upstream archive modes.')


FIRST_LINE = 'dylath-test-remove 1 direct'
SECOND_LINE = 'dylath-test-purge 1 direct'

if __name__ == '__main__':
    assert os.geteuid() == 0 and os.environ.get('DYLATHLEEN_TEST_CONTAINER') == '1'
    assert not WRAPPER.exists()
    with TemporaryDirectory() as directory:
        test_removal(Path(directory))
        test_direct_package(Path(directory))
