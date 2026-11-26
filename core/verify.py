"""域名所有權驗證。

兩種方式任選其一即可通過：
1. DNS TXT：在 _loadtest-verify.<domain> 加入內容等於 token 的 TXT 記錄
2. HTTP 檔案：在 https://<domain>/.well-known/loadtest-verify.txt 放置 token

這是壓測產業的標準前置條件。沒有通過驗證的域名不得執行任何流量。
"""
import ipaddress
import secrets
import socket

import aiohttp

try:
    import dns.resolver
    _HAS_DNS = True
except ImportError:  # pragma: no cover
    _HAS_DNS = False


def new_token() -> str:
    return "lt-" + secrets.token_hex(16)


def resolve_ips(domain: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(domain, None)})
    except OSError:
        return []


def check_target_address(domain: str, allow_private: bool = False) -> tuple[bool, str]:
    """拒絕解析到內網/保留位址的目標，避免被用來探測內部網路。"""
    ips = resolve_ips(domain)
    if not ips:
        return False, "域名無法解析，請確認拼寫與 DNS 狀態"
    if allow_private:
        return True, f"解析到 {', '.join(ips)}"
    private = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            private.append(ip)
    if private and len(private) == len(ips):
        return False, (
            f"目標解析到內網或保留位址（{', '.join(private)}）。"
            f"若確實需要測試內部環境，請在 config/targets.json 開啟 allow_private_targets。"
        )
    return True, f"解析到 {', '.join(ips)}"


async def _check_dns(domain: str, token: str, prefix: str, timeout: int = 6):
    if not _HAS_DNS:
        return False, "未安裝 dnspython，無法執行 DNS 驗證"
    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    record = f"{prefix}.{domain}"
    try:
        answers = resolver.resolve(record, "TXT")
    except Exception as exc:
        return False, f"查無 TXT 記錄 {record}（{type(exc).__name__}）"
    for rdata in answers:
        value = b"".join(rdata.strings).decode("utf-8", "replace").strip()
        if value == token:
            return True, f"DNS TXT 記錄 {record} 內容相符"
    return False, f"TXT 記錄 {record} 存在但內容與 token 不符"


async def _check_http(domain: str, token: str, path: str, timeout: int = 10):
    url = f"https://{domain}{path}"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"{url} 返回 HTTP {resp.status}"
                text = (await resp.text(errors="replace")).strip()
    except Exception as exc:
        return False, f"無法取得 {url}（{type(exc).__name__}）"
    if text == token:
        return True, f"HTTP 驗證檔 {url} 內容相符"
    return False, f"{url} 內容與 token 不符"


async def verify_ownership(
    domain: str, token: str, txt_prefix: str, http_path: str
) -> tuple[bool, str]:
    ok, msg = await _check_dns(domain, token, txt_prefix)
    if ok:
        return True, msg
    ok2, msg2 = await _check_http(domain, token, http_path)
    if ok2:
        return True, msg2
    return False, f"{msg}；{msg2}"
