from tests.e2e_base import E2EBase


class TestE2EFocused(E2EBase):
    """Focused E2E run restricted to chrM; fast enough for routine verification."""

    REGION = "chrM"
    MODE = "CHRM"
    __test__ = True
