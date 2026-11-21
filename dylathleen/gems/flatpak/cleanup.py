"""Interactive cleanup of unused deployments in the default Flatpak installations."""
from html import escape

from dylathleen.api.abstract.view import InputOption, MessageType, MultipleSelectComponent


def unused(installation):
    installation.drop_caches(None)
    return {ref.format_ref() for ref in installation.list_unused_refs(None, None)
            if ref.format_ref().startswith('runtime/')}


def remove_selected(api, installation, selected):
    """Check the resolved transaction before allowing its first operation."""
    transaction = api.Transaction.new_for_installation(installation, None)
    transaction.set_disable_related(True)
    transaction.set_include_unused_uninstall_ops(False)
    for ref in sorted(selected):
        transaction.add_uninstall(ref)

    differences = set()

    def ready(tx):
        operations = tx.get_operations()
        planned = {op.get_ref() for op in operations}
        differences.update(planned ^ selected)
        differences.update(selected - unused(installation))
        return (not differences and
                all(op.get_operation_type() == api.TransactionOperationType.UNINSTALL
                    for op in operations))

    transaction.connect('ready', ready)
    try:
        transaction.run(None)
    except Exception:
        if not differences:
            raise
    return differences


def clean_unused(i18n, watcher):
    title = i18n['flatpak.cleanup.title']
    completed = []
    try:
        import gi
        gi.require_version('Flatpak', '1.0')
        from gi.repository import Flatpak
        installations = {'user': Flatpak.Installation.new_user(None),
                         'system': Flatpak.Installation.new_system(None)}
        candidates = {scope: unused(inst) for scope, inst in installations.items()}
        selection = candidates
        body = i18n['flatpak.cleanup.choose']
        while any(candidates.values()):
            groups = {}
            for scope, refs in candidates.items():
                if refs:
                    options = [InputOption(ref, ref) for ref in sorted(refs)]
                    groups[scope] = MultipleSelectComponent(
                        label=i18n['flatpak.cleanup.' + scope], options=options,
                        default_options={op for op in options if op.value in selection.get(scope, set())})
            if not watcher.request_confirmation(title, body, components=list(groups.values()),
                                               confirmation_label=i18n['flatpak.cleanup.remove'],
                                               deny_label=i18n['cancel'], window_cancel=True,
                                               min_width=620, min_height=320, count_selected=True):
                return True
            selection = {scope: set(group.get_selected_values()) for scope, group in groups.items()}
            current = {scope: unused(inst) for scope, inst in installations.items()}
            if current != candidates:
                candidates = current
                body = i18n['flatpak.cleanup.changed']
                continue
            for scope, refs in selection.items():
                if not refs:
                    continue
                inst = installations[scope]
                differences = remove_selected(Flatpak, inst, refs)
                if differences:
                    current = {key: unused(value) for key, value in installations.items()}
                    details = '<br>'.join(map(escape, sorted(differences)))
                    if current == candidates:
                        watcher.show_message(title, i18n['flatpak.cleanup.conflict'] + '<br>' + details,
                                             MessageType.WARNING)
                        return False
                    candidates = current
                    body = i18n['flatpak.cleanup.changed'] + '<br>' + details
                    break
                inst.drop_caches(None)
                remaining = refs.intersection(ref.format_ref() for ref in inst.list_installed_refs(None))
                if remaining:
                    raise RuntimeError(', '.join(sorted(remaining)))
                result = i18n['flatpak.cleanup.done'].format(scope, len(refs))
                completed.append(result)
                watcher.print(result)
            else:
                if completed:
                    watcher.show_message(title, '<br>'.join(completed))
                return True
        watcher.show_message(title, i18n['flatpak.cleanup.empty'])
        return True
    except Exception as exc:
        watcher.print(str(exc))
        watcher.show_message(title, '<br>'.join(completed + [escape(str(exc))]), MessageType.ERROR)
        return False
