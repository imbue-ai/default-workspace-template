"""Install the plugin's local CA into OS / browser trust stores (mkcert-style).

Used by ``mngr forward --trust-ca``: after this one-time, user-consented
install, plain browsers accept the proxy's per-startup leaf certificates for
``https://[<service>.]host-<hex>.localhost:<port>`` origins without
interstitials. The Electron shell never needs this -- it trusts the proxy
programmatically -- so the install exists purely for the plain-browser
testing/dev surface.

Two stores are targeted, mirroring what mkcert does for the browsers we care
about (Chromium is the supported target):

- macOS: the user's login keychain (``security add-trusted-cert``), which
  both Safari and Chrome consult.
- Linux: the per-user NSS database (``certutil -d sql:$HOME/.pki/nssdb``),
  which Chrome/Chromium consult. The system-wide store
  (``update-ca-certificates``) needs root, so instructions are printed
  instead of attempting a sudo dance.

The install is idempotent: re-running replaces/no-ops on the existing entry.
"""

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.mngr_forward.errors import ForwardTrustError

_NSS_NICKNAME: Final[str] = "mngr forward local CA"

_TRUST_COMMAND_TIMEOUT_SECONDS: Final[float] = 60.0


def _run_trust_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one trust-store command, raising ``ForwardTrustError`` on failure."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_TRUST_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ForwardTrustError(f"Trust-store command failed to run: {' '.join(command)}") from e
    if result.returncode != 0:
        raise ForwardTrustError(
            f"Trust-store command exited {result.returncode}: {' '.join(command)}\n{result.stderr.strip()}"
        )
    return result


def _install_ca_macos(ca_cert_path: Path) -> None:
    login_keychain = Path.home() / "Library" / "Keychains" / "login.keychain-db"
    _run_trust_command(
        [
            "security",
            "add-trusted-cert",
            "-r",
            "trustRoot",
            "-k",
            str(login_keychain),
            str(ca_cert_path),
        ]
    )
    logger.info("Installed the local CA into the login keychain ({})", login_keychain)


def _install_ca_linux_nss(ca_cert_path: Path) -> None:
    certutil = shutil.which("certutil")
    if certutil is None:
        raise ForwardTrustError(
            "certutil (from libnss3-tools / nss-tools) is required to install the CA into Chrome's "
            "per-user NSS store on Linux. Install it (e.g. `apt install libnss3-tools`) and re-run, "
            "or import the CA manually in your browser's certificate settings."
        )
    nss_dir = Path.home() / ".pki" / "nssdb"
    nss_dir.mkdir(parents=True, exist_ok=True)
    # `-A` adds-or-replaces by nickname, so re-running is idempotent. "C,,"
    # marks the cert trusted for issuing TLS server certs.
    _run_trust_command(
        [
            certutil,
            "-d",
            f"sql:{nss_dir}",
            "-A",
            "-t",
            "C,,",
            "-n",
            _NSS_NICKNAME,
            "-i",
            str(ca_cert_path),
        ]
    )
    logger.info("Installed the local CA into the per-user NSS store ({})", nss_dir)
    logger.info(
        "For non-Chromium consumers of the system store, install manually (needs root), e.g.: "
        "`sudo cp {} /usr/local/share/ca-certificates/mngr-forward-local-ca.crt && sudo update-ca-certificates`",
        ca_cert_path,
    )


def install_ca_into_trust_stores(ca_cert_path: Path) -> None:
    """Install the CA certificate at ``ca_cert_path`` into this platform's trust stores.

    Raises ``ForwardTrustError`` with actionable text when the platform's
    tooling is missing or the install command fails.
    """
    if not ca_cert_path.exists():
        raise ForwardTrustError(f"CA certificate not found at {ca_cert_path}")
    system = platform.system()
    if system == "Darwin":
        _install_ca_macos(ca_cert_path)
    elif system == "Linux":
        _install_ca_linux_nss(ca_cert_path)
    else:
        raise ForwardTrustError(f"CA trust install is not supported on {system} (macOS and Linux only).")
