# Sample Web Application Host

[English](./README.md) | [简体中文](./README.zh-CN.md)

基于 CloudFront 的动态子域名路由系统，使用 Lambda@Edge 和 DynamoDB 根据子域名将请求路由到不同的后端服务。

## 🏗️ 架构

```text
用户请求 (api.example.com)
    ↓
CloudFront Distribution
    ↓
[Origin Request] Lambda@Edge
    └─ 在 DynamoDB 中查询子域名映射
    └─ 动态路由到目标后端
    ↓
后端服务 (Lambda URL / API Gateway / ALB)
```

### 应用承载模型 — Lambda + Lambda Web Adapter (LWA)

> **你的 Web 应用运行在 AWS Lambda 内部，由
> [Lambda Web Adapter (LWA)](https://github.com/awslabs/aws-lambda-web-adapter)
> 包裹。** 仓库自带的 Express.js 示例 (`examples/expressjs-demo/`) 是一个
> 监听 8080 端口的普通 HTTP 服务 — 没有任何 Lambda 专用 handler 代码。
> LWA 作为 Lambda 扩展加入容器，把 Lambda 的调用事件翻译成针对你的应用
> 的 HTTP 请求，因此任何标准 Web 框架（Express、FastAPI、Flask、
> Spring Boot、Gin…）都可以无需改造直接部署到 Lambda。

```text
┌────────────── Lambda 容器镜像 ──────────────┐
│                                             │
│   /opt/extensions/lambda-adapter   ← LWA    │
│              ↕  HTTP (localhost:8080)       │
│   你的 Web 应用 (Express.js / FastAPI / …)  │
│                                             │
└─────────────────────────────────────────────┘
              ↑              ↑
   Function URL (AWS_IAM)    来自 Lambda@Edge
                             路由器的 SigV4 签名请求
```

因此完整的请求路径是：

```text
CloudFront → Lambda@Edge (路由器) → Lambda Function URL → LWA → 你的应用
```

DynamoDB 中注册的每个后端都被假定为
**部署在 Lambda 上、由 LWA 包裹的 HTTP 服务**，通过 Function URL 访问。
非 Lambda 后端（API Gateway、ALB）也可工作，但 Function URL + LWA
组合是本仓库默认支持的方案。

## ✨ 特性

- ✅ **动态路由**：基于子域名路由请求，无需修改代码
- ✅ **完整 HTTP 支持**：所有 HTTP 方法 (GET、POST、PUT、DELETE 等)
- ✅ **透明路由**：浏览器地址栏 URL 保持不变
- ✅ **SSL/TLS**：自定义域名自动 HTTPS
- ✅ **查询字符串保留**：完整转发 URI 与 query string
- ✅ **DynamoDB 后端**：灵活的子域名到后端映射
- ✅ **成本优化**：viewer-request 阶段使用 CloudFront Function（成本低于 Lambda@Edge）

## 📁 项目结构

```text
.
├── README.md                            # 英文文档
├── README.zh-CN.md                      # 中文文档（本文件）
├── LICENSE                              # MIT-0 协议
├── CONTRIBUTING.md                      # 贡献指南
├── CODE_OF_CONDUCT.md                   # 行为准则
├── config.ini.example                   # 配置模板
│
├── infrastructure/                      # CDK 基础设施代码
│   ├── stack.py                        # 一体化 CDK Stack
│   ├── cdk.json                        # CDK 配置
│   ├── requirements.txt                # Python 依赖
│   │
│   └── lambda/                         # Lambda 函数
│       └── origin_request.py          # 动态路由逻辑
│
├── scripts/                            # 部署脚本
│   └── deploy_lambda.py               # Lambda 部署脚本
│
└── examples/                           # 示例应用
    └── expressjs-demo/                # Express.js 示例
```

## 🚀 部署流程 (EC2 / Amazon Linux 2023)

本流程以 **运行 Amazon Linux 2023 的 EC2 实例** 作为部署主机。在 EC2 上构建可获得原生 `linux/amd64` Docker 镜像供 Lambda 使用，同时避免在开发笔记本上管理凭证。如果你偏好 Ubuntu，请相应替换包管理命令。

### Step 0 — 准备 EC2 部署主机

1. 启动 EC2 实例：
   - **AMI**: Amazon Linux 2023 (`x86_64`)
   - **实例类型**: `t3.medium` 或更大（Docker 构建需要内存）
   - **EBS**: 20 GB gp3
   - **网络**: 任意可访问公网 HTTPS 的子网

2. 创建 **IAM 角色**（实例配置文件），授予团队批准的部署权限。仓库提供两份参考策略可直接附加：

   | 文件 | 适用场景 |
   |------|----------|
   | [`cdk-minimal-policy.json`](./cdk-minimal-policy.json) | 最小权限 CDK 部署（推荐）。资源 ARN 通过 `*WebRouterStack*` 模式限定 — 见 [Stack 命名规则](#%EF%B8%8F-stack-命名重要说明)。 |
   | [`cdk-policy-full-access.json`](./cdk-policy-full-access.json) | 适合首次探索的较宽松权限。 |
   | [`deploy-lambda-policy.json`](./deploy-lambda-policy.json) | `scripts/deploy_lambda.py` 所需的额外权限 (Lambda + ECR + IAM PassRole)。 |

   将策略附加到角色，然后在启动 EC2 时选择该角色（或事后 `aws ec2 associate-iam-instance-profile`）。部署脚本和 CDK 通过 IMDSv2 自动获取凭证 — **无需 `aws configure`，无需静态访问密钥**。

3. 通过 SSM Session Manager 连接（无需 SSH）：

   ```bash
   aws ssm start-session --target i-0123456789abcdef0 --region us-east-1
   ```

### Step 1 — 在 EC2 主机安装依赖

```bash
sudo dnf update -y

# Git、Python 3.11、构建工具
sudo dnf install -y git python3.11 python3.11-pip

# Node.js 20（CDK CLI 需要）
sudo dnf install -y nodejs20
sudo alternatives --set node /usr/bin/node-20

# AWS CDK CLI
sudo npm install -g aws-cdk

# Docker（构建 demo 镜像需要）
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
newgrp docker        # 在当前 shell 应用组成员变更

# 健康检查
aws sts get-caller-identity      # 应显示实例角色
node --version                   # v20.x
cdk --version                    # 2.x
docker version                   # client + server 都可达
```

### Step 2 — 申请 ACM 证书 (us-east-1)

Lambda@Edge 和 CloudFront alternate domain 要求 ACM 证书 **位于 us-east-1** 并使用 **DNS 验证**。

```bash
aws acm request-certificate \
  --region us-east-1 \
  --domain-name "*.your-domain.com" \
  --subject-alternative-names "your-domain.com" \
  --validation-method DNS \
  --key-algorithm RSA_2048
```

复制返回的 `CertificateArn`，获取验证用的 CNAME，并在 DNS 提供商处发布该记录：

```bash
CERT_ARN=arn:aws:acm:us-east-1:<ACCOUNT_ID>:certificate/<CERT_ID>

aws acm describe-certificate --region us-east-1 --certificate-arn "$CERT_ARN" \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord' --output table
```

等待证书状态变为 `ISSUED`（验证记录传播后通常 1–10 分钟）：

```bash
aws acm wait certificate-validated --region us-east-1 --certificate-arn "$CERT_ARN"
```

> **Cloudflare DNS** — 验证用的 CNAME 必须设为 **DNS only**（灰云）。Proxied 状态会让 AWS 验证器无法解析。

### Step 3 — 克隆并配置

```bash
git clone https://github.com/aws-samples/sample-web-application-host.git
cd sample-web-application-host

cp config.ini.example config.ini
${EDITOR:-vi} config.ini       # 填入 account_id、certificate_arn、domain_name
```

`config.ini` 必须修改的字段：

```ini
[AWS]
account_id      = 123456789012
region          = us-east-1

[CloudFront]
domain_name     = *.your-domain.com
certificate_arn = arn:aws:acm:us-east-1:123456789012:certificate/<CERT_ID>
default_origin  = example.com   # 必须是 DNS 可解析的占位主机
```

> **为什么 `default_origin = example.com`** — 这是 CloudFront 在 Lambda@Edge 路由器改写之*前*尝试连接的 fallback 主机。如果填了一个尚未在 DNS 中存在的域名（例如你刚买的新域名），CloudFront 会返回 `502 "CloudFront wasn't able to resolve the origin domain name"` 直到传播完成。请保留 `example.com`（或任何永远可解析的域名），让路由器去覆盖它。

创建 Python 虚拟环境并安装 CDK 依赖：

```bash
cd infrastructure
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 4 — Bootstrap 并部署 CDK Stack

```bash
# 在该账户/区域只需做一次
cdk bootstrap aws://<ACCOUNT_ID>/us-east-1

# 在产生费用前先 synth 干跑校验
cdk synth >/dev/null

# 部署
cdk deploy --require-approval never
```

部署大约耗时 4–5 分钟（CloudFront + Lambda@Edge 是主要耗时）。请记下输出 — 后续每一步都会用到：

| 输出 | 在哪一步使用 |
|------|--------------|
| `DistributionDomainName` (例如 `d1234abcd.cloudfront.net`) | DNS CNAME 目标 (Step 7) 与 `curl --resolve` 测试 (Step 8) |
| `DistributionId` | 映射变更后的 `cloudfront create-invalidation` |
| `DynamoDBTableName` | 子域名映射的 `put-item` 调用 |
| `EdgeFunctionArn` | 日志组定位参考 (`/aws/lambda/us-east-1.<FunctionName>`) |

> **如果 `cdk deploy` 中途中断**，可能会留下卡在 `REVIEW_IN_PROGRESS` 的 stack 和孤儿 Lambda 函数；下次部署时会报 `Lambda function ... already exists`。恢复方法：
>
> ```bash
> aws cloudformation delete-stack --stack-name ApplicationWebRouterStack --region us-east-1
> aws cloudformation wait stack-delete-complete --stack-name ApplicationWebRouterStack --region us-east-1
> aws lambda delete-function --function-name ApplicationWebRouterStack-application-web-router --region us-east-1
> ```

### Step 5 — 在同一台 EC2 上构建并推送 demo 镜像

EC2 / Amazon Linux 2023 原生就是 `x86_64`，无需 `docker buildx --platform` 等跨平台技巧。

```bash
# 创建 ECR 仓库（一次性）
aws ecr create-repository --repository-name expressjs-demo --region us-east-1

# Docker 登录 ECR
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com"

# 构建并推送
cd ~/sample-web-application-host/examples/expressjs-demo/app
docker build -f Dockerfile.lambda \
  -t "$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/expressjs-demo:x86_64" .
docker push "$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/expressjs-demo:x86_64"
```

> **从 macOS / Apple Silicon 构建？** 改用 `docker buildx build --platform linux/amd64 --provenance=false --push ...` — 没有 `--provenance=false` 时 Lambda 拉镜像会失败并报 "image manifest references unsupported manifest type"。

### Step 6 — 部署 demo Lambda 并注册子域名

```bash
cd ~/sample-web-application-host
python3 scripts/deploy_lambda.py \
  --image "$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/expressjs-demo:x86_64" \
  --name expressjs-demo
```

脚本会读取 `config.ini`，创建 Lambda 函数，开放带 `AWS_IAM` 鉴权的 Function URL，并把 `<random-name>` → Function URL 的映射写入 DynamoDB。请记下输出中的 `Subdomain` — Step 8 会通过它从 CloudFront 访问。

### Step 7 — 配置通配 DNS

将 `*.your-domain.com` 指向 Step 4 输出的 CloudFront 域名：

```text
*.your-domain.com   CNAME   d1234abcd.cloudfront.net
```

> **如果你的 DNS 在 Cloudflare**，三件事必须：
>
> - 通配 CNAME 必须设为 **DNS only**（灰云），**不能 Proxied**（橙云） — Cloudflare 代理会改写本路由依赖的 `Host` header。
> - 在 **SSL/TLS → Overview** 中，加密模式必须选 **Full** 或 **Full (strict)**。**Flexible** 会以 HTTP 回源 CloudFront 触发重定向死循环。
> - Step 2 的 ACM 验证 CNAME 也必须始终保持 **DNS only**。

### Step 8 — 端到端验证路由

DNS 传播之前可以用 `--resolve` 注入 curl 提前测试：

```bash
CF=$(dig +short d1234abcd.cloudfront.net | head -1)
SUB=expressjs-demo-xxxxxx               # 来自 Step 6 输出
DOM=your-domain.com

curl -sS --resolve "$SUB.$DOM:443:$CF" \
  -w "\nHTTP %{http_code}\n" \
  "https://$SUB.$DOM/api/items"
```

预期：`HTTP 200` + JSON 数组（含 `Lambda@Edge`、`CloudFront`、`DynamoDB` 三个 demo 项）。

DNS CNAME 传播完成后即可去掉 `--resolve`：

```bash
curl -sS "https://$SUB.$DOM/api/items"
curl -X POST "https://$SUB.$DOM/api/items/1/increment"
curl -X DELETE "https://$SUB.$DOM/api/items/2"
```

> **Lambda@Edge 传播延迟** 可达 15–30 分钟（首次部署或代码变更后）。如果首次请求返回 502 "Failed to contact the origin"，请等待并重试 — 验证 `/aws/lambda/us-east-1.<edge-function-name>` 的多区域日志组中是否出现 Edge 调用日志。

## 📖 使用示例

### 添加子域名映射

```bash
# 把 'app' 子域名映射到 Lambda Function URL
aws dynamodb put-item \
  --table-name subdomain-mapping \
  --region us-east-1 \
  --item '{
    "subdomain": {"S": "app"},
    "target_url": {"S": "https://xyz123.lambda-url.us-east-1.on.aws"}
  }'

# 把 'api' 子域名映射到 API Gateway
aws dynamodb put-item \
  --table-name subdomain-mapping \
  --region us-east-1 \
  --item '{
    "subdomain": {"S": "api"},
    "target_url": {"S": "https://api.execute-api.us-east-1.amazonaws.com"}
  }'
```

### 测试路由

```bash
# GET 请求
curl https://api.your-domain.com/users

# 带 JSON body 的 POST
curl -X POST https://api.your-domain.com/users \
  -H "Content-Type: application/json" \
  -d '{"name":"John","email":"john@example.com"}'

# 带 query 参数的请求
curl https://api.your-domain.com/users?page=2&limit=10
```

### 更新映射

```bash
aws dynamodb update-item \
  --table-name subdomain-mapping \
  --region us-east-1 \
  --key '{"subdomain": {"S": "api"}}' \
  --update-expression "SET target_url = :url" \
  --expression-attribute-values '{":url": {"S": "https://new-backend.com"}}'
```

### 删除映射

```bash
aws dynamodb delete-item \
  --table-name subdomain-mapping \
  --region us-east-1 \
  --key '{"subdomain": {"S": "api"}}'
```

## 🔍 工作原理

### 1. Viewer Request 阶段 (CloudFront Function)

- 在 CloudFront 边缘节点执行
- 把原始 `Host` header 保留到 `X-Original-Host`
- 极快、极便宜

### 2. Origin Request 阶段 (Lambda@Edge)

- 读取 `X-Original-Host` header
- 提取子域名（例如从 `api.example.com` 中得到 `api`）
- 查询 DynamoDB 获取目标后端 URL
- 动态修改请求 origin
- 保留原始 URI 与 query string

### 3. CloudFront Distribution

- 从动态 origin 拉取响应
- 把响应返回给用户
- 浏览器 URL 保持不变

## 🛠️ 配置

### 环境变量

所有配置都可以通过环境变量覆盖：

```bash
# 域名与 SSL
export APP_DOMAIN_NAME="*.example.com"
export APP_CERTIFICATE_ARN="arn:aws:acm:us-east-1:123456789012:certificate/abc-123"

# DynamoDB
export APP_DYNAMODB_TABLE="my-subdomain-mappings"
export APP_DYNAMODB_REGION="us-east-1"

# Lambda@Edge
export APP_ORIGIN_REQUEST_FUNCTION_NAME="my-router-function"
export APP_LAMBDA_MEMORY_SIZE="256"
export APP_LAMBDA_TIMEOUT_SECONDS="10"

# CloudFront Function
export APP_VIEWER_REQUEST_FUNCTION_NAME="my-viewer-function"

# CloudFront
export APP_DEFAULT_ORIGIN="default.example.com"
export APP_ORIGIN_REQUEST_POLICY_NAME="MyCustomPolicy"

cdk deploy
```

### ⚠️ Stack 命名重要说明

修改 `config.ini` 中的 stack 名称时，新名称 **必须包含 `WebRouterStack` 字符串**：

```ini
# ✅ 合法名称（最小权限策略可放行）
stack_name = ApplicationWebRouterStack
stack_name = MyWebRouterStackV2
stack_name = ProdWebRouterStack2024

# ❌ 非法名称（会触发权限不足错误）
stack_name = MyAppStack
stack_name = RouterApplication
```

**原因**：最小化 IAM 策略基于 `*WebRouterStack*` 模式做资源限制，不含此字符串的命名会被拒绝。

### CDK Context

```bash
cdk deploy -c environment=staging
```

## 📊 监控与排错

### 查看 Lambda@Edge 日志

Lambda@Edge 日志会创建在最接近函数执行位置的区域：

```bash
# 查看指定区域日志
aws logs tail /aws/lambda/us-east-1.application-web-router \
  --region us-west-2 \
  --since 30m \
  --follow

# 列出所有日志组
aws logs describe-log-groups \
  --log-group-name-prefix /aws/lambda/us-east-1.application-web-router \
  --region us-east-1
```

### CloudFront 缓存失效

```bash
aws cloudfront create-invalidation \
  --distribution-id E1234ABCD5678 \
  --paths "/*"
```

### 常见问题

| 问题 | 解决方案 |
|------|----------|
| **502 "CloudFront wasn't able to resolve the origin domain name"** | `config.ini` 中的 `default_origin` 在 DNS 中无法解析。改成 `example.com` 等真实可解析的域名；路由器会在每次请求时改写它。 |
| **部署后立即出现 502 "Failed to contact the origin"** | Lambda@Edge 还在向边缘节点传播（15–30 分钟）。先在多个区域查看日志组，再判断函数是否真的失败。 |
| **`cdk deploy` 报 "Lambda function … already exists"** | 之前的部署中断后留下了 `REVIEW_IN_PROGRESS` 的 stack 与孤儿 Lambda。运行 [Step 4](#step-4--bootstrap-并部署-cdk-stack) 末尾给出的恢复脚本。 |
| **POST/PUT 带 JSON body 时返回 403 `SignatureDoesNotMatch`** | 已知问题：`infrastructure/lambda/origin_request.py` 中的 `_add_sigv4_auth` 当前未对请求体做 base64 解码再签名。GET 请求和无 body 的 POST 不受影响。 |
| 403 Forbidden | 检查 CloudFront 允许的 HTTP 方法配置 |
| 404 Not Found | 检查 DynamoDB 中是否存在对应 `subdomain` 分区键，且通配 CNAME 已指向 CloudFront |
| 映射更新后路由仍旧 | `aws cloudfront create-invalidation --distribution-id <ID> --paths "/*"` |

## 🔒 安全最佳实践

- ✅ 仅允许 HTTPS（默认强制）
- ✅ 最低 TLS 1.2 协议
- ✅ IAM 角色遵循最小权限原则
- ✅ DynamoDB 默认静态加密
- ✅ CloudFront 访问日志（可选，在 stack 中配置）

## 💰 费用考量

- **CloudFront**：按请求与流量计费
- **Lambda@Edge**：按请求与执行时间计费
- **CloudFront Function**：约 Lambda@Edge 1/6 的成本
- **DynamoDB**：按读取量计费（按需价格）

每月 100 万请求估算成本：约 1–5 美元

## 🧪 示例应用参考

仓库在 `examples/expressjs-demo/` 下提供一个 Express.js 示例，演示
[应用承载模型](#应用承载模型--lambda--lambda-web-adapter-lwa) 中描述的
**Lambda + LWA 承载模型**：一个原生 Express HTTP 服务运行在 Lambda 容器
镜像里，由 Lambda Web Adapter 包裹，使其讲普通 HTTP 而不是 Lambda 调用
事件格式。完整的构建、推送、部署、验证流程在
[部署流程的 Step 5–8](#-部署流程-ec2--amazon-linux-2023) 中已覆盖。

`scripts/deploy_lambda.py` 是部署驱动脚本：

| 参数 | 是否必填 | 默认值 |
|------|----------|--------|
| `--image <ECR_URI>` | 是 | — |
| `--name <prefix>`   | 否 | 取自 `config.ini` 的 `LambdaTest.function_prefix` |

脚本会：

1. 确保 Lambda 执行角色存在（不存在则创建 `lambda-execution-role`）。
2. 用指定容器镜像创建 Lambda 函数。
3. 创建带 `AWS_IAM` 鉴权的 Function URL。
4. 在 DynamoDB 中注册 `subdomain → target_url` 映射。
5. 输出经路由的访问 URL（`https://<function-name>.<your-domain>/`）。

> **LWA 已知限制** — Lambda Web Adapter 对平台敏感：构建必须以 `linux/amd64` 为目标（在 EC2 / AL2023 上是原生，在 macOS 上需要 `docker buildx --platform linux/amd64 --provenance=false`），并且需要可写文件系统的应用（如 nginx 写 `/run/nginx.pid`）无法在 Lambda 上运行。如果 LWA 与你的技术栈不兼容，改用 `@vendia/serverless-express` + `public.ecr.aws/lambda/nodejs:18` 基础镜像。

## 🚧 限制

- Lambda@Edge 必须部署在 `us-east-1`
- Lambda@Edge 不支持环境变量（CDK stack 在构建时通过字符串替换注入表名和区域）
- Origin-request 超时时间最长 30 秒
- Lambda@Edge 代码大小：1 MB 压缩 / 50 MB 解压
- 部署后传播时间：15–30 分钟
- **POST/PUT 带 body 路由到 Lambda Function URL 后端可能返回 `403 SignatureDoesNotMatch`**，因为 `_add_sigv4_auth` 当前未对 CloudFront 请求体做 base64 解码再签名。GET 请求、仅含 query string 的 POST、以及非 Lambda Function URL 后端不受影响。

## 🗑️ 清理

```bash
# 1. 删除 demo Lambda（由 scripts/deploy_lambda.py 创建）
aws lambda delete-function --function-name <expressjs-demo-xxxxxx> --region us-east-1
aws ecr delete-repository --repository-name expressjs-demo --force --region us-east-1

# 2. 销毁 CDK stack
cd infrastructure && source .venv/bin/activate
cdk destroy

# 3.（可选）删除 ACM 证书
aws acm delete-certificate --region us-east-1 --certificate-arn <CERT_ARN>
```

> **Lambda@Edge 删除耗时** 比一般 Lambda 长得多 — CloudFront 必须将所有边缘节点的副本移除，可能需要 **数小时**。如果 `cdk destroy` 报告函数仍被引用，请等待后再试，不要强行删除。

## 📚 扩展资料

- [AWS Lambda@Edge 文档](https://docs.aws.amazon.com/lambda/latest/dg/lambda-edge.html)
- [CloudFront Functions vs Lambda@Edge](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/edge-functions.html)
- [CDK 最佳实践](https://docs.aws.amazon.com/cdk/latest/guide/best-practices.html)

## 🔐 安全

如需报告安全问题，请参见 [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications)。

## 👥 贡献

欢迎贡献！请参阅 [CONTRIBUTING.md](CONTRIBUTING.md) 中的指南，并遵守 [行为准则](CODE_OF_CONDUCT.md)。

## 📝 许可证

本库基于 MIT-0 协议开源。详见 [LICENSE](LICENSE) 文件。

---

**Built with AWS CDK**
