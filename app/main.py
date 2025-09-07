# app/main.py
# —— 稳定版：展示新币 → 风控 → 手动/自动下单 → 持仓监控 → 规则卖出
# 亮点：
# 1) DEFAULT_BUY_SOL 从 .env 读取；MAX_BUY_SOL 为硬上限
# 2) MAX_BUYS_PER_MIN 节流（.env 配置）
# 3) 统一 positions.json/run.log 到项目根目录路径
# 4) zero_hits 宽限，避免“刚买入余额暂为0就被误删”
# 5) ✅ 控制台降噪：过滤的不显示；持仓汇总默认2分钟一次；逐笔pnl默认不刷屏

import asyncio, os, json, time, requests, sys, re
from collections import defaultdict, deque
import websockets
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path
from typing import Dict, Tuple, Optional

# 控制台编码（Windows 防乱码）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()

# === 统一文件路径到“项目根目录” ===
ROOT_DIR = Path(__file__).resolve().parents[1]       # .../memecoin_observer
POS_FILE = str(ROOT_DIR / "positions.json")          # 根目录 positions.json
_env_log = os.getenv("LOG_FILE", "run.log").strip()
LOG_FILE = str((ROOT_DIR / _env_log) if not os.path.isabs(_env_log) else Path(_env_log))

# 全局持仓
positions: Dict[str, Dict] = {}

def _load_positions() -> Dict[str, Dict]:
    """从磁盘加载持仓 -> 全局 positions；失败不崩溃并给出提示"""
    global positions
    p = os.path.abspath(POS_FILE)
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                txt = f.read().strip() or "{}"
            positions = json.loads(txt)
        else:
            positions = {}
        print("📂 持仓文件：", p)
        return positions
    except Exception as e:
        print(f"⚠️ 读取 positions.json 失败：{p} -> {e}")
        positions = {}
        return positions

def _save_positions():
    """把内存中的 positions 落盘；失败时明确打印错误"""
    p = os.path.abspath(POS_FILE)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        print(f"💾 已写入持仓：{p}")
    except Exception as e:
        print(f"❗ 写入持仓失败：{p} -> {e}")

# === 常量/环境 ===
WS_URL = "wss://pumpportal.fun/api/data"
FROM_ADDRESS = os.getenv("FROM_ADDRESS", "").strip()

RPC_URL = os.getenv("RPC_URL", "").strip()
DEFAULT_RPC = os.getenv("RPC_FOR_DECIMALS", "https://api.mainnet-beta.solana.com").strip()
ACTIVE_RPC = RPC_URL or DEFAULT_RPC or "https://api.mainnet-beta.solana.com"

# 观察/安全阈值
MIN_INIT_SOL   = float(os.getenv("MIN_INIT_SOL", "0.2"))
MAX_BUY_SOL    = float(os.getenv("MAX_BUY_SOL", "0.2"))
COOLDOWN_SEC   = int(float(os.getenv("COOLDOWN_SEC", "20")))

# 默认买入额（来自 .env）
DEFAULT_BUY_SOL = float(os.getenv("DEFAULT_BUY_SOL", "0.05"))

# 自动化/GPT
USE_GPT  = os.getenv("USE_GPT_ANALYSIS", "false").lower() == "true"
AUTO_BUY = os.getenv("AUTO_BUY", "false").lower() == "true"
AUTO_SELL= os.getenv("AUTO_SELL", "true").lower() == "true"

# 卖出规则
TP1_X = float(os.getenv("TP1_X", "2.0"))
TP1_SELL_PCT = float(os.getenv("TP1_SELL_PCT", "0.5"))
TP2_X = float(os.getenv("TP2_X", "3.0"))
SL_PCT = float(os.getenv("SL_PCT", "0.35"))

# 展示优先（避免“看不见新币”）
SHOW_NEW_TOKEN_BEFORE_FILTER = os.getenv("SHOW_NEW_TOKEN_BEFORE_FILTER", "true").lower() == "true"

# 风控
ENABLE_FILTERS = os.getenv("ENABLE_FILTERS", "true").lower() == "true"
BLACK_WORDS = [w.strip() for w in os.getenv("BLACK_WORDS", "ai,elon,musk,porno,xxx,scam,rug,sex,moonshot").split(",") if w.strip()]
SAFE_SYMBOL_HINTS = [w.strip() for w in os.getenv("SAFE_SYMBOL_HINTS", "doge,cat,pepe,memesol").split(",") if w.strip()]
SAFE_HINT_DISCOUNT = float(os.getenv("SAFE_HINT_DISCOUNT", "0.5"))
CREATOR_BURST_WINDOW_SEC = int(os.getenv("CREATOR_BURST_WINDOW_SEC", "60"))
CREATOR_BURST_MAX = int(os.getenv("CREATOR_BURST_MAX", "3"))

# 输出控制（新增）
VERBOSE_FILTERS = os.getenv("VERBOSE_FILTERS", "false").lower() == "true"              # 是否在控制台打印“被过滤”的币
PRINT_PNL_EACH_CHECK = os.getenv("PRINT_PNL_EACH_CHECK", "false").lower() == "true"    # 是否每次监控都打印单币 pnl
PORTFOLIO_INTERVAL_SEC = int(os.getenv("PORTFOLIO_INTERVAL_SEC", "120"))               # 资产汇总打印间隔（默认2分钟）
VERBOSE_HEARTBEAT = os.getenv("VERBOSE_HEARTBEAT", "false").lower() == "true"          # 是否打印“心跳正常”
VERBOSE_RAW = os.getenv("VERBOSE_RAW", "false").lower() == "true"                      # 是否打印原始WS片段

# 过滤后的TG告警是否发送
ALERT_FILTERED_TO_TG = os.getenv("ALERT_FILTERED_TO_TG", "true").lower() == "true"

# zero_hits 最大宽限次数（默认15次 ≈ 90s）
ZERO_HITS_MAX = int(os.getenv("ZERO_HITS_MAX", "15"))

# 全局节流
MAX_BUYS_PER_MIN = int(os.getenv("MAX_BUYS_PER_MIN", "2"))
_buy_ticks = deque()

# Telegram（可选）
def _tg_noop(_): return (False, "TG_NOT_CONFIGURED")
try:
    from app.notify import tg_send as _tg_send
except Exception:
    _tg_send = _tg_noop

def tg_send_safe(text: str):
    try:
        return _tg_send(text)
    except Exception as e:
        return (False, repr(e))

def log_to_file(line: str, path: str = LOG_FILE):
    """统一把日志写到根目录的 run.log（或 .env 指定的路径）"""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        pass

# 交易接口
from app.trade import (
    buy_token_by_amount_sol, sell_token_for_sol,
    get_token_balance, get_token_decimals
)

# 环境校验
def _validate_env():
    errs = []
    if not FROM_ADDRESS:
        errs.append("FROM_ADDRESS 未配置")
    if not ACTIVE_RPC:
        errs.append("无可用 RPC，请设置 RPC_URL 或 RPC_FOR_DECIMALS")
    if AUTO_BUY or AUTO_SELL:
        if not (os.getenv("PRIVATE_KEY_BASE58", "") or os.getenv("PRIVATE_KEY_JSON", "")):
            errs.append("未提供 PRIVATE_KEY_BASE58/JSON（交易功能需要）")
    if DEFAULT_BUY_SOL <= 0:
        errs.append("DEFAULT_BUY_SOL 必须 > 0")
    if errs:
        raise RuntimeError("环境变量错误：\n- " + "\n- ".join(errs))
    if DEFAULT_BUY_SOL > MAX_BUY_SOL:
        print(f"⚠️ 提示：DEFAULT_BUY_SOL={DEFAULT_BUY_SOL} 大于 MAX_BUY_SOL={MAX_BUY_SOL}。"
              f"实际下单时将因超过上限被拒绝。建议在 .env 调整。")

# GPT（可选）
def gpt_judge(symbol, mint, creator, init_sol, when):
    if not USE_GPT:
        return {"pass": True, "score": 1.0, "verdict": "SKIP", "reason": "GPT_OFF"}
    try:
        from app.gpt_analysis import analyze
        res = analyze(symbol, mint, creator or "", init_sol, when, extra="Pump.fun new token")
        print(f"🤖 GPT建议：score={res['score']:.2f} verdict={res['verdict']} reason={res['reason']}")
        tg_send_safe(f"🤖 GPT建议\nToken: {symbol or '(无)'}\nScore:{res['score']:.2f} Verdict:{res['verdict']}\n{res['reason']}")
        return res
    except Exception as e:
        print(f"GPT分析失败：{e}（继续人工规则）")
        return {"pass": True, "score": 0.0, "verdict": "ERR", "reason": "ERR"}

# 风控
_creator_hits: Dict[str, deque] = defaultdict(deque)
_word_re = re.compile(r"[a-z0-9]+", re.I)
def _norm(s: str) -> str:
    return " ".join(_word_re.findall((s or "").lower()))

def passes_filters(symbol: str, name: str, creator: str, init_sol: float) -> Tuple[bool, str]:
    """返回 (ok: bool, reason: str)"""
    if not ENABLE_FILTERS:
        return True, "FILTERS_OFF"

    sym = _norm(symbol); nam = _norm(name); txt = f"{sym} {nam}"
    for w in BLACK_WORDS:
        if w and w.lower() in txt:
            return False, f"BLACKWORD:{w}"

    th = MIN_INIT_SOL
    if any(h in txt for h in SAFE_SYMBOL_HINTS):
        th = max(MIN_INIT_SOL * SAFE_HINT_DISCOUNT, 0.01)
    if init_sol is not None and init_sol < th:
        return False, f"LOW_INIT_SOL:{init_sol:.3f}<{th:.3f}"

    now = time.time()
    dq = _creator_hits[creator or "unknown"]; dq.append(now)
    while dq and now - dq[0] > CREATOR_BURST_WINDOW_SEC:
        dq.popleft()
    if len(dq) > CREATOR_BURST_MAX:
        return False, f"CREATOR_BURST>{CREATOR_BURST_MAX} in {CREATOR_BURST_WINDOW_SEC}s"

    return True, "OK"

# —— 余额 & 资产小结 —— #
def get_sol_balance(pubkey: str, rpc_url: str) -> float:
    """返回钱包 SOL 余额（单位 SOL）"""
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [pubkey, {"commitment": "processed"}]
        }
        r = requests.post(rpc_url, json=payload, timeout=10).json()
        lamports = int(r["result"]["value"])
        return lamports / 1_000_000_000
    except Exception as e:
        print(f"❗ 查询 SOL 余额失败：{e}")
        return -1.0

def _quote_token_value_in_sol(mint: str, amount_smallest: int) -> Optional[float]:
    """用 Jupiter 估当前 token 数量可换多少 SOL（失败返回 None）"""
    try:
        url = ("https://quote-api.jup.ag/v6/quote"
               f"?inputMint={mint}"
               f"&outputMint=So11111111111111111111111111111111111111112"
               f"&amount={amount_smallest}&slippageBps=50")
        q = requests.get(url, timeout=10).json()
        out = int(q.get("outAmount", "0"))
        return out / 1_000_000_000
    except Exception:
        return None

def print_wallet_status():
    """打印钱包余额 + 已记录持仓的小结（成本/估值/盈亏倍数/合计）"""
    if not FROM_ADDRESS:
        print("⚠️ 未配置 FROM_ADDRESS，无法展示资产小结。"); return

    sol_bal = get_sol_balance(FROM_ADDRESS, ACTIVE_RPC)
    if sol_bal >= 0:
        print(f"💰 钱包余额：{sol_bal:.4f} SOL")

    if not positions:
        print("📦 当前无记录持仓（positions.json 为空）。")
        return

    total_cost = 0.0
    total_now  = 0.0
    lines = []
    for mint, pos in positions.items():
        sym   = pos.get("symbol", "")
        cost  = float(pos.get("in_sol", 0.0))
        total_cost += cost

        # 当前数量（最小单位）
        bal_smallest, dec = (0, pos.get("decimals", 9))
        try:
            bal_smallest, dec = get_token_balance(FROM_ADDRESS, mint, ACTIVE_RPC)
        except Exception:
            pass

        cur_val = None
        if bal_smallest > 0:
            cur_val = _quote_token_value_in_sol(mint, bal_smallest)

        if cur_val is None:
            rr = None
            now_str = "N/A"
        else:
            total_now += cur_val
            rr = (cur_val / cost) if cost > 0 else None
            now_str = f"{cur_val:.4f} SOL"

        rr_str = f"{rr:.2f}x" if rr is not None else "N/A"
        lines.append(f"- {sym or mint[:6]}  成本:{cost:.4f}  现估:{now_str}  盈亏:{rr_str}")

    print("📊 持仓小结：")
    for ln in lines:
        print("  " + ln)

    if total_cost > 0:
        total_rr = (total_now / total_cost) if total_now > 0 else 0.0
        print(f"📈 合计：成本 {total_cost:.4f} SOL  |  现估 {total_now:.4f} SOL  |  总盈亏 {total_rr:.2f}x")

# 买入（含冷却/节流）
_last_buy_ts = 0.0
async def maybe_buy(mint: str, symbol: str, est_amt_sol: float = None):
    global _last_buy_ts
    # 默认买入额
    if est_amt_sol is None:
        est_amt_sol = DEFAULT_BUY_SOL

    now = time.time()
    # 每分钟节流
    while _buy_ticks and now - _buy_ticks[0] > 60:
        _buy_ticks.popleft()
    if len(_buy_ticks) >= MAX_BUYS_PER_MIN:
        print(f"⛔ 已达每分钟买入上限（MAX_BUYS_PER_MIN={MAX_BUYS_PER_MIN}），跳过。")
        tg_send_safe(f"⛔ 触发全局节流：每分钟最多 {MAX_BUYS_PER_MIN} 笔买入。")
        return

    amt = est_amt_sol
    if amt > MAX_BUY_SOL:
        print(f"❌ 超过单笔上限 {MAX_BUY_SOL} SOL"); return
    if now - _last_buy_ts < COOLDOWN_SEC:
        print(f"⌛ 冷却中，请稍后"); return

    if not AUTO_BUY:
        cmd = (await asyncio.to_thread(
            input, f"是否买入 {symbol or '(无符号)'} ? 输入 YES 执行（默认{DEFAULT_BUY_SOL} SOL，可输入 BUY 0.03 修改）："
        )).strip().lower()
        if cmd.startswith("buy"):
            try:
                amt = float(cmd.split()[1])
            except:
                print(f"金额解析失败，使用默认 {DEFAULT_BUY_SOL}"); amt = DEFAULT_BUY_SOL
        elif cmd not in ("yes","y","ok"):
            print("已取消。"); return
        if amt > MAX_BUY_SOL:
            print(f"❌ 超过单笔上限 {MAX_BUY_SOL} SOL"); return

    try:
        print("📝 正在下单（Jupiter）...")
        tx_sig, quote = buy_token_by_amount_sol(mint, amt)
        _last_buy_ts = time.time(); _buy_ticks.append(_last_buy_ts)
        out_min = int((quote or {}).get("outAmount","0"))
        dec = get_token_decimals(mint, ACTIVE_RPC)
        positions[mint] = {
            "symbol": symbol or "", "in_sol": amt, "min_out": out_min,
            "decimals": dec, "tp1_done": False, "ts": int(time.time()),
            "zero_hits": 0,  # 余额为0的宽限计数器
        }
        _save_positions()
        print(f"✅ 已买入 {amt} SOL -> {symbol or ''}, tx: {tx_sig}")
        tg_send_safe(f"✅ 已买入 {symbol or '(无)'}\nMint:{mint}\n金额:{amt} SOL\nTx:https://solscan.io/tx/{tx_sig}")
        log_to_file(f"[BUY_OK] {symbol} {mint} amt={amt} sig={tx_sig}")
        print_wallet_status()
    except Exception as e:
        print(f"❌ 下单失败：{e}")
        tg_send_safe(f"❌ 下单失败 {symbol or '(无)'}: {e}")
        log_to_file(f"[BUY_ERR] {symbol} {mint} err={e}")

# 卖出
def sell_percent(mint: str, pct: float, reason: str):
    try:
        owner = FROM_ADDRESS
        if not owner:
            print("⚠️ 未配置 FROM_ADDRESS，无法卖出"); return
        bal_smallest, dec = get_token_balance(owner, mint, ACTIVE_RPC)
        to_sell = int(bal_smallest * pct)
        sym = positions.get(mint, {}).get("symbol", "") or mint[:6]
        if to_sell <= 0:
            print(f"⚠️ 可卖数量为 0，跳过 [{sym}]"); return
        sig, q = sell_token_for_sol(mint, to_sell)
        print(f"✅ 已卖出 {pct*100:.0f}% [{sym}]，tx: {sig}")
        tg_send_safe(f"✅ 卖出 {pct*100:.0f}% [{sym}]\nMint:{mint}\n原因:{reason}\nTx:https://solscan.io/tx/{sig}")
        log_to_file(f"[SELL_OK] {mint} pct={pct} reason={reason} sig={sig}")
        print_wallet_status()
    except Exception as e:
        print(f"❌ 卖出失败：{e}")
        tg_send_safe(f"❌ 卖出失败 {mint}: {e}")
        log_to_file(f"[SELL_ERR] {mint} err={e}")

# 持仓监控（报价/TP/SL）
async def monitor_positions():
    while True:
        try:
            if not positions:
                await asyncio.sleep(5); continue
            for mint, pos in list(positions.items()):
                bal_smallest, dec = get_token_balance(FROM_ADDRESS, mint, ACTIVE_RPC)

                # 宽限逻辑：余额为0时，不要立刻删除；累计多次才认为无持仓
                if bal_smallest <= 0:
                    pos["zero_hits"] = int(pos.get("zero_hits", 0)) + 1
                    if pos["zero_hits"] < ZERO_HITS_MAX:
                        # 仅在需要时打印，以免噪音
                        # print(f"⏳ {pos.get('symbol','')} 余额暂为0（{pos['zero_hits']}次），稍后再查…")
                        _save_positions()
                        continue
                    else:
                        # 宽限期结束仍为0，移除
                        # print(f"🧹 {pos.get('symbol','')} 连续为0，认为已无持仓，移除记录")
                        positions.pop(mint, None); _save_positions(); continue
                else:
                    if pos.get("zero_hits"):
                        pos["zero_hits"] = 0
                        _save_positions()

                # 报价
                try:
                    url = ("https://quote-api.jup.ag/v6/quote"
                           f"?inputMint={mint}&outputMint=So11111111111111111111111111111111111111112"
                           f"&amount={bal_smallest}&slippageBps=50")
                    cur = requests.get(url, timeout=10).json()
                    out = int(cur.get("outAmount","0"))
                except Exception:
                    # print(f"❗ 报价失败，稍后重试: {mint}")
                    continue

                cur_sol = out / 1_000_000_000
                rr = cur_sol / max(pos["in_sol"],1e-9)
                sym = pos.get("symbol","") or mint[:6]

                if PRINT_PNL_EACH_CHECK:
                    print(f"📈 {sym} pnl={rr:.2f}x（持仓估{cur_sol:.4f} SOL / 入场 {pos['in_sol']}）")

                if AUTO_SELL:
                    # TP2：全清
                    if rr >= TP2_X:
                        sell_percent(mint, 1.0, f"TP2 {TP2_X}x")
                        positions.pop(mint, None); _save_positions(); continue
                    # TP1：部分
                    if (not pos.get("tp1_done")) and rr >= TP1_X:
                        sell_percent(mint, TP1_SELL_PCT, f"TP1 {TP1_X}x 部分")
                        pos["tp1_done"] = True; _save_positions(); continue
                    # SL：全清
                    if rr <= (1.0 - SL_PCT):
                        sell_percent(mint, 1.0, f"SL -{SL_PCT*100:.0f}%")
                        positions.pop(mint, None); _save_positions(); continue
            await asyncio.sleep(6)
        except Exception as e:
            print(f"监控异常：{e}")
            await asyncio.sleep(6)

# 资产定时打印（默认2分钟）
async def portfolio_loop():
    while True:
        try:
            print_wallet_status()
        except Exception as e:
            print(f"资产小结异常：{e}")
        await asyncio.sleep(PORTFOLIO_INTERVAL_SEC)

# 工具
_seen = set()
def _links(mint: str):
    return (f"https://gmgn.ai/sol/token/{mint}",
            f"https://birdeye.so/token/{mint}?chain=solana",
            f"https://pump.fun/coin/{mint}")

# 主监听
async def listen():
    retry = 0
    while True:
        try:
            print(f"🔌 连接 {WS_URL} ...")
            async with websockets.connect(
                WS_URL, ping_interval=20, ping_timeout=20,
                open_timeout=15, close_timeout=5
            ) as ws:
                print("✅ 已连，发送订阅...")
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                print("📨 订阅完成，等待事件..."); last = time.time()
                globals()["_last_dbg"] = 0.0

                async for raw in ws:
                    if VERBOSE_RAW and time.time() - globals().get("_last_dbg", 0) > 10:
                        print("⚙️ 收到原始消息片段：", str(raw)[:200])
                        globals()["_last_dbg"] = time.time()

                    if VERBOSE_HEARTBEAT and time.time() - last > 20:
                        print("⏱️ 心跳正常"); last = time.time()

                    try:
                        data = json.loads(raw)
                    except:
                        continue
                    if not isinstance(data, dict):
                        continue

                    mint = symbol = name = creator = None
                    when = "N/A"; init_sol = 0.0

                    evt_type = data.get("type")
                    if evt_type == "newToken":
                        mint   = data.get("mint")
                        symbol = data.get("symbol")
                        name   = data.get("name") or symbol
                        creator= data.get("creator")
                        ts     = data.get("timestamp")
                        when   = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "N/A"
                        init_sol = float(data.get("solAmount") or 0.0)
                    elif all(k in data for k in ("signature", "mint", "traderPublicKey", "txType", "initialBuy")):
                        if str(data.get("txType","")).lower() == "create" and bool(data.get("initialBuy")):
                            mint    = data.get("mint")
                            symbol  = data.get("symbol") or data.get("name")
                            name    = data.get("name") or symbol
                            creator = data.get("traderPublicKey") or data.get("creator")
                            ts      = data.get("timestamp") or int(time.time())
                            when    = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                            init_sol= float(data.get("solAmount") or 0.0)
                        else:
                            continue
                    else:
                        continue

                    if not mint or mint in _seen:
                        continue
                    _seen.add(mint)

                    # 先展示，再风控（保留链接展示）
                    if SHOW_NEW_TOKEN_BEFORE_FILTER:
                        gmgn, be, pump = _links(mint)
                        print("\n==============================")
                        print(f"🆕 新币: {symbol or '(无)'} (pre-filter)")
                        print(f"Mint: {mint}")
                        if creator: print(f"创建者: {creator}")
                        print(f"时间: {when}")
                        if init_sol: print(f"初始池子: {init_sol:.3f} SOL")
                        print("🔗 GMGN:", gmgn)
                        print("🔗 Birdeye:", be)
                        print("🔗 Pump:", pump)
                        print("==============================")
                        log_to_file(f"[NEW_RAW] {when} {symbol or '(nosym)'} {mint} creator={creator or 'N/A'} init={init_sol}")

                    # 风控
                    ok, reason = passes_filters(symbol or "", name or "", creator or "", init_sol)
                    if not ok:
                        # 控制台不显示被过滤的币；仍然写日志；按需发TG
                        msg = (f"⛔ 过滤 {symbol or '(无)'}  Mint:{mint}\n"
                               f"原因:{reason}  initSOL:{init_sol:.3f}\n"
                               f"创建者:{creator or 'N/A'}")
                        if VERBOSE_FILTERS:
                            print(msg)
                        log_to_file(f"[FILTER] {reason} {symbol} {mint}")
                        if ALERT_FILTERED_TO_TG:
                            tg_send_safe(msg)
                        continue

                    if not SHOW_NEW_TOKEN_BEFORE_FILTER:
                        gmgn, be, pump = _links(mint)
                        print("\n==============================")
                        print(f"🆕 新币: {symbol or '(无)'}")
                        print(f"Mint: {mint}")
                        if creator: print(f"创建者: {creator}")
                        print(f"时间: {when}")
                        if init_sol: print(f"初始池子: {init_sol:.3f} SOL")
                        print("🔗 GMGN:", gmgn)
                        print("🔗 Birdeye:", be)
                        print("🔗 Pump:", pump)
                        print("==============================")
                        log_to_file(f"[NEW] {when} {symbol or '(nosym)'} {mint} creator={creator or 'N/A'} init={init_sol}")

                    # GPT（可选）
                    g = gpt_judge(symbol, mint, creator or "", init_sol, when)
                    if not g.get("pass", True):
                        # print("⚠️ GPT未通过，跳过。")
                        continue

                    # 进入买入流程（AUTO_BUY=false 时会询问）
                    await maybe_buy(mint, symbol)  # 使用 DEFAULT_BUY_SOL

        except Exception as e:
            print(f"❌ 连接异常：{e!r}")
        retry = min(retry + 1, 10)
        print(f"🔁 {retry} 秒后重连..."); await asyncio.sleep(retry)

async def main():
    _validate_env(); _load_positions()
    print("✅ 配置：",
          f"MIN_INIT_SOL={MIN_INIT_SOL}  MAX_BUY_SOL={MAX_BUY_SOL}  COOL={COOLDOWN_SEC}s  ",
          f"AUTO_BUY={AUTO_BUY}  AUTO_SELL={AUTO_SELL}  FILTERS={ENABLE_FILTERS}  ",
          f"MAX_BUYS_PER_MIN={MAX_BUYS_PER_MIN}  DEFAULT_BUY_SOL={DEFAULT_BUY_SOL}")
    print(f"🗂 位置：positions.json -> {POS_FILE}")
    print(f"🗂 日志：run.log -> {LOG_FILE}")
    print(f"🧰 输出控制：VERBOSE_FILTERS={VERBOSE_FILTERS}  PRINT_PNL_EACH_CHECK={PRINT_PNL_EACH_CHECK}  "
          f"PORTFOLIO_INTERVAL_SEC={PORTFOLIO_INTERVAL_SEC}  VERBOSE_HEARTBEAT={VERBOSE_HEARTBEAT}  VERBOSE_RAW={VERBOSE_RAW}")
    print(f"🛡️ zero_hits 宽限：ZERO_HITS_MAX={ZERO_HITS_MAX}")

    # Telegram 指令循环（可选）
    try:
        from app.notify import start_command_loop
        asyncio.create_task(start_command_loop())
        print("✅ Telegram 指令循环已启动。支持 /ping /status /positions /buy /sell /help")
    except Exception:
        pass

    # 定时资产小结 & 持仓监控 & 主监听
    asyncio.create_task(portfolio_loop())
    asyncio.create_task(monitor_positions())
    await listen()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出。")
