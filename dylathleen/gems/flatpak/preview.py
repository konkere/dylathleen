"""Resolve Flatpak download bounds before the user confirms the upgrade."""
import logging


def download_bounds(scope, packages):
    import gi
    gi.require_version('Flatpak', '1.0')
    from gi.repository import Flatpak, GLib

    bounds = {}

    def read_plan(transaction):
        for operation in transaction.get_operations():
            if operation.get_is_skipped() or operation.get_operation_type() == Flatpak.TransactionOperationType.UNINSTALL:
                continue
            size = operation.get_download_size()
            # Older Flatpak versions omit separately fetched extra-data from this estimate.
            metadata = operation.get_metadata()
            if metadata and metadata.has_group('Extra Data'):
                size = None
            bounds[operation.get_ref()] = size or None
        return False

    try:
        constructor = Flatpak.Installation.new_user if scope == 'user' else Flatpak.Installation.new_system
        transaction = Flatpak.Transaction.new_for_installation(constructor(None), None)
        transaction.set_no_interaction(True)
        transaction.add_default_dependency_sources()
        for pkg in packages:
            if pkg.update_component:
                transaction.add_install(pkg.origin, pkg.ref, None)
            else:
                transaction.add_update(pkg.ref, None, None)
        # Stop before authentication or deployment. Keep a second veto at ready.
        transaction.connect('ready-pre-auth', read_plan)
        transaction.connect('ready', lambda tx: False)
        transaction.run(None)
    except GLib.Error as exc:
        if not exc.matches(Flatpak.error_quark(), Flatpak.Error.ABORTED):
            logging.getLogger(__name__).warning('Flatpak download preview (%s): %s', scope, exc)
            return {}
    return bounds
