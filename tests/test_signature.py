"""Embedded AppImage signatures (DESIGN §25).

The signed samples are made here exactly the way the AppImage tools make them: the SHA-256 of
the finished file (``sha256sum``) is written as 64 hex characters, signed with ``gpg
--detach-sign --armor`` (AppImageKit 13: binary mode; the gpgme based appimagetool: text mode)
and embedded in ``.sha256_sig``; the exported public key goes into ``.sig_key``. The keys are
throwaway keys in a temporary GNUPGHOME; the user's key ring is never involved.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from easy_installer.core import signature as sig
from easy_installer.core.elf import read_elf_info
from easy_installer.core.signature import SignatureInfo, read_signature, reference_digest
from fakeappimage import build_runtime

SIG_SIZE = 1024       # what the real runtime reserves
KEY_SIZE = 8192
PAYLOAD = b"hsqs" + bytes(range(256)) * 64

GPG = shutil.which("gpg")
requires_gpg = pytest.mark.skipif(GPG is None, reason="gpg (GnuPG) is not installed")


# --------------------------------------------------------------------------------------------
# building and signing fake AppImages
# --------------------------------------------------------------------------------------------

def make_appimage(path: Path, *, sections: bool = True, payload: bytes = PAYLOAD,
                  upd_info: str | None = "", bits: int = 64) -> Path:
    """A fake type-2 AppImage whose runtime has the (empty) signature sections of a real one."""
    extra = {".digest_md5": bytes(16)}
    if sections:
        extra[".sha256_sig"] = bytes(SIG_SIZE)
        extra[".sig_key"] = bytes(KEY_SIZE)
    runtime = build_runtime(upd_info=upd_info, extra_sections=extra, bits=bits)
    path.write_bytes(runtime + payload)
    path.chmod(0o644)
    return path


def embed(path: Path, section: str, data: bytes) -> None:
    offset, size = read_elf_info(path).sections[section]
    assert len(data) <= size, f"{section}: {len(data)} bytes do not fit into {size}"
    with open(path, "r+b") as handle:
        handle.seek(offset)
        handle.write(data.ljust(size, b"\0"))


def flip_byte(path: Path, offset: int) -> None:
    with open(path, "r+b") as handle:
        handle.seek(offset)
        value = handle.read(1)[0]
        handle.seek(offset)
        handle.write(bytes([value ^ 0x01]))


class Signer:
    """Throwaway keys in a temporary GNUPGHOME (never the user's)."""

    def __init__(self) -> None:
        # Short path: gpg-agent's socket path must fit into 108 bytes.
        self.home = tempfile.mkdtemp(prefix="ei-gpg-")
        self.env = {"PATH": os.environ.get("PATH", os.defpath), "GNUPGHOME": self.home,
                    "HOME": self.home, "LC_ALL": "C"}
        self.main = self._generate("Easy Installer Test <signer@example.invalid>")
        self.other = self._generate("Somebody Else <other@example.invalid>")

    def gpg(self, *args: str, check: bool = True, faked_time: str | None = None) -> bytes:
        command = [GPG, "--homedir", self.home, "--batch", "--no-tty", "--quiet",
                   "--pinentry-mode", "loopback", "--passphrase", ""]
        if faked_time:
            command += ["--faked-system-time", faked_time]
        proc = subprocess.run([*command, *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=120, check=False)
        if check and proc.returncode != 0:
            raise RuntimeError(f"gpg {args} failed: {proc.stderr.decode(errors='replace')}")
        return proc.stdout

    def _generate(self, user_id: str, *, expires: str = "never",
                  faked_time: str | None = None) -> str:
        self.gpg("--quick-generate-key", user_id, "ed25519", "sign", expires,
                 faked_time=faked_time)
        listing = self.gpg("--with-colons", "--list-keys", user_id.split("<")[1].rstrip(">"))
        return next(line.split(":")[9] for line in listing.decode().splitlines()
                    if line.startswith("fpr:"))

    def expired_key(self) -> str:
        return self._generate("Expired Key <old@example.invalid>", expires="2020-02-01",
                              faked_time="20200101T000000")

    def revoke(self, fingerprint: str) -> None:
        """Apply the revocation certificate gpg made when the key was created."""
        text = Path(self.home, "openpgp-revocs.d", f"{fingerprint}.rev").read_text()
        with tempfile.NamedTemporaryFile("w", suffix=".asc", dir=self.home) as handle:
            handle.write(text.replace(":-----BEGIN", "-----BEGIN"))
            handle.flush()
            self.gpg("--import", handle.name)

    def export(self, fingerprint: str) -> bytes:
        return self.gpg("--armor", "--export", fingerprint)

    def detached(self, data: bytes, fingerprint: str, *, textmode: bool = False,
                 faked_time: str | None = None) -> bytes:
        with tempfile.TemporaryDirectory(dir=self.home) as tmp:
            source = Path(tmp, "digest")
            source.write_bytes(data)
            mode = ["--textmode"] if textmode else []
            return self.gpg("--local-user", fingerprint, *mode, "--armor", "--detach-sign",
                            "--output", "-", str(source), faked_time=faked_time)

    def sign(self, path: Path, fingerprint: str | None = None, *, textmode: bool = False,
             embed_key: str | None | bool = True, faked_time: str | None = None) -> str:
        """Sign ``path`` like appimagetool: returns the hex digest that was signed."""
        fingerprint = fingerprint or self.main
        # The sections are still empty, so this is simply `sha256sum <file>`.
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        embed(path, ".sha256_sig",
              self.detached(digest.encode("ascii"), fingerprint, textmode=textmode,
                            faked_time=faked_time))
        if embed_key:
            embed(path, ".sig_key",
                  self.export(fingerprint if embed_key is True else embed_key))
        return digest

    def close(self) -> None:
        gpgconf = shutil.which("gpgconf")
        if gpgconf:
            subprocess.run([gpgconf, "--homedir", self.home, "--kill", "all"], env=self.env,
                           capture_output=True, timeout=30, check=False)
        shutil.rmtree(self.home, ignore_errors=True)


@pytest.fixture(scope="module")
def signer():
    if GPG is None:
        pytest.skip("gpg (GnuPG) is not installed")
    try:
        instance = Signer()
    except (RuntimeError, OSError, subprocess.TimeoutExpired, StopIteration) as exc:
        pytest.skip(f"cannot create a throwaway key here: {exc}")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def signed(tmp_path, signer):
    """A fake AppImage signed like AppImageKit's appimagetool does it."""
    path = make_appimage(tmp_path / "Signed-1.0-x86_64.AppImage")
    signer.sign(path)
    return path


def check(path: Path) -> SignatureInfo | None:
    return read_signature(path, read_elf_info(path))


# --------------------------------------------------------------------------------------------
# not signed -> None (the normal case; must never be anything else)
# --------------------------------------------------------------------------------------------

def test_no_signature_sections_at_all(tmp_path):
    path = make_appimage(tmp_path / "a.AppImage", sections=False)
    assert ".sha256_sig" not in read_elf_info(path).sections
    assert check(path) is None


def test_empty_sections_are_not_a_signature(tmp_path):
    path = make_appimage(tmp_path / "a.AppImage")
    elf = read_elf_info(path)
    assert elf.sections[".sha256_sig"][1] == SIG_SIZE and elf.sections[".sig_key"][1] == KEY_SIZE
    assert check(path) is None


@pytest.mark.parametrize("garbage", [
    b"\xff" * SIG_SIZE,
    bytes(range(256)) * 4,
    b"hello, this is not a signature",
    b"-----BEGIN PGP MESSAGE-----\n\nabc\n-----END PGP MESSAGE-----\n",
    b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nabc\n-----END PGP PUBLIC KEY BLOCK-----\n",
    b"BEGIN PGP SIGNATURE",
    b"\0" * 100 + b"x",
])
def test_garbage_in_the_sections_is_not_a_signature(tmp_path, garbage):
    path = make_appimage(tmp_path / "a.AppImage")
    embed(path, ".sha256_sig", garbage)
    embed(path, ".sig_key", garbage * 2)
    assert check(path) is None


def test_plain_digest_of_go_appimage_is_not_a_signature(tmp_path):
    """go-appimage's appimagetool writes the bare SHA-256 into .sha256_sig of unsigned files."""
    path = make_appimage(tmp_path / "a.AppImage")
    embed(path, ".sha256_sig", hashlib.sha256(path.read_bytes()).hexdigest().encode())
    assert check(path) is None


@requires_gpg
def test_key_without_signature_is_not_signed(tmp_path, signer):
    path = make_appimage(tmp_path / "a.AppImage")
    embed(path, ".sig_key", signer.export(signer.main))
    assert check(path) is None


def test_unsigned_needs_no_gpg(tmp_path, monkeypatch):
    path = make_appimage(tmp_path / "a.AppImage")
    monkeypatch.setattr(sig, "_find_gpg", lambda: pytest.fail("gpg must not be looked for"))
    monkeypatch.setattr(sig, "reference_digest", lambda *a: pytest.fail("no need to hash"))
    assert check(path) is None


def test_missing_file_or_odd_elf_gives_none(tmp_path):
    path = make_appimage(tmp_path / "a.AppImage")
    elf = read_elf_info(path)
    assert read_signature(tmp_path / "gone.AppImage", elf) is None
    assert read_signature(tmp_path, elf) is None                       # a folder
    assert read_signature(path, dataclasses.replace(elf, sections={})) is None
    assert read_signature(path, None) is None                          # type: ignore[arg-type]
    huge = dataclasses.replace(elf, sections={".sha256_sig": (1 << 60, 1 << 60),
                                              ".sig_key": (-5, 12)})
    assert read_signature(path, huge) is None


@pytest.mark.real
def test_real_unsigned_appimages_give_none(real_appimages):
    """Real runtimes reserve both sections; almost every AppImage leaves them empty."""
    for path in real_appimages:
        elf = read_elf_info(path)
        offset, size = elf.sections.get(".sha256_sig", (0, 0))
        with open(path, "rb") as handle:
            handle.seek(offset)
            signed = b"BEGIN PGP SIGNATURE" in handle.read(min(size, 65536))
        info = read_signature(path, elf)
        assert (info is not None) == signed, path


# --------------------------------------------------------------------------------------------
# valid / invalid
# --------------------------------------------------------------------------------------------

@requires_gpg
@pytest.mark.parametrize("textmode", [False, True], ids=["AppImageKit-13", "gpgme-appimagetool"])
def test_signed_appimage_is_valid(tmp_path, signer, textmode):
    path = make_appimage(tmp_path / "Signed.AppImage",
                         upd_info="gh-releases-zsync|me|app|latest|App-*.AppImage.zsync")
    digest = signer.sign(path, textmode=textmode)
    elf = read_elf_info(path)
    assert reference_digest(path, elf) == digest
    info = read_signature(path, elf)
    assert info == SignatureInfo("valid", signer.main,
                                 "Easy Installer Test <signer@example.invalid>", None)


@requires_gpg
def test_signed_32_bit_appimage_is_valid(tmp_path, signer):
    path = make_appimage(tmp_path / "Signed.AppImage", bits=32)
    signer.sign(path)
    assert check(path).status == "valid"


@requires_gpg
def test_one_changed_byte_makes_it_invalid(signed, signer):
    assert check(signed).status == "valid"
    size = signed.stat().st_size
    elf = read_elf_info(signed)
    for offset in (size - 1, elf.payload_offset + 10, 20, elf.sections[".upd_info"][0] + 1,
                   elf.sections[".digest_md5"][0]):
        flip_byte(signed, offset)
        info = check(signed)
        assert info.status == "invalid", offset
        assert info.fingerprint == signer.main          # who it claims to be from
        assert "changed" in info.details
        flip_byte(signed, offset)                       # undo
    assert check(signed).status == "valid"


@requires_gpg
def test_appended_or_cut_off_data_makes_it_invalid(signed):
    original = signed.read_bytes()
    signed.write_bytes(original + b"\0")
    assert check(signed).status == "invalid"
    signed.write_bytes(original[:-1])
    assert check(signed).status == "invalid"
    signed.write_bytes(original)
    assert check(signed).status == "valid"


@requires_gpg
def test_signature_of_another_file_is_invalid(tmp_path, signer, signed):
    other = make_appimage(tmp_path / "Other.AppImage", payload=PAYLOAD + b"something else")
    signer.sign(other)
    offset, size = read_elf_info(other).sections[".sha256_sig"]
    embed(signed, ".sha256_sig", other.read_bytes()[offset:offset + size].rstrip(b"\0"))
    assert check(signed).status == "invalid"


@requires_gpg
def test_the_two_sections_themselves_are_not_part_of_the_digest(signed):
    """Reference behaviour: .sha256_sig and .sig_key are read as zeros, everything else counts."""
    elf = read_elf_info(signed)
    key_offset, key_size = elf.sections[".sig_key"]
    flip_byte(signed, key_offset + key_size - 1)        # padding behind the key
    assert check(signed).status == "valid"
    expected = bytearray(signed.read_bytes())
    for name in (".sha256_sig", ".sig_key"):
        offset, size = elf.sections[name]
        expected[offset:offset + size] = bytes(size)
    assert reference_digest(signed, elf) == hashlib.sha256(expected).hexdigest()


def test_reference_digest_of_an_unsigned_file_is_its_sha256(tmp_path):
    path = make_appimage(tmp_path / "a.AppImage")
    assert reference_digest(path, read_elf_info(path)) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_reference_digest_survives_odd_section_tables(tmp_path):
    path = make_appimage(tmp_path / "a.AppImage")
    elf = read_elf_info(path)
    data = path.read_bytes()
    size = len(data)

    def digest(sections):
        return reference_digest(path, dataclasses.replace(elf, sections=sections))

    assert digest({}) == hashlib.sha256(data).hexdigest()
    # overlapping, touching, beyond the end, negative, empty
    zeroed = bytearray(data)
    zeroed[10:50] = bytes(40)
    assert digest({".sha256_sig": (10, 30), ".sig_key": (20, 30)}) == hashlib.sha256(zeroed).hexdigest()
    tail = bytearray(data)
    tail[size - 8:] = bytes(8)
    assert digest({".sha256_sig": (size - 8, 1 << 40)}) == hashlib.sha256(tail).hexdigest()
    assert digest({".sha256_sig": (size + 5, 10), ".sig_key": (-3, 0)}) == hashlib.sha256(data).hexdigest()
    assert digest({".sig_key": (0, size)}) == hashlib.sha256(bytes(size)).hexdigest()


# --------------------------------------------------------------------------------------------
# unverified: there is a signature, but nothing could be established
# --------------------------------------------------------------------------------------------

@requires_gpg
def test_missing_gpg_gives_unverified(signed, tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    info = check(signed)
    assert info == SignatureInfo("unverified", None, None, info.details, "no-gpg")
    assert "GnuPG" in info.details and info.explanation == info.details


def test_missing_gpg_needs_no_real_signature(tmp_path, monkeypatch):
    """Hermetic variant: a made-up signature block and an empty PATH."""
    path = make_appimage(tmp_path / "a.AppImage")
    embed(path, ".sha256_sig", b"-----BEGIN PGP SIGNATURE-----\n\nAAAA\n=AAAA\n-----END PGP SIGNATURE-----\n")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    info = check(path)
    assert info.status == "unverified" and info.fingerprint is None and "GnuPG" in info.details


def fake_gpg(directory: Path, script: str) -> Path:
    directory.mkdir(exist_ok=True)
    tool = directory / "gpg"
    tool.write_text("#!/bin/sh\n" + script)
    tool.chmod(0o755)
    return tool


def made_up_signature(tmp_path: Path) -> Path:
    path = make_appimage(tmp_path / "a.AppImage")
    embed(path, ".sha256_sig", b"-----BEGIN PGP SIGNATURE-----\n\nAAAA\n=AAAA\n-----END PGP SIGNATURE-----\n")
    embed(path, ".sig_key", b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nAAAA\n=AAAA\n-----END PGP PUBLIC KEY BLOCK-----\n")
    return path


def test_gpg_that_hangs_gives_unverified(tmp_path, monkeypatch):
    path = made_up_signature(tmp_path)
    monkeypatch.setenv("PATH", str(fake_gpg(tmp_path / "bin", "exec sleep 30\n").parent) + ":/usr/bin:/bin")
    monkeypatch.setattr(sig, "GPG_TIMEOUT", 0.3)
    info = check(path)
    assert info.status == "unverified" and info.details


def test_gpg_that_fails_gives_unverified(tmp_path, monkeypatch):
    path = made_up_signature(tmp_path)
    monkeypatch.setenv("PATH", str(fake_gpg(tmp_path / "bin", "echo boom >&2\nexit 2\n").parent))
    assert check(path).status == "unverified"


def test_gpg_that_cannot_be_started_gives_unverified(tmp_path, monkeypatch):
    path = made_up_signature(tmp_path)
    monkeypatch.setattr(sig, "_find_gpg", lambda: str(tmp_path / "no-such-gpg"))
    assert check(path).status == "unverified"


def test_a_lying_gpg_without_validsig_is_not_trusted(tmp_path, monkeypatch):
    """GOODSIG alone (no VALIDSIG with a fingerprint) or a failing exit code is never "valid"."""
    path = made_up_signature(tmp_path)
    script = 'echo "[GNUPG:] GOODSIG 0123456789ABCDEF Somebody"\nexit 0\n'
    monkeypatch.setenv("PATH", str(fake_gpg(tmp_path / "bin", script).parent))
    assert check(path).status == "unverified"


def test_gpg_runs_in_a_private_home_with_safe_options(tmp_path, monkeypatch):
    path = made_up_signature(tmp_path)
    log = tmp_path / "calls.log"
    script = (f'echo "$GNUPGHOME|$HOME|$*" >> "{log}"\n'
              f'stat -c %a "$GNUPGHOME" >> "{log}"\nexit 2\n')
    monkeypatch.setenv("PATH", str(fake_gpg(tmp_path / "bin", script).parent) + ":/usr/bin:/bin")
    users_keyring = tmp_path / "users-gnupg"
    monkeypatch.setenv("GNUPGHOME", str(users_keyring))
    assert check(path).status == "unverified"
    lines = log.read_text().splitlines()
    calls, modes = lines[0::2], lines[1::2]
    assert calls and set(modes) == {"700"}
    for call in calls:
        gnupghome, home, args = call.split("|", 2)
        assert gnupghome == home and gnupghome != str(users_keyring)
        assert f"--homedir {gnupghome} --batch --no-tty" in args
        assert "--no-autostart" in args and "--no-auto-key-retrieve" in args
        assert not Path(gnupghome).exists(), "the temporary key ring must be removed"
    assert not users_keyring.exists()


@requires_gpg
def test_the_users_keyring_is_never_touched(signed, isolated_env, tmp_path, monkeypatch):
    users_keyring = tmp_path / "users-gnupg"
    monkeypatch.setenv("GNUPGHOME", str(users_keyring))
    before = set(Path(tempfile.gettempdir()).glob("easy-installer-gpg-*"))
    assert check(signed).status == "valid"
    assert not users_keyring.exists()
    assert not (isolated_env / ".gnupg").exists()
    assert set(Path(tempfile.gettempdir()).glob("easy-installer-gpg-*")) == before


@requires_gpg
def test_signature_without_embedded_key_is_unverified(tmp_path, signer):
    path = make_appimage(tmp_path / "a.AppImage")
    signer.sign(path, embed_key=False)
    info = check(path)
    assert info.status == "unverified" and info.signer is None
    assert "key" in info.details
    assert info.fingerprint in (signer.main, None)      # gpg >= 2.2 names the signing key


@requires_gpg
def test_signature_by_another_key_than_the_embedded_one_is_unverified(tmp_path, signer):
    path = make_appimage(tmp_path / "a.AppImage")
    signer.sign(path, signer.main, embed_key=signer.other)
    info = check(path)
    assert info.status == "unverified"
    assert info.fingerprint != signer.other and info.signer is None
    assert "does not belong" in info.details


@requires_gpg
def test_damaged_embedded_key_is_unverified(tmp_path, signer):
    path = make_appimage(tmp_path / "a.AppImage")
    signer.sign(path)
    flip_byte(path, read_elf_info(path).sections[".sig_key"][0] + 80)
    assert check(path).status == "unverified"


@requires_gpg
def test_damaged_signature_is_unverified_not_valid(signed):
    offset = read_elf_info(signed).sections[".sha256_sig"][0]
    flip_byte(signed, offset + 60)                      # inside the base64 text
    info = check(signed)
    assert info.status == "unverified" and "could not be read" in info.details


@requires_gpg
def test_cut_off_signature_is_unverified(signed):
    offset, size = read_elf_info(signed).sections[".sha256_sig"]
    block = signed.read_bytes()[offset:offset + size].rstrip(b"\0")
    embed(signed, ".sha256_sig", block[:-40])           # "-----END PGP SIGNATURE-----" is gone
    info = check(signed)
    assert info.status == "unverified" and "could not be read" in info.details


@requires_gpg
def test_signature_of_an_empty_text_is_unverified(tmp_path, signer):
    """go-appimage signs "" when the AppImage has update information: says nothing about the file."""
    path = make_appimage(tmp_path / "a.AppImage")
    embed(path, ".sha256_sig", signer.detached(b"", signer.main))
    embed(path, ".sig_key", signer.export(signer.main))
    info = check(path)
    assert info.status == "unverified" and info.fingerprint == signer.main
    assert "does not cover" in info.details


@requires_gpg
def test_signature_over_the_raw_digest_bytes_is_invalid(tmp_path, signer):
    """The signed data is the hex text - a signature of the 32 raw bytes does not count."""
    path = make_appimage(tmp_path / "a.AppImage")
    raw = hashlib.sha256(path.read_bytes()).digest()
    embed(path, ".sha256_sig", signer.detached(raw, signer.main))
    embed(path, ".sig_key", signer.export(signer.main))
    assert check(path).status == "invalid"


@requires_gpg
def test_expired_key_is_unverified(tmp_path, signer):
    try:
        expired = signer.expired_key()
    except (RuntimeError, StopIteration) as exc:
        pytest.skip(f"this gpg cannot make a back-dated key: {exc}")
    path = make_appimage(tmp_path / "a.AppImage")
    signer.sign(path, expired, faked_time="20200110T000000")
    info = check(path)
    assert info.status == "unverified" and info.fingerprint == expired
    assert info.signer == "Expired Key <old@example.invalid>"
    assert "expired" in info.details


@requires_gpg
def test_revoked_key_is_invalid(tmp_path, signer):
    fingerprint = signer._generate("Revoked Key <revoked@example.invalid>")
    path = make_appimage(tmp_path / "a.AppImage")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    signature = signer.detached(digest.encode(), fingerprint)
    try:
        signer.revoke(fingerprint)
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"no revocation certificate: {exc}")
    embed(path, ".sha256_sig", signature)
    embed(path, ".sig_key", signer.export(fingerprint))
    info = check(path)
    assert info.status == "invalid" and info.fingerprint == fingerprint
    assert "withdrawn" in info.details


def test_unexpected_errors_never_escape(tmp_path, monkeypatch):
    path = made_up_signature(tmp_path)

    def boom(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(sig, "_check", boom)
    assert check(path).status == "unverified"
    monkeypatch.setattr(sig, "_embedded_signature", boom)
    assert check(path) is None


# --------------------------------------------------------------------------------------------
# how gpg's answers are read
# --------------------------------------------------------------------------------------------

FPR = "E8212F503765BBD5EF5D5B292BF4B56A124A6AE6"
SUB = "0123456789ABCDEF0123456789ABCDEF01234567"


def status(*lines: str) -> list[tuple[str, list[str]]]:
    return sig._parse_status("".join(f"[GNUPG:] {line}\n" for line in lines).encode())


def valid_line(fingerprint: str = FPR, primary: str | None = FPR) -> str:
    return f"VALIDSIG {fingerprint} 2026-08-29 1787987537 0 4 0 1 10 01" + (f" {primary}" if primary else "")


def test_interpret_good_signature():
    verdict = sig._interpret(status("NEWSIG", "GOODSIG 2BF4B56A124A6AE6 Git Butler <x@y>", valid_line()), 0)
    assert verdict == sig._Verdict("good", FPR)


def test_interpret_prefers_the_primary_key_fingerprint():
    verdict = sig._interpret(status("NEWSIG", "GOODSIG AAAA Name", valid_line(SUB, FPR)), 0)
    assert verdict == sig._Verdict("good", FPR)
    verdict = sig._interpret(status("NEWSIG", "GOODSIG AAAA Name", valid_line(SUB, None)), 0)
    assert verdict == sig._Verdict("good", SUB)


@pytest.mark.parametrize("lines, returncode, outcome", [
    (["NEWSIG", "BADSIG 2BF4B56A124A6AE6 Name"], 1, "bad"),
    (["NEWSIG", "GOODSIG A Name", valid_line(), "NEWSIG", "BADSIG B Name"], 1, "bad"),
    (["NEWSIG", "EXPKEYSIG B45E1BB05D530376 Name", valid_line()], 0, "expired"),
    (["NEWSIG", "EXPSIG B45E1BB05D530376 Name", valid_line()], 0, "expired"),
    (["NEWSIG", "REVKEYSIG B45E1BB05D530376 Name", valid_line()], 0, "revoked"),
    (["NEWSIG", f"ERRSIG 2BF4B56A124A6AE6 1 10 01 1787987537 9 {FPR}", "NO_PUBKEY 2BF4B56A124A6AE6"], 2, "no-key"),
    (["NEWSIG", "ERRSIG 2BF4B56A124A6AE6 1 10 00 1787987537 9"], 2, "no-key"),
    (["NEWSIG", "ERRSIG 2BF4B56A124A6AE6 99 10 00 1787987537 4"], 2, "error"),
    (["NODATA 4", "FAILURE gpg-exit 33554433"], 2, "unreadable"),
    (["NEWSIG", "GOODSIG A Name"], 0, "error"),                      # no VALIDSIG
    (["NEWSIG", "GOODSIG A Name", valid_line()], 1, "error"),        # gpg itself says "failed"
    (["NEWSIG", "GOODSIG A Name", "VALIDSIG nonsense"], 0, "error"),
    (["NEWSIG", "NEWSIG", "GOODSIG A Name", valid_line()], 0, "error"),   # a second, unknown one
    (["NEWSIG", "GOODSIG A Name", valid_line(), "NEWSIG", "ERRSIG B 1 10 00 1 9"], 2, "no-key"),
    ([], 0, "error"),
    (["GOODSIG"], 0, "error"),
])
def test_interpret_everything_else_is_not_good(lines, returncode, outcome):
    assert sig._interpret(status(*lines), returncode).outcome == outcome


def test_interpret_names_the_signing_key_of_an_unknown_signature():
    verdict = sig._interpret(status("NEWSIG", f"ERRSIG 2BF4B56A124A6AE6 1 10 01 1 9 {FPR}"), 2)
    assert verdict == sig._Verdict("no-key", FPR)
    verdict = sig._interpret(status("NEWSIG", "ERRSIG 2bf4b56a124a6ae6 1 10 01 1 9"), 2)
    assert verdict == sig._Verdict("no-key", "2BF4B56A124A6AE6")


def test_parse_status_ignores_everything_else():
    output = b"gpg: some text\n[GNUPG:] NEWSIG\n[GNUPG:]\nnoise [GNUPG:] BADSIG x\n[GNUPG:] \xff\xfe X\n"
    assert [word for word, _args in sig._parse_status(output)] == ["NEWSIG", "��"]


def test_parse_keys():
    listing = (
        b"pub:-:4096:1:2BF4B56A124A6AE6:1690810649:::-:::scSC::::::23::0:\n"
        b"fpr:::::::::E8212F503765BBD5EF5D5B292BF4B56A124A6AE6:\n"
        b"uid:r::::1690810649::AAAA::Old Name <old@example.org>::::::::::0:\n"
        b"uid:-::::1690810649::5662::GitButler\\x3a Inc \\xc3\\xa9\\x0a\\x1b[31m <hello@gitbutler.com>::::::::::0:\n"
        b"sub:-:4096:1:1111111111111111:1690810649::::::e::::::23:\n"
        b"fpr:::::::::0123456789ABCDEF0123456789ABCDEF01234567:\n"
        b"pub:e:255:22:B45E1BB05D530376:1577836800:1580554800::u:::sc:::::ed25519:::0:\n"
        b"fpr:::::::::0A9C32143E4D7CD0EE92C7B1B45E1BB05D530376:\n"
        b"uid:e::::1577836800::0328::Expired Signer <old@example.invalid>::::::::::0:\n"
        b"pub:r:255:22:AAAAAAAAAAAAAAAA:1577836800:::u:::sc:::::ed25519:::0:\n"
        b"fpr:::::::::not-a-fingerprint:\n"
        b"garbage line\n"
    )
    keys = sig._parse_keys(listing)
    assert [key.fingerprint for key in keys] == [FPR, "0A9C32143E4D7CD0EE92C7B1B45E1BB05D530376"]
    first, second = keys
    # escapes are decoded, control characters never reach the user interface
    assert first.user_ids == ["GitButler: Inc é [31m <hello@gitbutler.com>", "Old Name <old@example.org>"]
    assert first.subkeys == [SUB] and not first.revoked and not first.expired
    assert first.owns(FPR) and first.owns("2BF4B56A124A6AE6") and first.owns(SUB.lower())
    assert not first.owns("6AE6") and not first.owns(None) and not first.owns("")
    assert second.expired and second.user_ids == ["Expired Signer <old@example.invalid>"]


# --------------------------------------------------------------------------------------------
# SignatureInfo
# --------------------------------------------------------------------------------------------

def test_signature_info_round_trip():
    info = SignatureInfo("valid", FPR, "GitButler <hello@gitbutler.com>", None)
    assert info.to_dict() == {"status": "valid", "fingerprint": FPR,
                              "signer": "GitButler <hello@gitbutler.com>", "details": None,
                              "reason": None}
    assert SignatureInfo.from_dict(info.to_dict()) == info
    other = SignatureInfo("unverified", None, None, "The signature could not be checked.")
    assert SignatureInfo.from_dict(other.to_dict()) == other
    assert SignatureInfo("invalid") == SignatureInfo("invalid", None, None, None)


def test_the_reason_is_told_in_the_language_of_the_reader(monkeypatch):
    """I18N-1: an app checked from an English terminal shows its note in German later."""
    stored = SignatureInfo("unverified", FPR, None, "The file does not include the signer's "
                           "key, so the signature could not be checked.", "no-key").to_dict()
    monkeypatch.setattr(sig, "_text_no_key", lambda: "Die Datei enthält den Schlüssel nicht.")
    monkeypatch.setitem(sig.REASONS, "no-key", sig._text_no_key)
    info = SignatureInfo.from_dict(stored)
    assert info.reason == "no-key" and info.explanation == "Die Datei enthält den Schlüssel nicht."
    # a record made by 0.2.0 has only its sentence; an unknown code is ignored
    old = SignatureInfo.from_dict({**stored, "reason": None})
    assert old.explanation == stored["details"]
    assert SignatureInfo.from_dict({**stored, "reason": "rm -rf"}).reason is None


@pytest.mark.parametrize("value", [
    None, "valid", 5, [], {}, {"status": None}, {"status": "great"}, {"status": "VALID"},
    {"status": ["valid"]}, {"fingerprint": FPR},
])
def test_signature_info_from_dict_rejects_nonsense(value):
    assert SignatureInfo.from_dict(value) is None


def test_signature_info_from_dict_cleans_what_it_reads():
    info = SignatureInfo.from_dict({
        "status": "valid",
        "fingerprint": FPR.lower(),
        "signer": "Evil\x1b[31m ‮Name\n<x@y>\0",
        "details": 12,
        "extra": "ignored",
    })
    assert info == SignatureInfo("valid", FPR, "Evil[31m Name <x@y>", None)
    assert SignatureInfo.from_dict({"status": "invalid", "fingerprint": "../../etc"}).fingerprint is None
    assert SignatureInfo.from_dict({"status": "invalid", "fingerprint": 7}).fingerprint is None
    long = SignatureInfo.from_dict({"status": "unverified", "signer": "x" * 5000, "details": "y" * 5000})
    assert len(long.signer) <= sig.MAX_SIGNER_LENGTH and len(long.details) <= sig.MAX_DETAILS_LENGTH


# --------------------------------------------------------------------------------------------
# a real signed AppImage, if one is at hand
# --------------------------------------------------------------------------------------------

@pytest.mark.real
@requires_gpg
def test_real_signed_appimage(tmp_path):
    """Set EASY_INSTALLER_TEST_SIGNED_APPIMAGE to a signed AppImage (e.g. GitButler's) to run this."""
    name = os.environ.get("EASY_INSTALLER_TEST_SIGNED_APPIMAGE")
    if not name or not Path(name).is_file():
        pytest.skip("no real signed AppImage given (EASY_INSTALLER_TEST_SIGNED_APPIMAGE)")
    source = Path(name)
    info = read_signature(source, read_elf_info(source))
    assert info is not None and info.status == "valid" and info.fingerprint and info.signer
    if source.stat().st_size > 300 * 1024 * 1024:
        return
    copy = tmp_path / source.name
    shutil.copyfile(source, copy)
    flip_byte(copy, copy.stat().st_size - 4096)
    changed = read_signature(copy, read_elf_info(copy))
    assert changed.status == "invalid" and changed.fingerprint == info.fingerprint
