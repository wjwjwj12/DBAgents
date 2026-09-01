# 可选组件装载清单

项目源码只保留 PPT Master 的核心脚本、工作流和轻量模板。图标、音效、
AI 风格对比图以及第三方转换工具由运行环境按需提供。

## 基础运行环境

Python 环境建议放在项目目录之外，或使用 Conda：

```bash
python -m venv /opt/venvs/ai-ppt
source /opt/venvs/ai-ppt/bin/activate
python -m pip install -r requirements.txt
```

Windows 示例：

```powershell
python -m venv D:\envs\ai-ppt
D:\envs\ai-ppt\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

前端依赖由 npm 锁文件管理，开发或构建前重新生成，不随源码包传输：

```bash
cd frontend
npm ci
npm run build
```

## PPT Master 资源包

需要图标检索、音效同步或 AI 风格预览时，安装与项目兼容的 PPT Master：

```bash
git clone --depth 1 https://github.com/hugohe3/ppt-master.git /opt/ppt-master
```

在项目 `.env` 中指向 Skill 目录：

```env
PPT_MASTER_HOME=/opt/ppt-master/skills/ppt-master
```

Windows 示例：

```env
PPT_MASTER_HOME=D:\tools\ppt-master\skills\ppt-master
```

该目录提供以下可选资源：

| 资源 | 外部相对路径 | 对应功能 |
|---|---|---|
| 图标库 | `templates/icons` | 图标搜索、同步和 SVG 嵌入 |
| 音效库 | `templates/sounds` | 转场和动画音效检索、同步 |
| AI 风格对比图 | `references/ai-image-comparison` | 确认页面中的风格预览 |

未配置 `PPT_MASTER_HOME` 时，基础 PPT 生成、模板填充和原生增强仍可使用，
但上述三个资源功能不可用。

## Python 可选依赖

完整安装 PPT Master 的 Python 扩展：

```bash
python -m pip install -r skills/ppt-master/requirements.txt
```

按功能补充：

```bash
python -m pip install playwright cairosvg lxml
python -m playwright install chromium
```

- `playwright` + Chromium：视觉检查、SVG 图片区域提取。
- `cairosvg`：部分 SVG 媒体回退转换。
- `lxml`：增强 PPTX 动画和切换 XML 处理。

## 系统工具

这些工具不应复制进项目，根据实际功能安装：

| 工具 | 用途 | 是否必需 |
|---|---|---|
| FFmpeg（含 `ffprobe`） | 音频时长、混音、字幕和视频处理 | 仅音视频功能 |
| Pandoc | `.doc`、`.odt`、LaTeX、RST 等小众格式转换 | 可选 |
| ImageMagick（`magick`） | 部分图片转 SVG 回退路径 | 可选 |
| Chromium | 高保真页面截图和视觉检查 | 可选 |
| Microsoft PowerPoint | Windows 下导出 PowerPoint 视频 | 可选且仅 Windows |
| Git | 更新或重新装载外部 PPT Master | 推荐 |

Linux 常用安装示例：

```bash
sudo apt install ffmpeg pandoc imagemagick
```

生产环境只安装业务实际使用的组件，不需要一次性安装全部可选项。
