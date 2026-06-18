#!/usr/bin/env python3
"""Daily morning stock briefing — fetches data and emails a performance + SMA report."""

import os
import sys
import yaml
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

import pytz
import yfinance as yf
import pandas as pd


def load_config(path: str = "config/stocks.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def get_watchlist(config: dict) -> list:
    watchlist = []
    for tickers in config["groups"].values():
        watchlist.extend(tickers)
    return watchlist


def is_within_market_close_window() -> bool:
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    minutes_since_close = (now_et.hour - 16) * 60 + now_et.minute
    return 0 <= minutes_since_close <= 60


def fetch_stock(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = t.info
    fast = t.fast_info
    hist = t.history(period="1y")

    price_info = info.get("currentPrice") or info.get("regularMarketPrice")
    price_fast = float(fast.last_price) if fast.last_price else None
    price_hist = float(hist["Close"].iloc[-1]) if not hist.empty else None

    prices = [p for p in [price_info, price_fast, price_hist] if p is not None]
    cross_ok = (max(prices) - min(prices)) / min(prices) * 100 < 1.0 if len(prices) >= 2 else len(prices) > 0

    current_price = price_info or price_fast or price_hist
    prev_close = info.get("previousClose") or (float(fast.previous_close) if fast.previous_close else None)
    change_pct = (
        (current_price - prev_close) / prev_close * 100
        if current_price and prev_close else None
    )

    perf_1w = perf_1m = perf_6m = None
    vs_sma20 = vs_sma50 = None
    golden_cross = momentum_break = False

    if not hist.empty:
        closes = hist["Close"]
        last = float(closes.iloc[-1])

        def _pct(n):
            if len(closes) >= n + 1:
                base = float(closes.iloc[-(n + 1)])
                return (last - base) / base * 100 if base else None
            return None

        perf_1w = _pct(5)
        perf_1m = _pct(21)
        perf_6m = _pct(126)

        if len(closes) >= 20:
            sma20 = float(closes.tail(20).mean())
            vs_sma20 = (last - sma20) / sma20 * 100
        if len(closes) >= 50:
            sma50 = float(closes.tail(50).mean())
            vs_sma50 = (last - sma50) / sma50 * 100

        if len(closes) >= 21:
            yesterday = float(closes.iloc[-2])
            sma20_y = float(closes.iloc[-21:-1].mean())
            golden_cross = (yesterday < sma20_y) and (last > float(closes.tail(20).mean()))

        if len(closes) >= 51:
            yesterday = float(closes.iloc[-2])
            sma50_y = float(closes.iloc[-51:-1].mean())
            momentum_break = (yesterday > sma50_y) and (last < float(closes.tail(50).mean()))

    return {
        "ticker": ticker,
        "name": info.get("shortName") or info.get("longName") or ticker,
        "current_price": current_price,
        "change_pct": change_pct,
        "perf_1w": perf_1w,
        "perf_1m": perf_1m,
        "perf_6m": perf_6m,
        "vs_sma20": vs_sma20,
        "vs_sma50": vs_sma50,
        "golden_cross": golden_cross,
        "momentum_break": momentum_break,
        "cross_ok": cross_ok,
    }


def _price_str(val, ticker: str) -> str:
    if val is None:
        return "N/A"
    return f"₩{int(val):,}" if ticker.endswith(".KS") else f"${val:.2f}"


def _pct_str(val) -> str:
    if val is None:
        return "N/A"
    return f"{val:+.1f}%"


def _pct_color(val) -> str:
    if val is None or val == 0:
        return "#64748b"
    return "#16a34a" if val > 0 else "#dc2626"


def _vs50_color(val) -> str:
    if val is None or val == 0:
        return "#64748b"
    if val < 0:
        return "#dc2626"
    if val < 5:
        return "#eab308"
    return "#16a34a"


def _group_table_html(group_name: str, tickers: list, stock_map: dict, names: dict) -> str:
    rows = ""
    for ticker in tickers:
        s = stock_map.get(ticker)
        if not s:
            continue
        display_name = names.get(ticker, s["name"])[:14]
        rows += (
            f"<tr style='border-bottom:1px solid #f1f5f9;'>"
            f"<td style='padding:5px 8px;font-size:12px;white-space:nowrap;'>"
            f"<div style='font-weight:600;'>{display_name}</div>"
            f"<div style='font-size:10px;color:#94a3b8;'>{ticker}</div>"
            f"</td>"
            f"<td style='padding:5px 8px;text-align:right;font-family:monospace;font-size:11px;white-space:nowrap;'>{_price_str(s['current_price'], ticker)}</td>"
            f"<td style='padding:5px 8px;text-align:right;font-size:11px;color:{_pct_color(s['change_pct'])};'>{_pct_str(s['change_pct'])}</td>"
            f"<td style='padding:5px 8px;text-align:right;font-size:11px;color:{_pct_color(s['perf_1w'])};'>{_pct_str(s['perf_1w'])}</td>"
            f"<td style='padding:5px 8px;text-align:right;font-size:11px;color:{_pct_color(s['perf_1m'])};'>{_pct_str(s['perf_1m'])}</td>"
            f"<td style='padding:5px 8px;text-align:right;font-size:11px;color:{_pct_color(s['perf_6m'])};'>{_pct_str(s['perf_6m'])}</td>"
            f"<td style='padding:5px 8px;text-align:right;font-size:11px;color:{_pct_color(s['vs_sma20'])};'>{_pct_str(s['vs_sma20'])}</td>"
            f"<td style='padding:5px 8px;text-align:right;font-size:11px;color:{_vs50_color(s['vs_sma50'])};'>{_pct_str(s['vs_sma50'])}</td>"
            f"</tr>"
        )

    return f"""
    <div style="padding:12px 20px 4px;">
      <div style="font-size:12px;font-weight:700;color:#475569;margin-bottom:6px;">#{group_name}</div>
    </div>
    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;min-width:580px;">
      <thead>
        <tr style="background:#f8fafc;">
          <th style="padding:5px 8px;text-align:left;font-size:10px;color:#94a3b8;font-weight:500;white-space:nowrap;">종목</th>
          <th style="padding:5px 8px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;white-space:nowrap;">현재가</th>
          <th style="padding:5px 8px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;white-space:nowrap;">등락률</th>
          <th style="padding:5px 8px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;white-space:nowrap;">1W</th>
          <th style="padding:5px 8px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;white-space:nowrap;">1M</th>
          <th style="padding:5px 8px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;white-space:nowrap;">6M</th>
          <th style="padding:5px 8px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;white-space:nowrap;">vs20D</th>
          <th style="padding:5px 8px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;white-space:nowrap;">vs50D</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


def _todays_points_html(stocks: list, names: dict) -> str:
    items = []

    def display(s):
        return names.get(s["ticker"], s["ticker"])

    breaks = [s for s in stocks if s["momentum_break"]]
    if breaks:
        label = ", ".join(f"{display(s)}({s['vs_sma50']:+.1f}%)" for s in breaks)
        items.append(f"\U0001f4c9 모멘텀 이탈 (50일선 돌파 하락): {label}")

    crosses = [s for s in stocks if s["golden_cross"]]
    if crosses:
        label = ", ".join(f"{display(s)}({s['vs_sma20']:+.1f}%)" for s in crosses)
        items.append(f"✨ 골든 크로스 (20일선 상향 돌파): {label}")

    if not items:
        return "<li>신호 없음</li>"
    return "".join(f'<li style="padding:4px 0;">{item}</li>' for item in items)


def build_email_html(groups: dict, stock_map: dict, names: dict, date_str: str) -> str:
    all_ok = all(s["cross_ok"] for s in stock_map.values())
    cross_badge = "✅ 3중 크로스체크 완료" if all_ok else "⚠️ 일부 데이터 불일치"
    cross_color = "#4ade80" if all_ok else "#fbbf24"

    group_blocks = ""
    for group_name, tickers in groups.items():
        group_blocks += _group_table_html(group_name, tickers, stock_map, names)
        group_blocks += "<div style='height:8px;'></div>"

    all_stocks = list(stock_map.values())

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:12px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1e293b;">
  <div style="max-width:800px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1);">
    <div style="background:#0f172a;padding:16px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;">
      <div>
        <div style="color:#94a3b8;font-size:10px;letter-spacing:.1em;text-transform:uppercase;">Daily Stock Briefing</div>
        <div style="color:#fff;font-size:16px;font-weight:700;margin-top:2px;">{date_str}</div>
      </div>
      <div style="color:{cross_color};font-size:11px;font-weight:600;">{cross_badge}</div>
    </div>
    {group_blocks}
    <div style="padding:14px 20px;border-top:1px solid #e2e8f0;">
      <div style="font-size:13px;font-weight:700;margin-bottom:8px;">\U0001f4a1 오늘의 포인트</div>
      <ul style="margin:0;padding-left:18px;line-height:1.9;color:#374151;font-size:13px;">{_todays_points_html(all_stocks, names)}</ul>
    </div>
    <div style="background:#f8fafc;padding:10px 20px;border-top:1px solid #e2e8f0;text-align:center;font-size:10px;color:#94a3b8;">
      본 브리핑은 투자 조언이 아닙니다 · {date_str}
    </div>
  </div>
</body>
</html>"""


def send_email(subject: str, html: str, recipient: str, sender: str, password: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(sender, password)
        srv.sendmail(sender, [recipient], msg.as_string())


def main():
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    is_scheduled = event == "schedule"
    is_manual = event == "workflow_dispatch"
    if not is_scheduled and not is_manual and not is_within_market_close_window():
        et = pytz.timezone("America/New_York")
        now_et = datetime.now(et)
        print(f" 시장 마감 시간이 아닙니다 (ET {now_et.strftime('%H:%M')}). 발송 건너맜.")
        sys.exit(0)

    config_path = os.environ.get("CONFIG_PATH", "config/stocks.yaml")
    config = load_config(config_path)
    groups: dict = config["groups"]
    names: dict = config.get("names", {})
    watchlist = get_watchlist(config)

    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    day_ko = {"Monday": "월요일", "Tuesday": "화요일", "Wednesday": "수요일",
               "Thursday": "목요일", "Friday": "금요일", "Saturday": "토요일", "Sunday": "일요일"}
    date_str = f"{now.strftime('%Y년 %m월 %d일')} ({day_ko.get(now.strftime('%A'), '')})"

    print(f"\n{'='*55}")
    print(f" \U0001f4ca Daily Stock Briefing  {date_str}")
    print(f"{'='*55}")
    print(f" 수집 종목: {len(watchlist)}개\n")

    stock_map = {}
    failed = []
    for ticker in watchlist:
        try:
            data = fetch_stock(ticker)
            stock_map[ticker] = data
            status = "✅" if data["cross_ok"] else "⚠️"
            price_str = _price_str(data["current_price"], ticker)
            chg_str = f"({data['change_pct']:+.2f}%)" if data["change_pct"] else ""
            print(f"  {status} {ticker:<12}  {price_str}  {chg_str}")
        except Exception as exc:
            failed.append(ticker)
            print(f"  ❌ {ticker:<12}  오류: {exc}")

    all_ok = all(s["cross_ok"] for s in stock_map.values())
    print(f"\n {'✅ 3중 크로스체크 완료' if all_ok else '⚠️ 일부 데이터 불일치'}")
    if failed:
        print(f" ❌ 수집 실패: {', '.join(failed)}")

    html = build_email_html(groups, stock_map, names, date_str)
    subject = f"\U0001f4ca 주식 브리핑 — {date_str}"

    sender = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT_EMAIL", sender)

    print(f"\n 이메일 발송 → {recipient}")
    send_email(subject, html, recipient, sender, password)
    print(" 완료!\n")


if __name__ == "__main__":
    main()
