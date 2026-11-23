"""설정과 한도값. 요구사항 명세 v0.1의 추천 기본값을 그대로 담는다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_APP_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DupFinder"
)
DEFAULT_QUARANTINE_DIR = os.path.join(os.path.expanduser("~"), "DupFinder_격리")

# R1-3 기본 제외 경로 (+ 휴지통·시스템 볼륨 정보: 시스템 폴더)
DEFAULT_EXCLUDE_PATHS = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
]
DEFAULT_EXCLUDE_DIR_NAMES = [".git", "node_modules"]


@dataclass
class Limits:
    """5번 영역(종료·정지 조건)의 한도값."""

    max_depth: int = 64                      # R5-B2
    max_files: int = 10_000_000              # R5-B3
    max_path_len: int = 32_767               # R5-B4
    read_idle_timeout: float = 30.0          # R5-C1 (진행 없는 시간 기준)
    read_retries: int = 2                    # R5-C1
    lock_retry_delays: tuple = (1.0, 2.0, 4.0)  # R5-C2
    big_file_bytes: int = 50 * 1024 ** 3     # R5-C5
    time_limit: float | None = None          # R5-D1 (초, None=무제한)
    stall_seconds: float = 60.0              # R5-D2
    max_consecutive_errors: int = 100        # R5-D3
    max_error_ratio: float = 0.20            # R5-D3
    error_ratio_min_samples: int = 100       # R5-D3 (표본이 적을 때 오판 방지)
    memory_ratio: float = 0.50               # R5-D4
    memory_grace_seconds: float = 10.0       # R5-D4 (작업 수를 줄인 뒤 기다리는 시간)
    min_free_bytes: int = 1024 ** 3          # R5-D5
    network_retries: int = 3                 # R5-E2
    root_check_interval: float = 2.0         # R5-E1~E3 점검 주기
    root_check_timeout: float = 1.5          # 점검 1회 제한 시간(취소 2초 보장용)
    cancel_response: float = 2.0             # R5-F1
    ask_timeout: float = 60.0                # Ⓔ 무응답 시 기본 동작까지
    preview_timeout: float = 3.0             # R5-I3
    max_abandoned_tasks: int = 32            # 응답 없는 작업이 이만큼 쌓이면 중단


@dataclass
class ScanConfig:
    roots: list[str]                                   # 검사 폴더(비교 모드에서는 대상 폴더)
    base_roots: list[str] = field(default_factory=list)  # 비교 모드의 기준 폴더
    mode: str = "all"                                  # "all" | "compare"
    exclude_paths: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PATHS))
    exclude_dir_names: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIR_NAMES))
    min_size: int = 1024                               # R1-4
    max_size: int | None = None
    workers: int | None = None                         # None이면 R2-4 규칙으로 자동
    app_dir: str = DEFAULT_APP_DIR                     # 캐시·작업 기록·무시 목록
    quarantine_dir: str = DEFAULT_QUARANTINE_DIR
    use_cache: bool = True
    limits: Limits = field(default_factory=Limits)

    def all_roots(self) -> list[tuple[str, bool]]:
        """(경로, 기준 폴더 여부) 목록."""
        out = [(os.path.abspath(r), False) for r in self.roots]
        if self.mode == "compare":
            out += [(os.path.abspath(r), True) for r in self.base_roots]
        return out

    def own_dirs(self) -> list[str]:
        """프로그램이 스스로 만드는 폴더(R4-7, R5-B5)."""
        return [os.path.abspath(self.app_dir), os.path.abspath(self.quarantine_dir)]

    def validate(self) -> None:
        roots = self.all_roots()
        if not roots:
            raise ValueError("검사할 폴더를 하나 이상 고르세요.")
        if self.mode == "compare":
            if not self.roots or not self.base_roots:
                raise ValueError("비교 모드에서는 기준 폴더와 대상 폴더가 모두 필요합니다.")
            for t in self.roots:
                for b in self.base_roots:
                    if is_under(t, b) or is_under(b, t):
                        raise ValueError(f"기준 폴더와 대상 폴더가 겹칩니다: {b} / {t}")
        if self.min_size < 0 or (self.max_size is not None and self.max_size < self.min_size):
            raise ValueError("크기 범위가 올바르지 않습니다.")


def norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/")


def is_under(path: str, parent: str) -> bool:
    """path가 parent와 같거나 그 아래에 있는지."""
    p, q = norm(path), norm(parent)
    return p == q or p.startswith(q + os.sep)
