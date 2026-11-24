from unittest import TestCase
from unittest.mock import Mock, create_autospec

from dylathleen.api.abstract.controller import SearchResult, SoftwareManager
from dylathleen.view.core.controller import GenericSoftwareManager


class PackageSearchTest(TestCase):
    def setUp(self):
        self.backend = create_autospec(SoftwareManager, instance=True)
        self.backend.get_managed_types.return_value = set()
        self.backend.is_enabled.return_value = True
        self.backend.can_work.return_value = (True, None)
        self.manager = GenericSoftwareManager([self.backend], Mock(),
                                              {'system': {'single_dependency_checking': False}})

    def test_url_returns_empty_without_querying_catalogue(self):
        result = self.manager.search('HTTPS://example.org/app')
        self.assertEqual(SearchResult.empty(), result)
        self.backend.search.assert_not_called()
        self.manager.disk_loader_factory.new.assert_not_called()

    def test_name_reaches_catalogue_and_returns_matches(self):
        package = Mock()
        self.backend.search.return_value = SearchResult([], [package], 1)
        result = self.manager.search('  GIMP  ')
        self.backend.search.assert_called_once_with(words='gimp', limit=-1,
                                                    disk_loader=self.manager.disk_loader_factory.new.return_value)
        self.assertEqual([package], result.new)
        self.assertEqual(1, result.total)
