# DBAgent

DBAgent 是一个通用对话与任务执行智能体。普通问题直接回答；需要生成演示文稿、文档、标书或报告时，Agent 会在运行中按需加载对应 Skill 和工具，而不是由固定意图路由驱动。

## 统一启动

双击 `start.bat`，或在项目根目录运行：

```powershell
.\backend\venv\Scripts\python.exe start.py
```

启动器会同时运行 FastAPI 后端和 Next.js 前端。启动完成后访问：

- 网页入口：<http://127.0.0.1:6477>
- 接口文档：<http://127.0.0.1:14499/docs>

网页入口会自动跳转至前端。按 `Ctrl+C` 会同时停止前后端。

生产模式会先构建前端，再启动两个服务：

```powershell
.\backend\venv\Scripts\python.exe start.py --production
```

### Linux

服务器使用 Conda 管理 Python 环境时，先激活已经安装好项目依赖的环境：

```bash
conda activate <环境名>
pip install -r requirements.txt
```

开发模式：

```bash
python start.py
```

生产模式：

```bash
python start.py --production
```

也可以不激活环境，直接通过 Conda 启动：

```bash
conda run -n <环境名> python start.py --production
```

启动器会使用当前 Conda 环境中的 `python`，不再依赖项目目录下的
`backend/venv`。

## 目录约定

- `backend/`：后端源码。
- `frontend/`：前端源码。
- `tests/`：独立回归测试，不参与生产运行。
- `data/`：运行数据，包含业务数据库、LangGraph 检查点、上传文件和生成产物；该目录不会提交到代码仓库。
