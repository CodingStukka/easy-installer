"""Signatures embedded in AppImages.

A type-2 AppImage may carry an OpenPGP signature in two ELF sections of its runtime:
``.sha256_sig`` (the signature) and ``.sig_key`` (the signer's public key). Unsigned is the norm:
both sections are then empty and :func:`read_signature` returns None.

**The algorithm** (identical in appimagetool of AppImageKit 13 [``sha256sum`` + ``gpg
--detach-sign --armor``], the gpgme based appimagetool [``appimagetool_sign.c``, text mode],
go-appimage [``CalculateSHA256Digest``/``SignAppImage``] and the validators ``validate.c`` and
AppImageUpdate's ``SignatureValidator``):

1. digest = SHA-256 of the whole file in which the bytes of the sections ``.sha256_sig`` and
   ``.sig_key`` are read as zeros (they are still empty when the file is signed; ``.upd_info``
   and ``.digest_md5`` were filled in before and are hashed as they are);
2. the signed data is that digest as 64 lower-case hexadecimal characters - the text, not the
   32 raw bytes, and without a line end;
3. ``.sha256_sig`` holds the ASCII-armoured *detached* signature of that text, ``.sig_key`` the
   ASCII-armoured public key; both are padded with NUL bytes.

This implementation was checked against a real signed AppImage (GitButler 0.22.3, signed by
``E8212F503765BBD5EF5D5B292BF4B56A124A6AE6``): it verifies as "valid", and changing a single
byte of the payload turns it into "invalid". tests/test_signature.py signs fake AppImages the
way both appimagetool generations do.

**What "valid" means - and what it does not.** The key travels inside the file, so a valid
signature only says: *this file is exactly what the holder of that key signed*. Anyone can make
a key with any name. The value lies in the fingerprint staying the same from one version of an
app to the next (DESIGN §25 "signature continuity"), never in the signer's name alone.

Not a signature: go-appimage writes the plain SHA-256 digest (64 hex characters) into
``.sha256_sig`` of AppImages it did *not* sign, and may embed a public key without a signature.
Both are reported as "not signed" (None).

gpg always runs with a private, temporary ``GNUPGHOME`` (0700) and ``--batch --no-tty``, with
time limits, without starting an agent or contacting key servers; the user's key ring is never
read or changed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..i18n import _
from .elf import ElfInfo, read_section

log = logging.getLogger(__name__)

STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_UNVERIFIED = "unverified"
STATUSES = (STATUS_VALID, STATUS_INVALID, STATUS_UNVERIFIED)

SIGNATURE_SECTION = ".sha256_sig"
KEY_SECTION = ".sig_key"
MAX_SIGNATURE_SIZE = 64 * 1024      # appimagetool reserves 1024 bytes
MAX_KEY_SIZE = 1024 * 1024          # appimagetool reserves 8192 bytes
GPG_TIMEOUT = 20.0                  # seconds per gpg call

MAX_SIGNER_LENGTH = 200
MAX_DETAILS_LENGTH = 300

_SIGNATURE_BEGIN = b"-----BEGIN PGP SIGNATURE-----"
_SIGNATURE_RE = re.compile(rb"-----BEGIN PGP SIGNATURE-----.*?-----END PGP SIGNATURE-----", re.S)
_KEY_RE = re.compile(
    rb"-----BEGIN PGP PUBLIC KEY BLOCK-----.*?-----END PGP PUBLIC KEY BLOCK-----", re.S)
_FINGERPRINT_RE = re.compile(r"[0-9A-F]{40}(?:[0-9A-F]{24})?\Z")
_STORED_FINGERPRINT_RE = re.compile(r"[0-9A-F]{16,64}\Z")
_STATUS_PREFIX = b"[GNUPG:] "
_CHUNK = 1 << 20


def _clean_text(value: object, limit: int) -> str | None:
    """``value`` as one line of plain text (no control or invisible characters), or None."""
    if not isinstance(value, str):
        return None
    text = "".join(" " if ch.isspace() else ch for ch in value
                   if ch.isspace() or not unicodedata.category(ch).startswith("C"))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or None


def _clean_fingerprint(value: object, pattern: re.Pattern[str] = _FINGERPRINT_RE) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text if pattern.match(text) else None


@dataclass(frozen=True)
class SignatureInfo:
    """What is known about an AppImage's embedded signature.

    ``status``: "valid" (gpg confirmed: the file is exactly what the key's holder signed),
    "invalid" (gpg confirmed the opposite: the file differs from what was signed, or the signer
    withdrew the key) or "unverified" (there is a signature, but nothing could be established:
    no gpg, no key, unreadable signature, expired key ...). ``fingerprint`` identifies the key
    (upper-case hex), ``signer`` is the name on the key, ``details`` a translated sentence that
    explains a status other than "valid" (in the language of whoever looked at the file) and
    ``reason`` the same as a stable code (one of ``REASONS``): :attr:`explanation` says it in
    the language of whoever reads it now.
    """

    status: str
    fingerprint: str | None = None
    signer: str | None = None
    details: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "fingerprint": self.fingerprint, "signer": self.signer,
                "details": self.details, "reason": self.reason}

    @property
    def explanation(self) -> str | None:
        """Why the status is not "valid", translated now (records made by 0.2.0 only have the
        sentence of the time)."""
        text = REASONS.get(self.reason or "")
        return text() if text is not None else self.details

    @classmethod
    def from_dict(cls, d: object) -> SignatureInfo | None:
        """Tolerant reader for registry entries; None for anything that is not a signature."""
        if not isinstance(d, dict) or d.get("status") not in STATUSES:
            return None
        return cls(
            status=d["status"],
            fingerprint=_clean_fingerprint(d.get("fingerprint"), _STORED_FINGERPRINT_RE),
            signer=_clean_text(d.get("signer"), MAX_SIGNER_LENGTH),
            details=_clean_text(d.get("details"), MAX_DETAILS_LENGTH),
            reason=d.get("reason") if d.get("reason") in REASONS else None,
        )


# -- reading the sections ---------------------------------------------------------------------------


def _section(path: Path, elf: ElfInfo, name: str, limit: int) -> bytes:
    return read_section(path, elf, name, max_size=limit) or b""


def _embedded_signature(path: Path, elf: ElfInfo) -> bytes | None:
    """The armoured signature, ``b""`` if one begins but is cut off, None if there is none."""
    data = _section(path, elf, SIGNATURE_SECTION, MAX_SIGNATURE_SIZE)
    if _SIGNATURE_BEGIN not in data:
        return None                       # empty, a plain digest (go-appimage) or something else
    match = _SIGNATURE_RE.search(data)
    return match.group(0) + b"\n" if match else b""


def _embedded_key(path: Path, elf: ElfInfo) -> bytes | None:
    match = _KEY_RE.search(_section(path, elf, KEY_SECTION, MAX_KEY_SIZE))
    return match.group(0) + b"\n" if match else None


def _zero_ranges(elf: ElfInfo, file_size: int) -> list[tuple[int, int]]:
    """``[start, end)`` of the two sections inside the file, sorted and without overlaps."""
    ranges: list[tuple[int, int]] = []
    for name in (SIGNATURE_SECTION, KEY_SECTION):
        entry = elf.sections.get(name)
        if entry is None:
            continue
        start = max(0, min(int(entry[0]), file_size))
        end = max(start, min(start + max(0, int(entry[1])), file_size))
        if end > start:
            ranges.append((start, end))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def reference_digest(path: Path, elf: ElfInfo) -> str:
    """The text an AppImage's signature is made over (see the module documentation)."""
    digest = hashlib.sha256()
    zeros = bytes(_CHUNK)
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        position = 0
        for start, end in _zero_ranges(elf, size):
            while position < start:
                chunk = handle.read(min(_CHUNK, start - position))
                if not chunk:
                    raise OSError(f"{path} ended at {position}, expected {size} bytes")
                digest.update(chunk)
                position += len(chunk)
            remaining = end - start
            while remaining > 0:
                step = min(_CHUNK, remaining)
                digest.update(zeros[:step])
                remaining -= step
            handle.seek(end)
            position = end
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# -- gpg --------------------------------------------------------------------------------------------


class _GpgUnavailable(Exception):
    """gpg could not be started or did not finish in time."""


def _find_gpg() -> str | None:
    return shutil.which("gpg") or shutil.which("gpg2")


def _run_gpg(gpg: str, home: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    command = [
        gpg, "--homedir", str(home), "--batch", "--no-tty", "--no-options", "--no-autostart",
        "--no-auto-key-retrieve", "--trust-model", "always", *args,
    ]
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GNUPGHOME": str(home), "HOME": str(home),
        "LC_ALL": "C", "LANG": "C", "LANGUAGE": "C",
    }
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, env=env,
                              timeout=GPG_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise _GpgUnavailable(f"gpg took longer than {GPG_TIMEOUT:g} s") from exc
    except (OSError, ValueError) as exc:
        raise _GpgUnavailable(f"gpg could not be started: {exc}") from exc


@dataclass
class _Key:
    fingerprint: str
    revoked: bool = False
    expired: bool = False
    user_ids: list[str] = field(default_factory=list)
    subkeys: list[str] = field(default_factory=list)

    def owns(self, identifier: str | None) -> bool:
        """``identifier``: a fingerprint or a (long) key id of this key or one of its subkeys."""
        if not identifier:
            return False
        wanted = identifier.upper()
        return any(fpr == wanted or (len(wanted) >= 16 and fpr.endswith(wanted))
                   for fpr in (self.fingerprint, *self.subkeys))


def _unescape_colons(raw: bytes) -> str:
    r"""A field of ``--with-colons`` output: ``\x3a`` style escapes, UTF-8."""
    data = re.sub(rb"\\x([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
    return data.decode("utf-8", "replace")


def _parse_keys(listing: bytes) -> list[_Key]:
    """Keys in the output of ``gpg --list-keys --with-colons --with-fingerprint``."""
    keys: list[_Key] = []
    withdrawn: dict[int, list[str]] = {}
    expect: str | None = None             # which fingerprint the next "fpr" record is
    for line in listing.splitlines():
        fields = line.split(b":")
        record = fields[0]
        if record == b"pub":
            validity = fields[1] if len(fields) > 1 else b""
            keys.append(_Key("", revoked=validity == b"r", expired=validity == b"e"))
            expect = "primary"
        elif record == b"sub":
            expect = "subkey"
        elif record == b"fpr" and keys and len(fields) > 9:
            fingerprint = _clean_fingerprint(fields[9].decode("ascii", "replace"))
            if fingerprint and expect == "primary":
                keys[-1].fingerprint = fingerprint
            elif fingerprint and expect == "subkey":
                keys[-1].subkeys.append(fingerprint)
            expect = None
        elif record == b"uid" and keys and len(fields) > 9:
            name = _clean_text(_unescape_colons(fields[9]), MAX_SIGNER_LENGTH)
            if not name or fields[1] == b"n":
                continue
            if fields[1] == b"r" and not keys[-1].revoked:
                withdrawn.setdefault(len(keys) - 1, []).append(name)   # a name given up
            else:
                keys[-1].user_ids.append(name)
    for index, names in withdrawn.items():
        keys[index].user_ids += names     # only used if the key has no other name
    return [key for key in keys if key.fingerprint]


def _parse_status(output: bytes) -> list[tuple[str, list[str]]]:
    """``[GNUPG:] KEYWORD args...`` lines of ``--status-fd`` output."""
    result: list[tuple[str, list[str]]] = []
    for line in output.splitlines():
        if not line.startswith(_STATUS_PREFIX):
            continue
        words = line[len(_STATUS_PREFIX):].decode("utf-8", "replace").split()
        if words:
            result.append((words[0], words[1:]))
    return result


@dataclass(frozen=True)
class _Verdict:
    outcome: str                          # good | bad | expired | revoked | no-key | unreadable | error
    identifier: str | None = None         # fingerprint (preferred) or long key id gpg named


def _interpret(status: list[tuple[str, list[str]]], returncode: int) -> _Verdict:
    """What gpg said about the signature. Anything unclear is "error", never "good"."""
    def first(keyword: str) -> list[str] | None:
        return next((args for word, args in status if word == keyword), None)

    def count(keyword: str) -> int:
        return sum(1 for word, _args in status if word == keyword)

    def named(args: list[str] | None) -> str | None:
        return args[0].upper() if args else None

    if first("BADSIG") is not None:
        return _Verdict("bad", named(first("BADSIG")))
    if first("REVKEYSIG") is not None:
        return _Verdict("revoked", named(first("REVKEYSIG")))
    for keyword in ("EXPKEYSIG", "EXPSIG"):
        if first(keyword) is not None:
            return _Verdict("expired", named(first(keyword)))
    errsig = first("ERRSIG")
    if errsig is not None or first("NO_PUBKEY") is not None:
        identifier = named(errsig) or named(first("NO_PUBKEY"))
        if errsig is not None and len(errsig) > 6 and _clean_fingerprint(errsig[6]):
            identifier = errsig[6].upper()
        missing = first("NO_PUBKEY") is not None or (errsig is not None and len(errsig) > 5
                                                     and errsig[5] == "9")
        return _Verdict("no-key" if missing else "error", identifier)
    valid = first("VALIDSIG")
    if (returncode == 0 and valid and count("GOODSIG") >= 1
            and count("GOODSIG") == count("VALIDSIG") == max(count("NEWSIG"), 1)):
        # The last field is the primary key's fingerprint: it stays the same when the signer
        # switches to a new signing subkey.
        primary = valid[9] if len(valid) > 9 else valid[0]
        fingerprint = _clean_fingerprint(primary) or _clean_fingerprint(valid[0])
        if fingerprint:
            return _Verdict("good", fingerprint)
    if first("GOODSIG") is None and first("NODATA") is not None:
        return _Verdict("unreadable")     # damaged armour, or a format this gpg does not know
    return _Verdict("error")


def _verify(gpg: str, home: Path, signature: Path, data: Path) -> _Verdict:
    proc = _run_gpg(gpg, home, ["--status-fd", "1", "--verify", str(signature), str(data)])
    log.debug("gpg --verify exit %s: %s", proc.returncode,
              proc.stderr.decode("utf-8", "replace").strip())
    return _interpret(_parse_status(proc.stdout), proc.returncode)


def _load_key(gpg: str, home: Path, key_file: Path) -> list[_Key]:
    """Put the embedded key into the private key ring and describe it; [] if it is unusable.

    The keys are listed from the key ring after the import rather than with ``--show-keys``:
    that lists exactly what ``--verify`` will use, and gpg 2.4 stops with "can't open
    trustdb.gpg" when it is asked to show an expired key in a fresh home.
    """
    imported = _run_gpg(gpg, home, ["--status-fd", "1", "--import", str(key_file)])
    if not any(word == "IMPORT_OK" for word, _args in _parse_status(imported.stdout)):
        log.debug("embedded key was not imported: %s", imported.stderr.decode("utf-8", "replace"))
        return []
    listed = _run_gpg(gpg, home, ["--with-colons", "--with-fingerprint", "--list-keys"])
    keys = _parse_keys(listed.stdout) if listed.returncode == 0 else []
    if not keys:
        log.debug("embedded key is unreadable: %s", listed.stderr.decode("utf-8", "replace"))
    return keys


# -- putting it together ----------------------------------------------------------------------------


def _text_no_gpg() -> str:
    return _("The signature could not be checked because GnuPG is not installed.")


def _text_not_checked() -> str:
    return _("The signature could not be checked.")


def _text_no_key() -> str:
    return _("The file does not include the signer's key, so the signature could not be checked.")


def _text_other_key() -> str:
    return _("The key inside the file does not belong to its signature.")


def _text_expired() -> str:
    return _("The signature matches, but the signer's key has expired.")


def _text_not_covered() -> str:
    return _("The signature does not cover the contents of the app.")


def _text_changed() -> str:
    return _("The file was changed after it was signed, or it is damaged.")


def _text_revoked() -> str:
    return _("The signer has withdrawn the key this file was signed with.")


def _text_unreadable() -> str:
    return _("The signature inside the file could not be read.")


#: Why a signature is not "valid": stable codes (stored) and their sentences (shown).
REASONS: dict[str, Callable[[], str]] = {
    "no-gpg": _text_no_gpg, "not-checked": _text_not_checked, "no-key": _text_no_key,
    "other-key": _text_other_key, "expired": _text_expired, "not-covered": _text_not_covered,
    "changed": _text_changed, "revoked": _text_revoked, "unreadable": _text_unreadable,
}


def _info(status: str, reason: str | None, key: _Key | None = None,
          fingerprint: str | None = None) -> SignatureInfo:
    signer = key.user_ids[0] if key and key.user_ids else None
    return SignatureInfo(
        status=status,
        fingerprint=(key.fingerprint if key else None) or _clean_fingerprint(fingerprint),
        signer=_clean_text(signer, MAX_SIGNER_LENGTH),
        details=REASONS[reason]() if reason else None,
        reason=reason,
    )


def _check(path: Path, elf: ElfInfo, signature: bytes, key_block: bytes | None) -> SignatureInfo:
    if not signature:                     # it begins, but its end is missing
        return _info(STATUS_UNVERIFIED, "unreadable")
    gpg = _find_gpg()
    if gpg is None:
        return _info(STATUS_UNVERIFIED, "no-gpg")
    digest = reference_digest(path, elf)

    with tempfile.TemporaryDirectory(prefix="easy-installer-gpg-") as tmp:
        work = Path(tmp)
        home = work / "gnupg"
        home.mkdir(mode=0o700)
        signature_file = work / "signature.asc"
        signature_file.write_bytes(signature)
        digest_file = work / "digest.txt"
        digest_file.write_bytes(digest.encode("ascii"))       # no line end, like sha256sum's field

        keys: list[_Key] = []
        if key_block is not None:
            key_file = work / "key.asc"
            key_file.write_bytes(key_block)
            keys = _load_key(gpg, home, key_file)

        verdict = _verify(gpg, home, signature_file, digest_file)
        key = next((k for k in keys if k.owns(verdict.identifier)), None)

        if verdict.outcome == "good":
            if key is None:               # cannot happen: only the embedded key is in the ring
                return _info(STATUS_UNVERIFIED, "not-checked")
            return _info(STATUS_VALID, None, key)
        if verdict.outcome == "bad":
            # go-appimage signs an empty text when the AppImage has update information: such a
            # signature is genuine but says nothing about the file - neither valid nor forged.
            empty = work / "empty.txt"
            empty.write_bytes(b"")
            if _verify(gpg, home, signature_file, empty).outcome == "good":
                return _info(STATUS_UNVERIFIED, "not-covered", key)
            return _info(STATUS_INVALID, "changed", key)
        if verdict.outcome == "revoked":
            return _info(STATUS_INVALID, "revoked", key)
        if verdict.outcome == "expired":
            return _info(STATUS_UNVERIFIED, "expired", key)
        if verdict.outcome == "no-key":
            return _info(STATUS_UNVERIFIED, "other-key" if keys else "no-key",
                         fingerprint=verdict.identifier)
        if verdict.outcome == "unreadable":
            return _info(STATUS_UNVERIFIED, "unreadable")
        return _info(STATUS_UNVERIFIED, "not-checked")


def read_signature(path: Path, elf: ElfInfo) -> SignatureInfo | None:
    """The embedded signature of the AppImage ``path``; None if it is not signed.

    Never raises and never runs the AppImage. Only a file with an OpenPGP signature block in
    ``.sha256_sig`` counts as signed; then the whole file is hashed and gpg is asked (see the
    module documentation). "valid" is only ever returned when gpg confirmed a good signature of
    the reference digest by the embedded key.
    """
    try:
        path = Path(path)
        signature = _embedded_signature(path, elf)
    except Exception as exc:  # noqa: BLE001 - trust information must never break an install
        log.debug("cannot read the signature sections of %s: %s", path, exc)
        return None
    if signature is None:
        return None
    try:
        return _check(path, elf, signature, _embedded_key(path, elf))
    except _GpgUnavailable as exc:
        log.warning("signature of %s not checked: %s", path, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("signature of %s not checked: %s", path, exc, exc_info=True)
    return SignatureInfo(STATUS_UNVERIFIED, details=_text_not_checked())
