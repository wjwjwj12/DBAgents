# Skills 管理目录

本目录是项目唯一的技能仓库。后端启动时会扫描每个一级子目录中的
`manifest.json`，模型根据清单描述选择并按需加载对应技能。

## 技能包格式

用户上传的技能包必须是 ZIP，内容可直接放在根目录，也可以包含一个顶层目录：

```text
example-skill/
├── manifest.json
├── SKILL.md
└── references/
    └── guide.md
```

`manifest.json` 示例：

```json
{
  "name": "example-skill",
  "version": "1.0.0",
  "description": "说明该技能适用的任务",
  "entrypoint": "SKILL.md",
  "aliases": ["example"],
  "tools": ["search_web"]
}
```

- `name` 和 `aliases` 只能包含小写字母、数字、短横线和下划线。
- `entrypoint` 必须是包内 Markdown 文件。
- `tools` 只能引用平台已经注册的工具；上传包不会执行安装脚本或任意代码。
- 技能名称、别名和目录不可覆盖已有技能。

可通过环境变量 `APP_SKILLS_DIR` 将整个技能仓库迁移到外部持久化目录。
