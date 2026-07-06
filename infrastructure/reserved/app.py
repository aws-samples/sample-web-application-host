#!/usr/bin/env python3
"""
CDK app entrypoint for Reserved Mode — two stacks, both CDK-created:

  ReservedProdStack  (core): VPC, ECS cluster, EC2 ASG, NLB, Envoy, DynamoDB,
                             tenant task def — the stable, tear-down-clean layer.
  ReservedEdgeStack  (edge): CloudFront + VPC Origin (→ core NLB) + Host policy.

The edge is split out because a CloudFront VPC Origin cancelled mid-create becomes
stuck/undeletable; isolating it means core failures never strand it and it only
associates with an already-stable NLB. Deploy both with:

    cdk deploy --app "python3 app.py" --all
"""
from aws_cdk import App, Environment

from config_loader import ReservedConfig
from stacks.prod_stack import ReservedProdStack
from stacks.edge_stack import ReservedEdgeStack


def main() -> None:
    config = ReservedConfig()
    app = App()

    env = Environment(
        account=config.get("AWS", "account_id", "APP_ACCOUNT_ID"),
        region=config.get("AWS", "region", "APP_REGION"),
    )

    core = ReservedProdStack(
        app,
        config.get("Reserved", "prod_stack_name", "APP_RESERVED_PROD_STACK",
                   fallback="ReservedProdStack"),
        config=config, env=env,
        description="Reserved Mode core: ECS-on-EC2 + NLB + Envoy + tenants (no edge)",
    )

    edge = ReservedEdgeStack(
        app,
        config.get("Reserved", "edge_stack_name", "APP_RESERVED_EDGE_STACK",
                   fallback="ReservedEdgeStack"),
        config=config, env=env, nlb=core.nlb, nlb_sg=core.nlb_sg,
        description="Reserved Mode edge: CloudFront (VPC Origin) -> core NLB",
    )
    edge.add_dependency(core)  # edge (CloudFront VPC Origin) waits for a stable NLB

    app.synth()


if __name__ == "__main__":
    main()
