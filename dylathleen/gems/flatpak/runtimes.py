"""Local Flatpak pin rules and application-to-deployment relationships."""
import logging
import re
import subprocess
from collections import defaultdict
from html import escape

from dylathleen.api.abstract.view import MessageType, TextComponent
from dylathleen.gems.flatpak.inventory import _read


def pin_rules(scope):
    return tuple(sorted(line.strip() for line in _read('pin', '--' + scope).splitlines()
                        if line.strip()))


def matches(rule, ref):
    # flatpak-pin accepts a partial runtime ref. '*' cannot cross '/'.
    parts = rule.removeprefix('runtime/').split('/')
    fields = ref.removeprefix('runtime/').split('/')
    return len(parts) <= len(fields) and all(
        not part or re.fullmatch(re.escape(part).replace(r'\*', '.*'), field)
        for part, field in zip(parts, fields))


def pin_plan(scope, ref):
    rules = tuple(rule for rule in pin_rules(scope) if matches(rule, ref))
    targets = set()
    for item in _read('list', '--' + scope, '--runtime', '--all', '--columns=ref').splitlines():
        candidate = 'runtime/' + item.strip().removeprefix('runtime/')
        if candidate == ref or any(matches(rule, candidate) for rule in rules):
            targets.add(candidate)
    if ref not in targets:
        raise ValueError(ref)
    return rules, sorted(targets)


def change_pin(pkg, watcher, i18n, remove):
    scope, ref = pkg.installation, pkg.ref
    title = i18n['flatpak.pin.remove' if remove else 'flatpak.pin.add']
    try:
        rules, targets = pin_plan(scope, ref)
        if bool(rules) != remove or rules != pkg.pin_rules:
            watcher.show_message(title, i18n['flatpak.pin.changed'], MessageType.WARNING)
            return True  # refresh stale inventory
        lines = [scope, i18n['flatpak.pin.rules'], *(rules or (ref,)),
                 i18n['flatpak.pin.affected'], *targets]
        if not watcher.request_confirmation(title, None, components=[TextComponent('<br>'.join(map(escape, lines)))],
                                            min_width=560, min_height=250,
                                            deny_label=i18n['cancel'], window_cancel=True):
            return True
        if (rules, targets) != pin_plan(scope, ref):
            watcher.show_message(title, i18n['flatpak.pin.changed'], MessageType.WARNING)
            return True
        args = ('--remove', *rules) if remove else (ref,)
        _read('pin', '--' + scope, *args)
        if any(matches(rule, ref) for rule in pin_rules(scope)) == remove:
            raise RuntimeError(i18n['flatpak.pin.changed'])
        return True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        watcher.show_message(title, escape(getattr(exc, 'stderr', None) or str(exc)), MessageType.ERROR)
        return False


def annotate_runtimes(rows, complete=True):
    import gi
    gi.require_version('Flatpak', '1.0')
    from gi.repository import Flatpak, GLib

    by_key = {(r['installation'], r['ref']): r for r in rows}
    children = defaultdict(set)
    parents = defaultdict(set)
    applications = defaultdict(set)
    for scope, constructor in [('user', Flatpak.Installation.new_user),
                               ('system', Flatpak.Installation.new_system)]:
        scoped = [r for r in rows if r['installation'] == scope]
        if not scoped:
            continue
        try:
            rules = pin_rules(scope)
            for row in scoped:
                if row['runtime']:
                    row['pin_rules'] = tuple(rule for rule in rules if matches(rule, row['ref']))
        except (OSError, subprocess.SubprocessError) as exc:
            logging.getLogger(__name__).warning('Flatpak pins (%s): %s', scope, exc)
        try:
            inst = constructor(None)
            for row in scoped:
                key = (scope, row['ref'])
                related = inst.list_installed_related_refs_sync(row['origin'], row['ref'], None)
                for rel in related:
                    child = (scope, rel.format_ref())
                    if child in by_key:
                        children[key].add(child)
                        parents[child].add(key)
                runtime = row.get('required_runtime')
                if not row['runtime'] and runtime:
                    runtime = 'runtime/' + runtime.removeprefix('runtime/')
                    target = (scope, runtime)
                    if target not in by_key and scope == 'user':
                        target = ('system', runtime)
                    if target in by_key:
                        children[key].add(target)
        except GLib.Error as exc:
            complete = False
            logging.getLogger(__name__).warning('Flatpak related refs (%s): %s', scope, exc)

    if complete:
        for app, row in by_key.items():
            if row['runtime']:
                continue
            pending = list(children[app])
            seen = set()
            while pending:
                child = pending.pop()
                if child not in seen:
                    seen.add(child)
                    applications[child].add((row['name'], app[1], app[0]))
                    pending.extend(children[child] - seen)
        for key, row in by_key.items():
            if row['runtime']:
                row['runtime_apps'] = sorted(applications[key])
                row['extension_parents'] = sorted(ref for _, ref in parents[key])
