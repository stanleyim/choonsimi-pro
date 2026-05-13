"""
RegimeEngine v7.3 — FINAL PROFIT ENGINE (WITH METADATA)
────────────────────────────────────────
✔ v7.2 안정성 유지
✔ [수정] result.json 에 data_quality, biz_day, top10 필드 추가
✔ Entry Filter (기관급 타이밍 필터) 유지
✔ top20 / core / entry_top5 분리
✔ [추가] fetch_data.py의 fetch_daily_from_kis로 history.csv 업데이트
"""

import json, math, pandas as pd, os
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

TOP_N = 20
TOP_CORE = 5

W_FLOW, W_MOM, W_VOL, W_FUND, W_NEWS = 0.35, 0.25, 0.15, 0.10, 0.15

# ─────────────────────────────
# Utils
# ─────────────────────────────
def safe_float(v, d=0.0):
    try:
        f = float(v)
        if math.isnan(f):
            return d
        return f
    except:
        return d

def tanh_norm(v):
    return (math.tanh(v) + 1) / 2

def robust_scale(v, cap=3):
    return max(-cap, min(cap, v))

def is_common_stock(name, code=""):
    if not name:
        return False
    code = str(code).zfill(6)
    if code[-1] in ("5","7","9"):
        return False
    name = str(name).upper()
    blacklist = ["ETF","ETN","KODEX","TIGER","LEVERAGE","INVERSE","지수","REIT"]
    return not any(k in name for k in blacklist)

# ─────────────────────────────
# Scorer
# ─────────────────────────────
class StockScorer:

    def __init__(self, stocks, flow, fund, news, regime):
        self.stocks = stocks
        self.flow = flow
        self.fund = fund
        self.news = news
        self.regime = regime

        self.meta = {
            str(s.get("code","")).zfill(6): {
                "vol": safe_float(s.get("volume")),
                "chg": safe_float(s.get("change_rate"))
            } for s in stocks
        }

        self.flow_map = self._build_flow()

        vols = [m["vol"] for m in self.meta.values()]
        self.vol_mean = sum(vols)/len(vols) if vols else 1
        self.vol_std = (sum((v-self.vol_mean)**2 for v in vols)/len(vols))**0.5 if vols else 1

        chgs = [m["chg"] for m in self.meta.values()]
        self.chg_mean = sum(chgs)/len(chgs) if chgs else 0
        self.chg_std = (sum((c-self.chg_mean)**2 for c in chgs)/len(chgs))**0.5 if chgs else 1

    def _build_flow(self):
        fm = {}
        for seg, w in [
            ("KOSPI_foreign",0.40),
            ("KOSPI_institution",0.25),
            ("KOSDAQ_foreign",0.20),
            ("KOSDAQ_institution",0.15)
        ]:
            for r in self.flow.get(seg, {}).get("rows", []):
                c = str(r.get("code","")).zfill(6)
                net = safe_float(r.get("net"))
                val = math.copysign(math.log1p(abs(net)), net)
                fm[c] = fm.get(c, 0) + val * w
        return fm

    def score(self, s):
        code = str(s.get("code","")).zfill(6)
        chg = safe_float(s.get("change_rate"))
        vol = safe_float(s.get("volume"))
        fd = self.fund.get(code, {})
        news = safe_float(self.news.get(code, 0))

        mom = robust_scale((chg - self.chg_mean)/(self.chg_std or 1))
        mom_score = tanh_norm(mom) * W_MOM

        vol_score = robust_scale((vol - self.vol_mean)/(self.vol_std or 1))
        vol_score = tanh_norm(vol_score) * W_VOL

        flow_raw = self.flow_map.get(code, 0)

        # divergence penalty
        flow_dir = 1 if flow_raw > 0 else -1
        price_dir = 1 if chg > 0 else -1
        penalty = 1.0
        if flow_dir!= price_dir and abs(chg) > 2 and vol < self.vol_mean * 1.5:
            penalty = 0.65

        flow_score = tanh_norm(robust_scale(flow_raw)) * W_FLOW * penalty

        fund_score = tanh_norm(safe_float(fd.get("roe")) / 15) * W_FUND
        news_score = tanh_norm(news) * W_NEWS if abs(news) > 0.2 else 0

        base = flow_score + mom_score + vol_score + fund_score + news_score

        calibrated = base * (1.0 + 0.15 * (base - 0.5))

        if safe_float(s.get("close")) > 100000:
            calibrated *= 0.92

        if self.regime == "UPTREND":
            calibrated *= 1.05
        elif self.regime == "DOWNTREND":
            calibrated *= 0.93

        return round(max(0, min(100, calibrated * 100)), 2)

    # 🔥 Entry Filter (핵심)
    def entry_filter(self, s, base_score):
        code = str(s.get("code","")).zfill(6)
        chg = safe_float(s.get("change_rate"))
        vol = safe_float(s.get("volume"))
        flow = self.flow_map.get(code, 0)

        # 1. Flow + Price
        if flow <= 0 or chg <= 0:
            return None
        # 2. Volume
        vol_ratio = vol / (self.vol_mean or 1)
        if vol_ratio < 1.3:
            return None

        # 3. Momentum range
        if chg < 0.5 or chg > 6.0:
            return None

        # 4. Hard divergence cut
        if flow * chg < 0:
            return None

        entry_score = (
            base_score * 0.6 +
            min(vol_ratio, 3) * 10 +
            min(abs(flow), 3) * 10
        )

        return round(entry_score, 2)

    def top_n(self):
        scored = [(self.score(s), s) for s in self.stocks]
        scored.sort(reverse=True, key=lambda x: x[0])

        all_top, core_top = [], []

        for i, (sc, s) in enumerate(scored[:TOP_N], 1):
            code = str(s.get("code","")).zfill(6)
            chg = safe_float(s.get("change_rate"))

            flow_q = 1.0 if self.flow_map.get(code, 0) * chg > 0 else 0.7
            exp = round((sc - 50) * 0.06 * flow_q, 2)

            item = {
                "rank": i,
                "code": code,
                "name": s.get("name",""),
                "score": sc,
                "price": int(safe_float(s.get("close"))),
                "change_pct": chg,
                "expected_return_5d": exp
            }

            all_top.append(item)

            if i <= TOP_CORE and exp > 1.5 and flow_q >= 0.8:
                core_top.append(item)
        # 🔥 Entry candidates
        entry_candidates = []
        for sc, s in scored[:TOP_N]:
            es = self.entry_filter(s, sc)
            if es:
                entry_candidates.append((es, s, sc))

        entry_candidates.sort(reverse=True, key=lambda x: x[0])

        entry_top = []
        for i, (es, s, sc) in enumerate(entry_candidates[:5], 1):
            entry_top.append({
                "rank": i,
                "code": str(s.get("code","")).zfill(6),
                "name": s.get("name",""),
                "entry_score": es,
                "base_score": sc,
                "price": int(safe_float(s.get("close"))),
                "change_pct": safe_float(s.get("change_rate"))
            })

        return {
            "top20": all_top,
            "top5_core": core_top if core_top else all_top[:3],
            "entry_top5": entry_top
        }

# ─────────────────────────────
# Engine
# ─────────────────────────────
class RegimeEngine:

    def load_json(self, f):
        try:
            with open(f, encoding="utf-8-sig") as fp:
                return json.load(fp)
        except:
            return {}

    def load_stock_data(self):
        try:
            df = pd.read_csv("history.csv", dtype={"code": str}, encoding="utf-8-sig")
            today = datetime.now(KST).strftime("%Y-%m-%d")
            return df[df["date"] == today].to_dict("records")
        except:
            return []

    def compute_regime(self, flow):
        score = sum([
            flow.get("KOSPI_foreign",{}).get("score",0),
            flow.get("KOSPI_institution",{}).get("score",0),
            flow.get("KOSDAQ_foreign",{}).get("score",0),
            flow.get("KOSDAQ_institution",{}).get("score",0),
        ]) / 4

        if score > 0.5: return "UPTREND"
        elif score < -0.5: return "DOWNTREND"
        return "SIDEWAY"

    def pre_filter(self, stocks):
        return [
            s for s in stocks
            if safe_float(s.get("volume")) > 30000
            and safe_float(s.get("close")) > 1000
            and is_common_stock(s.get("name"), s.get("code"))
        ]

    def update_history(self):
        """
        fetch_data.py의 fetch_daily_from_kis를 호출해서 history.csv 업데이트
        기존 데이터는 유지하고 당일 데이터만 append
        """
        try:
            from fetch_data import get_token, fetch_daily_from_kis, fetch_volume_rank, get_flow_codes, fetch_price
        except ImportError:
            print("[WARN] fetch_data.py import 실패. history.csv 업데이트 건너뜀")
            return

        token = get_token()
        if not token:
            print("[WARN] 토큰 발급 실패. history.csv 업데이트 건너뜀")
            return

        print("[UPDATE] history.csv 업데이트 시작")

        # 1. 오늘의 유니버스 가져오기
        rank_rows = fetch_volume_rank(token)
        known = {r["code"] for r in rank_rows}

        flow_rows = []
        for c in get_flow_codes():
            if c not in known:
                d = fetch_price(token, c)
                if d:
                    flow_rows.append(d)

        raw = pd.concat([pd.DataFrame(rank_rows), pd.DataFrame(flow_rows)], ignore_index=True)
        if raw.empty:
            print("[UPDATE] 유니버스 없음")
            return

        # 2. 유니버스의 일봉 데이터 가져오기
        daily_records = []
        for code in raw["code"].unique():
            d = fetch_daily_from_kis(token, code)
            if d:
                daily_records.append(d)

        if not daily_records:
            print("[UPDATE] 일봉 데이터 없음")
            return

        new_df = pd.DataFrame(daily_records)

        # 3. 기존 history.csv와 합치기
        try:
            old_df = pd.read_csv("history.csv", dtype={"code": str}, encoding="utf-8-sig")
            df = pd.concat([old_df, new_df], ignore_index=True)
            df = df.drop_duplicates(subset=["date", "code"], keep="last")
        except FileNotFoundError:
            df = new_df

        df.to_csv("history.csv", index=False, encoding="utf-8-sig")
        print(f"[UPDATE] history.csv 업데이트 완료: {len(new_df)} 종목 추가")

    def run(self):

        # [추가] 먼저 history.csv 업데이트
        self.update_history()

        flow = self.load_json("market_flow.json")
        news = self.load_json("news_scores.json").get("scores", {})
        fund_raw = self.load_json("fundamental.json").get("stocks", [])
        fund = {str(s.get("code","")).zfill(6): s for s in fund_raw}

        stocks = self.load_stock_data()
        regime = self.compute_regime(flow)
        stocks = self.pre_filter(stocks)

        scorer = StockScorer(stocks, flow, fund, news, regime)
        result = scorer.top_n()

        # ─────────────────────────────
        # [v7.3 수정] 메타데이터 계산
        # ─────────────────────────────

        # 1. biz_day (현재 데이터의 기준일)
        biz_day = stocks[0].get("date") if stocks else datetime.now(KST).strftime("%Y-%m-%d")

        # 2. data_quality (fundamental 데이터 유효성 체크)
        valid_fund = len([s for s in fund_raw if s.get("roe") or s.get("eps")])
        if valid_fund > 50: # 데이터가 충분하면 full
            data_quality = "full"
        elif valid_fund > 10: # 부분적이면 partial
            data_quality = "partial"
        else:
            data_quality = "sample"

        final = {
            "date": datetime.now(KST).strftime("%Y-%m-%d"),
            "biz_day": biz_day, # ✅ 추가
            "data_quality": data_quality, # ✅ 추가
            "regime": regime,
            "top10": result["top20"][:10], # ✅ 모바일용 10개 추가
            "top20": result["top20"],
            "top5_core": result["top5_core"],
            "entry_top5": result["entry_top5"]
        }

        with open("result.json", "w", encoding="utf-8-sig") as f:
            json.dump(final, f, indent=2, ensure_ascii=False)

        print(f"[DONE] {regime} | TOP20:{len(result['top20'])} | ENTRY:{len(result['entry_top5'])} | QUALITY:{data_quality}")

if __name__ == "__main__":
    RegimeEngine().run()
