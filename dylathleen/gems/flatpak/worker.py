from io import StringIO
from threading import Thread

from requests.exceptions import ConnectionError

from dylathleen.api.abstract.cache import MemoryCache
from dylathleen.api.abstract.context import ApplicationContext
from dylathleen.api.abstract.controller import SoftwareManager
from dylathleen.api.abstract.model import PackageStatus
from dylathleen.gems.flatpak.constants import FLATHUB_API_URL, FLATHUB_URL
from dylathleen.gems.flatpak.model import FlatpakApplication


class FlatpakAsyncDataLoader(Thread):

    def __init__(self, app: FlatpakApplication, manager: SoftwareManager, context: ApplicationContext, api_cache: MemoryCache, category_cache: MemoryCache):
        super(FlatpakAsyncDataLoader, self).__init__()
        self.app = app
        self.manager = manager
        self.http_client = context.http_client
        self.api_cache = api_cache
        self.persist = False
        self.logger = context.logger
        self.category_cache = category_cache

    @staticmethod
    def format_category(category: str) -> str:
        word = StringIO()
        last_l = None
        for idx, l in enumerate(category):
            if idx != 0 and last_l != ' ' and l.isupper() and idx + 1 < len(category) and category[idx + 1].islower():
                word.write(' ')

            last_l = l.lower()
            word.write(last_l)

        word.seek(0)
        return word.read()

    def run(self):
        if self.app:
            self.app.status = PackageStatus.LOADING_DATA

            try:
                res = self.http_client.get(f'{FLATHUB_API_URL}/{self.app.id}', single_call=True)

                if res and res.text:
                    data = res.json()

                    if not data:
                        self.logger.warning("No data returned for id {} ({})".format(self.app.id, self.app.name))
                    else:
                        if not self.app.name:
                            self.app.name = data.get('name')

                        if not self.app.description:
                            self.app.description = data.get('description', data.get('summary', None))

                        self.app.icon_url = data.get('icon')
                        if not self.app.installed:
                            self.app.latest_version = self.app.version
                            self.app.version = self.app.latest_version

                        if self.app.icon_url and self.app.icon_url.startswith('/'):
                            self.app.icon_url = FLATHUB_URL + self.app.icon_url

                        if data.get('categories'):
                            cats = []
                            for c in data['categories']:
                                cached = self.category_cache.get(c)

                                if not cached:
                                    cached = self.format_category(c)
                                    self.category_cache.add_non_existing(c, cached)

                                cats.append(cached)

                            self.app.categories = cats

                        loaded_data = self.app.get_data_to_cache()

                        self.api_cache.add(self.app.id, loaded_data)
                        self.persist = self.app.supports_disk_cache()
                elif res is not None:
                    self.logger.warning("No Flathub metadata for %s (HTTP %s)", self.app.id, res.status_code)
            except ConnectionError:
                # HttpClient already logged the failed connection.
                pass
            except Exception:
                self.logger.exception("Could not retrieve app data for id '%s'", self.app.id)

            self.app.status = PackageStatus.READY

            if self.persist:
                self.manager.cache_to_disk(pkg=self.app, icon_bytes=None, only_icon=False)
