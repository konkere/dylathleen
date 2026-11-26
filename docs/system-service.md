# Dylath-Leen system service

The DEB installs dylathleend and its activation, policy and timer files. Launch the desktop application as your regular user. Opening or refreshing the installed list reads both bin registries and the native deb-get registrations. User bin commands and release downloads run in the desktop account. The system service commits the selected artifact. AppImage, Flatpak and Snap continue to use their own managers.

The system snapshot lives in `/var/lib/dylathleen/inventory.json`. Readers can open it when the service is offline. The daemon replaces it atomically after inventory and after transactions. Each refresh rereads local state. Network results can be reused for five minutes when the installed versions, system bin entries and selected recipes match. A failed source keeps the last candidate for the same installation and marks the row stale. A changed installation invalidates its old candidate. The interactive native catalogue refresh remains available for fetching new recipe definitions.

## Periodic checks

The `dylathleen-refresh.timer` unit requests an hourly metadata check. The default calendar timer catches up after downtime. To disable it, use `systemctl disable --now dylathleen-refresh.timer` with administrator authorization. To set a new interval through Polkit, run the following from your user session. The reply contains the operation number. A value of zero disables the timer. Positive values are minutes from 5 through 10080.

```sh
busctl call ru.konkere.DylathLeen1 /ru/konkere/DylathLeen1 ru.konkere.DylathLeen1 ConfigureTimer u 120
```

The foreground deb-get transaction can use the desktop user's existing GitHub token. For scheduled checks an administrator can place a dedicated read-only GitHub credential in `/etc/dylathleen/github-token`. The file must belong to root with mode 0600 and root-controlled parent directories. The daemon supplies release metadata to recipes using a literal GitHub repository and the native default or latest release selector. Dynamic selectors and other hosting services use their native anonymous network requests inside the worker. System bin dry runs use a root-controlled, publicly executable bin from `/usr/local/bin`, `/usr/bin` or its registered system location after migration.

## Service interface and trust boundaries

The system bus name and interface are `ru.konkere.DylathLeen1`, with object path `/ru/konkere/DylathLeen1`. Use `busctl introspect` for the installed method signatures.

| Method | Access and behavior |
| --- | --- |
| GetInventory | Public bin fields, installed deb-get versions, update candidates and stale keys |
| Refresh | Local inventory, with an optional unprivileged network check. Concurrent refreshes share a job |
| GetOperation | Operation kind, status and a public diagnostic for a returned operation number |
| Bin | Polkit-protected install, update, pin, unpin, remove or directory migration |
| DebGet | Polkit-protected native package or catalogue operation with a validated package list |
| Recipe | Polkit-protected local recipe editing, history and restore |
| ConfigureTimer | Polkit-protected interval change or disable |

The service validates the bus-assigned unique sender name with Polkit. It serializes system jobs, retains 32 recent operation states in memory, and exits after about a minute without work. A private Unix socket carries the deb-get token, progress, APT plan and responses. Closing the window after confirming a plan leaves the service in charge of the approved transaction. Further prompts require a connected client and expire after three minutes.

### Security review

- **Input to root.** Executable paths and the service environment are fixed. Native mutations require the audited deb-get 0.4.7 script digest. The installed guard is copied into an owner-only transaction directory on the package filesystem. Each guard must successfully execute a probe using the transaction's Python helper. The native command search path contains a single private directory. It includes each guarded program once and excludes the system APT binaries under those names. Missing, inaccessible or non-executable guards stop the transaction before the native command starts.
- **APT scope.** The helper validates registered names, the package identity of downloaded DEBs, action-specific arguments and the simulated removals. The live pre-dpkg hook checks removals again. The user must approve the plan. Bulk upgrade uses `--dg-only`. Explicit native update keeps `DISABLE_APT=y` and `DEBGET_CACHE_RTN=0`. Authorizing a privileged Bash recipe or a package maintainer script entails trusting its code as root.
- **Background execution.** A transient systemd service gets a dynamic UID, a private tmpfs cache and a read-only working copy of the selected recipes and registrations. The worker uses `deb-get show`, local APT candidate lookup and bin dry runs. Its filesystem view includes distribution programs, CA certificates, DNS configuration, local APT index data and dpkg status. Network sockets are restricted to IPv4 and IPv6. Source lookups can contact network endpoints accessible to the machine. The worker has CPU-time-independent wall-clock, task, memory and output limits. The daemon accepts only known installed keys and bounded version strings as returned data. It compares the inventory and recipe fingerprint again before publishing.
- **Credentials.** The background credential is read by the broker. It sends authenticated requests only to the fixed HTTPS GitHub API origin, disables redirects and bounds the response size. The worker receives filtered release metadata. The public snapshot excludes credentials, unknown bin fields and URLs containing credentials or query strings. Transaction diagnostics travel on the private socket with the supplied token redacted.
- **System bin.** The daemon accepts sealed artifact descriptors, verifies the expected registry entry and SHA-256, and preserves the native helper's locking, ownership, path and rollback checks. New destinations can use `/usr/local/bin` or `/opt/<directory>/bin`. Existing system registry directories remain eligible. Administrators may allow additional absolute directory paths, one per line in the root-controlled file `/etc/dylathleen/bin-directories`.
- **Package lifecycle.** Upgrades reload unit metadata and keep the running service and its active child alive. The private helper and guard copies survive unpack. Package removal stops the timer. Purging removes the service snapshot and the interval override created by the service. User launch profiles and manager settings are stored separately.

The integration harnesses are destructive to their test guest. Run `packaging/check-backend.py` only in a disposable Jammy container and `packaging/check-system-service.py` only in a disposable systemd VM with the previous and candidate DEBs. They cover authorization, native package and bin transactions, guard failures, self-update, stale checks and timer configuration.
