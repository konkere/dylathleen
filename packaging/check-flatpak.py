"""Exercise installed → remote → preview → updated against an isolated file remote."""
import gzip
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from collections import defaultdict
import logging
from unittest.mock import Mock


def flatpak(*args):
    subprocess.run(['flatpak', *args], check=True)


def publish(root, name, version, build):
    app_id = 'org.example.' + name
    stage = root / name
    data = stage / 'files/share/app-info/xmls'
    data.mkdir(parents=True, exist_ok=True)
    (stage / 'export').mkdir(exist_ok=True)
    (stage / 'metadata').write_text(
        f'[Application]\nname={app_id}\nruntime=org.example.Platform/{arch}/stable\n'
        f'sdk=org.example.Sdk/{arch}/stable\ncommand=example\n')
    (stage / 'files/build').write_text(str(build))
    xml = (f'<components><component type="desktop-application"><id>{app_id}</id>'
           f'<name>{name}</name><summary>Fixture</summary><metadata_license>CC0-1.0</metadata_license>'
           f'<releases><release version="{version}" timestamp="1234567890"/></releases>'
           '</component></components>')
    (data / (app_id + '.xml.gz')).write_bytes(gzip.compress(xml.encode()))
    flatpak('build-export', '--update-appstream', root / 'repo', stage, 'stable')


with TemporaryDirectory() as temp:
    root = Path(temp)
    os.environ.update(HOME=temp, XDG_CACHE_HOME=temp + '/cache', XDG_DATA_HOME=temp + '/data',
                      XDG_CONFIG_HOME=temp + '/config', FLATPAK_USER_DIR=temp + '/installation',
                      LC_ALL='C.UTF-8', GSETTINGS_BACKEND='memory')
    arch = subprocess.check_output(['flatpak', '--default-arch'], text=True).strip()
    runtime = root / 'platform'
    (runtime / 'usr').mkdir(parents=True)
    (runtime / 'metadata').write_text(f'[Runtime]\nname=org.example.Platform\n'
                                      f'runtime=org.example.Platform/{arch}/stable\n'
                                      f'sdk=org.example.Sdk/{arch}/stable\n')
    flatpak('build-export', '--runtime', '--files=usr', root / 'repo', runtime, 'stable')
    publish(root, 'Gimp', '3.2.4', 1)
    publish(root, 'Plexamp', '4.13.2', 1)
    flatpak('remote-add', '--user', '--no-gpg-verify', 'fixture', (root / 'repo').as_uri())
    flatpak('install', '--user', '-y', 'fixture', 'org.example.Gimp', 'org.example.Plexamp')
    publish(root, 'Gimp', '3.2.8', 2)
    publish(root, 'Plexamp', '4.13.2', 2)
    # Import after isolating paths: never inspect the host's real installations.
    os.environ['FLATPAK_SYSTEM_DIR'] = temp + '/system'
    from dylathleen.gems.flatpak.controller import FlatpakManager
    context = Mock()
    context.logger = logging.getLogger(__name__)
    context.i18n = defaultdict(str)
    context.cache_factory.new.return_value.get.return_value = None
    manager = FlatpakManager(context)
    models = {p.id: p for p in manager.read_installed(None, internet_available=True).installed}
    labels = {'flatpak.update.build': 'build update', 'flatpak.update.unknown': 'unknown'}
    gimp = models['org.example.Gimp']
    plex = models['org.example.Plexamp']
    assert gimp.update and gimp.update_version_label(labels) == '3.2.4 → 3.2.8', vars(gimp)
    assert plex.update and 'build update' in plex.update_version_label(labels), vars(plex)
    print(gimp.update_version_label(labels), plex.update_version_label(labels), flush=True)
    watcher = Mock()
    plan = manager.get_upgrade_requirements([gimp, plex], None, watcher)
    assert all(req.download_is_upper_bound and req.required_size > 0 and req.extra_size is None
               for req in plan.to_upgrade), [vars(req) for req in plan.to_upgrade]
    before_confirmation = manager.read_installed(None, internet_available=False).installed
    assert {pkg.id: pkg.commit for pkg in before_confirmation} == {key: pkg.commit for key, pkg in models.items()}
    assert manager.upgrade(plan, None, watcher)
    refreshed = manager.read_installed(None, internet_available=True).installed
    assert not any(pkg.update for pkg in refreshed)
    assert next(pkg for pkg in refreshed if pkg.id == gimp.id).version == '3.2.8'
