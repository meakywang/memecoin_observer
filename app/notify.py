import os, requests

BOT = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def tg_send(text: str):
    """
    发送 Telegram 文本消息。未配置则返回 False。
    返回: (ok: bool, resp_text: str)
    """
    if not BOT or not CHAT:
        return False, "TG_NOT_CONFIGURED"
    try:
        url = f"https://api.telegram.org/bot{BOT}/sendMessage"
        r = requests.post(url, json={"chat_id": CHAT, "text": text}, timeout=8)
        return r.ok, r.text
    except Exception as e:
        return False, repr(e)
