import os
from dataclasses import dataclass


@dataclass
class InstalledFiles:
    location: str
    paths: list[str]

    @classmethod
    def collect(cls, directory, launcher=None, *related):
        """List installed files and symlinks without descending into linked directories."""
        paths = set()
        if directory:
            for parent, dirs, files in os.walk(directory):
                paths.update(os.path.join(parent, name) for name in files)
                paths.update(os.path.join(parent, name) for name in dirs
                             if os.path.islink(os.path.join(parent, name)))

        paths.update(str(path) for path in related if path and os.path.lexists(path))
        ordered = sorted(paths)
        if launcher and os.path.lexists(launcher):
            launcher = str(launcher)
            return cls(launcher, [launcher, *(p for p in ordered if p != launcher)])
        if directory and os.path.isdir(directory):
            return cls(str(directory), ordered)
        if ordered:
            return cls(ordered[0], ordered)
