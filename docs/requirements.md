# 需求基线

## 目标

本项目提供一个面向 Docker Hub 的命令行清理工具，在不依赖 Docker daemon 的前提下完成以下任务：

1. 清理最后一次拉取时间早于指定时间的 tag。
2. 清理仓库中不再由任何 tag 引用的 manifest。
3. 支持单个仓库或 namespace 下全部有权限访问的仓库。

## 清理策略

### 长期未拉取的 tag

1. 截止时间支持带时区的 ISO 8601 时间，或 `24h`、`180d`、`8w` 等相对时长。
2. 只在 `tag_last_pulled` 明确存在且早于截止时间时选择 tag。
3. `tag_last_pulled` 为空的 tag 默认保留。用户显式启用 never-pulled 策略后，仅当 `tag_last_pushed` 也存在且早于截止时间时才能选择。
4. 用户可以重复提供 glob 模式保护 tag；任何匹配的 tag 都不能成为候选项。
5. 删除 tag 必须调用 Docker Hub 的 tag 管理接口，不能以删除底层 manifest 代替单独解除 tag 引用。

### 无 tag manifest

1. 无 tag manifest 定义为 Docker Hub Image Management 返回的全部 manifest digest 与计划保留 tag 的递归可达 manifest digest 的差集。可达集合必须包含 tag 的根 digest，以及 OCI image index 或 Docker manifest list 直接或间接引用的所有子 manifest。同时启用 stale tag 和无 tag 策略时，仅由待删除 tag 引用的 manifest 必须在同一删除计划中成为无 tag 候选。
2. `crane ls` 只能列出 `/tags/list` 返回的 tag，包括名称形似 digest 的 tag，不能用于枚举无 tag manifest。
3. Docker Hub 当前没有使用 PAT 枚举全部 manifest 的公开 API。工具必须把浏览器会话 Cookie 限定在 Image Management 发现流程中；删除仍使用 PAT 派生的 Registry 凭据。
4. 已知 digest 通过 `crane delete` 删除。被 image index、tag 或其他对象引用的 manifest 由 Docker Hub 拒绝删除，工具不得绕过引用保护。同一计划中其他 manifest 删除后，工具可以对明确的引用冲突执行有界重试。
5. Image Management 属于未公开的网页接口。响应格式、分页游标或认证方式不符合预期时，工具必须安全失败，不得根据不完整清单执行删除。
6. 计划阶段必须通过只读 registry manifest 检查计算保留引用闭包；manifest 读取失败、JSON 非法、descriptor 非法或 media type 未知时必须在任何删除前安全失败。

## 安全与认证

1. 默认行为必须为 dry-run，只输出候选项。
2. 实际删除必须同时提供 `--apply` 和与目标 namespace 完全一致的 `--confirm`。
3. Docker Hub 用户名和 PAT 从环境变量读取；PAT 也可以在交互式终端安全输入，但不能通过命令行参数传递。
4. Cookie 只从环境变量读取，且仅在启用无 tag 清理时要求。
5. 调用 `crane` 时使用临时 `DOCKER_CONFIG`，运行结束后删除，不能修改用户现有 Docker 登录配置。
6. 日志和异常不能输出 PAT、JWT、Cookie 或临时认证文件内容。
7. 一个候选项删除失败不能阻止后续候选项继续处理；命令最终以非零状态报告部分失败。
8. apply 模式必须逐项实时输出删除成功和最终失败结果，不能等待整个计划结束后再集中输出。
9. 无 tag manifest 删除可以在单个隔离认证会话内有界并发；默认并发度为 4，用户可以通过正整数 CLI 参数调整。参数必须在认证或删除前验证；依赖冲突仍必须分轮处理，且并发不得扩大到 tag 删除。

## CLI

CLI 至少提供以下选项：

- `--namespace`：必填，Docker Hub 用户或组织。
- `--repository`：可重复；省略时分页获取 namespace 下全部仓库。
- `--before`：启用长期未拉取策略并指定截止时间。
- `--include-never-pulled`：将从未拉取且 push 时间早于截止时间的 tag 纳入候选。
- `--keep-tag`：可重复的 tag glob 保护规则。
- `--untagged`：启用无 tag manifest 清理。
- `--manifest-workers N`：无 tag manifest 删除并发度，必须为正整数，默认为 4。
- `--apply --confirm <namespace>`：执行删除。

用户必须至少选择 `--before` 或 `--untagged` 中的一项。

## 运行环境

1. 支持 Python 3.11–3.14，以 Python 3.14 作为首选开发版本。
2. 运行时只使用 Python 标准库；`crane` 作为外部 CLI 依赖，由 mise 管理。

## 非目标

- 不实现 Docker Hub 之外注册表的生命周期策略。
- 不强制删除仍被引用的 manifest 或 blob。
- 不将 Cookie 持久化到配置文件。
- 不提供守护进程或内置调度器；定时运行由 cron、CI 或其他调度系统负责。
