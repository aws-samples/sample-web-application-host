#!/usr/bin/env python3
"""
CDK app entrypoint for Reserved Mode.

Single self-contained stack (ReservedProdStack) that builds the full production
link: CloudFront (VPC Origin) → internal NLB → Envoy → tenant containers on
ECS-on-EC2. Everything is CDK-created; no config-file resource IDs, no post-deploy
CLI. Run from infrastructure/reserved/:

    cdk deploy --app "python3 app.py" ReservedProdStack
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
        config.get(
            "Reserved", "prod_stack_name", "APP_RESERVED_PROD_STACK",
            fallback="ReservedProdStack",
        ),
        config=config,
        env=env,
        description="Reserved Mode full production link: CloudFront(VPC Origin)->NLB->Envoy->tenants",
    )

    app.synth()


if __name__ == "__main__":
    main()
