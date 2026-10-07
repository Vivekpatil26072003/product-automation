"""Real ClamAV (infra/local) verdicts. Skipped when clamd is not reachable."""

import pytest

from app.core.config import get_settings
from app.ingestion.scanner import ClamdScanner, ScannerUnavailable
from tests import filegen

pytestmark = pytest.mark.infra


@pytest.fixture(scope="module")
def clamd():
    s = get_settings()
    scanner = ClamdScanner(s.clamd_host, s.clamd_port, timeout=30)
    try:
        scanner.version()
    except ScannerUnavailable:
        pytest.skip("clamd unreachable")
    return scanner


def test_clean_file(clamd):
    verdict = clamd.scan(filegen.pdf())
    assert verdict.status == "CLEAN" and verdict.scanner.startswith("clamav")


def test_eicar_detected(clamd):
    verdict = clamd.scan(filegen.EICAR)
    assert verdict.status == "INFECTED" and "Eicar" in verdict.signature


def test_unreachable_scanner_is_never_clean():
    with pytest.raises(ScannerUnavailable):
        ClamdScanner("127.0.0.1", 1, timeout=2).scan(b"data")
