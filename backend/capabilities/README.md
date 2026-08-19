# Capabilities

此目录是平台内 Skill 与 Tool 的唯一管理入口。

```
capabilities/
├── skill_registry.py
├── skills/<skill>/
│   ├── manifest.json
│   ├── SKILL.md
│   └── workflows, references, scripts, templates ...
└── tools/registry.py
```

- Skill 通过 `manifest.json` 声明名称、版本、入口和允许工具。
- 大型 Skill 使用 `load_skill_resource` 渐进加载说明类资源，不一次性注入全部上下文。
- Tool 的权限、审计、超时和执行入口由 `tools/registry.py` 统一管理。
- `backend/skills` 与 `backend/harness/tools.py` 仅保留兼容导入，新能力不再写入这两处。
- PPT 能力的唯一 Skill 入口是 `skills/ppt-master`；后续模板填充、原生增强和视频等能力都在该 Skill 下扩展，不再引入其他 PPT Skill。
