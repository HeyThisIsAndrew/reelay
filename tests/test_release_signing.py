"""The macOS build's signing preflight (packaging/build_mac.sh with REELAY_PREFLIGHT_ONLY=1): it decides how
the app will be signed and refuses a notarization setup that can't work, before anything is built."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "packaging" / "build_mac.sh"
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS build script")


def preflight(**env):
    full = {k: v for k, v in os.environ.items() if not k.startswith("REELAY_")} | {"REELAY_PREFLIGHT_ONLY": "1"}
    full.update(env)
    run = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=full, timeout=60)
    return run.returncode, run.stdout + run.stderr


def identities() -> str:
    return subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True,
                          text=True).stdout


def test_default_build_needs_no_notarization_credentials():
    code, out = preflight()
    assert code == 0 and "not notarized" in out
    if "Apple Development:" in identities() or "Developer ID Application:" in identities():
        assert "hardened runtime" in out and "no hardened runtime" not in out
    else:
        assert "ad-hoc signature, no hardened runtime" in out


def test_ad_hoc_builds_skip_the_hardened_runtime():
    code, out = preflight(REELAY_SIGN_IDENTITY="-")
    assert code == 0 and "ad-hoc signature, no hardened runtime, not notarized" in out


def test_notarizing_without_a_developer_id_fails_clearly_before_building():
    code, out = preflight(REELAY_SIGN_IDENTITY="-", REELAY_NOTARY_PROFILE="reelay-notary")
    assert code == 1 and 'needs a "Developer ID Application" identity' in out
    assert "PyInstaller" not in out and "Built:" not in out


def test_an_unknown_identity_fails_clearly():
    code, out = preflight(REELAY_SIGN_IDENTITY="No Such Identity For Reelay")
    assert code == 1 and "isn't in this keychain" in out


@pytest.mark.skipif("Developer ID Application:" not in identities(), reason="no Developer ID certificate here")
def test_a_missing_notarization_profile_fails_clearly():
    code, out = preflight(REELAY_NOTARY_PROFILE="reelay-no-such-profile")
    assert code == 1 and "couldn't be used" in out
