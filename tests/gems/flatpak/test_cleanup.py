from unittest import TestCase
from unittest.mock import Mock

from dylathleen.gems.flatpak.cleanup import remove_selected

A = 'runtime/org.example.A/x86_64/stable'
B = 'runtime/org.example.B/x86_64/stable'


class CleanupTransactionTest(TestCase):
    def run_plan(self, planned, unused):
        api = Mock()
        inst = Mock()
        inst.list_unused_refs.return_value = [Mock(format_ref=lambda: unused)]
        tx = api.Transaction.new_for_installation.return_value
        tx.get_operations.return_value = [
            Mock(get_ref=Mock(return_value=ref),
                 get_operation_type=Mock(return_value=api.TransactionOperationType.UNINSTALL))
            for ref in planned]

        def run(_):
            ready = tx.connect.call_args.args[1]
            if not ready(tx):
                raise RuntimeError('aborted')

        tx.run.side_effect = run
        differences = remove_selected(api, inst, {A})
        tx.set_disable_related.assert_called_once_with(True)
        tx.set_include_unused_uninstall_ops.assert_called_once_with(False)
        tx.add_uninstall.assert_called_once_with(A)
        return differences

    def test_exact_selection(self):
        self.assertEqual(set(), self.run_plan([A], A))

    def test_unselected_related_extension_aborts(self):
        self.assertEqual({B}, self.run_plan([A, B], A))

    def test_runtime_now_in_use_aborts(self):
        self.assertEqual({A}, self.run_plan([A], B))
