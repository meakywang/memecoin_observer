from solders.keypair import Keypair

# 把你的私钥替换到这里，可以是json数组或base58字符串
# 例子1: json格式
# secret = [12,34,56,...]
# 例子2: base58字符串
# secret = "3hYg...abcd"

secret = "33gBHcvQYBFFvFrutsimEpj2P6oE9xsEbsBWzUuocCqEfCpbndmosToceMKLn4UwSKUzmHNSnD3bZvJeL1vZDFHu"

try:
    if isinstance(secret, str):
        kp = Keypair.from_base58_string(secret)
    else:
        kp = Keypair.from_bytes(bytes(secret))

    print("✅ 公钥地址:", kp.pubkey())
except Exception as e:
    print("❌ 私钥格式错误:", e)
