# deploy —— 把它装到一台机器上

这几个脚本干的事：**把仓库同步到远端目录，然后用 `docker compose` 把容器拉起来**。
它们只依赖 `paramiko`（`pip install paramiko`），不需要 rsync、ansible 之类。

```bash
# 1. 告诉它连哪台机器（一个地址就够）
export NAS_HOST=192.168.1.10
export NAS_USER=你的用户名
export NAS_KEY=~/.ssh/id_ed25519

# 2. 一键：传代码 + 建/保留 config.json + build + up -d
python deploy/deploy_woolbot.py
```

Windows PowerShell 里写 `$env:NAS_HOST='192.168.1.10'`，其余同理。

在家一个地址、在外面一个地址（Tailscale 之类的组网）时，可以给两个候选，它自己挑通的那个：

```bash
export NAS_HOST_LAN=192.168.1.10
export NAS_HOST_TS=100.101.102.103
```

| 脚本 | 干什么 |
| --- | --- |
| `deploy/deploy_woolbot.py` | 一键部署：上传 + 写 `config.json`（保住已有 token）+ `docker compose build && up -d` + 打印带口令的地址 |
| `deploy/nas_put.py` | 只上传（`--dry-run` 先看看会传哪些，跳过 `data/`、`.env`、`config.json`、`*.db`） |
| `deploy/nas_key.py` | 在远端跑一条命令或一个脚本，排查问题用（`--cmd "docker logs --tail 50 woolbot"`） |
| `deploy/nas_host.py` | 地址探测：谁通就用谁。单独跑可以看候选地址各自通不通 |

几个已经踩过的坑，脚本里都替你处理了：

- **`data/` 的属主**。`docker compose` 会自动创建缺失的 bind mount 源目录，但建出来是
  `root:root`，容器里的非 root 用户写不进去，sqlite 报 `unable to open database file`。
  脚本先建目录、再把宿主的真实 `uid:gid` 写进 `.env`，让 `docker-compose.yml` 的 `user:` 用它。
- **口令以页面里那份为准**。`config.json` 里那把只在「从没在页面上改过口令」时才对；
  部署完脚本直接读容器里 `settings` 表的 `web_token`，印出来的链接才是能点进去的。
- **端口三处同号**。容器里监听的、compose 映射的、`config.json` 里写的都是同一个数（16886），
  脚本在 build 之前先检查 compose 的映射对不对，不对就直接停下。

改完端口别忘了机器上那层反向代理（如果外网是反代进来的）—— 它的目标端口不会自己跟着变。
