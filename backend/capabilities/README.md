# Capabilities

此目录是平台内 Skill 与 Tool 的唯一管理入口。

```
capabilities/
├── skill_registry.py
└── tools/registry.py

项目根目录：

skills/<skill>/
├── manifest.json
├── SKILL.md
└── workflows, references, scripts, templates ...
```

- Skill 通过 `manifest.json` 声明名称、版本、入口和允许工具。
- 大型 Skill 使用 `load_skill_resource` 渐进加载说明类资源，不一次性注入全部上下文。
- Tool 的权限、审计、超时和执行入口由 `tools/registry.py` 统一管理。
- `backend/skills` 与 `backend/harness/tools.py` 仅保留兼容导入，新能力不再写入这两处。
- Skill 默认统一存放在项目根目录 `skills/`，可通过 `APP_SKILLS_DIR` 指向持久化目录。
- PPT 能力的唯一 Skill 入口是 `skills/ppt-master`；新建演示文稿由 Skill 在受管沙箱中执行，不保留旧 `generate_ppt` 平台工具。
