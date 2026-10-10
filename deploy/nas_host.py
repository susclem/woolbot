"""挑一个能连上的 NAS 地址：局域网优先，Tailscale 兜底。

同一台 NAS 往往有两个都能连上的地址：在家走局域网（`192.168.x.x` 之类，直连最快），
在外面走 Tailscale（`100.x.x.x`）。两个地址都从**环境变量**读，谁通就用谁：

    # Linux / macOS
    export NAS_HOST_LAN=192.168.1.10
    export NAS_HOST_TS=100.101.102.103
    # Windows PowerShell
    $env:NAS_HOST_LAN='192.168.1.10'; $env:NAS_HOST_TS='100.101.102.103'

优先级（从高到低）：

1. `--host` 参数（调用方直接给地址）
2. `NAS_HOST` 环境变量（临时强制指定，调试时用）
3. `NAS_HOST_LAN` / `NAS_HOST_TS` 两个候选：**先探局域网，不通再探 Tailscale**
   —— 在家时局域网一次就中，在外面时那 1 秒是纯等待，所以局域网的超时给得短

一个都没配就报错退出：这里**故意不留任何默认地址**。把真实地址写死在代码里，
公开出去等于把自家的门牌号贴在网上；真实地址只该待在你自己的环境变量里。
"""
from __future__ import annotations

import os
import socket
from functools import lru_cache

#: 探 Tailscale / 远端的超时。公网绕一圈，给宽容点。
PROBE_TIMEOUT = 2.0

#: 探局域网用更短的超时：同一个 WiFi 里的设备要么几十毫秒就应答，要么就是
#: 我们不在家（SYN 有去无回，只能等满超时）。在外面时这一秒是纯等待。
LAN_PROBE_TIMEOUT = 1.0

#: 说明怎么配地址，报错时原样打出来。
HOWTO = (
    "请先告诉它要连哪台机器（三选一）：\n"
    "    PowerShell:  $env:NAS_HOST='192.168.1.10'\n"
    "    bash:        export NAS_HOST=192.168.1.10\n"
    "或者给出两个候选，让它自己挑一个通的（在家走局域网、在外面走 Tailscale）：\n"
    "    NAS_HOST_LAN=192.168.1.10  NAS_HOST_TS=100.101.102.103"
)


def _env_candidates() -> tuple[str, ...]:
    """按「局域网在前、Tailscale 在后」的顺序收集候选地址（去重、去空）。"""
    out: list[str] = []
    for name in ("NAS_HOST_LAN", "NAS_HOST_TS"):
        val = (os.environ.get(name) or "").strip()
        if val and val not in out:
            out.append(val)
    return tuple(out)


@lru_cache(maxsize=None)
def _probe_cached(host: str, port: int, timeout: float) -> bool:
    """连上之后必须看到 SSH 的标识串（`SSH-`），才算「这是我们要找的那台机器」。

    只探 TCP 会撒谎：家里的网段里别的设备占着同一个 IP 时 TCP 也能秒开，于是
    resolve() 选了它，paramiko 连上去干等二十秒才超时。SSH 服务端一接上就先亮
    `SSH-2.0-...`，多读这一次不会慢多少，却能把「别人家的 IP」和真的机器分开。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = b""
            while len(data) < 4:          # 只认开头这 4 个字节，够了
                chunk = sock.recv(64)
                if not chunk:
                    break
                data += chunk
            return data.startswith(b"SSH-")
    except OSError:
        return False


def probe(host: str, port: int = 22, timeout: float | None = None) -> bool:
    """探一下这个地址通不通：TCP 连得上**并且**对面亮 SSH 标识串。不认证。

    超时不传就按地址自己定（局域网 1 秒、其余 2 秒）。结果按 (地址, 端口, 超时)
    缓存 —— 同一个进程里 resolve() 探过之后，describe() 不会再各等一遍。
    """
    if timeout is None:
        lan = (os.environ.get("NAS_HOST_LAN") or "").strip()
        timeout = LAN_PROBE_TIMEOUT if host == lan else PROBE_TIMEOUT
    return _probe_cached(host, port, float(timeout))


def resolve(port: int = 22) -> str:
    """返回这次该用哪个地址：按候选顺序探，第一个通的就是它。

    一个都没配就 `SystemExit`（把 HOWTO 打出来）。都配了但一个都探不通时，
    **回第一个候选**：让后续的报错说「连不上 192.168.x.x」——用户在家遇到这个
    最可能，看到的也是他熟悉的那个地址。
    """
    forced = (os.environ.get("NAS_HOST") or "").strip()
    if forced:
        return forced
    candidates = _env_candidates()
    if not candidates:
        raise SystemExit("❌ 不知道要连哪台机器。\n" + HOWTO)
    for host in candidates:
        if probe(host, port):
            return host
    return candidates[0]


def describe(port: int = 22) -> str:
    """给日志用的一行说明：选了谁、为什么。"""
    forced = (os.environ.get("NAS_HOST") or "").strip()
    if forced:
        return f"{forced}（NAS_HOST 指定的）"
    candidates = _env_candidates()
    if not candidates:
        return "（没配 NAS_HOST / NAS_HOST_LAN / NAS_HOST_TS）"
    reachable = [h for h in candidates if probe(h, port)]
    if not reachable:
        return f"{candidates[0]}（候选都探不通，先按它试）"
    which = "局域网" if reachable[0] == (os.environ.get("NAS_HOST_LAN") or "").strip() else "Tailscale"
    return f"{reachable[0]}（{which}地址）"


def user() -> str:
    """SSH 用户名：`NAS_USER`，没配就报错（同样不留默认值）。"""
    name = (os.environ.get("NAS_USER") or "").strip()
    if not name:
        raise SystemExit("❌ 没有配 NAS_USER。\n    $env:NAS_USER='你的用户名'   /   export NAS_USER=你的用户名")
    return name


def key_path() -> str:
    """SSH 私钥：`NAS_KEY`，没配就按 `~/.ssh/id_ed25519` 找。"""
    raw = (os.environ.get("NAS_KEY") or "").strip() or "~/.ssh/id_ed25519"
    path = os.path.expanduser(raw)
    if not os.path.isfile(path):
        raise SystemExit(
            f"❌ 找不到私钥：{path}\n"
            "   配一把：$env:NAS_KEY='C:\\Users\\你\\.ssh\\id_ed25519'（或 ~/.ssh/id_rsa）"
        )
    return path


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(describe())
    for h in _env_candidates():
        print(f"  {'✅' if probe(h) else '❌'} {h}:22")
