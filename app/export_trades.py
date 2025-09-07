# app/export_trades.py
import re, csv, os

LOG_FILE = os.getenv("LOG_FILE", "run.log")
OUT_CSV  = "trades.csv"

# 匹配样例：
# [BUY_OK] Kinginu DXRP... amt=0.02 sig=2PHfJz...
# [SELL_OK] DXRP... pct=0.5 reason=TP1 2.0x sig=xxxx
p_buy  = re.compile(r"^\[(?:BUY_OK)\]\s+(?P<symbol>\S+)\s+(?P<mint>\S+)\s+amt=(?P<amt>[\d\.]+)\s+sig=(?P<sig>\S+)", re.I)
p_sell = re.compile(r"^\[(?:SELL_OK)\]\s+(?P<mint>\S+)\s+pct=(?P<pct>[\d\.]+)\s+reason=(?P<reason>.+?)\s+sig=(?P<sig>\S+)", re.I)

rows = []
with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        line = line.strip()
        m1 = p_buy.search(line)
        m2 = p_sell.search(line)
        if m1:
            rows.append({
                "time": "",  # run.log 若需要可加时间戳，我保留空列；或你在 main.py 的 log_to_file 里加当前时间
                "side": "BUY",
                "symbol": m1.group("symbol"),
                "mint": m1.group("mint"),
                "amount_sol": m1.group("amt"),
                "percent": "",
                "reason": "",
                "tx": m1.group("sig"),
            })
        elif m2:
            rows.append({
                "time": "",
                "side": "SELL",
                "symbol": "",
                "mint": m2.group("mint"),
                "amount_sol": "",
                "percent": m2.group("pct"),
                "reason": m2.group("reason"),
                "tx": m2.group("sig"),
            })

with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["time","side","symbol","mint","amount_sol","percent","reason","tx"])
    w.writeheader()
    w.writerows(rows)

print(f"✅ 导出完成：{OUT_CSV}（共 {len(rows)} 条）")
print("提示：把 tx 粘到 https://solscan.io/tx/<tx> 可查看链上详情")
