# -*- coding: utf-8 -*-
"""
从链上扫描钱包非零 SPL 代币账户，写入 positions.json
注意：链上拿不到你的买入成本，本脚本将 cost 置为 None。
机器人就能识别“有哪些仓位”，手动/指令卖出没问题；自动 TP/SL 的盈亏计算会因缺成本而受限。
"""
import os, json, time
from decimal import Decimal
from dotenv import load_dotenv
from solana.rpc.api import Client
from solders.pubkey import Pubkey

TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

def main():
    load_dotenv()
    addr = os.environ.get("FROM_ADDRESS", "").strip()
    rpc  = os.environ.get("RPC_URL", "").strip()
    if not addr or not rpc:
        print("❌ 请先在 .env 填好 FROM_ADDRESS 与 RPC_URL")
        return
    owner = Pubkey.from_string(addr)
    cli = Client(rpc)

    # 获取钱包下所有 SPL Token 账户（解析版）
    resp = cli.get_token_accounts_by_owner_json_parsed(owner, {"programId": str(TOKEN_PROGRAM_ID)})
    if resp.value is None:
        print("❌ RPC 返回为空，检查 RPC_URL/网络")
        return

    positions = {}
    count_nonzero = 0
    for acc in resp.value:
        parsed = acc.account.data.parsed
        info = parsed["info"]
        mint = info["mint"]
        ta = info["tokenAmount"]
        ui_amt = Decimal(str(ta.get("uiAmount", 0)))  # 已按 decimals 处理的数量
        dec = int(ta.get("decimals", 0))
        raw_amt = Decimal(str(ta.get("amount", "0")))

        if ui_amt <= 0:
            continue  # 只记录非零余额

        # 生成一个可用的 symbol 占位（你也可以手工再改）
        symbol_guess = mint[:4] + "..." + mint[-4:]

        positions[mint] = {
            "symbol": symbol_guess,
            "mint": mint,
            "decimals": dec,
            "amount_token": float(ui_amt),   # 代币数量（已除以 10**decimals）
            "amount_raw": str(raw_amt),     # 原始整数数量
            "cost_sol": None,               # 成本未知；如需 TP/SL 盈亏，请手工补
            "avg_price": None,
            "opened_at": int(time.time()),
            "last_tx": None,
            "note": "rebuilt_from_chain"
        }
        count_nonzero += 1

    # 备份旧文件
    out_path = "positions.json"
    if os.path.exists(out_path):
        bak = f"positions_backup_{int(time.time())}.json"
        try:
            os.replace(out_path, bak)
            print(f"🗂️ 已备份旧 positions.json -> {bak}")
        except Exception as e:
            print(f"⚠️ 备份旧文件失败：{e}")

    # 写入新 positions.json
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)

    print(f"✅ 已写入 {out_path}，共 {count_nonzero} 条非零持仓。")
    if count_nonzero == 0:
        print("ℹ️ 未发现非零 SPL 代币余额，如已买入但余额为 0，可能已被卖出或在别的钱包/地址。")

if __name__ == "__main__":
    main()
