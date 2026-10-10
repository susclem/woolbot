"""通过 SSH 在远端机器上执行命令或脚本 —— 排查部署问题时最常用的那只手。

用法：

    python deploy/nas_key.py --cmd "docker ps"
    python deploy/nas_key.py --cmd "docker logs --tail 50 woolbot"
    python deploy/nas_key.py --script probe.sh          # 把本地脚本传上去跑一遍

认证：密钥，用户/地址/私钥路径都走环境变量（见 deploy/nas_host.py 的说明），
脚本里不留任何真实值：`NAS_HOST` / `NAS_HOST_LAN` / `NAS_HOST_TS` / `NAS_USER` / `NAS_KEY`。
"""
import argparse
import os
import sys

import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nas_host  # noqa: E402


def make_client(host, port, user, key):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, port=port, username=user, key_filename=key, timeout=20,
              look_for_keys=False, allow_agent=False)
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--user", default=None, help="默认取环境变量 NAS_USER")
    ap.add_argument("--key", default=None, help="默认取环境变量 NAS_KEY（或 ~/.ssh/id_ed25519）")
    ap.add_argument("--cmd", help="直接在远端跑这条命令")
    ap.add_argument("--script", help="本地脚本路径：传上去、跑完删掉")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()

    if not a.cmd and not a.script:
        ap.error("需要 --cmd 或 --script")

    a.host = a.host or nas_host.resolve()
    user = a.user or nas_host.user()
    key = a.key or nas_host.key_path()

    c = make_client(a.host, a.port, user, key)
    remote = None
    try:
        if a.script:
            remote = "/tmp/_nas_%d.sh" % os.getpid()
            sftp = c.open_sftp()
            sftp.put(a.script, remote)
            sftp.chmod(remote, 0o755)
            sftp.close()
            cmd = "bash " + remote
        else:
            cmd = a.cmd

        _stdin, stdout, stderr = c.exec_command(cmd, timeout=a.timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()

        blob = out
        if err:
            blob += "\n[stderr]\n" + err
        blob += "[exit code: %d]\n" % rc
        # 走 buffer 直接写 UTF-8：远端日志里有中文，Windows 控制台默认编码会把它弄成乱码。
        sys.stdout.buffer.write(blob.encode("utf-8", "replace"))
        sys.stdout.buffer.flush()
    finally:
        if remote:
            try:
                c.exec_command("rm -f " + remote)
            except Exception:
                pass
        c.close()


if __name__ == "__main__":
    main()
