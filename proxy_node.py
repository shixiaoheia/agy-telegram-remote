"""Translate a single proxy share link into a sing-box outbound.

Never log the input: share links contain credentials. Unsupported parameters are
rejected rather than silently producing a connection with different semantics.
"""
from __future__ import annotations

import base64
import json
import uuid
from urllib.parse import parse_qsl, unquote, urlsplit


class NodeError(ValueError):
    pass


def _base64(value: str) -> str:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise NodeError("节点编码无效。") from exc


def _port(value: object) -> int:
    try:
        port = int(str(value))
    except (ValueError, TypeError) as exc:
        raise NodeError("节点端口无效。") from exc
    if not 1 <= port <= 65535:
        raise NodeError("节点端口无效。")
    return port


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise NodeError("节点 UUID 无效。") from exc


def _transport(kind: str, host: str, path: str) -> dict | None:
    if kind in ("", "tcp"):
        if host or path:
            raise NodeError("TCP 节点包含不支持的传输参数。")
        return None
    if kind == "ws":
        result: dict = {"type": "ws", "path": path or "/"}
        if host:
            result["headers"] = {"Host": host}
        return result
    if kind == "grpc":
        if host:
            raise NodeError("gRPC 节点包含不支持的 Host 参数。")
        return {"type": "grpc", "service_name": path}
    raise NodeError("暂不支持该传输方式，请使用 sing-box 出站配置。")


def _tls(security: str, server_name: str, fingerprint: str, public_key: str,
         short_id: str, allow_insecure: str) -> dict | None:
    if allow_insecure not in ("", "0", "false"):
        raise NodeError("为保护凭据，不接受跳过 TLS 证书验证的节点。")
    if security in ("", "none"):
        if server_name or fingerprint or public_key or short_id:
            raise NodeError("非 TLS 节点包含 TLS 参数。")
        return None
    if security not in ("tls", "reality"):
        raise NodeError("不支持该 TLS 类型。")
    tls: dict = {"enabled": True}
    if server_name:
        tls["server_name"] = server_name
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    if security == "reality":
        if not public_key or not server_name or not fingerprint:
            raise NodeError("Reality 节点缺少公钥、服务器名称或指纹。")
        tls["reality"] = {"enabled": True, "public_key": public_key, "short_id": short_id}
    elif public_key or short_id:
        raise NodeError("非 Reality 节点包含 Reality 参数。")
    return tls


def parse_node(link: str) -> dict:
    if not isinstance(link, str) or not link or len(link) > 16384 or "\n" in link:
        raise NodeError("请输入一条有效节点链接。")
    if link.startswith("ss://"):
        payload = link[5:].split("#", 1)[0]
        if "?" in payload:
            raise NodeError("Shadowsocks 插件或额外参数暂不支持。")
        if "@" not in payload:
            payload = _base64(payload)
        userinfo, separator, address = payload.rpartition("@")
        if not separator:
            raise NodeError("Shadowsocks 节点格式无效。")
        if ":" not in unquote(userinfo):
            userinfo = _base64(userinfo)
        else:
            userinfo = unquote(userinfo)
        method, separator, password = userinfo.partition(":")
        try:
            endpoint = urlsplit("ss://" + address)
            port = _port(endpoint.port)
        except ValueError as exc:
            raise NodeError("Shadowsocks 地址无效。") from exc
        if not separator or not method or not password or not endpoint.hostname or endpoint.path:
            raise NodeError("Shadowsocks 节点格式无效。")
        return {"type": "shadowsocks", "server": endpoint.hostname, "server_port": port,
                "method": method, "password": password}
    if link.startswith("vmess://"):
        try:
            data = json.loads(_base64(link[8:].split("#", 1)[0]))
        except (ValueError, TypeError) as exc:
            raise NodeError("VMess 节点格式无效。") from exc
        if not isinstance(data, dict):
            raise NodeError("VMess 节点格式无效。")
        allowed = {"v", "ps", "add", "port", "id", "aid", "scy", "net", "type", "host", "path", "tls", "sni", "alpn", "fp"}
        if set(data) - allowed or data.get("type", "none") not in ("", "none") or data.get("alpn"):
            raise NodeError("VMess 节点包含暂不支持的参数。")
        server = data.get("add", "")
        if not isinstance(server, str) or not server:
            raise NodeError("节点服务器无效。")
        try:
            alter_id = int(data.get("aid") or 0)
        except (TypeError, ValueError) as exc:
            raise NodeError("VMess alterId 无效。") from exc
        if not 0 <= alter_id <= 65535:
            raise NodeError("VMess alterId 无效。")
        outbound = {"type": "vmess", "server": server, "server_port": _port(data.get("port")),
                    "uuid": _uuid(str(data.get("id", ""))), "security": data.get("scy") or "auto",
                    "alter_id": alter_id}
        transport = _transport(str(data.get("net") or "tcp"), str(data.get("host") or ""), str(data.get("path") or ""))
        tls = _tls(str(data.get("tls") or ""), str(data.get("sni") or ""), str(data.get("fp") or ""), "", "", "")
    else:
        try:
            parsed = urlsplit(link)
            port = _port(parsed.port)
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True) if parsed.query else []
        except ValueError as exc:
            raise NodeError("节点链接格式无效。") from exc
        kind = parsed.scheme.lower()
        if kind not in ("vless", "anytls", "trojan", "hysteria2", "hy2"):
            raise NodeError("暂不支持此节点协议。")
        if not parsed.hostname or not parsed.username or parsed.password is not None or parsed.path not in ("", "/"):
            raise NodeError("节点服务器或凭据无效。")
        params = dict(pairs)
        if len(params) != len(pairs):
            raise NodeError("节点包含重复参数。")
        allowed = {"security", "sni", "peer", "fp", "pbk", "sid", "allowInsecure", "insecure", "type", "host", "path", "serviceName", "flow", "encryption", "spx", "headerType"}
        if set(params) - allowed:
            raise NodeError("节点包含暂不支持的参数。")
        if params.get("encryption", "none") != "none":
            raise NodeError("不支持此 VLESS 加密方式。")
        outbound = {"type": "hysteria2" if kind == "hy2" else kind,
                    "server": parsed.hostname, "server_port": port}
        secret = unquote(parsed.username)
        if kind == "vless":
            outbound["uuid"] = _uuid(secret)
            if params.get("flow"):
                outbound["flow"] = params["flow"]
        else:
            outbound["password"] = secret
            if params.get("flow") or params.get("encryption"):
                raise NodeError("该协议不支持 flow 或 encryption。")
        security = params.get("security", "none" if kind == "vless" else "tls")
        if "spx" in params and (kind != "vless" or security != "reality" or params["spx"] not in ("", "/")):
            raise NodeError("仅支持 Reality 的默认 spiderX 路径。")
        if "headerType" in params and (params.get("type", "tcp") != "tcp" or params["headerType"] not in ("", "none")):
            raise NodeError("不支持该 TCP 伪装类型。")
        tls = _tls(security, params.get("sni", params.get("peer", "")), params.get("fp", ""),
                   params.get("pbk", ""), params.get("sid", ""), params.get("allowInsecure", params.get("insecure", "")))
        transport = _transport(params.get("type", "tcp"), params.get("host", ""),
                               params.get("path", params.get("serviceName", "")))
        if kind not in ("vless", "trojan") and transport:
            raise NodeError("该协议不支持此传输方式。")
        if kind != "vless" and not tls:
            raise NodeError("该协议必须启用 TLS。")
    if transport:
        outbound["transport"] = transport
    if tls:
        outbound["tls"] = tls
    return outbound


def build_config(outbound: dict, port: int) -> dict:
    """Build an HTTP proxy bound exclusively to the local machine."""
    listen_port = _port(port)
    if not isinstance(outbound, dict) or outbound.get("type") not in {
            "vless", "vmess", "anytls", "trojan", "hysteria2", "shadowsocks"}:
        raise NodeError("不支持的节点类型。")
    node = dict(outbound)
    node["tag"] = "node"
    return {"log": {"level": "warn"},
            "inbounds": [{"type": "http", "listen": "127.0.0.1", "listen_port": listen_port}],
            "outbounds": [node], "route": {"final": "node"}}
