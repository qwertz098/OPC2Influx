"""Unit tests for the should_write() decision function."""
from app.opcua.poller import should_write


# --- always mode ---
def test_always_writes_same_value():
    assert should_write(10.0, 10.0, "always", 0) is True

def test_always_writes_none_old():
    assert should_write(5.0, None, "always", 0) is True


# --- first value (old_val is None) ---
def test_first_value_always_written_regardless_of_mode():
    for mode in ["on_change", "on_change_absolute", "on_change_relative"]:
        assert should_write(42.0, None, mode, 0.0) is True, f"failed for mode={mode}"


# --- on_change mode ---
def test_on_change_writes_when_value_changed():
    assert should_write(11.0, 10.0, "on_change", 0) is True

def test_on_change_suppresses_same_value():
    assert should_write(10.0, 10.0, "on_change", 0) is False

def test_on_change_works_with_strings():
    assert should_write("new", "old", "on_change", 0) is True
    assert should_write("same", "same", "on_change", 0) is False


# --- on_change_absolute mode ---
def test_absolute_below_threshold_suppressed():
    assert should_write(10.1, 10.0, "on_change_absolute", 0.5) is False

def test_absolute_at_threshold_suppressed():
    assert should_write(10.5, 10.0, "on_change_absolute", 0.5) is False

def test_absolute_above_threshold_written():
    assert should_write(10.6, 10.0, "on_change_absolute", 0.5) is True

def test_absolute_negative_delta():
    assert should_write(9.4, 10.0, "on_change_absolute", 0.5) is True

def test_absolute_zero_threshold_behaves_like_on_change():
    assert should_write(10.0001, 10.0, "on_change_absolute", 0.0) is True
    assert should_write(10.0, 10.0, "on_change_absolute", 0.0) is False


# --- on_change_relative mode ---
def test_relative_below_threshold_suppressed():
    # 3% change, threshold 5%
    assert should_write(103.0, 100.0, "on_change_relative", 5.0) is False

def test_relative_above_threshold_written():
    # 6% change, threshold 5%
    assert should_write(106.0, 100.0, "on_change_relative", 5.0) is True

def test_relative_zero_base_no_division_error():
    # old_val = 0 → base = 1.0 (safety fallback)
    assert should_write(0.1, 0.0, "on_change_relative", 5.0) is True

def test_relative_negative_old_value():
    # |−100| → 6% change above 5% threshold
    assert should_write(-106.0, -100.0, "on_change_relative", 5.0) is True

def test_relative_same_value_suppressed():
    assert should_write(100.0, 100.0, "on_change_relative", 1.0) is False


# --- type coercion edge cases ---
def test_non_numeric_falls_back_to_equality():
    assert should_write("a", "b", "on_change_absolute", 0.5) is True
    assert should_write("a", "a", "on_change_absolute", 0.5) is False
