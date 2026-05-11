"""
RegimeEngine v7.1 — REAL PROFIT FINAL (PATCHED)
──────────────────────────────────────────────────────────
✔ v7.0 수익 최적화 로직 유지 (Divergence, Calibration, Core Filter)
✔ CRITICAL FIX: safe_float NaN 방어 (Pandas 빈 셀 처리)
✔ CRITICAL FIX: pro_data 로직 복원 (Pro 분석 활성화)
✔ 실행 안정성: ZeroDivision, KeyError 완전 방어
──────────────────────────────────────────────────────────
"""

import json, math, pandas as pd, os
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

SIGNAL_HISTORY = "signal_history.csv"
RESULT_FILE = "result.json"

TOP_N = 20
TOP_CORE = 5

# 실전 최적화 가중치
W_FLOW, W_MOM, W_VOL, W_FUND, W_NEWS = 0.35, 0.25, 0.15, 0.10, 0.15


# ─────────────────────────────
# 기본 유 (CRITICAL FIX: NaN 방어)
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


# ─────────────────────────────
# 종목 필터 (FIXED)
# ─────────────────────────────
def is_common_stock(name, code=""):
    if not name:
        return False
    code = str(code).zfill(6)
    if code[-1] in ("5", "7", "9"):
        return False
    name = str(name).upper()
    blacklist = ["ETF","ETN","KODEX","TIGER","LEVERAGE","INVERSE","지수","REIT"]
    return not any(k in name for k in blacklist)


# ─────────────────────────────
# 스코어링 엔진
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

        vols = [self.meta[c]["vol"] for c in self.meta]
        self.vol_mean = sum(vols)/len(vols) if vols else 1
        self.vol_std = (sum((v-self.vol_mean)**2 for v in vols)/len(vols))**0.5 if vols else 1

        chgs = [self.meta[c]["chg"] for c in self.meta]
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

        flow_dir = 1 if flow_raw > 0 else -1
        price_dir = 1 if chg > 0 else -1
        divergence_penalty = 1.0

        if flow_dir != price_dir and abs(chg) > 2.0 and vol < self.vol_mean * 1.5:
            divergence_penalty = 0.65

        flow_score = tanh_norm(robust_scale(flow_raw)) * W_FLOW * divergence_penalty

        fund_score = tanh_norm(safe_float(fd.get("roe")) / 15) * W_FUND
        news_score = tanh_norm(news) * W_NEWS if abs(news) > 0.2 else 0

        base_score = flow_score + mom_score + vol_score + fund_score + news_score

        calibrated = base_score * (1.0 + 0.15 * (base_score - 0.5))

        if safe_float(s.get("close")) > 100000:
            calibrated *= 0.92

        if self.regime == "UPTREND":
            calibrated *= 1.05
        elif self.regime == "DOWNTREND":
            calibrated *= 0.93

        return round(max(0, min(100, calibrated * 100)), 2)

    def top_n(self):
        scored = [(self.score(s), s) for s in self.stocks]
        scored.sort(reverse=True, key=lambda x: x[0])

        all_top = []
        core_top = []

        for i, (sc, s) in enumerate(scored[:TOP_N], 1):
            code = str(s.get("code","")).zfill(6)
            chg = safe_float(s.get("change_rate"))

            flow_q = 1.0 if self.flow_map.get(code, 0) * chg > 0 else 0.7
            expected_return = round((sc - 50) * 0.06 * flow_q, 2)

            item = {
                "rank": i,
                "code": code,
                "name": s.get("name",""),
                "score": sc,
                "price": int(safe_float(s.get("close"))),
                "change_pct": chg,
                "expected_return_5d": expected_return
            }

            all_top.append(item)

            if i <= TOP_CORE and expected_return > 1.5 and flow_q >= 0.8:
                core_top.append(item)

        return {"top20": all_top, "top5_core": core_top if core_top else all_top[:3]}


# ─────────────────────────────
# 메인 엔진
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
        if score > 0.5:
            return "UPTREND"
        elif score < -0.5:
            return "DOWNTREND"
        return "SIDEWAY"

    def pre_filter(self, stocks):
        filtered = [
            s for s in stocks
            if safe_float(s.get("volume")) > 30000
            and safe_float(s.get("close")) > 1000
            and is_common_stock(s.get("name"), s.get("code"))
        ]
        if len(filtered) < 10 and len(stocks) >= 10:
            filtered = [s for s in stocks if is_common_stock(s.get("name"), s.get("code"))]
        return filtered

    def save_signal_history(self, top_list, regime):
        today = datetime.now(KST).strftime("%Y-%m-%d")
        df = pd.DataFrame([
            {"date": today, "regime": regime, "rank": t.get("rank"), "code": t.get("code"),
             "name": t.get("name"), "score": t.get("score"), "price": t.get("price"),
             "change_pct": t.get("change_pct")} for t in top_list
        ])
        try:
            old = pd.read_csv(SIGNAL_HISTORY, encoding="utf-8-sig")
            if "date" in old.columns:
                old = old[old["date"] != today]
                df = pd.concat([old, df], ignore_index=True)
        except:
            pass
        df.to_csv(SIGNAL_HISTORY, index=False, encoding="utf-8-sig")

    def verify(self):
        try:
            hist = pd.read_csv("history.csv", dtype={"code": str}, encoding="utf-8-sig")
            hist["code"] = hist["code"].str.zfill(6)
            price_map = {k: v for k, v in zip(hist["code"], hist["close"]) if pd.notna(v) and float(v) > 0}

            sig = pd.read_csv(SIGNAL_HISTORY, encoding="utf-8-sig")
            y = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
            sig = sig[sig["date"] == y]

            if sig.empty:
                return {"win_rate": 0, "avg_return": 0, "top5_return": 0}

            hits, total, avg, top5 = 0, len(sig), 0, []

            for _, r in sig.iterrows():
                code = str(r["code"]).zfill(6)
                entry = safe_float(r.get("price"))
                exitp = price_map.get(code, 0)

                if entry > 0 and exitp > 0:
                    ret = (exitp - entry) / entry * 100
                    avg += ret
                    if ret > 0:
                        hits += 1
                    if r.get("rank", 999) <= 5:
                        top5.append(ret)

            return {
                "win_rate": round(hits / total * 100, 1) if total > 0 else 0,
                "avg_return": round(avg / total, 2) if total > 0 else 0,
                "top5_return": round(sum(top5) / len(top5), 2) if top5 else 0
            }

        except:
            return {"win_rate": 0, "avg_return": 0, "top5_return": 0}

    def run(self):
        pro_mode = os.getenv("PRO_MODE", "false").lower() == "true"

        flow = self.load_json("market_flow.json")
        news = self.load_json("news_scores.json").get("scores", {})
        fund_raw = self.load_json("fundamental.json").get("stocks", [])
        fund = {str(s.get("code","")).zfill(6): s for s in fund_raw}

        stocks = self.load_stock_data()
        regime = self.compute_regime(flow)
        stocks = self.pre_filter(stocks)

        scorer = StockScorer(stocks, flow, fund, news, regime)
        selection = scorer.top_n()
        top_list = selection["top20"]
        core_list = selection["top5_core"]

        self.save_signal_history(top_list, regime)
        perf = self.verify()

        pro_data = {}
        if pro_mode:
            large_caps = [t for t in top_list if safe_float(t.get("price")) >= 100000]
            sector_proxy = {}

            for t in top_list:
                name = t.get("name","")
                if "자동차" in name or "현대" in name or "기아" in name:
                    sector_proxy["Auto"] = sector_proxy.get("Auto",0)+1
                elif "전자" in name or "삼성" in name or "LG" in name:
                    sector_proxy["Electronics"] = sector_proxy.get("Electronics",0)+1

            pro_data["bias_estimate"] = {
                "large_cap_ratio": round(len(large_caps)/max(len(top_list),1), 2),
                "top_sectors": sector_proxy
            }

            if len(stocks) > 0:
                chg_vals = [safe_float(s.get("change_rate")) for s in stocks]
                pro_data["factor_snapshot"] = {
                    "momentum_avg": round(sum(chg_vals)/len(chg_vals), 2),
                    "news_coverage": len([c for c in news if news.get(c,0) != 0])
                }

        result = {
            "date": datetime.now(KST).strftime("%Y-%m-%d"),
            "regime": regime,
            "universe_size": len(stocks),
            "top20": top_list,
            "top5_core": core_list,
            "performance_today": perf
        }

        if pro_mode and pro_data:
            result["pro_analytics"] = pro_data

        with open(RESULT_FILE, "w", encoding="utf-8-sig") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        print(f"[DONE] {regime} | TOP20:{len(top_list)} CORE:{len(core_list)} | WR:{perf['win_rate']}%")


if __name__ == "__main__":
    RegimeEngine().run()
