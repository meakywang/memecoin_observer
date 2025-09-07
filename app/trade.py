import os, json, base64, time, requests, base58
from typing import Tuple

from dotenv import load_dotenv
load_dotenv()

from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

RPC_URL = os.getenv("RPC_URL","").strip()
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS","50"))
SOL_MINT = "So11111111111111111111111111111111111111112"

def _client() -> Client:
    if not RPC_URL: raise RuntimeError("请在 .env 设置 RPC_URL")
    return Client(RPC_URL)

def _lamports(sol: float) -> int:
    return int(sol * 1_000_000_000)

def _load_keypair() -> Keypair:
    kjson = os.getenv("PRIVATE_KEY_JSON","").strip()
    if kjson.startswith("["):
        arr = json.loads(kjson)
        return Keypair.from_bytes(bytes(arr))
    kb58 = os.getenv("PRIVATE_KEY_BASE58","").strip()
    if kb58:
        return Keypair.from_bytes(base58.b58decode(kb58))
    raise RuntimeError("未提供 PRIVATE_KEY_BASE58/JSON")

# ---------- Jupiter v6 ----------
def _jup_quote(input_mint: str, output_mint: str, amount: int) -> dict:
    url = ( "https://quote-api.jup.ag/v6/quote"
            f"?inputMint={input_mint}&outputMint={output_mint}"
            f"&amount={amount}&slippageBps={SLIPPAGE_BPS}&onlyDirectRoutes=false")
    r = requests.get(url, timeout=10); r.raise_for_status()
    return r.json()

def _jup_swap_tx(quote_json: dict, user_pubkey: str) -> str:
    url = "https://quote-api.jup.ag/v6/swap"
    payload = {"quoteResponse": quote_json, "userPublicKey": user_pubkey,
               "wrapAndUnwrapSol": True, "dynamicSlippage": True,
               "prioritizationFeeLamports": "auto"}
    r = requests.post(url, json=payload, timeout=25); r.raise_for_status()
    data = r.json()
    if "swapTransaction" not in data: raise RuntimeError(f"Jupiter返回异常:{data}")
    return data["swapTransaction"]

def _sign_send(b64_tx: str, kp: Keypair) -> str:
    raw = base64.b64decode(b64_tx)
    # 用 from_bytes 解析 Jupiter 返回的 base64 交易
    vtx = VersionedTransaction.from_bytes(raw)
    # ！不要 vtx.sign([.])（老版本 solders 没这个方法）
    # 正确做法：基于原 transaction 的 message，构造“已签名”的新交易
    signed_vtx = VersionedTransaction(vtx.message, [kp])
    # 发送签好名的交易
    cli = _client()
    sig = cli.send_raw_transaction(bytes(signed_vtx), opts=TxOpts(skip_preflight=True, max_retries=3))
    return sig.value

# ---------- 公共工具 ----------
def get_token_decimals(mint: str, rpc_url: str) -> int:
    try:
        r = requests.post(rpc_url, json={"jsonrpc":"2.0","id":1,"method":"getTokenSupply","params":[mint]}, timeout=8)
        return int(r.json()["result"]["value"]["decimals"])
    except Exception:
        return 9

def get_token_balance(owner: str, mint: str, rpc_url: str) -> Tuple[int,int]:
    """返回 (最小单位数量, decimals)"""
    try:
        payload = {"jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner",
                   "params":[owner,{"mint":mint},{"encoding":"jsonParsed"}]}
        r = requests.post(rpc_url, json=payload, timeout=10); r.raise_for_status()
        v = r.json().get("result", {}).get("value", [])
        total = 0; dec = 9
        for it in v:
            amt = it["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += int(amt["amount"]); dec = int(amt["decimals"])
        return total, dec
    except Exception:
        return 0, 9

# ---------- 交易：买入 / 卖出 ----------
def buy_token_by_amount_sol(token_out_mint: str, amount_sol: float) -> Tuple[str, dict]:
    kp = _load_keypair()
    quote = _jup_quote(SOL_MINT, token_out_mint, _lamports(amount_sol))
    b64 = _jup_swap_tx(quote, str(kp.pubkey()))
    sig = _sign_send(b64, kp)
    return sig, quote

def sell_token_for_sol(token_mint: str, amount_token_smallest: int) -> Tuple[str, dict]:
    kp = _load_keypair()
    quote = _jup_quote(token_mint, SOL_MINT, amount_token_smallest)
    b64 = _jup_swap_tx(quote, str(kp.pubkey()))
    sig = _sign_send(b64, kp)
    return sig, quote
