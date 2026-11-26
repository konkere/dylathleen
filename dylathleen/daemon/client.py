"""Synchronous system-bus client for the managers' existing worker threads."""
import fcntl
import json
import os
import shutil
import socket
import time

from dylathleen.daemon.inventory import load
from dylathleen.daemon.protocol import NAME, PATH


def interface():
    import dbus
    bus = dbus.SystemBus(private=True)
    return dbus.Interface(bus.get_object(NAME, PATH, introspect=False), NAME)


def wait(api, job):
    while True:
        _, status, error = api.GetOperation(job, signature='u')
        if status == 'done':
            return
        if status == 'failed':
            raise OSError(str(error))
        time.sleep(.2)


def refresh(online):
    try:
        api = interface()
        wait(api, api.Refresh(online, signature='b'))
        return load(), ''
    except Exception:
        return load(), 'System service unavailable. Showing last known package data.'


def change_bin(request, artifact):
    from dbus.types import UnixFd

    def fields(data):
        return {k: ('true' if v else 'false') if isinstance(v, bool) else str(v) for k, v in (data or {}).items()}

    api = interface()
    files = []
    entry = request.get('entry')
    if artifact:
        action = 'update' if request['expected'] else 'install'
        with artifact.open('rb') as source, os.fdopen(os.memfd_create('bin', os.MFD_ALLOW_SEALING), 'w+b') as staged:
            shutil.copyfileobj(source, staged)
            staged.flush()
            fcntl.fcntl(staged, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK)
            staged.seek(0)
            files.append(UnixFd(staged))
    elif request['operation'] == 'migrate':
        action = 'migrate'
    elif entry is None:
        action = 'remove'
    else:
        action = 'pin' if entry.get('pinned') else 'unpin'
    job = api.Bin(action, request.get('key', ''), fields(request.get('expected')), fields(entry),
                  request['directory'], files, signature='ssa{ss}a{ss}sah', timeout=125)
    wait(api, job)
    return {'bins': load().get('bins', {})}


def debget(request, token, watcher, confirm):
    from dylathleen.gems.debget import client
    api = interface()
    if request['operation'] == 'native':
        job, descriptor = api.DebGet(request['action'], request['packages'], signature='sas', timeout=125)
    else:
        job, descriptor = api.Recipe(request['operation'], request['name'], request.get('expected') or '',
                                     request.get('revision', ''), request.get('source', ''), signature='sssss', timeout=125)
    lines, events = [], []
    cancelled = False
    with socket.socket(fileno=descriptor.take()) as peer, peer.makefile('r', errors='replace') as reader:
        peer.sendall(token.encode() + b'\n')
        for raw in reader:
            line = client.ESCAPES.sub('', raw).strip()
            if line.startswith(client.MARKER):
                event = json.loads(line[len(client.MARKER):])
                events.append(event)
                if event['event'] == 'transaction':
                    approved = confirm(event['plan'])
                    peer.sendall(b'yes\n' if approved else b'no\n')
                    cancelled |= not approved
            elif line:
                lines.append(line)
                watcher.print(line)
                match = client.PROGRESS.search(line)
                if match:
                    watcher.change_progress(min(99, int(match[1])))
                if client.EULA.search(line):
                    approved = confirm('\n'.join(lines[-80:]))
                    peer.sendall(b'1\n' if approved else b'2\n')
                    cancelled |= not approved
    errors = [e['message'] for e in events if e['event'] == 'error']
    try:
        wait(api, job)
    except OSError:
        if cancelled:
            raise client.Cancelled()
        if errors:
            raise OSError('\n'.join(errors))
        raise
    if cancelled:
        raise client.Cancelled()
    return lines, events
