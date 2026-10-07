# wireview-monitor

Thermal Grizzly WireView Pro II의 측정값(핀별 전압·전류, 온도, 폴트)을 웹 대시보드, REST/WebSocket API, CLI로 제공하는 Linux 데몬 `wvd`입니다.

```
/dev/ttyACM0 ── wvd (10 Hz 폴링) ──┬─ 웹 대시보드         http://<호스트>:8765/
                                   ├─ REST + WS/SSE       /api/v1/…  (OpenAPI 문서: /docs)
                                   ├─ wvctl / wvd.client  셸, CI, pytest
                                   └─ /metrics            Prometheus
```

## 빠른 시작

```bash
scripts/up.sh                     # 켜기: 처음 실행 때 .venv를 만들고, 기기 연결을 확인한 뒤 접속 주소를 출력
scripts/up.sh --simulate load     # 기기 없이 시뮬레이터로 켜기 (wvd 옵션은 그대로 전달됨)
WVD_TOKEN=secret scripts/up.sh    # 환경변수도 적용됨
scripts/down.sh                   # 끄기
```

- 대시보드도 wvd가 서빙하므로 프로세스는 하나입니다.
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
| `GET /api/v1/health` | 연결 상태, 마지막 샘플 이후 경과 시간, 실측 Hz, 읽기 카운터 (인증 불필요) |
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

### 샘플 형식

REST, WS/SSE, `wvctl --json` 모두 같은 형식입니다.

```json
{"ts": 1791353794.1, "seq": 75, "device": "7D005E001150455441313220",
 "pins": [{"v": 12.281, "a": 0.216, "w": 2.652}, …],
 "total_w": 17.01, "total_a": 1.385, "avg_v": 12.285, "vdd_v": 3.456,
 "temps_c": {"in": 28.8, "out": 29.4, "ext1": 30.2, "ext2": 30.2},
 "fan_pct": 0, "psu_cap_w": 600, "fault_status": 0, "fault_log": 0, "faults": [],
 "derived": {"max_pin_a": 0.256, "pin_imbalance": 1.11, "max_temp_c": 30.2}}
```

- `seq`: 샘플마다 1씩 증가하는 번호입니다. 빠진 샘플을 찾을 때 씁니다.
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

서비스는 별도 사용자(`DynamicUser`)와 `dialout` 그룹으로 실행되고, 데이터는 `/var/lib/wvd`에 저장됩니다. Arch 계열은 서비스 파일의 `SupplementaryGroups`를 `uucp`로 바꾸세요. `scripts/up.sh`로 띄운 서버와 동시에 실행할 수 없습니다.

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
│   ├── sampler.py      샘플링 스레드 (재연결, 명령 큐)
│   ├── samples.py      샘플 형식, 통계, 다운샘플링
│   ├── store.py        메모리 버퍼, SQLite, 이벤트, 세션 저장
│   ├── events.py       폴트·경고 이벤트 생성
│   ├── api.py          REST, WebSocket/SSE, /metrics, 인증
│   ├── daemon.py       wvd 진입점 (옵션 처리)
│   ├── client.py       Python 클라이언트
│   ├── cli.py          wvctl
│   ├── testing.py      pytest fixture
│   └── static/         웹 대시보드
├── tests/
├── scripts/            up.sh, down.sh
├── packaging/          systemd 서비스, udev 규칙
├── docs/design.html    설계 문서
└── pyproject.toml
```

## 테스트

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest                                                   # 시뮬레이터로 전체 실행
WVD_URL=http://127.0.0.1:8765 .venv/bin/pytest tests/test_cli.py   # 실행 중인 데몬(실제 기기)으로 실행
```
