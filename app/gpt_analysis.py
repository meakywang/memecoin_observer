import os, json, requests

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
BUY_TH = float(os.getenv("GPT_BUY_THRESHOLD", "0.65"))

PROMPT_TMPL = """你是Solana新币风控顾问。请依据信息给出是否小仓位买入建议。
请只输出JSON：{{"score":0~1,"verdict":"BUY或SKIP","reason":"简要理由"}}

信息：
- 名称/符号: {symbol}
- Mint: {mint}
- 创建者: {creator}
- 初始买入(估): {init_sol} SOL
- 其它: {extra}
- 规则参考：避免流动性过低/创作者频繁发币/名字乱写/钱包关联度高。
"""

def _call_openai(prompt: str) -> dict:
    if not OPENAI_API_KEY:
        return {"score":0.0,"verdict":"SKIP","reason":"NO_API_KEY"}
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    body = {"model": MODEL, "messages":[{"role":"user","content":prompt}], "temperature":0.2}
    r = requests.post(url, headers=headers, json=body, timeout=20); r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(txt)
    except Exception:
        return {"score":0.0,"verdict":"SKIP","reason":f"BAD_JSON:{txt[:160]}"}

def analyze(symbol: str, mint: str, creator: str, init_sol: float, when: str, extra: str = "") -> dict:
    prompt = PROMPT_TMPL.format(symbol=symbol or "(无符号)", mint=mint, creator=creator or "N/A",
                                init_sol=init_sol or 0, extra=extra or "")
    res = _call_openai(prompt)
    score   = float(res.get("score", 0.0))
    verdict = str(res.get("verdict","SKIP")).upper()
    reason  = str(res.get("reason",""))
    return {"score":score, "verdict":verdict, "reason":reason, "pass":(score>=BUY_TH and verdict=="BUY")}
