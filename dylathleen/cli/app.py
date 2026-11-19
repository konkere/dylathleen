import os

import urllib3

from dylathleen import ROOT_DIR, __version__
from dylathleen.api import user
from dylathleen.api.abstract.context import ApplicationContext
from dylathleen.api.http import HttpClient
from dylathleen.cli import __app_name__, cli_args
from dylathleen.cli.controller import CLIManager
from dylathleen.commons.internet import InternetChecker
from dylathleen.context import generate_i18n, DEFAULT_I18N_KEY
from dylathleen.view.core import gems
from dylathleen.view.core.config import CoreConfigManager
from dylathleen.view.core.controller import GenericSoftwareManager
from dylathleen.view.core.downloader import AdaptableFileDownloader
from dylathleen.view.util import logs, util, resource
from dylathleen.view.util.cache import DefaultMemoryCacheFactory
from dylathleen.view.util.disk import DefaultDiskCacheLoaderFactory


def main():
    if not os.getenv('PYTHONUNBUFFERED'):
        os.environ['PYTHONUNBUFFERED'] = '1'

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    args = cli_args.read()
    logger = logs.new_logger(__app_name__, False)

    app_config = CoreConfigManager().get_config()
    http_client = HttpClient(logger)

    i18n = generate_i18n(app_config, resource.get_path('locale'))

    cache_factory = DefaultMemoryCacheFactory(expiration_time=0)

    downloader = AdaptableFileDownloader(logger=logger, multithread_enabled=app_config['download']['multithreaded'],
                                         multithread_client=app_config['download']['multithreaded_client'],
                                         i18n=i18n, http_client=http_client,
                                         check_ssl=app_config['download']['check_ssl'])

    context = ApplicationContext(i18n=i18n,
                                 http_client=http_client,
                                 download_icons=bool(app_config['download']['icons']),
                                 app_root_dir=ROOT_DIR,
                                 cache_factory=cache_factory,
                                 disk_loader_factory=DefaultDiskCacheLoaderFactory(logger),
                                 logger=logger,
                                 distro=util.get_distro(),
                                 file_downloader=downloader,
                                 app_name=__app_name__,
                                 app_version=__version__,
                                 internet_checker=InternetChecker(offline=False),
                                 suggestions_mapping=None,  # TODO not needed at the moment
                                 root_user=user.is_root())

    managers = gems.load_managers(context=context, locale=i18n.current_key, config=app_config,
                                  default_locale=DEFAULT_I18N_KEY, logger=logger)

    cli = CLIManager(GenericSoftwareManager(managers, context=context, config=app_config))

    if args.command == 'updates':
        cli.list_updates(args.format)


if __name__ == '__main__':
    main()
