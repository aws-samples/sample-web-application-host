#!/usr/bin/env python3
"""
Register N tenant apps for the multi-tenant test (subsystem B/D helper).

For each tenant i in 1..N:
  1. RunTask the reserved-tenant-app task def on the runtime instance, injecting
     TENANT_ID=tenant-<i> via container override.
  2. Wait for RUNNING, then extract host_ip:host_port (bridge dynamic port),
     per spec §5.1 semantics:
       host_port = DescribeTasks.containers[].networkBindings[].hostPort
       host_ip   = containerInstance -> ec2InstanceId -> PrivateIpAddress
  3. Write the route into app_routes with status=routing so Envoy serves it.

This is the test-harness version of subsystem D's deploy orchestration.

Usage:
  python register_tenants.py --cluster reserved-mode-cluster \
      --task-def reserved-tenant-app --table reserved-app-routes --count 10
"""
import argparse
import time

import boto3


def instance_private_ip(ecs, ec2, cluster: str, container_instance_arn: str) -> str:
    ci = ecs.describe_container_instances(
        cluster=cluster, containerInstances=[container_instance_arn]
    )["containerInstances"][0]
    ec2_id = ci["ec2InstanceId"]
    inst = ec2.describe_instances(InstanceIds=[ec2_id])["Reservations"][0]["Instances"][0]
    return inst["PrivateIpAddress"]


def run_tenant(ecs, cluster: str, task_def: str, tenant_id: str) -> str:
    resp = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_def,
        count=1,
        launchType="EC2",
        overrides={"containerOverrides": [
            {"name": "app", "environment": [{"name": "TENANT_ID", "value": tenant_id}]}
        ]},
    )
    if resp.get("failures"):
        raise RuntimeError(f"RunTask failed for {tenant_id}: {resp['failures']}")
    return resp["tasks"][0]["taskArn"]


def wait_binding(ecs, cluster: str, task_arn: str, timeout_s: int = 120) -> tuple[str, int]:
    """Wait for RUNNING and return (container_instance_arn, host_port)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        t = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
        if t["lastStatus"] == "RUNNING":
            binds = t["containers"][0].get("networkBindings", [])
            host_port = next((b["hostPort"] for b in binds if b.get("containerPort") == 80), None)
            if host_port:
                return t["containerInstanceArn"], host_port
        time.sleep(3)
    raise TimeoutError(f"task {task_arn} did not reach RUNNING with a host port")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cluster", required=True)
    p.add_argument("--task-def", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--start", type=int, default=1, help="starting tenant index")
    args = p.parse_args()

    ecs = boto3.client("ecs", region_name=args.region)
    ec2 = boto3.client("ec2", region_name=args.region)
    ddb = boto3.client("dynamodb", region_name=args.region)

    print(f"Registering tenants {args.start}..{args.start + args.count - 1}")
    for i in range(args.start, args.start + args.count):
        tenant = f"tenant-{i}"
        task_arn = run_tenant(ecs, args.cluster, args.task_def, tenant)
        ci_arn, host_port = wait_binding(ecs, args.cluster, task_arn)
        host_ip = instance_private_ip(ecs, ec2, args.cluster, ci_arn)
        ddb.put_item(
            TableName=args.table,
            Item={
                "subdomain": {"S": tenant},
                "host_ip": {"S": host_ip},
                "host_port": {"N": str(host_port)},
                "app_id": {"S": tenant},
                "task_arn": {"S": task_arn},
                "status": {"S": "routing"},
                "gsi_bucket": {"S": "all"},
                "updated_at": {"S": str(int(time.time()))},
            },
        )
        print(f"  {tenant} -> {host_ip}:{host_port}  (task {task_arn.split('/')[-1][:12]})")

    print(f"Done. {args.count} tenants registered in {args.table}.")


if __name__ == "__main__":
    main()
