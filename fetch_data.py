"""
fetch_data.py — v4.8.2 MULTI-CALL FIXED
─────────────────────────────────────────────
✔ SyntaxError 전체 수정
✔ KIS API Retry / 401 / 429 방어
✔ ETF / ETN 필터 유지
✔ Multi-market volume-rank
✔ Flow + Rank hybrid universe
✔ Production-safe env handling
─────────────────────────────────────────────
"""

import os, json, time, requests, pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

KIS_BASE = "https://openapi.koreainvestment.com:9443"
TIMEOUT = 10
DELAY = 0.25
MAX_RETRY = 3

OUTPUT_CSV = "history.csv"
TOKEN_FILE = "kis_token.json"
FLOW_FILE = "market_flow.json"

KST = timezone(timedelta(hours=9))

MAX_STOCKS = 200
RANK_RATIO = 0.75

# ───────────────── ETF / FILTER ─────────────────
BLOCK_KEYWORDS = [
    "KODEX","TIGER","KBSTAR","ARIRANG","KOSEF","HANARO",
    "TIMEFOLIO","TREX","SOL","ACE","ETF","ETN",
    "레버리지","인버스","선물","REIT","리츠"
]

def safe_float(v):
    try:
        return float(str(v).replace(",",""))
    except:
        return 0.0

def safe_int(v):
    try:
        return int(str(v).replace(",",""))
    except:
        return 0

def is_common_stock(code, name):
    code = str(code).strip()
    name = str(name or "").strip()

    if not code.isdigit() or len(code) != 6:
        return False
    if code[-1] in ("5","7","9"):
        return False
    if name.lower() in ("", "nan", "none"):
        return False
    if any(k in name.upper() for k in BLOCK_KEYWORDS):
        return False

    return True


# ───────────────── TOKEN ─────────────────
def get_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)

        issued = datetime.fromisoformat(
            data.get("issued_at","").replace("Z","") or "2000-01-01T00:00:00"
        )

        if (datetime.now(KST) - issued).seconds < 21600:
            return data.get("access_token")
    except:
        pass

    for _ in range(MAX_RETRY):
        try:
            r = requests.post(
                f"{KIS_BASE}/oauth2/tokenP",
                json={
                    "grant_type":"client_credentials",
                    "appkey":os.environ.get("KIS_APP_KEY",""),
                    "appsecret":os.environ.get("KIS_APP_SECRET","")
                },
                timeout=TIMEOUT
            )
            r.raise_for_status()
            token = r.json().get("access_token")

            with open(TOKEN_FILE,"w",encoding="utf-8-sig") as f:
                json.dump({
                    "access_token":token,
                    "issued_at":datetime.now(KST).isoformat()
                }, f)

            return token

        except:
            time.sleep(1)

    return None


def headers(token, tr_id):
    return {
        "authorization": f"Bearer {token}",
        "appkey": os.environ.get("KIS_APP_KEY",""),
        "appsecret": os.environ.get("KIS_APP_SECRET",""),
        "tr_id": tr_id,
        "content-type": "application/json",
        "custtype": "P"
    }


# ───────────────── VOLUME RANK ─────────────────
def fetch_volume_rank(token):
    if not token:
        return []

    market_codes = ["0000", "0002", "0003", "0004"]
    all_rows = []

    for market_code in market_codes:
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": market_code,
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0",
            "FID_INPUT_DATE_1": ""
        }

        for _ in range(MAX_RETRY):
            try:
                r = requests.get(
                    f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/volume-rank",
                    headers=headers(token, "FHPST01710000"),
                    params=params,
                    timeout=TIMEOUT
                )

                if r.status_code == 401:
                    return []

                r.raise_for_status()
                data = r.json()

                if data.get("rt_cd") != "0":
                    break

                rows_raw = data.get("output") or data.get("output1") or []

                if not isinstance(rows_raw, list):
                    break

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

                break

            except:
                time.sleep(DELAY)

        time.sleep(0.1)

    seen = set()
    unique_rows = []

    for r in all_rows:
        if r["code"] not in seen:
            seen.add(r["code"])
            unique_rows.append(r)

    print(f"[DATA] volume_rank raw={len(all_rows)} unique={len(unique_rows)}")
    return unique_rows


# ───────────────── FLOW ─────────────────
def get_flow_codes():
    try:
        with open(FLOW_FILE, encoding="utf-8-sig") as f:
            flow = json.load(f)

        codes = set()

        for seg in ["KOSPI_foreign","KOSPI_institution","KOSDAQ_foreign","KOSDAQ_institution"]:
            for r in flow.get(seg, {}).get("rows", []):
                c = str(r.get("code","")).zfill(6)
                if c.isdigit():
                    codes.add(c)

        return list(codes)

    except:
        return []


# ───────────────── PRICE ─────────────────
def fetch_price(token, code):
    if not token:
        return {}

    for _ in range(MAX_RETRY):
        try:
            r = requests.get(
                f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=headers(token, "FHKST01010100"),
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": code
                },
                timeout=TIMEOUT
            )

            if r.status_code == 401:
                return {}

            r.raise_for_status()
            d = r.json()

            if d.get("rt_cd") != "0":
                return {}

            o = d.get("output") or d.get("output1") or {}
            if isinstance(o, list):
                o = o[0] if o else {}

            name = o.get("hts_kor_isnm","")
            close = safe_int(o.get("stck_prpr"))
            volume = safe_int(o.get("acml_vol"))

            if is_common_stock(code, name):
                return {
                    "code": code,
                    "name": name,
                    "close": close,
                    "volume": volume,
                    "change_rate": safe_float(o.get("prdy_ctrt")),
                    "value": close * volume
                }

        except:
            time.sleep(DELAY)

    return {}


# ───────────────── MAIN ─────────────────
def main():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    print("[START]", today)

    token = get_token()

    if not token:
        pd.DataFrame(columns=["date","code","name","close","volume","change_rate"])\
            .to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
        return

    rank_rows = fetch_volume_rank(token)
    known = {r["code"] for r in rank_rows}

    flow_rows = []

    for c in get_flow_codes():
        if c not in known:
            d = fetch_price(token, c)
            if d:
                flow_rows.append(d)
            time.sleep(DELAY)

    raw = pd.concat(
        [pd.DataFrame(rank_rows), pd.DataFrame(flow_rows)],
        ignore_index=True
    ).drop_duplicates("code")

    if raw.empty:
        pd.DataFrame(columns=["date","code","name","close","volume","change_rate"])\
            .to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
        return

    raw["value_score"] = np.log1p(raw["close"] * raw["volume"])

    df_rank = raw[raw["code"].isin(known)]
    df_flow = raw[~raw["code"].isin(known)]

    rank_limit = int(MAX_STOCKS * RANK_RATIO)
    flow_limit = MAX_STOCKS - rank_limit

    selected = pd.concat([
        df_rank.nlargest(rank_limit, "value_score"),
        df_flow.nlargest(flow_limit, "value_score")
    ]).drop_duplicates("code")

    try:
        excluded = raw[~raw["code"].isin(selected["code"])]
        if len(excluded) > 0:
            excluded.sample(min(30, len(excluded)), random_state=42)\
                .to_csv("shadow_universe.csv", index=False, encoding="utf-8-sig")
    except:
        pass

    selected["date"] = today
    selected["code"] = selected["code"].astype(str).str.zfill(6)

    result = selected[["date","code","name","close","volume","change_rate"]]

    result.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"[DONE] {len(result)} stocks saved")


if __name__ == "__main__":
    main()
