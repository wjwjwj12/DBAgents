# auto-agent

auto-agent 是一个通用对话与任务执行智能体。普通问题直接回答；需要生成演示文稿、文档、标书或报告时，Agent 会在运行中按需加载对应 Skill 和工具，而不是由固定意图路由驱动。

## 安装

Windows 和 Linux 使用同一份源码、`requirements.txt` 和
`frontend/package-lock.json`。不要复制 Windows 的 `backend/venv` 或
`frontend/node_modules` 到 Linux，这两个目录包含平台相关的原生模块。

在目标系统使用当前 Python 环境安装后端依赖，并按锁文件安装前端依赖：

```bash
python -m pip install -r requirements.txt
cd frontend
npm ci
cd ..
```

## 生产环境

首次部署或前端代码更新后，构建并启动：

```bash
python start.py --build
```

后续启动不再重复构建，直接运行：

```bash
python start.py
```

Linux 后台启动：

```bash
nohup python -u start.py > output.log 2>&1 &
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

- 网页入口：<http://127.0.0.1:6477>
- 接口文档：<http://127.0.0.1:14499/docs>

按 `Ctrl+C` 会同时停止前后端。任一服务意外退出时，日志会输出服务名称和
退出码，再停止另一个服务。

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

Windows 的 `start.bat` 只是现有虚拟环境的快捷入口，跨平台流程统一使用上述
`python start.py` 命令。

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
- `tests/`：独立回归测试，不参与生产运行。
- `data/`：运行数据，包含业务数据库、LangGraph 检查点、上传文件和生成产物；该目录不会提交到代码仓库。
