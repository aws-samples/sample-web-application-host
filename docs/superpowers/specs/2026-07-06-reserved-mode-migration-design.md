# Reserved Mode → AWS 迁移实施规划 Spec

## 0. 文档头

| 项 | 值 |
|----|----|
| 目的 | 将 website-builder 的 Reserved 托管模式从 Railway 迁移到 AWS，产出可执行的子系统实施规划（本 spec + 后续分子系统 plan） |
| 日期 | 2026-07-06 |
| 状态 | Approved design → Ready for implementation planning（本 spec 不含实现代码） |
| 决策来源 | (1) 需求评估稿 `reserved-mode-rp-evaluation.md`（v6）；(2) Codex(GPT-5.5) 交叉验证采纳/驳回记录（见 §9）；(3) 用户批准的架构方向与硬约束 |
| 范围 | Day-1 MVP，排除项见 §8 |
| 交付物性质 | 规划文档。所有 schema/接口/验证标准可直接转为实施 backlog |

**本 spec 的权威性边界**：架构方向已锁定。标注【已定】的是硬约束，不得推翻。标注【已核实】的附 AWS 文档出处。标注【Open Question】的进入 §7，须在实施中用指定方法验证。

---

## 1. 背景与目标

### 1.1 现状

website-builder 有两种 hosting mode：

| 模式 | 底层 | 状态 |
|------|------|------|
| Autoscale | AWS Lambda + Web Adapter + CloudFront（本 repo 即其参考实现） | 生产中 |
| Reserved | Railway（外部平台） | 生产中，~900 apps，需迁移 |

Reserved 模式特征：24/7 常驻容器化 Web App，**一租户 = 一容器（ECS Task）**，按实际 CPU/内存计费，通过固定 URL `https://<subdomain>.<wildcard-domain>` 访问。

### 1.2 迁移动因（浓缩）

Railway 平台级风险：整站宕机（GCP 封号致 8h 下线）、共享出口 IP 污染（被 Google/Mailjet/Discord 封）、安全声誉（被用作钓鱼基础设施）、API 限速。win-back 核心理由是**可靠性 + 数据可控 + 规模无上限**，成本只需持平。

### 1.3 规模目标【已定】

| 里程碑 | Apps | 承载策略 |
|--------|------|---------|
| Day-1 | ~900 | 3× m6g.4xlarge，2× Envoy，单 NLB |
| 近期 | 数千~10k | ASG 自动扩 EC2，Envoy 加 replica |
| 架构不重构上限 | ~100k | 同架构可达；Envoy 路由源需从 DynamoDB 轮询演进到 xDS 推送（非 Day-1，见 ADR-5） |

**设计原则**：Day-1 满足 900→10k，架构不设计成 100k 才需要的形态（避免过度设计），但**必须标注每个 Day-1 机制的失效规模点**，使演进路径清晰。

---

## 2. 架构总览

### 2.1 修正后的请求链路【已定】

```
用户浏览器  https://<subdomain>.<wildcard-domain>
      │  (viewer TLS 在 CloudFront 终止)
      ▼
┌─────────────────────────────────────────────────────────────┐
│ CloudFront distribution (Reserved 专用，新建，与 Autoscale 解耦) │
│   • ACM 泛域名证书 (viewer 侧)                                  │
│   • Origin Request Policy: 转发 Host header (取代 Lambda@Edge)   │
│   • VPC Origin 指向 internal NLB                                │
│   ✗ 无 Lambda@Edge (VPC Origins 与 L@E origin 触发器互斥)        │
└─────────────────────────────────────────────────────────────┘
      │  (CloudFront 服务托管 ENI → 私网骨干)
      ▼
┌─────────────────────────────────────────────────────────────┐
│ internal NLB (L4/TCP, 3 AZ, 挂 security group)                 │
│   • Listener: TCP (非 TLS listener — VPC Origin 硬约束)         │
│   • Target Group: Envoy×2, TCP 健康检查                         │
│   • 职责: Envoy 集群入口 + 负载均衡 + Envoy 存活探测            │
└─────────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│ Envoy ×2 (L7 代理, ECS Service)                                │
│   • TLS 终止在此 (若 origin 走 HTTPS，见 ADR-4)                 │
│   • 按 Host header 匹配 → 转发 宿主EC2私有IP:动态hostPort        │
│   • 路由源: DynamoDB app_routes 增量拉取 (<5s 生效)             │
│   • 主动健康检查每个 upstream；不健康自动摘除                    │
└─────────────────────────────────────────────────────────────┘
      │  (bridge 动态端口)
      ▼
┌─────────────────────────────────────────────────────────────┐
│ ECS Task (租户 app 容器, ECS on EC2, bridge 网络模式)           │
│   容器无独立 IP；端口映射到宿主 EC2 动态 hostPort               │
└─────────────────────────────────────────────────────────────┘
```

> **相对原需求稿的关键修正**：已**去掉 Lambda@Edge**，改用 **CloudFront VPC Origins + Origin Request Policy 转发 Host header**。原设计"Lambda@Edge 硬编码 internal NLB 地址"在物理上不可达（Lambda@Edge 不在 VPC 内，CloudFront edge 不走用户 VPC 私网路由，internal NLB 私有 DNS 不可解析）。详见 ADR-1/ADR-2。

### 2.2 分层总览

```
┌──────────────── 控制面 (全 Serverless) ────────────────┐
│ API Gateway(HTTP API, OAuth2)                           │
│   → Lambda: deploy/status/rollback/delete/config/billing │
│ DynamoDB ×3: app_metadata / app_routes / billing_records │
│ Secrets Manager (env vars, KMS)                          │
│ EventBridge (ECS Task State Change → 路由注册 Lambda)    │
└─────────────────────────────────────────────────────────┘
        │ startBuild            │ RunTask / DescribeTasks
        ▼                       ▼
┌──── 构建层 (隔离 VPC) ────┐  ┌──── 运行层 (ECS on EC2) ────────┐
│ CodeBuild                 │  │ ASG + Capacity Provider, 3 AZ    │
│  privileged=false         │  │ m6g.4xlarge (16vCPU/64GB Graviton)│
│  + CNB Buildpacks         │  │ bridge 模式, ~400 Task/台(待验)   │
│    ↓ push                 │  │ Task: cpu=64(soft) mem=2048(hard) │
│ ECR                       │  │ + Envoy Service (同集群或独立)    │
└───────────────────────────┘  └──────────────────────────────────┘
                                          │ node agent 读容器 stats
                                          ▼
┌──────────── 计费层 (自采集，见 ADR-6) ────────────┐
│ 每台 EC2: node-level 采集 agent (daemon task)        │
│   读本机容器 stats + docker labels 归因 (task-arn→app) │
│   → 分钟级用量直写 billing_records (per-app per-hour) │
│   → 月末 Lambda 出账                                 │
│   ✗ 不用 Container Insights custom metrics(成本击穿) │
└──────────────────────────────────────────────────────┘
```

### 2.3 与现有 Autoscale repo 的复用关系

| 现有资产 | Reserved 模式处置 |
|----------|------------------|
| CDK `ConfigLoader`（config.ini + env 覆盖，`stack.py:29-50`） | **复用**：新 stack 沿用同一配置加载模式 |
| CloudFront distribution + Lambda@Edge（`stack.py:120-209`, `origin_request.py`） | **不复用**：Reserved 新建独立 distribution，无 Lambda@Edge |
| `OriginRequestPolicy`（`stack.py:157-164`，已转发 all headers） | **模式复用**：Reserved 版收窄为转发 Host header |
| DynamoDB `subdomain→target_url`（`stack.py:68-78`） | **改造**：语义变为 `subdomain→宿主IP:hostPort`，见 §5 |
| `deploy_lambda.py`（部署 + 注册路由脚本） | **参考**：控制面 deploy Lambda 是其容器化 + ECS 版 |

---

## 3. 架构决策记录 (ADR)

### ADR-1: CloudFront VPC Origins vs internet-facing NLB + 来源限制

**Status**: Accepted（用户已拍板 VPC Origins）

**Context**: CloudFront/Lambda@Edge 无法访问 internal NLB。需要让 CloudFront 安全回源到私有子网的 NLB。两条路线：
- (A) **VPC Origins**：CloudFront 经服务托管 ENI 私网直连私有子网 NLB。
- (B) **internet-facing NLB** + 靠来源限制（CloudFront 托管 prefix-list / header 密钥）防止公网直连绕过。

**Decision**: 选 **(A) VPC Origins**。

**理由与权衡**：

| 维度 | (A) VPC Origins | (B) internet-facing NLB |
|------|-----------------|--------------------------|
| 隔离性 | NLB 在私有子网，公网不可达，CloudFront 是唯一入口 | NLB 公网可达 |
| 防绕过密钥 | 服务托管 SG 精确限定来源 | **L4 NLB 看不到 HTTP header，无法用 header 密钥校验**；只能靠 CloudFront 托管 prefix-list（数百个共享 IP 段），任何人用 CloudFront 都在该段内 → 隔离性差 |
| 运维 | CloudFront 托管 ENI+SG，无需自管 ACL | 需自管公网暴露面 |
| 约束 | 见下"硬约束" | 无 VPC Origins 的协议约束，但安全性不达标 |

(B) 的致命弱点：Reserved 是 L4 NLB 透传，**NLB 层无法基于 header 密钥鉴权**，只能退化到共享 IP 段限制，这与迁移动因（逃离共享 IP 污染）自相矛盾。故排除。

**Consequences（VPC Origins 硬约束，全部【已核实】）**：
- 来源：ALB/NLB/EC2 私有子网，NLB **必须挂 SG**、**不能是 dual-stack**、**不能有 TLS listener**（我们用 TCP listener，满足）。
- **不支持 Lambda@Edge origin-request/response 触发器** → 连带 ADR-2。
- 不支持 gRPC；NACL 对此流量不生效（用 SG 控制）。
- VPC 需有 IGW（仅标记可接收 internet 流量，**不用于路由到 origin**）+ 私有子网至少 1 个可用 IPv4（IPv6-only 子网不支持）。
- CloudFront 自动创建服务托管 SG `CloudFront-VPCOrigins-Service-SG`；来源放行用 CloudFront 托管 prefix-list 或该服务 SG（后者更严格，限定到你的 distribution）。
- VPC Origin 创建 + 部署到 Deployed 状态最长 ~15min（影响 A 的部署时序）。
- 来源：`private-content-vpc-origins.html`。

### ADR-2: 去掉 Lambda@Edge，用 Origin Request Policy 转发 Host header

**Status**: Accepted

**Context**: Reserved 模式下 Lambda@Edge 的唯一职责是把 Host header 透传给 Envoy 做 L7 路由（不查 DynamoDB、不做 SigV4，与 Autoscale 不同）。而 VPC Origins 与 Lambda@Edge origin 触发器互斥（ADR-1）。

**Decision**: 不用 Lambda@Edge。用 **CloudFront Origin Request Policy 转发 Host header** 实现 Host 透传。

**理由**：透传 header 是 Origin Request Policy 的原生能力（现有 `stack.py:157-164` 已用 `OriginRequestHeaderBehavior.all()` 证明可行）。为一个纯透传动作引入 Lambda@Edge 是过度设计，且与 VPC Origins 冲突。去掉后 A 更简单、与 Autoscale 解耦更干净、消除 edge 冷启动与调试成本。

**Consequences**:
- A 从"改造 Lambda@Edge"变为"新建 distribution + 配 VPC Origin + Origin Request Policy"。
- **需验证 Host header 转发语义**：CloudFront 到自定义/VPC origin 默认会把 Host 改写为 origin domain。必须确认 Envoy 实际收到的是 viewer 的 `<subdomain>.<domain>` 而非 NLB 内部名。这是 A 的头号验证项（§7 / A 验证标准）。备选：若 CloudFront 强制改写 Host，则改用转发一个自定义 header（如 `X-Tenant-Host`），Envoy 按该 header 路由——现有 `origin_request.py:144-169` 已有 `X-Original-Host` 回退先例。

### ADR-3: L7 代理选型 — Envoy (Day-1)

**Status**: Accepted（用户确认 Day-1 用 Envoy）

**Context**: 需要 L7 组件按 Host header 路由到"一租户一 Task"。ALB 与 VPC Lattice 因配额被排除；开源代理候选 Envoy/HAProxy/Traefik。

**Decision**: Day-1 用 **Envoy**。**首要理由是 Day-1 运维关切（动态发现、健康检查、WebSocket、运维复杂度），xDS 演进路径只是次要加分项。**

**AWS 原生方案排除证据【已核实】**：

| 方案 | 排除依据 |
|------|---------|
| ALB + Host routing | **Target Groups per ALB = 100 且不可调**；10k apps 需 ~100 个 ALB（Rules per ALB=100 可调，但 TG 数是硬墙）。不可行 |
| VPC Lattice | resource configs per service network=500，per region=2000；连 900 apps 都撑不住 |

**开源代理对比**：

| 能力 | Envoy | HAProxy | Traefik |
|------|-------|---------|---------|
| 许可证 | Apache 2.0 (CNCF 毕业) | GPLv2 | MIT |
| 动态路由热更新 | xDS / 定期拉取 | Runtime API / DPA / reload | Provider 自动发现 |
| 主动健康检查 | 强，成熟 | 强，成熟 | 有，较弱 |
| WebSocket | 原生 | 原生 | 原生 |
| 高密度资源占用 | 中 | **低（省资源）** | 中 |
| 大规模动态后端 | 强（xDS 面向此设计） | 强 | auto-provider 大规模需谨慎验证 |
| 运维复杂度 | 高（配置面陡） | 中 | 低 |

**结论与降级路径**：
- 团队**已有 Envoy/xDS 能力 → 用 Envoy**（本 spec Day-1 选型）。
- 团队**无 Envoy 经验 → Day-1 可优先 HAProxy**（高密度省资源、健康检查成熟、运维更简单）。
- Traefik 的 auto-provider 可能省掉"DynamoDB→配置"同步器，但大规模需谨慎验证，不作 Day-1 默认。

**Consequences**: 需自建"DynamoDB → Envoy 配置"同步器（B 的核心工作项）。选 Traefik 可能省此同步器，但引入未验证的大规模风险。

### ADR-4: origin 侧 TLS / 协议方案

**Status**: Accepted（本 spec 决策；附【需人工复核】的一处运行期确认）

**Context**: viewer TLS 在 CloudFront 终止。CloudFront → NLB(TCP 透传) → Envoy 的 origin 侧协议有两条路线：
- (i) **CloudFront→origin 走 HTTPS**：NLB TCP 透传，**TLS 在 Envoy 终止**，Envoy 持证书。
- (ii) **CloudFront→origin 走 HTTP**：私网骨干内不加密，Envoy 只做 HTTP L7 路由。

**已核实事实**：`VpcOriginEndpointConfig.OriginProtocolPolicy` 取值 `http-only | match-viewer | https-only`，且有 `OriginSslProtocols`（来源：`API_VpcOriginEndpointConfig.html`）。→ 两条路线技术上都被 VPC Origin 支持。

**Decision**: **Day-1 采用 (ii) HTTP origin**（`OriginProtocolPolicy=http-only`，NLB TCP:80 → Envoy HTTP L7）。**推荐在 B 完成后、上线前评估切换到 (i)。**

**理由**：
1. 流量路径 CloudFront→ENI→NLB→Envoy **全程在 AWS 私有骨干与用户私有子网内**，未经公网。VPC Origins 的安全模型本就以"私网 + 单入口"为主要保障。
2. (ii) 省掉 Envoy 证书签发/部署/轮换与 SNI 校验复杂度，Day-1 交付更快，符合"不为想象需求过度设计"。
3. viewer↔CloudFront 已强制 HTTPS（`ViewerProtocolPolicy=redirect-to-https`），端到端对用户是加密的。

**(i) 的成立条件与代价（如合规要求回源加密则选 (i)）**：
- Envoy 证书需覆盖 CloudFront 建连时的 SNI/Host。**【需人工复核】**：CloudFront VPC Origin HTTPS 回源时对私有 origin 的证书校验/SNI 行为（CloudFront 通常按 origin domain 校验证书 CN/SAN）。VPC Origin 的 origin domain 是 NLB，Envoy 证书需覆盖该名称；私有 CA（ACM PCA）签发即可，无需公网可信。
- 代价：ACM PCA 成本 + 证书轮换自动化 + Envoy 热加载证书。

**Consequences**: Day-1 Envoy listener 为明文 HTTP；`app_routes` 与健康检查均基于 HTTP。切换到 (i) 时改动集中在 Envoy listener + VPC Origin `OriginProtocolPolicy` + NLB listener 端口，不影响路由数据契约。

### ADR-5: Envoy 路由源 — DynamoDB 增量轮询 (Day-1) vs xDS (未来)

**Status**: Accepted

**Context**: Envoy 需知道 `Host → 宿主IP:hostPort`。Day-1 用 DynamoDB 定期拉取；~100k 规模用 xDS 推送。

**Decision**: **Day-1 = DynamoDB 增量拉取**（非全表扫描）。

**增量拉取机制（Day-1 必须实现，不能只写"未来上 xDS"）**：
- `app_routes` 建 GSI on `updated_at`（或维护单调递增 `route_version`）。
- Envoy sidecar 同步器每 5s 只 `Query` since last-seen watermark 的变更（新增/更新/删除），本地维护全量路由快照，增量 patch 后经 Envoy 动态配置（xDS-over-file 或 Envoy 的 ADS local aggregator）热更新。
- 首次冷启动做一次全量加载，之后只走增量。

**失效规模点【必须标注】**：
- 全量冷启动加载在 ~10k 路由内可接受；数万级冷启动加载延迟与 DynamoDB 读放大显著上升。
- 每 5s 轮询 × N 个 Envoy replica 的读放大：10k 级可控；~100k 级出现**热分区风险 + 读成本上升**。
- **触发演进的临界信号**：Envoy 同步器 p99 拉取延迟 > 2s，或 DynamoDB 读 RCU 成为成本大头，或路由生效延迟 > 5s SLO → 切换到 **xDS 推送**（控制面在路由变更时主动推给 Envoy，消除轮询）。

**Consequences**: Day-1 同步器与未来 xDS 控制面共享同一数据源（app_routes），演进时替换的是"拉→推"传输层，数据契约不变。

### ADR-6: 计费数据源 — 节点自采集，不用 Container Insights custom metrics

**Status**: Accepted（用户拍板；spec 自审阶段发现的成本 Blocker，根因修复）

**Context**: 早期设计用 Container Insights 采集 per-app CPU/内存做计费。spec 自审时核实到一条推翻性事实。

**推翻性事实【已核实】**：
- CloudWatch 官方明示 **"Metrics collected by CloudWatch Container Insights are charged as custom metrics"**（来源：`service_utilization.html`）——Container Insights 指标按 **CloudWatch 自定义指标**计费。
- 为做 per-app 归因需"一 app 一 `TaskDefinitionFamily`"，则每 family × 各维度组合产生独立计费指标（每 family ~8–10 个）。
- 自定义指标定价（us-east-1）：前 1 万个 $0.30/个/月，超出 $0.10/个/月。

**成本击穿量化**：

| 规模 | family 计费指标数 | Container Insights 实际月成本 | 需求稿 §7.1 估算 | 偏差 |
|------|-----------------|------------------------|------------------|------|
| 900 apps | ~7,200 | **~$2,400/月** | $30/月 | ~80× |
| 10k apps | ~90,000 | **~$11,000/月** | — | 击穿成本模型 |

按 900 apps ~$2,400/月计，平台总成本从 ~$937 升到 ~$3,300/月，**每 app $0.94→$3.7，比 Railway($1–2)贵 2–3 倍**，直接推翻本次 win-back 的成本前提（"持平或低于 Railway"）。**注**：enhanced observability 模式虽提供 per-TaskId 维度（`ContainerCpuUtilized` 带 `TaskId`，来源 `Container-Insights-enhanced-observability-metrics-ECS.html`），但指标更细=更多=更贵，加剧而非缓解。

**根因判断**：Container Insights 的 custom-metrics 计费模式，成本随 app 数线性增长，天生不适配"每租户独立计费"的高基数场景。这是**数据源选型错误**，非调参可解。

**Decision**: **计费数据源改为节点自采集（需求稿 §8.3 已提 cAdvisor+Prometheus 作为此方向备选）**。
- 每台 EC2 运行一个 node-level 采集 agent（ECS daemon task 或 systemd）。
- agent 读**本机容器 stats**（ECS task metadata endpoint `${ECS_CONTAINER_METADATA_URI_V4}/task/stats`，或直接读 cgroup / `docker stats` API）拿 per-container CPU/内存。
- 用容器上的 docker labels（部署时注入 `app_id` / `task_arn`）做归因，分钟级用量直写 `billing_records`。
- **成本与 app 数解耦**：只按"采集算力（已在 EC2 内，边际为 0）+ DynamoDB 写入”计费，不随指标基数爆炸。

**Consequences**:
- Container Insights 仍可**开启用于运维可观测性**（集群/服务级聚合视图），但**不作为计费数据源**——两者职责分离。
- 计费 agent 是新增自建组件（F 的核心工作项），需保证：宕机不丢账（本地缓冲 + 重试）、时钟一致、幂等写入。
- `MemoryUtilized` 的 page cache 语义问题（§7 #4）在自采集下同样存在（cgroup memory.stat 亦含 cache），仍需压测确认口径。
- 归因不再依赖 `TaskDefinitionFamily` 维度 → "一 app 一 family"的理由从"计费硬要求"降级为"部署/回滚单元的自然选择"（见 C / F）。

---

## 4. 子系统分章

每章统一含：**职责 / 与现有代码关系 / 接口契约 / 阻塞依赖 / 验证标准 / 主要风险**。验证标准均写成"做 X，观察到 Y 即通过"的可执行形式。

### A. 边缘层（CloudFront distribution + VPC Origin + Origin Request Policy）

**职责**：为 Reserved 模式新建独立 CloudFront distribution；viewer TLS 终止；经 VPC Origin 私网回源到 internal NLB；转发 Host header 供 Envoy 路由。**无 Lambda@Edge。**

**与现有代码关系**：复用 `stack.py` 的 `ConfigLoader` 与 `OriginRequestPolicy` 构造模式；不复用 Autoscale 的 distribution / Lambda@Edge / SigV4 逻辑（新建独立 stack）。

**接口契约**：
- 入：viewer HTTPS 请求 `https://<subdomain>.<wildcard-domain>`，ACM 泛域名证书（us-east-1，供 CloudFront 使用）。
- 出：回源到 VPC Origin(NLB)，携带原始 Host（或 `X-Tenant-Host`，见 ADR-2 备选）。
- 配置：`OriginProtocolPolicy=http-only`（ADR-4 Day-1）；`ViewerProtocolPolicy=redirect-to-https`；WebSocket 需显式设置 origin idle timeout（见 §4 WebSocket 小节）；缓存策略对动态 app 建议 `CACHING_DISABLED` 或短 TTL（Reserved 是动态容器，非静态）。

**阻塞依赖**：B 的 NLB 必须先 Active（VPC Origin 创建要求 origin 资源已 Deployed）；VPC Origin 部署到 Deployed 最长 ~15min。

**验证标准（可执行）**：
1. **前置区域检查**：确认业务区域在 VPC Origins 支持列表内且未落在被排除 AZ。业务区域若为 `us-east-1`，NLB 子网**须避开 `use1-az3`**。做法：`aws cloudfront` 建 VPC Origin 成功即通过；被排除 AZ 会创建失败。
2. **Host 透传验证（头号）**：部署一个回显 Host 的测试容器，curl `https://<sub>.<domain>/`，观察容器日志收到的 Host == `<sub>.<domain>`。若收到的是 NLB 内部名 → 切换到自定义 header 方案（ADR-2 备选）并重验。
3. **私网可达验证**：将 NLB 子网改为私有（去公网路由）后，经 CloudFront 仍能访问（证明走 VPC Origin ENI 而非公网）；直接公网访问 NLB DNS 应超时。
4. **端到端**：与 B/C/D 联调后，真实 app 的固定 URL 返回 200。

**主要风险**：Host header 被 CloudFront 改写（→ ADR-2 备选）；区域/AZ 不支持（→ 前置检查拦截）。

### B. 网络层（internal NLB + Envoy×2 + 路由同步器 + TLS/证书 + 健康检查）

**职责**：L4 入口 + L7 Host 路由 + upstream 健康检查 + 路由数据同步。

**与现有代码关系**：全新。DynamoDB 路由源沿用本 repo 的 PAY_PER_REQUEST DynamoDB 模式。

**接口契约**：
- NLB：internal，3 AZ，挂 SG（VPC Origin 硬约束），**TCP listener（非 TLS）**，Target Group = Envoy tasks，**TCP 健康检查**探 Envoy 监听端口。
- Envoy：listener 收 HTTP（ADR-4 Day-1）；route 按 `:authority`/Host 精确匹配 → cluster = 单个 app 的 `宿主IP:hostPort`；**未注册 Host 返回 404**（在 Envoy 层做合法性校验，控制面无需额外校验）。
- 路由同步器：见 ADR-5 增量拉取；输出 Envoy 动态配置。

**bridge 模式服务发现【已核实，关键】**：
- 容器无独立 IP，端口映射到宿主 EC2 的**动态 hostPort**（未指定 hostPort 时 Docker 从 ephemeral 范围随机选，如 47760）。
- 数据来源链：控制面 `ECS.DescribeTasks` → `containers[].networkBindings[].hostPort` 拿动态端口；`task.containerInstanceArn` → `DescribeContainerInstances` → `ec2InstanceId` → `DescribeInstances` → `PrivateIpAddress` 拿宿主私有 IP。二者组合写入 `app_routes`。
- ECS 亦会自动更新 target group / Cloud Map（`networking-networkmode-bridge.html`），但本方案 Envoy 不经 target group 发现单个 app（NLB target 是 Envoy 自身），故走控制面 DescribeTasks 主动提取。

**TLS/证书（Day-1 = HTTP，见 ADR-4）**：Day-1 Envoy listener 明文；切 (i) 时 ACM PCA 私有证书覆盖 NLB origin 名，Envoy 热加载轮换。

**健康检查分层【已核实驳回项澄清】**：NLB 查 Envoy（L4 存活）与 Envoy 查 app Task（L7 就绪）是**正确分层，非缺陷**。注意：**NLB 健康只代表 Envoy 活着，不代表 end-app 可用**；end-app 可用性由 Envoy 主动健康检查保障。

**验证标准（可执行）**：
1. 手动在 `app_routes` 写一条 `宿主IP:hostPort` 指向 C 跑出的真实 running task，5s 内 curl 经 NLB→Envoy 返回该 app 200。
2. kill 该 task，Envoy 主动健康检查在阈值内摘除该 upstream，请求返回 503/无 5xx 泄漏到已知坏后端；重新拉起并注册后自动恢复。
3. 写一个未注册 Host，Envoy 返回 404。
4. **端到端真实性（Codex 采纳项）**：B 的验证必须用 C/E/D 跑出的真实 running task + 真实动态 hostPort + 真实路由记录 + 真实健康态迁移，**不接受空壳健康检查**。
5. WebSocket：建立 WS 连接，持续 > CloudFront/Envoy idle timeout，配合 app 层 ping/pong，连接不被中断。

**主要风险**：Envoy 轮询扩展性（→ ADR-5 失效点 + §7）；readiness 竞态（→ D 状态机）；bridge 端口天花板（→ C 密度门禁）。

### C. 运行层（ECS on EC2 + ASG + Capacity Provider + 密度压测门禁）

**职责**：承载租户 app 容器；高密度、超卖 CPU、硬限内存。

**与现有代码关系**：全新（Autoscale 是 Lambda，无 EC2 集群）。

**配置【已定】**：

| 项 | 值 | 说明 |
|----|----|----|
| 网络模式 | bridge | 高密度，无 ENI 限制 |
| Task cpu | 64 units (soft) | 允许超卖，忙时可用空闲 CPU |
| Task memoryReservation | 128 MB | 调度 floor |
| Task memory (hard) | 2048 MB | OOM cap = $36/月 上限 |
| 实例 | m6g.4xlarge (16vCPU/64GB Graviton) | 性价比 |
| 集群 | ASG + Capacity Provider, 3 AZ | 自动扩缩 |
| 购买 | 1yr RI | 降本 ~40% |
| **每 app 独立 TaskDefinitionFamily** | 是 | 部署/回滚单元（family=`app-{id}`）；**非计费依赖**（计费改自采集，见 ADR-6/F） |

**密度压测门禁（Day-1 头等验证，Codex 采纳项并入）**：
在 1 台 m6g.4xlarge 上跑 100 / 200 / 300 / 400 idle task，逐档验证并记录失效点：

| 验证项 | 通过判据 |
|--------|---------|
| ECS Agent 稳定性 | 高 task 数下 agent 不 OOM、不失联，task 状态上报正常 |
| containerd shim 内存 overhead | 实测每 task 宿主侧固定开销，反推真实密度上限 |
| **bridge 动态端口天花板** | ephemeral 端口范围耗尽点；确认单机 task 数不撞端口墙（归入本门禁，不单列） |
| **计费自采集 agent 高密度完整性（并入 C；ADR-6）** | 数百 task 下 node agent 读 stats 无遗漏容器、单轮采集延迟 < 采集周期、agent 自身 CPU/内存开销可接受；若某档遗漏 → 计费不可靠，调采集并发/周期或降密度 |
| **CPU 计费公平性（头号 Open Question，压测一并验）** | 邻居争抢下，同一 idle app 的 CpuUtilized 读数波动在可接受带宽内；否则动摇"按量计费"卖点 |

**验证标准**：上表全部达标；产出"单机安全密度"数字（替换需求稿的理论值 ~400），作为容量规划输入。

**主要风险**：~400/台是理论值（→ §7 Open Question）；CPU soft limit 非硬隔离导致计费口径偏离（→ §7 头号）。

### D. 控制面（API GW + Lambda CRUD + 部署编排 + EventBridge 路由注册 + readiness 状态机）

**职责**：对外管理 API；编排 build→deploy→注册路由；处理 Task 生命周期事件维护 `app_routes`；管理 config/secret。

**与现有代码关系**：deploy Lambda 是 `deploy_lambda.py` 的容器化 + ECS 版（用 `ECS.RunTask` 替代 `create_function`，用 `app_routes` 写入替代 `subdomain→target_url`）。

**接口契约**：见 §5 的 7 个 REST 端点 + EventBridge 事件契约。

**部署编排流程**：
```
POST /apps/{id}/deploy
 → CodeBuild.startBuild (CNB → ECR)
 → ECS 注册/更新 per-app TaskDefinitionFamily (family = app-{id})
 → ECS.RunTask
 → (EventBridge 异步) Task RUNNING → readiness 检查通过 → 写 app_routes
 → Envoy 增量拉取 → <5s 生效
```

**readiness 状态机（Day-1 最小实现，Codex 采纳；勿过度建模）**：
```
pending → routing → draining → stopped
```
| 状态 | 含义 | 进入条件 |
|------|------|---------|
| pending | task 已 RUNNING 但未纳入路由 | ECS 报 RUNNING |
| routing | 已写入 app_routes，对外可路由 | **控制面主动健康检查通过后**才写入（关键：不是见 RUNNING 就写） |
| draining | 停止中，从路由摘除，排空连接 | 收到 STOPPED/替换信号 |
| stopped | 完全下线，路由已删 | 排空完成 |

**竞态修正（Codex 采纳）**：EventBridge 见 RUNNING **不立即写路由**；必须先主动健康检查（探 app 就绪）通过，再从 pending→routing。避免部署/重启期 Envoy 拉到未 ready 的后端导致 5xx。

**EventBridge 事件处理**：
- Task RUNNING → 提取 `宿主IP:hostPort`（见 B 服务发现链）→ 健康检查 → 写 app_routes（status=routing）。
- Task STOPPED → app_routes 置 draining→stopped 并删条目（Envoy 健康检查此前已摘除，此为清理）。
- Task 重启/迁移（新 RUNNING） → 更新 `宿主IP:hostPort`（IP/端口会变）。

**验证标准（可执行）**：
1. `POST /deploy` 一个真实 app，端到端在预期时间内固定 URL 返回 200。
2. 手动 stop task，观察 app_routes 条目在阈值内被删，Envoy 不再路由到坏后端。
3. 触发 task 迁移，观察 `宿主IP:hostPort` 被更新为新值，URL 不变仍可访问（验证固定 URL 保证）。
4. readiness：在 app 启动慢的容器上验证 pending→routing 只在健康检查通过后发生（构造启动延迟，观察路由写入时机晚于 RUNNING 事件）。

**主要风险**：readiness 健康检查探针定义（app 无统一健康端点时的兜底探测）；EventBridge 事件乱序/重放（幂等写入）。

### E. 构建层（CodeBuild + CNB Buildpacks + ECR）

**职责**：用户提交代码 → 自动构建镜像 → 推 ECR，用户无需写 Dockerfile（对等 Railway）。

**与现有代码关系**：全新；ECR 消费方是 C 的 TaskDefinition。

**接口契约**：
- CodeBuild：独立 VPC，`privileged=false`，IAM 仅 ECR push 权限（最小权限）。
- **Day-1 构建方式【已定】= Cloud Native Buildpacks (CNB)**：自动检测语言/框架，无需 Dockerfile（对等 Railway）。
- 产物：ECR 私有镜像，tag 关联 app_id + version；ECR 经 VPC Endpoint 访问。

**验证标准（可执行）**：
1. 提交一个无 Dockerfile 的示例 app（如 Node/Python），CNB 自动检测并构建成功，镜像出现在 ECR。
2. 该镜像被 C 的 RunTask 拉起并正常提供服务。
3. IAM 越权验证：CodeBuild role 尝试非 ECR 操作被拒（最小权限生效）。

**主要风险**：`privileged=false` 下 CNB 的兼容性（多数 CNB 无需特权，但需实测目标语言栈）；构建缓存策略影响成本/时延。

### F. 计费层（节点自采集 + 归因链 + 计费方法定义 + 聚合出账）

**职责**：按 per-app 实际 CPU/内存用量 × 时间计费；月末出账；idle app 几毛钱、cap $36/月。

**与现有代码关系**：全新；数据源 = 节点自采集 agent（ADR-6），写 `billing_records`。**不依赖 Container Insights 做计费**。

**为什么不用 Container Insights（见 ADR-6）**：其指标按 CloudWatch 自定义指标计费，"一 app 一 family"会在 900 apps 就产生 ~$2,400/月（估算 §7.1 的 80×），万级击穿成本模型。故计费改自采集。

**数据源与归因链设计【ADR-6】**：

- **采集**：每台 EC2 一个 node-level agent（ECS daemon task），周期（默认 1min）读**本机所有容器** stats——`${ECS_CONTAINER_METADATA_URI_V4}/task/stats`（per-container CPU/内存），或直接读 cgroup / Docker stats API。
- **归因**：容器部署时由控制面注入 docker labels（`app_id`、`task_arn`、`version`）。agent 读 stats 时一并读 label，天然拿到 `container → app_id`，无需依赖任何 CloudWatch 维度。归因链：`docker label(app_id) ← 部署时控制面写入`（1:1，最短链）。
- **写入**：agent 将 per-app 分钟级用量直写 `billing_records`（或先本地缓冲再批量写，防抖 + 防丢账）。
- **成本特性**：采集算力在 EC2 内（边际 0），仅 DynamoDB 写入计费，**与 app 数解耦**，不随基数爆炸。

**计费方法定义【必须明确，Codex 采纳】**：

| 计费项 | 口径 | 说明 |
|--------|------|------|
| 采集粒度 | 1 min（agent 采集周期） | Railway 亦分钟级 |
| 聚合周期 | 分钟级用量 → per-app per-hour 桶 | agent 直写或每小时 Lambda 归并 |
| **CPU 计费口径** | **按分钟 CPU 用量求和折算 vCPU-time**（Σ 每分钟均值），非 max/p95 | max/p95 会显著高估 idle app 账单；用量计费取实际消耗积分 |
| **内存计费口径** | **按分钟内存用量均值 × 时长**（GB-hour） | 见下"内存语义" |
| Cap | memory hard limit=2048MB → 单 app ~$36/月封顶 | OOM kill 保护 |

> 明确：**用 avg（分钟级积分求和）计费，不用 max/p95。** 三者会得出完全不同账单；用量计费的正确口径是实际消耗的时间积分。

**内存计费语义【明确定义，Codex 采纳】**：

| 概念 | 含义 | 是否计费 |
|------|------|---------|
| `memoryReservation` (128MB) | 调度 floor，非实际用量 | 否（仅调度） |
| `memory` (2048MB hard) | OOM cap | 否（仅上限=$36） |
| 容器实际内存用量（cgroup `memory.stat`，**含 page cache**） | 实际占用 | **是，计费口径基于此** |

**"可公平售卖的内存口径"定义**：以容器实际内存用量为准。**注意：cgroup 内存统计同样含 page cache**（与 Container Insights 的 `MemoryUtilized` 同源问题），须在密度压测中确认是否虚高 idle app 用量；若明显偏高 → §7 #4 评估扣除 cache（`memory.stat` 的 `cache`/`inactive_file` 分量可拆）。

**聚合出账（最后做）**：
- 分钟级 → 每小时 Lambda（或 agent 侧）归并到 `billing_records`（per-app per-hour）。
- 月末 Lambda：汇总 `billing_records` → 账单，应用 cap。

**验证标准（可执行）**：
1. 跑一个已知负载 app（固定 CPU/内存），1 小时后 `billing_records` 的 CPU/内存 GB-hour 与理论值误差在可接受范围。
2. idle app 验证：常驻 idle，月度折算落在"几毛钱"量级。
3. cap 验证：满载 app 月度折算 ≤ ~$36。
4. 归因正确性：多 app 并存，各自 label 归因不串扰；kill agent 后重启，缺口分钟不重复计费（幂等）。
5. **成本自检**：核算计费层自身月成本（agent 开销 + DynamoDB 写入），确认与 app 数近似线性且远低于 custom-metrics 方案。

**主要风险**：自采集 agent 高密度完整性（→ 并入 C 门禁）；agent 宕机丢账（→ 本地缓冲+重试+幂等）；CPU 公平性（→ §7 头号）；page cache 虚高（→ §7 #4）。

---

## 5. 数据契约

### 5.1 DynamoDB 表（3 张，均 PAY_PER_REQUEST）

**`app_metadata`** — app 元数据、版本、归因映射

| 字段 | 类型 | 说明 |
|------|------|------|
| `app_id` (PK) | S | 租户 app 唯一 ID |
| `subdomain` | S | 固定 URL 的 subdomain（唯一） |
| `task_def_family` | S | = `app-{app_id}`，计费归因 1:1 关键 |
| `current_version` | S | 当前 TaskDefinition revision |
| `previous_version` | S | 回滚目标 |
| `secret_arn` | S | Secrets Manager env vars ARN |
| `cpu_units` / `mem_reservation` / `mem_hard` | N | 资源配置（默认 64/128/2048） |
| `status` | S | 见 readiness 状态机 |
| `created_at` / `updated_at` | S | ISO8601 |

**`app_routes`** — Envoy 路由源（**语义修正：宿主IP:动态hostPort，非 Task IP:Port**）

| 字段 | 类型 | 说明 |
|------|------|------|
| `subdomain` (PK) | S | Envoy 按 Host 匹配键 |
| `host_ip` | S | **宿主 EC2 私有 IP**（bridge 模式，非容器 IP） |
| `host_port` | N | **动态 hostPort**（Docker 随机分配，如 47760） |
| `app_id` | S | 反查 |
| `task_arn` | S | 当前 task |
| `status` | S | pending/routing/draining/stopped |
| `route_version` | N | 单调递增，供 Envoy 增量拉取 watermark（ADR-5） |
| `updated_at` | S | GSI 分区键候选（增量拉取） |

> GSI：`updated_at-index`（或 `route_version`）支撑 ADR-5 的增量 `Query since watermark`，避免全表扫描。

**`billing_records`** — per-app per-hour 用量

| 字段 | 类型 | 说明 |
|------|------|------|
| `app_id` (PK) | S | |
| `hour_bucket` (SK) | S | `YYYY-MM-DDTHH`（UTC） |
| `cpu_vcpu_minutes` | N | Σ 分钟级 CpuUtilized 折算（avg 口径，见 F） |
| `mem_gb_minutes` | N | Σ 分钟级 MemoryUtilized 折算 |
| `task_def_family` | S | 归因来源维度 |
| `capped` | BOOL | 是否触及 $36 cap |

### 5.2 EventBridge 事件契约（ECS Task State Change → 路由注册 Lambda）

| 事件 | 触发 | Lambda 动作 |
|------|------|------------|
| Task `RUNNING` (首次) | 部署/扩容 | 提取 host_ip:host_port（DescribeTasks→networkBindings + containerInstance→EC2 私有IP）→ **主动健康检查** → 通过则写 app_routes(status=routing) |
| Task `STOPPED` | 停止/故障 | app_routes → draining → 删条目（清理） |
| Task `RUNNING` (重启/迁移新 task) | 迁移 | 更新 host_ip:host_port（值会变），subdomain 不变 |

幂等：Lambda 按 `task_arn` + `route_version` 幂等写入，容忍 EventBridge 重放/乱序。

### 5.3 控制面 REST API（7 端点，API GW HTTP API + OAuth2）

| 方法 | 路径 | 请求 | 成功响应 | 错误码 |
|------|------|------|----------|--------|
| POST | `/apps/{id}/deploy` | `{source_ref, env?}` | 202 `{build_id, status:pending}` | 400 无效源 / 409 部署中 / 404 app |
| GET | `/apps/{id}/status` | — | 200 `{status, version, url, health}` | 404 |
| POST | `/apps/{id}/rollback` | `{to_version?}` | 202 `{status}` | 400 无历史版本 / 404 |
| PUT | `/apps/{id}/config` | `{env?, cpu?, mem?}` | 200 `{updated}` | 400 校验失败 / 404 |
| DELETE | `/apps/{id}` | — | 202 `{status:deleting}` | 404 / 409 |
| GET | `/apps/{id}/logs` | `?since&limit` | 200 `{log_events[]}` | 404 |
| GET | `/apps/{id}/metrics` | `?period` | 200 `{cpu[], mem[]}` | 404 |

统一错误响应遵循项目 API 规范：`{success:false, data:null, error:"<msg>"}`；分页响应带 `meta{total,page,limit}`。所有端点 OAuth2 鉴权 + 输入校验（边界验证，fail fast）。

---

## 6. 实施顺序与依赖图

**顺序【已定】= C → E → D → B → A**；F 的**指标采集验证并入 C 的密度门禁**，F 的**聚合出账逻辑最后做**。

**依赖 DAG**：
```
        ┌──────────────────────────────────────────────┐
        │ C 运行层 (ECS/EC2/ASG/CP)                      │
        │  └─ 密度门禁 + 计费自采集 agent 完整性验证(F前移) │
        └───────┬───────────────────────┬────────────────┘
                │ 产出真实 running task   │ 产出真实指标
                ▼                         │
        ┌───────────────┐                │
        │ E 构建层       │                │
        │ (CNB→ECR)     │                │
        └───────┬───────┘                │
                │ 提供可运行镜像          │
                ▼                         │
        ┌───────────────────────────┐    │
        │ D 控制面                    │    │
        │ (API/CRUD/编排/EventBridge/ │    │
        │  readiness 状态机)          │    │
        └───────┬─────────────────────┘   │
                │ 产出真实 app_routes 记录  │
                ▼                          │
        ┌───────────────────────────┐     │
        │ B 网络层                    │     │
        │ (NLB+Envoy+同步器+健康检查) │     │
        │  用 C/E/D 真实产物端到端验证 │     │
        └───────┬─────────────────────┘    │
                │ NLB Active               │
                ▼                          │
        ┌───────────────┐                 │
        │ A 边缘层       │                 │
        │ (CF+VPC Origin│                 │
        │ +Host 转发)   │                 │
        └───────┬───────┘                 │
                │                          │
                ▼                          ▼
        ┌──────────────────────────────────────┐
        │ F 聚合出账 (最后做，依赖 C 采集已验证)  │
        └──────────────────────────────────────┘
```

**每步可验证中间态**：
| 步 | 中间态（可验证） |
|----|-----------------|
| C | 单机安全密度数字产出；idle task 能跑；计费自采集 agent 在高密度下读全量容器 stats 无遗漏 |
| E | 无 Dockerfile app 经 CNB 构建成功并被 C 拉起服务 |
| D | `POST /deploy` → task RUNNING → readiness 通过 → app_routes 写入真实 host_ip:host_port |
| B | 经 NLB→Envoy 用真实路由记录访问真实 app 200；健康态迁移生效（**非空壳**） |
| A | 固定 URL 端到端 200；Host 透传验证通过；私网可达验证通过 |
| F | 已知负载 app 账单误差达标；cap 生效 |

**关键约束（Codex 采纳）**：B 不能在 C/E/D 之前用空壳健康检查验证；其真实性依赖上游产出的真实 task + 动态端口 + 路由 + 健康态迁移。

---

## 7. Open Questions / 待验证假设

每条含"验证方法"与"若失败的影响"。

| # | 假设 / 问题 | 验证方法 | 若失败的影响 |
|---|------------|---------|-------------|
| 1 | **CPU 计费公平性（头号）**：ECS cpu units 是相对权重非硬隔离，邻居争抢下 CpuUtilized 与"按量计费"口径可能偏离 | 密度压测中固定 idle app，制造邻居争抢，观察其 CpuUtilized 波动带宽 | **动摇核心卖点**；可能需改计费口径或加 CPU 限额 |
| 2 | 单机密度 ~400/台 | C 密度门禁 100→400 逐档压测 | 密度低于预期 → 每 app 成本上升，容量规划重算 |
| 3 | 计费方法口径（avg vs max/p95） | 已在 F 定义为 avg 积分；用已知负载 app 对账 | 口径错误 → 账单系统性偏高/低 |
| 4 | 内存计费语义：MemoryUtilized 含 page cache 是否虚高 idle app | 压测测量 idle app 的 MemoryUtilized 是否远高于 RSS | idle app 账单虚高 → 需扣除 cache 分量 |
| 5 | **Envoy 轮询扩展性失效点**（ADR-5） | 模拟 10k/50k 路由，测同步器 p99 拉取延迟与 DynamoDB RCU | 超阈值 → 提前触发 xDS 演进 |
| 6 | 计费自采集 agent 高密度完整性（ADR-6） | 并入 C 门禁：数百 task 下 node agent 读 stats 无遗漏、开销可接受 | 遗漏/开销过高 → 计费不可靠，调采集并发/周期或降密度 |
| 6b | 自采集 agent 成本确实随 app 数线性且远低于 Container Insights custom metrics（ADR-6 前提） | 密度门禁阶段核算 agent 开销 + DynamoDB 写入成本 | 若仍偏高 → 重新评估计费数据源（Logs Insights 等） |
| 7 | VPC Origins 区域/AZ 支持 | A 前置检查（已核实主流区域支持，注意 AZ 例外） | 业务区域不支持 → 需换区域或改 internet-facing（安全性降级） |
| 8 | **Host header 透传**（ADR-2） | A 验证标准 #2：回显容器观察 Host | CloudFront 改写 Host → 切自定义 header 方案 |
| 9 | origin 侧是否需回源加密（ADR-4） | 与合规确认；Day-1 HTTP，评估切 HTTPS | 合规要求加密 → 引入 ACM PCA + Envoy 证书轮换 |

---

## 8. Day-1 范围边界（§10 排除项，不得纳入）

| 排除项 | 原因 |
|--------|------|
| Sleep/Wake | 客户明确不用 |
| 多 Region | Day-1 单 Region |
| 出站流量监控 | 非 Day-1 |
| 多 NLB 分片 | 单 NLB 足够，万级以上再引入 |
| VM 级隔离（awsvpc 每 task 独立 ENI） | 客户确认容器级够用；密度会骤降到 ~15/台 |
| **IP 共享污染风险** | **仅记录缓解路径，不实现**（客户确认非优先）：可选 NAT Gateway 多 IP 池轮换 + 出站行为监控自动隔离 abusive 容器。Day-1 不做 |
| xDS 推送 | Day-1 用 DynamoDB 轮询（ADR-5），~100k 规模再演进 |

---

## 9. 附录：Codex 交叉验证采纳/驳回记录

| 级别 | 项 | 处置 | 落点 |
|------|----|------|------|
| 🔴 | Lambda@Edge 不可达 internal NLB | 采纳，改 VPC Origins | ADR-1/2, §2.1 |
| 🔴 | bridge 路由语义（宿主IP:动态端口，非 Task IP:Port） | 采纳，修正 schema | §5.1 app_routes, B 服务发现 |
| 🔴 | TLS 终止自相矛盾（NLB L4 透传则 Envoy 必须终止 TLS） | 采纳，整块设计 | ADR-4, B |
| 🟠 | CPU 公平性 = 头号 OQ | 采纳，列 §7 #1 + 并入 C 压测 | §7, C |
| 🟠 | 计费归因链 taskArn→...→appId | 采纳；spec 自审进一步发现 Container Insights custom-metrics 成本击穿，最终改**节点自采集 + docker label 归因** | ADR-6, F 归因链 |
| 🔴 | **（spec 自审新增，非 Codex）** Container Insights custom-metrics 计费成本在万级击穿成本模型（80×） | 采纳，根因修复：计费数据源改自采集 | ADR-6, §2.2, F, C 门禁, §7 #6/6b |
| 🟠 | 计费方法定义（avg/max/p95） | 采纳，明确 avg 积分 | F 计费方法 |
| 🟠 | 内存计费语义（reservation/hard/Utilized） | 采纳，定义口径 | F 内存语义 |
| 🟠 | 计费验证前移并入 C 门禁 | 采纳 | C 门禁, §6 |
| 🟡 | 路由 readiness 竞态 | 采纳，最小状态机 | D 状态机 |
| 🟡 | WebSocket idle timeout + 心跳 | 采纳 | A, B 验证 #5 |
| 🟡 | B 端到端真实验证（非空壳） | 采纳 | B 验证 #4, §6 |
| 🟡 | Envoy 轮询扩展性 + 增量拉取 + 失效点 | 采纳 | ADR-5, §7 #5 |
| ⚪ | NLB/Envoy 健康检查分层是正确设计 | 驳回（非缺陷），加澄清 | B 健康检查分层 |
| ⚪ | bridge 端口天花板 | 驳回单列，归入 C 门禁 | C 密度门禁 |

**已核实 AWS 文档出处**：
- CloudFront VPC Origins（区域列表、NLB 硬约束、无 Lambda@Edge、SG/prefix-list、IGW+私有子网）：`private-content-vpc-origins.html`
- VPC Origin 协议/证书字段（`OriginProtocolPolicy: http-only|match-viewer|https-only` + `OriginSslProtocols`）：`API_VpcOriginEndpointConfig.html`
- Container Insights 指标维度（标准模式 CpuUtilized/MemoryUtilized 仅 TaskDefinitionFamily/ServiceName/ClusterName，无 per-TaskId；enhanced 模式有 per-TaskId 但更贵；MemoryUtilized 实为 MiB 且含 overhead）：`Container-Insights-metrics-ECS.html`、`Container-Insights-enhanced-observability-metrics-ECS.html`
- **Container Insights 指标按 CloudWatch 自定义指标计费**（ADR-6 成本击穿依据）：`service_utilization.html`（"Metrics collected by CloudWatch Container Insights are charged as custom metrics"）
- bridge 动态端口 + ECS 自动更新 target group/Cloud Map：`networking-networkmode-bridge.html`




