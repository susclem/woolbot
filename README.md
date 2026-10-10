# 🐑 羊毛机器人

把「今天 10 点京东超市满 99 减 50 要领券」这种话（或者一张活动截图）发给微信机器人，
它自动理成结构化活动存进 SQLite；之后问它「今天抢什么」「周二 9 点半抢什么」，
它按你问的时间把对应活动发回来。另有一个零依赖的网页日历可以总览和手动改。

- **数据留在自己机器上**：全部数据只有一个 SQLite 文件，没有云、没有账号。
- **不需要公网 IP / 域名 / 备案**：微信那侧走个人号协议；也可以完全不用微信，只跑网页。
- **模型随便换**：任何 OpenAI 兼容接口（DeepSeek / OpenAI / 本地 Ollama…），改两个字段即可。
- **省调用**：固定的查询句式由 Python 直接查库，**一次模型都不调**；只有截图、大白话、
  非标准句式才交给模型。

---

## 一、它能做什么

**录入**
- 微信里发一句话，或直接发截图（调用视觉模型识别）→ 标题、平台、品类、开抢时刻、
  有效期、链接、备注。
- 信息不够时会反问（比如只说了「京东那个券」），你回一句「对」就入库。
- 网页日历也能手动新增/编辑。

**查询**
- 按分钟精确回答：「11:00 抢什么」只给那一刻开抢的，另补一节当天还没开始的。
- 「今天抢什么」「明天 8:30 抢什么」「本周六抢什么」「今天十五点抢什么」这类**固定句式
  不经过模型**，Python 直接查库，秒回。
- 问法里多一个字（例如加「京东」）就整句丢回模型解析 —— 宁可多花一次调用，也不猜。

**删除**
- 发「删+关键词」（如「删迪士尼」），机器人把匹配到的列出来，你回编号才删；
  回「都删」全删，回「取消」一条都不动。**模型没有删除能力**，只能出主意。

**网页日历**（`web/`，无框架、无 CDN、断网可用）
- 月视图：左边月历、右边当天的活动清单；格子里只有「几号」和「几条」，点标题就地展开详情。
- 列表视图：表格总览，支持搜索、按状态/平台筛选。
- 增删改、标记完成，白天/夜间两套配色（跟随系统，按钮可定死，记在 `localStorage`）。
- 格子里带农历、二十四节气、中国节日、法定节假日的「休/班」标记（优先级
  节气 > 节日 > 农历日）。
- 支持直接开某条详情的链接：`?id=25`、`?settings=1`、`?help=1`。

**到点提醒**
- 开抢前主动推一条微信消息，**不调模型**，纯查库拼字；同一条活动只提醒最近那个时刻。
- 提前量在设置页配（默认 10 分钟，下限 5 分钟、上限 1440）。

**重复活动与领券次数**
- 认「每天 / 每日 / 每周二、四 / 每月 1 号、15 号」这类排期，日历上按场次展开。
- 认「每周限领 1 次 / 每月最多 2 张」这类次数限制，清单里显示进度（如「本周 1/2」），
  领满之后其余场次不再催你。

**一言**
- 回复末尾附一句（文学/诗词/哲学/影视），可在设置页关掉，或指定分类。

**帮助**
- 微信里发「使用帮助」或 `/help` 直接回（不走模型）；网页右上角按钮弹同一段字。

**通道分工（帮助里那句大白话）**

> 标准句式或快捷指令 = 不调用AI；截图、大白话、非标句式 = 调用AI。

---

## 二、三十秒上手

```bash
git clone https://github.com/susclem/woolbot.git
cd woolbot
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.json config.json               # 再填一把 API key（或放 .env 里的 WOOLBOT_API_KEY）
python main.py check                             # 自检：时区、数据库、模型连不连得上
python tools/seed_demo.py                        # 塞几条演示数据，方便看前端
python main.py web                               # 打开提示的网址就能看到日历
```

- 需要一个 **OpenAI 兼容**接口：DeepSeek、OpenAI、本地 Ollama 都行，改 `config.json` 的
  `base_url` / `model` 即可。**不需要公网 IP、域名或备案。**
- 微信通道是**可选**的：`python main.py web` 只跑网页，`python main.py all` 才连微信（扫码登录）。
- 演示数据不想要了：`python tools/seed_demo.py --wipe`。
- 试抽取效果（真模型、只读不写库）：`python tools/check_extract.py`。
- 依赖只有四个：`requests`、`pycryptodome`、`tzdata`、`segno`。

---

## 三、目录一览

| 路径 | 干什么的 |
| --- | --- |
| `main.py` | 命令行入口：`web` / `chat` / `bot` / `all` / `check` / `backfill-repeat` |
| `app/` | 全部后端逻辑，每个文件开头有一句「这个文件负责什么」 |
| `web/` | 前端：`index.html` + `app.js` + `style.css`，无框架、无 CDN |
| `deploy/` | 部署脚本：SFTP 上传 + `docker compose` 拉起来，地址/用户名/私钥全走环境变量 |
| `tests/` | 八套自测（见「九、测试」），`python tests/test_core.py` 这样单跑 |
| `tools/` | 排查与演示的小工具：`smoke.py`（打接口）、`seed_demo.py`、`check_extract.py`… |
| `config.example.json` | 配置骨架，复制成 `config.json` 再填 |

`config.json`、`.env`、`data/`（数据库与登录状态）、日志、缓存都**不进仓库**，
见 `.gitignore`。

---

## 四、数据是怎么流的

```
微信消息 / 截图                      网页日历（web/）
      │                                    │
      ▼                                    ▼
app/wechat.py  收下              app/web.py  /api/*
      │                                    │
      └────────────► app/pipeline.py ◄─────┘    ← 「这句话是要干嘛」只在这一个地方判断
                          │
          ┌───────────────┼────────────────┐
          ▼               ▼                ▼
     app/llm.py      app/repeat.py     app/db.py
   抽取 / 截图识别   重复规则与次数     SQLite（唯一的数据源）
```

三层各管一件事，**模型不接触数据库**：`pipeline.py` 判断意图，`llm.py` 只负责把话/图
变成 JSON，`db.py` 只执行读写。

---

## 五、关键代码导读

每个文件开头都有一段「为什么这么写」的说明。要动代码时，下面这几处建议先看：

| 位置 | 看什么 |
| --- | --- |
| `main.py:64` `boot()` | 启动顺序：配置 → 日志 → 数据库 → 各组件；`main.py:454` 是子命令表 |
| `app/config.py:65` `_ENV_MAP` | 哪些配置能被环境变量覆盖（容器里靠它，不用改 `config.json`） |
| `app/db.py:22` `SCHEMA` | 全部建表语句；`app/db.py:126` `_ADDED_COLUMNS` 是给老库补列的增量迁移 |
| `app/pipeline.py:815` `handle_text()` | 微信和网页共用的唯一入口 |
| `app/pipeline.py:852` `_handle_text_inner()` | 意图判断：固定句式直接查库、其余才叫模型 |
| `app/pipeline.py:51` `HELP_TEXT` | 微信「使用帮助」和网页弹窗**同一段字**的唯一权威 |
| `app/llm.py:56` `UNDERSTAND_PROMPT` | 抽取活动的提示词；`app/llm.py:196` `IMAGE_PROMPT` 是截图那套 |
| `app/llm.py:373` `LLM` | HTTP 层：超时、重试、把模型报错翻成人话写进设置页 |
| `app/repeat.py:382` `parse_repeat()` | 「每天 / 每周二四 / 每月 1 号」→ 规则；`parse_quota()` 认次数限制 |
| `app/repeat.py:588` `quota_state()` | 「本周 1/2」这类配额进度的唯一算法（网页和微信都调它） |
| `app/reminder.py:103` `Reminder` | 开抢前主动提醒：不调模型，纯查库拼字 |
| `app/wechat.py:59` `CHANNEL_VERSION` | 协议版本号；收发与加解密（`decode_aes_key()` / `decrypt_media()`）都在这个文件 |
| `app/web.py:32` `COOKIE_NAME` | 页面鉴权与登录节流；前端在 `web/app.js`，按 `// ---` 分节 |

---

## 六、配置：config.json

骨架见 `config.example.json`（字段上方的 `//xxx` 是解释，程序会忽略）。

| 段落 | 关键字段 |
| --- | --- |
| 顶层 | `timezone`（默认 `Asia/Shanghai`）、`db_path`（默认 `data/woolbot.db`）、`provider`、`log.level` |
| `providers.<名字>` | `base_url`、`api_key`、`model`、`vision_model`（留空＝用 `model`）、`temperature`、`timeout` |
| `query` | `show_ongoing`（全天清单里要不要附「还能领的」）、`later_limit`（那一刻没命中时后面最多列几条） |
| `web` | `enabled`、`host`、`port`（默认 16886）、`token`（自定义口令，留空＝不要口令）、`public_host` / `public_port`（外面能访问到的地址，部署脚本自动写） |
| `wechat` | `enabled`、`state_file`、`media_dir`、`allowed_users`（白名单）、`session_hours`、`save_media`、`entry_base` / `cdn_base`（正常别动） |

- API key 也可以只放在设置页（存进数据库），或放 `.env` 的 `WOOLBOT_API_KEY`，
  三种来源都认；页面上的那个框永远是掩码显示，只改模型名不会把已存的 key 清掉。
- 环境变量覆盖清单在 `app/config.py::_ENV_MAP`（容器里用它，不必改 `config.json`）。

---

## 七、常用命令

| 命令 | 干什么 |
| --- | --- |
| `python main.py web` | 只起日历页面 |
| `python main.py chat` | 命令行对话（调提示词、试抽取最快的方式，不碰微信） |
| `python main.py bot` | 只起微信通道 |
| `python main.py all` | 微信通道 + 日历页面（部署时跑这个） |
| `python main.py check` | 自检配置、数据库、模型连通性 |
| `python main.py backfill-repeat` | 给**旧数据**补判重复规则与「每周/每月几次」；`--dry-run` 只看不改，`--all` 连已有排期的行一起重判 |

`backfill-repeat` 把 `raw_text`（截图 OCR 原文）和 `note` 拼起来送进 `app/repeat.py`、
**不调模型**。排期只在「还没判过规则」的行上写，配额是「补，但不动已经有的」——
所以它不会覆盖你在日历上手工填的规则或次数。

**微信里能用的指令**：`/今天` `/即将` `/统计` `/日历` `/help`。
其中 `/日历` 回的是按当前设置算出来的真实地址（域名、端口、口令都跟着走），
和启动日志里那行「日历页面已启动」同源。

---

## 八、网页日历与设置页

设置弹窗按 **功能 / 模型 / 安全** 分成三小页：

- **功能**：微信那一套（扫码、连接状态、退出微信）、允许机器人闲聊、开抢提醒开关与提前量、
  一言开关与「一言来源」（框下面那行字母就是可选分类，留空＝默认四类）。
- **模型**：一个模型框（同时写文字模型与截图模型两个键，想分开配就改 `config.json` 的
  `vision_model`）、API key、接口地址。
- **安全**：自定义口令（≥4 位，可整层关掉）、访问秘钥（≥8 位，需含大写、小写、数字、符号）、
  外网域名。

几个固定行为：

- **口令层**是给「谁拿到地址谁能看」的场合：没带 `?token=` 时它伪装成一个「请输入秘钥」的
  页面，不留明文、不种 cookie；带对了才认。连错 2 次，同一 IP 与设备封 24 小时。
- **访问秘钥层**是唯一的门闩（没有用户名）。登录一次管 7 天；改秘钥立刻作废所有旧 cookie。
- 模型与开关都是**存完立刻生效**，不用重启容器。

---

## 九、部署到自己的机器

**容器跑（推荐）**：

```bash
docker compose up -d --build
```

`docker-compose.yml` 里挂了两样东西：`./data`（数据库与微信登录状态）和 `./config.json`（只读）。
API key 建议走环境变量，同目录放一个 `.env` 写一行 `WOOLBOT_API_KEY=sk-xxx` 即可。
容器内监听的端口、compose 映射的端口、`config.json` 里写的 `web.port` **三处必须同号**
（默认 `16886`）；改了端口，机器上那层反向代理也要跟着改（它不会自己变）。

**远程一键部署**：`deploy/` 下几个只依赖 `paramiko` 的脚本，地址、用户名、私钥全走环境变量。

```bash
export NAS_HOST=192.168.1.10        # 家/外两个地址时用 NAS_HOST_LAN + NAS_HOST_TS
export NAS_USER=你的用户名
export NAS_KEY=~/.ssh/id_ed25519
python deploy/deploy_woolbot.py     # 上传 + 建/保留 config.json + build + up -d + 打印带口令的地址
```

| 脚本 | 干什么 |
| --- | --- |
| `deploy/deploy_woolbot.py` | 一键部署，最后打印能点进去的地址 |
| `deploy/nas_put.py` | 只上传（`--dry-run` 先看会传哪些，跳过 `data/`、`.env`、`config.json`、`*.db`） |
| `deploy/nas_key.py` | 在远端跑一条命令或脚本，排查用（`--cmd "docker logs --tail 50 woolbot"`） |
| `deploy/nas_host.py` | 地址探测：谁通就用谁 |

两件脚本替你处理掉的事：**`data/` 的属主**（compose 自动建的 bind mount 目录是 `root:root`，
容器里的非 root 用户写不进去，sqlite 会报 `unable to open database file`；脚本先建目录再把宿主
真实 `uid:gid` 写进 `.env`，交给 compose 的 `user:`）；以及**口令以页面里那份为准**（页面改过
口令之后，`config.json` 里那把就不再是真实的，部署脚本直接读容器 `settings` 表里那把）。

外网访问靠自己前面挂一层反向代理或隧道 —— 本程序不需要公网 IP、域名或备案。

---

## 十、测试

```bash
python tests/test_core.py      # 165 个用例：流水线 + 日期修正 + 重复规则与配额 + 查询 + 固定句式快路 + 帮助文案 + 删除 + 接口鉴权
python tests/test_web.py       # 125 个用例：页面真正在发的那些 HTTP 请求（含帮助、日历、设置三小页、口令与秘钥门、月视图版式、白天夜间）
python tests/test_wechat.py    #  85 个用例：协议层 + 长轮询 + 扫码/刷新/退出的状态机 + 回复末尾那句一言（起一台假 iLink 服务器）
python tests/test_settings.py  #  82 个用例：页面上配模型与各开关、口令与域名、访问秘钥校验、热生效、地址怎么算
python tests/test_llm.py       #  73 个用例：模型 HTTP 层 + 错误码翻译 + 截图入库 + 抽取检查工具
python tests/test_main.py      #  26 个用例：命令行入口与启动日志
python tests/test_reminder.py  #  23 个用例：开抢提醒（窗口、去重、领满不催、开关起停）
python tests/test_lunar.py     #  22 个用例：农历、节气、节日、法定节假日休/班
```

合计 **601** 个用例。都不需要 API key、也不出网（假服务器都跑在 `127.0.0.1`，
端口由系统分配，所以可以并行跑）。

这八套验的是**代码**（协议、边界、回归）；「真模型抽得准不准」它们管不了，
拿 `tools/check_extract.py` 对着真模型跑。

---

## 十一、常见问题

| 现象 | 原因 / 怎么办 |
| --- | --- |
| 设置里只有一个模型框 | 正常：它同时写文字模型和截图模型两个键。想分开配，改 `config.json` 的 `vision_model` |
| 「一言来源」框下面那行字母是空的 | 多半跑的是旧镜像；这行由服务端 `sayings_categories` 填 |
| 外网打开 502 / 打不开 | 反向代理的目标端口要跟 `config.json` 的 `web.port`（默认 16886）一致；改了端口，反代那边不会自己跟着变 |
| 微信显示已连接但收不到消息 | 个人号协议随时可能失效，重新登录一次；日志里的报错可以直接看 `docker logs` |
| 编辑时间会不会把填好的「每周几次」清掉 | 不会：只有解析结果认得出那个次数时才覆盖它（不认得的列一律不动） |
| 清单里看不到「本周 1/2」 | 那一行有排期、配额是空的（早先入库的数据）。跑 `python main.py backfill-repeat` 从备注原文里补，或直接在编辑框里填 |
| 领满之后那几天还是「待抢」 | 检查有没有配 `quota_week` / `quota_month`。领满只影响**本期**剩余几场（会一直显示「已完成」），下期第一天自动恢复「待抢」——这是故意的 |
| 想改微信里的帮助文案 | 唯一权威是 `app/pipeline.py::HELP_TEXT`，改完重启容器 |

---

## 说明与免责

> 微信那侧走的是**个人号协议**（ClawBot / iLink），**不是官方接口**，随时可能失效；
> 请只用在**自己的**账号上，风险自负。请遵守活动平台的规则，不要用于自动化刷单。
>
> 仓库里没有 `config.json`、`.env`、`data/`（数据库）、日志和缓存 —— 那些都是各人自己那份，
> 骨架看 `config.example.json`。代码注释与文档都是中文，记的是实际踩过的坑。
