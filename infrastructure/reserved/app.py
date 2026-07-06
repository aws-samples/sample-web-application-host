#!/usr/bin/env python3
"""
CDK app entrypoint for Reserved Mode stacks.

Kept separate from the Autoscale app (infrastructure/stack.py) so the two modes
deploy independently. Run from infrastructure/reserved/:

    cdk synth   --app "python3 app.py"
    cdk deploy  --app "python3 app.py" ReservedRuntimeStack

Day-1 this app contains only subsystem C (runtime). B/D/E/A/F stacks are added
here as they are implemented, in the spec's C→E→D→B→A order.
"""
from aws_cdk import App, Environment

from config_loader import ReservedConfig
from stacks.runtime_stack import ReservedRuntimeStack


def main() -> None:
    config = ReservedConfig()
    app = App()

    env = Environment(
        account=config.get("AWS", "account_id", "APP_ACCOUNT_ID"),
        region=config.get("AWS", "region", "APP_REGION"),
    )

    ReservedRuntimeStack(
        app,
        config.get(
            "Reserved", "runtime_stack_name", "APP_RESERVED_RUNTIME_STACK",
            fallback="ReservedRuntimeStack",
        ),
        config=config,
        env=env,
        description="Reserved Mode subsystem C: ECS on EC2 runtime for density validation",
    )

    app.synth()


if __name__ == "__main__":
    main()
