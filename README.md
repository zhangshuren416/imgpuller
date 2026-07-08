# imgpuller

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**imgpuller** 是一个不依赖 Docker/Podman CLI，直接通过 HTTP 从 OCI 兼容的 Registry 拉取镜像的工具。

它将镜像层下载到本地并保存为标准的 **OCI Layout** 格式，支持断点续传、并行下载、SHA256 完整性校验。

## 特性

- **纯 HTTP 下载**：直接通过 Docker Registry HTTP API V2 下载镜像层，无需 Docker/Podman 守护进程
- **断点续传**：下载中断后重新运行即可从断点恢复，状态持久化到 `.imgpuller-state/`
- **完整性校验**：流式 SHA256 计算，边下载边校验，确保每一层数据正确
- **并行下载**：多层并发下载，可配置并发数 (1-16)
- **多架构支持**：自动解析 manifest list，按平台选择匹配的镜像
- **标准 OCI Layout**：输出兼容 `skopeo`、`podman`、`docker` 的标准格式
- **认证支持**：Token 认证 (Bearer)、Basic 认证、Docker config.json 证书读取
- **代理支持**：支持 HTTP/HTTPS 代理，自动读取 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量

## 安装

```bash
# 从源码安装
git clone https://github.com/zhangshuren/imgpuller.git
cd imgpuller
pip install -e .
```

依赖：`aiohttp`、`aiofiles`、`click`、`rich`，Python 3.10+

安装后可使用 `imgpuller` 命令：

```bash
imgpuller --help
```

## 快速开始

### 拉取公开镜像

```bash
# 拉取 ubuntu:22.04 到当前目录
imgpuller pull ubuntu:22.04

# 指定输出目录
imgpuller pull -o ./my-images/ubuntu ubuntu:22.04

# 指定平台
imgpuller pull --platform linux/arm64 nginx:alpine
```

### 使用代理

```bash
# 显式指定代理
imgpuller pull --proxy http://127.0.0.1:7890 alpine:latest

# 或通过环境变量
export HTTPS_PROXY=http://127.0.0.1:7890
imgpuller pull alpine:latest
```

### 使用镜像站

```bash
# 通过阿里云镜像站拉取
imgpuller pull --registry registry.cn-hangzhou.aliyuncs.com google_containers/pause:3.9
```

### 私有仓库认证

```bash
# 交互式输入密码
imgpuller pull -u myuser --password-stdin ghcr.io/myorg/private-app:v1

# 自动读取 ~/.docker/config.json
docker login ghcr.io -u myuser
imgpuller pull ghcr.io/myorg/private-app:v1
```

### 导入到 Docker/Podman

```bash
# 拉取镜像后，使用 skopeo 导入到本地 Docker
imgpuller pull ubuntu:22.04 -o ./ubuntu-22.04
skopeo copy oci:./ubuntu-22.04 docker-daemon:ubuntu:22.04

# 使用 podman 加载
podman pull oci:./ubuntu-22.04
```

## CLI 命令

### `pull` — 拉取镜像

```
imgpuller pull [OPTIONS] IMAGE
```

| 选项 | 说明 |
|------|------|
| `IMAGE` | 镜像引用，如 `ubuntu:22.04`, `ghcr.io/org/app:v1` |
| `--platform` | 目标平台，格式 `os/arch[/variant]`，默认当前系统 |
| `-o, --output` | 输出目录，默认 `./<镜像名>-<tag>` |
| `--registry` | 显式指定 registry URL |
| `-u, --username` | Registry 用户名 |
| `--password-stdin` | 从 stdin 读取密码 |
| `-j, --jobs` | 并行下载数 (1-16, 默认 4) |
| `--proxy` | HTTP 代理 URL |
| `--insecure` | 允许 HTTP 连接 |
| `--no-verify` | 跳过 SHA256 校验 |
| `--no-resume` | 不恢复之前的下载进度 |
| `-v, -vv` | 详细/调试输出 |

支持的镜像引用格式：
- `ubuntu:22.04` — Docker Hub 官方镜像
- `nginx` — 等同于 `nginx:latest`
- `library/ubuntu:22.04` — Docker Hub 显式命名空间
- `docker.io/library/nginx:latest` — 完整 Docker Hub 引用
- `ghcr.io/org/app:v2` — GitHub Container Registry
- `registry.example.com:5000/myapp:v1` — 带端口自定义 registry
- `ubuntu@sha256:abc123...` — 按 digest 拉取

### `inspect` — 查看镜像信息

```bash
imgpuller inspect alpine:latest
# 输出: 媒体类型、平台、层数、每层大小、总大小
```

### `verify` — 校验 OCI Layout

```bash
imgpuller verify ./my-output-dir
# 输出: 验证所有 blob 的 SHA256 摘要，检查布局完整性
```

## 断点续传

下载过程中每 10MB 自动保存进度。如果进程被杀或网络中断：

```bash
# 第一次运行 — 下载到一半被中断
imgpuller pull ubuntu:22.04 -o ./output
# ^C

# 第二次运行 — 自动从中断处继续
imgpuller pull ubuntu:22.04 -o ./output
# 输出: Resuming sha256:xxx from byte 52428800
```

状态文件存储在 `<output>/.imgpuller-state/`，下载完成后自动清理。

## 输出格式

生成的 OCI Layout 目录结构：

```
<output>/
├── oci-layout          {"imageLayoutVersion": "1.0.0"}
├── index.json          指向 manifest 的索引
└── blobs/
    └── sha256/
        ├── <config-digest>         镜像配置 (JSON)
        ├── <manifest-digest>       Manifest (JSON)
        └── <layer-digest>          层文件 (tar.gz)
```

## 项目结构

```
imgpuller/
├── pyproject.toml
├── README.md
└── src/imgpuller/
    ├── cli.py               # Click CLI 入口
    ├── config.py             # 镜像引用解析、Docker config 读取
    ├── exceptions.py         # 异常体系
    ├── registry/
    │   ├── client.py         # HTTP 客户端 (认证流、Range 头)
    │   └── auth.py           # Bearer/Token/Basic 认证
    ├── manifest/
    │   └── resolver.py       # Manifest 解析、多架构选择
    ├── download/
    │   ├── manager.py        # 并行下载编排
    │   ├── worker.py         # 单 blob 下载 + 断点续传 + 校验
    │   └── state.py          # 断点续传状态持久化
    ├── verification/
    │   └── hasher.py         # 流式 SHA256 校验
    └── oci/
        └── layout.py         # OCI Layout 格式写入
```

## 异常退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 通用错误 |
| 2 | 配置/参数错误 |
| 3 | Registry 错误 (认证失败、API 不可用) |
| 4 | 下载错误 (校验失败、网络中断) |
| 5 | Manifest/平台未找到 |
| 6 | OCI Layout 写入错误 |

## License

MIT
