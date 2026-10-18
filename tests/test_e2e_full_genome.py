from tests.e2e_base import E2EBase


class TestE2EFull(E2EBase):
    """Whole-genome E2E run (region filters removed). Slow and rigorous."""

    REGION = None
    MODE = "FULL GENOME"
    __test__ = True
