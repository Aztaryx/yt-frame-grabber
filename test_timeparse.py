"""Run with:  python test_timeparse.py   (or pytest)"""
from timeparse import TimeParseError, parse_range, parse_time


def test_times():
    assert parse_time("87") == 87
    assert parse_time("87.5") == 87.5
    assert parse_time("87s") == 87
    assert parse_time("1:27") == 87
    assert parse_time("1:27.250") == 87.25
    assert parse_time("0:01:27.250") == 87.25
    assert parse_time("1:02:03") == 3723


def test_ranges():
    assert parse_range("87-88") == (87, 88)
    assert parse_range("1:27-1:28") == (87, 88)
    assert parse_range("87.123 - 88.456") == (87.123, 88.456)
    assert parse_range("1:27.250 to 1:28") == (87.25, 88)
    assert parse_range("87–88") == (87, 88)


def test_errors():
    for bad in ("", "87", "88-87", "87-", "abc-def", "1:99-2:00", "1:2:3:4-5"):
        try:
            parse_range(bad)
        except TimeParseError:
            continue
        raise AssertionError(f"{bad!r} should have failed")


if __name__ == "__main__":
    test_times(); test_ranges(); test_errors()
    print("ok")
