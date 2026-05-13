import os
import time
import json
import csv
import requests
from datetime import datetime, timedelta

KIS_APP_KEY = os.getenv("KIS_APP_KEY")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET")
KIS_BASE = "https://openapi.koreainvestment.com:9443"
TOKEN_PATH = "kis_token.json"
HISTORY_PATH = "history.csv"
RESULT_PATH = "result.json"

MAX_RETRY = 3
DELAY = 0.2
TIMEOUT = 10

def headers(token, tr_id):
    return {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id,
        "content-type": "application/json"
    }

def get_token():
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            data = json.load(f)
            if data.get("access_token_expired_at", "") > datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
                return data["access_token"]

    r = requests.post(
        f"{KIS_BASE}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET
        },
        timeout=TIMEOUT
    )
    data = r.json()
    if "access_token" not in data:
        print(f"[ERROR] token 발급 실패: {data}")
        return None

    with open(TOKEN_PATH, "w") as f:
        json.dump(data, f)
    return data["access_token"]

def safe_int(v):
    try:
        return int(str(v).replace(",", ""))
    except:
        return 0

def safe_float(v):
    try:
        return float(v)
    except:
        return 0.0

def is_common_stock(code, name):
    if code.startswith(("A", "K")):
        return False
    if "스팩" in name or "리츠" in name or "ETN" in name or "ETF" in name:
        return False
    return True

def fetch_volume_rank(token):
    if not token:
        print("[ERROR] token 없음")
        return []

    market_codes = ["0000", "0002", "0003", "0004"]
    tr_ids = ["FHPST01710000", "FHPST01700000"]
    all_rows = []

    for market_code in market_codes:
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": market_code,
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111",
            "FID_TRGT_EXLS_CLS_CODE": "0000",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0",
            "FID_INPUT_DATE_1": ""
        }

        success = False
        for tr_id in tr_ids:
            for _ in range(MAX_RETRY):
                try:
                    r = requests.get(
                        f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/volume-rank",
                        headers=headers(token, tr_id),
                        params=params,
                        timeout=TIMEOUT
                    )

                    data = r.json()
                    print(f"[DEBUG] market={market_code} tr_id={tr_id} rt_cd={data.get('rt_cd')} msg1={data.get('msg1')} keys={list(data.keys())}")

                    if data.get("rt_cd") == "0":
                        rows_raw = data.get("output") or data.get("output1") or data.get("output2") or []
                        if rows_raw:
                            for i in rows_raw:
                                code = str(i.get("mksc_shrn_iscd","")).zfill(6)
                                name = i.get("hts_kor_isnm","")
                                if not is_common_stock(code, name):
                                    continue
                                all_rows.append({
                                    "code": code,
                                    "name": name,
                                    "close": safe_int(i.get("stck_prpr")),
                                    "volume": safe_int(i.get("acml_vol")),
                                    "change_rate": safe_float(i.get("prdy_ctrt")),
                                    "value": safe_int(i.get("acml_tr_pbmn"))
                                })
                            success = True
                            break
                    else:
                        print(f" 실패: {data.get('msg1')}")
                        time.sleep(0.5)

                except Exception as e:
                    print(f"[ERROR] 예외: {e}")
                    time.sleep(DELAY)

            if success:
                break

        time.sleep(0.1)

    seen = set()
    unique_rows = []
    for r in all_rows:
        if r["code"] not in seen:
            seen.add(r["code"])
            unique_rows.append(r)

    print(f"[DATA] volume_rank raw={len(all_rows)} unique={len(unique_rows)}")
    return unique_rows

def fetch_daily_from_kis(token, code, days=30):
    for _ in range(MAX_RETRY):
        try:
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=days+5)).strftime("%Y%m%d")

            r = requests.get(
                f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                headers=headers(token, "FHKST03010100"),
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": start_date,
                    "FID_INPUT_DATE_2": end_date,
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "1"
                },
                timeout=TIMEOUT
            )
            data = r.json()
            if data.get("rt_cd") == "0":
                rows = data.get("output2", [])
                result = []
                for row in rows:
                    result.append({
                        "date": row.get("stck_bsop_date"),
                        "open": safe_int(row.get("stck_oprc")),
                        "high": safe_int(row.get("stck_hgpr")),
                        "low": safe_int(row.get("stck_lwpr")),
                        "close": safe_int(row.get("stck_clpr")),
                        "volume": safe_int(row.get("acml_vol"))
                    })
                return result
        except Exception as e:
            print(f"[ERROR] daily fetch 실패 {code}: {e}")
            time.sleep(DELAY)
    return []

def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {}
    history = {}
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row["code"]
            if code not in history:
                history[code] = []
            history[code].append({
                "date": row["date"],
                "open": int(row["open"]),
                "high": int(row["high"]),
                "low": int(row["low"]),
                "close": int(row["close"]),
                "volume": int(row["volume"])
            })
    return history

def save_history(history):
    with open(HISTORY_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["code", "date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        for code, rows in history.items():
            for r in rows:
                r["code"] = code
                writer.writerow(r)

def update_history(token, codes):
    print("[UPDATE] history.csv 업데이트 시작")
    history = load_history()
    today = datetime.now().strftime("%Y%m%d")

    for i, code in enumerate(codes):
        if i % 20 == 0:
            time.sleep(0.5)

        daily = fetch_daily_from_kis(token, code, days=30)
        if not daily:
            continue

        if code not in history:
            history[code] = []

        existing_dates = {r["date"] for r in history[code]}
        for row in daily:
            if row["date"] not in existing_dates:
                history[code].append(row)

        history[code] = sorted(history[code], key=lambda x: x["date"])[-60:]

    save_history(history)
    print(f"[UPDATE] history.csv 업데이트 완료: {len(codes)} 종목")

def get_20day_breakout(candles):
    if len(candles) < 21:
        return False, 0, 0
    recent = candles[-21:-1]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]
    high_20 = max(highs)
    low_20 = min(lows)
    today_close = candles[-1]["close"]
    return today_close > high_20, high_20, low_20

def main():
    print(f"[START] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    token = get_token()
    if not token:
        return

    volume_rank = fetch_volume_rank(token)
    if not volume_rank:
        print("[ERROR] volume_rank 0건")
        return

    codes = [r["code"] for r in volume_rank[:200]]
    update_history(token, codes)

    history = load_history()
    results = []

    for code in codes:
        candles = history.get(code, [])
        if len(candles) < 21:
            continue

        breakout, high_20, low_20 = get_20day_breakout(candles)
        if not breakout:
            continue

        today = candles[-1]
        results.append({
            "code": code,
            "name": next((r["name"] for r in volume_rank if r["code"] == code), ""),
            "close": today["close"],
            "high_20": high_20,
            "low_20": low_20,
            "volume": today["volume"],
            "change_rate": today["close"] / candles[-2]["close"] * 100 - 100 if len(candles) > 1 else 0
        })

    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"[RESULT] {len(results)} 종목 저장됨 -> {RESULT_PATH}")
    print("[END] 완료")

if __name__ == "__main__":
    main()
