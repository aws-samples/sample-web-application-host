# Reserved Mode — 类 Railway 完整测试计划（多租户访问为核心）

> 目标：验证迁移后的 Reserved Mode 平台在功能上对等 Railway，**重点验证多租户访问的正确性、隔离性与固定 URL 保证**。
> 日期：2026-07-06 | 依据 spec：`docs/superpowers/specs/2026-07-06-reserved-mode-migration-design.md`

---

## 0. 被测系统现状与测试环境补齐

**现状**：已部署子系统 C（`ReservedRuntimeStack`）——ECS on EC2 集群 + m6g.4xlarge 实例，密度已验证 ≥400 task/台。但当前跑的是**无差别 idle nginx**，无租户区分、无路由层、无公网入口。

**多租户测试必须补齐的最小组件**（测试专用，非生产完整实现）：

| 组件 | 测试版实现 | 对应 spec |
|------|-----------|----------|
| 可区分租户 app | nginx 容器，`TENANT_ID` 环境变量注入首页 + 返回容器 HOSTNAME | 模拟 E 层构建产物 |
| L7 路由层 | Envoy task，按 `Host` 头路由到 `宿主IP:hostPort` | 子系统 B（Envoy） |
| 路由数据源 | DynamoDB `app_routes` 表 + 注册脚本 | 子系统 B（路由源） |
| 公网入口 | **面向公网的 ALB → Envoy**（测试便利，替代生产的 CloudFront→NLB→Envoy） | 简化 A 层 |

> **入口简化说明**：生产链路是 `CloudFront → NLB → Envoy`。测试只验证**多租户路由正确性**（其核心逻辑在 Envoy），故用公网 ALB 直连 Envoy 作为入口，跳过 CloudFront/NLB（它们不改变 Host 路由语义）。此简化已获授权（"studio 的 ALB 变公网方便测试"）。

**测试环境架构**：
```
测试客户端 (curl/websocat/负载工具)
      │  HTTP/WS，Host: tenant-<id>.<test-domain>
      ▼
公网 ALB (test-only)  ──健康检查──> Envoy
      │
      ▼
Envoy (L7, 按 Host 路由)  ──读──> DynamoDB app_routes
      │
      ▼
租户 nginx task (bridge, 宿主IP:动态hostPort)
  各租户返回 {tenant_id, hostname, headers}
```

---

## 1. 测试目标与范围

**核心目标**：证明"一租户一容器 + 固定 URL + 按 Host 路由"的多租户模型正确、隔离、稳定。

**范围内**：多租户路由正确性/隔离、固定 URL、故障隔离、健康检查摘除、WebSocket、高密度路由、Railway 功能对标。

**范围外**（Day-1 spec §8 已排除）：Sleep/Wake、多 Region、CloudFront/NLB 实链路（测试用 ALB 替代）、自动构建（用预构建镜像替代）。

---

## 2. 多租户访问测试用例（核心，重点）

> 断言约定：租户 app 返回 JSON `{"tenant_id": "<配置值>", "hostname": "<容器ID>"}`。
> "路由正确" = 响应的 `tenant_id` == 请求 Host 的 subdomain。

### MT-1 路由正确性（Positive Routing）
- **目标**：每个租户 URL 只路由到该租户的容器。
- **前置**：部署 N=10 个可区分租户 app（tenant-1..tenant-10），各注册路由。
- **步骤**：依次 `curl -H "Host: tenant-<i>.<domain>" http://<alb>/`，i=1..10。
- **通过判据**：每个响应的 `tenant_id` == `tenant-<i>`，10/10 全对。
- **失败信号**：任一响应租户 ID 不匹配 = 路由错配（严重）。

### MT-2 租户隔离 / 无串扰（No Cross-Tenant Leakage）
- **目标**：高并发下租户 A 的请求绝不到达租户 B 的容器。
- **步骤**：对 10 个租户 URL 各发 100 并发请求（共 1000），收集每个响应的 `(请求subdomain, 返回tenant_id)`。
- **通过判据**：1000 个响应零错配（请求 subdomain == 返回 tenant_id）。
- **工具**：`hey` 或并发 curl 脚本 + 响应校验。

### MT-3 固定 URL（Stable URL across Restart/Migration）★核心卖点
- **目标**：容器重启/迁移导致 `宿主IP:hostPort` 变化后，租户 URL 不变仍可访问。
- **步骤**：
  1. 记录 tenant-1 当前路由 `host_ip:host_port` 和容器 HOSTNAME。
  2. `aws ecs stop-task` 停掉 tenant-1 的 task。
  3. 重新 `run-task` 拉起 tenant-1（获得**新** `host_ip:host_port` 和新 HOSTNAME）。
  4. 控制面/脚本更新 `app_routes` 的 tenant-1 条目为新值。
  5. 等待 <5s，再次 `curl -H "Host: tenant-1.<domain>"`。
- **通过判据**：URL 完全不变；响应 `tenant_id`==tenant-1；HOSTNAME 为新容器（证明确实迁移了）；路由生效延迟 <5s。

### MT-4 未注册租户 → 404（Unregistered Subdomain）
- **目标**：访问未注册 subdomain，Envoy 返回 404，不泄漏到默认/他人后端。
- **步骤**：`curl -H "Host: tenant-999.<domain>" http://<alb>/`（tenant-999 未注册）。
- **通过判据**：HTTP 404（Envoy 层返回，非任何租户容器）。

### MT-5 租户故障隔离（Fault Isolation — OOM）★核心
- **目标**：一个租户容器 OOM 崩溃，不影响其他租户。
- **步骤**：
  1. 基线：确认 tenant-2、tenant-3 正常返回 200。
  2. 对 tenant-2 容器注入内存压力撑爆 2048MB hard limit（`stress-ng --vm` 或大内存分配），触发 OOM kill。
  3. 立即并持续访问 tenant-3、tenant-4 的 URL。
- **通过判据**：tenant-2 返回 5xx/503（或短暂不可用后被健康检查摘除）；tenant-3/4 **全程 200 不受影响**（内存 hard limit 隔离生效）。

### MT-6 健康检查摘除（Unhealthy Tenant Removal — No Hang）
- **目标**：租户容器假死（端口在但不响应），Envoy 主动健康检查摘除，请求快速失败不 hang。
- **步骤**：让 tenant-5 容器 SIGSTOP（进程冻结，端口仍监听）→ 持续访问 tenant-5 URL。
- **通过判据**：Envoy 在健康检查阈值内摘除该 upstream；请求快速返回 503（**非超时 hang**，响应时间 < Envoy 健康检查间隔 + 超时）。

### MT-7 WebSocket 多租户（Concurrent WS Isolation）
- **目标**：各租户独立 WS 长连接，互不串扰，长连接不被中途断开。
- **前置**：租户 app 支持 WS echo（或部署 WS echo 镜像）。
- **步骤**：对 tenant-6、tenant-7 同时建立 WS 连接，各自 send 唯一 payload，持续 > ALB/Envoy idle timeout（配合应用层 ping）。
- **通过判据**：各租户 WS 收到自己的 echo（无串扰）；连接持续存活未被 idle timeout 断开。

### MT-8 高密度路由正确性（Routing at Scale）
- **目标**：数百租户时路由表仍 100% 正确，新增租户路由 <5s 生效。
- **步骤**：
  1. 注册 200 个租户路由（复用密度测试的多 task 能力，每 task 一个 tenant_id）。
  2. 随机抽样 50 个租户 URL 访问，校验路由正确。
  3. 新增 1 个租户 → 计时从写入 `app_routes` 到该 URL 首次可访问的延迟。
- **通过判据**：抽样 50/50 正确；新路由生效延迟 <5s（spec ADR-5 SLO）。

### MT-9 并发压力下路由稳定性（Load Stability）
- **目标**：高 QPS 并发下路由不错配、5xx 可控。
- **步骤**：对 10 个租户 URL 施加持续负载（如 `hey -z 60s -c 50`），监控错配数、5xx 率、p99 延迟。
- **通过判据**：路由错配 0；5xx 率 < 1%（排除故意注入的故障）；p99 延迟在可接受带宽。

---

## 3. Railway 功能对标测试矩阵

> 对照 spec §5 Railway 功能对标表逐项验证。

| # | Railway 功能 | 用例 | 方法 | 通过判据 |
|---|-------------|------|------|---------|
| RW-1 | 部署→固定 URL | 用预构建镜像 `run-task` + 注册路由 | 部署一个租户 app → 访问固定 URL | 返回 200，URL 稳定 |
| RW-2 | 24/7 常驻运行 | 长时运行不被回收 | 租户 task 运行 ≥30min，周期访问 | 全程可用，无自动停止 |
| RW-3 | 按用量计费 | 自采集 agent 采 per-tenant CPU/mem | 跑已知负载 app → 查 `billing_records` | 用量非零且与负载相符（ADR-6） |
| RW-4 | WebSocket | MT-7 | 见 MT-7 | 见 MT-7 |
| RW-5 | 环境变量/Secret | Secrets Manager → ECS 注入 | 注入 `TENANT_ID` 等，容器内读到 | 容器返回注入的值 |
| RW-6 | 日志 | CloudWatch Logs per tenant | 访问触发日志 → 查 log group | 各租户日志独立可查 |
| RW-7 | 一键回滚 | Task Definition revision 切换 | 部署 v2 → 回滚到 v1 | URL 不变，内容回到 v1 |
| RW-8 | 资源上限 | 2GB OOM cap | MT-5 的 OOM 部分 | 超 2GB 被 kill，cap 生效 |
| RW-9 | DDoS/边缘防护 | （生产 CloudFront+WAF，测试环境跳过） | N/A Day-1 测试 | 记录为生产验证项 |

---

## 4. 测试方法与工具

| 类别 | 工具 | 用途 |
|------|------|------|
| 单请求路由校验 | `curl -H "Host: ..."` | MT-1/3/4 |
| 并发/负载 | `hey` / `wrk` / 并发 curl 脚本 | MT-2/8/9 |
| WebSocket | `websocat` | MT-7 |
| 故障注入 | `stress-ng`（OOM）、`kill -STOP`（假死） | MT-5/6 |
| 路由管理 | boto3 脚本操作 `app_routes` | MT-3/8 |
| 观测 | CloudWatch Logs、ECS DescribeTasks、Envoy admin `/stats` `/clusters` | 全程 |

**响应校验脚本原则**：每个响应解析 `tenant_id`，与请求 Host 的 subdomain 比对，累计错配数。错配 > 0 即用例失败。

---

## 5. 测试数据

- **租户 app 镜像**：`public.ecr.aws/nginx/nginx:stable`（arm64 多架构），entrypoint 覆盖：
  `sh -c 'echo "{\"tenant_id\":\"$TENANT_ID\",\"hostname\":\"$HOSTNAME\"}" > /usr/share/nginx/html/index.html; nginx -g "daemon off;"'`
- **租户规模**：功能用例 N=10；高密度用例 N=200。
- **测试域名**：使用 `*.example.com`（已有 ACM 证书）或 ALB 原生 DNS + Host 头注入。

---

## 6. 执行计划与顺序

1. **环境搭建**：部署 Envoy + 公网 ALB + `app_routes` 表 + N 个租户 app + 注册路由。
2. **冒烟**：MT-1（10 租户路由正确）+ MT-4（404）——最快验证链路通。
3. **核心多租户**：MT-2（隔离）→ MT-3（固定 URL）→ MT-5（故障隔离）→ MT-6（健康摘除）。
4. **进阶**：MT-7（WS）→ MT-8（高密度）→ MT-9（负载）。
5. **Railway 对标**：RW-1..RW-8。
6. **清理**：`cdk destroy` 测试栈。

---

## 7. 通过标准（Exit Criteria）

- **必过**（阻断级）：MT-1、MT-2、MT-3、MT-4、MT-5 全通过（路由正确性 + 隔离 + 固定 URL 是平台立身之本）。
- **应过**：MT-6、MT-7、MT-8、MT-9、RW-1..RW-8。
- **记录**：任何失败附 Envoy `/stats`、ECS task 状态、CloudWatch 日志作为证据。
