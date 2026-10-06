# 放疗质控平台（RTQC API）

记录疗程各照射通道的分次实测剂量。并发上报、网络重试与服务重启均不会造成重复累计或突破处方上限。

## 一致性设计

- 所有状态存于单个 SQLite 数据库（挂载在持久化卷上，WAL 模式）。
- 每个写请求都在进程级锁 + `BEGIN IMMEDIATE` 事务内串行执行，一次提交要么全部落库、要么零写入。
- `deliveryId` 唯一约束使重试安全：已接纳的提交按原样回放首次结果；同号异内容返回 409。
- `expectedRevision` 乐观并发：相互竞争的提交中恰好一个被接纳，每次成功接纳只递增一个修订号。
- 任一通道累计值超过处方即整次拒绝；全部通道恰好达到处方时疗程转为 `complete`，此后除原提交回放外一律 409。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `PUT` | `/api/courses/{courseId}` | 创建不可变疗程。Body: `{"channels": {"A": "3.0", ...}}`，1–16 个唯一通道，正规范十进制剂量。同内容重试返回 `200` 既有结果，异内容返回 `409` |
| `POST` | `/api/courses/{courseId}/deliveries` | 提交分次。Body: `{"deliveryId": "...", "expectedRevision": 0, "increments": {"A": "1.0"}}`。成功返回 `200` 及各通道累计量；异内容同号 / 过期修订 / 超限 / 疗程已完成均返回 `409` 且零写入 |
| `GET` | `/api/courses/{courseId}` | 查询疗程当前修订号、状态与各通道累计量 |

## 运行

```bash
# 启动 API（宿主机端口可用 HOST_PORT 覆盖，默认 8000）
HOST_PORT=8080 docker compose up -d app

# 一次性验证：单元测试 + 构建确认 + API 冒烟
# （创建疗程、并发提交竞争分次、重启 app 容器验证持久化，以退出码汇总）
docker compose run --rm verify; echo "exit=$?"
```

`verify` 服务通过挂载的 `/var/run/docker.sock` 重启 `app` 容器，确认重启后疗程状态与提交回放保持不变。

## 本地开发（无 Docker）

```bash
python3 -m unittest discover -s tests -v          # 单元测试
PORT=8000 DB_PATH=/tmp/app.db python3 -m app.server  # 启动 API
```

## 项目结构

```
app/server.py      # HTTP 层 + 事务化存储（CourseStore），仅依赖标准库
tests/test_server.py
verify/verify.py   # 一次性验证服务
Dockerfile         # 单镜像：API 与 verify 共用
docker-compose.yml # app（健康检查 + 持久化卷 + 可配置端口）与 verify
```
