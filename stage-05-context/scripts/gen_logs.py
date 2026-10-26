"""Sinh bộ log giả TẤT ĐỊNH cho Challenge B và tính đáp án đúng (ground truth) từ chính các file log.

    python scripts/gen_logs.py            # ghi vào fixtures/data/logs/
    python scripts/gen_logs.py --check    # in ground truth của fixtures/data/logs/

Mỗi dòng: `<ts> <LEVEL> <service> <code> endpoint=<ep> status=<st> latency_ms=<ms> req=<id> [session=<sid>] [detail=<x>]`.
Câu chuyện cài sẵn cho B1: ERR_TOKEN_REFRESH_RACE ở /api/auth/refresh xảy ra khi hai refresh cùng session chạy
đồng thời (có dòng WARN REFRESH_CONCURRENT ngay trước). Ngày 2026-10-15 token TTL tăng 900s -> 1800s nhưng tỉ lệ lỗi
không giảm, tức "tăng TTL không giúp".
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

SEED = 373
FILE_COUNT = 10
LINES_PER_FILE = 150
FIRST_DAY = datetime(2026, 10, 11)
TTL_CHANGE_DAY = 4  # file thứ 5 (2026-10-15)

# (code, service, [(endpoint, weight)], status, weight)
ERRORS = [
    ("ERR_DB_TIMEOUT", "api-gateway", [("/api/orders", 7), ("/api/checkout", 3)], 500, 30),
    ("ERR_TOKEN_REFRESH_RACE", "auth-service", [("/api/auth/refresh", 1)], 500, 22),
    ("ERR_UPSTREAM_502", "payment-service", [("/api/payments", 1)], 502, 18),
    ("ERR_VALIDATION", "api-gateway", [("/api/cart", 1)], 400, 14),
    ("ERR_RATE_LIMIT", "auth-service", [("/api/auth/login", 1)], 429, 10),
    ("ERR_CACHE_STAMPEDE", "api-gateway", [("/api/products", 1)], 503, 6),
]
OK_ENDPOINTS = [
    ("api-gateway", "/api/orders"),
    ("api-gateway", "/api/products"),
    ("api-gateway", "/api/cart"),
    ("api-gateway", "/api/checkout"),
    ("auth-service", "/api/auth/login"),
    ("auth-service", "/api/auth/refresh"),
    ("payment-service", "/api/payments"),
]
LINE_RE = re.compile(r"^(\S+) (INFO|WARN|ERROR) (\S+) (\S+) (.*)$")


def _pick(rng: random.Random, pairs):
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights, k=1)[0]


def _line(ts: datetime, level: str, service: str, code: str, endpoint: str, status, latency: int, req: str, **extra) -> str:
    tail = "".join(f" {k}={v}" for k, v in extra.items())
    return f"{ts:%Y-%m-%dT%H:%M:%S}.{ts.microsecond // 1000:03d}Z {level} {service} {code} endpoint={endpoint} status={status} latency_ms={latency} req={req}{tail}"


def generate_file(rng: random.Random, day_index: int) -> list[str]:
    ts = FIRST_DAY + timedelta(days=day_index, hours=8)
    lines: list[str] = []
    if day_index == TTL_CHANGE_DAY:
        lines.append(_line(ts, "INFO", "auth-service", "CONFIG_CHANGE", "-", "-", 0, "cfg-ttl", detail="token_ttl_900s->1800s"))
    for _ in range(LINES_PER_FILE):
        ts += timedelta(seconds=rng.randint(5, 90), milliseconds=rng.randint(0, 999))
        req = f"{rng.getrandbits(32):08x}"
        roll = rng.random()
        if roll < 0.68:
            service, endpoint = rng.choice(OK_ENDPOINTS)
            lines.append(_line(ts, "INFO", service, "OK", endpoint, 200, rng.randint(8, 180), req))
        elif roll < 0.78:
            lines.append(_line(ts, "WARN", "api-gateway", "WARN_SLOW_QUERY", "/api/orders", 200, rng.randint(1200, 2900), req))
        else:
            code, service, endpoints, status, _ = _pick(rng, [(e, e[4]) for e in ERRORS])
            endpoint = _pick(rng, endpoints)
            if code == "ERR_TOKEN_REFRESH_RACE":
                session = f"s{rng.randint(100, 999)}"
                first = ts - timedelta(milliseconds=rng.randint(5, 40))
                lines.append(_line(first, "WARN", service, "REFRESH_CONCURRENT", endpoint, 200, rng.randint(20, 60), f"{rng.getrandbits(32):08x}", session=session))
                lines.append(_line(ts, "ERROR", service, code, endpoint, status, rng.randint(20, 60), req, session=session, detail="refresh_token_already_rotated"))
            else:
                lines.append(_line(ts, "ERROR", service, code, endpoint, status, rng.randint(900, 5000), req))
    return lines


def log_name(day_index: int) -> str:
    return f"app-{FIRST_DAY + timedelta(days=day_index):%Y-%m-%d}.log"


def generate(out_dir: Path) -> list[Path]:
    rng = random.Random(SEED)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for day in range(FILE_COUNT):
        path = out_dir / log_name(day)
        path.write_text("\n".join(generate_file(rng, day)) + "\n", encoding="utf-8", newline="\n")
        written.append(path)
    return written


def parse_line(line: str) -> dict | None:
    match = LINE_RE.match(line.strip())
    if not match:
        return None
    ts, level, service, code, rest = match.groups()
    fields = dict(part.split("=", 1) for part in rest.split() if "=" in part)
    return {"ts": ts, "level": level, "service": service, "code": code, **fields}


def expected_summary(log_dir: Path) -> dict:
    """Đáp án đúng, tính độc lập với generator: đọc và đếm trực tiếp từ các file log."""
    error_codes: Counter = Counter()
    endpoints_500: Counter = Counter()
    race_by_day: dict[str, int] = {}
    lines = 0
    files = sorted(log_dir.glob("*.log"))
    for path in files:
        day = path.stem.removeprefix("app-")
        race_by_day[day] = 0
        for raw in path.read_text(encoding="utf-8").splitlines():
            record = parse_line(raw)
            if record is None:
                continue
            lines += 1
            if record["level"] == "ERROR":
                error_codes[record["code"]] += 1
                if record["code"] == "ERR_TOKEN_REFRESH_RACE":
                    race_by_day[day] += 1
            if record.get("status") == "500":
                endpoints_500[record["endpoint"]] += 1
    return {
        "files": len(files),
        "lines": lines,
        "error_total": sum(error_codes.values()),
        "top_error_codes": error_codes.most_common(3),
        "top_500_endpoints": endpoints_500.most_common(3),
        "refresh_race_by_day": race_by_day,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="chỉ in ground truth, không ghi file")
    args = parser.parse_args()
    log_dir = Path(__file__).resolve().parent.parent / "fixtures" / "data" / "logs"
    if not args.check:
        for path in generate(log_dir):
            print(f"{path.name}: {path.stat().st_size} bytes")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(expected_summary(log_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
