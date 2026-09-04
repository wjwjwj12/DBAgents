# DBAgent

DBAgent 是一个通用对话与任务执行智能体。普通问题直接回答；需要生成演示文稿、文档、标书或报告时，Agent 会在运行中按需加载对应 Skill 和工具，而不是由固定意图路由驱动。

## 安装

Windows 和 Linux 使用同一份源码、`requirements.txt` 和
`frontend/package-lock.json`。不要复制 Windows 的 `backend/venv` 或
`frontend/node_modules` 到 Linux，这两个目录包含平台相关的原生模块。

PPT 图标、音效和 AI 风格对比图不随源码分发。需要这些可选能力时，按照
[`OPTIONAL_COMPONENTS.md`](OPTIONAL_COMPONENTS.md) 安装外部资源和工具。

在目标系统使用 Conda 或项目目录外的 Python 虚拟环境安装后端依赖，并按锁文件
安装前端依赖：

```bash
python -m pip install -r requirements.txt
cd frontend
npm ci
cd ..
```

## 生产环境

本项目按 Web 服务器/API/多租户方式运行，不使用本地 Agent 的目录或终端权限。
生产环境设置 `APP_ENV=production`、`AUTH_MODE=trusted_headers`，并由可信认证网关
移除客户端同名请求头后，注入 `X-Tenant-ID`、`X-User-ID` 和 `X-Auth-Secret`。
`TRUSTED_PROXY_AUTH_SECRET`、模型密钥只配置在服务器密钥管理或进程环境中，
不写入源码、技能包或沙箱。`APP_SECRET_KEY` 为可选配置；未设置时平台会在
`APP_DATA_DIR/.app_secret` 自动生成并持久化下载签名密钥。

若部署在可信内网且不需要登录或多租户身份隔离，可显式设置
`AUTH_MODE=disabled`，此时无需配置 `TRUSTED_PROXY_AUTH_SECRET`，所有请求均使用
同一个本地管理员身份。该模式不可直接暴露到不可信网络。

默认 `SANDBOX_PROVIDER=disabled`，Agent 只有线程状态文件能力，没有本机 Shell。
需要代码和终端工具时，应配置受管沙箱提供方（当前支持 `langsmith`）；沙箱按
租户、用户和会话组成的线程标识隔离和复用，不会回退到宿主机执行。

上传附件会映射到线程虚拟目录 `/attachments/`。可插拔 Skill 可以读取完整解析
内容，但不能假设宿主机路径或本地命令一定存在；运行时会把不兼容的执行步骤映射
到当前已注册的平台工具。模型和工具调用保护上限分别由
`AGENT_MODEL_CALL_LIMIT`（默认 50）和 `AGENT_TOOL_CALL_LIMIT`（默认 200）控制。

首次部署或前端代码更新后，构建并启动：

```bash
python start.py --build
```

后续启动不再重复构建，直接运行：

```bash
python start.py
```

Linux 临时后台启动（仅用于短期验证）：

```bash
nohup python -u start.py > output.log 2>&1 &
```

生产环境使用 [`deploy/systemd/dbagent.service`](deploy/systemd/dbagent.service)。
systemd 会在启动器或子服务异常退出时自动拉起，并通过 cgroup 清理前后端完整
进程树，避免 `npm` 退出后 `next-server` 继续占用端口。模板不强制创建专用用户；
复制前应按服务器实际目录和 Python 环境修改 `WorkingDirectory` 与 `ExecStart`。
安装示例：

```bash
cp deploy/systemd/dbagent.service /etc/systemd/system/dbagent.service
systemctl daemon-reload
systemctl enable --now dbagent
```

`-u` 会关闭 Python 日志缓冲。不要在旧的 Next.js 服务仍运行时原地执行
`npm run build`，否则浏览器可能在更新期间请求到不匹配的 chunk。应先停止旧服务，
完成安装和构建后再启动；需要零停机时，应在新的发布目录构建后切换目录。

生产构建完成后，如果服务器不再需要 lint 和重新构建，可以缩减前端安装：

```bash
cd frontend
npm prune --omit=dev
cd ..
```

启动器会同时运行 FastAPI 后端和 Next.js 生产服务器。启动完成后访问：

- 网页入口：<http://127.0.0.1:6080>
- 接口文档：<http://127.0.0.1:14499/docs>

按 `Ctrl+C` 会同时停止前后端。任一服务意外退出时，日志会输出服务名称和
退出码，再停止另一个服务。

## 故障日志

- 后端运行、模型任务、工具重试和未处理请求异常会同时输出到控制台，并写入
  `data/logs/app.log`。
- 单个日志文件默认最大 10 MB，保留 5 个历史文件；可通过 `LOG_LEVEL`、
  `LOG_MAX_BYTES`、`LOG_BACKUP_COUNT` 调整。
- 使用 `nohup python -u start.py > output.log 2>&1 &` 时，启动器和 Next.js
  输出记录在项目根目录的 `output.log`，后端完整异常堆栈仍记录在上述 `app.log`。
- 页面任务失败时会显示运行编号，可在 `app.log` 和数据库运行记录中按该编号定位。

Linux 查询示例：

```bash
grep "运行编号或 run_id" data/logs/app.log
tail -f data/logs/app.log
```

旧的生产命令仍然兼容，等价于构建后启动：

```bash
python start.py --production
```

## 开发环境

只有本地开发需要热更新：

```bash
python start.py --dev
```

生产环境不要使用该参数；它会启用 `next dev`、Turbopack HMR 和
`uvicorn --reload`。

Windows 的 `start.bat` 使用当前已激活环境中的 `python`，跨平台流程统一使用
上述 `python start.py` 命令。

## 打包

只打包 Git 中的源码，不要打包 `backend/venv`、`frontend/node_modules`、
`frontend/.next`、`.env`、`data`、日志或已有压缩包：

```bash
git archive --format=zip --output=../AI-PPT-source.zip HEAD
```

压缩包输出到项目目录之外，避免把压缩包或项目副本再次打进自身。部署到目标
系统后，再按照“安装”一节生成该平台自己的运行环境。

## 目录约定

- `backend/`：后端源码。
- `frontend/`：前端源码。
- `skills/`：统一技能仓库，包含 PPT 模板和所有模型可按需加载的 Skill；技能包格式见 [`skills/README.md`](skills/README.md)。
- `tests/`：独立回归测试，不参与生产运行。
- `data/`：运行数据，包含业务数据库、LangGraph 检查点、上传文件和生成产物；该目录不会提交到代码仓库。

网页中的“技能广场”会展示当前仓库内容，并支持上传声明式 ZIP 技能包。上传成功后
注册表立即刷新，后续模型任务可以自动选择新技能。若生产部署使用只读发布目录，
请将 `APP_SKILLS_DIR` 指向持久化目录，并在首次部署时把项目自带的 `skills/`
内容复制到该目录。
