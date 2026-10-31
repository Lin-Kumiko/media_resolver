"""下载请求的 SSRF 与重定向防护。"""

import asyncio
import ipaddress
import socket
from typing import Any, Iterable, List, Optional, Set
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


class UnsafeMediaURLError(aiohttp.ClientError):
    """媒体 URL 指向不允许访问的地址。"""


class PublicOnlyResolver(AbstractResolver):
    """只向连接器返回公网地址，从连接前消除 DNS rebinding 窗口。"""

    def __init__(
        self,
        allowed_hosts: Optional[Set[str]] = None,
        allowed_ips: Optional[Set[str]] = None,
    ) -> None:
        self._resolver = ThreadedResolver()
        self._allowed_hosts = allowed_hosts or set()
        self._allowed_ips = allowed_ips if allowed_ips is not None else set()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> List[ResolveResult]:
        records = await self._resolver.resolve(host, port, family)
        if not records:
            raise OSError("媒体URL主机名没有可用地址")
        for record in records:
            address = str(record["host"]).split("%", 1)[0]
            if host.lower().rstrip(".") in self._allowed_hosts:
                self._allowed_ips.add(address)
                continue
            try:
                _validate_ip(address, source="连接器DNS解析")
            except UnsafeMediaURLError as exc:
                raise OSError(str(exc)) from exc
        return records

    async def close(self) -> None:
        await self._resolver.close()


def _public_socket_factory(
    address_info,
    delegate=None,
    allowed_ips: Optional[Set[str]] = None,
) -> socket.socket:
    family, sock_type, protocol, _, sockaddr = address_info
    address = str(sockaddr[0]).split("%", 1)[0]
    if address not in (allowed_ips or set()):
        _validate_ip(address, source="连接前")
    if delegate is not None:
        return delegate(address_info)
    return socket.socket(family=family, type=sock_type, proto=protocol)


def create_public_only_connector(
    *,
    trusted_proxy_urls: Iterable[str] = (),
    **kwargs: Any,
) -> aiohttp.TCPConnector:
    """创建下载会话专用连接器；私网代理必须由调用方显式列为受信。"""
    if kwargs.get("resolver") is not None:
        raise ValueError("公共地址连接器不接受外部resolver")
    delegate = kwargs.pop("socket_factory", None)
    allowed_hosts: Set[str] = set()
    allowed_ips: Set[str] = set()
    for proxy_url in trusted_proxy_urls:
        try:
            hostname = (urlsplit(str(proxy_url)).hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if not hostname:
            continue
        allowed_hosts.add(hostname)
        try:
            allowed_ips.add(str(ipaddress.ip_address(hostname.split("%", 1)[0])))
        except ValueError:
            pass

    resolver = PublicOnlyResolver(allowed_hosts, allowed_ips)
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        socket_factory=(
            lambda address_info: _public_socket_factory(
                address_info, delegate, allowed_ips
            )
        ),
        **kwargs,
    )
    # 供安全请求入口与集成测试确认会话使用了连接前防护。
    connector._media_public_only_resolver = resolver
    return connector


def session_uses_public_only_connector(session: aiohttp.ClientSession) -> bool:
    connector = getattr(session, "connector", None)
    return isinstance(
        getattr(connector, "_media_public_only_resolver", None),
        PublicOnlyResolver,
    )


def _validate_ip(value: str, *, source: str) -> None:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError as exc:
        raise UnsafeMediaURLError(f"无法识别{source}地址") from exc
    if not address.is_global:
        raise UnsafeMediaURLError(f"拒绝访问非公网{source}地址")


async def validate_remote_url(url: str) -> None:
    """验证 URL 语法并确保 DNS 的全部结果均为公网地址。"""
    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeMediaURLError("媒体URL格式无效") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeMediaURLError("媒体URL仅允许HTTP或HTTPS协议")
    if not parsed.hostname:
        raise UnsafeMediaURLError("媒体URL缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeMediaURLError("媒体URL不得包含账号凭据")

    hostname = parsed.hostname.rstrip(".")
    try:
        _validate_ip(hostname, source="目标")
        return
    except UnsafeMediaURLError:
        # 字面 IP 已成功解析时必须直接拒绝；域名则继续做 DNS 查询。
        try:
            ipaddress.ip_address(hostname.split("%", 1)[0])
        except ValueError:
            pass
        else:
            raise

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                ascii_hostname,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            timeout=10,
        )
    except (OSError, UnicodeError, asyncio.TimeoutError) as exc:
        raise UnsafeMediaURLError("媒体URL主机名无法解析") from exc

    addresses = {record[4][0] for record in records if record[4]}
    if not addresses:
        raise UnsafeMediaURLError("媒体URL主机名没有可用地址")
    for address in addresses:
        _validate_ip(address, source="DNS解析")


def _connected_peer_ip(response: aiohttp.ClientResponse) -> Optional[str]:
    connection = getattr(response, "connection", None)
    transport = getattr(connection, "transport", None)
    if transport is None:
        protocol = getattr(response, "_protocol", None)
        transport = getattr(protocol, "transport", None)
    if transport is None:
        return None
    peer = transport.get_extra_info("peername")
    if isinstance(peer, (tuple, list)) and peer:
        return str(peer[0])
    if isinstance(peer, str):
        return peer
    return None


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower().rstrip("."),
        parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
    )


async def safe_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    proxy: str = None,
    max_redirects: int = _MAX_REDIRECTS,
    **kwargs,
) -> aiohttp.ClientResponse:
    """逐跳验证 URL 和实际连接地址，返回仍由调用方负责关闭的响应。"""
    if not session_uses_public_only_connector(session):
        raise UnsafeMediaURLError("下载会话未使用公共地址安全连接器")
    current_url = str(url)
    kwargs.pop("allow_redirects", None)
    if kwargs.get("headers"):
        kwargs["headers"] = {
            str(key): value
            for key, value in kwargs["headers"].items()
            if str(key).lower()
            not in {
                "host",
                "content-length",
                "connection",
                "transfer-encoding",
            }
        }

    for redirect_count in range(max_redirects + 1):
        # 受信代理的私网例外只用于连接代理本身，不能扩展到目标 URL。
        # 即使目标最终由代理解析，也必须先在本地证明其全部 DNS 结果为公网地址。
        await validate_remote_url(current_url)
        response = await session.request(
            method,
            current_url,
            proxy=proxy,
            allow_redirects=False,
            **kwargs,
        )
        try:
            # 使用显式代理时 peername 是代理本身，目标只能做逐跳 DNS 校验。
            if not proxy:
                peer_ip = _connected_peer_ip(response)
                if peer_ip:
                    _validate_ip(peer_ip, source="实际连接")

            if response.status not in _REDIRECT_STATUSES:
                return response

            location = response.headers.get("Location")
            if not location:
                return response
            if redirect_count >= max_redirects:
                raise UnsafeMediaURLError("媒体URL重定向次数过多")

            next_url = urljoin(str(response.url), location)
            await validate_remote_url(next_url)
            if _origin(current_url) != _origin(next_url):
                sensitive = {
                    "authorization",
                    "cookie",
                    "proxy-authorization",
                }
                kwargs["headers"] = {
                    key: value
                    for key, value in (kwargs.get("headers") or {}).items()
                    if str(key).lower() not in sensitive
                }
            response.release()
            current_url = next_url
        except BaseException:
            response.close()
            raise

    raise UnsafeMediaURLError("媒体URL重定向次数过多")
