"""代理池：解析清單、驗證、編碼成 JMeter 可用的形式。

支援的清單格式（一行一筆，`#` 開頭為註解）：

    http://user:pass@1.2.3.4:8080
    socks5://user:pass@1.2.3.4:1080
    user:pass@1.2.3.4:8080
    1.2.3.4:8080
    1.2.3.4:8080:user:pass
    1.2.3.4,8080,user,pass

為什麼要編碼成 base64：JMeter 分散式模式下只有 `-G`（全域屬性）會傳遞到
遠端引擎，而 `-J` 不會。屬性值必須是單一字串，因此把整份清單編碼後用
`-Gproxylist=` 一次帶過去，遠端引擎解碼後依執行緒編號挑選。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

FIELD_SEP = "\u0001"
ROW_SEP = "\n"

HTTP_SCHEMES = ("http", "https")
SOCKS_SCHEMES = ("socks5", "socks5h", "socks4")
ALL_SCHEMES = HTTP_SCHEMES + SOCKS_SCHEMES

DEFAULT_SCHEME = "http"


@dataclass(frozen=True)
class Proxy:
    scheme: str
    host: str
    port: int
    user: str = ""
    password: str = ""

    @property
    def is_socks(self) -> bool:
        return self.scheme in SOCKS_SCHEMES

    @property
    def has_auth(self) -> bool:
        return bool(self.user)

    def masked(self) -> str:
        auth = f"{self.user}:***@" if self.user else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def fields(self) -> str:
        return FIELD_SEP.join(
            [self.scheme, self.host, str(self.port), self.user, self.password]
        )


class ProxyError(ValueError):
    """清單格式錯誤。"""


def _clean(value: str) -> str:
    return value.strip().strip("\"'").strip()


def _check(proxy: Proxy, line: str) -> Proxy:
    if not proxy.host:
        raise ProxyError(f"缺少主機位址：`{line}`")
    if not 1 <= proxy.port <= 65535:
        raise ProxyError(f"連接埠不合法（{proxy.port}）：`{line}`")
    if proxy.scheme not in ALL_SCHEMES:
        raise ProxyError(
            f"不支援的協議 `{proxy.scheme}`：`{line}`"
            f"（可用：{'、'.join(ALL_SCHEMES)}）"
        )
    return proxy


def parse_line(line: str) -> Proxy | None:
    """解析一行。空白或註解行回傳 None，格式錯誤丟 ProxyError。"""
    raw = _clean(line)
    if not raw or raw.startswith("#") or raw.startswith("//"):
        return None

    # 1) scheme://user:pass@host:port
    if "://" in raw:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or DEFAULT_SCHEME).lower()
        host = parsed.hostname or ""
        try:
            port = parsed.port or 0
        except ValueError:
            raise ProxyError(f"連接埠格式錯誤：`{raw}`")
        if not port:
            port = 1080 if scheme in SOCKS_SCHEMES else 8080
        return _check(Proxy(scheme, host, port,
                            parsed.username or "", parsed.password or ""), raw)

    # 2) user:pass@host:port
    if "@" in raw:
        userinfo, _, hostpart = raw.rpartition("@")
        user, _, password = userinfo.partition(":")
        host, port = _split_host_port(hostpart, raw)
        return _check(Proxy(DEFAULT_SCHEME, host, port, user, password), raw)

    # 3) 逗號分隔：host,port[,user,pass]
    if "," in raw:
        parts = [_clean(p) for p in raw.split(",")]
        if len(parts) < 2:
            raise ProxyError(f"欄位不足：`{raw}`")
        host, port = parts[0], _to_port(parts[1], raw)
        user = parts[2] if len(parts) > 2 else ""
        password = parts[3] if len(parts) > 3 else ""
        return _check(Proxy(DEFAULT_SCHEME, host, port, user, password), raw)

    # 4) host:port:user:pass
    bits = [_clean(p) for p in raw.split(":")]
    if len(bits) == 4:
        return _check(Proxy(DEFAULT_SCHEME, bits[0], _to_port(bits[1], raw),
                            bits[2], bits[3]), raw)
    if len(bits) == 2:
        return _check(Proxy(DEFAULT_SCHEME, bits[0], _to_port(bits[1], raw)), raw)

    raise ProxyError(f"無法辨識的格式：`{raw}`")


def _to_port(value: str, line: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProxyError(f"連接埠不是數字（`{value}`）：`{line}`")


def _split_host_port(value: str, line: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host:
        raise ProxyError(f"缺少主機位址：`{line}`")
    return host, _to_port(port, line)


def parse_text(text: str) -> tuple[list[Proxy], list[str]]:
    """解析整份清單。回傳 (成功清單, 錯誤訊息清單)。"""
    proxies: list[Proxy] = []
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        try:
            item = parse_line(line)
        except ProxyError as exc:
            errors.append(f"第 {lineno} 行：{exc}")
            continue
        if item is not None:
            proxies.append(item)
    return proxies, errors


def load(path: Path) -> tuple[list[Proxy], list[str]]:
    if not path.exists():
        return [], [f"找不到代理清單：{path}"]
    return parse_text(path.read_text(encoding="utf-8", errors="replace"))


def split_by_scheme(proxies: list[Proxy]) -> tuple[list[Proxy], list[Proxy]]:
    """回傳 (HTTP/HTTPS 代理, SOCKS 代理)。"""
    http = [p for p in proxies if not p.is_socks]
    socks = [p for p in proxies if p.is_socks]
    return http, socks


def encode_for_jmeter(proxies: list[Proxy]) -> str:
    """編成單一字串，供 `-Gproxylist=` 傳遞。"""
    text = ROW_SEP.join(p.fields() for p in proxies)
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def summarize(proxies: list[Proxy]) -> dict:
    counts: dict[str, int] = {}
    for p in proxies:
        counts[p.scheme] = counts.get(p.scheme, 0) + 1
    return {
        "total": len(proxies),
        "by_scheme": counts,
        "with_auth": sum(1 for p in proxies if p.has_auth),
        "unique_hosts": len({p.host for p in proxies}),
    }
