# -*- coding: utf-8 -*-
"""进程级网络环境修复（2026-10-14 实测定位，勿删）。

【问题现象】「拉取数据」异常缓慢：东财日K主源全量失败 → 熔断 →
全市场走新浪/腾讯逐股兜底慢源（每只 >1s × 5500 只 → 数十分钟）。

【根因（本机实测证据）】
1) IPv6 通道异常（首要）：
   push2his / push2.eastmoney.com 等东财行情域名解析 IPv6 优先
   （CNAME → push2hisipv6.trafficmanager.cn）。本机 IPv6 对这类域名
   「TCP 可连、HTTP 层被立即断连」：
       curl -6 → Empty reply from server（0.1s 即失败）
       curl -4 → HTTP 200 OK
   urllib3 在 TCP 已建立、请求阶段收到 EOF 时抛 RemoteDisconnected，
   **不会回退到同域名的其他地址** → 逐股东财K线全量失败。
2) 环境代理干扰（次要）：
   进程若从注入了 HTTP(S)_PROXY=http://127.0.0.1:53618 的终端启动，
   requests 走该代理直接 ProxyError（Max retries exceeded）→ 全挂。

【修复】进程内、幂等、不改动系统设置：
- 只走 IPv4：patch urllib3.util.connection.allowed_gai_family +
  socket.getaddrinfo（family=0/AF_UNSPEC 时强制 AF_INET；调用方显式
  指定 AF_INET6 时保持原样不干预）。
- 不用环境代理：patch requests 的 get_environ_proxies（sessions / utils
  两处引用，覆盖 akshare 内部自建 session）+ 清空进程内代理环境变量
  （含大小写 4+2 个）。调用方显式传入 proxies 时不受影响。

【为什么安全】本项目全部数据源（东财 / 腾讯 / 新浪 / akshare / B站）均为
国内直连源，禁代理无误伤；所有源均实测 IPv4 可用（datacenter-web 等本身
就是 v4-only 域名）。patch 仅作用于本进程，不影响系统或其他程序。

【调用点】app/main.py 顶部（服务进程最早入口）+ app/core/sync.py 顶部
（保障独立 import 脚本场景）。测试：scripts/test_netfix.py。
"""
import logging
import os
import socket

_log = logging.getLogger("core.netfix")
_INSTALLED = False
_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "all_proxy")


def install() -> dict:
    """安装进程级网络修复。幂等：重复调用直接返回（already=True）。"""
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already": True}

    st = {"installed": True, "already": False,
          "env_cleared": [], "requests_patched": False,
          "urllib3_patched": False, "getaddrinfo_patched": False}

    # 1) 清空代理环境变量（后续新建的请求会话不再读到）
    for k in _ENV_KEYS:
        if os.environ.pop(k, None) is not None:
            st["env_cleared"].append(k)

    # 2) requests：忽略环境代理（含 akshare 内部自建 session）
    try:
        import requests.sessions as _rs
        import requests.utils as _ru

        def _no_env_proxies(url, no_proxy=None):  # noqa: ARG001
            return {}

        _rs.get_environ_proxies = _no_env_proxies
        _ru.get_environ_proxies = _no_env_proxies
        st["requests_patched"] = True
    except Exception as e:  # noqa: BLE001
        _log.warning("netfix: requests 代理补丁失败: %s", e)

    # 3) urllib3：DNS 只解析 IPv4
    try:
        from urllib3.util import connection as _u3c  # type: ignore

        _u3c.allowed_gai_family = lambda: socket.AF_INET
        st["urllib3_patched"] = True
    except Exception as e:  # noqa: BLE001
        _log.warning("netfix: urllib3 IPv4 补丁失败: %s", e)

    # 4) socket 兜底：family 未指定时只解析 IPv4
    #    （覆盖非 urllib3 的请求栈；显式 AF_INET6 的调用不干预）
    try:
        _orig_gai = socket.getaddrinfo

        def _v4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
            if family in (0, socket.AF_UNSPEC):
                family = socket.AF_INET
            return _orig_gai(host, port, family, type, proto, flags)

        socket.getaddrinfo = _v4_getaddrinfo
        st["getaddrinfo_patched"] = True
    except Exception as e:  # noqa: BLE001
        _log.warning("netfix: socket.getaddrinfo 补丁失败: %s", e)

    _INSTALLED = True
    _log.info("netfix 已安装（IPv4-only + 忽略环境代理）: %s", st)
    return st


def is_installed() -> bool:
    return _INSTALLED
