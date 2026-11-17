# Java 任务控制面

Java 21 / Spring Boot 3.5，Spring JDBC + Flyway。Java 管理任务、租约、调用预算和逐 case checkpoint，Python worker 执行 probe-v2 探针。默认 mock 模式不调用模型；LLM 模式由 worker 读取模型 API 凭证，Java 不接收或保存该凭证。

## 运行

在仓库根目录执行：

```powershell
mvn -f control-plane/pom.xml verify
java -jar control-plane/target/liars-eval-control-plane-0.1.0.jar
```

默认 `127.0.0.1:8080`，H2 文件保存到**启动工作目录**的 `data/eval.mv.db`。重启时必须使用同一路径。默认文件 URL 设置 `WRITE_DELAY=0`，使提交同步写盘，避免进程强杀时丢失刚确认的checkpoint；自定义H2文件URL时也应保留此参数。健康检查为 `GET /actuator/health`，公开且不暴露数据库详情。所有 `/api/**` 请求必须提供 `X-API-Key`。

| 配置 | 默认值 | 用途 |
|---|---|---|
| `LIARS_CONTROL_API_KEY` | `local-dev-key-change-me` | 客户端和 worker 共用密钥 |
| `SERVER_ADDRESS` | `127.0.0.1` | 监听地址；容器内设置为 `0.0.0.0` |
| `SERVER_PORT` / `PORT` | `8080` | HTTP 端口 |
| `SPRING_DATASOURCE_URL` | H2 文件 JDBC URL | 显式固定存储位置 |
| `SPRING_DATASOURCE_USERNAME` | `sa` | 数据库账号 |
| `SPRING_DATASOURCE_PASSWORD` | 空 | 数据库密码 |
| `SPRING_PROFILES_ACTIVE` | 默认 profile | `postgres` 切换 PostgreSQL 默认驱动/URL |
| `EVAL_LEASE_SECONDS` / `--eval.lease-seconds` | `30` | 租约时长，1..3600秒 |

PostgreSQL 示例：

```powershell
$env:SPRING_PROFILES_ACTIVE = 'postgres'
$env:SPRING_DATASOURCE_URL = 'jdbc:postgresql://localhost:5432/liars_eval'
$env:SPRING_DATASOURCE_USERNAME = 'liars_eval'
$env:SPRING_DATASOURCE_PASSWORD = 'replace-me'
$env:LIARS_CONTROL_API_KEY = 'replace-this-development-key'
java -jar control-plane/target/liars-eval-control-plane-0.1.0.jar
```

Flyway 自动建表。默认 H2 文件适合本机单个 Java 进程；多个控制面实例应连接同一 PostgreSQL。API key 是开发环境的共享鉴权，没有用户、租户或角色体系。

## 请求与执行流程

```powershell
$headers = @{ 'X-API-Key' = 'local-dev-key-change-me'; 'Idempotency-Key' = 'my-mock-run-001' }
$run = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/api/runs -Headers $headers -ContentType application/json -Body '{"mode":"mock","boards":["QQJ_3"],"repeats":1}'
Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/runs/$($run.runId)/report" -Headers $headers
```

启动根目录的 Python worker 后，查询同一报告可以看到完成数、参考一致率、调用次数、延迟和 token。各接口及请求结构见 [OpenAPI](openapi.yaml)。任务状态为 `QUEUED → RUNNING → SUCCEEDED/FAILED`；排队中或运行中可取消为 `CANCELLED`。只有 `FAILED/CANCELLED` 且存在可继续的未完成 case 才能恢复为 `QUEUED`。

1. 创建时规范化所有缺省字段并计算配置 SHA-256；相同 `Idempotency-Key` + 相同规范化配置返回已有 run，配置不同返回 `409 IDEMPOTENCY_CONFLICT`。
2. worker 领取 run，获得随机 lease token。每个 run 同时只有一个持有者；多个 worker 可以领取不同 run。领取时返回整个 case 列表和 `completedCaseIds`，worker 跳过已 checkpoint 的 case。
3. 每次模型调用前必须预留一个 UUID attempt。事务锁住 run 后检查全局 `maxCalls` 与 case 的 `maxAttempts`；相同 UUID 重放不重复计次。
4. finish 成功则写入 case checkpoint。失败可重试时保留 case 为 PENDING；不可重试、case预算耗尽或全局预算耗尽则写失败 checkpoint。重复相同 finish 幂等，不重复累计 token；不同结果不能覆盖。
5. heartbeat、预留、finish、complete、fail 都检查当前 status/token/截止时间。租约到期后新 worker 可以重新领取；旧 token 和旧租约所属 attempt 无法继续 finish。
6. 取消立即使 token 失效。恢复保留所有 checkpoint、attempt 和已用预算；预算耗尽返回409，绝不偷偷清零预算。全部 checkpoint 完成后 worker complete；只要有失败 case，run 就是 FAILED。

## 数据与并发边界

`eval_run` 保存不可变配置、幂等键、全局调用预留数和租约；`eval_case` 保存确定性 case ID、seed、尝试数与最终 checkpoint；`eval_attempt` 保存每次预留及回传的结果、模型版本、prompt hash、延迟和 usage。

创建与领取通过数据库中的短 coordinator 行锁串行化，所有 worker 写入通过 run 行锁与事务串行化。模型调用发生在锁外。它适合本项目规模，不把此调度实现宣称为高吞吐队列；后续可在保留 token fencing 的前提下改为 PostgreSQL `SKIP LOCKED`。

这里保证的是**本地预算预留和结果落库不重复计数**。进程可能在预留之后、调用之前崩溃，也可能在模型已响应但 finish 尚未落库时崩溃。因而 STARTED attempt 仍然消耗预算，报告 `unfinishedAttempts` 保留这部分不确定性；它不保证外部模型请求 exactly-once。新的 lease 使用新 attempt，不能冒领旧 attempt 的响应。

报告的 `callCount` 是保守预留数；`totalTokens` 仅汇总已回传 usage；`p95LatencyMs` 采用已完成 attempt（包括重试错误）的 nearest-rank 95分位。参考一致率只对成功决策统计，表示**在 probe-v2 外生张数、诚实优先、单步效用假设下的参考一致率**，不等于真实牌局的最优策略准确率。无成功决策时比率为 null。mock 结果会显式标注 `mode=mock` 与 `mock=true`。

## 验证

`mvn -f control-plane/pom.xml verify` 运行17个集成测试，使用真实 H2、Flyway 迁移、Spring事务、MockMvc 与可控 Clock。覆盖并发领取/创建、预算竞争、租约失效与旧attempt fencing、取消恢复、重复回传不重复计数、重试错误、终态约束、鉴权及跨语言 case 协议。真实 Java 重启与 Python worker 联调由根目录 `scripts/smoke_platform.py` 验证。
