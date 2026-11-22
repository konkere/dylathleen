"""Replace integration files first and atomically publish the executable last."""
import fcntl
import json
import logging
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp


def _child(parent: Path, name: str) -> Path:
    if not name or Path(name).name != name or name.startswith('.'):
        raise ValueError(f'Invalid installation path component: {name!r}')
    return parent / name


def _files(work, manifest, desktop_dir):
    live = _child(work.parent.parent, manifest['installation'])
    for index, (directory, name, existed) in enumerate(manifest['files']):
        parent = desktop_dir if directory == 'desktop' else live
        target = _child(parent, name)
        prefix = f'.dylathleen-{work.name}-{index}'
        yield target, parent / f'{prefix}.new', parent / f'{prefix}.old', existed


def _finish(work, desktop_dir):
    journal = work / 'journal'
    if journal.exists():
        manifest = json.loads(journal.read_text())
        files = list(_files(work, manifest, desktop_dir))
        armed = work / 'prepared'
        if armed.exists() and (work / 'image').exists():
            # The image rename is the commit point. A remaining image means
            # a partial switch must be undone, including after a process exit.
            for target, pending, backup, existed in reversed(files):
                if not pending.exists():
                    if backup.exists():
                        os.replace(backup, target)
                    elif not existed:
                        target.unlink(missing_ok=True)
        armed.unlink(missing_ok=True)
        for _, pending, backup, _ in files:
            pending.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
        journal.unlink()
    shutil.rmtree(work)


@contextmanager
def installation_lock(root, desktop_dir):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root, os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for work in (root / '.transactions').glob('replace-*'):
            _finish(work, Path(desktop_dir))
        yield
    finally:
        os.close(fd)


@contextmanager
def staging(root, desktop_dir):
    """Caller holds the installation lock throughout the transaction."""
    transactions = Path(root) / '.transactions'
    transactions.mkdir(mode=0o700, exist_ok=True)
    work = Path(mkdtemp(prefix='replace-', dir=transactions))
    try:
        yield work
    except BaseException:
        _finish(work, Path(desktop_dir))
        raise
    else:
        # Once the new image is live, cleanup errors cannot undo the commit.
        # The next lock holder will resume disposal of any leftover backups.
        try:
            _finish(work, Path(desktop_dir))
        except OSError:
            logging.getLogger(__name__).exception('Deferred AppImage cleanup in %s', work)


def publish(work, image, desktop, contents):
    """contents maps final metadata, icon and launcher paths to staged files."""
    manifest = {'installation': image.parent.name, 'files': []}
    desktop.parent.mkdir(parents=True, exist_ok=True)
    for target in contents:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f'Unexpected integration file: {target}')
        manifest['files'].append(('desktop' if target == desktop else 'installation',
                                  target.name, target.exists()))

    journal = work / 'journal.tmp'
    journal.write_text(json.dumps(manifest))
    journal.rename(work / 'journal')
    files = list(_files(work, manifest, desktop.parent))
    for target, pending, backup, existed in files:
        shutil.copy2(contents[target], pending)
        if existed:
            pending.chmod(target.stat().st_mode & 0o777)
            os.link(target, backup)
    (work / 'prepared').touch()
    for target, pending, _, _ in files:
        os.replace(pending, target)
    os.replace(work / 'image', image)
