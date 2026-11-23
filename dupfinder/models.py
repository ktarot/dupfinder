"""검사 결과 데이터."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Reason(Enum):  # R5-G3
    COMPLETED = "완료"
    CANCELLED = "사용자 취소"
    LIMIT = "한도 초과"
    ERROR = "오류"
    ENVIRONMENT = "환경 변화"


class State(Enum):
    PROCESSED = "처리"
    SKIPPED = "건너뜀"
    FAILED = "실패"


@dataclass(eq=False)
class FileEntry:
    path: str
    size: int
    mtime_ns: int
    ctime: float
    root: int
    is_base: bool = False
    fid: tuple | None = None
    partial: bytes | None = None
    full: bytes | None = None
    state: State | None = None


@dataclass
class Issue:
    path: str
    kind: str      # 파일 | 폴더 | 링크
    phase: str
    reason: str
    failed: bool = False  # True=오류, False=의도적으로 건너뜀


@dataclass
class DupGroup:
    size: int
    hash_hex: str
    files: list[FileEntry]

    @property
    def reclaimable(self) -> int:
        return self.size * (len(self.files) - 1)


@dataclass
class ScanResult:
    groups: list[DupGroup]
    issues: list[Issue]
    reason: Reason
    detail: str
    complete: bool
    counts: dict
    hardlinks: list[tuple[str, str]] = field(default_factory=list)
    elapsed: float = 0.0
    mode: str = "all"
