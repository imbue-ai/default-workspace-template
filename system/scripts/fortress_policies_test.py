"""The Fortress env.d unit's managed policies must be ones Chromium can use.

Chromium ignores a policy file that is not valid JSON, and silently skips a
``CADistrustedCertificates`` entry that is not a base64 DER certificate, so a
policy that looks right can distrust nothing; and the Local Network Access policy
is what keeps a page the Screen's browser shows from asking to reach the
workspace's loopback services, so it must deny every URL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_UNIT_PATH = Path(__file__).parent / "env.d" / "2000-screen.sh"
_DISTRUST_HEREDOC = re.compile(
    r"<<'DISTRUST_POLICY'\n(.*?)\nDISTRUST_POLICY\n", re.DOTALL
)
_LOCAL_NETWORK_HEREDOC = re.compile(
    r"<<'LOCAL_NETWORK_POLICY'\n(.*?)\nLOCAL_NETWORK_POLICY\n", re.DOTALL
)

# SHA-256 of the SubjectPublicKeyInfo of each compromised eMudhra intermediate,
# as electron/electron#55126 adds them to Chromium's SPKI blocklist.
_COMPROMISED_SPKI_SHA256 = {
    "49dd353dc2b919a3770645117d0e60c16115e7d1bc259eefe734b00ea434fe5f",
    "5c0660a2b164910d8ff589c18e14552e9e0ec237dcefe2cb736d41a4811f81a0",
    "63ecefee431d49523bc1136c4e72d0206a477086caf606cd5cf025686c109cf8",
    "92c063d67e8f14db71d288e11266ffdaf69cbf115f7bab14f3bc2f68d25e0e30",
    "ebed6475c2c2d80fcb199f3fe8dd6e6b3ce60b77eb15d4e42c3dec0c6f5b5105",
    "ec4ffa1ad0ac88081314b7aaffef6a91f97cd57291bb5ba686c5415f5eb4f6b6",
    "43bffa19d6ef651451f6841e019931e96f6f2eaeff28ab4b6f1ce4576de2717b",
    "e0254c7fcdc265e2f0f8a428dabb75ab4ae039b4872d62f5ee57f0877d5a8a41",
}


def _policy(heredoc: re.Pattern[str]) -> dict[str, Any]:
    match = heredoc.search(_UNIT_PATH.read_text())
    assert match is not None, f"no {heredoc.pattern[:30]} heredoc in {_UNIT_PATH}"
    return json.loads(match.group(1))


def test_the_local_network_policy_denies_every_site_the_permission() -> None:
    policy = _policy(_LOCAL_NETWORK_HEREDOC)
    assert policy == {"LocalNetworkAccessBlockedForUrls": ["*"]}


def test_the_policy_distrusts_exactly_the_compromised_keys() -> None:
    policy = _policy(_DISTRUST_HEREDOC)
    assert list(policy) == ["CADistrustedCertificates"]

    spki_hashes = set()
    for entry in policy["CADistrustedCertificates"]:
        certificate = x509.load_der_x509_certificate(
            base64.b64decode(entry, validate=True)
        )
        spki = certificate.public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo
        )
        spki_hashes.add(hashlib.sha256(spki).hexdigest())

    assert spki_hashes == _COMPROMISED_SPKI_SHA256


def _run_installers(policy_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the unit's two policy installers against ``policy_dir``, called the way its main() calls them."""
    unit = _UNIT_PATH.read_text()
    # Sliced up to the next function: the policy heredocs put a "}" in column 0 too.
    start = unit.index("_install_policy_file() {\n")
    functions = unit[start : unit.index("\n_install_fortress() {", start)]
    script = (
        "set -euo pipefail\n"
        "_log() { printf '%s\\n' \"$*\"; }\n"
        f"_CHROMIUM_POLICY_DIR={str(policy_dir)!r}\n"
        '_DISTRUST_POLICY="${_CHROMIUM_POLICY_DIR}/distrust.json"\n'
        '_LOCAL_NETWORK_POLICY="${_CHROMIUM_POLICY_DIR}/local-network.json"\n'
        + functions
        + 'rc=0\n_install_distrust_policy || rc=$?\n_install_local_network_policy || rc=$?\necho "rc=$rc"\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


def test_the_installers_write_each_policy_once(tmp_path: Path) -> None:
    policy_dir = tmp_path / "chromium" / "policies" / "managed"

    first = _run_installers(policy_dir)
    second = _run_installers(policy_dir)

    assert "rc=0" in first.stdout
    assert json.loads((policy_dir / "distrust.json").read_text()) == _policy(
        _DISTRUST_HEREDOC
    )
    assert json.loads((policy_dir / "local-network.json").read_text()) == _policy(
        _LOCAL_NETWORK_HEREDOC
    )
    assert (policy_dir / "local-network.json").stat().st_mode & 0o777 == 0o644
    assert "rc=0" in second.stdout and second.stdout.count("already in place") == 2
    assert list(policy_dir.parent.iterdir()) == [policy_dir]


def test_the_installer_reports_a_policy_it_could_not_write(tmp_path: Path) -> None:
    blocker = tmp_path / "chromium"
    blocker.write_text("a file where the policy directory's parent should be\n")

    result = _run_installers(blocker / "policies" / "managed")

    assert "rc=0" not in result.stdout
    assert "wrote" not in result.stdout
