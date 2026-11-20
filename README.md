# Dylath-Leen

A harbor for Linux applications.

[Русская версия](README.ru.md) · [Changes](CHANGELOG.md)

Dylath-Leen is an independently modified fork of [bauh](https://github.com/vinifmor/bauh) by Vinícius Moreira. Its native Python / Qt 5 desktop manages Flatpak, Snap, AppImage and Web Apps. APT, dpkg, pacman and AUR are outside its catalogue.

## Install and run

From a source checkout run `./packaging/build-deb` (requires dpkg-deb, tar and standard GNU tools). Install the resulting file with `sudo apt install ./dist/dylathleen_0.1.2-1_all.deb`. Open **Dylath-Leen** from the desktop menu or run `dylathleen` as your regular user. For a downloaded DEB, run apt in its directory with `./` before its filename. No pip, virtual environment or AppImage launcher is needed.

Refresh to list installed software, search by name, select updates and review the confirmation before applying them. Use `dylathleen --offline` for local inventory, `dylathleen --settings` for format settings and `dylathleen-tray` for notifications. System Flatpak transactions use the system's authorization policy. Snap operations require snapd and may prompt for sudo. Configuration, installed AppImage/Web data and caches use separate dylathleen directories. Existing bauh-managed AppImages and Web Apps are not automatically imported.

## Flatpak updates

The inventory joins installed deployments to the remote update list by user/system scope, origin and typed ref (including architecture and branch). Flatpak supplies the installed version, active commit, target commit and the remote catalogue's release label. A changed commit with an unchanged reported version is a build update. An absent label, a failed catalogue refresh or a numerically older catalogue version yields an unknown target. A catalogue label is publisher metadata and can lag the actual commit. The displayed preview describes the last refresh. Flatpak resolves the transaction again when applying it.

## Formats and platform targets

Flatpak 1.12 or newer is required. Configure a Flatpak remote for the desired installation. Snap needs a running snapd. The inherited AppImage backend targets x86-64 and uses upstream catalogues. Web Apps use the inherited Nativefier toolchain, which downloads Node/Electron to wrap a website. The manager itself uses PyQt5. Network services, catalogue availability and desktop authorization affect individual transactions.

CI targets Ubuntu 22.04, 24.04, 26.04 and Debian 12, 13. Run `/usr/bin/python3 -m unittest discover` with the system dependencies installed. The build accepts SOURCE_DATE_EPOCH and uses a fixed default for repeatable archives. CI produces downloadable build artifacts. See [implementation notes](docs/iteration-1.md) for limitations and outstanding work.

## License

The original Zlib notice by Vinícius Moreira is preserved. Dylath-Leen changes by Konkere use the same [license](LICENSE).
