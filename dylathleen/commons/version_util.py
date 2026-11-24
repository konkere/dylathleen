import re
from typing import Tuple

RE_VERSION_WITH_EPOCH = re.compile(r'^\d+:(.+)$')
RE_VERSION_WITH_RELEASE = re.compile(r'^(.+)-\d+$')


def map_str_version(version: str) -> Tuple[str, ...]:
    return tuple(part.zfill(8) for part in version.split("."))


def normalize_version(version: str) -> Tuple[int, Tuple[str, ...], int]:
    raw_version = version.strip()

    epoch = 0

    if RE_VERSION_WITH_EPOCH.match(raw_version):
        epoch_version = raw_version.split(":", maxsplit=1)
        epoch = int(epoch_version[0])
        raw_version = epoch_version[1]

    release = 1
    if RE_VERSION_WITH_RELEASE.match(raw_version):
        version_release = raw_version.rsplit("-", maxsplit=1)
        raw_version = version_release[0]
        release = int(version_release[1])

    if not raw_version.split(".")[0].isdigit():
        # this is required to properly compare versions starting with a number against versions starting with alpha
        raw_version = f"0.{raw_version}"

    return epoch, map_str_version(raw_version), release
