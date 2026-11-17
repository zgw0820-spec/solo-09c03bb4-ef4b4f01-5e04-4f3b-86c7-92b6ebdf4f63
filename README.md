# 固定内网区域主备 DNS（FastAPI + SQLite + dnslib）

为固定内网区域 `internal.local`（可用 `DNS_ZONE` 修改）提供主备两台权威 DNS：

- **主机 (primary)**：通过 REST API 接收一次提交、最多 8 条 A 记录的**完整区域**，
  可附带**一个子区委派**（子区名、位于该子区的 NS 名、该 NS 的 IPv4 glue）；
  仅在整份区域校验并落库成功后才把序号 (serial) 加 1。发布不依赖备用机。
- **备用机 (backup)**：从主机 HTTP 拉取**整份版本**（含委派），支持手动同步、
  后台自动同步和版本查询；两机各自使用独立 SQLite 数据库（不同文件/卷）。
- 两机都通过 **UDP 53 风格端口（默认 5053）** 回答区域内 A 查询；对委派子区名及
  其下所有名称返回**非权威转介**（authority 中的 NS、additional 中的 glue）。
  每个回答只取自**一份不可变的完整版本**快照，绝不混版。

## 设计要点与需求对应

| 需求 | 实现方式 |
| --- | --- |
| 单次提交 ≤8 条 A 记录的完整区域 | `app/models.py`：非空、≤8、仅 IPv4、名称必须在固定区域内、禁止重名 |
| 可选子区委派（子区名 + 子区内 NS + IPv4 glue） | `ZonePayload.delegation`（也接受 `subzone`/`delegation_name` 等平铺写法）；子区必须是父区的下级，NS 必须在子区内，glue 必须为 IPv4 |
| 父区 A 记录不得落在委派子区内 | `_validate_delegation()`：`name == child` 或 `name.endswith("." + child)` 一律 422 |
| 冲突提交不改变序号与生效区 | 校验在进入存储事务之前完成，422 时不调用 `next_publish()`；已有 e2e 断言 serial 与 active 委派均不变 |
| 发布成功才递增序号 | `Storage.next_publish()`：单事务 `BEGIN IMMEDIATE` 内插入新版本（A 记录 + 委派一起）并切换 active 指针 |
| 子区及下级查询返回非权威转介 | `ZoneResolver._referral()`：`RCODE=NOERROR`、`AA=0`、authority 内 NS、additional 内 A glue；A/AAAA/NS/AXFR 等任何类型都返回转介 |
| 不能用旧 A 或 NXDOMAIN 代答 | 解析时先判断 `qname == child or qname.endswith("." + child)`，命中即转介，不再查 A 表、不再返回 NXDOMAIN（即使历史父区 A 数据仍存在） |
| 父区其他查询照常 | 委派判断只作用于子区及其下级；父区 A 命中、NXDOMAIN、非 A NODATA、区外 REFUSED 行为不变 |
| 委派随区域一起同步 | 委派存于同一 `zone_versions` 行（`delegation_json`），`GET /api/zone` 随整份版本返回 |
| 传输失败仍用旧边界 | 同步失败/坏报文/冲突报文不触碰 active 版本，旧 A 记录与旧委派同时继续服务 |
| 新旧委派不得混入一次应答 | DNS 每次解析只读一次 active 版本行（`get_active_zone()`），记录与委派来自同一行；备用机在一个事务内同时写入新记录和新委派 |
| 追平后才切换 | 慢传在途期间仍按旧版回答，完整报文到达并提交后才切换（`tests/slow_transfer_test.py`、`tests/delegation_test.py`） |
| 迟到旧传输不得回退 | `commit_version()` 拒绝任何 `serial <= active_serial` 的版本（返回 `ignored_stale`/False），事务保护 |
| 原有 A 发布仍可用 | `delegation` 可省略；不带委派的发布与旧行为一致，且会用“无委派”覆盖旧委派（整区语义） |
| Docker Compose 双服务 + Swagger | `docker-compose.yml` 起 primary/backup；`http://<host>:8080/docs`、`http://<host>:8081/docs` |

### 数据模型（SQLite，WAL 模式）

```
zone_versions(serial INTEGER PRIMARY KEY, records_json TEXT,
              record_count INTEGER, published_at TEXT,
              delegation_json TEXT)   -- 不可变整份版本（A 记录 + 委派）
state(key TEXT PRIMARY KEY, value TEXT)
        active_serial      当前生效版本指针
        last_sync_*        备用机最近一次同步状态（at/status/error）
        upstream_serial    最近看到的上游序号
```

旧库启动时会自动 `ALTER TABLE` 补上 `delegation_json`（旧版本行为 NULL，即无委派）。
版本切换 = 插入新版本行 + 更新 `active_serial` 指针，在同一个事务里提交，
因此外部观察到的永远是“某一份完整版本”，不存在记录与委派来自不同版本的中间态。

## 端口与 API

| 角色 | API/Swagger | UDP DNS |
| --- | --- | --- |
| primary | 主机 8080 → 容器 8000 | 主机 5053 → 容器 5053/udp |
| backup  | 主机 8081 → 容器 8000 | 主机 5054 → 容器 5053/udp |

- `GET  /health` 健康检查
- `GET  /api/version` 角色、当前序号、记录数、当前委派名、备用机同步状态
- `GET  /api/zone` 当前完整区域（serial、records、delegation、published_at）
- `POST /api/zone` **仅主机**，成功返回 201 与新 serial 及生效委派
- `POST /api/sync` **仅备用机**，立即拉取一次，返回 `synced` / `ignored_stale` / `failed`

发布请求体（委派可省略）：

```json
{
  "records": [
    {"name": "db.internal.local", "ip": "10.0.0.11"},
    {"name": "web.internal.local", "ip": "10.0.0.21"}
  ],
  "delegation": {
    "name": "dev.internal.local",
    "ns": "ns1.dev.internal.local",
    "glue": "10.0.0.53"
  }
}
```

`delegation` 也可平铺为 `"delegation_name"/"subzone"`、`"ns"`、`"glue"`
（或 `nameserver`/`glue_ip` 等别名）；嵌套对象与平铺字段同时出现视为歧义，返回 422。

DNS 行为：委派子区名及其下任意名称 → **非权威转介**；父区 A 查询命中返回
NOERROR+A；父区不存在的名称 NXDOMAIN；父区非 A 查询返回 NOERROR NODATA；
区域外查询 REFUSED。委派只影响该子区，父区其余名称行为不变。

## 运行方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 主机 Swagger:  http://localhost:8080/docs
# 备用 Swagger:  http://localhost:8081/docs

curl -sX POST http://localhost:8080/api/zone -H 'content-type: application/json' \
  -d '{"records":[{"name":"db.internal.local","ip":"10.0.0.11"},
                  {"name":"web.internal.local","ip":"10.0.0.21"}],
       "delegation":{"name":"dev.internal.local",
                     "ns":"ns1.dev.internal.local","glue":"10.0.0.53"}}'

# 手动同步（后台也会每 3 秒自动同步）
curl -sX POST http://localhost:8081/api/sync

# UDP 查询
dig @127.0.0.1 -p 5053 db.internal.local A
dig @127.0.0.1 -p 5054 db.internal.local A   # 备用机
dig @127.0.0.1 -p 5053 www.dev.internal.local A  # 非权威转介（NS+glue）
```

### 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv --without-pip   # 或普通 venv
.venv/bin/pip install -r requirements.txt
DNS_ROLE=primary DB_PATH=./data/primary.db DNS_PORT=15053 API_PORT=18000 \
  .venv/bin/python -m uvicorn app.main:app --port 18000
DNS_ROLE=backup  DB_PATH=./data/backup.db  DNS_PORT=15054 API_PORT=18001 \
  PRIMARY_API_URL=http://127.0.0.1:18000 \
  .venv/bin/python -m uvicorn app.main:app --port 18001
```

## 测试

```bash
.venv/bin/python -m pytest tests/test_unit.py -q     # 校验/事务/防回退/委派单元测试
.venv/bin/python -I tests/e2e_test.py                # 双进程端到端 30 项
.venv/bin/python -I tests/auto_sync_test.py          # 自动同步/幂等/Swagger 8 项
.venv/bin/python -I tests/delegation_test.py         # 委派发布/转介/冲突/同步/切换 44 项
.venv/bin/python -I tests/slow_transfer_test.py      # 慢传期间旧版持续服务
```

端到端覆盖：启动空版本 NXDOMAIN、非法发布不升序号、发布后 UDP 应答、
手动与自动同步、黑洞上游期间旧版持续服务、恢复后自动追平、
迟到旧版本注入被拒且不回退、进程重启持久化与追赶、备用停机时主机照常发布；
委派测试另覆盖：子区及下级（A/AAAA/NS 及更深名称）返回非权威转介、
父区查询不受影响、冲突提交 422 且序号与生效委派不变、委派随版本同步、
旧子区在换委派后恢复为权威 NXDOMAIN、慢传在途保持旧边界、断流不产生半套委派。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DNS_ROLE` | `primary` | `primary` 或 `backup` |
| `DNS_ZONE` | `internal.local` | 固定内网区域名 |
| `DB_PATH` | `/data/dns.db` | 本机独立 SQLite 文件 |
| `DNS_PORT` / `API_PORT` | `5053` / `8000` | UDP DNS / HTTP 端口 |
| `PRIMARY_API_URL` | `http://primary:8000` | 备用机指向上机 API |
| `SYNC_INTERVAL` | `5` | 备用机后台同步间隔秒 |
| `HTTP_TIMEOUT` | `10` | 拉取区域 HTTP 超时秒 |
| `MAX_RECORDS` | `8` | 单次区域最大 A 记录数 |

## 已知边界

- 仅支持 A 记录与 IPv4（题目范围）；区域固定为一个，区域名通过配置设定。
- 每个版本最多携带**一个**子区委派（题目范围）；若需要多子区，需要扩展
  `delegation` 为数组并同步调整解析与校验。
- 转介中的 NS/glue 由父区数据直接给出（in-bailiwick，必需 glue）；不实现
  子区内部的权威解析，也不做递归。
- 传输为整份区域拉取（无增量/AXFR 分帧）；区域上限 8 条，单份报文很小。
- 单机内嵌一个 UDP 解析线程与一个 uvicorn 进程；未做水平扩展与 TSIG 鉴权，
  内网部署时应限制 `/api/zone` 的调用来源。
