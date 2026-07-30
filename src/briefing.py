#!/usr/bin/env python3
"""포트폴리오 트래커 — 코어-위성-바벨 전략 일일 리밸런싱 브리핑"""

import os
import sys
import yaml
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

import pytz
import yfinance as yf


def load_config(path="config/portfolio.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def fetch_price(ticker):
    t = yf.Ticker(ticker)
    info = t.info
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if price is None:
        try:
            price = float(t.fast_info.last_price)
        except Exception:
            pass
    if price is None:
        hist = t.history(period="5d")
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
    return price


def fetch_usdkrw():
    try:
        t = yf.Ticker("USDKRW=X")
        rate = float(t.fast_info.last_price)
        if rate and rate > 100:
            return rate
    except Exception:
        pass
    hist = yf.Ticker("USDKRW=X").history(period="5d")
    if not hist.empty:
        return float(hist["Close"].iloc[-1])
    return 1380.0


def evaluate_holdings(config):
    usdkrw = None
    results = {}

    for category, items in config["holdings"].items():
        total = 0
        positions = []

        for item in items:
            if "ticker" in item:
                ticker = item["ticker"]
                shares = item.get("shares", 0)
                name = item.get("name", ticker)
                price = fetch_price(ticker)

                if price is None:
                    positions.append({"name": name, "ticker": ticker, "value": None, "error": True})
                    continue

                if not ticker.endswith(".KS"):
                    if usdkrw is None:
                        usdkrw = fetch_usdkrw()
                    value = price * shares * usdkrw
                else:
                    value = price * shares

                total += value
                positions.append({"name": name, "ticker": ticker, "shares": shares,
                                   "price": price, "value": value})

            elif "usd" in item:
                if usdkrw is None:
                    usdkrw = fetch_usdkrw()
                value = item["usd"] * usdkrw
                total += value
                positions.append({"name": item.get("name", "USD"), "ticker": None, "value": value})

            elif "krw" in item:
                value = item["krw"]
                total += value
                positions.append({"name": item.get("name", "현금"), "ticker": None, "value": value})

        results[category] = {"total": total, "positions": positions}

    return results, usdkrw


def calculate_weights(evaluated):
    grand_total = sum(v["total"] for v in evaluated.values())
    weights = {cat: data["total"] / grand_total for cat, data in evaluated.items()} if grand_total else {}
    return weights, grand_total


def check_rebalance(weights, grand_total, config):
    targets = config["strategy"]["targets"]
    band = config["strategy"]["rebalance_band"]
    signals = []

    for cat, (lo, hi) in band.items():
        w = weights.get(cat, 0)
        if w < lo or w > hi:
            tgt = targets[cat]
            diff_krw = (tgt - w) * grand_total
            signals.append({
                "category": cat,
                "current": w,
                "target": tgt,
                "diff_krw": diff_krw,
                "direction": "매수" if diff_krw > 0 else "매도",
                "band": (lo, hi),
            })
    return signals


def fmt_krw(v):
    if v is None:
        return "N/A"
    return f"₩{int(v):,}"


def _pct_color(v):
    if v > 0.001:
        return "#16a34a"
    if v < -0.001:
        return "#dc2626"
    return "#64748b"


CAT_LABELS = {
    "core": "코어 · 나스닥100",
    "satellite": "위성 · 모멘텀",
    "barbell": "바벨 · 배당다우존스",
    "cash": "현금 · 안전자산",
}


def build_html(evaluated, weights, grand_total, signals, config, date_str, usdkrw):
    targets = config["strategy"]["targets"]
    band = config["strategy"]["rebalance_band"]

    summary_rows = ""
    for cat, label in CAT_LABELS.items():
        if cat not in evaluated:
            continue
        w = weights.get(cat, 0)
        tgt = targets.get(cat, 0)
        diff = w - tgt
        val = evaluated[cat]["total"]

        if cat in band:
            lo, hi = band[cat]
            if w < lo or w > hi:
                badge = "\U0001f534 이탈"
                badge_color = "#dc2626"
            else:
                badge = "✅ 정상"
                badge_color = "#16a34a"
        else:
            badge = "—"
            badge_color = "#94a3b8"

        bar_w = min(int(w * 200), 200)
        diff_color = _pct_color(diff)

        summary_rows += f"""
        <tr style="border-bottom:1px solid #f1f5f9;">
          <td style="padding:10px 12px;">
            <div style="font-size:12px;font-weight:700;">{label}</div>
            <div style="margin-top:4px;background:#e2e8f0;border-radius:4px;height:6px;width:200px;">
              <div style="background:#3b82f6;width:{bar_w}px;height:6px;border-radius:4px;"></div>
            </div>
          </td>
          <td style="padding:10px 12px;text-align:right;font-size:16px;font-weight:800;">{w*100:.1f}%</td>
          <td style="padding:10px 12px;text-align:right;font-size:12px;color:#64748b;">{tgt*100:.0f}%</td>
          <td style="padding:10px 12px;text-align:right;font-size:13px;font-weight:600;color:{diff_color};">{diff*100:+.1f}%p</td>
          <td style="padding:10px 12px;text-align:right;font-size:12px;font-family:monospace;white-space:nowrap;">{fmt_krw(val)}</td>
          <td style="padding:10px 12px;text-align:center;font-size:12px;color:{badge_color};white-space:nowrap;">{badge}</td>
        </tr>"""

    if signals:
        alert_bg = "#fff7ed"
        alert_border = "#f59e0b"
        alert_title = "⚠️ 리밸런싱 필요"
        alert_items = ""
        for sig in signals:
            label = CAT_LABELS.get(sig["category"], sig["category"])
            lo, hi = sig["band"]
            side = "하단" if sig["current"] < lo else "상단"
            alert_items += (
                f"<li style='padding:5px 0;'>"
                f"<strong>{label}</strong> 현재 {sig['current']*100:.1f}% → "
                f"밴드 {side}({lo*100:.0f}~{hi*100:.0f}%) 이탈<br>"
                f"<span style='color:#92400e;'>→ {fmt_krw(abs(sig['diff_krw']))} {sig['direction']} 필요 "
                f"(목표 {sig['target']*100:.0f}%까지)</span>"
                f"</li>"
            )
    else:
        alert_bg = "#f0fdf4"
        alert_border = "#16a34a"
        alert_title = "✅ 리밸런싱 불필요"
        alert_items = "<li style='padding:5px 0;'>코어·위성 모두 밴드(20~40%) 내 정상 유지 중</li>"

    detail_blocks = ""
    for cat, label in CAT_LABELS.items():
        if cat not in evaluated:
            continue
        positions = evaluated[cat]["positions"]
        rows = ""
        for p in positions:
            val = p.get("value")
            val_str = fmt_krw(val) if val is not None else "⚠️ 오류"
            ticker_str = p.get("ticker") or ""
            rows += (
                f"<tr style='border-bottom:1px solid #f8fafc;'>"
                f"<td style='padding:5px 10px;font-size:12px;'>{p['name']}</td>"
                f"<td style='padding:5px 10px;font-size:10px;color:#94a3b8;'>{ticker_str}</td>"
                f"<td style='padding:5px 10px;text-align:right;font-size:12px;font-family:monospace;'>{val_str}</td>"
                f"</tr>"
            )
        detail_blocks += f"""
        <div style="padding:10px 20px 2px;">
          <div style="font-size:11px;font-weight:700;color:#475569;">#{label}</div>
        </div>
        <table style="width:100%;border-collapse:collapse;"><tbody>{rows}</tbody></table>"""

    usdkrw_str = f"USD/KRW {usdkrw:,.0f}" if usdkrw else ""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:12px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1e293b;">
  <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1);">

    <div style="background:#0f172a;padding:18px 20px;">
      <div style="color:#94a3b8;font-size:10px;letter-spacing:.1em;text-transform:uppercase;">Portfolio Briefing · 코어-위성-바벨</div>
      <div style="color:#fff;font-size:15px;font-weight:700;margin-top:2px;">{date_str}</div>
      <div style="color:#60a5fa;font-size:22px;font-weight:800;margin-top:4px;">{fmt_krw(grand_total)}</div>
      <div style="color:#475569;font-size:10px;margin-top:2px;">{usdkrw_str}</div>
    </div>

    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;min-width:480px;">
      <thead>
        <tr style="background:#f8fafc;">
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#94a3b8;font-weight:500;">전략</th>
          <th style="padding:8px 12px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;">현재</th>
          <th style="padding:8px 12px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;">목표</th>
          <th style="padding:8px 12px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;">편차</th>
          <th style="padding:8px 12px;text-align:right;font-size:10px;color:#94a3b8;font-weight:500;">금액</th>
          <th style="padding:8px 12px;text-align:center;font-size:10px;color:#94a3b8;font-weight:500;">밴드</th>
        </tr>
      </thead>
      <tbody>{summary_rows}</tbody>
    </table>
    </div>

    <div style="margin:12px 16px;padding:14px 16px;background:{alert_bg};border-left:4px solid {alert_border};border-radius:4px;">
      <div style="font-size:13px;font-weight:700;margin-bottom:8px;">{alert_title}</div>
      <ul style="margin:0;padding-left:18px;font-size:12px;line-height:1.7;color:#374151;">{alert_items}</ul>
    </div>

    <div style="border-top:1px solid #e2e8f0;">{detail_blocks}</div>

    <div style="background:#f8fafc;padding:10px 20px;border-top:1px solid #e2e8f0;text-align:center;font-size:10px;color:#94a3b8;">
      본 브리핑은 투자 조언이 아닙니다 · 종목 변경 시 config/portfolio.yaml 업데이트 · {date_str}
    </div>
  </div>
</body>
</html>"""


def send_email(subject, html, recipient, sender, password):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(sender, password)
        srv.sendmail(sender, [recipient], msg.as_string())


def main():
    config = load_config(os.environ.get("CONFIG_PATH", "config/portfolio.yaml"))

    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    day_ko = {"Monday": "월요일", "Tuesday": "화요일", "Wednesday": "수요일",
               "Thursday": "목요일", "Friday": "금요일", "Saturday": "토요일", "Sunday": "일요일"}
    date_str = f"{now.strftime('%Y년 %m월 %d일')} ({day_ko.get(now.strftime('%A'), '')})"

    print(f"\n{'='*55}")
    print(f" \U0001f4ca Portfolio Briefing  {date_str}")
    print(f"{'='*55}\n")

    evaluated, usdkrw = evaluate_holdings(config)
    if usdkrw:
        print(f"  USD/KRW: {usdkrw:,.0f}")

    weights, grand_total = calculate_weights(evaluated)

    print(f"\n  전체 포트폴리오: {fmt_krw(grand_total)}\n")
    for cat, label in CAT_LABELS.items():
        if cat not in weights:
            continue
        w = weights[cat]
        tgt = config["strategy"]["targets"].get(cat, 0)
        print(f"  {label:22}: {w*100:5.1f}%  (목표 {tgt*100:.0f}%)")

    signals = check_rebalance(weights, grand_total, config)

    if signals:
        print("\n  ⚠️ 리밸런싱 필요:")
        for sig in signals:
            label = CAT_LABELS.get(sig["category"], sig["category"])
            print(f"     {label} {sig['current']*100:.1f}% → {fmt_krw(abs(sig['diff_krw']))} {sig['direction']}")
    else:
        print("\n  ✅ 리밸런싱 불필요")

    html = build_html(evaluated, weights, grand_total, signals, config, date_str, usdkrw)
    subject = f"\U0001f4ca 포트폴리오 {'⚠️ 리밸런싱' if signals else '✅ 정상'} — {date_str}"

    sender = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT_EMAIL", sender)

    print(f"\n  이메일 발송 → {recipient}")
    send_email(subject, html, recipient, sender, password)
    print("  완료!\n")


if __name__ == "__main__":
    main()
