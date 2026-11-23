"""CSV 내보내기(R3-5, R5-G4). Excel에서 한글이 깨지지 않게 UTF-8 BOM을 붙인다."""
from __future__ import annotations

import csv
import datetime as dt

from .models import DupGroup, Issue


def fmt_time(ts: float) -> str:
    try:
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def export_groups(path: str, groups: list[DupGroup], selected: set[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["묶음", "해시(BLAKE3)", "크기(바이트)", "처리 대상", "구분", "경로", "생성일", "수정일"])
        for i, g in enumerate(groups, 1):
            for e in g.files:
                w.writerow([i, g.hash_hex, g.size, "예" if e.path in selected else "",
                            "기준" if e.is_base else "", e.path, fmt_time(e.ctime), fmt_time(e.mtime_ns / 1e9)])


def export_issues(path: str, issues: list[Issue]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["경로", "종류", "단계", "구분", "사유"])
        for x in issues:
            w.writerow([x.path, x.kind, x.phase, "오류" if x.failed else "건너뜀", x.reason])
