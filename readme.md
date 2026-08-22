# 시장 신호반 (Market Signal Panel)

나스닥 / 비트코인 / 리플 / 금 / 달러 / 엔화의 데일리 매수·매도 신호를 신호등처럼 보여주는 대시보드.
실제 매매는 실행하지 않고, 참고용 신호만 표시함.

```
├── index.html          # GitHub Pages에 올릴 페이지 (signals.json을 fetch해서 렌더링)
├── signals.json         # 백엔드가 갱신하는 데이터 파일 (지금 든 건 미리보기용 샘플)
├── backend/
│   ├── market_signal_updater.py   # 라즈베리파이에서 크론으로 돌릴 스크립트
│   └── requirements.txt
└── README.md
```

## 동작 방식

라즈베리파이가 인터넷에 직접 열려있을 필요가 없도록, **백엔드가 signals.json을 만들어서 같은 깃허브 저장소에 커밋+푸시**하는 방식으로 설계함. GitHub Pages(index.html)는 그냥 같은 경로의 signals.json을 fetch만 함.

```
[라즈베리파이 크론] → market_signal_updater.py 실행
                      → CNN F&G / alternative.me / Upbit / Stooq 조회
                      → signals.json 생성
                      → git commit & push
                                ↓
[GitHub Pages] index.html가 fetch('./signals.json')으로 표시
```

## 설치

### 1) 깃허브 저장소 준비
이 폴더 전체(`index.html`, `signals.json`, `backend/`)를 하나의 깃허브 저장소에 push하고, Settings → Pages에서 GitHub Pages를 켜기.

### 2) 라즈베리파이
```bash
git clone <저장소 주소> market-signal-dashboard
cd market-signal-dashboard
pip install -r backend/requirements.txt --break-system-packages
```

**중요 — 크론이 무인으로 push할 수 있어야 함:**
- HTTPS로 clone했다면 `git config credential.helper store` 후 한 번 수동으로 push해서 자격 증명을 저장해두거나, PAT(Personal Access Token)를 remote 주소에 박아두기
- 또는 SSH로 clone하고 deploy key(쓰기 권한)를 등록해두는 쪽을 더 추천 (raspberry pi에서 이미 다른 봇들에 쓰는 방식이 있다면 그걸 재사용해도 됨)

한번 수동 실행해서 확인:
```bash
python3 backend/market_signal_updater.py
# push 없이 로컬 결과만 보고 싶으면:
python3 backend/market_signal_updater.py --no-push
```
`errorLog/log_YYYYMMDD.log`에 각 자산별 조회 결과/에러가 남음 — 처음 돌릴 땐 이 로그로 소스별로 잘 붙는지 확인 추천.

### 3) 크론 등록
```
10 8 * * * cd /home/pi/market-signal-dashboard && /usr/bin/python3 backend/market_signal_updater.py >> errorLog/cron_out.log 2>&1
```
F&G 지수 자체가 하루 단위로만 갱신되는 값이라 하루 1~2회면 충분함. 더 자주 확인하고 싶으면 하루 2회(예: 08:10, 18:10)로 늘려도 무방.

### 4) (선택) 텔레그램 실패 알림
스크립트 실행 환경에 아래 두 환경변수를 넣어두면 git push 실패 등 치명적 에러일 때만 텔레그램으로 알려줌 (평소엔 조용함):
```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

## 데이터 소스 & 혹시 안 될 때

| 자산 | 신호 소스 | 가격/365일 소스 |
|---|---|---|
| 나스닥 | CNN F&G (직접 API, 실패시 feargreedchart.com) | Stooq `^ndq` |
| 비트코인/리플 | alternative.me 코인 F&G (실패시 feargreedchart.com) | Upbit 일봉 |
| 금 | 이동평균 | Stooq `xauusd` |
| 달러 | 이동평균 | Stooq `usdkrw` |
| 엔화 | 이동평균 | Stooq `usdkrw` ÷ `usdjpy` × 100 (100엔 기준) |

Stooq 심볼은 직접 확인해서 넣었지만 외부 사이트라 언제든 바뀔 수 있음. 특정 자산만 계속 에러 나면 `backend/market_signal_updater.py` 상단 `STOOQ_SYMBOLS` 딕셔너리만 고치면 됨 (다른 로직 안 건드려도 됨). `https://stooq.com/q/d/?s=원하는심볼` 로 브라우저에서 먼저 확인 가능.

## 금/달러/엔화 신호 로직 — 요청하신 방식 그대로 구현했고, 참고할 만한 대안도 적어둠

지금 구현된 건 요청하신 그대로: **현재가 < 120일선 → 매수**, **현재가 > 5·20·60·120일선 & 정배열 → 매도(부분)**.

다만 이 방식은 "매수 조건"은 이평선 1개만 보고 "매도 조건"은 이평선 4개+정배열을 보는 비대칭 구조라, 변동성이 큰 시기엔 좀 둔감하게 느껴질 수 있음. 참고할 만한 대안:

- **RSI(14)**: 30 이하 과매도/70 이상 과매수. 지금 대시보드에도 참고 지표로 이미 같이 표시해뒀음 (신호 자체엔 아직 안 씀).
- **볼린저밴드 %B**: 20일 이평 ±2표준편차 기준으로 "얼마나 벗어났는지"를 변동성 대비 상대적으로 봄 — 금처럼 변동성 자체가 시기별로 크게 바뀌는 자산엔 고정폭 이평선보다 잘 맞는 경우가 많음.
- 둘 다 원하면 나중에 `build_ma_asset()` 함수의 신호 판정 부분만 바꾸면 되는 구조로 짜놨음.

## 자유롭게 수정할 부분
- 디자인(`index.html`의 `<style>`)은 요청대로 마음대로 잡았으니 편하게 색/폰트/레이아웃 수정하면 됨.
- 신호 임계값(Extreme Fear/Greed 진입 기준)은 CNN·alternative.me가 주는 공식 rating 문자열을 그대로 씀 — 더 보수적으로 잡고 싶으면 `market_signal_updater.py`의 `build_fng_asset()`에서 숫자 임계값 비교로 바꿀 수도 있음.
