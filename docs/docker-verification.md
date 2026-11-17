# Docker 本地验收记录

验收日期：2026-11-17。部署为 PostgreSQL 16、Java 21 控制面和两个 Python worker，均运行于 Docker Compose。所有任务使用 `mock`，供应商调用次数为 0。

## 实测结果

| 检查 | 结果 |
|---|---|
| 服务就绪 | 四个容器运行，PostgreSQL healthy，`/actuator/health` 返回 `UP` |
| 并行任务 | 8 个任务成功，72 个样本完成 |
| 数据校验 | 538 项断言通过，包括幂等重放与冲突、样本唯一性、调用计数、原始 prompt 与指纹 |
| 预算上限 | 额外创建一个 `maxCalls=1` 的任务，预留 1 次调用后按预期失败 |
| 两个 worker 实际参与 | 根据 `run_claimed` 容器日志核实，两个不同 worker 分别领取 1 个和 8 个任务 |
| 持久化 | 重启 PostgreSQL 与 Java 容器后，9 个任务的状态、完成数、调用数和成功/失败数保持一致 |

验收脚本为 [scripts/smoke_compose.py](../scripts/smoke_compose.py)。本次原始证据保存在工作区的 `.local/docker-compose-smoke.json`，包含任务 ID、计数、worker ID 和重启核验结果；`.local/` 不纳入 Git。脚本自身执行 HTTP 与数据断言，worker 数量和重启持久化由本次额外操作核实后记录。

这些结果证明任务执行链路在当前本地环境中工作。mock 不验证真实模型能力；本次执行耗时也不作为生产吞吐或压测数据。

## 本次实际构建入口

当前网络下 Docker daemon 拉取默认镜像超时，因此本次使用预先构建的 jar 和已载入的兼容运行时镜像，没有修改全局代理或镜像源。使用的替代构建入口如下：

```powershell
docker compose -f compose.yaml -f compose.prebuilt.yaml build --build-arg JAVA_RUNTIME_IMAGE=ghcr.io/graalvm/jdk-community:21 control-plane
docker compose -f compose.yaml -f compose.prebuilt.yaml build --build-arg PYTHON_IMAGE=ghcr.io/astral-sh/uv:python3.11-bookworm-slim --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple worker
docker compose -f compose.yaml -f compose.prebuilt.yaml up -d --no-build --scale worker=2
.\.venv\Scripts\python.exe scripts/smoke_compose.py --out .local/docker-compose-smoke.json
```

运行上述构建前，需要先打包 `control-plane/target/liars-eval-control-plane-0.1.0.jar` 并准备基础镜像。默认 Dockerfile 的 Maven 容器构建与默认基础镜像路径不属于本次成功验证的构建路径。

| 组件 | 本次使用的镜像 |
|---|---|
| Java | `ghcr.io/graalvm/jdk-community:21` |
| Python | `ghcr.io/astral-sh/uv:python3.11-bookworm-slim` |
| 数据库 | `postgres:16`，实测版本 16.14 |

健康检查地址为 `http://127.0.0.1:8080/actuator/health`。任务 API 的使用方式见 [控制面说明](../control-plane/README.md)，设计及当前边界见 [架构说明](architecture.md)。
