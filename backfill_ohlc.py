"""
backfill_ohlc.py — v1.0 SIGNAL HISTORY OHLC BACKFILL
─────────────────────────────────────────────────────────
✔ signal_history.csv의 day_open/high/low/pct 빈칸 자동 채움
✔ KIS 일봉차트 API (FHKST03010100) 사용 → 100일치 OHLC 한 번에 조회
✔ 멱등성: 이미 채워진 값은 절대 덮어쓰지 않음
✔ .bak 자동 백업 (실행 전)
✔ 5컬럼 보완: day_open, day_high, day_low, day_high_pct, day_low_pct
✔ 추가 비용 0원 (KIS 무료 API)
─────────────────────────────────────────────────────────
실행: python backfill_ohlc.py
필요 ENV: KIS_APP_KEY, KIS_APP_SECRET
"""

import os, json, time, shutil, requests
import pandas as pd
from datetime import datetime, timezone, timedelta

# ── 경로 (repo root에서 실행 가정) ────────────────────────
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
SIGNAL_HISTORY = os.path.join(BASE_DIR, "signal_history.csv")
TOKEN_FILE     = os.path.join(BASE_DIR, "kis_token.json")

# ── KIS 상수 ──────────────────────────────────────────────
KIS_BASE  = "https://openapi.koreainvestment.com:9443"
TIMEOUT   = 10
MAX_RETRY = 3
DELAY     = 0.2
KST       = timezone(timedelta(hours=9))


# ═══════════════════════════════════════════════════════════
# UTILS
# ═══════════════════════════════════════════════════════════
def safe_int(v, d=0):
    try:
        if pd.isna(v): return d
        return int(float(str(v).replace(",", "")))
    except:
        return d


def safe_float(v, d=0.0):
    try:
        if pd.isna(v): return d
        return float(str(v).replace(",", ""))
    except:
        return d


def is_blank(v):
    """NaN / 빈문자열 / 0 → 빈칸으로 간주"""
    if pd.isna(v):
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    if safe_float(v) == 0:
        return True
    return False


# ═══════════════════════════════════════════════════════════
# KIS TOKEN
# ═══════════════════════════════════════════════════════════
def get_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
        issued_str = data.get("issued_at", "").replace("Z", "") or "2000-01-01T00:00:00"
        issued = datetime.fromisoformat(issued_str)
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=KST)
        if (datetime.now(KST) - issued).total_seconds() < 21600:
            return data.get("access_token")
    except:
        pass

    for _ in range(MAX_RETRY):
        try:
            r = requests.post(
                f"{KIS_BASE}/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey":     os.environ.get("KIS_APP_KEY", ""),
                    "appsecret":  os.environ.get("KIS_APP_SECRET", "")
                },
                timeout=TIMEOUT
            )
            r.raise_for_status()
            token = r.json().get("access_token")
            with open(TOKEN_FILE, "w", encoding="utf-8-sig") as f:
                json.dump({"access_token": token,
                           "issued_at": datetime.now(KST).isoformat()}, f)
            return token
        except:
            time.sleep(1)
    return None


def kis_headers(token, tr_id):
    return {
        "authorization": f"Bearer {token}",
        "appkey":        os.environ.get("KIS_APP_KEY", ""),
        "appsecret":     os.environ.get("KIS_APP_SECRET", ""),
        "tr_id":         tr_id,
        "content-type":  "application/json",
        "custtype":      "P"
    }


# ═══════════════════════════════════════════════════════════
# KIS 일봉차트 (기간 OHLC 한 번에 조회)
# ═══════════════════════════════════════════════════════════
def fetch_daily_ohlc(token, code, start_date, end_date):
    """
    start_date / end_date: 'YYYY-MM-DD'
    반환: {'YYYY-MM-DD': {'open':..., 'high':..., 'low':...}, ...}
    """
    if not token:
        return {}

    start_yyyymmdd = start_date.replace("-", "")
    end_yyyymmdd   = end_date.replace("-", "")

    for _ in range(MAX_RETRY):
        try:
            r = requests.get(
                f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                headers=kis_headers(token, "FHKST03010100"),
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD":         code,
                    "FID_INPUT_DATE_1":       start_yyyymmdd,
                    "FID_INPUT_DATE_2":       end_yyyymmdd,
                    "FID_PERIOD_DIV_CODE":    "D",
                    "FID_ORG_ADJ_PRC":        "0"
                },
                timeout=TIMEOUT
            )
            if r.status_code == 401:
                return {}
            r.raise_for_status()
            d = r.json()
            if d.get("rt_cd") != "0":
                return {}

            output2 = d.get("output2") or []
            if not isinstance(output2, list):
                return {}

            ohlc_by_date = {}
            for row in output2:
                date_str = str(row.get("stck_bsop_date", ""))
                if len(date_str) != 8 or not date_str.isdigit():
                    continue
                date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                opn  = safe_int(row.get("stck_oprc"))
                high = safe_int(row.get("stck_hgpr"))
                low  = safe_int(row.get("stck_lwpr"))
                if high > 0 and low > 0:
                    ohlc_by_date[date_fmt] = {"open": opn, "high": high, "low": low}
            return ohlc_by_date
        except Exception:
            time.sleep(DELAY)
    return {}


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    print(f"[BACKFILL] start {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 파일 존재 확인
    if not os.path.exists(SIGNAL_HISTORY):
        print(f"[BACKFILL] {SIGNAL_HISTORY} 없음 → 종료")
        return

    # 2. 자동 백업
    backup_path = f"{SIGNAL_HISTORY}.bak"
    shutil.copy2(SIGNAL_HISTORY, backup_path)
    print(f"[BACKFILL] 백업 생성: {backup_path}")

    # 3. CSV 로드
    df = pd.read_csv(SIGNAL_HISTORY, dtype={"code": str}, encoding="utf-8-sig")
    df["code"] = df["code"].str.zfill(6)
    total_rows = len(df)
    print(f"[BACKFILL] 전체 {total_rows} rows 로드")

    # 4. 빈칸 행 식별 (day_high 또는 day_low가 빈칸)
    mask = df["day_high"].apply(is_blank) | df["day_low"].apply(is_blank)
    blank_rows = df[mask]

    if len(blank_rows) == 0:
        print("[BACKFILL] 빈칸 없음 → 종료")
        return

    print(f"[BACKFILL] 빈칸 행: {len(blank_rows)}개 발견")

    # 5. (종목, 날짜)별로 그룹화 → 종목당 1회 API 호출
    targets_by_code = {}
    for _, r in blank_rows.iterrows():
        code = r["code"]
        # biz_day 우선, 없으면 date
        date_val = r.get("biz_day")
        if pd.isna(date_val) or not str(date_val).strip():
            date_val = r.get("date")
        if pd.isna(date_val) or not str(date_val).strip():
            continue
        targets_by_code.setdefault(code, set()).add(str(date_val))

    print(f"[BACKFILL] 조회 대상 종목: {len(targets_by_code)}개")

    # 6. KIS 토큰
    token = get_token()
    if not token:
        print("[BACKFILL] KIS 토큰 실패 → 종료")
        return

    # 7. 종목별 OHLC 조회 (날짜 범위로 한 번에)
    ohlc_lookup = {}   # {(code, date): {open, high, low}}
    fetch_ok    = 0
    fetch_fail  = 0

    for code, dates in targets_by_code.items():
        dates_sorted = sorted(dates)
        start_date   = dates_sorted[0]
        end_date     = dates_sorted[-1]

        result = fetch_daily_ohlc(token, code, start_date, end_date)
        if not result:
            fetch_fail += 1
            print(f"[BACKFILL] ✗ {code} 조회 실패")
            time.sleep(DELAY)
            continue

        fetch_ok += 1
        for date in dates:
            if date in result:
                ohlc_lookup[(code, date)] = result[date]

        time.sleep(DELAY)

    print(f"[BACKFILL] API 조회: 성공 {fetch_ok} / 실패 {fetch_fail}")

    # 8. DataFrame 업데이트 (멱등성 — 빈칸만 채움)
    patched_open = patched_high = patched_low = 0
    patched_hpct = patched_lpct = 0

    for idx, r in df.iterrows():
        code = r["code"]
        date_val = r.get("biz_day")
        if pd.isna(date_val) or not str(date_val).strip():
            date_val = r.get("date")
        if pd.isna(date_val):
            continue

        key = (code, str(date_val))
        if key not in ohlc_lookup:
            continue

        ohlc  = ohlc_lookup[key]
        entry = safe_float(r.get("price"))
        opn   = ohlc.get("open", 0)
        high  = ohlc.get("high", 0)
        low   = ohlc.get("low",  0)

        if high == 0 or low == 0:
            continue

        # day_open
        if is_blank(r.get("day_open")) and opn > 0:
            df.at[idx, "day_open"] = opn
            patched_open += 1

        # day_high
        if is_blank(r.get("day_high")):
            df.at[idx, "day_high"] = high
            patched_high += 1

        # day_low
        if is_blank(r.get("day_low")):
            df.at[idx, "day_low"] = low
            patched_low += 1

        # day_high_pct (연산값)
        if is_blank(r.get("day_high_pct")) and entry > 0:
            df.at[idx, "day_high_pct"] = round((high - entry) / entry * 100, 2)
            patched_hpct += 1

        # day_low_pct (연산값)
        if is_blank(r.get("day_low_pct")) and entry > 0:
            df.at[idx, "day_low_pct"] = round((low - entry) / entry * 100, 2)
            patched_lpct += 1

    # 9. 저장
    df.to_csv(SIGNAL_HISTORY, index=False, encoding="utf-8-sig")

    print(f"[BACKFILL] ✅ 완료")
    print(f"  day_open      : +{patched_open}")
    print(f"  day_high      : +{patched_high}")
    print(f"  day_low       : +{patched_low}")
    print(f"  day_high_pct  : +{patched_hpct}")
    print(f"  day_low_pct   : +{patched_lpct}")
    print(f"  총 {total_rows} rows 저장")


if __name__ == "__main__":
    main()
