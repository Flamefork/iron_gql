from pathlib import Path

import pytest

from tests.scan_real_code import SITE_PACKAGES
from tests.scan_real_code import assert_real_code_package

PACKAGES = [
    SITE_PACKAGES / "pydantic",
    SITE_PACKAGES / "graphql",
    SITE_PACKAGES / "httpx2",
    SITE_PACKAGES / "hypothesis",
]


@pytest.mark.parametrize("package", PACKAGES, ids=[path.name for path in PACKAGES])
def test_real_code_is_walked_without_claiming_anything(package: Path):
    assert_real_code_package(package)
