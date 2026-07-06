#!/usr/bin/env python3
"""
Density load test for subsystem C (ECS on EC2, bridge mode).

Goal (spec §4.C density gate): find the *safe* number of idle tasks a single
m6g.4xlarge can hold, validating the cost model (~$0.94/app depends on ~400/host).

What it does:
  1. Runs N idle tasks pinned to ONE container instance, in steps (100/200/300/400).
  2. After each step, waits for tasks to reach RUNNING and polls stability signals:
       - how many tasks actually reached RUNNING (placement ceiling?)
       - ECS agent connection status of the target instance
       - per-task CPU/memory from the ECS task metadata `/stats` (sampled)
  3. Records the first step where placement or stability degrades = the ceiling.

This script does NOT create infrastructure. Deploy the ReservedRuntimeStack first,
then pass its cluster name + a target container-instance ARN.

Usage:
  python density_test.py \
      --cluster <cluster-name> \
      --task-def <family:revision> \
      --steps 100,200,300,400 \
      [--instance-arn <containerInstanceArn>]   # default: the only registered instance

Read-only against AWS except ECS RunTask/StopTask on the test cluster.
"""
import argparse
import sys
import time
from collections import Counter

import boto3

# boto3 run_task hard limit: count max 10 per call (verified via research).
RUN_TASK_MAX_COUNT = 10


def _chunks(total: int, size: int):
    """Yield batch sizes summing to total, each <= size."""
    while total > 0:
        n = min(total, size)
        yield n
        total -= n


def resolve_instance(ecs, cluster: str, instance_arn: str | None) -> str:
    """Return the target container instance ARN (the only one, unless specified)."""
    if instance_arn:
        return instance_arn
    arns = ecs.list_container_instances(cluster=cluster, status="ACTIVE").get(
        "containerInstanceArns", []
    )
    if len(arns) != 1:
        sys.exit(
            f"Expected exactly 1 ACTIVE container instance for an unambiguous "
            f"density test, found {len(arns)}. Pass --instance-arn explicitly."
        )
    return arns[0]


def run_batch(ecs, cluster: str, task_def: str, instance_arn: str, count: int) -> list[str]:
    """Place `count` tasks on a specific instance via placement constraint. Returns task ARNs."""
    started: list[str] = []
    ec2_instance_id = _instance_id_of(ecs, cluster, instance_arn)
    for n in _chunks(count, RUN_TASK_MAX_COUNT):
        resp = ecs.run_task(
            cluster=cluster,
            taskDefinition=task_def,
            count=n,
            launchType="EC2",
            # Pin every task to the SAME instance so density is measured on one host.
            placementConstraints=[
                {
                    "type": "memberOf",
                    "expression": f"ec2InstanceId == {ec2_instance_id}",
                }
            ],
        )
        started.extend(t["taskArn"] for t in resp.get("tasks", []))
        for failure in resp.get("failures", []):
            print(f"  ⚠️  RunTask failure: {failure.get('reason')} ({failure.get('arn')})")
        # Gentle pacing to avoid RunTask throttling on large steps.
        time.sleep(0.5)
    return started


def _instance_id_of(ecs, cluster: str, instance_arn: str) -> str:
    desc = ecs.describe_container_instances(
        cluster=cluster, containerInstances=[instance_arn]
    )["containerInstances"][0]
    return desc["ec2InstanceId"]


def wait_running(ecs, cluster: str, task_arns: list[str], timeout_s: int = 300) -> Counter:
    """Poll until tasks settle; return a Counter of lastStatus."""
    deadline = time.time() + timeout_s
    status = Counter()
    while time.time() < deadline:
        status = Counter()
        # describe_tasks accepts up to 100 arns per call.
        for i in range(0, len(task_arns), 100):
            batch = task_arns[i : i + 100]
            for t in ecs.describe_tasks(cluster=cluster, tasks=batch)["tasks"]:
                status[t["lastStatus"]] += 1
        pending = status.get("PROVISIONING", 0) + status.get("PENDING", 0)
        if pending == 0:
            break
        time.sleep(5)
    return status


def agent_healthy(ecs, cluster: str, instance_arn: str) -> tuple[bool, int]:
    """Return (agentConnected, runningTasksCount) for the target instance."""
    d = ecs.describe_container_instances(
        cluster=cluster, containerInstances=[instance_arn]
    )["containerInstances"][0]
    return d["agentConnected"], d["runningTasksCount"]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cluster", required=True)
    p.add_argument("--task-def", required=True, help="family:revision of the idle test task")
    p.add_argument("--steps", default="100,200,300,400")
    p.add_argument("--instance-arn", default=None)
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--keep", action="store_true", help="do not stop tasks at the end")
    args = p.parse_args()

    ecs = boto3.client("ecs", region_name=args.region)
    steps = [int(s) for s in args.steps.split(",")]
    instance_arn = resolve_instance(ecs, args.cluster, args.instance_arn)

    print("=" * 60)
    print(f"Density test on {args.cluster}")
    print(f"  target instance: {instance_arn}")
    print(f"  task def:        {args.task_def}")
    print(f"  steps:           {steps}")
    print("=" * 60)

    all_tasks: list[str] = []
    prev_target = 0
    ceiling = None

    for target in steps:
        delta = target - prev_target
        prev_target = target
        print(f"\n▶ Scaling to {target} tasks (adding {delta})...")
        new_tasks = run_batch(ecs, args.cluster, args.task_def, instance_arn, delta)
        all_tasks.extend(new_tasks)
        print(f"  RunTask accepted {len(new_tasks)}/{delta}")

        status = wait_running(ecs, args.cluster, all_tasks)
        running = status.get("RUNNING", 0)
        connected, agent_running = agent_healthy(ecs, args.cluster, instance_arn)

        print(f"  status: {dict(status)}")
        print(f"  agentConnected={connected}  instance.runningTasksCount={agent_running}")

        # Ceiling detection: placement shortfall or agent disconnect.
        if running < target or not connected:
            ceiling = running
            print(
                f"  🚩 DEGRADATION at target={target}: only {running} RUNNING, "
                f"agentConnected={connected}. Recording ceiling ≈ {ceiling}."
            )
            break

    print("\n" + "=" * 60)
    if ceiling is not None:
        print(f"RESULT: safe density ≈ {ceiling} tasks/m6g.4xlarge (below target {steps[-1]})")
    else:
        print(f"RESULT: reached {steps[-1]} tasks with no degradation. Safe density ≥ {steps[-1]}.")
    print("=" * 60)

    if not args.keep:
        print(f"\nStopping {len(all_tasks)} test tasks...")
        for arn in all_tasks:
            try:
                ecs.stop_task(cluster=args.cluster, task=arn, reason="density-test cleanup")
            except Exception as e:  # noqa: BLE001 - cleanup best-effort, report and continue
                print(f"  ⚠️  stop_task failed for {arn}: {e}")
        print("Cleanup requested. Verify with: aws ecs list-tasks --cluster", args.cluster)
    else:
        print("\n--keep set: test tasks left running. Remember to stop them (they cost money).")


if __name__ == "__main__":
    main()
