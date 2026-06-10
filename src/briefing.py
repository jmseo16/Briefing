#!/usr/bin/env python3
"""Daily morning stock briefing — fetches data, cross-checks, and emails a table report."""

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


def is_within_market_close_window() -> bool:
    """NYSE closes at 4:00 PM ET. Return True only within 60 min of close."""
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    minutes_since_close = (now_et.hour - 16) * 60 + now_et.minute
    return 0 <= minutes_since_close <= 60


def calculate_rsi(closes: pd.Series, period: int = 14):
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 1)


def fetch_stock(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = t.info
    fast = t.fast_info
    hist = t.history(period="3mo")

    price_info = info.get("currentPrice") or info.get("regularMarketPrice")
    price_fast = float(fast.last_price) if fast.last_price else None
    price_hist = float(hist["Close"].iloc[-1]) if not hist.empty else None

    prices = [p for p in [price_info, price_fast, price_hist] if p is not None]
    if len(prices) >= 2:
        spread = (max(prices) - min(prices)) / min(prices) * 100
        cross_ok = spread < 1.0
    else:
        cross_ok = len(prices) > 0

    current_price = price_info or price_fast or price_hist
    prev_close = float(fast.previous_close) if fast.previous_close else info.get("previousClose")

    change_pct = (
        (current_price - prev_close) / prev_close * 100
        if current_price and prev_close
        else None
    )

    return {
        "ticker": ticker,
        "name": info.get("shortName") or info.get("longName") or ticker,
        "current_price": current_price,
        "change_pct": change_pct,
        "forward_pe": info.get("forwardPE"),
        "peg_ratio": info.get("pegRatio"),
        "rsi": calculate_rsi(hist["Close"]) if not hist.empty else None,
        "cross_ok": cross_ok,
    }


def _fmt(val, decimals=2, prefix="", suffix="") -> str:
    if val is None:
        return "N/A"
    return f"{prefix}{val:.{decimals}f}{suffix}"


def _change_emoji(v) -> str:
    if v is None:
        return ""
    return "🟢" if v >= 0 else "🔴"


def _rsi_emoji(v) -> str:
    if v is None:
        return "⚪"
    if v <= 30:
        return "🟢"
    if v <= 40:
        return "🟡"
    return "⚪"


def _table_rows_html(stocks: list, names: dict) -> str:
    rows = ""
    for s in stocks:
        display_name = names.get(s["ticker"], s["name"])[:16]
        ch_color = "#16a34a" if s["change_pct"] and s["change_pct"] >= 0 else "#dc2626"
        rows += (
            f"<tr style='border-bottom:1px solid #f1f5f9;'>"
            f"<td style='padding:6px 8px;white-space:nowrap;font-size:12px;'>"
            f"<div style='font-weight:600;'>{display_name}</div>"
            f"<div style='font-size:10px;color:#94a3b8;'>{s['ticker']}</div>"
            f"</td>"
            f"<td style='padding:6px 8px;text-align:right;font-family:monospace;font-size:12px;'>{_fmt(s['current_price'], prefix='$')}</td>"
            f"<td style='padding:6px 8px;text-align:right;color:{ch_color};font-size:12px;'>{_change_emoji(s['change_pct'])} {_fmt(s['change_pct'], suffix='%')}</td>"
            f"<td style='padding:6px 8px;text-align:right;font-size:12px;'>{_fmt(s['forward_pe'], 1)}</td>"
            f"<td style='padding:6px 8px;text-align:right;font-size:12px;'>{_fmt(s['peg_ratio'], 2)}</td>"
            f"<td style='padding:6px 8px;text-align:right;font-size:12px;'>{_rsi_emoji(s['rsi'])} {_fmt(s['rsi'], 1)}</td>"
            f"</tr>"
        )
    return rows


def _todays_points_html(stocks: list) -> str:
    items = []

    rsi_picks = sorted(
        [s for s in stocks if s["rsi"] is not None and s["rsi"] <= 40],
        key=lambda x: x["rsi"],
    )
    if rsi_picks:
        label = ", ".join(
            f"{s['ticker']}({_rsi_emoji(s['rsi'])} {s['rsi']:.0f})" for s in rsi_picks[:4]
        )
        items.append(f"📉 RSI 관심 신호: {label}")

    peg_picks = sorted(
        [s for s in stocks if s["peg_ratio"] is not None and 0 < s["peg_ratio"] < 1.0],
        key=lambda x: x["peg_ratio"],
    )
    if peg_picks:
        label = ", ".join(f"{s['ticker']}(PEG {s['peg_ratio']:.2f})" for s in peg_picks[:4])
        items.append(f"💎 PEG 저평가 종목: {label}")

    if not items:
        return "<li>포인트 없음</li>"
    return "".join(f'<li style="padding:4px 0;">{item}</li>' for item in items)


def build_email_html(stocks: list, names: dict, date_str: str) -> str:
    all_ok = all(s["cross_ok"] for s in stocks)
    cross_badge = "✅ 3중 크로스체크 완료" if all_ok else "⚠️ 일부 데이터 불일치"
    cross_color = "#4ade80" if all_ok else "#fbbf24"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:12px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1e293b;">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1);">
    <div style="background:#0f172a;padding:16px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;">
      <div>
        <div style="color:#94a3b8;font-size:10px;letter-spacing:.1em;text-transform:uppercase;">Daily Stock Briefing</div>
        <div style="color:#fff;font-size:16px;font-weight:700;margin-top:2px;">{date_str}</div>
      </div>
      <div style="color:{cross_color};font-size:11px;font-weight:600;">{cross_badge}</div>
    </div>
    <div style="overflow-x:auto;">
      <table style="width:100%;border-collapse:collapse;min-width:320px;">
        <thead>
          <tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0;">
            <th style="padding:6px 8px;text-align:left;font-size:10px;color:#64748b;font-weight:600;">종목</th>
            <th style="padding:6px 8px;text-align:right;font-size:10px;color:#64748b;font-weight:600;">현재가</th>
            <th style="padding:6px 8px;text-align:right;font-size:10px;color:#64748b;font-weight:600;">등락률</th>
            <th style="padding:6px 8px;text-align:right;font-size:10px;color:#64748b;font-weight:600;">Fwd PER</th>
            <th style="padding:6px 8px;text-align:right;font-size:10px;color:#64748b;font-weight:600;">PEG</th>
            <th style="padding:6px 8px;text-align:right;font-size:10px;color:#64748b;font-weight:600;">RSI</th>
          </tr>
        </thead>
        <tbody>{_table_rows_html(stocks, names)}</tbody>
      </table>
    </div>
    <div style="padding:16px 20px;border-top:1px solid #e2e8f0;">
      <div style="font-size:13px;font-weight:700;margin-bottom:8px;">💡 오늘의 포인트</div>
      <ul style="margin:0;padding-left:18px;line-height:1.9;color:#374151;font-size:13px;">{_todays_points_html(stocks)}</ul>
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
    # workflow_dispatch(수동 실행)은 항상 통과, 스케줄 실행은 마감 후 60분 이내만 허용
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not is_manual and not is_within_market_close_window():
        et = pytz.timezone("America/New_York")
        now_et = datetime.now(et)
        print(f" 시장 마감 시간이 아닙니다 (ET {now_et.strftime('%H:%M')}). 발송 건너맜.")
        sys.exit(0)

    config_path = os.environ.get("CONFIG_PATH", "config/stocks.yaml")
    config = load_config(config_path)
    watchlist = config["watchlist"]
    names = config.get("names", {})

    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    day_ko = {
        "Monday": "월요일", "Tuesday": "화요일", "Wednesday": "수요일",
        "Thursday": "목요일", "Friday": "금요일", "Saturday": "토요일", "Sunday": "일요일",
    }
    date_str = f"{now.strftime('%Y년 %m월 %d일')} ({day_ko.get(now.strftime('%A'), '')})"

    print(f"\n{'='*55}")
    print(f" 📊 Daily Stock Briefing  {date_str}")
    print(f"{'='*55}")
    print(f" 수집 종목: {len(watchlist)}개\n")

    stocks = []
    failed = []
    for ticker in watchlist:
        try:
            data = fetch_stock(ticker)
            stocks.append(data)
            status = "✅" if data["cross_ok"] else "⚠️"
            rsi_str = f"RSI={data['rsi']:.0f}" if data["rsi"] else "RSI=N/A"
            price_str = f"${data['current_price']:.2f}" if data["current_price"] else "N/A"
            chg_str = f"({data['change_pct']:+.2f}%)" if data["change_pct"] else ""
            print(f"  {status} {ticker:<6}  {price_str}  {chg_str}  {rsi_str}")
        except Exception as exc:
            failed.append(ticker)
            print(f"  ❌ {ticker:<6}  오류: {exc}")

    all_ok = all(s["cross_ok"] for s in stocks)
    print(f"\n {'✅ 3중 크로스체크 완료' if all_ok else '⚠️ 일부 데이터 불일치'}")
    if failed:
        print(f" ❌ 수집 실패: {', '.join(failed)}")

    html = build_email_html(stocks, names, date_str)
    subject = f"📊 주식 브리핑 — {date_str}"

    sender = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT_EMAIL", sender)

    print(f"\n 이메일 발송 → {recipient}")
    send_email(subject, html, recipient, sender, password)
    print(" 완료!\n")


if __name__ == "__main__":
    main()
