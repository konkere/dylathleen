import os
import socket
import subprocess
from pathlib import Path
import time
from collections import OrderedDict
from threading import Thread

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from dbus.types import UnixFd
from gi.repository import GLib

from dylathleen.daemon import inventory, operations, checks
from dylathleen.daemon.protocol import ACTION, NAME, PATH
from dylathleen.gems.debget.privileged import LIMIT, package, secure, atomic_file

method = dbus.service.method
mutation = dict(sender_keyword='sender', async_callbacks=('success', 'failure'))


class Backend(dbus.service.Object):
    def __init__(self, bus, loop):
        self.name = dbus.service.BusName(NAME, bus, do_not_queue=True)
        super().__init__(self.name, PATH)
        self.bus = bus
        self.loop = loop
        self.last = time.monotonic()
        self.pending = set()
        self.active = None
        self.jobs = OrderedDict()
        self.sequence = 0
        self.want_online = False
        GLib.timeout_add_seconds(10, self.idle)

    def idle(self):
        if not self.active and not self.pending and time.monotonic() - self.last > 60:
            self.loop.quit()
        return True

    def start(self, label, function):
        if self.active:
            raise ValueError('Another system operation is running')
        self.sequence += 1
        job = self.sequence
        self.active = job
        self.jobs[job] = (label, 'running', '')
        if len(self.jobs) > 32:
            self.jobs.popitem(last=False)

        self.run_job(job, label, function)
        return job

    def run_job(self, job, label, function):
        def work():
            try:
                function()
                status, error = 'done', ''
            except Exception as exc:
                # Privileged subprocess output and paths must not reach the public bus.
                status, error = 'failed', f'{label} failed ({type(exc).__name__})'
            GLib.idle_add(self.finish, job, label, status, error)

        Thread(target=work, daemon=False).start()

    def finish(self, job, label, status, error):
        if label == 'refresh' and status == 'done' and self.want_online and not self.did_online:
            self.run_job(job, label, self.refresh_inventory)
            return False
        self.jobs[job] = label, status, error
        self.active = None
        self.want_online = False
        self.last = time.monotonic()
        return False

    def authorize(self, sender, success, failure, begin):
        if sender in self.pending or len(self.pending) >= 8:
            raise ValueError('Authorization busy')
        self.pending.add(sender)

        def denied(error):
            self.pending.discard(sender)
            failure(dbus.DBusException('Authorization unavailable or denied', name=NAME + '.Denied'))

        def decision(answer):
            self.pending.discard(sender)
            if not answer[0]:
                denied(None)
                return
            try:
                success(*begin())
            except Exception as error:
                failure(error)
        try:
            policy = self.bus.get_object('org.freedesktop.PolicyKit1', '/org/freedesktop/PolicyKit1/Authority',
                                         introspect=False)
            policy.CheckAuthorization(('system-bus-name', {'name': sender}), ACTION, {}, 1, '',
                                      dbus_interface='org.freedesktop.PolicyKit1.Authority',
                                      signature='(sa{sv})sa{ss}us', reply_handler=decision, error_handler=denied,
                                      timeout=120)
        except Exception as error:
            denied(error)

    @method(NAME, in_signature='', out_signature='a{sa{ss}}a{s(sss)}a{ss}as')
    def GetInventory(self):
        data = inventory.load()
        bins = {k: {f: str(v).lower() if isinstance(v, bool) else str(v) for f, v in row.items()}
                for k, row in data.get('bins', {}).items()}
        return bins, data.get('debget', {}), data.get('updates', {}), data.get('stale', [])

    @method(NAME, in_signature='u', out_signature='u', **mutation)
    def ConfigureTimer(self, minutes, sender, success, failure):
        minutes = int(minutes)
        if minutes and not 5 <= minutes <= 10080:
            raise ValueError('Check interval must be zero or 5 to 10080 minutes')
        def configure():
            systemctl = '/usr/bin/systemctl'
            timer = 'dylathleen-refresh.timer'
            dropin = Path('/etc/systemd/system/dylathleen-refresh.timer.d')
            if minutes:
                if not secure(dropin, directory=True, missing=True):
                    dropin.mkdir(mode=0o755)
                atomic_file(dropin / 'interval.conf', '[Timer]\nOnCalendar=\nOnBootSec=5min\n'
                            f'OnUnitActiveSec={minutes}min\n')
                subprocess.run([systemctl, 'daemon-reload'], check=True, timeout=30)
                subprocess.run([systemctl, 'enable', '--now', timer], check=True, timeout=30)
                subprocess.run([systemctl, 'restart', timer], check=True, timeout=30)
            else:
                subprocess.run([systemctl, 'disable', '--now', timer], check=True, timeout=30)
        self.authorize(sender, success, failure, lambda: (self.start('timer', configure),))

    @method(NAME, in_signature='u', out_signature='sss')
    def GetOperation(self, job):
        return self.jobs[int(job)]

    @method(NAME, in_signature='b', out_signature='u')
    def Refresh(self, online):
        if self.active and self.jobs[self.active][0] == 'refresh':
            self.want_online |= bool(online)
            return self.active
        self.want_online = bool(online)
        self.did_online = False
        return self.start('refresh', self.refresh_inventory)

    def refresh_inventory(self):
        data = inventory.inventory(inventory.load())
        inventory.persist(data)
        if self.want_online:
            self.did_online = True
            checks.refresh(data)

    def stream(self, request):
        client, server = socket.socketpair()
        try:
            def run():
                try:
                    operations.debget(request, server)
                finally:
                    inventory.persist(inventory.inventory(inventory.load()))
            job = self.start('deb-get', run)
            return job, UnixFd(client)
        except Exception:
            server.close()
            raise
        finally:
            client.close()

    @method(NAME, in_signature='sas', out_signature='uh', **mutation)
    def DebGet(self, action, packages, sender, success, failure):
        if action not in operations.ACTIONS or len(packages) != (0 if action in ('update', 'catalogue', 'upgrade') else 1):
            raise ValueError('Invalid deb-get operation')
        request = {'operation': 'native', 'action': action, 'packages': [package(p) for p in packages]}
        self.authorize(sender, success, failure, lambda: self.stream(request))

    @method(NAME, in_signature='sssss', out_signature='uh', **mutation)
    def Recipe(self, action, name, expected, revision, source, sender, success, failure):
        if action not in operations.RECIPES or len(source.encode()) > LIMIT:
            raise ValueError('Invalid recipe operation')
        request = dict(operation=action, name=package(name), expected=expected or None, revision=revision, source=source)
        self.authorize(sender, success, failure, lambda: self.stream(request))

    @method(NAME, in_signature='ssa{ss}a{ss}sah', out_signature='u', **mutation)
    def Bin(self, action, key, expected, entry, directory, files, sender, success, failure):
        if (action not in ('install', 'update', 'pin', 'unpin', 'remove', 'migrate')
                or len(files) != int(action in ('install', 'update')) or len(key) > 4096
                or any(k not in inventory.FIELDS or len(v) > 4096 for d in (entry, expected) for k, v in d.items())):
            raise ValueError('Invalid bin arguments')
        def unpack(fields):
            data = dict(fields)
            if 'pinned' in data:
                data['pinned'] = data['pinned'] == 'true'
            return data

        def run():
            if files:
                with os.fdopen(files[0].take(), 'rb') as artifact:
                    operations.bin_change(action, key, unpack(expected), unpack(entry), directory, artifact)
            else:
                operations.bin_change(action, key, unpack(expected), unpack(entry), directory)
            inventory.persist(inventory.inventory(inventory.load()))
        self.authorize(sender, success, failure, lambda: (self.start('bin', run),))


def main():
    os.environ.clear()
    os.environ.update(HOME='/root', PATH='/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL='C.UTF-8')
    DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()
    Backend(dbus.SystemBus(), loop)
    loop.run()
