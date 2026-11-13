import pytest

from app.alerting.rule_checker import _evaluate


@pytest.mark.parametrize("raw,cond,thr,expected", [
    # Bool signal against a numeric threshold: true = 1, false = 0
    (True, ">", "0", True),
    (False, ">", "0", False),
    (True, "=", "1", True),
    (False, "=", "0", True),
    # Bool signal against true/false, with every condition
    (True, "=", "true", True),
    (False, "=", "true", False),
    (True, "<>", "false", True),
    (False, "<>", "false", False),
    (True, ">", "false", True),
    (False, ">", "false", False),
    (False, "<", "true", True),
    (True, "<", "true", False),
    # Numbers and strings against true/false
    (5, "=", "true", True),
    (0, "=", "false", True),
    ("TRUE", "=", "true", True),
    ("true", ">", "0", True),
    ("false", ">", "0", False),
    # Plain numeric comparisons
    (4.95, ">", "4.9", True),
    (4.9, ">", "4.9", False),
    (3, "<", "4.9", True),
    (2, "<>", "2", False),
    # Unusable values never fire
    ("n/a", ">", "0", False),
    (1, ">", "abc", False),
])
def test_evaluate(raw, cond, thr, expected):
    assert _evaluate(raw, cond, thr) is expected
