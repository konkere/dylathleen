"""Exercise installed → remote → preview → updated against an isolated file remote."""
import gzip
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from dylathleen.gems.flatpak.inventory import inventory
from dylathleen.gems.flatpak.model import FlatpakApplication


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
    publish(root, 'Gimp', '3.2.4', 1)
    publish(root, 'Plexamp', '4.13.2', 1)
    flatpak('remote-add', '--user', '--no-gpg-verify', 'fixture', (root / 'repo').as_uri())
    flatpak('install', '--user', '-y', '--no-deps', 'fixture', 'org.example.Gimp', 'org.example.Plexamp')
    publish(root, 'Gimp', '3.2.8', 2)
    publish(root, 'Plexamp', '4.13.2', 2)
    models = {row['id']: FlatpakApplication(**row) for row in inventory('user', True)}
    labels = {'flatpak.update.build': 'build update', 'flatpak.update.unknown': 'unknown'}
    gimp = models['org.example.Gimp']
    plex = models['org.example.Plexamp']
    assert gimp.update and gimp.update_version_label(labels) == '3.2.4 → 3.2.8', vars(gimp)
    assert plex.update and 'build update' in plex.update_version_label(labels), vars(plex)
    print(gimp.update_version_label(labels), plex.update_version_label(labels), flush=True)
    flatpak('update', '--user', '-y', '--no-deps')
    assert not any(row.get('update') for row in inventory('user', True))
