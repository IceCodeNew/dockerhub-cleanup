# Docker Hub Cleanup

安全预览并清理 Docker Hub 仓库中的长期未拉取 tag 和无 tag manifest。工具默认只输出删除计划；真正删除必须同时提供 `--apply` 和精确匹配 namespace 的 `--confirm`。

## 安装

需要 Python 3.11 或更高版本、[uv](https://docs.astral.sh/uv/) 和 [mise](https://mise.jdx.dev/)。使用 mise 安装 `crane`，再从 GitHub 安装命令行工具：

```bash
mise use --global crane@0.21.7
uv tool install git+https://github.com/IceCodeNew/dockerhub-cleanup.git
```

验证安装：

```bash
dockerhub-cleanup --help
crane version
```

## 认证

创建具有 Read、Write、Delete 权限的 Docker Hub Personal Access Token。`DH_USERNAME` 省略时默认使用 `--namespace` 的值。交互式运行可以让工具安全提示输入 PAT；非交互式运行则需要设置 `DH_PAT`。

下面的 Bash 示例将凭据读入环境变量，不会把它写入 shell 历史：

```bash
export DH_USERNAME='your-docker-id'
IFS= read -r -s -p 'Docker Hub PAT: ' DH_PAT
printf '\n'
export DH_PAT
```

只有使用 `--untagged` 时才需要 `DH_COOKIE`。登录 `hub.docker.com`，在浏览器开发者工具的 Network 面板中，从任一 `hub.docker.com` 请求复制完整的 `Cookie` 请求头值，然后临时设置：

```bash
IFS= read -r -s -p 'Docker Hub Cookie: ' DH_COOKIE
printf '\n'
export DH_COOKIE
```

浏览器 Cookie 会过期。发现无 tag manifest 依赖 Docker Hub 未公开的 Image Management 网页接口；接口或认证方式变化时，工具会停止执行，不会根据不完整清单删除镜像。

## 预览长期未拉取的 tag

预览单个仓库中最后拉取时间早于 180 天的 tag，并保护 `latest` 和所有生产 tag：

```bash
dockerhub-cleanup \
  --namespace your-docker-id \
  --repository app \
  --before 180d \
  --keep-tag latest \
  --keep-tag 'prod-*'
```

`--repository` 可以重复使用；省略时处理 namespace 下全部可见仓库。`--before` 支持 `24h`、`180d`、`8w` 等相对时长，也支持带时区的 ISO 8601 时间：

```bash
dockerhub-cleanup \
  --namespace your-docker-id \
  --before 2026-01-01T00:00:00Z
```

最后拉取时间为空的 tag 默认保留。若要把“从未拉取且 push 时间也早于截止时间”的 tag 纳入候选：

```bash
dockerhub-cleanup \
  --namespace your-docker-id \
  --before 180d \
  --include-never-pulled
```

## 预览无 tag manifest

预览 namespace 下全部仓库中不再由任何 tag 引用的 manifest：

```bash
dockerhub-cleanup \
  --namespace your-docker-id \
  --untagged
```

`crane ls` 只能列出 tag，不能枚举无 tag manifest。本工具使用浏览器 Cookie 发现完整 digest 清单，但删除仍通过 PAT 和隔离的临时 `DOCKER_CONFIG` 完成。Cookie 不会传给 `crane`。

## 执行删除

先检查完整的 dry-run 输出，再增加 `--apply` 和与 `--namespace` 完全一致的 `--confirm`：

```bash
dockerhub-cleanup \
  --namespace your-docker-id \
  --before 180d \
  --untagged \
  --keep-tag latest \
  --apply \
  --confirm your-docker-id
```

长期未拉取的 tag 通过 Docker Hub tag API 删除，不会直接删除可能被其他 tag 共享的 manifest。无 tag digest 通过 `crane delete` 删除；仍被 image index、tag 或其他对象引用时，Docker Hub 会拒绝操作。

一个候选项失败不会阻止后续候选项。只要存在失败，命令最终返回非零状态。
每个删除成功或最终失败的结果都会立即输出，便于观察长时间运行的清理任务。
无 tag manifest 在同一依赖轮次内最多并发执行 4 个删除；引用冲突会延后到下一轮。

## 安全使用

- 第一次执行 `--apply` 时只使用专门创建的测试仓库。
- 每次都先人工核对 dry-run 输出。
- 使用满足操作需求的最小权限 PAT，并定期轮换。
- 不要把 PAT 或 Cookie 写入配置文件、脚本或命令行参数。
- 使用完毕后执行 `unset DH_PAT DH_COOKIE`。
- manifest 删除不可恢复；工具不会绕过 Docker Hub 的引用保护。
