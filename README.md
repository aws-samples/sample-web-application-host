# Sample Web Application Host

[English](./README.md) | [简体中文](./README.zh-CN.md)

A CloudFront-based dynamic subdomain routing system that uses Lambda@Edge and DynamoDB to route requests to different backend services based on subdomain.

## 🏗️ Architecture

```text
User Request (api.example.com)
    ↓
CloudFront Distribution
    ↓
[Origin Request] Lambda@Edge
    └─ Query DynamoDB for subdomain mapping
    └─ Dynamically route to target backend
    ↓
Backend Service (Lambda URL / API Gateway / ALB)
```

### Application Hosting Model — Lambda + Lambda Web Adapter (LWA)

> **Your web application runs inside AWS Lambda, wrapped by the
> [Lambda Web Adapter (LWA)](https://github.com/awslabs/aws-lambda-web-adapter).**
> The shipped Express.js demo (`examples/expressjs-demo/`) is a normal
> HTTP server listening on port 8080 — no Lambda-specific handler code.
> LWA is added as a Lambda layer/extension that translates Lambda's
> invocation events into ordinary HTTP requests against your app, so
> any standard web framework (Express, FastAPI, Flask, Spring Boot,
> Gin, …) can be deployed without rewriting it for Lambda.

```text
┌────────────── Lambda container image ──────────────┐
│                                                    │
│   /opt/extensions/lambda-adapter   ← LWA           │
│              ↕  HTTP (localhost:8080)              │
│   your web app (Express.js / FastAPI / …)          │
│                                                    │
└────────────────────────────────────────────────────┘
              ↑              ↑
   Function URL (AWS_IAM)    SigV4-signed request
                             from Lambda@Edge router
```

This means the routing path becomes:

```text
CloudFront → Lambda@Edge (router) → Lambda Function URL → LWA → your app
```

Every backend you register in DynamoDB is expected to be a
**Lambda-hosted, LWA-wrapped HTTP service** addressed by its Function
URL. Non-Lambda backends (API Gateway, ALB) also work, but the
Function URL + LWA combination is the supported default in this
repository.

## ✨ Features

- ✅ **Dynamic Routing**: Route requests based on subdomain without code changes
- ✅ **Full HTTP Support**: All HTTP methods (GET, POST, PUT, DELETE, etc.)
- ✅ **Transparent Routing**: URL in browser remains unchanged
- ✅ **SSL/TLS**: Automatic HTTPS with custom domain
- ✅ **Query String Preservation**: Complete URI and query string forwarding
- ✅ **DynamoDB Backend**: Flexible subdomain-to-backend mappings
- ✅ **Cost Optimized**: CloudFront Function for viewer-request (cheaper than Lambda@Edge)

## 📁 Project Structure

```
.
├── README.md
├── LICENSE                              # MIT-0 license
├── CONTRIBUTING.md                      # Contribution guidelines
├── CODE_OF_CONDUCT.md                   # Code of conduct
├── config.ini.example                   # Configuration template
│
├── infrastructure/                      # CDK Infrastructure as Code
│   ├── stack.py                        # All-in-one CDK stack
│   ├── cdk.json                        # CDK configuration
│   ├── requirements.txt                # Python dependencies
│   │
│   └── lambda/                         # Lambda functions
│       └── origin_request.py          # Dynamic routing logic
│
├── scripts/                            # Deployment scripts
│   └── deploy_lambda.py               # Lambda deployment script
│
└── examples/                           # Example applications
    └── expressjs-demo/                # Express.js demo app
```

## 🚀 Deployment Walkthrough (EC2 / Amazon Linux 2023)

This walkthrough uses an **EC2 instance running Amazon Linux 2023** as the
deployment host. Building on EC2 gives you native `linux/amd64` Docker images
for Lambda and avoids credential management on a developer laptop. Adapt the
package-manager commands if you prefer Ubuntu.

### Step 0 — Provision the EC2 deployment host

1. Launch an EC2 instance:
   - **AMI**: Amazon Linux 2023 (`x86_64`)
   - **Instance type**: `t3.medium` or larger (Docker build needs RAM)
   - **EBS**: 20 GB gp3
   - **Network**: any public subnet with outbound HTTPS

2. Create an **IAM role** (instance profile) with the permissions your team
   approves for deployment. The repository ships two reference policies you
   can attach:

   | File | Use case |
   |------|----------|
   | [`cdk-minimal-policy.json`](./cdk-minimal-policy.json) | Least-privilege CDK deploy (recommended). Resource ARNs are scoped by the `*WebRouterStack*` pattern — see [Stack Naming Notice](#%EF%B8%8F-stack-naming-important-notice). |
   | [`cdk-policy-full-access.json`](./cdk-policy-full-access.json) | Broader access for first-time exploration only. |
   | [`deploy-lambda-policy.json`](./deploy-lambda-policy.json) | Extra permissions needed by `scripts/deploy_lambda.py` (Lambda + ECR + IAM PassRole). |

   Attach the policies to the role and select the role at launch (or
   `aws ec2 associate-iam-instance-profile` afterwards). The deployment
   scripts and CDK pick up credentials automatically via IMDSv2 — **no
   `aws configure` and no static access keys required**.

3. Connect via SSM Session Manager (no SSH needed):

   ```bash
   aws ssm start-session --target i-0123456789abcdef0 --region us-east-1
   ```

### Step 1 — Install prerequisites on the EC2 host

```bash
sudo dnf update -y

# Git, Python 3.11, build tools
sudo dnf install -y git python3.11 python3.11-pip

# Node.js 20 (for CDK CLI)
sudo dnf install -y nodejs20
sudo alternatives --set node /usr/bin/node-20

# AWS CDK CLI
sudo npm install -g aws-cdk

# Docker (for the demo backend image)
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
newgrp docker        # apply group membership in current shell

# Sanity check
aws sts get-caller-identity      # should show the instance role
node --version                   # v20.x
cdk --version                    # 2.x
docker version                   # client + server both reachable
```

### Step 2 — Issue an ACM certificate (us-east-1)

Lambda@Edge and the CloudFront alternate domain require an ACM certificate
**in us-east-1** with **DNS validation**.

```bash
aws acm request-certificate \
  --region us-east-1 \
  --domain-name "*.your-domain.com" \
  --subject-alternative-names "your-domain.com" \
  --validation-method DNS \
  --key-algorithm RSA_2048
```

Copy the returned `CertificateArn`, then fetch the validation CNAME and
publish it at your DNS provider:

```bash
CERT_ARN=arn:aws:acm:us-east-1:<ACCOUNT_ID>:certificate/<CERT_ID>

aws acm describe-certificate --region us-east-1 --certificate-arn "$CERT_ARN" \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord' --output table
```

Wait until the certificate becomes `ISSUED` (typically 1–10 minutes after
the validation record propagates):

```bash
aws acm wait certificate-validated --region us-east-1 --certificate-arn "$CERT_ARN"
```

> **Cloudflare DNS** — set the validation CNAME to **DNS only** (grey cloud).
> Proxied records cannot be reached by AWS validators.

### Step 3 — Clone and configure

```bash
git clone https://github.com/aws-samples/sample-web-application-host.git
cd sample-web-application-host

cp config.ini.example config.ini
${EDITOR:-vi} config.ini       # fill in account_id, certificate_arn, domain_name
```

Required edits in `config.ini`:

```ini
[AWS]
account_id      = 123456789012
region          = us-east-1

[CloudFront]
domain_name     = *.your-domain.com
certificate_arn = arn:aws:acm:us-east-1:123456789012:certificate/<CERT_ID>
default_origin  = example.com   # MUST be a DNS-resolvable placeholder host
```

> **Why `default_origin = example.com`** — this is the fallback host CloudFront
> dials *before* the Lambda@Edge router rewrites it. If you point it at a
> domain that doesn't resolve in DNS yet (e.g. your own brand new domain),
> CloudFront returns `502 "CloudFront wasn't able to resolve the origin
> domain name"` until propagation completes. Keep `example.com` (or any
> always-resolvable domain) and trust the router to override it.

Create a Python virtual environment and install CDK dependencies:

```bash
cd infrastructure
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 4 — Bootstrap and deploy the CDK stack

```bash
# First time only in this account/region
cdk bootstrap aws://<ACCOUNT_ID>/us-east-1

# Synth as a dry-run check before paying for resources
cdk synth >/dev/null

# Deploy
cdk deploy --require-approval never
```

The deploy takes roughly 4–5 minutes (CloudFront + Lambda@Edge dominate).
Capture the outputs — every later step uses one of them:

| Output | Used in |
|--------|---------|
| `DistributionDomainName` (e.g. `d1234abcd.cloudfront.net`) | DNS CNAME target (Step 7) and `curl --resolve` testing (Step 8) |
| `DistributionId` | `cloudfront create-invalidation` after mapping changes |
| `DynamoDBTableName` | Subdomain mapping `put-item` calls |
| `EdgeFunctionArn` | Reference for log-group lookups (`/aws/lambda/us-east-1.<FunctionName>`) |

> **If `cdk deploy` aborts mid-way**, you may end up with a stuck
> `REVIEW_IN_PROGRESS` stack and orphaned Lambda functions; subsequent
> deploys then fail with `Lambda function ... already exists`. Recover with:
>
> ```bash
> aws cloudformation delete-stack --stack-name ApplicationWebRouterStack --region us-east-1
> aws cloudformation wait stack-delete-complete --stack-name ApplicationWebRouterStack --region us-east-1
> aws lambda delete-function --function-name ApplicationWebRouterStack-application-web-router --region us-east-1
> ```

### Step 5 — Build and push the demo backend image (on the same EC2)

EC2 / Amazon Linux 2023 is `x86_64` natively, so we don't need
`docker buildx --platform` tricks here.

```bash
# Provision an ECR repo (one-time)
aws ecr create-repository --repository-name expressjs-demo --region us-east-1

# Authenticate Docker to ECR
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com"

# Build and push
cd ~/sample-web-application-host/examples/expressjs-demo/app
docker build -f Dockerfile.lambda \
  -t "$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/expressjs-demo:x86_64" .
docker push "$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/expressjs-demo:x86_64"
```

> **Building from macOS / Apple Silicon instead?** Use
> `docker buildx build --platform linux/amd64 --provenance=false --push ...`
> — without `--provenance=false`, Lambda fails to pull the image with
> "image manifest references unsupported manifest type".

### Step 6 — Deploy the demo Lambda + register the subdomain

```bash
cd ~/sample-web-application-host
python3 scripts/deploy_lambda.py \
  --image "$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/expressjs-demo:x86_64" \
  --name expressjs-demo
```

The script reads `config.ini`, creates the Lambda function, exposes a
Function URL with `AWS_IAM` auth, and writes the `<random-name>` →
Function URL mapping into DynamoDB. Note the `Subdomain` value in the
output — you'll route to it through CloudFront in Step 8.

### Step 7 — Configure your wildcard DNS

Point `*.your-domain.com` to the CloudFront distribution domain from Step 4:

```text
*.your-domain.com   CNAME   d1234abcd.cloudfront.net
```

> **If your DNS is on Cloudflare**, three things are required:
>
> - Wildcard CNAME proxy status must be **DNS only** (grey cloud), **not Proxied** (orange cloud) — Cloudflare's proxy rewrites the `Host` header that this router depends on.
> - Under **SSL/TLS → Overview**, set the encryption mode to **Full** or **Full (strict)**. **Flexible** sends HTTP to CloudFront and triggers a redirect loop.
> - The ACM DNS-validation CNAME from Step 2 also stays **DNS only** forever.

### Step 8 — Verify routing end-to-end

You can test before DNS propagates by injecting `--resolve` into curl:

```bash
CF=$(dig +short d1234abcd.cloudfront.net | head -1)
SUB=expressjs-demo-xxxxxx               # from Step 6 output
DOM=your-domain.com

curl -sS --resolve "$SUB.$DOM:443:$CF" \
  -w "\nHTTP %{http_code}\n" \
  "https://$SUB.$DOM/api/items"
```

Expected: `HTTP 200` plus a JSON list of demo items (`Lambda@Edge`,
`CloudFront`, `DynamoDB`).

After your DNS CNAME propagates you can drop `--resolve`:

```bash
curl -sS "https://$SUB.$DOM/api/items"
curl -X POST "https://$SUB.$DOM/api/items/1/increment"
curl -X DELETE "https://$SUB.$DOM/api/items/2"
```

> **Lambda@Edge propagation** can take 15–30 minutes after the first deploy
> or any code change. If the very first request returns 502 with
> "Failed to contact the origin", wait and retry — verify Edge logs are
> appearing in `/aws/lambda/us-east-1.<edge-function-name>` log groups
> across multiple regions.

## 📖 Usage Examples

### Add Subdomain Mapping

```bash
# Map 'app' subdomain to Lambda Function URL
aws dynamodb put-item \
  --table-name subdomain-mapping \
  --region us-east-1 \
  --item '{
    "subdomain": {"S": "app"},
    "target_url": {"S": "https://xyz123.lambda-url.us-east-1.on.aws"}
  }'

# Map 'api' subdomain to API Gateway
aws dynamodb put-item \
  --table-name subdomain-mapping \
  --region us-east-1 \
  --item '{
    "subdomain": {"S": "api"},
    "target_url": {"S": "https://api.execute-api.us-east-1.amazonaws.com"}
  }'
```

### Test Routing

```bash
# GET request
curl https://api.your-domain.com/users

# POST request with JSON body
curl -X POST https://api.your-domain.com/users \
  -H "Content-Type: application/json" \
  -d '{"name":"John","email":"john@example.com"}'

# Request with query parameters
curl https://api.your-domain.com/users?page=2&limit=10
```

### Update Mapping

```bash
aws dynamodb update-item \
  --table-name subdomain-mapping \
  --region us-east-1 \
  --key '{"subdomain": {"S": "api"}}' \
  --update-expression "SET target_url = :url" \
  --expression-attribute-values '{":url": {"S": "https://new-backend.com"}}'
```

### Delete Mapping

```bash
aws dynamodb delete-item \
  --table-name subdomain-mapping \
  --region us-east-1 \
  --key '{"subdomain": {"S": "api"}}'
```

## 🔍 How It Works

### 1. Viewer Request Stage (CloudFront Function)
- Executes at CloudFront edge locations
- Preserves original `Host` header as `X-Original-Host`
- Extremely fast and cost-effective

### 2. Origin Request Stage (Lambda@Edge)
- Reads `X-Original-Host` header
- Extracts subdomain (e.g., "api" from "api.example.com")
- Queries DynamoDB for target backend URL
- Dynamically modifies request origin
- Preserves original URI and query strings

### 3. CloudFront Distribution
- Fetches content from dynamic origin
- Returns response to user
- Browser URL remains unchanged

## 🛠️ Configuration

### Environment Variables

All configuration can be overridden using environment variables:

```bash
# Domain and SSL
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

### ⚠️ Stack Naming Important Notice

When modifying the stack name in `config.ini`, ensure the new name contains **"WebRouterStack"** string:

```ini
# ✅ Valid names (will work with minimal permissions)
stack_name = ApplicationWebRouterStack
stack_name = MyWebRouterStackV2
stack_name = ProdWebRouterStack2024

# ❌ Invalid names (will cause permission errors)
stack_name = MyAppStack
stack_name = RouterApplication
```

**Reason**: The minimal IAM policy uses `*WebRouterStack*` pattern matching for security. Names not containing this string will be denied access.

### CDK Context

```bash
cdk deploy -c environment=staging
```

## 📊 Monitoring and Troubleshooting

### View Lambda@Edge Logs

Lambda@Edge logs are created in the region closest to where the function executed:

```bash
# Check logs in specific region
aws logs tail /aws/lambda/us-east-1.application-web-router \
  --region us-west-2 \
  --since 30m \
  --follow

# Find all log groups
aws logs describe-log-groups \
  --log-group-name-prefix /aws/lambda/us-east-1.application-web-router \
  --region us-east-1
```

### CloudFront Cache Invalidation

```bash
aws cloudfront create-invalidation \
  --distribution-id E1234ABCD5678 \
  --paths "/*"
```

### Common Issues

| Issue | Solution |
|-------|----------|
| **502 "CloudFront wasn't able to resolve the origin domain name"** | Your `default_origin` in `config.ini` doesn't resolve in DNS. Set it to a real domain such as `example.com`; the router rewrites it on every request. |
| **502 "Failed to contact the origin"** right after deploy | Lambda@Edge is still propagating to edge locations (15–30 minutes). Check log groups in multiple regions before assuming the function is broken. |
| **`cdk deploy` reports "Lambda function … already exists"** | A previous deploy aborted and left a `REVIEW_IN_PROGRESS` stack plus orphan Lambda. Run the stack delete + lambda delete sequence shown in [Step 4](#step-4--bootstrap-and-deploy-the-cdk-stack). |
| **403 `SignatureDoesNotMatch` on POST/PUT with JSON body** | Known limitation: `_add_sigv4_auth` in `infrastructure/lambda/origin_request.py` does not yet base64-decode the request body before signing. Tracked as a known bug; GET requests and POSTs without a body work today. |
| 403 Forbidden | Check CloudFront allowed methods configuration |
| 404 Not Found | Verify the `subdomain` partition key exists in DynamoDB and the wildcard CNAME points at CloudFront |
| Stale routing after a mapping update | `aws cloudfront create-invalidation --distribution-id <ID> --paths "/*"` |

## 🔒 Security Best Practices

- ✅ Use HTTPS only (enforced by default)
- ✅ Minimum TLS 1.2 protocol
- ✅ IAM roles with least privilege
- ✅ DynamoDB encryption at rest (default)
- ✅ CloudFront access logs (optional, configure in stack)

## 💰 Cost Considerations

- **CloudFront**: Pay per request and data transfer
- **Lambda@Edge**: Pay per request and execution time
- **CloudFront Function**: ~1/6th the cost of Lambda@Edge
- **DynamoDB**: Pay per read (on-demand pricing)

Estimated cost for 1M requests/month: ~$1-5 USD

## 🧪 Demo Application Reference

The repository ships an Express.js example under `examples/expressjs-demo/`
that demonstrates the **Lambda + LWA hosting model** described in
[Application Hosting Model](#application-hosting-model--lambda--lambda-web-adapter-lwa):
a vanilla Express HTTP server running inside a Lambda container image,
wrapped by the Lambda Web Adapter so it speaks plain HTTP rather than
Lambda's invocation event format. The end-to-end build, push, deploy
and verify flow is covered in
[Steps 5–8 of the deployment walkthrough](#-deployment-walkthrough-ec2--amazon-linux-2023).

`scripts/deploy_lambda.py` is the deployment driver:

| Flag | Required | Default |
|------|----------|---------|
| `--image <ECR_URI>` | yes | — |
| `--name <prefix>`   | no  | `LambdaTest.function_prefix` from `config.ini` |

The script:

1. Ensures the Lambda execution role exists (creates `lambda-execution-role` if missing).
2. Creates a Lambda function from the supplied container image.
3. Creates a Function URL with `AWS_IAM` authentication.
4. Registers a `subdomain → target_url` mapping in DynamoDB.
5. Prints the routed access URL (`https://<function-name>.<your-domain>/`).

> **Known LWA caveats** — the Lambda Web Adapter is platform-sensitive:
> builds must target `linux/amd64` (native on EC2 / AL2023, opt-in via
> `docker buildx --platform linux/amd64 --provenance=false` on macOS), and
> apps that need a writable filesystem (e.g. nginx writing
> `/run/nginx.pid`) won't work. Switch to `@vendia/serverless-express`
> with `public.ecr.aws/lambda/nodejs:18` if LWA is incompatible with your
> stack.

## 🚧 Limitations

- Lambda@Edge must be deployed in `us-east-1`
- Lambda@Edge doesn't support environment variables (the CDK stack injects
  the table name and region at build time via string substitution)
- Origin-request timeout: 30 seconds max
- Lambda@Edge code size: 1 MB compressed / 50 MB uncompressed
- Propagation time: 15–30 minutes for Lambda@Edge updates after deploy
- **POST/PUT requests with a body that target a Lambda Function URL backend
  may fail with `403 SignatureDoesNotMatch`** because `_add_sigv4_auth`
  does not yet base64-decode the CloudFront request body before signing.
  GET requests, querystring-only POSTs, and non-Lambda-URL backends are
  unaffected.

## 🗑️ Cleanup

```bash
# 1. Remove the demo Lambda (created by scripts/deploy_lambda.py)
aws lambda delete-function --function-name <expressjs-demo-xxxxxx> --region us-east-1
aws ecr delete-repository --repository-name expressjs-demo --force --region us-east-1

# 2. Tear down the CDK stack
cd infrastructure && source .venv/bin/activate
cdk destroy

# 3. (Optional) Delete the ACM certificate
aws acm delete-certificate --region us-east-1 --certificate-arn <CERT_ARN>
```

> **Lambda@Edge takes longer to delete** than other Lambda functions —
> CloudFront has to remove the replicated copies from every edge location,
> which can take **hours**. If `cdk destroy` reports the function as still
> in use, wait and retry rather than force-deleting.

## 📚 Additional Resources

- [AWS Lambda@Edge Documentation](https://docs.aws.amazon.com/lambda/latest/dg/lambda-edge.html)
- [CloudFront Functions vs Lambda@Edge](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/edge-functions.html)
- [CDK Best Practices](https://docs.aws.amazon.com/cdk/latest/guide/best-practices.html)

## 🔐 Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information on reporting security issues.

## 👥 Contributing

Contributions welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines, and abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## 📝 License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

---

**Built with AWS CDK**
