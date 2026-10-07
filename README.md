# wireview-monitor

Thermal Grizzly WireView Pro II의 측정값(핀별 전압·전류, 온도, 폴트)을 웹 대시보드, REST/WebSocket API, CLI로 제공하는 Linux 데몬 `wvd`입니다.

```
/dev/ttyACM0 ── wvd (10 Hz 폴링) ──┬─ 웹 대시보드         http://<호스트>:8765/
                                   ├─ REST + WS/SSE       /api/v1/…  (OpenAPI 문서: /docs)
                                   ├─ wvctl / wvd.client  셸, CI, pytest
                                   └─ /metrics            Prometheus
```

`wvd`는 내부적으로 프로세스 3개로 동작합니다(1.2부터). 어느 한쪽에 부하가 걸려도 다른 쪽을 늦추지 못하게 하기 위해서입니다.

| 프로세스 | 하는 일 |
|---|---|
| recorder | 기기 읽기, DB 기록, 폴트·경고 판정, 실시간 피드 배포 |
| front | 8765 포트: 대시보드, WS/SSE, `latest`, `health`, `/metrics`. 나머지 `/api/*` 요청은 api로 전달 |
| api | history, stats, export, 세션 (무거운 조회 전담) |

`wvd` 명령 자체는 이 세 프로세스를 띄우고 감시만 합니다. 자식이 죽으면 1초 뒤 다시 띄우고, 60초 안에 5번 넘게 죽으면 종료 코드 70으로 끝납니다. `wvd`가 끝나면 자식도 함께 끝납니다. 프로세스 간 소켓은 `$RUNTIME_DIRECTORY`(systemd) 또는 `$XDG_RUNTIME_DIR` 아래 비공개 디렉터리에 만듭니다.

## 버전 기록

현재 버전은 **1.2.0**입니다. 버전별 배경, 결정 사항, 시험 결과는 아래 상세 문서에 있습니다. 변경 목록은 [CHANGELOG.md](CHANGELOG.md), 처음 설계는 [docs/design.html](docs/design.html)을 보세요.

| 버전 | 주요 변화 | 부하 중 최대 수집 공백¹ | 상세 문서 |
|---|---|---|---|
| **1.2.0** | 프로세스 3개로 분리(recorder · api · front)하고 `wvd`가 감시. 자식이 죽으면 자동 재시작. 재시작해도 seq가 겹치지 않음. health에 프로세스 상태 추가 | **20 ms** (수집 주기 그대로, 누락 0) | [docs/v1.2.html](docs/v1.2.html) |
| 1.1.0 | USB 재연결 시 수집 스레드가 죽던 문제(C1) 수정. DB 기록을 별도 스레드로 분리해 수집이 잠금을 기다리지 않음. 샘플에 `gap_s` 추가. health 확장. export 스트리밍. 무거운 조회는 동시 2개로 제한 | 0.38 s (GIL 경합이 남음) | [docs/v1.1.html](docs/v1.1.html) |
| 1.0.0 | 첫 릴리스: 단일 프로세스 데몬, 대시보드, REST/WS/SSE, CLI, systemd 배포 | 26.5 s (조회 중 수집 정지) | [docs/v1.0.html](docs/v1.0.html) |

¹ 50 Hz에서 1시간 구간 history·stats·export, 대시보드 세션 갱신, SSE 5개를 동시에 건 부하 시험(`tests/wvd_stress.py`) 결과입니다. 1.0에서 드러난 문제(C1–C4, E1–E7)는 [docs/v1.0.html](docs/v1.0.html)에 정리돼 있습니다.

## 빠른 시작

```bash
scripts/up.sh                     # 켜기: 처음 실행 때 .venv를 만들고, 기기 연결을 확인한 뒤 접속 주소를 출력
scripts/up.sh --simulate load     # 기기 없이 시뮬레이터로 켜기 (wvd 옵션은 그대로 전달됨)
WVD_TOKEN=secret scripts/up.sh    # 환경변수도 적용됨
scripts/down.sh                   # 끄기
```

- 대시보드도 wvd가 서빙합니다. 실행하는 명령은 `wvd` 하나입니다(내부 프로세스 3개는 자동 관리).
- 브라우저에서 http://127.0.0.1:8765/ 를 엽니다. 다른 PC에서는 `http://<이 PC의 IP>:8765/`로 접속합니다.
- PID 파일과 로그는 `run/`에 저장됩니다(`run/wvd.log`).
- `up.sh`로 띄운 서버는 터미널을 닫아도 계속 실행됩니다. 재부팅 후에도 자동으로 켜려면 [서비스로 실행](#서비스로-실행)을 참고하세요.

스크립트 없이 직접 실행하려면:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[server]"
.venv/bin/wvd                       # 실제 기기 (자동 탐지)
.venv/bin/wvd --simulate load       # 시뮬레이터: idle | load | imbalance | fault
```

### 준비

- **시리얼 권한**: udev 규칙을 설치합니다.
  ```bash
  sudo cp packaging/70-wireview.rules /etc/udev/rules.d/
  sudo udevadm control --reload-rules && sudo udevadm trigger
  ```
  로컬에 로그인한 사용자는 바로 접근할 수 있습니다. SSH로 접속해 쓰는 경우에는 `sudo usermod -aG dialout $USER` 후 다시 로그인하세요. Arch 계열은 `dialout` 대신 `uucp` 그룹을 씁니다(규칙 파일 주석 참고).
- **포트를 쓰는 다른 프로그램 종료**: 공식 GUI나 `wireviewd`처럼 같은 포트를 여는 프로그램은 먼저 끄세요. 이 프로토콜에는 프레이밍과 CRC가 없어서 두 프로그램이 동시에 쓰면 응답이 섞입니다. 그래서 wvd는 포트를 배타적으로 열고(`TIOCEXCL`), wvd가 실행 중일 때 다른 프로그램이 포트를 열면 `EBUSY`로 실패합니다.

## 옵션

| 옵션 | 환경변수 | 기본값 | 설명 |
|---|---|---|---|
| `--host` | `WVD_HOST` | `0.0.0.0` | 바인드 주소 (모든 인터페이스) |
| `--port` | `WVD_PORT` | `8765` | HTTP 포트 |
| `--device` | `WVD_DEVICE` | 자동 탐지 | 시리얼 포트 (예: `/dev/ttyACM0`, `/dev/wireview-pro2`) |
| `--simulate` | `WVD_SIMULATE` | 없음 | 가상 기기: `idle`, `load`, `imbalance`, `fault` |
| `--rate` | `WVD_RATE` | `10` | 초당 샘플 수 (1–50) |
| `--db` | `WVD_DB` | `~/.local/share/wvd/wvd.db` | SQLite 경로 (`:memory:`면 디스크에 저장하지 않음) |
| `--retention` | `WVD_RETENTION` | `72h` | 원본 샘플 보관 기간 (세션 구간은 계속 보관) |
| `--token` | `WVD_TOKEN` | 없음 | 설정하면 Bearer 토큰 인증 필요 |
| `--allow-write` | `WVD_ALLOW_WRITE=1` | 꺼짐 | 기기 쓰기 명령(폴트 해제) 허용 |
| `--log-level` | `WVD_LOG_LEVEL` | `info` | 로그 수준 |
| `--limit-pin-a` | 없음 | `9.5` | 핀당 전류 경고 기준 (A) |
| `--limit-total-w` | 없음 | `600` | 총 전력 경고 기준 (W) |
| `--limit-temp-c` | 없음 | `80` | 온도 경고 기준 (°C) |
| `--limit-imbalance` | 없음 | `1.5` | 핀 불균형 경고 기준 |

경고 기준에 `none`을 주면 해당 경고가 꺼집니다. 이 기준은 대시보드 경고, 이벤트 기록, `wvctl assert --daemon-limits`에 똑같이 적용됩니다.

## 대시보드

- **구성**: 연결 상태와 폴트 배지, KPI 5개(총 전력, 총 전류, 평균 전압, 최고 온도, 핀 불균형), 핀별 전류 막대와 V/A/W 표, 차트 4개(총 전력, 핀별 전류, 핀별 전압, 온도), 이벤트, 세션, 기기 정보
- **화면 배치**: 폭 1500px 이상에서는 모든 요소가 한 화면에 들어오도록 타일 형태로 배치되고, 차트 높이가 화면 높이에 맞춰집니다. 그보다 좁으면 한 열로 쌓입니다.
- **기능**: 차트 범위 선택(1분, 5분, 15분, 1시간), 일시정지, 세션 시작·정지, 세션별 CSV 다운로드, 라이트·다크 테마
- **재연결**: 연결이 끊기면 자동으로 다시 연결하고, 끊긴 동안의 샘플을 받아 채웁니다.

## 데이터 가져오기

### CLI (`wvctl`)

```bash
wvctl now                                   # 최신 샘플 (--json: JSON 출력)
wvctl watch                                 # 실시간 보기 (--json: 한 줄에 JSON 하나)
wvctl stats --last 5m                       # 필드별 min / avg / p95 / max, 에너지, 폴트
wvctl log --duration 60s -o run.csv         # 지정한 시간만큼 기록해 파일로 저장
wvctl session start "gpu-stress"  …  wvctl session stop
wvctl assert --duration 30s --max-total-w 600 --max-pin-a 9.5 --max-temp 80 --no-faults
echo $?                                     # 0 통과, 1 위반, 2 오류
wvctl events --type fault
wvctl --url http://host:8765 --token … now  # 원격 데몬 (WVD_URL, WVD_TOKEN 환경변수도 가능)
```

venv를 activate하지 않아도 됩니다.

```bash
.venv/bin/wvctl now                               # 경로로 실행
python3 -m wvd.cli now                            # 이 디렉터리에서 시스템 Python으로 실행 (venv 불필요)
ln -s "$PWD/.venv/bin/wvctl" ~/.local/bin/wvctl   # PATH에 연결해 어디서든 `wvctl`
```

- `wvctl`과 `wvd.client`는 표준 라이브러리만 사용합니다. venv는 서버(`wvd`)를 실행할 때만 필요합니다.
- `wvctl`의 소스는 `wvd/cli.py`입니다. `pip install -e`로 설치했다면 소스를 고친 내용이 재설치 없이 반영됩니다.
- 값만 필요하면 `curl`로도 충분합니다(`curl -s :8765/api/v1/sensors/latest`). `wvctl`은 여기에 임계값 판정과 종료 코드(`assert`), 세션 관리, 스트림 출력, 표 형식 출력을 더합니다.

### Python / pytest

```python
from wvd.client import WireView

wv = WireView("http://127.0.0.1:8765")
with wv.session("gpu-stress-01", meta={"dut": "RTX"}) as s:
    run_load(60)
st = s.stats()
assert st["fields"]["total_w"]["max"] < 600
assert st["fields"]["max_pin_a"]["max"] < 9.5
assert not st["faults_seen"]

for kind, data in wv.stream(hz=1):    # 실시간 구독 (SSE)
    if kind == "sample":
        print(data["seq"], data["total_w"])
```

`wvd/client.py`는 표준 라이브러리만 쓰므로, 이 파일만 복사해서 다른 PC에서 쓸 수도 있습니다.

패키지를 설치하면 pytest fixture `wireview`가 등록됩니다. `WVD_URL`을 지정하면 그 데몬(실제 기기)에 연결하고, 지정하지 않으면 테스트 중에만 시뮬레이터 데몬을 띄웁니다. 같은 테스트를 기기 없이 CI에서도 돌릴 수 있습니다.

```python
def test_power_budget(wireview):
    with wireview.session("budget") as s:
        run_load(30)
    assert s.stats()["fields"]["total_w"]["max"] < 450
```

### REST API

| 엔드포인트 | 내용 |
|---|---|
| `GET /api/v1/health` | 연결 상태, 수집·기록 스레드 생존 여부, 마지막 샘플 이후 경과 시간, 실측 Hz, 수집 공백, 읽기 카운터 (인증 불필요) |
| `GET /api/v1/info` | UID, 에디션, 펌웨어 빌드 |
| `GET /api/v1/limits` | 경고 기준, 폴트 비트 정의 |
| `GET /api/v1/sensors/latest` | 최신 샘플 |
| `GET /api/v1/sensors/history?last=5m&step=1s` | 구간 조회 (`from`/`to`/`after_seq`도 가능, `step`을 주면 구간 평균) |
| `GET /api/v1/sensors/stats?last=60s` 또는 `?session=ID` | 구간 통계 |
| `WS /api/v1/stream?hz=5`, `GET /api/v1/stream/sse` | 샘플과 이벤트 실시간 수신 |
| `GET /api/v1/events?type=fault` | 폴트, 경고 기준 초과, 연결 이벤트 |
| `POST /api/v1/sessions` `{label, meta}`, `POST /api/v1/sessions/ID/stop` | 세션 시작, 종료 |
| `GET /api/v1/sessions/ID/export?format=csv\|jsonl`, `GET /api/v1/export?last=10m` | 원본 데이터 내보내기 |
| `GET /metrics` | Prometheus 형식 |
| `POST /api/v1/device/clear-faults` `{fault?: "OCP"}` | 폴트 해제 (`--allow-write`일 때만) |

시각은 epoch 초 또는 ISO 8601로, 기간은 `500ms`, `30s`, `5m`, `1h`처럼 씁니다. 전체 스펙은 `/docs`에서 볼 수 있습니다.

- **무거운 조회 제한**: 링 버퍼를 넘는 history, stats, 세션 통계, export는 동시에 2개까지만 실행됩니다. 나머지는 최대 30초 기다린 뒤 `503`(`Retry-After: 5`)을 받습니다. 요청이 몰려도 수집이 받는 영향을 일정하게 묶어 두기 위한 것입니다.
- **history 잘림 표시**: 한 번에 50만 샘플(50 Hz에서 약 2.8시간)을 넘으면 앞부분만 반환하고 `"truncated": true`를 붙입니다.
- **export**: 상한 없이 스트리밍합니다. 서버가 바빠서 중간에 끊기면 마지막 줄에 `# export incomplete…`(CSV) 또는 `{"error": …}`(JSONL)를 남깁니다.
- **세션 통계**: 세션이 끝날 때 한 번 계산해 저장하고, 목록(`GET /api/v1/sessions`)에도 함께 담깁니다. 진행 중인 세션의 통계는 상세 조회에서만 계산합니다.

### health

```json
{"status": "ok", "connected": true, "sampler_alive": true, "writer_alive": true, "fatal": null,
 "rate_hz": 50.0, "measured_hz": 50.0, "last_sample_age_s": 0.01, "last_sample_wall": 1791357044.17,
 "gaps_total": 3, "max_gap_s_5m": 0.12, "last_gap": {"ts": 1791357001.2, "gap_s": 0.12, "seq": 61200},
 "db_queue": 12, "db_dropped": 0, "stream_dropped": 0, "counters": {"ok": 606124, "loop_errors": 0, …}, …}
```

- `status`: `ok`, `degraded`(기기 연결 끊김, 샘플이 3초 넘게 없음, 또는 api 프로세스 응답 없음), `down`(recorder가 없거나 재시작 중, 또는 수집·DB 기록 스레드가 죽음)
- `processes`: 프로세스별 `pid`, `restarts`, `last_exit`. `api_ok`는 api 프로세스 응답 여부, `recorder_status_age_s`는 recorder의 마지막 상태 보고 이후 시간입니다.
- `gaps_total`, `max_gap_s_5m`, `last_gap`: 샘플 간격이 주기의 2배를 넘은 횟수, 최근 5분 최대 간격, 마지막 공백. 0.5초 이상이면 `sampler.gap` 이벤트도 기록됩니다.
- `db_queue`, `db_dropped`: DB 기록 대기 샘플 수, DB에 기록하지 못한 샘플 수(디스크가 5분 넘게 막힌 경우)
- `stream_dropped`: 느린 스트림 구독자에게 보내지 못하고 버린 메시지 수. `feed_dropped`는 recorder가 내부 구독자(front, api)에게 보내지 못한 수입니다.

### 실시간 스트림의 지연 처리

구독자마다 약 5초분의 큐가 있습니다. 클라이언트가 따라오지 못해 큐가 넘치면, 밀린 메시지를 모두 버리고 `lag` 메시지(`{"ts": …, "dropped": N}`)를 보낸 뒤 최신 샘플부터 다시 보냅니다. 오래된 샘플을 최신 값처럼 받는 일을 막기 위한 것입니다. 수신 측은 `now - sample.ts`로 지연을 확인하는 것이 안전합니다.

### 샘플 형식

REST, WS/SSE, `wvctl --json` 모두 같은 형식입니다.

```json
{"ts": 1791353794.1, "seq": 75, "gap_s": 0.02, "device": "7D005E001150455441313220",
 "pins": [{"v": 12.281, "a": 0.216, "w": 2.652}, …],
 "total_w": 17.01, "total_a": 1.385, "avg_v": 12.285, "vdd_v": 3.456,
 "temps_c": {"in": 28.8, "out": 29.4, "ext1": 30.2, "ext2": 30.2},
 "fan_pct": 0, "psu_cap_w": 600, "fault_status": 0, "fault_log": 0, "faults": [],
 "derived": {"max_pin_a": 0.256, "pin_imbalance": 1.11, "max_temp_c": 30.2}}
```

- `seq`: 읽기에 성공한 샘플마다 1씩 증가하는 번호입니다. 수집이 멈춘 구간에는 샘플이 없을 뿐 번호가 비지 않으므로, 공백은 `gap_s`나 `ts` 간격으로 확인하세요.
- `gap_s`: 직전 샘플과의 간격(초)입니다. 정상이면 수집 주기와 같습니다(50 Hz에서 0.02). 데몬 시작 후 첫 샘플은 `null`이고, 1.0에서 기록된 샘플도 `null`입니다. CSV에서는 마지막 열입니다.
- `pin_imbalance`: 가장 큰 핀 전류 ÷ 핀 평균 전류입니다(1.0이면 완전 균형). 총 전류가 1 A 미만이면 `null`입니다.
- 연결되지 않은 온도 센서의 값은 `null`입니다.

## 서비스로 실행

`packaging/wvd.service`로 wvd를 systemd 서비스로 등록하면 부팅 때 자동으로 시작되고, 비정상 종료 시 재시작됩니다. 로그는 `journalctl -u wvd`로 봅니다.

```bash
sudo python3 -m venv /opt/wvd
sudo /opt/wvd/bin/pip install "$PWD[server]"
sudo cp packaging/wvd.service /etc/systemd/system/
sudo systemctl enable --now wvd
```

서비스는 별도 사용자(`DynamicUser`)와 `dialout` 그룹으로 실행되고, 데이터는 `/var/lib/wvd`에, 프로세스 간 소켓은 `/run/wvd`에 저장됩니다.

- recorder의 수집 스레드나 DB 기록 스레드가 예외 처리로도 막지 못하고 끝나면, recorder가 종료 코드 70으로 끝나고 `wvd`가 1초 뒤 다시 띄웁니다. 멈춘 값을 계속 내보내는 것보다 안전하기 때문입니다. 재시작 구간은 `sampler.gap` 이벤트로 남습니다.
- 어느 프로세스든 60초 안에 5번 넘게 죽으면 `wvd` 전체가 종료 코드 70으로 끝나고, systemd가 2초 뒤 다시 시작합니다. Arch 계열은 서비스 파일의 `SupplementaryGroups`를 `uucp`로 바꾸세요. `scripts/up.sh`로 띄운 서버와 동시에 실행할 수 없습니다.

## 네트워크 보안

기본 설정에서는 모든 네트워크 인터페이스(`0.0.0.0`)로 접속을 받으므로, 같은 네트워크의 누구나 대시보드와 API로 데이터를 읽을 수 있습니다. 읽기만 가능하고 기기 설정은 바꿀 수 없습니다. 토큰 없이 실행하면 시작할 때 경고가 기록됩니다.

- `--token`을 지정하면 `/api/v1/health`를 제외한 모든 요청에 `Authorization: Bearer <토큰>` 또는 `?token=<토큰>`이 필요합니다. 대시보드는 처음 한 번 토큰을 입력받아 저장합니다.
- `--host 127.0.0.1`로 실행하면 이 PC에서만 접속할 수 있습니다.
- 토큰 없이 외부 접속을 허용한 상태에서는 `--allow-write`를 켤 수 없습니다(시작 거부).
- TLS는 지원하지 않습니다. 외부망에 공개하려면 리버스 프록시를 앞에 두세요.
- 다른 PC에서 접속되지 않으면 방화벽을 확인하세요(ufw: `sudo ufw allow 8765/tcp`).

## 프로토콜

실제 기기(펌웨어 `TG-WV-PRO2-FW_20260430_1838`)로 검증했습니다.

- 115200 8N1, raw 모드입니다. 입력 버퍼를 비우고 명령 1바이트를 보낸 뒤, 정해진 길이의 응답을 읽습니다.
- `0x01` vendor 데이터(3 B), `0x02` UID(12 B), `0x04` 센서 값(100 B, `<4hHBx` + `h2xII`×6 + `IIHBxHH`), `0x0D` 빌드 정보(68 B), `0x0E` 폴트 해제(u16 keep 마스크 2개)
- 프레임에 CRC가 없어서, 패딩 바이트가 0이 아니거나 팬 듀티가 100을 넘는 프레임은 손상된 것으로 보고 버립니다.
- 응답 시간은 약 0.2 ms이고, 기기는 약 16 ms마다 측정값을 갱신합니다.

자세한 구현은 `wvd/protocol.py`와 `wvd/transport.py`에 있습니다.

## 디렉터리 구조

```
├── wvd/
│   ├── protocol.py     명령, 프레임 구조, 디코딩
│   ├── transport.py    시리얼 포트(배타적 열기, 자동 탐지)와 시뮬레이터
│   ├── sampler.py      샘플링 스레드 (재연결, 명령 큐, 공백 측정)
│   ├── samples.py      샘플 형식, 통계, 다운샘플링
│   ├── store.py        메모리 버퍼, SQLite 기록 스레드, 이벤트, 세션 저장
│   ├── events.py       폴트·경고 이벤트 생성
│   ├── recorder.py     recorder 프로세스 (기기, DB 기록, 피드)
│   ├── front.py        front 프로세스 (포트, 대시보드, WS/SSE, health, 인증, api로 전달)
│   ├── api.py          api 프로세스 (history, stats, export, 세션)
│   ├── bus.py          프로세스 간 통신 (피드, 제어 채널)
│   ├── daemon.py       wvd 진입점 (옵션 처리, 프로세스 감시)
│   ├── client.py       Python 클라이언트
│   ├── cli.py          wvctl
│   ├── testing.py      pytest fixture
│   └── static/         웹 대시보드
├── tests/
├── scripts/            up.sh, down.sh
├── packaging/          systemd 서비스, udev 규칙
├── docs/              설계 문서(design.html), 버전별 기록(v1.0.html, v1.1.html, v1.2.html)
└── pyproject.toml
```

## 테스트

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest                                                   # 시뮬레이터로 전체 실행
WVD_URL=http://127.0.0.1:8765 .venv/bin/pytest tests/test_cli.py   # 실행 중인 데몬(실제 기기)으로 실행
```

부하 시험(`tests/wvd_stress.py`, pytest가 수집하지 않음)은 조회 부하를 거는 동안 수집 공백을 측정합니다. 판정 기준은 시험 구간에 저장된 샘플 사이의 최대 간격입니다.

```bash
.venv/bin/python tests/wvd_stress.py --duration 120                     # 시뮬레이터 + 3시간 분량 DB
.venv/bin/python tests/wvd_stress.py --workload dashboard                # 대시보드 부하만
.venv/bin/python tests/wvd_stress.py --url http://127.0.0.1:8765 --duration 600   # 실행 중인 데몬
```

시험용 장애 주입: `WVD_TEST_FAULT=read-termios:N`(N번째 읽기마다 EIO), `loop-error:N`, `kill:N`(N번째 읽기에서 수집 스레드 종료), `db-slow:S`(DB 기록마다 S초 지연).

결과에는 저장 샘플의 최대 간격과 함께, SSE로 받은 샘플의 전달 지연(수신 시각 − 샘플 시각)도 표시됩니다.
