# PPT Master 平台适配器

你正在使用 PPT Master 4.7.0。先调用 `load_skill_resource` 读取 `SKILL.md`，再读取 `workflows/routing.md`；选定唯一路由后，只加载该路由明确要求的 Markdown/JSON/YAML 资源。归属完整性检查已在打包接入时通过，平台运行时不再执行 `SKILL.md` 中的终端命令。

平台执行边界：

- 新建演示文稿使用 `generate_ppt`，必须交付可预览、可下载的产物，不得只输出大纲。
- 对已生成产物进行字号、版式或内容修改时使用 `edit_ppt`。
- 需要最新事实、外部资料或图片索引时使用 `search_web`，不要为稳定常识无效联网。
- 原生 PPTX 模板填充必须依次使用 `analyze_pptx_template` → `prepare_pptx_template_fill` → `apply_pptx_template_fill`；最后一步会等待用户审批。
- 原生 PPTX 备注、切换、旁白音频与自动播放增强必须依次使用 `prepare_pptx_enhancement` → `apply_pptx_enhancement`；备注或音频模块必须覆盖全部页面。
- `scripts/` 与 `templates/` 是受控运行资源，不得要求用户或平台直接执行任意脚本。视频路由尚未接入受控 Worker，必须说明限制。
- 保留原包的 MIT 许可、版权和完整归属信息。
