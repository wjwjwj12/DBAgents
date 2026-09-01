# OpenSandbox 预装镜像

在运行 OpenSandbox Docker daemon 的服务器上执行：

```bash
cd AI-PPT/deploy/opensandbox
docker build -t ai-ppt-sandbox:2026.09 .
docker run --rm ai-ppt-sandbox:2026.09 \
  python -c "import PIL, fitz, playwright, pptx, xlsxwriter"
```

随后在 AI-PPT 后端环境中配置：

```bash
OPENSANDBOX_IMAGE=ai-ppt-sandbox:2026.09
```

镜像必须构建在 OpenSandbox 实际使用的 Docker daemon 中。如果 OpenSandbox
服务运行在容器内并通过 `/var/run/docker.sock` 管理宿主机容器，则在该宿主机
构建即可。构建完成后只需重启 AI-PPT 后端使环境变量生效，不需要重启
OpenSandbox，也不会影响已经运行的其他 Docker 容器。

依赖只在 `docker build` 阶段下载。新建沙箱不会运行 `pip install`；首次执行
PPT Skill 时只进行一次无网络的导入校验，错误时会提示镜像未正确配置。
