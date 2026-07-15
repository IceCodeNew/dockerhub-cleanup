# Docker Hub Cleanup

一个安全预览并清理 Docker Hub 仓库的命令行工具，支持：

- 清理最后一次拉取时间早于指定时间的 tag。
- 清理不再由任何 tag 引用的 manifest。
- 处理单个仓库，或自动分页处理 namespace 下全部可见仓库。

默认行为始终是 dry-run。实际删除必须显式提供 `--apply --confirm <namespace>`。

完整需求与架构见 [docs/requirements.md](docs/requirements.md) 和 [docs/design.md](docs/design.md)。

## 工作原理

Docker Hub 的公开 API 提供 tag 的 `tag_last_pulled` 和 `tag_last_pushed`，因此长期未拉取策略只使用 PAT 即可完成。

标准 OCI Registry API 和 `crane ls` 只能列出 tag，不能枚举没有 tag 的 manifest。Docker Hub 当前也没有使用 PAT 获取完整 manifest 清单的公开 API。因此，无 tag 发现使用 Docker Hub 网页 Image Management 的会话接口，并且需要临时浏览器 Cookie；得到 digest 后仍由 PAT 和 `crane delete` 完成删除。

Cookie 只进入 Image Management adapter，不会传给 `crane`。`crane` 登录使用临时 `DOCKER_CONFIG`，不会修改现有 Docker 登录配置。

## 环境

- Python 3.11–3.14
- uv 0.11.28
- crane 0.21.7
- mise（推荐，用于固定本地工具版本）

安装开发环境：

```bash
uv sync --frozen
```

## 认证

创建具有 Read、Write、Delete 权限的 Docker Hub Personal Access Token。不要把 PAT 或 Cookie 写入仓库或命令行参数：

```bash
export DH_USERNAME='your-docker-id'
IFS= read -r -s -p 'Docker Hub PAT: ' DH_PAT
printf '\n'
export DH_PAT
```

只有启用 `--untagged` 时才需要 `DH_COOKIE`。登录 `hub.docker.com`，从浏览器开发者工具 Network 中任一 `hub.docker.com` 请求复制完整 `Cookie` 请求头值，然后临时设置：

```bash
IFS= read -r -s -p 'Docker Hub Cookie: ' DH_COOKIE
printf '\n'
export DH_COOKIE
```

浏览器 Cookie 会过期，且依赖 Docker Hub 未公开的网页接口。接口变化时工具会安全失败，不会使用不完整清单执行删除。

## 使用

预览单个仓库中 180 天前最后拉取的 tag，并保护 `latest` 与生产 tag：

```bash
uv run --frozen dockerhub-cleanup \
  --namespace your-docker-id \
  --repository app \
  --before 180d \
  --keep-tag latest \
  --keep-tag 'prod-*'
```

截止时间也可以使用带时区的 ISO 8601：

```bash
uv run --frozen dockerhub-cleanup \
  --namespace your-docker-id \
  --before 2026-01-01T00:00:00Z
```

`tag_last_pulled` 为空表示从未拉取，默认不会删除。若要将“从未拉取且 push 时间也早于 cutoff”的 tag 纳入候选：

```bash
uv run --frozen dockerhub-cleanup \
  --namespace your-docker-id \
  --before 180d \
  --include-never-pulled
```

预览 namespace 下全部仓库的无 tag manifest：

```bash
uv run --frozen dockerhub-cleanup \
  --namespace your-docker-id \
  --untagged
```

核对完整 dry-run 输出后，同时执行两项策略：

```bash
uv run --frozen dockerhub-cleanup \
  --namespace your-docker-id \
  --before 180d \
  --untagged \
  --keep-tag latest \
  --apply \
  --confirm your-docker-id
```

若一个候选删除失败，工具会继续处理后续候选，并在结束时返回非零状态。仍被 image index、tag 或其他对象引用的 manifest 会由 Docker Hub 拒绝删除。

## 开发与验证

```bash
mise run test
mise run coverage
mise run lint
SKIP=no-commit-to-branch prek run --all-files
```

CI 覆盖 Python 3.11–3.14，并要求语句和分支覆盖率均保持 100%。所有单元测试使用 fake transport 和 fake subprocess，不访问真实 Docker Hub。

## 安全建议

- 第一次 apply 应只针对专门创建的测试仓库。
- 始终人工核对 dry-run 输出。
- 使用最小权限 PAT，并定期轮换。
- 不要持久化浏览器 Cookie。
- manifest 删除不可恢复；工具不会绕过 Docker Hub 的引用保护。

## License

[MIT](LICENSE)
