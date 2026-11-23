# 중복 파일 관리 (v0.1)

`중복파일관리_요구사항명세.md`의 추천 기본값을 구현한 Windows 데스크톱 프로그램.

## 실행

```powershell
python -m pip install --user blake3 pillow
python -m dupfinder
```

`실행.bat`을 더블클릭해도 됩니다. 필요 환경: Python 3.12 이상(tkinter 포함).

## 테스트

```powershell
python -m unittest discover -s tests -v
```

## 구성

| 파일 | 역할 | 관련 요구사항 |
|---|---|---|
| `dupfinder/config.py` | 설정·한도 기본값 | R0~R1, R5 한도값 |
| `dupfinder/engine.py` | 검사 엔진(탐색·해시·정지 조건) | R2, R5-A~G |
| `dupfinder/hashing.py` | BLAKE3 해시, 진행 없음 감지 | R2-2·3, R5-C1·C3 |
| `dupfinder/actions.py` | 자동 선택, 휴지통·격리 이동, 무시 목록 | R4, R5-H |
| `dupfinder/store.py` | 해시 캐시, 무시 목록, 비정상 종료 표시 | R2-5, R5-F4 |
| `dupfinder/winutil.py` | 긴 경로, 절전 방지, 메모리, 휴지통 | R5-B4, R5-E4, R5-D4 |
| `dupfinder/report.py` | CSV 내보내기 | R3-5, R5-G4 |
| `dupfinder/gui.py` | 화면(가상 스크롤 표, 질문 창, 미리보기) | R3, R5-I |

프로그램 데이터 위치: `%LOCALAPPDATA%\DupFinder` (cache.db, ignore.json, logs\). 기본 격리 폴더: `%USERPROFILE%\DupFinder_격리`.

## 명세와 다르게 구현했거나 보충한 점

- **파일 읽기 제한 시간(R5-C1)**: 30초를 '전체 시간'이 아니라 '진행이 없는 시간'으로 적용. 큰 파일이 정상적으로 오래 걸려도 오판하지 않게.
- **오류 비율(R5-D3)**: 처리 100건 이상부터 20% 기준 적용. 처음 몇 건에서 오판하지 않게. '접근 거부'는 권한 문제라 미처리 목록에는 오류로 남기되 중단 판단에는 넣지 않음.
- **드라이브 최상위 시스템 폴더**: 모든 드라이브의 `$Recycle.Bin`, `System Volume Information`, `found.000` 같은 디스크 검사 복구 폴더는 항상 제외.
- **휴지통(R4-3)**: 고정 디스크가 아닌 드라이브(USB·네트워크)나 260자 이상 경로는 거부. Windows가 이런 경우 휴지통 대신 영구 삭제하기 때문.
- **기본 제외 경로(R1-3)**: `C:\$Recycle.Bin`, `C:\System Volume Information` 추가.
- **응답 없는 작업 누적**: 응답 없는 작업이 32개를 넘으면 '한도 초과'로 중단(장치·네트워크 이상 신호).
