#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_signal_updater.py
=========================
나스닥 / 비트코인 / 리플 / 금 / 달러(USD-KRW) / 엔화(JPY-KRW) 데일리 매수-매도 신호를 계산해서
signals.json 으로 저장하고, 깃허브 저장소에 커밋+푸시까지 하는 스크립트.

신호 로직
---------
- 나스닥      : CNN Fear & Greed  -> Extreme Fear 진입 시 매수 / Extreme Greed 진입 시 매도(부분)
- 비트코인/리플: 코인 Fear & Greed -> Extreme Fear 진입 시 매수 / Extreme Greed 진입 시 매도(부분)
- 금/달러/엔화 : 현재가 < 120일선 -> 매수
                현재가 > 5일선 > 20일선 > 60일선 > 120일선 (정배열) -> 매도(부분)
                그 외 -> 관망
  (+ 참고용으로 RSI(14)도 같이 계산해서 넣어줌. 대안 아이디어는 README 참고)

시세 소스: 나스닥/금/달러/엔화 = 야후 파이낸스 v8 차트 API (query1/query2, apikey 불필요)
          Stooq는 2026-04부터 apikey를 요구하도록 바뀌어서 더 이상 안 씀 (README 참고)

사용법
------
    python3 market_signal_updater.py              # 계산 + signals.json 저장 + git push
    python3 market_signal_updater.py --no-push     # git push 없이 로컬 저장만
    python3 market_signal_updater.py --repo-dir /path/to/repo

크론 예시 (매일 아침 8시 10분, KST)
------------------------------------
    10 8 * * * cd /home/pi/market-signal-dashboard && \
        /usr/bin/python3 backend/market_signal_updater.py >> errorLog/cron_out.log 2>&1

필요 패키지: requirements.txt 참고 (pip install -r requirements.txt --break-system-packages)
"""

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

KST = timezone(timedelta(hours=9))
LOOKBACK_DAYS = 400          # 365일 계산에 여유를 둔 조회 기간
REQUEST_TIMEOUT = 20

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# 야후 파이낸스 티커 (Stooq는 2026-04부터 apikey 필요해져서 교체함)
YAHOO_SYMBOLS = {
    "nasdaq": "^IXIC",     # 나스닥 종합지수
    "gold": "GC=F",        # COMEX 금 선물 (국제 금 시세 프록시로 사용, 현물가와 거의 동일하게 움직임)
    "usdkrw": "KRW=X",     # USD/KRW
    "usdjpy": "JPY=X",     # USD/JPY
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

log = logging.getLogger("market_signal")


# ---------------------------------------------------------------------------
# 로깅 / 텔레그램 (기존 봇들과 동일한 errorLog/log_YYYYMMDD.log 포맷)
# ---------------------------------------------------------------------------

def setup_logger(base_dir: str) -> None:
    log_dir = os.path.join(base_dir, "errorLog")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"log_{datetime.now(KST).strftime('%Y%m%d')}.log")

    log.setLevel(logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def notify_telegram(message: str) -> None:
    """TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 설정된 경우에만 전송. 실패해도 죽지 않음."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"텔레그램 알림 실패: {e}")


# ---------------------------------------------------------------------------
# 원본 데이터 조회
# ---------------------------------------------------------------------------

def fetch_cnn_fear_greed():
    """CNN Fear & Greed Index (나스닥 신호원). 1순위 CNN 직접 -> 실패시 feargreedchart.com"""
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        fg = r.json()["fear_and_greed"]
        return float(fg["score"]), str(fg["rating"]).lower().strip()
    except Exception as e:  # noqa: BLE001
        log.warning(f"CNN F&G 직접 조회 실패 ({e}), 대체 소스(feargreedchart.com) 시도")

    try:
        r = requests.get(
            "https://feargreedchart.com/api/?action=all",
            headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        score = float(r.json()["score"]["score"])
        return score, _score_to_rating(score)
    except Exception as e:  # noqa: BLE001
        log.error(f"CNN F&G 대체 소스도 실패: {e}")
        return None, None


def fetch_crypto_fear_greed():
    """코인 Fear & Greed (비트코인/리플 공용 신호원). 1순위 alternative.me -> 실패시 feargreedchart.com"""
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()["data"][0]
        return float(d["value"]), str(d["value_classification"]).lower().strip()
    except Exception as e:  # noqa: BLE001
        log.warning(f"alternative.me 조회 실패 ({e}), 대체 소스(feargreedchart.com) 시도")

    try:
        r = requests.get(
            "https://feargreedchart.com/api/?action=crypto",
            headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()["crypto_fng"]
        return float(d["score"]), str(d["label"]).lower().strip()
    except Exception as e:  # noqa: BLE001
        log.error(f"코인 F&G 대체 소스도 실패: {e}")
        return None, None


def _score_to_rating(score: float) -> str:
    """feargreedchart.com 은 rating 라벨을 안 주므로 CNN 공식 구간대로 직접 환산."""
    if score <= 20:
        return "extreme fear"
    if score <= 40:
        return "fear"
    if score <= 60:
        return "neutral"
    if score <= 80:
        return "greed"
    return "extreme greed"


def fetch_upbit_daily(market: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """업비트 일봉 종가. 1회 최대 200개라 필요시 페이지네이션."""
    rows = []
    to_ts = None
    remaining = days

    while remaining > 0:
        count = min(200, remaining)
        params = {"market": market, "count": count}
        if to_ts:
            params["to"] = to_ts
        r = requests.get(
            "https://api.upbit.com/v1/candles/days",
            params=params, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        chunk = r.json()
        if not chunk:
            break
        rows.extend(chunk)
        to_ts = chunk[-1]["candle_date_time_utc"]
        remaining -= count
        if len(chunk) < count:
            break

    if not rows:
        raise ValueError(f"업비트 캔들 데이터가 비어있음: {market}")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["candle_date_time_kst"])
    df["close"] = df["trade_price"].astype(float)
    df = df.sort_values("date").drop_duplicates(subset="date")
    return df[["date", "close"]].reset_index(drop=True)


def fetch_binance_daily(symbol: str = "BTCUSDT", days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """바이낸스 일봉 종가. 김치프리미엄 계산용 해외 기준가. limit 1000까지 한 번에 되므로 페이지네이션 불필요."""
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": "1d", "limit": days},
        headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError(f"바이낸스 캔들 데이터가 비어있음: {symbol}")

    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_asset_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df["date"] = pd.to_datetime(df["open_time"], unit="ms").dt.normalize()
    df["close"] = df["close"].astype(float)
    df = df.sort_values("date").drop_duplicates(subset="date")
    return df[["date", "close"]].reset_index(drop=True)


def fetch_yahoo_daily(symbol: str) -> pd.DataFrame:
    """야후 파이낸스 v8 차트 API 일봉 종가. (나스닥지수 / 금 / 원화-달러 / 원화-엔화 공용)

    query1 -> 실패시 query2 순으로 시도 (야후가 두 호스트로 로드밸런싱하길래 폴백으로 활용).
    이 차트(v8) 엔드포인트는 시세 조회(v7/quote)와 달리 crumb/쿠키 없이도 동작함.
    """
    params = {"range": "2y", "interval": "1d"}
    last_err = None

    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            url = f"https://{host}/v8/finance/chart/{symbol}"
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            payload = r.json()

            result = payload.get("chart", {}).get("result")
            if not result:
                raise ValueError(f"result 없음 (error={payload.get('chart', {}).get('error')})")

            node = result[0]
            timestamps = node.get("timestamp")
            quote = node.get("indicators", {}).get("quote", [{}])[0]
            closes = quote.get("close")
            if not timestamps or not closes:
                raise ValueError("timestamp/close 필드 없음")

            df = pd.DataFrame({
                "date": pd.to_datetime(timestamps, unit="s").normalize(),
                "close": closes,
            })
            df = df.dropna(subset=["close"]).sort_values("date").drop_duplicates(subset="date")
            if df.empty:
                raise ValueError("유효한 종가가 하나도 없음")
            return df[["date", "close"]].reset_index(drop=True)

        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning(f"야후 파이낸스 {host} 조회 실패 (symbol={symbol}): {e}")

    raise ValueError(f"야후 파이낸스 조회 전체 실패: symbol={symbol}, 마지막 에러={last_err}")


# ---------------------------------------------------------------------------
# 지표 계산
# ---------------------------------------------------------------------------

def sma(closes: pd.Series, window: int):
    if len(closes) < window:
        return None
    return float(closes.tail(window).mean())


def rsi(closes: pd.Series, period: int = 14):
    """단순 이동평균 기반 RSI (참고용 보조 지표)."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def price_stats(df: pd.DataFrame, days: int = 365):
    """현재가 / 365일 최고가 / MDD(고점 대비 %) / 기준일"""
    recent = df.tail(days)
    current = float(recent["close"].iloc[-1])
    high = float(recent["close"].max())
    mdd = (current / high - 1) * 100 if high else None
    as_of = recent["date"].iloc[-1].strftime("%Y-%m-%d")
    return current, high, mdd, as_of


# ---------------------------------------------------------------------------
# 자산별 signal 딕셔너리 조립
# ---------------------------------------------------------------------------

def error_asset(name_kr: str, driver: str) -> dict:
    return {
        "name_kr": name_kr,
        "driver": driver,
        "signal": "unknown",
        "signal_label": "일시 오류",
        "price": None, "high_365d": None, "mdd_pct": None, "as_of": None,
    }


def build_fng_asset(name_kr, df, score, rating, currency, unit_note=""):
    current, high, mdd, as_of = price_stats(df)

    if score is None or rating is None:
        signal, label = "unknown", "신호 없음(소스 오류)"
    elif "extreme fear" in rating:
        signal, label = "buy", "매수"
    elif "extreme greed" in rating:
        signal, label = "sell", "매도(부분)"
    else:
        signal, label = "hold", "관망"

    return {
        "name_kr": name_kr,
        "driver": "fear_greed",
        "signal": signal,
        "signal_label": label,
        "fg_score": score,
        "fg_rating": rating,
        "price": round(current, 2),
        "high_365d": round(high, 2),
        "mdd_pct": round(mdd, 2) if mdd is not None else None,
        "as_of": as_of,
        "currency": currency,
        "unit_note": unit_note,
    }


def build_ma_asset(name_kr, df, currency, unit_note=""):
    current, high, mdd, as_of = price_stats(df)
    closes = df["close"]
    ma5, ma20, ma60, ma120 = sma(closes, 5), sma(closes, 20), sma(closes, 60), sma(closes, 120)
    rsi14 = rsi(closes, 14)

    if None in (ma5, ma20, ma60, ma120):
        signal, label = "unknown", "데이터 부족"
    elif current < ma120:
        signal, label = "buy", "매수"
    elif current > ma5 > ma20 > ma60 > ma120:
        signal, label = "sell", "매도(부분)"
    else:
        signal, label = "hold", "관망"

    return {
        "name_kr": name_kr,
        "driver": "moving_average",
        "signal": signal,
        "signal_label": label,
        "ma5": round(ma5, 4) if ma5 is not None else None,
        "ma20": round(ma20, 4) if ma20 is not None else None,
        "ma60": round(ma60, 4) if ma60 is not None else None,
        "ma120": round(ma120, 4) if ma120 is not None else None,
        "rsi14": round(rsi14, 1) if rsi14 is not None else None,
        "price": round(current, 4),
        "high_365d": round(high, 4),
        "mdd_pct": round(mdd, 2) if mdd is not None else None,
        "as_of": as_of,
        "currency": currency,
        "unit_note": unit_note,
    }


def build_kimchi_premium_asset(upbit_btc_df, binance_btc_df, usdkrw_df, days: int = 365):
    """업비트 BTC/KRW vs 바이낸스 BTC/USDT(원화환산) 괴리율.
    기준(사용자와 합의): +5% 이상 매도(부분), -5% 이하 매수, 그 사이 관망.
    USD/KRW은 주말에 안 갱신되니 merge_asof로 가장 최근 환율을 끌어다 씀.
    """
    upbit = upbit_btc_df.rename(columns={"close": "close_krw"})
    binance = binance_btc_df.rename(columns={"close": "close_usdt"})
    fx = usdkrw_df.rename(columns={"close": "close_fx"}).sort_values("date")

    merged = pd.merge(upbit, binance, on="date")
    merged = pd.merge_asof(merged.sort_values("date"), fx, on="date", direction="backward")
    merged = merged.dropna(subset=["close_krw", "close_usdt", "close_fx"])

    if merged.empty:
        raise ValueError("김치프리미엄 계산에 쓸 공통 날짜 데이터가 없음")

    merged["premium"] = (merged["close_krw"] / (merged["close_usdt"] * merged["close_fx"]) - 1) * 100

    recent = merged.tail(days)
    current = float(recent["premium"].iloc[-1])
    high = float(recent["premium"].max())
    low = float(recent["premium"].min())
    as_of = recent["date"].iloc[-1].strftime("%Y-%m-%d")

    if current >= 5:
        signal, label = "sell", "매도(부분)"
    elif current <= -5:
        signal, label = "buy", "매수"
    else:
        signal, label = "hold", "관망"

    return {
        "name_kr": "김프",
        "driver": "kimchi_premium",
        "signal": signal,
        "signal_label": label,
        "premium_pct": round(current, 2),
        "high_365d": round(high, 2),
        "low_365d": round(low, 2),
        "as_of": as_of,
    }


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def compute_all_assets() -> dict:
    assets = {}

    # 나스닥 (CNN F&G)
    try:
        score, rating = fetch_cnn_fear_greed()
        df = fetch_yahoo_daily(YAHOO_SYMBOLS["nasdaq"])
        assets["nasdaq"] = build_fng_asset("나스닥", df, score, rating, "pt", "나스닥종합지수")
    except Exception as e:  # noqa: BLE001
        log.error(f"[나스닥] 처리 실패: {e}")
        assets["nasdaq"] = error_asset("나스닥", "fear_greed")

    # 비트코인 / 리플 (코인 F&G 공용 조회 1회)
    c_score, c_rating = fetch_crypto_fear_greed()

    for key, market, name in (("bitcoin", "KRW-BTC", "비트코인"), ("xrp", "KRW-XRP", "리플")):
        try:
            df = fetch_upbit_daily(market)
            assets[key] = build_fng_asset(name, df, c_score, c_rating, "KRW")
        except Exception as e:  # noqa: BLE001
            log.error(f"[{name}] 처리 실패: {e}")
            assets[key] = error_asset(name, "fear_greed")

    # 김치프리미엄 (업비트 BTC vs 바이낸스 BTC, 원화환산 괴리율)
    try:
        kp_upbit_df = fetch_upbit_daily("KRW-BTC")
        kp_binance_df = fetch_binance_daily("BTCUSDT")
        kp_usdkrw_df = fetch_yahoo_daily(YAHOO_SYMBOLS["usdkrw"])
        assets["kimchi_premium"] = build_kimchi_premium_asset(kp_upbit_df, kp_binance_df, kp_usdkrw_df)
    except Exception as e:  # noqa: BLE001
        log.error(f"[김프] 처리 실패: {e}")
        assets["kimchi_premium"] = error_asset("김프", "kimchi_premium")

    # 금
    try:
        gold_df = fetch_yahoo_daily(YAHOO_SYMBOLS["gold"])
        assets["gold"] = build_ma_asset("금", gold_df, "USD", "트로이온스당")
    except Exception as e:  # noqa: BLE001
        log.error(f"[금] 처리 실패: {e}")
        assets["gold"] = error_asset("금", "moving_average")

    # 달러 (USD-KRW)
    usdkrw_df = None
    try:
        usdkrw_df = fetch_yahoo_daily(YAHOO_SYMBOLS["usdkrw"])
        assets["usdkrw"] = build_ma_asset("달러", usdkrw_df, "KRW", "1달러당")
    except Exception as e:  # noqa: BLE001
        log.error(f"[달러] 처리 실패: {e}")
        assets["usdkrw"] = error_asset("달러", "moving_average")

    # 엔화 (JPY-KRW = USDKRW / USDJPY * 100, 100엔당 원화로 표시)
    try:
        usdjpy_df = fetch_yahoo_daily(YAHOO_SYMBOLS["usdjpy"])
        if usdkrw_df is None:
            usdkrw_df = fetch_yahoo_daily(YAHOO_SYMBOLS["usdkrw"])
        merged = pd.merge(usdkrw_df, usdjpy_df, on="date", suffixes=("_krw", "_jpy"))
        merged["close"] = merged["close_krw"] / merged["close_jpy"] * 100
        assets["jpykrw"] = build_ma_asset("엔화", merged[["date", "close"]], "KRW", "100엔당")
    except Exception as e:  # noqa: BLE001
        log.error(f"[엔화] 처리 실패: {e}")
        assets["jpykrw"] = error_asset("엔화", "moving_average")

    return assets


def acquire_lock(repo_dir: str):
    """같은 스크립트가 겹쳐 돌지 않게 파일 락. 프로세스가 죽으면 OS가 알아서 풀어줌.
    반환값을 변수에 계속 들고 있어야 락이 유지됨(가비지컬렉션되면 자동 해제).
    """
    lock_path = os.path.join(repo_dir, ".market_signal_updater.lock")
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def ensure_gitignore(repo_dir: str) -> None:
    """락 파일 / 로그 / 파이썬 캐시가 실수로 커밋되지 않게 .gitignore를 보장해둠."""
    gitignore_path = os.path.join(repo_dir, ".gitignore")
    needed = [".market_signal_updater.lock", "errorLog/", "__pycache__/", "*.pyc"]
    existing = ""
    if os.path.exists(gitignore_path):
        existing = open(gitignore_path, encoding="utf-8").read()
    missing = [e for e in needed if e not in existing]
    if missing:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(missing) + "\n")


def git_commit_and_push(repo_dir: str, message: str) -> None:
    def run(*args):
        return subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True, text=True,
        )

    # -A로 바꾼 이유: signals.json만 add하면, 라파 로컬에서 이 스크립트 자체를 수정해도
    # 그 변경분이 깃헙에 영영 안 올라가서 예전 코드가 원격에 남아있게 됨.
    # (전에 reset --hard origin/main을 실행 초반에 넣었다가, 바로 이 문제 때문에
    #  스크립트 자신이 원격의 구버전으로 되돌아가버리는 부작용이 있었음 — 그 로직은 제거함)
    add = run("add", "-A")
    if add.returncode != 0:
        log.error(f"git add 실패: {add.stderr}")
        return

    commit = run("commit", "-m", message)
    if commit.returncode != 0:
        if "nothing to commit" in (commit.stdout + commit.stderr):
            log.info("변경된 내용이 없어 커밋 생략")
            return
        log.error(f"git commit 실패: {commit.stderr}")
        return

    push = run("push")
    if push.returncode == 0:
        log.info("git push 완료")
        return

    # non-fast-forward(다른 실행/깃헙 웹 수정 등으로 원격이 앞서있는 경우)면 pull로 합치고 한 번만 재시도.
    # signals.json이 충돌나면 이번에 새로 계산한 값(우리 쪽)을 우선함 -X ours.
    log.warning(f"git push 실패, pull 후 재시도: {push.stderr.strip()[:200]}")
    pull = run("pull", "--no-edit", "--no-rebase", "-X", "ours")
    if pull.returncode != 0:
        log.error(f"git pull 실패: {pull.stderr}")
        notify_telegram(f"⚠️ 시장 신호 대시보드 git pull 실패\n{pull.stderr[:300]}")
        return

    push2 = run("push")
    if push2.returncode != 0:
        log.error(f"재시도 git push도 실패: {push2.stderr}")
        notify_telegram(f"⚠️ 시장 신호 대시보드 git push 재시도 실패\n{push2.stderr[:300]}")
        return

    log.info("재시도 후 git push 완료")


def main():
    parser = argparse.ArgumentParser(description="데일리 매수/매도 신호 계산 -> signals.json")
    default_repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--repo-dir", default=default_repo_dir, help="signals.json 을 저장할 git 저장소 경로")
    parser.add_argument("--no-push", action="store_true", help="git commit/push 없이 로컬 저장만")
    args = parser.parse_args()

    setup_logger(args.repo_dir)
    log.info("=== market_signal_updater 시작 ===")

    lock = acquire_lock(args.repo_dir)
    if lock is None:
        log.warning("이미 다른 인스턴스가 실행 중이라 이번 실행은 건너뜀 (락 획득 실패)")
        sys.exit(0)

    if not args.no_push:
        ensure_gitignore(args.repo_dir)

    try:
        assets = compute_all_assets()
    except Exception as e:  # noqa: BLE001
        log.exception(f"전체 실행 중 예외 발생: {e}")
        notify_telegram(f"🚨 시장 신호 대시보드 스크립트 전체 실패: {e}")
        sys.exit(1)

    failed = [a["name_kr"] for a in assets.values() if a["signal"] == "unknown"]
    if failed:
        log.warning(f"일부 자산 조회 실패: {', '.join(failed)}")

    output = {
        "updated_at": datetime.now(KST).isoformat(),
        "assets": assets,
    }

    out_path = os.path.join(args.repo_dir, "signals.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info(f"signals.json 저장 완료: {out_path}")

    for key, a in assets.items():
        log.info(f"  - {a['name_kr']:>4s}: {a['signal_label']:>10s}  price={a.get('price')}")

    if not args.no_push:
        git_commit_and_push(args.repo_dir, f"signals update {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}")

    log.info("=== market_signal_updater 종료 ===")


if __name__ == "__main__":
    main()
