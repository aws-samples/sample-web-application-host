#!/usr/bin/env python3
"""
CDK app entrypoint for Reserved Mode — single self-contained stack.

ReservedProdStack builds the full production link, all CDK-created:
  CloudFront (VPC Origin + Host-forwarding Origin Request Policy, no Lambda@Edge)
    → internal NLB (L4/TCP) → Envoy (L7 host routing) → tenant containers (ECS/EC2).

Deploy:
    cdk deploy --app "python3 app.py" ReservedProdStack

Merged back from the earlier core+edge split now that all root-cause deploy fixes
are in place (ASG managed-scaling off, no circuit breaker, correct CDK version,
NLB SG opened to CloudFront, explicit Distribution→NLB dependency).
"""
from aws_cdk import App, Environment

from config_loader import ReservedConfig
from stacks.prod_stack import ReservedProdStack


def main() -> None:
    config = ReservedConfig()
    app = App()

    env = Environment(
        account=config.get("AWS", "account_id", "APP_ACCOUNT_ID"),
        region=config.get("AWS", "region", "APP_REGION"),
    )

    ReservedProdStack(
        app,
        config.get("Reserved", "prod_stack_name", "APP_RESERVED_PROD_STACK",
                   fallback="ReservedProdStack"),
        config=config, env=env,
        description="Reserved Mode full production link: CloudFront(VPC Origin)->NLB->Envoy->tenants",
    )

    app.synth()


if __name__ == "__main__":
    main()
