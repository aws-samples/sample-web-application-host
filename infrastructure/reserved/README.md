# Reserved Mode — AWS 迁移实现

将 website-builder 的 **Reserved 托管模式**（24/7 常驻容器化 Web App，一租户一容器，按 CPU/内存用量计费）从 Railway 迁移到 AWS 的实现。本目录是**可部署的 CDK 工程 + 测试/运维工具**。

设计依据：`docs/superpowers/specs/2026-07-06-reserved-mode-migration-design.md`（spec，含 A–F 子系统与 6 个 ADR）。

---

## 架构（生产链路）

```
用户浏览器  https://<subdomain>.webhost.jaydencrazy.win
      │  viewer TLS 在 CloudFront 终止
      ▼
CloudFront (CDN)                         公网唯一入口
  • VPC Origin → 私网直连 internal NLB
  • Origin Request Policy 转发 Host header（取代 Lambda@Edge，ADR-2）
      │
      ▼
internal NLB (L4/TCP, 私有子网)            无公网地址
      │
      ▼
Envoy (L7 代理, ECS Service)              按 Host header 路由到租户
  • route-sync sidecar 轮询 DynamoDB app_routes 生成动态配置（ADR-5）
  • 未注册 Host → 404；主动健康检查；WebSocket/流式透传
      │  bridge 动态端口
      ▼
租户容器 (ECS on EC2, bridge, m6g.4xlarge Graviton)
```

**关键设计点**（均经真实环境验证）：
- **单一自洽 CDK 栈** `ReservedProdStack`：VPC / ECS 集群 / EC2 ASG / 所有 SG / NLB / Envoy / CloudFront / DynamoDB / 租户任务定义全部 CDK 创建，栈内对象引用打通 SG，**无 config 资源 ID、无部署后手动 CLI**。
- **一租户一容器**，路由记录语义 `subdomain → 宿主EC2私有IP : 动态hostPort`（bridge 模式，**非 Task IP**）。
- **CPU 无放置预留**（container `cpu=0`）：密度只受 `memoryReservation` 约束，实测 **≥400 task / m6g.4xlarge**。
- **CloudFront VPC Origins**（非 Lambda@Edge 直连）：解决"边缘无法访问 internal NLB"的物理不可达问题（ADR-1）。

---

## 目录结构

```
infrastructure/reserved/
├── app.py                    CDK 入口（部署 ReservedProdStack）
├── cdk.json                  CDK 配置
├── config_loader.py          读 config.ini（复用 Autoscale 的 ConfigLoader 模式）
├── stacks/
│   └── prod_stack.py         ★ 单一自洽栈：全链路所有资源
├── envoy/                    子系统 B 数据面（Envoy）
│   ├── bootstrap.yaml        Envoy 配置：node / 静态 listener / 文件系统 xDS / WebSocket
│   ├── route_sync.py         DynamoDB app_routes → Envoy 动态配置同步 sidecar
│   ├── Dockerfile.envoy      arm64 Envoy 镜像（wait-and-run 等待 seed 文件）
│   └── Dockerfile.sync       arm64 route-sync 镜像（boto3 预装）
├── scripts/                  运维 / 测试工具
│   ├── density_test.py       子系统 C 密度压测（逐档 100→400，检测天花板）
│   ├── register_tenants.py   起租户容器 + 提取宿主IP:端口 + 写 app_routes（D 层编排的测试版）
│   ├── run_mt_tests.py       多租户路由测试（MT-1/2/4，curl）
│   ├── test_passthrough.py   复杂请求透传 20 项（header/cookie/query/method/body/8KB/深路径）
│   └── test_streaming.sh     流式 SSE 透传测试（验证全链路不缓冲）
├── tests/
│   └── multi-tenant-test-plan.md   完整测试计划（MT-1~9 + Railway 对标 RW-1~9）
├── IMPLEMENTATION-NOTES.md   真实部署踩坑与修复全记录（部署前必读）
└── README.md                 本文件
```

---

## 前置要求

- **aws-cdk-lib ≥ 2.170**（CloudFront VPC Origins L2 API；本工程用 2.261）
- **CDK CLI ≥ 2.1129**（cloud-assembly schema v54）：`npx -y aws-cdk@2.1129.0`
- Docker（buildx，构建 arm64 Graviton 镜像）
- `config.ini`（从 `config.ini.example` 复制，gitignored）——需 `[AWS]`、`[CloudFront]`（域名 + us-east-1 ACM 证书）、`[Reserved]` 段
- ACM 证书须在 **us-east-1**、覆盖 `*.<domain>`、状态 ISSUED

---

## 部署

```bash
cd infrastructure/reserved

# 1. 构建并推送 Envoy + route-sync 的 arm64 镜像到 ECR（首次/镜像变更时）
#    (ECR repo: reserved-envoy, reserved-route-sync)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <acct>.dkr.ecr.us-east-1.amazonaws.com
docker buildx build --platform linux/arm64 -f envoy/Dockerfile.envoy -t <acct>.dkr.ecr.us-east-1.amazonaws.com/reserved-envoy:latest --push envoy/
docker buildx build --platform linux/arm64 -f envoy/Dockerfile.sync  -t <acct>.dkr.ecr.us-east-1.amazonaws.com/reserved-route-sync:latest --push envoy/

# 2. 部署单栈（CloudFront 传播 ~10-15 min）
npx -y aws-cdk@2.1129.0 deploy ReservedProdStack --require-approval never

# 3. 部署输出的 CloudFrontDomain → 在 DNS 处把 *.<domain> 配 CNAME 指向它（DNS-only，不走代理）
```

**DNS 注意**：`*.<domain>` 的 CNAME 目标 = CloudFront 域名，Proxy 必须关闭（Cloudflare 灰云）——否则双重 CDN 会破坏 Host 头路由与证书校验。

---

## 测试

```bash
# 注册 N 个可区分租户（起 nginx 容器 + 写路由）
python scripts/register_tenants.py --cluster reserved-mode-cluster \
    --task-def reserved-tenant-app --table reserved-app-routes --count 10

# 多租户路由：MT-1 正确性 / MT-2 隔离(并发0串扰) / MT-4 未注册404
python scripts/run_mt_tests.py --domain webhost.jaydencrazy.win --tenants 10 --concurrency 50

# 复杂请求透传 20 项（需 echo 租户，见 IMPLEMENTATION-NOTES）
python scripts/test_passthrough.py --host echo.webhost.jaydencrazy.win

# 流式 SSE（需 stream 租户）
bash scripts/test_streaming.sh stream.webhost.jaydencrazy.win

# 子系统 C 密度压测（单机安全密度）
python scripts/density_test.py --cluster reserved-mode-cluster --task-def <idle-family> --steps 100,200,300,400
```

> 测试脚本用 **curl**（非 Python urllib）：某些 pyenv 环境 hashlib 缺 blake2 会破坏 urllib 的 HTTPS。

### 已验证结果（真域名 HTTPS 生产链路）
| 测试 | 结果 |
|------|------|
| 多租户路由 MT-1/2/4（10 租户 + 500 并发） | ✅ 全 PASS，0 串扰 |
| 复杂请求透传（Host/query/header/cookie/6 方法/body/8KB/深路径） | ✅ 20/20 |
| 流式 SSE（10 chunk 逐秒实时，全链路不缓冲） | ✅ PASS |
| 单机密度 | ✅ ≥400 task / m6g.4xlarge |

---

## 数据契约（DynamoDB `reserved-app-routes`）

Envoy 的路由数据源，spec §5.1：

| 字段 | 说明 |
|------|------|
| `subdomain` (PK) | 租户 subdomain，Envoy 按 Host 匹配键 |
| `host_ip` | **宿主 EC2 私有 IP**（bridge 模式，非容器 IP） |
| `host_port` | **动态 hostPort**（Docker 随机分配） |
| `status` | `routing` 才被 Envoy 服务（readiness gate） |
| `app_id` / `task_arn` / `gsi_bucket` / `updated_at` | 反查 / 增量拉取 watermark |

---

## 部署陷阱（详见 IMPLEMENTATION-NOTES.md）

真实部署中踩过并已修复的坑，按代价排序：
1. **ASG managed scaling 默认开** → 集群卡 0 台 → `enable_managed_scaling=False`
2. **CloudFront VPC Origin 被回滚卡死**（既不能关联也不能删）→ 移除 circuit breaker + 显式 `distribution → nlb_listener` 依赖
3. **NLB SG 不自动放行 CloudFront** → 加 CloudFront origin-facing 托管 prefix-list 入站规则（否则回源 HTTP 000）
4. **CDK 版本过旧** → 升 2.261 / CLI 2.1129
5. **Envoy 启动坑**：`node.id/cluster` 必填；共享 volume 遮盖 seed 文件（route-sync 启动即 seed + Envoy wait-and-run）
6. **bridge 高密度端口冲突** → 关 Docker userland-proxy
7. **孤儿 CloudFront distribution 占用域名别名** → 部署失败后需清理残留

---

## 尚未实现 / Day-1 排除（spec §8）

Sleep/Wake、多 Region、出站流量监控、多 NLB 分片、VM 级隔离；计费聚合出账逻辑（数据源已定为节点自采集，ADR-6，避免 Container Insights custom-metrics 成本击穿）。
