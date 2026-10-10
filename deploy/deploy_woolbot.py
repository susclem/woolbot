"""把 woolbot 部署到一台能 SSH 的机器（NAS / 小主机 / 树莓派都行）并拉起来。

    python deploy/deploy_woolbot.py                 # 传代码 + 建/保留 config.json + build + up -d
    python deploy/deploy_woolbot.py --no-build      # 只传代码（改完前端不想重建镜像时用）
    python deploy/deploy_woolbot.py --restart-only  # 不传代码，只重启容器

地址、用户名、私钥一律走环境变量，脚本里不留任何真实值：

    $env:NAS_HOST='192.168.1.10'    # 或者给 NAS_HOST_LAN / NAS_HOST_TS 两个候选，自己挑通的
    $env:NAS_USER='你的用户名'
    $env:NAS_KEY='C:\\Users\\你\\.ssh\\id_ed25519'

设计取舍：web token 第一次生成后会写进远端 config.json 并在后续部署里复用，
否则每次部署都换 token，手机上的书签就全失效了。

API key 不写进 config.json —— 走远端 .env 文件（docker compose 会自动读），
也可以在日历页的「设置 → 模型」里填，页面里填的优先级更高、存了立刻生效。

端口：容器里监听 `PORT`（16886），对外映射同一个号；`app/config.py` 的
`DEFAULT_WEB_PORT`、`Dockerfile`、`docker-compose.yml` 三处必须一致。改端口还要去
机器上改那层反向代理（如果外网是反代进来的），否则外网域名会变成 502 Bad Gateway。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nas_host  # noqa: E402
import nas_put  # noqa: E402
import paramiko  # noqa: E402

#: 仓库根目录 = 本脚本的上一级（deploy/ 在仓库里）
REPO = Path(__file__).resolve().parent.parent

#: 容器里监听的端口 —— 和 `app/config.py::DEFAULT_WEB_PORT`、`Dockerfile`、
#: `docker-compose.yml` 说的是同一个号。改它必须同时改那三个文件。
PORT = 16886


def run(c, cmd: str, timeout: int = 1800) -> tuple[int, str]:
    """在远端跑一条命令，返回 (exit code, stdout+stderr)。"""
    _stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    return rc, (out + (("\n[stderr]\n" + err) if err.strip() else ""))


def read_text(sftp, path: str) -> str | None:
    """读一个远端文本文件；不存在返回 None（首次部署就是这种）。"""
    try:
        with sftp.open(path, "r") as fh:
            return fh.read().decode("utf-8")
    except IOError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=str(REPO), help="要上传的本地目录（默认仓库根）")
    ap.add_argument("--dst", default=None, help="远端目录（默认 /home/<用户>/woolbot）")
    ap.add_argument("--no-build", action="store_true", help="跳过 docker build")
    ap.add_argument("--skip-upload", action="store_true")
    ap.add_argument("--restart-only", action="store_true")
    ap.add_argument("--local-port", type=int, default=PORT,
                    help=f"宿主机上对外映射的端口（默认 {PORT}，要和 docker-compose.yml 的映射对得上）")
    a = ap.parse_args()

    host = nas_host.resolve()
    user = nas_host.user()
    key = nas_host.key_path()
    if not a.dst:
        a.dst = f"/home/{user}/woolbot"

    # 三处端口必须对得上：容器里监听 PORT、compose 映射成 {local_port}:{PORT}、
    # 写进 config.json 的 public_port 是 local_port。对不上就是「页面打不开」或者
    # 「健康检查永远 unhealthy」，在这儿直接拦住，别等部署完才发现。
    compose = Path(a.src, "docker-compose.yml")
    want = f"{a.local_port}:{PORT}"
    if compose.is_file() and want not in compose.read_text(encoding="utf-8"):
        print(f"❌ {compose} 里没有「{want}」这个端口映射。")
        print(f"   容器监听 {PORT}，对外映射 {a.local_port}。要么改 compose，要么改 --local-port。")
        return 2

    print(f"→ 目标：{user}@{host}:{a.dst}")
    client = nas_put.make_client(host, 22, user, key)
    try:
        sftp = client.open_sftp()
        nas_put.mkdirs(sftp, a.dst)

        if not (a.restart_only or a.skip_upload):
            planned = []
            for root, dirs, files in os.walk(a.src):
                dirs[:] = [d for d in dirs
                           if not nas_put.should_skip(os.path.relpath(os.path.join(root, d), a.src))]
                for name in files:
                    local = os.path.join(root, name)
                    rel = os.path.relpath(local, a.src).replace("\\", "/")
                    if not nas_put.should_skip(rel):
                        planned.append((local, rel))
            print(f"→ 上传 {len(planned)} 个文件到 {a.dst}")
            made: set[str] = set()
            for local, rel in planned:
                remote = nas_put.posixpath.join(a.dst, rel)
                parent = nas_put.posixpath.dirname(remote)
                if parent not in made:
                    nas_put.mkdirs(sftp, parent)
                    made.add(parent)
                sftp.put(local, remote)

        # config.json：远端存在就沿用（保住 token），不存在就按示例生成一份
        cfg_path = nas_put.posixpath.join(a.dst, "config.json")
        example = json.loads(Path(a.src, "config.example.json").read_text(encoding="utf-8"))
        raw = read_text(sftp, cfg_path)
        if raw:
            try:
                cfg = json.loads(raw)
                print(f"→ 复用远端已有的 config.json（token {cfg.get('web', {}).get('token', '')[:6]}…）")
            except json.JSONDecodeError:
                cfg = example
                print("→ 远端的 config.json 不是合法 JSON，已用示例覆盖")
        else:
            cfg = example
            print("→ 首次部署，生成 config.json")

        token = (cfg.get("web") or {}).get("token") or secrets.token_urlsafe(12)
        cfg.setdefault("web", {})["token"] = token
        cfg["web"]["host"] = "0.0.0.0"  # 容器里必须监听 0.0.0.0，否则端口映射进不来
        cfg["web"]["port"] = PORT       # 容器内监听的端口，与 compose 映射右侧同号
        # 容器自己算不出宿主机的地址（它看到的是 docker 网桥的 172.x），所以在部署时
        # 把真实地址和映射端口写进去 —— `app/settings.front_url()` 优先用这两个，
        # 启动日志、口令页、微信里的 `/日历` 指令三条路都靠它，口径才会一致。
        cfg["web"]["public_host"] = host
        cfg["web"]["public_port"] = a.local_port
        cfg.setdefault("wechat", {})["state_file"] = "data/weixin_state.json"
        cfg["db_path"] = cfg.get("db_path") or "data/woolbot.db"

        with sftp.open(cfg_path, "w") as fh:
            fh.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        sftp.close()

        # data/ 必须在 docker 之前建好，而且要属于当前用户。
        # docker compose 会自动创建缺失的 bind mount 源目录，但建出来是 root:root，
        # 容器里的非 root 用户写不进去，sqlite 会报 "unable to open database file"。
        # 组也要注意：宿主的 gid 常见是 1001，而镜像里是 1000 —— 所以真实 uid/gid
        # 会写进 .env，由 docker-compose.yml 的 user: 使用。
        _rc, out = run(client, f"cd {a.dst} && "
                               "if [ -d data ] && [ ! -w data ]; then "
                               "  rmdir data 2>/dev/null && echo 'removed root-owned empty data/' "
                               "    || echo 'WARN: data/ 不是空的且不可写，需要 sudo 处理'; "
                               "fi; mkdir -p data data/media && chmod 775 data && ls -ld data")
        print(f"→ 数据目录：{out.strip()}")

        # 真实 uid/gid 落进 .env（幂等：只在缺失时追加，不会碰已有的 API key）
        _rc, out = run(client, f"cd {a.dst} && touch .env && "
                               "grep -q '^WOOLBOT_UID=' .env || echo \"WOOLBOT_UID=$(id -u)\" >> .env; "
                               "grep -q '^WOOLBOT_GID=' .env || echo \"WOOLBOT_GID=$(id -g)\" >> .env; "
                               "grep '^WOOLBOT_U' .env")
        print(f"→ 容器以 {out.strip().replace(chr(10), ' ')} 运行")

        # .env 里有没有 API key（没有也不算错：页面上填过就行）
        _rc, out = run(client, f"cd {a.dst} && test -s .env && grep -c WOOLBOT_API_KEY .env || echo 0")
        has_key = out.strip().splitlines()[-1] not in ("", "0")
        if not has_key:
            print("ℹ️  远端的 .env 里没有 API key。")
            print("    如果已经在日历页「设置 → 模型」里填过，忽略这条 —— 页面上的设置优先级更高，")
            print("    而且存了立刻生效，不用重启。想改用 .env 的话：")
            print(f"      echo 'WOOLBOT_API_KEY=sk-xxx' >> {a.dst}/.env  &&  cd {a.dst} && docker compose up -d")

        if not a.no_build and not a.restart_only:
            print("→ docker compose build（第一次要拉 python:3.12-slim，慢）")
            rc, out = run(client, f"cd {a.dst} && docker compose build 2>&1 | tail -25")
            print(out.strip())
            if rc != 0:
                print(f"❌ build 失败（exit {rc}）")
                return rc

        print("→ docker compose up -d")
        rc, out = run(client, f"cd {a.dst} && docker compose up -d 2>&1 | tail -15")
        print(out.strip())
        if rc != 0:
            print(f"❌ 启动失败（exit {rc}）")
            return rc

        _rc, out = run(client, "sleep 6; docker ps --filter name=woolbot --format '{{.Status}}|{{.Ports}}'")
        print(f"→ 容器状态：{out.strip()}")
        _rc, out = run(client, "docker logs --tail 25 woolbot 2>&1")
        print("—— 容器日志 ——")
        print(out.strip())

        # 口令以**页面里那份**为准。config.json 里那把只在「从没在页面上改过口令」时才是
        # 对的；改过之后 config.json 里留着的是部署当时生成的旧值，照它印出来的链接谁点
        # 谁进不去。所以直接读 settings 表：`docker exec` 起来的是个新进程，
        # `st.web_token()` 在没有 `db.init()` 时会**静默按空处理**（连 config.json 里那把
        # 都拿不到），所以别偷懒去调它。
        live = ""
        probe = ("import sqlite3; c=sqlite3.connect('/app/data/woolbot.db'); "
                 "r=c.execute('select value from settings where key=?', ('web_token',)).fetchone(); "
                 "print((r[0] if r else '') or '')")
        _rc, out = run(client, f"docker exec woolbot python -c \"{probe}\" 2>/dev/null || true")
        if out.strip():
            live = out.strip().splitlines()[-1].strip()
        shown = live or token
        note = "" if (not live or live == token) else "  ← config.json 里那把（%s…）已经作废" % token[:6]
        print(f"\n🗓  日历页面：http://{host}:{a.local_port}/?token={shown}{note}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
