"""The Fortress env.d unit's managed policy must be one Chromium can use.

Chromium ignores a policy file that is not valid JSON, and silently skips a
``CADistrustedCertificates`` entry that is not a base64 DER certificate, so a
policy that looks right can distrust nothing.
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

_UNIT_PATH = Path(__file__).parent / "env.d" / "1000-playwright-fortress.sh"
_POLICY_HEREDOC = re.compile(r"<<'POLICY'\n(.*?)\nPOLICY\n", re.DOTALL)

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


def _policy() -> dict[str, Any]:
    match = _POLICY_HEREDOC.search(_UNIT_PATH.read_text())
    assert match is not None, f"no <<'POLICY' heredoc in {_UNIT_PATH}"
    return json.loads(match.group(1))


def test_the_policy_distrusts_exactly_the_compromised_keys() -> None:
    policy = _policy()
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


def _run_installer(policy_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the unit's policy installer against ``policy_dir``, called the way its main() calls it."""
    unit = _UNIT_PATH.read_text()
    # Sliced up to the next function: the policy heredoc puts a "}" in column 0 too.
    start = unit.index("_install_distrust_policy() {\n")
    function = unit[start : unit.index("\n_install_fortress() {", start)]
    script = (
        "set -euo pipefail\n"
        "_log() { printf '%s\\n' \"$*\"; }\n"
        f"_CHROMIUM_POLICY_DIR={str(policy_dir)!r}\n"
        '_DISTRUST_POLICY="${_CHROMIUM_POLICY_DIR}/distrust.json"\n'
        + function
        + 'rc=0\n_install_distrust_policy || rc=$?\necho "rc=$rc"\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


def test_the_installer_writes_the_policy_once(tmp_path: Path) -> None:
    policy_dir = tmp_path / "chromium" / "policies" / "managed"

    first = _run_installer(policy_dir)
    second = _run_installer(policy_dir)

    assert "rc=0" in first.stdout
    assert json.loads((policy_dir / "distrust.json").read_text()) == _policy()
    assert "rc=0" in second.stdout and "already in place" in second.stdout
    assert list(policy_dir.parent.iterdir()) == [policy_dir]


def test_the_installer_reports_a_policy_it_could_not_write(tmp_path: Path) -> None:
    blocker = tmp_path / "chromium"
    blocker.write_text("a file where the policy directory's parent should be\n")

    result = _run_installer(blocker / "policies" / "managed")

    assert "rc=0" not in result.stdout
    assert "wrote" not in result.stdout
