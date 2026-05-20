"""
engine.py — v7.3.3 CHOONSIMI PRO CORE
─────────────────────────────────────────────────────────
✔ PRO_MODE=true  → Pro 확신 필터 적용 (0~10개, 기준 충족 시만)
✔ PRO_MODE=false → 일반 모드 (기존 top20/core/entry)
✔ v7.3 스코어링 유지 (log1p, divergence penalty, calibration)
✔ BASE_DIR 경로 수정 (engine/engine.py → repo root)
✔ KIS 실시간 가격 주입
✔ confidence / universe_size / biz_day / data_quality
✔ verify() 주말/공휴일 자동 스킵
✔ performance_today
✔ signal_history.csv 신규 생성
✔ day_open=0 버그 수정 (장 시작 직후 0값 → 빈값으로 통일)
✔ [v7.3.3] fetch_price_kis OHLC 필드 추가 (open/high/low)
✔ [v7.3.3] save_signal_history에서 KIS로 OHLC 자동 보완
─────────────────────────────────────────────────────────
"""

import os, json, math, time
import pandas as pd
import requests
import holidays
from datetime import datetime, timezone, timedelta

# ── 경로 (engine/engine.py → repo root) ──────────────────
BASE_DIR       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNAL_HISTORY = os.path.join(BASE_DIR, "signal_history.csv")
HISTORY_CSV    = os.path.join(BASE_DIR, "history.csv")
RESULT_FILE    = os.path.join(BASE_DIR, "result.json")
FUND_FILE      = os.path.join(BASE_DIR, "fundamental.json")
FLOW_FILE      = os.path.join(BASE_DIR, "market_flow.json")
NEWS_FILE      = os.path.join(BASE_DIR, "news_scores.json")
TOKEN_FILE     = os.path.join(BASE_DIR, "kis_token.json")

# ── PRO_MODE 감지 ─────────────────────────────────────────
PRO_MODE = os.environ.get("PRO_MODE", "false").lower() == "true"

# ── 상수 ─────────────────────────────────────────────────
KST          = timezone(timedelta(hours=9))
KR_HOLIDAYS  = holidays.KR(years=[2025, 2026, 2027])
MAX_GAP_DAYS = 7
KIS_BASE     = "https://openapi.koreainvestment.com:9443"
TIMEOUT      = 10
MAX_RETRY    = 3
DELAY        = 0.2

TOP_N    = 20
TOP_CORE = 5
ENTRY_N  = 5

W_FLOW, W_MOM, W_VOL, W_FUND, W_NEWS = 0.35, 0.25, 0.15, 0.10, 0.15

BLOCK_KW = [
    "KODEX","TIGER","KBSTAR","ARIRANG","KOSEF","HANARO",
    "TIMEFOLIO","TREX","SOL","ACE","ETF","ETN",
    "레버리지","인버스","선물","REIT","리츠","INDEX","지수"
]

# ── Pro 확신 필터 기준 ────────────────────────────────────
PRO_FILTER = {
    "min_score"       : 75.0,   # AI 점수 75점 이상
    "min_flow"        : 0.0,    # 수급 반드시 양수 (외국인+기관 순매수)
    "chg_min"         : 0.3,    # 모멘텀 최소 0.3%
    "chg_max"         : 7.0,    # 모멘텀 최대 7.0% (과열 제외)
    "min_vol_ratio"   : 1.2,    # 거래량 평균 대비 1.2배 이상
    "min_exp_return"  : 1.0,    # 5일 예상 수익 1.0% 이상
}

# ── signal_history 컬럼 정의 ─────────────────────────────
SIG_COLS = [
    "date", "biz_day", "regime", "mode",
    "rank", "code", "name",
    "score", "price", "change_pct",
    "expected_return_5d", "entry_score",
    "day_open", "day_high", "day_low",      # 당일 시가/고가/저가
    "exit_price", "actual_return",           # verify() 실행 후 자동 기록
    "day_high_pct", "day_low_pct"            # 진입가 대비 고가/저가 %
]

# ── "신호 없음" 문구 ──────────────────────────────────────
NO_SIGNAL = {
    "signal": False,
    "message_ko": "⚠ 오늘 진입 신호 없음",
    "message_sub": "시장 조건 미충족 — 현금 보유 권고"
}


# ═══════════════════════════════════════════════════════
# UTILS
# ═══════════════════════════════════════════════════════
def safe_float(v, d=0.0):
    try:
        f = float(v)
        return d if math.isnan(f) else f
    except:
        return d

def safe_int(v, d=0):
    try:
        return int(str(v).replace(",", ""))
    except:
        return d

def tanh_norm(v):
    return (math.tanh(v) + 1) / 2

def robust_scale(v, cap=3):
    return max(-cap, min(cap, v))

def is_common_stock(code, name=""):
    code = str(code).strip()
    name = str(name or "").strip().upper()
    if not code.isdigit() or len(code) != 6: return False
    if code[-1] in ("5", "7", "9"):          return False
    if name in ("", "NAN", "NONE"):           return False
    return not any(k in name for k in BLOCK_KW)

def get_next_trading_day(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d").date() + timedelta(days=1)
    for _ in range(MAX_GAP_DAYS):
        if d.weekday() < 5 and d not in KR_HOLIDAYS:
            return d.strftime("%Y-%m-%d")
        d += timedelta(days=1)
    return None

def load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except:
        return {}


# ═══════════════════════════════════════════════════════
# KIS TOKEN / PRICE
# ═══════════════════════════════════════════════════════
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

def fetch_price_kis(token, code):
    """[v7.3.3] OHLC 필드 추가 (open/high/low)"""
    if not token: return {}
    for _ in range(MAX_RETRY):
        try:
            r = requests.get(
                f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=kis_headers(token, "FHKST01010100"),
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
                timeout=TIMEOUT
            )
            if r.status_code == 401: return {}
            r.raise_for_status()
            d = r.json()
            if d.get("rt_cd") != "0": return {}
            o = d.get("output") or d.get("output1") or {}
            if isinstance(o, list): o = o[0] if o else {}
            return {
                "open"       : safe_int(o.get("stck_oprc")),
                "high"       : safe_int(o.get("stck_hgpr")),
                "low"        : safe_int(o.get("stck_lwpr")),
                "close"      : safe_int(o.get("stck_prpr")),
                "volume"     : safe_int(o.get("acml_vol")),
                "change_rate": safe_float(o.get("prdy_ctrt")),
            }
        except:
            time.sleep(DELAY)
    return {}

def enrich_with_kis(stocks, token):
    if not token: return stocks
    enriched, patched = [], 0
    for s in stocks:
        if safe_float(s.get("volume")) == 0 or safe_float(s.get("change_rate")) == 0:
            code = str(s.get("code", "")).zfill(6)
            p    = fetch_price_kis(token, code)
            if p and p.get("close", 0) > 0:
                s = {**s, **p}
                patched += 1
            time.sleep(DELAY)
        enriched.append(s)
    print(f"[KIS] {patched}종목 패치 / {len(enriched)}종목")
    return enriched


# ═══════════════════════════════════════════════════════
# DATA LOAD
# ═══════════════════════════════════════════════════════
def load_stock_data(today):
    try:
        df = pd.read_csv(HISTORY_CSV, dtype={"code": str}, encoding="utf-8-sig")
        df["code"]  = df["code"].str.zfill(6)
        df["date"]  = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        latest      = df[df["date"] == today]

        if latest.empty:
            latest = df[df["date"] == df["date"].max()]
            print(f"[DATA] fallback → {df['date'].max()}")

        biz_day = latest["date"].iloc[0] if not latest.empty else today
        print(f"[DATA] date={biz_day} rows={len(latest)}")
        return latest.to_dict("records"), biz_day
    except Exception as e:
        print(f"[DATA ERROR] {e}")
        return [], today

def load_fundamental():
    raw = load_json(FUND_FILE)
    items = raw if isinstance(raw, list) else raw.get("stocks", [])
    return items, {str(s.get("code", "")).zfill(6): s for s in items}


# ═══════════════════════════════════════════════════════
# REGIME
# ═══════════════════════════════════════════════════════
def compute_regime(flow):
    segs   = ["KOSPI_foreign","KOSPI_institution","KOSDAQ_foreign","KOSDAQ_institution"]
    scores = [flow.get(s, {}).get("score", 0) for s in segs]
    valid  = [s for s in scores if s != 0]
    fs     = max(-1.0, min(1.0, sum(valid) / len(valid) if valid else 0))

    if   fs >  0.3: return "UPTREND",   round(abs(fs), 2)
    elif fs < -0.3: return "DOWNTREND", round(abs(fs), 2)
    else:           return "SIDEWAY",   0.50


# ═══════════════════════════════════════════════════════
# PRE-FILTER
# ═══════════════════════════════════════════════════════
def pre_filter(stocks, regime):
    cfg = {
        "UPTREND"  : (30000, 1000),
        "SIDEWAY"  : (20000, 1000),
        "DOWNTREND": (40000, 2000),
    }.get(regime, (20000, 1000))
    min_vol, min_price = cfg

    filtered = [
        s for s in stocks
        if safe_float(s.get("volume"))  > 0
        and safe_float(s.get("volume")) >= min_vol
        and safe_float(s.get("close"))  >= min_price
        and is_common_stock(s.get("code",""), s.get("name",""))
    ]

    if len(filtered) < 10:
        filtered = [
            s for s in stocks
            if safe_float(s.get("volume"))  > 0
            and safe_float(s.get("volume")) >= min_vol   * 0.5
            and safe_float(s.get("close"))  >= min_price * 0.5
            and is_common_stock(s.get("code",""), s.get("name",""))
        ]
        print(f"[FILTER] 폴백 적용 → {len(filtered)}종목")

    return filtered


# ═══════════════════════════════════════════════════════
# SCORER (v7.3 로직)
# ═══════════════════════════════════════════════════════
class StockScorer:

    def __init__(self, stocks, flow, fund, news, regime):
        self.stocks = stocks
        self.fund   = fund
        self.news   = news
        self.regime = regime

        self.flow_map = {}
        for seg, w in [
            ("KOSPI_foreign",     0.40),
            ("KOSPI_institution", 0.25),
            ("KOSDAQ_foreign",    0.20),
            ("KOSDAQ_institution",0.15)
        ]:
            for r in flow.get(seg, {}).get("rows", []):
                c   = str(r.get("code","")).zfill(6)
                net = safe_float(r.get("net"))
                val = math.copysign(math.log1p(abs(net)), net)
                self.flow_map[c] = self.flow_map.get(c, 0) + val * w

        vols = [safe_float(s.get("volume"))      for s in stocks]
        chgs = [safe_float(s.get("change_rate")) for s in stocks]
        self.vol_mean = sum(vols)/len(vols) if vols else 1
        self.vol_std  = (sum((v-self.vol_mean)**2 for v in vols)/len(vols))**0.5 if len(vols)>1 else 1
        self.chg_mean = sum(chgs)/len(chgs) if chgs else 0
        self.chg_std  = (sum((c-self.chg_mean)**2 for c in chgs)/len(chgs))**0.5 if len(chgs)>1 else 1

    def score(self, s):
        code = str(s.get("code","")).zfill(6)
        chg  = safe_float(s.get("change_rate"))
        vol  = safe_float(s.get("volume"))
        fd   = self.fund.get(code, {})
        news = safe_float(self.news.get(code, 0))

        mom       = robust_scale((chg - self.chg_mean) / (self.chg_std or 1))
        mom_score = tanh_norm(mom) * W_MOM

        vol_z     = robust_scale((vol - self.vol_mean) / (self.vol_std or 1))
        vol_score = tanh_norm(vol_z) * W_VOL

        flow_raw  = self.flow_map.get(code, 0)
        flow_dir  = 1 if flow_raw > 0 else -1
        price_dir = 1 if chg > 0 else -1
        penalty   = 0.65 if (flow_dir != price_dir and abs(chg) > 2 and vol < self.vol_mean * 1.5) else 1.0
        flow_sc   = tanh_norm(robust_scale(flow_raw)) * W_FLOW * penalty

        fund_sc = tanh_norm(safe_float(fd.get("roe")) / 15) * W_FUND
        news_sc = tanh_norm(news) * W_NEWS if abs(news) > 0.2 else 0

        base       = flow_sc + mom_score + vol_score + fund_sc + news_sc
        calibrated = base * (1.0 + 0.15 * (base - 0.5))

        if safe_float(s.get("close")) > 100000:
            calibrated *= 0.92

        if self.regime == "UPTREND":
            calibrated *= 1.05
        elif self.regime == "DOWNTREND":
            calibrated *= 0.93

        return round(max(0, min(100, calibrated * 100)), 2)

    def entry_filter(self, s, base_score):
        code      = str(s.get("code","")).zfill(6)
        chg       = safe_float(s.get("change_rate"))
        vol       = safe_float(s.get("volume"))
        flow      = self.flow_map.get(code, 0)
        vol_ratio = vol / (self.vol_mean or 1)

        if flow <= 0 or chg <= 0:   return None
        if vol_ratio < 1.3:         return None
        if not (0.5 <= chg <= 6.0): return None
        if flow * chg < 0:          return None

        return round(base_score * 0.6 + min(vol_ratio, 3) * 10 + min(abs(flow), 3) * 10, 2)

    # ── Pro 확신 필터 ─────────────────────────────────────
    def pro_filter(self, s, sc, exp):
        """
        기준 전부 충족해야 Pro 종목으로 선정.
        기준 미달 시 None 반환 → 억지로 채우지 않음.
        """
        code      = str(s.get("code","")).zfill(6)
        chg       = safe_float(s.get("change_rate"))
        vol       = safe_float(s.get("volume"))
        flow      = self.flow_map.get(code, 0)
        vol_ratio = vol / (self.vol_mean or 1)

        f = PRO_FILTER
        if sc        < f["min_score"]:      return None  # AI 점수
        if flow      <= f["min_flow"]:      return None  # 수급 양수 필수
        if chg       < f["chg_min"]:        return None  # 모멘텀 최소
        if chg       > f["chg_max"]:        return None  # 과열 제외
        if vol_ratio < f["min_vol_ratio"]:  return None  # 거래량
        if exp       < f["min_exp_return"]: return None  # 예상 수익

        return round(sc * 0.7 + min(vol_ratio, 3) * 8 + min(abs(flow), 3) * 8 + exp * 2, 2)

    def top_n(self):
        scored = [(self.score(s), s) for s in self.stocks]
        scored.sort(reverse=True, key=lambda x: x[0])

        all_top, core_top = [], []

        for i, (sc, s) in enumerate(scored[:TOP_N], 1):
            code = str(s.get("code","")).zfill(6)
            fd   = self.fund.get(code, {})
            chg  = safe_float(s.get("change_rate"))
            fq   = 1.0 if self.flow_map.get(code, 0) * chg > 0 else 0.7
            exp  = round((sc - 50) * 0.06 * fq, 2)

            item = {
                "rank"              : i,
                "code"              : code,
                "name"              : s.get("name",""),
                "score"             : sc,
                "price"             : int(safe_float(s.get("close"))),
                "change_pct"        : chg,
                "expected_return_5d": exp,
                "roe"               : fd.get("roe"),
                "debt_ratio"        : fd.get("debt_ratio"),
            }
            all_top.append(item)

            if i <= TOP_CORE and exp > 1.5 and fq >= 0.8:
                core_top.append(item)

        # entry top5 (일반 모드)
        entry_candidates = []
        for sc, s in scored[:TOP_N]:
            es = self.entry_filter(s, sc)
            if es:
                entry_candidates.append((es, s, sc))
        entry_candidates.sort(reverse=True, key=lambda x: x[0])

        entry_top = []
        for i, (es, s, sc) in enumerate(entry_candidates[:ENTRY_N], 1):
            code = str(s.get("code","")).zfill(6)
            fd   = self.fund.get(code, {})
            chg  = safe_float(s.get("change_rate"))
            fq   = 1.0 if self.flow_map.get(code, 0) * chg > 0 else 0.7
            exp  = round((sc - 50) * 0.06 * fq, 2)
            entry_top.append({
                "rank"              : i,
                "code"              : code,
                "name"              : s.get("name",""),
                "score"             : sc,
                "entry_score"       : es,
                "price"             : int(safe_float(s.get("close"))),
                "change_pct"        : chg,
                "expected_return_5d": exp,
                "roe"               : fd.get("roe"),
                "debt_ratio"        : fd.get("debt_ratio"),
            })

        # ── Pro 확신 종목 선정 ────────────────────────────
        pro_candidates = []
        for sc, s in scored:
            code = str(s.get("code","")).zfill(6)
            fd   = self.fund.get(code, {})
            chg  = safe_float(s.get("change_rate"))
            fq   = 1.0 if self.flow_map.get(code, 0) * chg > 0 else 0.7
            exp  = round((sc - 50) * 0.06 * fq, 2)
            ps   = self.pro_filter(s, sc, exp)
            if ps:
                pro_candidates.append((ps, sc, s, exp, fd))

        pro_candidates.sort(reverse=True, key=lambda x: x[0])

        pro_top = []
        for i, (ps, sc, s, exp, fd) in enumerate(pro_candidates[:10], 1):
            code = str(s.get("code","")).zfill(6)
            pro_top.append({
                "rank"              : i,
                "code"              : code,
                "name"              : s.get("name",""),
                "score"             : sc,
                "pro_score"         : ps,
                "price"             : int(safe_float(s.get("close"))),
                "change_pct"        : safe_float(s.get("change_rate")),
                "expected_return_5d": exp,
                "roe"               : fd.get("roe"),
                "debt_ratio"        : fd.get("debt_ratio"),
            })

        return {
            "top20"     : all_top,
            "top5_core" : core_top if core_top else all_top[:3],
            "entry_top5": entry_top,
            "pro_top"   : pro_top,   # 0~10개, 기준 충족 시만
        }


# ═══════════════════════════════════════════════════════
# VERIFY
# ═══════════════════════════════════════════════════════
def verify(today):
    try:
        hist = pd.read_csv(HISTORY_CSV, dtype={"code": str}, encoding="utf-8-sig")
        hist["code"]  = hist["code"].str.zfill(6)
        hist["date"]  = pd.to_datetime(hist["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        hist["close"] = pd.to_numeric(hist["close"], errors="coerce")

        sig        = pd.read_csv(SIGNAL_HISTORY, dtype={"code": str}, encoding="utf-8-sig")
        prev_dates = sorted([d for d in sig["date"].dropna().unique() if d < today])

        if not prev_dates:
            print("[VERIFY] 이전 신호 없음 — 데이터 축적 중")
            return {"win_rate": 0, "avg_return": 0, "top5_return": 0}

        y        = prev_dates[-1]
        eval_day = get_next_trading_day(y)
        eval_day = eval_day if (eval_day and eval_day <= today) else today

        hist_eval = hist[hist["date"] == eval_day]
        if hist_eval.empty:
            print(f"[VERIFY] {eval_day} 데이터 없음")
            return {"win_rate": 0, "avg_return": 0, "top5_return": 0}

        price_map = {
            k: v for k, v in zip(hist_eval["code"], hist_eval["close"])
            if pd.notna(v) and float(v) > 0
        }

        sig_y = sig[sig["date"] == y]
        print(f"[VERIFY] 신호일:{y} → 평가일:{eval_day} | {len(sig_y)}종목")

        hits, total, avg_sum, top5 = 0, len(sig_y), 0.0, []
        updated_rows = []

        for _, r in sig_y.iterrows():
            code  = str(r["code"]).zfill(6)
            entry = safe_float(r.get("price"))
            exitp = price_map.get(code, 0)
            row   = r.to_dict()

            if entry > 0 and exitp > 0:
                ret = round((exitp - entry) / entry * 100, 2)
                avg_sum += ret
                if ret > 0: hits += 1
                if safe_float(r.get("rank", 999)) <= 5:
                    top5.append(ret)
                # exit_price / actual_return 기록
                row["exit_price"]    = int(exitp)
                row["actual_return"] = ret

            updated_rows.append(row)

        # signal_history에 exit_price / actual_return 업데이트
        if updated_rows:
            try:
                updated_df = pd.DataFrame(updated_rows)
                rest = sig[sig["date"] != y]
                merged = pd.concat([rest, updated_df], ignore_index=True)
                # 컬럼 순서 유지
                for col in ["exit_price", "actual_return"]:
                    if col not in merged.columns:
                        merged[col] = ""
                merged.to_csv(SIGNAL_HISTORY, index=False, encoding="utf-8-sig")
                print("[VERIFY] signal_history 업데이트 완료")
            except Exception as e:
                print(f"[VERIFY] signal_history 업데이트 실패: {e}")

        return {
            "win_rate"   : round(hits / total * 100, 1)    if total else 0,
            "avg_return" : round(avg_sum / total, 2)        if total else 0,
            "top5_return": round(sum(top5) / len(top5), 2)  if top5  else 0,
        }
    except Exception as e:
        print(f"[VERIFY ERROR] {e}")
        return {"win_rate": 0, "avg_return": 0, "top5_return": 0}


# ═══════════════════════════════════════════════════════
# PRO 당일 성과 검증
# 전일 pro_top 진입가 → 오늘 종가 비교
# ═══════════════════════════════════════════════════════
def verify_pro(today):
    try:
        # 오늘 종가 맵
        hist = pd.read_csv(HISTORY_CSV, dtype={"code": str}, encoding="utf-8-sig")
        hist["code"]  = hist["code"].str.zfill(6)
        hist["date"]  = pd.to_datetime(hist["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        hist["close"] = pd.to_numeric(hist["close"], errors="coerce")

        hist_today = hist[hist["date"] == today]
        if hist_today.empty:
            return None

        price_map = {
            k: float(v) for k, v in zip(hist_today["code"], hist_today["close"])
            if pd.notna(v) and float(v) > 0
        }

        # 전일 signal_history에서 pro 종목 찾기
        sig = pd.read_csv(SIGNAL_HISTORY, dtype={"code": str}, encoding="utf-8-sig")
        sig["code"] = sig["code"].str.zfill(6)

        prev_dates = sorted([d for d in sig["date"].dropna().unique() if d < today])
        if not prev_dates:
            return None

        y     = prev_dates[-1]
        sig_y = sig[(sig["date"] == y) & (sig["mode"] == "pro")] if "mode" in sig.columns else sig[sig["date"] == y]

        if sig_y.empty:
            return None

        details    = []
        win_count  = 0
        avg_sum    = 0.0
        evaluated  = 0

        for _, r in sig_y.iterrows():
            code  = str(r["code"]).zfill(6)
            name  = r.get("name", "")
            entry = safe_float(r.get("price"))
            exitp = price_map.get(code, 0)

            if entry <= 0 or exitp <= 0:
                continue

            evaluated += 1
            ret = round((exitp - entry) / entry * 100, 2)
            avg_sum += ret
            hit = ret > 0
            if hit:
                win_count += 1

            details.append({
                "rank"      : int(safe_float(r.get("rank", 0))),
                "code"      : code,
                "name"      : name,
                "entry"     : int(entry),
                "exit"      : int(exitp),
                "return_pct": ret,
                "result"    : "✅ 상승" if hit else "❌ 하락",
            })

        if evaluated == 0:
            return None

        details.sort(key=lambda x: x["rank"])

        print(f"[PRO VERIFY] 기준일:{y} | 평가:{evaluated}종목 | 승률:{round(win_count/evaluated*100,1)}%")

        return {
            "signal_date": y,
            "eval_date"  : today,
            "evaluated"  : evaluated,
            "win_count"  : win_count,
            "win_rate"   : round(win_count / evaluated * 100, 1),
            "avg_return" : round(avg_sum / evaluated, 2),
            "details"    : details,
        }

    except Exception as e:
        print(f"[PRO VERIFY ERROR] {e}")
        return None


# ═══════════════════════════════════════════════════════
# SIGNAL HISTORY SAVE
# ═══════════════════════════════════════════════════════
def save_signal_history(top20, entry_top5, regime, today, biz_day, token=None):
    """[v7.3.3] token 추가 → KIS inquire-price로 OHLC 자동 보완"""
    mode      = "pro" if PRO_MODE else "standard"
    entry_map = {e["code"]: e.get("entry_score") for e in entry_top5}

    # 1단계: history.csv에서 당일 OHLC 읽기
    ohlc_map = {}
    try:
        hist = pd.read_csv(HISTORY_CSV, dtype={"code": str}, encoding="utf-8-sig")
        hist["code"] = hist["code"].str.zfill(6)
        hist["date"] = pd.to_datetime(hist["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        day_hist = hist[hist["date"] == biz_day]
        for _, r in day_hist.iterrows():
            ohlc_map[r["code"]] = {
                "open": safe_int(r.get("open",  0)),
                "high": safe_int(r.get("high",  0)),
                "low" : safe_int(r.get("low",   0)),
            }
    except Exception as e:
        print(f"[SIGNAL] OHLC CSV 로드 실패: {e}")

    # 2단계: OHLC=0이거나 누락된 top20만 KIS inquire-price로 보완
    if token:
        patched = 0
        for t in top20:
            code = str(t["code"]).zfill(6)
            ohlc = ohlc_map.get(code, {})
            if ohlc.get("high", 0) > 0 and ohlc.get("low", 0) > 0:
                continue
            p = fetch_price_kis(token, code)
            if p and p.get("high", 0) > 0:
                ohlc_map[code] = {
                    "open": p.get("open", 0),
                    "high": p.get("high", 0),
                    "low" : p.get("low",  0),
                }
                patched += 1
            time.sleep(DELAY)
        print(f"[SIGNAL] KIS OHLC 보완: {patched}종목")

    rows = []
    for t in top20:
        code  = str(t["code"]).zfill(6)
        ohlc  = ohlc_map.get(code, {})
        entry = t["price"]
        high  = ohlc.get("high", 0)
        low   = ohlc.get("low",  0)

        rows.append({
            "date"              : today,
            "biz_day"           : biz_day,
            "regime"            : regime,
            "mode"              : mode,
            "rank"              : t["rank"],
            "code"              : code,
            "name"              : t["name"],
            "score"             : t["score"],
            "price"             : entry,
            "change_pct"        : t["change_pct"],
            "expected_return_5d": t.get("expected_return_5d", 0),
            "entry_score"       : entry_map.get(code, ""),
            "day_open"          : ohlc.get("open", "") if ohlc.get("open") else "",
            "day_high"          : high if high else "",
            "day_low"           : low  if low  else "",
            "exit_price"        : "",
            "actual_return"     : "",
            "day_high_pct"      : round((high - entry) / entry * 100, 2) if high and entry else "",
            "day_low_pct"       : round((low  - entry) / entry * 100, 2) if low  and entry else "",
        })

    df = pd.DataFrame(rows, columns=SIG_COLS)

    try:
        old = pd.read_csv(SIGNAL_HISTORY, dtype={"code": str}, encoding="utf-8-sig")
        old["code"] = old["code"].str.zfill(6)
        if "date" in old.columns:
            old = old[old["date"] != today]
        df = pd.concat([old, df], ignore_index=True)
    except:
        pass

    df.to_csv(SIGNAL_HISTORY, index=False, encoding="utf-8-sig")
    print(f"[SIGNAL] 저장: {len(df)} rows")


# ═══════════════════════════════════════════════════════
# DATA QUALITY
# ═══════════════════════════════════════════════════════
def calc_data_quality(fund_raw):
    valid = len([s for s in fund_raw if s.get("roe") or s.get("eps")])
    if valid > 50:  return "full"
    if valid > 10:  return "partial"
    return "sample"


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════
def run():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    mode  = "PRO" if PRO_MODE else "STANDARD"
    print(f"[START] engine v7.3.3 Choonsimi Core  {today}  MODE={mode}")

    stocks, biz_day = load_stock_data(today)
    if not stocks:
        print("[NO DATA] 종료")
        return

    flow      = load_json(FLOW_FILE)
    news      = load_json(NEWS_FILE).get("scores", {})
    fund_raw, fund = load_fundamental()

    token = get_token()
    if token:
        stocks = enrich_with_kis(stocks, token)

    regime, confidence = compute_regime(flow)
    print(f"[REGIME] {regime}  confidence={confidence}")

    filtered = pre_filter(stocks, regime)
    print(f"[UNIVERSE] {len(filtered)}종목")
    if not filtered:
        print("[WARN] 필터 후 종목 없음")
        return

    scorer = StockScorer(filtered, flow, fund, news, regime)
    result = scorer.top_n()

    perf     = verify(today)
    pro_perf = verify_pro(today) if PRO_MODE else None

    save_signal_history(
        result["top20"], result["entry_top5"],
        regime, today, biz_day, token=token
    )

    data_quality = calc_data_quality(fund_raw)

    # ── Pro 신호 없음 처리 ─────────────────────────────
    pro_top      = result["pro_top"]
    pro_signal   = len(pro_top) > 0
    pro_no_signal = NO_SIGNAL if not pro_signal else None

    print(f"[PRO] 확신 종목: {len(pro_top)}개 {'✅' if pro_signal else '⚠ 신호 없음'}")

    final = {
        "date"             : today,
        "biz_day"          : biz_day,
        "run_at"           : datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "mode"             : mode,
        "regime"           : regime,
        "confidence"       : confidence,
        "universe_size"    : len(filtered),
        "data_quality"     : data_quality,
        "top10"            : result["top20"][:10],
        "top20"            : result["top20"],
        "top5_core"        : result["top5_core"],
        "entry_top5"       : result["entry_top5"],
        "pro_top"          : pro_top,
        "pro_signal"       : pro_signal,
        "pro_no_signal"    : pro_no_signal,
        "performance_today": perf,
        "pro_performance"  : pro_perf,
    }

    with open(RESULT_FILE, "w", encoding="utf-8-sig") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)

    pro_perf_str = f"PRO승률:{pro_perf['win_rate']}%" if pro_perf else "PRO검증:축적중"
    print(f"[DONE] {regime} | TOP20:{len(result['top20'])} | PRO:{len(pro_top)} | WIN:{perf['win_rate']}% | {pro_perf_str} | QUALITY:{data_quality}")


if __name__ == "__main__":
    run()
