# deb-get

Dylath-Leen uses the package-provided `/usr/bin/deb-get`. Privileged operations are available with the audited version **0.4.7**. The application can read the local catalogue and existing registrations with another version, but privileged commands will report that the version needs verification. Install deb-get separately and open Dylath-Leen as a regular user. If you use a saved list of enabled managers, enable **deb-get** under Settings → Types.

## Catalogue and updates

- Open **Actions → Browse the deb-get catalogue** for the local catalogue, or search for `deb-get` from the main window to use the standard table, filters and sorting. **Refresh deb-get catalogue** populates the catalogue on a new installation. The catalogue reads the manifest and static recipe fields locally. Fields calculated by Bash can be resolved on demand from the row menu. The installed list joins deb-get's `/etc/deb-get/installed` with dpkg status for those registered names.
- Use **Check deb-get updates** for direct downloads. The command suppresses the system-wide APT refresh and uses deb-get's short cache policy. The application keeps the results for the session. Adjust the minimum delay between checks in settings. An empty or invalid response that deb-get reports is shown as a check failure or partial result.
- APT and PPA rows display their candidate from **local APT indexes**, which can be outdated. A row's **Upgrade this APT/PPA application** action can refresh its own repository before a targeted upgrade. **Upgrade all deb-get applications** uses `deb-get upgrade --dg-only`, with existing APT indexes and the native release cache. Check updates before using this action.
- Review the resolver plan before confirming each APT transaction. Dylath-Leen strips automatic removal and blocks removals outside the selected package. For both remove and purge, deb-get updates its registry after the authorized transaction. Use **Purge** while the package is still registered if you also want to remove its package configuration. Interactive licence questions from deb-get use a GUI confirmation. APT/debconf run noninteractively and apply the existing configuration when dpkg would otherwise ask how to handle a modified file.
- **View last deb-get log** opens the last operation saved to the Dylath-Leen cache. Commands already running in APT finish before cancellation can take effect.

The privileged helper uses the package-provided executable directly. Your PATH wrapper is never invoked or edited. The native update has `DISABLE_APT=y` and `DEBGET_CACHE_RTN=0`, and the native bulk upgrade gets one `--dg-only` flag. An APT guard exists only in the helper's child processes. System-wide APT updates, upgrades and automatic removal are unavailable. APT can still resolve dependencies of selected applications. A second check in APT's pre-dpkg hook rejects unexpected removals if the plan changes.

The optional GitHub token comes from `DEBGET_TOKEN` or the original user's `~/.config/deb-get/github-token`. The file must be a regular file owned by that user and private to them. The helper supplies the token to the GitHub API via a temporary private wget configuration.

## Recipes

Manage recipes from **Actions → Manage local deb-get recipes**. The local catalogue lives in `/etc/deb-get/99-local.d/`. The editor always shows a red Bash execution warning. Reading, importing, editing, syntax checking and saving recipes are text operations. ShellCheck is offered when installed. **Resolve recipe version and download** is a separate action that calls deb-get after confirmation because resolving can execute the recipe. Privileged native catalogue refreshes, checks and upgrades can execute recipes from all configured catalogues, including local ones. The transaction guard is not a sandbox for arbitrary Bash or for package maintainer scripts.

Saving a revision uses atomic replacement and detects changes made since the editor opened. A previous version is preserved under `/etc/deb-get/dylathleen-backups/`. Restore by selecting a revision from the recipe manager. Export adds a new file to the chosen directory. Deleting a recipe leaves the installed application on disk. When the last recipe is deleted, a subsequent native update may drop the registration. Removing an override can change the effective recipe at the next check. If an official recipe appears, compare the two in the recipe manager before using **Switch to the official recipe**.

The generator reads recent stable GitHub and GitLab releases and presents DEB candidates by platform. Choose one per supported platform. It inspects downloaded DEBs with `dpkg-deb` and validates SHA-256 when provided by the API or a checksum file. Review the distribution, codename, architecture, filename and version transformation. The generated Bash uses native release helpers and requires an unambiguous match in the selected release. The generated recipe is editable and is saved only after confirmation. Install the application separately.

GitLab generation supports release links with HTTPS DEB URLs. The native GitLab helper resolves the project's latest release. If that release has only a pre-release candidate or its filenames no longer fit the recipe, resolution fails until a matching release appears or you adjust the recipe. Advanced recipes with dynamic metadata may display fewer fields in the static catalogue.

## Manual verification

Use a disposable machine or a test account with deb-get 0.4.7.

- Start Dylath-Leen after upgrading from 0.4.0 and verify the saved theme, locale, filters and manager settings. Open the application once without deb-get to verify graceful absence.
- Compare the GUI's installed entries with `deb-get list --include-unsupported --installed`. Browse the catalogue and filter by source. Check a direct DEB and an APT/PPA entry for distinct version labels.
- Review and cancel one APT plan. Install and reinstall a test app, then try removal and purge on two disposable packages. Confirm that unrelated automatically installed packages remain.
- Check updates, then try a selected update and a bulk update. Verify that an unavailable API or an APT lock is reported and review the operation log.
- Import a recipe, verify the red warning, run Bash syntax checking and save it. Edit and restore a saved revision. Compare a local override with an official recipe and switch only after inspecting the diff.
- Generate Dylath-Leen's universal recipe and TurboJedi's platform-specific recipe. Check a GitLab project with DEB release links. Save a recipe and resolve it separately before installing.
