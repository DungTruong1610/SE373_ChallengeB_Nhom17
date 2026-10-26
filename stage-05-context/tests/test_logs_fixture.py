"""Fixture log của Challenge B: sinh tất định, đáp án đúng không hòa ở top 3, vừa giới hạn read_file."""

import paths
from scripts.gen_logs import FILE_COUNT, expected_summary, generate, parse_line
from tools.files import MAX_READ_BYTES

FIXTURE_LOGS = paths.FIXTURES_DIR / "data" / "logs"


def test_generator_is_deterministic_and_matches_fixtures(tmp_path):
    written = generate(tmp_path)
    assert len(written) == FILE_COUNT
    for path in written:
        assert path.read_bytes() == (FIXTURE_LOGS / path.name).read_bytes()
        assert path.stat().st_size < MAX_READ_BYTES  # baseline native vẫn đọc được từng file bằng read_file


def test_expected_summary_has_unambiguous_answers():
    truth = expected_summary(FIXTURE_LOGS)
    assert truth["files"] == FILE_COUNT
    counts = [n for _, n in truth["top_error_codes"]]
    assert counts == sorted(set(counts), reverse=True)  # không hòa trong top 3
    assert truth["top_error_codes"][0][0] == "ERR_DB_TIMEOUT"
    # bẫy cố ý: mã lỗi nhiều nhất khác endpoint trả 500 nhiều nhất
    assert truth["top_500_endpoints"][0][0] == "/api/auth/refresh"
    endpoint_counts = [n for _, n in truth["top_500_endpoints"]]
    assert endpoint_counts == sorted(set(endpoint_counts), reverse=True)


def test_refresh_race_story_for_b1():
    lines = (FIXTURE_LOGS / "app-2026-10-15.log").read_text(encoding="utf-8").splitlines()
    assert "token_ttl_900s->1800s" in lines[0]
    records = [parse_line(line) for path in sorted(FIXTURE_LOGS.glob("*.log")) for line in path.read_text(encoding="utf-8").splitlines()]
    for i, record in enumerate(records):
        if record and record["code"] == "ERR_TOKEN_REFRESH_RACE":
            previous = records[i - 1]
            assert previous["code"] == "REFRESH_CONCURRENT" and previous["session"] == record["session"]
    by_day = expected_summary(FIXTURE_LOGS)["refresh_race_by_day"]
    before = sum(n for day, n in by_day.items() if day < "2026-10-15") / 4
    after = sum(n for day, n in by_day.items() if day >= "2026-10-15") / 6
    assert after >= before  # tăng TTL không làm giảm lỗi
