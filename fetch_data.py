"""
fetch_data.py — v5.0 MARKET HOURS GUARD ADDED
─────────────────────────────────────────────
✔ v4.9 OHL enrich 유지
✔ NEW: 장중(09:00~15:45 KST) 실행 차단
✔ 오로지 장마감 자료만 사용 원칙
✔ 기존 파일 보존 (이전 영업일 장마감 자료 유지)
─────────────────────────────────────────────
"""

import os, json, time, requests, pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

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

BLOCK_KEYWORDS = [
    "KODEX","TIGER","KBSTAR","ARIRANG","KOSEF","HANARO",
    "TIMEFOLIO","TREX","SOL","ACE","ETF","ETN",
    "레버리지","인버스","선물","REIT","리츠"
]

# ═══════════════════════════════════════════════════════
# v5.0 NEW — 장중 가드 (오로지 장마감 자료만 사용)
# ═══════════════════════════════════════════════════════
def is_market_hours() -> bool:
    """평일 09:00 ~ 15:45 KST = 정규장 시간 (장마감 데이터 미확정)"""
    now = datetime.now(KST)
    if now.weekday() >= 5:  # 토(5)/일(6) 제외
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 <= mins < 15 * 60 + 45

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
                        "code"       : code,
                        "name"       : name,
                        "open"       : safe_int(i.get("stck_oprc")),
                        "high"       : safe_int(i.get("stck_hgpr")),
                        "low"        : safe_int(i.get("stck_lwpr")),
                        "close"      : safe_int(i.get("stck_prpr")),
                        "volume"     : safe_int(i.get("acml_vol")),
                        "change_rate": safe_float(i.get("prdy_ctrt")),
                        "value"      : safe_int(i.get("acml_tr_pbmn"))
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

def fetch_price(token, code):
    if not token:
        return {}
    for _ in range(MAX_RETRY):
        try:
            r = requests.get(
                f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=headers(token, "FHKST01010100"),
                params={"FID_COND_MRKT_DIV_CODE": "J","FID_INPUT_ISCD": code},
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
                    "code"       : code,
                    "name"       : name,
                    "open"       : safe_int(o.get("stck_oprc")),
                    "high"       : safe_int(o.get("stck_hgpr")),
                    "low"        : safe_int(o.get("stck_lwpr")),
                    "close"      : close,
                    "volume"     : volume,
                    "change_rate": safe_float(o.get("prdy_ctrt")),
                    "value"      : close * volume
                }
        except:
            time.sleep(DELAY)
    return {}

# ═══════════════════════════════════════════════════════
# v4.9 — OHL ENRICH (병렬, graceful)
# volume-rank API 가 OHL 미반환 → inquire-price 로 보강
# ═══════════════════════════════════════════════════════
def enrich_ohl(token, rows):
    """volume_rank 결과 중 OHL=0 종목을 inquire-price로 보강.
    ✅ graceful: 일부 실패해도 close/volume 보존, 시스템 유지"""
    if not token or not rows:
        return rows

    targets = [r for r in rows if not r.get("open")]
    if not targets:
        print("[DATA] OHL enrich 불필요 (이미 정상)")
        return rows

    print(f"[DATA] OHL enrich 시작: {len(targets)}종목 (workers=4)")
    code_to_row = {r["code"]: r for r in rows}
    success = 0

    def _fetch(code):
        d = fetch_price(token, code)
        return code, d

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_fetch, r["code"]) for r in targets]
        for fut in as_completed(futures):
            try:
                code, d = fut.result()
                if d and d.get("open", 0) > 0:
                    row = code_to_row.get(code)
                    if row:
                        # OHL만 덮어씀 (close/volume/change_rate는 volume_rank가 더 정확)
                        row["open"] = d["open"]
                        row["high"] = d["high"]
                        row["low"]  = d["low"]
                        success += 1
            except:
                pass

    print(f"[DATA] OHL enrich 완료: {success}/{len(targets)}종목 성공")
    return rows

def main():
    # ═══════════════════════════════════════════════════
    # v5.0 GUARD — 장중(09:00~15:45) 실행 차단
    # ═══════════════════════════════════════════════════
    if is_market_hours():
        now = datetime.now(KST)
        print(f"[GUARD] 장중 실행 차단 ({now:%H:%M} KST)")
        print(f"[GUARD] 기존 history.csv 유지 (이전 영업일 장마감 자료)")
        print(f"[GUARD] 오로지 장마감 자료만 사용 원칙")
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")
    print("[START]", today)

    token = get_token()
    if not token:
        pd.DataFrame(columns=["date","code","name","open","high","low","close","volume","change_rate"])\
            .to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
        return

    rank_rows = fetch_volume_rank(token)
    rank_rows = enrich_ohl(token, rank_rows)   # v4.9: OHL 보강
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
        pd.DataFrame(columns=["date","code","name","open","high","low","close","volume","change_rate"])\
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
    result = selected[["date","code","name","open","high","low","close","volume","change_rate"]]
    result.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"[DONE] {len(result)} stocks saved")

if __name__ == "__main__":
    main()
