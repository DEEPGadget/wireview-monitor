# wireview-monitor: WireView 측정 데몬 (`wvd`)

`wvd`는 WireView Pro II의 시리얼 포트를 혼자 소유하고, 읽은 측정값을 여러 곳에 동시에 제공합니다.

- 웹 대시보드
- REST / WebSocket / SSE API
- CLI (`wvctl`)
- 자동화 테스트용 Python 클라이언트와 pytest fixture

모니터링 용도로는 이 저장소의 .NET 앱을 대신하며, .NET 앱이 없어도 동작합니다. 같은 저장소에 들어 있지만 .NET 앱과는 별개 구현이고 코드를 공유하지 않습니다. 설계 문서는 [`docs/design.html`](docs/design.html)에 있습니다.

```
/dev/ttyACM0 ── wvd (단독 소유, 10 Hz) ──┬─ 웹 대시보드         http://<호스트>:8765/
                                         ├─ REST + WS/SSE       /api/v1/…  (OpenAPI 문서: /docs)
                                         ├─ wvctl / wvd.client  셸, CI, pytest
                                         └─ /metrics            Prometheus
```

## 빠른 시작

`scripts/`의 스크립트로 켜고 끕니다. 백엔드가 대시보드도 함께 서빙하므로 프로세스는 하나입니다.

```bash
cd wireview-monitor
scripts/up.sh                     # 켜기: 처음 실행 때 .venv를 만들고, 정상 동작을 확인한 뒤 접속 주소를 출력
scripts/up.sh --simulate load     # wvd 옵션을 그대로 넘길 수 있음 (기기 없이 시뮬레이터로 실행)
WVD_TOKEN=secret scripts/up.sh    # WVD_* 환경변수도 그대로 적용
scripts/down.sh                   # 끄기 (PID 파일이 없으면 8765 포트의 wvd를 찾아서 끔)
```

- PID 파일과 로그는 `run/`에 저장됩니다(`run/wvd.log`).
- 브라우저에서 http://127.0.0.1:8765/ 를 엽니다. 다른 PC에서는 `http://<이 PC의 IP>:8765/`로 접속합니다.
- `up.sh`는 `nohup`으로 띄우므로, 실행한 터미널이나 세션을 닫아도 서버는 계속 돕니다. 재부팅하면 꺼지니, 부팅 때 자동으로 켜려면 아래 [서비스로 실행](#서비스로-실행)을 참고하세요.

스크립트 없이 직접 실행할 수도 있습니다.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[server]"
.venv/bin/wvd                       # 실제 기기 (자동 탐지)
.venv/bin/wvd --simulate load       # 시뮬레이터: idle | load | imbalance | fault
```

### 실행 전 확인할 것

- **WireView GUI와 `wireviewd`를 먼저 끄세요.** 시리얼 프로토콜에는 프레이밍도 CRC도 없어서, 두 프로세스가 같은 포트를 쓰면 응답이 서로 섞입니다. 그래서 wvd는 포트를 배타적으로 엽니다(`TIOCEXCL`). wvd가 도는 동안 다른 프로그램이 포트를 열면 `EBUSY`로 거부됩니다.
- **시리얼 접근 권한**은 저장소의 udev 규칙(`udev/99-wireview.rules`)으로 설정합니다.

## 옵션

| 옵션 | 환경변수 | 기본값 | 설명 |
|---|---|---|---|
| `--host` | `WVD_HOST` | `0.0.0.0` | 바인드 주소 (모든 인터페이스) |
| `--port` | `WVD_PORT` | `8765` | HTTP 포트 |
| `--device` | `WVD_DEVICE` | 자동 탐지 | 시리얼 포트 (예: `/dev/ttyACM0`, `/dev/wireview-pro2`) |
| `--simulate` | `WVD_SIMULATE` | 없음 | 가상 기기: `idle`, `load`, `imbalance`, `fault` |
| `--rate` | `WVD_RATE` | `10` | 초당 샘플 수 (1–50) |
| `--db` | `WVD_DB` | `~/.local/share/wvd/wvd.db` | SQLite 경로 (`:memory:`면 디스크에 저장하지 않음) |
| `--retention` | `WVD_RETENTION` | `72h` | 원본 샘플 보관 기간. 세션 구간은 기간과 상관없이 보관 |
| `--token` | `WVD_TOKEN` | 없음 | 설정하면 Bearer 토큰 인증 필요 |
| `--allow-write` | `WVD_ALLOW_WRITE=1` | 꺼짐 | 기기 쓰기 명령(폴트 해제) 허용 |
| `--log-level` | `WVD_LOG_LEVEL` | `info` | 로그 수준 |
| `--limit-pin-a` | 없음 | `9.5` | 핀당 전류 경고 기준 (A) |
| `--limit-total-w` | 없음 | `600` | 총 전력 경고 기준 (W) |
| `--limit-temp-c` | 없음 | `80` | 온도 경고 기준 (°C) |
| `--limit-imbalance` | 없음 | `1.5` | 핀 불균형 경고 기준 |

임계값 옵션에 `none`을 주면 그 경고를 끕니다. 임계값은 대시보드 경고, 이벤트 기록, `wvctl assert --daemon-limits`에 모두 같은 기준으로 쓰입니다.

## 대시보드

브라우저에서 `/`를 열면 됩니다. 빌드 단계 없는 정적 페이지(`wvd/static/`)이고, wvd가 직접 서빙합니다.

- **구성**: 연결 상태와 폴트 배지, KPI 5개(총 전력, 총 전류, 평균 전압, 최고 온도, 핀 불균형), 6핀 전류 막대와 V/A/W 표, 차트 4개(총 전력, 핀별 전류, 핀별 전압, 온도), 이벤트, 세션, 기기 정보
- **넓은 화면(폭 1500px 이상)**: 왼쪽 열에 핀 패널과 이벤트, 오른쪽에 차트·세션·기기 정보 타일이 바둑판처럼 놓입니다. 16:9와 16:10 모니터에서 한 화면에 모두 들어오도록 차트 높이가 화면 높이에 맞춰집니다.
- **좁은 화면**: 한 열로 쌓이고 세로로 스크롤합니다.
- **기능**: 차트 범위(1분, 5분, 15분, 1시간) 선택, 일시정지, 세션 시작·정지와 CSV 다운로드, 라이트·다크 테마
- **연결이 끊겼을 때**: 자동으로 재연결하고, 끊긴 동안의 샘플을 받아 빈 구간 없이 이어 붙입니다.

## 데이터 가져오기

### CLI (`wvctl`)

```bash
wvctl now                                   # 최신 샘플 (기계용은 --json)
wvctl watch                                 # 실시간 보기 (--json이면 한 줄에 JSON 하나)
wvctl stats --last 5m                       # 필드별 min / avg / p95 / max, 에너지, 폴트
wvctl log --duration 60s -o run.csv         # 일정 시간 기록 후 파일로 저장
wvctl session start "gpu-stress"  …  wvctl session stop
wvctl assert --duration 30s --max-total-w 600 --max-pin-a 9.5 --max-temp 80 --no-faults
echo $?                                     # 0 통과, 1 위반, 2 오류
wvctl events --type fault
wvctl --url http://host:8765 --token … now  # 원격 데몬 (WVD_URL / WVD_TOKEN 환경변수도 가능)
```

**실행 방법.** venv를 activate할 필요는 없습니다. 다음 중 편한 방법을 쓰면 됩니다.

```bash
.venv/bin/wvctl now                                       # 경로를 직접 지정 (첫 줄에 venv Python 경로가 박혀 있음)
python3 -m wvd.cli now                                    # wireview-monitor/ 안에서, venv 없이 시스템 Python으로
ln -s "$PWD/.venv/bin/wvctl" ~/.local/bin/wvctl           # PATH에 걸어 두고 어디서든 `wvctl`
```

`wvctl`과 `wvd.client`는 표준 라이브러리만 쓰기 때문에 venv 없이도 돌아갑니다. venv가 꼭 필요한 것은 서버(`wvd`)뿐입니다(FastAPI, uvicorn).

**소스 위치.** `wvctl`의 소스는 `wvd/cli.py`입니다. `.venv/bin/wvctl`은 pip가 `pyproject.toml`의 `[project.scripts]`를 보고 만든 짧은 실행 파일로, `wvd.cli:entry`를 호출하는 일만 합니다. 편집 가능 모드(`pip install -e`)로 설치했기 때문에 venv에는 소스 복사본이 없고 소스 디렉터리를 가리키는 연결만 있습니다. 그래서 `wvd/cli.py`를 고치면 재설치 없이 바로 반영됩니다.

**curl과의 차이.** 값만 꺼낼 거라면 `curl`로도 충분합니다(`curl -s :8765/api/v1/sensors/latest`). `wvctl`은 그 위에 다음을 더해 줍니다.
- 임계값 판정과 종료 코드 (`assert`)
- 진행 중인 세션 자동 선택 (`session stop`)
- SSE 스트림을 JSON 한 줄씩으로 변환 (`watch --json`)
- 환경변수로 URL과 토큰 설정
- 사람이 읽기 좋은 표 출력

### Python / pytest

`wvd.client`는 표준 라이브러리만 씁니다. 다른 PC에 이 패키지만 복사해도 동작합니다.

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

패키지를 설치하면 pytest fixture `wireview`가 등록됩니다. `WVD_URL`이 있으면 그 데몬(실제 기기)을 쓰고, 없으면 테스트 동안 시뮬레이터 데몬을 띄웁니다. 그래서 같은 테스트를 기기 없이 CI에서도 돌릴 수 있습니다.

```python
def test_power_budget(wireview):
    with wireview.session("budget") as s:
        run_load(30)
    assert s.stats()["fields"]["total_w"]["max"] < 450
```

### REST API

| 엔드포인트 | 내용 |
|---|---|
| `GET /api/v1/health` | 연결 상태, 마지막 샘플 경과 시간, 실측 Hz, 읽기 카운터 (인증 불필요) |
| `GET /api/v1/info` | UID, 에디션, 펌웨어 빌드 |
| `GET /api/v1/limits` | 임계값과 폴트 비트 정의 |
| `GET /api/v1/sensors/latest` | 최신 샘플 |
| `GET /api/v1/sensors/history?last=5m&step=1s` | 구간 조회 (`from`/`to`/`after_seq`도 가능), `step`을 주면 구간 평균 |
| `GET /api/v1/sensors/stats?last=60s` 또는 `?session=ID` | 구간 통계 |
| `WS /api/v1/stream?hz=5`, `GET /api/v1/stream/sse` | 샘플과 이벤트 실시간 푸시 |
| `GET /api/v1/events?type=fault` | 폴트, 임계값, 연결 이벤트 |
| `POST /api/v1/sessions` `{label, meta}`, `POST /api/v1/sessions/ID/stop` | 테스트 구간 시작과 종료 |
| `GET /api/v1/sessions/ID/export?format=csv\|jsonl`, `GET /api/v1/export?last=10m` | 원본 데이터 내보내기 |
| `GET /metrics` | Prometheus 형식 |
| `POST /api/v1/device/clear-faults` `{fault?: "OCP"}` | 폴트 해제 (`--allow-write`일 때만) |

시각은 epoch 초 또는 ISO 8601로, 기간은 `500ms`, `30s`, `5m`, `1h`처럼 적습니다. 전체 스펙은 `/docs`(OpenAPI)에서 볼 수 있습니다.

### 샘플 형식

모든 경로(REST, WS/SSE, CLI `--json`)가 같은 형식을 씁니다.

```json
{"ts": 1791353794.1, "seq": 75, "device": "7D005E001150455441313220",
 "pins": [{"v": 12.281, "a": 0.216, "w": 2.652}, …],
 "total_w": 17.01, "total_a": 1.385, "avg_v": 12.285, "vdd_v": 3.456,
 "temps_c": {"in": 28.8, "out": 29.4, "ext1": 30.2, "ext2": 30.2},
 "fan_pct": 0, "psu_cap_w": 600, "fault_status": 0, "fault_log": 0, "faults": [],
 "derived": {"max_pin_a": 0.256, "pin_imbalance": 1.11, "max_temp_c": 30.2}}
```

- `seq`는 샘플마다 1씩 늘어나는 번호로, 빠진 구간을 찾는 기준입니다.
- `pin_imbalance`는 가장 높은 핀 전류를 핀 평균 전류로 나눈 값입니다(1.0이면 완전 균형). 총 전류가 1 A 미만이면 비율이 의미 없어서 `null`입니다.
- 연결되지 않은 온도 센서는 `null`입니다.

## 서비스로 실행

`packaging/wvd.service`는 wvd를 격리된 systemd 서비스로 실행합니다(`DynamicUser`, `dialout` 그룹, 데이터는 `/var/lib/wvd`). 부팅 때 자동으로 시작되고 오류로 종료되면 재시작되며, 로그는 `journalctl -u wvd`로 봅니다. 설치 절차는 파일 맨 위 주석에 있습니다. Arch 계열 배포판에서는 `dialout` 대신 `uucp` 그룹을 쓰세요.

## 네트워크 노출

wvd는 기본으로 `0.0.0.0`(모든 인터페이스)에서 접속을 받습니다. 따라서 다른 PC에서도 대시보드와 API를 열 수 있습니다. 토큰이 없으면 포트에 접근할 수 있는 누구나 데이터를 읽을 수 있으며, wvd는 시작할 때 경고를 남깁니다. 읽기만으로는 기기를 바꿀 수 없습니다.

접근을 제한하려면 다음을 쓰세요.
- `--token …`(또는 `WVD_TOKEN`)을 주면 `/api/v1/health`를 뺀 모든 요청에 `Authorization: Bearer …`(또는 `?token=`)가 필요합니다. 대시보드는 처음 한 번 토큰을 물어보고 기억합니다.
- `--host 127.0.0.1`로 이 PC에서만 접속하게 할 수 있습니다.
- 토큰 없이 외부 주소에 바인딩한 상태에서 `--allow-write`를 켜면 wvd가 시작을 거부합니다. 네트워크의 아무나 기기 폴트를 해제할 수 있게 되기 때문입니다.
- TLS는 없습니다. 신뢰할 수 있는 LAN 밖으로 열려면 앞에 리버스 프록시를 두세요.
- 다른 PC에서 접속이 안 되면 방화벽을 확인하세요(ufw: `sudo ufw allow 8765/tcp`).

## 프로토콜

`WireViewDeviceLib`에서 추출해 실제 기기로 검증했습니다.

- 115200 8N1, raw 모드. 입력 버퍼를 비우고, 명령 1바이트를 쓰고, 정해진 길이의 응답을 읽습니다.
- `0x01` vendor 데이터(3 B), `0x02` UID(12 B), `0x04` 센서 값(100 B, `<4hHBx` + `h2xII`×6 + `IIHBxHH`), `0x0D` 빌드 정보(68 B), `0x0E` 폴트 해제(+ u16 keep 마스크 2개)
- 프레임에 CRC가 없습니다. 공식 클라이언트와 같은 규칙으로, 패딩 바이트가 0이 아니거나 팬 듀티가 100을 넘는 프레임은 버립니다.
- 테스트한 기기 기준으로 읽기 한 번에 약 0.2 ms가 걸리고, 펌웨어는 약 16 ms마다 값을 갱신합니다.

자세한 내용은 `wvd/protocol.py`를 보세요. wvd 없이 시리얼을 직접 읽어야 한다면 `wvd/protocol.py`와 `wvd/transport.py`를 그대로 쓸 수 있습니다. 이때는 wvd를 먼저 꺼야 합니다.

## 디렉터리 구조

```
wireview-monitor/
├── wvd/
│   ├── protocol.py     명령, 프레임 구조, 디코딩
│   ├── transport.py    시리얼 포트(배타 오픈, 자동 탐지)와 시뮬레이터
│   ├── sampler.py      샘플링 스레드 (포트 단독 소유, 재연결, 명령 큐)
│   ├── samples.py      샘플 형식, 통계, 다운샘플링
│   ├── store.py        링버퍼 + SQLite, 이벤트, 세션
│   ├── events.py       폴트·임계값 이벤트 엔진
│   ├── api.py          REST, WebSocket/SSE, /metrics, 인증
│   ├── daemon.py       wvd 실행 진입점 (옵션 처리)
│   ├── client.py       Python 클라이언트 (표준 라이브러리만 사용)
│   ├── cli.py          wvctl
│   ├── testing.py      pytest fixture `wireview`
│   └── static/         웹 대시보드 (index.html, app.js, style.css, uPlot)
├── tests/              테스트 25개
├── scripts/            up.sh, down.sh
├── packaging/          wvd.service (systemd)
├── docs/design.html    설계 문서
└── pyproject.toml
```

## 테스트

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest                                                   # 시뮬레이터로 전체 실행
WVD_URL=http://127.0.0.1:8765 .venv/bin/pytest tests/test_cli.py   # 실제 기기 데몬 대상
```
