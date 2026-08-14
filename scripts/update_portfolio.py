#!/usr/bin/env python3
"""
update_portfolio.py ── 保有銘柄の評価額を計算し portfolio.json を再生成する

データソース: J-Quants API V2（JPX公式）[⑤]
  GET /v2/equities/bars/daily?date=YYYYMMDD  で全銘柄の四本値（未調整終値 C を含む）を取得し、
  portfolio/holdings.yaml に登録された保有銘柄だけを抜き出す。

  当日分がまだ配信されていない時間帯に実行されても落ちないよう、
  直近営業日を新しい方から遡って最初に見つかった配信済み日付を採用する。

使い方:
  export JQUANTS_API_KEY="..."
  python3 scripts/update_portfolio.py

出力:
  portfolio.json （サイトルート）
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

API = "https://api.jquants.com/v2"
ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_YAML = ROOT / "portfolio" / "holdings.yaml"
OUT_JSON = ROOT / "portfolio.json"

LOOKBACK_DAYS = 10  # 連休対策。直近10日を新しい順に遡って配信済みの日を探す


def api_key() -> str:
    key = os.environ.get("JQUANTS_API_KEY")
    if not key:
        sys.exit("環境変数 JQUANTS_API_KEY が設定されていません。")
    return key


def load_holdings() -> list[dict]:
    data = yaml.safe_load(HOLDINGS_YAML.read_text(encoding="utf-8")) or {}
    out = []
    for h in data.get("holdings", []):
        code = str(h["code"]).strip()
        purchases = h.get("purchases", [])
        shares = sum(p["shares"] for p in purchases)
        cost_basis = sum(p["price"] * p["shares"] for p in purchases)
        if shares <= 0:
            continue
        out.append({
            "code": code,
            "name": h.get("name", code),
            "shares": shares,
            "avg_cost": cost_basis / shares,
            "cost_basis": cost_basis,
            "purchases": purchases,
        })
    return out


def code5(code: str) -> str:
    """4桁コードをJ-Quants API v2が返す5桁表記に揃える。"""
    return code if len(code) == 5 else code + "0"


def fetch_daily_bars(date_str: str) -> list[dict]:
    r = requests.get(
        f"{API}/equities/bars/daily",
        params={"date": date_str},
        headers={"x-api-key": api_key()},
        timeout=60,
    )
    if r.status_code == 429:
        time.sleep(20)
        r = requests.get(
            f"{API}/equities/bars/daily",
            params={"date": date_str},
            headers={"x-api-key": api_key()},
            timeout=60,
        )
    r.raise_for_status()
    return r.json().get("data", [])


def find_latest_prices(codes: set[str]) -> tuple[str, dict[str, float]]:
    """codes（4桁）に対応する未調整終値を、配信済みの最新営業日から取得する。"""
    wanted5 = {code5(c) for c in codes}
    today = dt.date.today()
    for i in range(LOOKBACK_DAYS):
        d = today - dt.timedelta(days=i)
        if d.weekday() >= 5:  # 土日はスキップ
            continue
        rows = fetch_daily_bars(d.strftime("%Y%m%d"))
        if not rows:
            continue  # 未配信 or 休場日 → もっと過去へ
        prices = {}
        for row in rows:
            rc = str(row.get("Code", ""))
            if rc in wanted5:
                close = row.get("C")
                if close is None:
                    close = row.get("AdjC")  # 保険（本来は直叩きAPIにCが含まれる）
                if close is not None:
                    prices[rc] = float(close)
        if prices:
            return d.strftime("%Y-%m-%d"), prices
        # データはあるが対象コードが1件も無い（マスタ不整合等）→ 念のため次の日も試す
    sys.exit("直近営業日の株価データを取得できませんでした（保有銘柄が見つかりません）。")


def main():
    holdings = load_holdings()
    if not holdings:
        sys.exit("portfolio/holdings.yaml に保有銘柄がありません。")

    codes = {h["code"] for h in holdings}
    price_date, prices = find_latest_prices(codes)

    rows = []
    total_cost = total_value = 0.0
    for h in holdings:
        rc5 = code5(h["code"])
        price = prices.get(rc5)
        if price is None:
            print(f"[警告] {h['code']} {h['name']} の株価が見つかりませんでした。スキップします。")
            continue
        value = price * h["shares"]
        gain = value - h["cost_basis"]
        gain_pct = (gain / h["cost_basis"] * 100) if h["cost_basis"] else 0.0
        total_cost += h["cost_basis"]
        total_value += value
        rows.append({
            "code": h["code"],
            "name": h["name"],
            "shares": h["shares"],
            "avg_cost": round(h["avg_cost"], 2),
            "cost_basis": round(h["cost_basis"]),
            "price": price,
            "value": round(value),
            "gain": round(gain),
            "gain_pct": round(gain_pct, 2),
            "purchases": h["purchases"],
        })

    rows.sort(key=lambda r: r["value"], reverse=True)

    total_gain = total_value - total_cost
    total_gain_pct = (total_gain / total_cost * 100) if total_cost else 0.0

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price_date": price_date,
        "source": "J-Quants API V2（JPX公式・未調整終値）",
        "holdings": rows,
        "totals": {
            "cost_basis": round(total_cost),
            "value": round(total_value),
            "gain": round(total_gain),
            "gain_pct": round(total_gain_pct, 2),
        },
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"portfolio.json を更新しました（基準日 {price_date} / {len(rows)}銘柄）。")


if __name__ == "__main__":
    main()
