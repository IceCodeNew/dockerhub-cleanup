# 设计

## 架构

项目按依赖方向分为四层：

```text
CLI -> cleanup service -> domain policies
                    -> Docker Hub adapter
                    -> Image Management adapter
                    -> crane adapter
```

核心策略不直接调用网络、读取环境变量或启动子进程。Docker Hub、网页会话和 `crane` 都通过窄接口接入，使候选选择与执行流程可以使用内存 fake 完整测试。

## 领域模型

`Tag` 保存仓库、名称、digest、最后拉取时间和最后 push 时间。`Candidate` 保存候选类型、仓库、reference 和可审计原因。

通用策略包含：

- 解析绝对或相对截止时间。
- 根据 cutoff、never-pulled 开关和保护模式筛选 stale tag。
- 从任意 JSON 结构提取规范 SHA-256 digest。
- 以集合差计算未被计划保留 tag 的 manifest 图递归引用的 digest，使仅由 stale tag 引用的 manifest 在删除前就进入同一不可变计划。

所有时间进入领域层前统一为 UTC aware `datetime`。无时区的 Docker Hub 值视为 UTC；CLI 提供的绝对时间必须可明确归一化。

## Docker Hub 适配器

PAT 先通过 `POST /v2/auth/token` 换取短期 JWT。后续 Hub API 请求使用 Bearer JWT，并沿响应中的 `next` URL 分页：

- `GET /v2/namespaces/{namespace}/repositories`
- `GET /v2/namespaces/{namespace}/repositories/{repository}/tags`
- `DELETE /v2/namespaces/{namespace}/repositories/{repository}/tags/{tag}`

适配器负责 URL 编码、显式拒绝 transport 返回的非 2xx 状态、响应结构校验和把 API 字段转换为领域模型。分页地址必须与配置的 Hub API 同源，避免把 Bearer JWT 转发给不可信主机。HTTP 错误统一转换为不包含认证信息或响应正文的应用异常。
Hub API 的 GET 和认证 POST 遇到网络错误、429 或瞬时 5xx 时执行两次有界退避重试，并尊重不超过 30 秒的数值 `Retry-After`；DELETE 不重试，避免重放结果不确定的删除。
Docker Hub 偶尔会在不足 `page_size` 的末页后仍返回陈旧的 `next`，且该 URL 返回 404；适配器保留首个请求中已知的 page size，不依赖后续 `next` URL 重复该参数。仅在上一页明确不足请求大小时，适配器把这个 404 视为分页结束；首屏或满页后的 404 仍安全失败。

## 无 tag 发现

标准 OCI Distribution API 的 `/tags/list` 不返回无 tag manifest。Docker Hub 网页的 Image Management 当前通过以下会话接口获取仓库中的 image 和 index：

```text
/repository/docker/{namespace}/{repository}/image-management.data
```

第一次请求使用 GET，后续页使用带 `lastEvaluatedKey` 的 POST。响应使用带整数引用的扁平序列；适配器通过对象中的键和值引用解析游标，并把 devalue 的 `-5`（JavaScript `undefined`）值引用视为分页结束。适配器递归提取响应中的规范 SHA-256 digest，并拒绝重复分页游标，避免接口变化导致无限循环或不完整删除。
请求使用明确的 `dockerhub-cleanup` User-Agent 和同仓库页面 Referer，满足 Docker Hub 网页路由的请求校验。
只读发现请求遇到瞬时网络错误时执行两次有界指数退避重试；Docker Hub 删除请求不重试。

发现流程得到全部 digest 后，service 把公开 Hub API 返回且不在 stale tag 计划中的 digest 作为保留根。crane 适配器通过 `crane manifest` 读取这些根：普通 image manifest 结束当前路径，OCI image index 和 Docker manifest list 的 descriptor digest 进入保护集合，嵌套 index 继续遍历。已检查集合同时避免重复请求和异常环。descriptor 结构、digest 或 media type 不符合预期时，整个计划安全失败。

最终候选是 Image Management 全量 digest 与保留可达闭包的差集。Cookie 不进入领域模型，也不传递给 `crane`。

## 删除执行

stale tag 通过 Hub API 删除，以保留共享同一 manifest 的其他 tag。

无 tag 计划使用 `crane manifest` 读取保留 manifest 图；候选 digest 通过以下形式交给 `crane`：

```text
crane delete index.docker.io/{namespace}/{repository}@{digest}
```

适配器优先使用 mise 中的 `crane`，缺少 mise 时才查找 PATH 中的独立二进制。执行前在临时目录中完成 `crane auth login --password-stdin`，并通过 `DOCKER_CONFIG` 隔离认证状态。传给子进程的环境会移除 `DH_PAT` 和 `DH_COOKIE`；所有调用都有有限超时，认证失败或调用结束后都会清理临时目录。
manifest 删除失败的 stderr 若包含大小写无关的 `referenced by` 片段，适配器把它分类为依赖冲突并交给 service 分轮重试；其他文案仍作为普通安全失败处理。

## 应用流程

1. CLI 在任何认证前验证策略选择、apply 确认和 manifest worker 数量。
2. 使用 PAT 创建 Docker Hub 客户端。
3. 确定目标仓库列表。
4. 对每个仓库收集 tag；启用无 tag 策略时，在同一隔离 crane 会话中递归读取保留 manifest 图并执行候选选择。
5. 输出完整 dry-run 计划。
6. apply 模式下先逐个删除 stale tag，再在单个临时 crane 会话中按 CLI 配置的 worker 数量（默认 4）删除同一轮无 tag digest。明确因其他 image 引用而失败的 manifest 会在同轮有删除进展时进入下一轮；没有进展时停止重试并报告失败。
7. service 在每个删除成功或失败最终确定时调用进度回调，CLI 立即 flush 对应输出。
8. 汇总失败并以非零状态退出。

任何发现阶段错误都会在删除开始前终止，确保不会使用部分候选清单执行变更。

## 测试边界

- 领域测试覆盖时间解析、保护模式、never-pulled 语义、digest 提取和集合差。
- Hub 适配器测试使用可注入 HTTP transport，覆盖认证、分页、URL 编码、响应校验与错误脱敏。
- Image Management 测试覆盖 GET/POST 分页、重复游标和 Cookie 缺失。
- crane 测试使用临时目录与 fake runner，覆盖 manifest 图遍历、响应校验、命令、标准输入、超时、凭据环境边界和 `DOCKER_CONFIG` 清理，不调用真实注册表。
- CLI 与 service 测试使用 fake adapters，覆盖 dry-run、确认门槛、部分失败及退出状态。
