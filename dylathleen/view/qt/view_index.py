from collections import defaultdict
from typing import Generator, Tuple, Optional, Union

from dylathleen.view.qt.commons import PackageFilters
from dylathleen.view.qt.view_model import PackageView


def new_category_idx():
    return defaultdict(list)


def new_type_index():
    return defaultdict(new_category_idx)


def new_verified_index():
    return defaultdict(new_type_index)


def new_update_index():
    return defaultdict(new_verified_index)


def new_app_index():
    return defaultdict(new_update_index)


def new_package_index():
    return defaultdict(new_app_index)


def add_to_index(pkgv: PackageView, index: dict) -> None:
    # root keys: (1) installed | (0) not installed
    root_idx = index[1 if pkgv.model.installed else 0]

    # app keys: (1) app | (0) not app
    app_lvl = root_idx[1 if pkgv.model.is_application() else 0]

    # update keys: (1) update | (0) no update
    if pkgv.model.installed and pkgv.model.update and not pkgv.model.is_update_ignored():
        update_lvl = app_lvl[1]
    else:
        update_lvl = app_lvl[0]

    # verified keys: (1) verified | (0) unverified
    verified_lvl = update_lvl[1 if pkgv.model.is_trustable() else 0]

    categories = {cat.strip().lower() for cat in (pkgv.model.categories or ()) if cat}
    categories.add('any')
    for category in categories:
        verified_lvl['any'][category].append(pkgv)
        verified_lvl[pkgv.model.get_type()][category].append(pkgv)


def generate_queries(filters: PackageFilters) -> Generator[Tuple[Optional[Union[int, str]], ...], None, None]:
    chars_query = None

    if filters.name:
        chars_query = filters.name.strip().lower()

    installed_queries = (1,) if filters.only_installed else (1, 0)
    apps_queries = (1,) if filters.only_apps else (1, 0)
    updates_queries = (1,) if filters.only_updates else (1, 0)
    verified_queries = (1,) if filters.only_verified else (1, 0)

    for installed in installed_queries:
        for app in apps_queries:
            for update in updates_queries:
                for verified in verified_queries:
                    yield installed, app, update, verified, filters.type, filters.category, chars_query


def query_packages(index: dict, filters: PackageFilters) -> Generator[PackageView, None, None]:
    for installed, app, update, verified, type_, category, name in generate_queries(filters):
        bucket = index[installed][app][update][verified][type_][category]
        for pkg in bucket:
            if not name or name in pkg.name.lower():
                yield pkg
