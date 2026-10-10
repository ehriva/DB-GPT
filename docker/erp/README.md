# ERP Docker 启动

此部署从当前源码构建，保留 DeepSeek API 对话模型、本地 CPU Qwen 嵌入模型、现有前端静态资源和 ERP SQLite 数据。无需 GPU，也无需单独启动 MySQL 或 Next.js。

镜像通过 uv 导出锁定依赖，安装相同版本的 CPU PyTorch，再安装工作区源码并检查依赖完整性。本地 `uv.lock` 不变。

普通 Python 依赖从清华 PyPI 镜像下载，CPU PyTorch 从官方 PyTorch 源下载。

## 前提

- Docker Desktop 已启动，使用 Linux containers。
- 以下本地文件存在：`configs/dbgpt-proxy-deepseek.toml`、`pilot/meta_data/dbgpt.db`、`models/Qwen3-Embedding-0.6B/`、`examples/erp_query_trial/data/erp_trial.sqlite`。
- 元数据库已注册 SQLite 数据源 `erp_trial_readonly`。
- 原配置中的 API 地址、模型和凭据可用；本地模型权重完整。不要同时运行使用相同知识库及消息目录的本地服务。

## 启动

在仓库根目录运行：

```powershell
docker compose -f docker-compose.erp.yml up -d --build
docker compose -f docker-compose.erp.yml ps
```

访问 http://192.168.3.11:5670/ 。首次构建需联网下载镜像及依赖，模型加载也需要时间；等待服务显示 healthy。当前端口只绑定本机 WLAN 地址 `192.168.3.11`，本机也使用此地址访问。

同一办公 Wi-Fi 试用：在管理员 PowerShell 中运行 `& .\docker\erp\Enable-WifiAccess.ps1`，仅允许 WLAN 上的 `192.168.3.0/24` 访问 TCP 5670，并显式阻止该接口、地址和端口的其他 IPv4 来源，避免 Docker 既有宽泛允许规则扩大范围。脚本保留当前 Public 网络分类，不关闭防火墙；地址或网络分类变化时会拒绝执行，需要先核对配置。建议在路由器中为服务器保留当前地址。WLAN 地址或网段变化时通过参数覆盖：`& .\docker\erp\Enable-WifiAccess.ps1 -WlanIpAddress 192.168.3.11 -WlanSubnetCidr 192.168.3.0/24`，阻止范围由网段参数自动推导。

只修改端口映射后运行 `docker compose -f docker-compose.erp.yml up -d --no-build`，无需重新构建镜像。此试用没有新增账号和权限隔离，且使用 HTTP，仅供受信任人员共用；自然语言查询仍使用现有外部对话模型。其他电脑只需浏览器，查询的是服务器已挂载并注册的数据源。

前端必须使用同源 API：`web/next.config.js` 的默认 `API_BASE_URL` 为空。若已有静态包包含 `http://127.0.0.1:5670`，需重新构建并导出前端，再更新镜像；运行时改环境变量不会改变静态 JavaScript 中的地址。不要使用放宽 CORS 来解决客户端请求自身 localhost 的问题。

回退：将 Compose 发布地址改回 `127.0.0.1`，执行上述 `up -d --no-build`，然后在管理员 PowerShell 中执行 `Disable-NetFirewallRule -Name 'DBGPT-Docker-WLAN-5670','DBGPT-Docker-WLAN-5670-OutsideSubnet'`。不删除数据卷。

默认只读挂载现有 DeepSeek TOML 配置，保留模型名、API 地址、密钥设置和嵌入参数。配置不会进入镜像。可通过环境变量 `DEEPSEEK_API_KEY`、`DEEPSEEK_API_BASE` 覆盖对话凭据/地址。已有 TOML 中引用的其他环境变量需要在 Compose 的 environment 中显式传入。

可将 `docker/erp/.env.example` 复制为仓库根目录 `.env`，在本地填写覆盖值，勿提交 `.env`。设置 `DBGPT_ERP_PORT` 可更改宿主机端口，例如 5671，容器内仍为 5670。

模型列表同时提供 `LongCat-2.5-Preview`，通过 `proxy/openai` 连接
`https://api.longcat.chat/openai/v1`。在根目录 `.env` 中设置必需的
`LONGCAT_API_KEY`，密钥由 Compose 传入容器，TOML 仅引用环境变量。
DeepSeek 仍排在首位，本地 Qwen 嵌入模型保持原配置。在聊天页面的模型列表中
选择 LongCat 即可使用；使用它查询数据库时，相关提示词和查询上下文会发送至 LongCat。
只修改这些模型配置或 `.env` 后，执行
`docker compose -f docker-compose.erp.yml up -d --no-build` 使配置生效，无需重建镜像。

## 数据与停止

- 首次启动使用 SQLite backup 将原元数据库复制至 `erp-metadata` 卷。原元数据库保持不变。
- 同时同步原元数据库旁的 Alembic 脚本，使数据库版本与迁移历史匹配；容器生成的新迁移脚本保留在同一卷内。
- 副本中仅 `erp_trial_readonly` 的路径变为 `file:/erp/erp_trial.sqlite?mode=ro&uri=true`。ERP 目录在容器中也是只读。
- 重启复用副本，会话及后续配置不会被原数据库覆盖。以后本地元数据库新增的记录不会自动同步到 Docker 副本。
- `pilot/data` 和 `pilot/message` 直接挂载，容器会写入这些持久化目录；日志保存在 `erp-logs` 卷。
- 其他已有数据源/知识库记录若含 Windows 绝对路径，需要在容器中重新配置相应路径；此次只迁移明确使用的 ERP 数据源。

```powershell
docker compose -f docker-compose.erp.yml stop
docker compose -f docker-compose.erp.yml start
# 删除容器和网络，保留数据卷
docker compose -f docker-compose.erp.yml down
```

不要使用 `down -v`，它会删除 Docker 元数据库和日志卷。回到原启动方式时，原元数据库仍可使用，但 Docker 中新产生的会话不会自动合并。

修改 Python 源码后重新执行 `up -d --build`。前端使用仓库中的 `packages/dbgpt-app/src/dbgpt_app/static/web` 已打包资源；修改 `web/` 源码需要先按项目流程重新导出静态前端。

## 验证

```powershell
& .\.venv\Scripts\python.exe -B -m unittest discover -s docker/erp -p "test_*.py" -v
docker compose -f docker-compose.erp.yml config --quiet
```

前者检查原库保护、只修改 ERP 路径、重启保留数据、错误初始化不发布副本、只读 URI 及配置覆盖。后者只验证 Compose 结构，不证明容器启动成功。
