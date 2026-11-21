"""Exercise pin edits against a disposable Flatpak installation, including shared rules."""
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from dylathleen.gems.flatpak.inventory import _read, deployments
from dylathleen.gems.flatpak.model import FlatpakApplication
from dylathleen.gems.flatpak.runtimes import change_pin, pin_rules
from dylathleen.view.util.translation import I18n

i18n = I18n('en', {}, 'en', {})
with TemporaryDirectory() as tmp:
    root = Path(tmp)
    os.environ.update(FLATPAK_USER_DIR=tmp + '/installation', FLATPAK_SYSTEM_DIR=tmp + '/system',
                      HOME=tmp, XDG_DATA_HOME=tmp + '/data', XDG_CACHE_HOME=tmp + '/cache',
                      XDG_CONFIG_HOME=tmp + '/config', GSETTINGS_BACKEND='memory')
    (root / 'system').mkdir()
    arch = _read('--default-arch').strip()
    name = 'org.example.Platform'
    refs = [f'runtime/{name}/{arch}/{branch}' for branch in ('one', 'two')]
    for ref in refs:
        _, _, _, branch = ref.split('/')
        stage = root / branch
        (stage / 'usr').mkdir(parents=True)
        (stage / 'metadata').write_text(f'[Runtime]\nname={name}\nruntime={ref[8:]}\nsdk={ref[8:]}\n')
        _read('build-export', '--runtime', '--files=usr', str(root / 'repo'), str(stage), branch)
    _read('remote-add', '--user', '--no-gpg-verify', 'fixture', (root / 'repo').as_uri())
    # An initialized but empty system installation keeps both scopes readable.
    shutil.copytree(root / 'installation' / 'repo', root / 'system' / 'repo')
    _read('install', '--user', '-y', 'fixture', *refs)
    for ref in refs:
        _read('pin', '--user', '--remove', ref)

    def first():
        row = next(r for r in deployments(False) if r['ref'] == refs[0])
        return FlatpakApplication(**row, installed=True)

    watcher = Mock()
    watcher.request_confirmation.return_value = True
    assert change_pin(first(), watcher, i18n, remove=False) and pin_rules('user') == (refs[0],)
    assert first().pin_rules == (refs[0],)
    assert change_pin(first(), watcher, i18n, remove=True) and not pin_rules('user')
    # Two overlapping patterns, one matching both installed branches.
    _read('pin', '--user', name, name + '//one')
    watcher.request_confirmation.return_value = False
    before = pin_rules('user')
    assert change_pin(first(), watcher, i18n, remove=True)
    assert before == pin_rules('user')
    preview = watcher.request_confirmation.call_args.kwargs['components'][0].value
    assert all(ref in preview for ref in refs) and all(rule in preview for rule in before)
    watcher.request_confirmation.return_value = True
    assert change_pin(first(), watcher, i18n, remove=True)
    assert not pin_rules('user'), pin_rules('user')
    print('Exact and overlapping pins, confirmation, cancellation and scoped removal passed.')
