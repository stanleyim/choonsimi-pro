"""
backfill_exit.py — v1.0 SIGNAL HISTORY EXIT_PRICE BACKFILL
─────────────────────────────────────────────────────────
✔ signal_history.csv의 exit_price/actual_return 빈칸 자동 채움
✔ KIS 일봉차트 API (FHKST03010100) 사용 → 다음 거래일 종가 조회
✔ 멱등성: 이미 채워진 값은 절대 덮어쓰지 않음
✔ .exit.bak 자동 백업
✔ 신호일 → 다음 거래일(주말/공휴일 스킵) → 그날의 종가를 exit로 사용
✔ actual_return = (exit - entry) / entry * 100
✔ 추가 비용 0원 (KIS 무료 API)
─────────────────────────────────────────────────────────
실행: python backfill_exit.py
필요 ENV: KIS_APP_KEY, KIS_APP_SECRET
"""

import os, json, time, shutil, requests
import pandas as pd
import holidays
from datetime import datetime, timezone, timedelta

# ── 경로 ──────────────────────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
SIGNAL_HISTORY = os.path.join(BASE_DIR, "signal_history.csv")
TOKEN_FILE     = os.path.join(BASE_DIR, "kis_token.json")

# ── KIS 상수 ──────────────────────────────────────────────
KIS_BASE     = "https://openapi.koreainvestment.com:9443"
TIMEOUT      = 10
MAX_RETRY    = 3
DELAY        = 0.2
KST          = timezone(timedelta(hours=9))
KR_HOLIDAYS  = holidays.KR(years=[2025, 2026, 2027])
MAX_GAP_DAYS = 7


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


def is_blank_or_zero(v):
    """NaN, 빈문자열, 0 → 빈칸 (exit_price는 0이 비정상)"""
    if pd.isna(v): return True
    if isinstance(v, str) and v.strip() == "": return True
    if safe_float(v) == 0: return True
    return False


def is_blank_strict(v):
    """NaN, 빈문자열만 빈칸 (actual_return은 0%가 정상값일 수 있음)"""
    if pd.isna(v): return True
    if isinstance(v, str) and v.strip() == "": return True
    return False


def get_next_trading_day(date_str):
    """다음 거래일 계산 (주말/공휴일 스킵)"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date() + timedelta(days=1)
    except:
        return None
    for _ in range(MAX_GAP_DAYS):
        if d.weekday() < 5 and d not in KR_HOLIDAYS:
            return d.strftime("%Y-%m-%d")
        d += timedelta(days=1)
    return None


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
# KIS 일봉차트 (종가 조회)
# ═══════════════════════════════════════════════════════════
def fetch_daily_close(token, code, start_date, end_date):
    """
    start_date / end_date: 'YYYY-MM-DD'
    반환: {'YYYY-MM-DD': close, ...}
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

            close_by_date = {}
            for row in output2:
                date_str = str(row.get("stck_bsop_date", ""))
                if len(date_str) != 8 or not date_str.isdigit():
                    continue
                date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                close = safe_int(row.get("stck_clpr"))
                if close > 0:
                    close_by_date[date_fmt] = close
            return close_by_date
        except Exception:
            time.sleep(DELAY)
    return {}


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    print(f"[BACKFILL_EXIT] start {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 파일 존재 확인
    if not os.path.exists(SIGNAL_HISTORY):
        print(f"[BACKFILL_EXIT] {SIGNAL_HISTORY} 없음 → 종료")
        return

    # 2. 자동 백업
    backup_path = f"{SIGNAL_HISTORY}.exit.bak"
    shutil.copy2(SIGNAL_HISTORY, backup_path)
    print(f"[BACKFILL_EXIT] 백업 생성: {backup_path}")

    # 3. CSV 로드
    df = pd.read_csv(SIGNAL_HISTORY, dtype={"code": str}, encoding="utf-8-sig")
    df["code"] = df["code"].str.zfill(6)
    total_rows = len(df)
    print(f"[BACKFILL_EXIT] 전체 {total_rows} rows 로드")

    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    # 4. exit_price 빈칸 식별 (NaN, 빈문자열, 0)
    mask = df["exit_price"].apply(is_blank_or_zero)
    blank_rows = df[mask]

    if len(blank_rows) == 0:
        print("[BACKFILL_EXIT] exit_price 빈칸 없음 → 종료")
        return

    print(f"[BACKFILL_EXIT] exit_price 빈칸: {len(blank_rows)}개")

    # 5. 각 빈칸 행에 대해 (code, signal_date, eval_day) 식별
    targets = []  # [(idx, code, signal_date, eval_day), ...]
    skipped_future = 0
    for idx, r in blank_rows.iterrows():
        code = r["code"]

        # signal_date 선정: biz_day 우선, 없으면 date
        signal_date = r.get("biz_day")
        if pd.isna(signal_date) or not str(signal_date).strip():
            signal_date = r.get("date")
        if pd.isna(signal_date) or not str(signal_date).strip():
            continue
        signal_date = str(signal_date)

        # 다음 거래일 = 평가일
        eval_day = get_next_trading_day(signal_date)
        if not eval_day:
            continue

        # 평가일이 미래면 스킵 (아직 데이터 없음)
        if eval_day > today_str:
            skipped_future += 1
            continue

        targets.append((idx, code, signal_date, eval_day))

    if skipped_future > 0:
        print(f"[BACKFILL_EXIT] 미래 평가일 스킵: {skipped_future}개")

    if not targets:
        print("[BACKFILL_EXIT] 평가 가능 행 없음 → 종료")
        return

    print(f"[BACKFILL_EXIT] 평가 대상: {len(targets)}개")

    # 6. 종목별 그룹화 (날짜 범위)
    code_to_evals = {}
    for _, code, _, eval_day in targets:
        code_to_evals.setdefault(code, set()).add(eval_day)

    print(f"[BACKFILL_EXIT] 조회 종목: {len(code_to_evals)}개")

    # 7. KIS 토큰
    token = get_token()
    if not token:
        print("[BACKFILL_EXIT] KIS 토큰 실패 → 종료")
        return

    # 8. 종목별 종가 조회 (날짜 범위로 1회 호출)
    close_lookup = {}  # {(code, eval_day): close}
    fetch_ok = fetch_fail = 0

    for code, evals in code_to_evals.items():
        evals_sorted = sorted(evals)
        result = fetch_daily_close(token, code, evals_sorted[0], evals_sorted[-1])
        if not result:
            fetch_fail += 1
            print(f"[BACKFILL_EXIT] ✗ {code} 조회 실패")
            time.sleep(DELAY)
            continue

        fetch_ok += 1
        for date in evals:
            if date in result:
                close_lookup[(code, date)] = result[date]
        time.sleep(DELAY)

    print(f"[BACKFILL_EXIT] API 조회: 성공 {fetch_ok} / 실패 {fetch_fail}")

    # 9. DataFrame 업데이트 (멱등성)
    patched_exit = 0
    patched_return = 0

    for idx, code, signal_date, eval_day in targets:
        key = (code, eval_day)
        if key not in close_lookup:
            continue

        exit_price = close_lookup[key]
        if exit_price <= 0:
            continue

        entry = safe_float(df.at[idx, "price"])
        if entry <= 0:
            continue

        # exit_price 채움 (0/빈칸만)
        if is_blank_or_zero(df.at[idx, "exit_price"]):
            df.at[idx, "exit_price"] = exit_price
            patched_exit += 1

        # actual_return 채움 (빈칸만, 0%는 정상값일 수 있음)
        if is_blank_strict(df.at[idx, "actual_return"]):
            df.at[idx, "actual_return"] = round((exit_price - entry) / entry * 100, 2)
            patched_return += 1

    # 10. 저장
    df.to_csv(SIGNAL_HISTORY, index=False, encoding="utf-8-sig")

    print(f"[BACKFILL_EXIT] ✅ 완료")
    print(f"  exit_price    : +{patched_exit}")
    print(f"  actual_return : +{patched_return}")
    print(f"  총 {total_rows} rows 저장")


if __name__ == "__main__":
    main()
