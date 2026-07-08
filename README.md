# imgpuller

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**imgpuller** 是一个不依赖 Docker/Podman CLI，直接通过 HTTP 从 OCI 兼容的 Registry 拉取镜像的工具。

它将镜像层下载并在本地打包成单个 **docker-archive `.tar`** 文件，可直接用 `docker load -i` 导入。支持断点续传、并行下载、SHA256 完整性校验。

## 特性

- **纯 HTTP 下载**：直接通过 Docker Registry HTTP API V2 下载镜像层，无需 Docker/Podman 守护进程
- **单文件输出**：拉取后直接生成 `docker save` 兼容的 `.tar`，`docker load -i xxx.tar` 即可导入
- **断点续传**：下载中断后重新运行即可从断点恢复，状态持久化到 `<name>.blobs/.imgpuller-state/`
- **完整性校验**：流式 SHA256 计算，边下载边校验；打包时再次校验每层 diff ID
- **并行下载**：多层并发下载，可配置并发数 (1-16)
- **多架构支持**：自动解析 manifest list，按平台选择匹配的镜像
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
# 拉取 ubuntu:22.04，输出 ubuntu-22.04.tar
imgpuller pull ubuntu:22.04

# 指定输出 .tar 文件
imgpuller pull -o ./my-image.tar ubuntu:22.04

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
# 拉取镜像后，直接用 docker load 导入
imgpuller pull ubuntu:22.04
docker load -i ubuntu-22.04.tar

# 使用 podman 加载
podman load -i ubuntu-22.04.tar

# 校验 .tar 完整性
imgpuller verify ubuntu-22.04.tar
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
| `-o, --output` | 输出 `.tar` 文件路径，默认 `./<镜像名>-<tag>.tar` |
| `--registry` | 显式指定 registry URL |
| `-u, --username` | Registry 用户名 |
| `--password-stdin` | 从 stdin 读取密码 |
| `-j, --jobs` | 并行下载数 (1-16, 默认 4) |
| `--proxy` | HTTP 代理 URL |
| `--insecure` | 允许 HTTP 连接 |
| `--no-verify` | 跳过 SHA256 校验 |
| `--no-resume` | 不恢复之前的下载进度 |
| `--keep-blobs` | 生成 `.tar` 后保留中间 blob 下载目录 |
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

### `verify` — 校验 tar 镜像

```bash
imgpuller verify ./my-image.tar
# 输出: 校验 config、每层 layer.tar 的 SHA256 diff ID 及 chain ID 一致性
```

## 断点续传

下载过程中每 10MB 自动保存进度。如果进程被杀或网络中断：

```bash
# 第一次运行 — 下载到一半被中断
imgpuller pull ubuntu:22.04 -o ./ubuntu-22.04.tar
# ^C

# 第二次运行 — 自动从中断处继续（复用 ./ubuntu-22.04.blobs/ 里的分片）
imgpuller pull ubuntu:22.04 -o ./ubuntu-22.04.tar
# 输出: Resuming sha256:xxx from byte 52428800
```

中间分片与状态文件存储在 `<output>.blobs/.imgpuller-state/`，成功生成 `.tar` 后默认自动清理（可用 `--keep-blobs` 保留）。

## 输出格式

生成的 `docker save` 兼容 `.tar` 结构：

```
<output>.tar
├── manifest.json          [{"Config","RepoTags","Layers"}]
├── repositories           {"repo": {"tag": "<top-chain-id>"}}
├── <config-digest>.json   镜像配置 (JSON)
└── <chain-id>/
    ├── layer.tar          未压缩的层 (tar)
    ├── VERSION            "1.0"
    └── json               层元数据
```

其中 chain-id 由 config 中 `rootfs.diff_ids` 逐层计算（第一层为 diff id，之后为 `sha256(parent + " " + child)`）。

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
        ├── docker_save.py     # docker-archive .tar 生成与校验
        └── layout.py          # OCI Layout 格式写入（可选）
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
| 6 | tar 归档写入/校验错误 |

## License

MIT
