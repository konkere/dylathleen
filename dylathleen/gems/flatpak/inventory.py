"""Read deployment state and the matching remote's update set (Flatpak 1.12+)."""
import logging
import os
import subprocess
from packaging.version import InvalidVersion, Version
from concurrent.futures import ThreadPoolExecutor


def _read(*args):
    result = subprocess.run(['flatpak', *args], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, 'LC_ALL': 'C.UTF-8'}, check=True)
    return result.stdout


def _updates(scope):
    return _read('remote-ls', '--' + scope, '--updates', '--all', '--arch=*',
                 '--columns=ref,origin,commit,version')


def catalogue_version(installed, reported):
    # An older catalogue release is insufficient evidence of a downgrade.
    if installed and reported:
        try:
            if Version(reported) < Version(installed):
                return None
        except InvalidVersion:
            pass
    return reported or None


def inventory(scope, online):
    columns = 'ref,origin,active,version,name,description,options'
    output = _read('list', '--' + scope, '--all', '--columns=' + columns)
    rows = {}
    for line in output.splitlines():
        parts = line.split('\t')
        if len(parts) != 7:
            continue
        ref, origin, commit, version, name, description, options = parts
        runtime = 'runtime' in options.split(',')
        if ref.count('/') == 2:
            ref = ('runtime/' if runtime else 'app/') + ref
        if ref.count('/') != 3:
            continue
        kind, app_id, arch, branch = ref.split('/')
        rows[ref, origin] = dict(id=app_id, arch=arch, branch=branch, ref=ref,
                                 runtime=kind == 'runtime', installation=scope, origin=origin,
                                 commit=commit, version=version or None, name=name or app_id,
                                 description=description, partial='partial' in options)
    if online and rows:
        try:
            pending = _updates(scope)
            if pending.strip():
                # The version column is backed by the remote's AppStream catalogue.
                # Refresh it before describing a target. Keep commit-only updates on failure.
                fresh = False
                try:
                    _read('update', '--' + scope, '--appstream')
                    pending = _updates(scope)
                    fresh = True
                except (OSError, subprocess.SubprocessError) as exc:
                    logging.getLogger(__name__).warning('Flatpak catalogue refresh: %s', exc)
                for line in pending.splitlines():
                    parts = line.split('\t')
                    if len(parts) < 3:
                        continue
                    ref, origin, commit = parts[:3]
                    row = rows.get((ref, origin))
                    if row and commit and commit != row['commit']:
                        row.update(update=True, target_commit=commit,
                                   latest_version=catalogue_version(row['version'], parts[3])
                                   if fresh and len(parts) == 4 else None)
        except (OSError, subprocess.SubprocessError) as exc:
            logging.getLogger(__name__).warning('Flatpak update check (%s): %s', scope, exc)
    return list(rows.values())


def deployments(online):
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = {scope: pool.submit(inventory, scope, online) for scope in ('system', 'user')}
        rows = []
        for scope, task in tasks.items():
            try:
                rows.extend(task.result())
            except (OSError, subprocess.SubprocessError) as exc:
                logging.getLogger(__name__).warning('Cannot list Flatpak installation (%s): %s', scope, exc)
        return rows
