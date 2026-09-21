from credit_extract.compare import mcnemar_exact


def test_mcnemar_exact():
    assert mcnemar_exact(0, 0) is None
    assert mcnemar_exact(3, 3) == 1.0
    assert mcnemar_exact(10, 0) < 0.01  # one side right on 10 rows the other gets wrong: not chance
    assert 0.2 < mcnemar_exact(3, 1) < 1.0
