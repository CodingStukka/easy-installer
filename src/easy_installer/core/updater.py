"""Updates for installed apps: check where each app publishes new versions, remember what was
found, and replace an app by its new version with one call.

``core.updates`` knows the sources (GitHub releases, electron-builder feeds, zsync files), how to
ask them and how to download; this module connects that with the registry and the installer:

* :func:`has_update_source` / :func:`update_source_of` - can this app be checked at all?
* :func:`check_app_update`, :func:`check_all_updates` - ask the network (at most once per
  interval unless forced) and remember the answer in the update cache;
* :func:`cached_updates` - what the last checks found, without any network access;
* :func:`apply_update` - download, check that the file really is the same app, and install it
  over the installed version with the choices made when that was installed.

Only AppImages are updated. A copy that was kept next to a newer version ("keep both") is pinned
to its version and never offered an update; neither are portable apps.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import (
    EasyInstallerError,
    ExtractionError,
    NotAnAppImageError,
    NotInstalledError,
    UpdateCancelled,
    UpdateError,
)
from ..i18n import _
from . import downloads, installer, reconcile, updates
from .downloads import downloads_dir  # noqa: F401 - part of this module's API (DESIGN §22)
from .elf import host_arch, is_foreign_arch
from .inspector import AppImageInfo, arch_label, inspect_appimage
from .integration import compare_versions
from .origin import clean_url
from .paths import Scope
from .registry import KIND_APPIMAGE, InstalledApp, Registry
from .settings import load_settings
from .updates import AvailableUpdate, Fetcher, UpdateCache, UpdateSource

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float | None, str], None]
#: ``downloader(update, dest_dir, *, progress=None, cancel=None) -> Path`` (``download_update``)
Downloader = Callable[..., Path]
UpdateKey = tuple[str, str]

#: Shares of the progress bar: downloading, checking the file, installing.
_DOWNLOAD_END = 0.7
_CHECK_END = 0.78


class SignerChangedError(UpdateError):
    """The installed version is signed by its maker and the downloaded update is not signed by
    the same maker. Nothing was changed; ``apply_update(..., allow_signer_change=True)`` installs
    it anyway (after asking the user)."""


class UpdateNotNeededError(UpdateError):
    """The installed app is no longer older than the update (it updated itself, or another
    version was installed meanwhile): nothing was downloaded or changed."""


@dataclass
class UpdateCheckResult(Mapping):
    """What :func:`check_all_updates` found. It is also the mapping ``(scope, id) -> update``
    itself (``result[key]``, ``len(result)``, ``result.items()``), so it can be used wherever
    the plain dict of :func:`cached_updates` is."""

    #: ``(scope.value, id)`` -> the update that can be installed
    updates: dict[UpdateKey, AvailableUpdate] = field(default_factory=dict)
    #: ``(scope.value, id)`` -> why that app could not be checked (``str(error)`` is the
    #: user-facing sentence; ``NetworkError`` = worth trying again later)
    errors: dict[UpdateKey, EasyInstallerError] = field(default_factory=dict)
    #: how many apps were looked at (apps without an update source are not counted)
    checked: int = 0

    def __getitem__(self, key: UpdateKey) -> AvailableUpdate:
        return self.updates[key]

    def __iter__(self) -> Iterator[UpdateKey]:
        return iter(self.updates)

    def __len__(self) -> int:
        return len(self.updates)


# ------------------------------------------------------------------------------------------------
# small helpers
# ------------------------------------------------------------------------------------------------


def _noop(fraction: float | None, message: str) -> None:
    pass


def _key(app: InstalledApp) -> UpdateKey:
    return (Scope(app.scope).value, app.id)


def _scaled(report: ProgressCallback, start: float, end: float) -> ProgressCallback:
    def scaled(fraction: float | None, message: str) -> None:
        report(None if fraction is None
               else start + (end - start) * min(max(fraction, 0.0), 1.0), message)

    return scaled


def update_source_of(app: InstalledApp) -> UpdateSource | None:
    """Where ``app`` publishes new versions: the source recorded when it was installed; for
    entries made by 0.1 (which lack it) the AppImage's update information, if it names one."""
    return UpdateSource.from_dict(app.update_source) or updates.parse_update_info(app.update_info)


def has_update_source(app: InstalledApp) -> bool:
    """``app`` can be checked for updates: an AppImage that is not pinned to its version (a
    copy kept next to another version) and that says where its new versions are published."""
    if app.kind != KIND_APPIMAGE or app.pinned or app.base_id:
        return False
    return update_source_of(app) is not None


def _current_filename(app: InstalledApp) -> str | None:
    return app.original_filename or os.path.basename(app.appimage_path) or None


def is_newer_than_installed(update: AvailableUpdate, app: InstalledApp) -> bool:
    """``update`` (found earlier) is still news for ``app`` as it is installed now - the app may
    have updated itself, or the file was installed by hand in the meantime."""
    if update.sha256 and app.sha256 and update.sha256 == app.sha256:
        return False
    if update.version and app.version:
        return compare_versions(update.version, app.version) > 0
    if not update.sha256 and update.size and update.size == app.size:
        return False   # a source without versions and checksums (.zsync): the length decides
    return True


def _registry_of(scope: Scope) -> Registry:
    return Registry(installer.layout_for(scope).registry_path)


# ------------------------------------------------------------------------------------------------
# checking
# ------------------------------------------------------------------------------------------------


def _check(app: InstalledApp, cache: UpdateCache, *, force: bool, fetch: Fetcher | None,
           interval_hours: int) -> AvailableUpdate | None:
    source = update_source_of(app) if has_update_source(app) else None
    if source is None:
        return None
    scope, app_id = _key(app)
    if not force and not cache.is_due(scope, app_id, interval_hours):
        cached = cache.get(scope, app_id)
        return cached if cached is not None and is_newer_than_installed(cached, app) else None
    try:
        update = updates.check_for_update(
            source, current_version=app.version, current_sha256=app.sha256,
            current_filename=_current_filename(app), arch=app.arch or "unknown", fetch=fetch,
            etags=cache.etags, current_size=app.size or None)
    except UpdateError:
        # The source itself is the problem (it moved, or publishes nothing readable): asking
        # again at every start would change nothing, so this counts as a finished check - one
        # that failed: what was found before stays known.
        cache.put_failed(scope, app_id)
        raise
    cache.put(scope, app_id, update)   # also saves the ETags for the next check
    return update


def check_app_update(app: InstalledApp, *, force: bool = False,
                     fetch: Fetcher | None = None) -> AvailableUpdate | None:
    """Look for a newer version of ``app``. None: it is up to date - or cannot be checked at
    all (:func:`has_update_source`).

    The network is only asked if the last check is older than the interval of the settings
    (``update_interval_hours``) or with ``force``; otherwise the remembered answer is returned.
    ``fetch``: replaces the HTTP request (tests). Raises NetworkError (no connection, the
    server is busy or limits requests: try again later) or UpdateError (the update information
    of the app cannot be used).
    """
    return _check(app, UpdateCache(), force=force, fetch=fetch,
                  interval_hours=load_settings().update_interval_hours)


def _one_request_per_address(fetch: Fetcher) -> Fetcher:
    """Within one :func:`check_all_updates`, every address is asked once: the same app installed
    for everyone and just for you (or two apps published in one place) share the answer, which
    saves requests of the source's small hourly allowance. Failed requests are not remembered;
    a request with another validator (``If-None-Match``) is another request."""
    answers: dict[tuple, updates.HttpResponse] = {}
    lock = threading.Lock()

    def fetch_once(url: str, *, headers: Mapping[str, str] | None = None,
                   **kwargs: object) -> updates.HttpResponse:
        wanted = {name.lower(): value for name, value in (headers or {}).items()}
        key = (url, wanted.get("range"), wanted.get("if-none-match"),
               tuple(sorted(kwargs.items(), key=lambda kv: kv[0])))
        with lock:
            known = answers.get(key)
        if known is not None:
            return known
        answer = fetch(url, headers=headers, **kwargs)
        with lock:
            answers[key] = answer
        return answer

    return fetch_once


def check_all_updates(apps: Iterable[InstalledApp] | None = None, *, force: bool = False,
                      progress: ProgressCallback | None = None,
                      fetch: Fetcher | None = None) -> UpdateCheckResult:
    """:func:`check_app_update` for every installed app (or for ``apps``).

    One app that cannot be checked never stops the others: its error is collected in
    ``result.errors``. ``progress(fraction, "Checking <name>…")`` is called before each app.
    """
    report = progress or _noop
    candidates = [app for app in (installer.list_installed() if apps is None else apps)
                  if has_update_source(app)]
    result = UpdateCheckResult()
    if not candidates:
        return result
    cache = UpdateCache()
    interval = load_settings().update_interval_hours
    fetch = _one_request_per_address(fetch or updates.http_get)
    for index, app in enumerate(candidates):
        report(index / len(candidates), _("Checking {name}…").format(name=app.name))
        key = _key(app)
        try:
            update = _check(app, cache, force=force, fetch=fetch, interval_hours=interval)
        except EasyInstallerError as exc:
            log.info("cannot check %s for updates: %s", app.id, exc.details or exc)
            result.errors[key] = exc
        except Exception as exc:  # noqa: BLE001 - one odd app must not stop the others
            log.warning("checking %s for updates failed", app.id, exc_info=True)
            result.errors[key] = UpdateError(
                _("The update information of this app could not be read."),
                details=f"{type(exc).__name__}: {exc}")
        else:
            if update is not None:
                result.updates[key] = update
        result.checked += 1
    report(1.0, _("Done"))
    return result


def cached_updates() -> dict[UpdateKey, AvailableUpdate]:
    """The updates found by earlier checks, for the apps as they are installed now. Never uses
    the network (for ``list`` and for the window before its first check)."""
    found: dict[UpdateKey, AvailableUpdate] = {}
    try:
        apps = [app for app in installer.list_installed() if has_update_source(app)]
        if not apps:
            return found
        cache = UpdateCache()
        for app in apps:
            scope, app_id = _key(app)
            update = cache.get(scope, app_id)
            if update is not None and is_newer_than_installed(update, app):
                found[(scope, app_id)] = update
    except Exception:  # noqa: BLE001 - the cache is a nicety
        log.warning("cannot read the remembered updates", exc_info=True)
    return found


# ------------------------------------------------------------------------------------------------
# applying
# ------------------------------------------------------------------------------------------------


def _download_dir(app: InstalledApp) -> Path:
    try:
        return downloads.app_download_dir(app.id)
    except ValueError as exc:
        raise UpdateError(_("This app cannot be updated automatically."),
                          details=f"unusable app id {app.id!r}") from exc


def _hold_download_dir(folder: Path, app: InstalledApp) -> int:
    """Create the private download folder (what an interrupted update left there goes) and
    hold it while the update runs (see ``core.downloads``)."""
    try:
        return downloads.hold(folder)
    except downloads.FolderInUse as exc:
        raise UpdateError(
            _("{name} is already being updated. Please wait until that is finished.").format(
                name=app.name), details=f"{folder} is in use") from exc
    except OSError as exc:
        raise UpdateError(
            _("The update could not be saved. Please check that there is enough free space."),
            details=f"{folder}: {exc}") from exc


def _current_entry(app: InstalledApp) -> InstalledApp:
    scope = Scope(app.scope)
    current = _registry_of(scope).get(app.id)
    if current is None:
        raise NotInstalledError(_("This app is not installed."),
                                details=f"{app.id} ({scope.value})")
    if not has_update_source(current):
        raise UpdateError(_("This app cannot be updated automatically."),
                          details=f"{app.id}: kind={current.kind!r}, pinned={current.pinned!r}, "
                                  f"update_source={current.update_source!r}")
    return current


def _caught_up(app: InstalledApp) -> InstalledApp:
    """``app`` as it is now: if the app replaced its own file since it was last looked at (a
    self-updater), the entry is first brought in line with that file (``core.reconcile``), so
    that nothing is decided - or kept as the previous version - by what the app was before."""
    if reconcile.needs_reconcile(app):
        result = reconcile.reconcile_app(app)
        log.info("%s changed its own file before its update: %s", app.id, result.change)
        return _current_entry(app)
    return app


def _check_still_needed(update: AvailableUpdate, app: InstalledApp) -> None:
    """An update found earlier is only installed while it is still newer than the app."""
    if is_newer_than_installed(update, app):
        return
    if app.version:
        message = _("{name} is now at version {version}, so this update is no longer "
                    "needed.").format(name=app.name, version=app.version)
    else:
        message = _("{name} is already up to date.").format(name=app.name)
    raise UpdateNotNeededError(
        message, details=f"installed {app.version!r} ({app.sha256}), update {update.version!r} "
                         f"({update.sha256})")


def _inspect_download(path: Path, report: ProgressCallback) -> AppImageInfo:
    try:
        return inspect_appimage(path, progress=report)
    except (NotAnAppImageError, ExtractionError) as exc:
        raise UpdateError(
            _("The downloaded file is not a working app. Nothing was changed."),
            details=f"{path.name}: {exc} ({exc.details})") from exc


def _check_same_app(info: AppImageInfo, app: InstalledApp) -> None:
    if info.app_id != app.main_id:
        raise UpdateError(
            _("The downloaded file is a different app, not {name}. Nothing was changed.").format(
                name=app.name),
            details=f"the download is {info.app_id!r}, expected {app.main_id!r}")
    if is_foreign_arch(info.elf) and info.arch != app.arch:
        raise UpdateError(
            _("The downloaded update is made for a different kind of computer ({arch}) and "
              "cannot run on this one ({host}). Nothing was changed.").format(
                  arch=arch_label(info.elf), host=host_arch()),
            details=f"installed: {app.arch!r}, download: {info.arch!r}")


def _plan(info: AppImageInfo, app: InstalledApp, update: AvailableUpdate,
          allow_signer_change: bool,
          confirm_signer_change: Callable[[str], bool] | None = None) -> installer.InstallPlan:
    if info.origin_url is None:
        info.origin_url = clean_url(update.url)   # a browser would have noted it on the file
    plan = installer.plan_update(info, app, version_hint=update.version)
    if plan.action == installer.ACTION_DOWNGRADE:
        raise UpdateError(
            _("The downloaded file contains version {new}, which is older than the installed "
              "version {installed}. Nothing was changed.").format(new=plan.version,
                                                                 installed=app.version),
            details=f"announced {update.version!r}, the file says {plan.version!r}")
    if plan.signer_changed and not allow_signer_change:
        message = _("The installed version of {name} was signed by its maker, but this update "
                    "is not signed by the same maker. Nothing was changed.").format(name=app.name)
        # Asked here, with the file downloaded: a yes installs it without a second download.
        if confirm_signer_change is None or not confirm_signer_change(message):
            raise SignerChangedError(
                message, details=f"installed: {app.signature!r}, download: {info.signature!r}")
    return plan


def _notes_for_user(info: AppImageInfo, plan: installer.InstallPlan,
                    planned: list[str]) -> list[str]:
    """What the person who started the update should read afterwards: a broken signature of
    the new file, and what came up while installing (the previous version could not be kept,
    the sandbox permission could not be given, ...). The plan's other notes (the sandbox
    choice, another installation) describe the app as it was before and are left out."""
    notes = [note for note in plan.warnings if note not in planned]
    broken = installer.invalid_signature_note(info)
    if broken and broken not in notes:
        notes.insert(0, broken)
    return notes


def apply_update(app: InstalledApp, update: AvailableUpdate, *,
                 progress: ProgressCallback | None = None,
                 cancel: threading.Event | None = None, allow_signer_change: bool = False,
                 downloader: Downloader | None = None,
                 warnings: list[str] | None = None,
                 confirm_signer_change: Callable[[str], bool] | None = None) -> InstalledApp:
    """Download ``update`` and install it over ``app``; returns the new registry entry.

    0. If the app replaced its own file since it was last looked at (a self-updater), the
       entry is brought in line with that file first (``core.reconcile``) - before the
       download and again after it. :class:`UpdateNotNeededError` if ``update`` is then no
       longer newer than the app (nothing is downloaded or changed).
    1. The file is downloaded into a private folder of the cache (``cancel`` stops it:
       UpdateCancelled; size and published checksums are verified by the download).
    2. It is inspected like any AppImage. It must be the same app (its id equals the app's
       main id), fit this computer and not be older than the installed version - else
       UpdateError.
    3. Signature continuity: if the installed version carries a valid signature and the file is
       unsigned, invalid or signed with another key: :class:`SignerChangedError`, unless
       ``allow_signer_change`` - or ``confirm_signer_change(sentence)`` (called with the file
       downloaded) answers True.
    4. It is installed with the choices of the installed app (scope, sandbox fix, start mode);
       the replaced version is kept as the settings say (``backup_days``), so
       ``installer.rollback`` can go back. A system-wide app asks for the password here
       (AuthorizationError if that is refused).
    5. The remembered update is forgotten.

    Whatever happens, the download folder is removed again, and on any error the installed app
    is exactly as it was. ``downloader`` replaces ``updates.download_update`` (tests).
    ``warnings``: a list that gets the translated notes the user should read after a
    successful update (e.g. "The previous version could not be kept…"); the CLI prints them,
    the update dialog shows them on its last page.
    """
    report = progress or _noop
    current = _caught_up(_current_entry(app))
    _check_still_needed(update, current)
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled(_("The download was cancelled."))
    folder = _download_dir(current)
    download = downloader or updates.download_update
    info: AppImageInfo | None = None
    held = _hold_download_dir(folder, current)
    try:
        path = Path(download(update, folder, progress=_scaled(report, 0.0, _DOWNLOAD_END),
                             cancel=cancel))
        if cancel is not None and cancel.is_set():
            raise UpdateCancelled(_("The download was cancelled."))
        if os.path.realpath(path.parent) != os.path.realpath(folder) or not path.is_file():
            raise UpdateError(_("The update could not be downloaded."),
                              details=f"unexpected download result {path}")
        # The app may have updated itself while the download ran (e.g. it was quit).
        current = _caught_up(current)
        _check_still_needed(update, current)
        info = _inspect_download(path, _scaled(report, _DOWNLOAD_END, _CHECK_END))
        _check_same_app(info, current)
        plan = _plan(info, current, update, allow_signer_change, confirm_signer_change)
        if cancel is not None and cancel.is_set():
            raise UpdateCancelled(_("The download was cancelled."))
        planned = list(plan.warnings)
        new = installer.execute_install(plan, progress=_scaled(report, _CHECK_END, 1.0))
        UpdateCache().forget(Scope(current.scope).value, current.id)
        if warnings is not None:
            warnings.extend(_notes_for_user(info, plan, planned))
        return new
    finally:
        if info is not None:
            info.cleanup()
        downloads.release(folder, held)
