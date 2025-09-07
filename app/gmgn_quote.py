import requests
from urllib.parse import urlencode

SOL_MINT = "So11111111111111111111111111111111111111112"
GMGN_ROUTE_URL = "https://gmgn.ai/defi/router/v1/sol/tx/get_swap_route"

def sol_to_lamports(sol_amount: float) -> int:
    return int(sol_amount * 1_000_000_000)

def get_swap_route(token_out_mint: str, from_address: str, in_amount_sol: float,
                   slippage_pct: float = 10.0, fee: float = 0.006, anti_mev: bool = True):
    """
    注意：GMGN 这里的 slippage 参数为“百分比”(10.0=10%)，不是 bps。
    仅用于展示参考路由；真实下单走 trade.py 的 Jupiter v6。
    """
    params = {
        "token_in_address": SOL_MINT,
        "token_out_address": token_out_mint,
        "in_amount": str(sol_to_lamports(in_amount_sol)),
        "from_address": from_address,
        "slippage": slippage_pct,
        "swap_mode": "ExactIn",
        "fee": fee,
        "is_anti_mev": str(anti_mev).lower(),
    }
    url = f"{GMGN_ROUTE_URL}?{urlencode(params)}"
    r = requests.get(url, timeout=12)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"GMGN route error: {data.get('msg') or data}")
    return {
        "request_url": url,
        "raw": data,
        "quote": (data.get("data") or {}).get("quote") or {},
    }
