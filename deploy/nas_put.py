"""把本地目录整棵传到远端（SFTP 递归上传）。

    python deploy/nas_put.py --src . --dst /home/你/woolbot
    python deploy/nas_put.py --src . --dst /home/你/woolbot --dry-run   # 只列出会传什么

默认跳过运行数据、登录态、缓存和调试产物 —— 部署时要的是「代码」，
不是本机那份数据库（远端会因为 config.json 里的 db_path 重新建一份）。

地址/用户/密钥都走环境变量（见 deploy/nas_host.py 的说明），脚本里不留任何真实值：

    NAS_HOST_LAN / NAS_HOST_TS / NAS_HOST    # 连哪台机器
    NAS_USER                                 # SSH 用户名
    NAS_KEY                                  # 私钥路径（默认 ~/.ssh/id_ed25519）
"""

from __future__ import annotations

import argparse
import os
import posixpath
import sys

import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nas_host  # noqa: E402

#: 相对路径里出现这些片段就跳过（目录名或文件名都算）
SKIP_DIRS = {"__pycache__", ".git", "data", ".preview", ".chrome", ".chrome2",
             ".tmp", "node_modules", ".venv", "docs"}
SKIP_FILES = {".env", "config.json"}
SKIP_SUFFIX = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log")
SKIP_PREFIX = ("out_", "out.", "preview.")


def should_skip(rel: str) -> bool:
    """这个相对路径要不要跳过（与仓库里的 .gitignore 一个口径）。"""
    parts = rel.replace("\\", "/").split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if parts[0] in SKIP_DIRS or name in SKIP_FILES:
        return True
    if name in SKIP_DIRS:
        return True
    if name.endswith(SKIP_SUFFIX) or name.startswith(SKIP_PREFIX):
        return True
    return False


def make_client(host: str, port: int, user: str, key: str) -> paramiko.SSHClient:
    """建一条 SSH 连接。密钥认证，不用 agent、不试别的 key（免得挑错一把）。"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, port=port, username=user, key_filename=key, timeout=20,
              look_for_keys=False, allow_agent=False)
    return c


def mkdirs(sftp: paramiko.SFTPClient, path: str) -> None:
    """像 mkdir -p 一样逐级建目录。"""
    parts, cur = path.strip("/").split("/"), "/"
    for part in parts:
        cur = posixpath.join(cur, part)
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="本地目录")
    ap.add_argument("--dst", required=True, help="远端目录（不存在会创建）")
    ap.add_argument("--host", default=None,
                    help="不给就交给 nas_host 挑：先探 NAS_HOST_LAN，不通再探 NAS_HOST_TS")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--user", default=None, help="默认取环境变量 NAS_USER")
    ap.add_argument("--key", default=None, help="默认取环境变量 NAS_KEY（或 ~/.ssh/id_ed25519）")
    ap.add_argument("--dry-run", action="store_true", help="只列出会传什么")
    a = ap.parse_args()

    a.host = a.host or nas_host.resolve()
    user = a.user or nas_host.user()
    key = a.key or nas_host.key_path()

    src = os.path.abspath(a.src)
    if not os.path.isdir(src):
        ap.error(f"不是目录：{src}")

    planned: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not should_skip(os.path.relpath(os.path.join(root, d), src))]
        for name in files:
            local = os.path.join(root, name)
            rel = os.path.relpath(local, src).replace("\\", "/")
            if should_skip(rel):
                continue
            planned.append((local, rel))

    total = sum(os.path.getsize(p) for p, _ in planned)
    print(f"待传 {len(planned)} 个文件，共 {total / 1024:.1f} KB → {user}@{a.host}:{a.dst}")
    if a.dry_run:
        for _, rel in sorted(planned):
            print("  ", rel)
        return 0

    c = make_client(a.host, a.port, user, key)
    try:
        sftp = c.open_sftp()
        mkdirs(sftp, a.dst)
        made: set[str] = set()
        for local, rel in planned:
            remote = posixpath.join(a.dst, rel)
            parent = posixpath.dirname(remote)
            if parent not in made:
                mkdirs(sftp, parent)
                made.add(parent)
            sftp.put(local, remote)
        sftp.close()
    finally:
        c.close()
    print(f"✅ 上传完成：{a.dst}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
