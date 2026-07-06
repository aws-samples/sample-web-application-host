"""
Subsystem C — Reserved Mode runtime layer: ECS on EC2, bridge mode, high density.

Spec: docs/superpowers/specs/2026-07-06-reserved-mode-migration-design.md §4.C

Design decisions baked in here (the load-bearing ones):

* **CPU reservation = 0 (no CPU placement reservation). This is the fix for a
  bug in the spec's stated "cpu=64 (soft)".**
  Verified against AWS docs (task_definition_parameters.html, capacity-tasksize.html,
  cluster_reservation.html): CPU reservations are GUARANTEED and ECS will not place
  a task where the reservation can't be fulfilled. The placement reservation is the
  task-level `cpu` if set, otherwise the SUM of container-level `cpu`. So a
  container-level cpu of 64 IS counted against placement — it is NOT a free "share
  weight". 400 tasks × 64 = 25,600 > 16,384 (16 vCPU) → placement caps at ~256
  tasks, never reaching the ~400 the cost model needs.
  Setting cpu=0 means NO CPU reservation: density is bounded only by
  memoryReservation, while the Linux CFS share falls back to the kernel minimum (2)
  — every container is equal-weighted and can still burst into all idle CPU. That is
  exactly the spec's intended "oversubscription / pay-per-use" behavior, done
  correctly. (If a minimum per-app CPU guarantee is later required, cpu must be ≤ 40
  — 16384/400 — to preserve 400 density; 64 is simply wrong.)

* **Placement is bounded by memoryReservation only.** 128 MB × 400 = 51.2 GB < 64 GB,
  leaving headroom for the ECS agent + OS. `memory` (hard) = 2048 MB is the OOM cap
  (= the $36/mo ceiling), NOT a placement reservation.

* **bridge networking, dynamic host ports.** No ENI-per-task limit (that's awsvpc),
  enabling the density this cost model needs.

This stack intentionally has NO external dependencies (no Envoy, no control plane).
It stands up a cluster + one ASG instance + an idle test task definition so the
density gate (scripts/density_test.py) can produce a real safe-density number.
"""
from aws_cdk import (
    Stack,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Tags,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_autoscaling as autoscaling,
    aws_iam as iam,
    aws_logs as logs,
)
from constructs import Construct


class ReservedRuntimeStack(Stack):
    """ECS-on-EC2 cluster sized for the Reserved Mode density gate."""

    def __init__(self, scope: Construct, construct_id: str, config, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        for k, v in config.get_tags().items():
            Tags.of(self).add(k, v)

        # --- Networking -----------------------------------------------------
        # Dedicated VPC so the Reserved runtime is isolated from Autoscale.
        # 3 AZ per spec; NAT for image pulls (ECR/CNB) — single NAT for the test
        # phase to save cost, revisit for prod HA.
        vpc = ec2.Vpc(
            self,
            "ReservedVpc",
            max_azs=3,
            nat_gateways=1,
            ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
        )

        cluster = ecs.Cluster(
            self,
            "ReservedCluster",
            vpc=vpc,
            cluster_name=config.get(
                "Reserved", "cluster_name", "APP_RESERVED_CLUSTER_NAME",
                fallback="reserved-mode-cluster",
            ),
            # Container Insights kept ON for ops observability only.
            # NOTE (spec ADR-6): it is NOT the billing source — billing uses
            # node self-collection to avoid custom-metric cost blow-up.
            container_insights=True,
        )

        # --- Capacity: m6g.4xlarge (Graviton/arm64) ASG ---------------------
        # Density gate runs on ONE instance; ASG min/max kept small and explicit.
        instance_type = config.get(
            "Reserved", "instance_type", "APP_RESERVED_INSTANCE_TYPE",
            fallback="m6g.4xlarge",
        )

        # arm64 ECS-optimized AMI for Graviton. AL2023 is the current default.
        machine_image = ecs.EcsOptimizedImage.amazon_linux2023(
            hardware_type=ecs.AmiHardwareType.ARM
        )

        # High-density ECS agent tuning. These reduce image-cleanup churn and
        # reserve memory for the agent/OS so hundreds of tasks stay stable.
        # (Reconciled against research on high-density ECS agent behavior.)
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            f"echo 'ECS_CLUSTER={cluster.cluster_name}' >> /etc/ecs/ecs.config",
            "echo 'ECS_ENABLE_TASK_IAM_ROLE=true' >> /etc/ecs/ecs.config",
            # Reserve memory so the agent/OS are not starved at high task counts.
            "echo 'ECS_RESERVED_MEMORY=1024' >> /etc/ecs/ecs.config",
            # Slow image GC so it doesn't thrash under many short-lived pulls.
            "echo 'ECS_ENGINE_TASK_CLEANUP_WAIT_DURATION=1h' >> /etc/ecs/ecs.config",
            "echo 'ECS_NUM_IMAGES_DELETE_PER_CYCLE=5' >> /etc/ecs/ecs.config",
            # Widen the ephemeral range so bridge dynamic host ports don't exhaust.
            "sysctl -w net.ipv4.ip_local_port_range='16384 65535' || true",
        )

        asg = autoscaling.AutoScalingGroup(
            self,
            "ReservedAsg",
            vpc=vpc,
            instance_type=ec2.InstanceType(instance_type),
            machine_image=machine_image,
            min_capacity=config.get_int(
                "Reserved", "asg_min", "APP_RESERVED_ASG_MIN", fallback=1
            ),
            max_capacity=config.get_int(
                "Reserved", "asg_max", "APP_RESERVED_ASG_MAX", fallback=3
            ),
            desired_capacity=config.get_int(
                "Reserved", "asg_desired", "APP_RESERVED_ASG_DESIRED", fallback=1
            ),
            user_data=user_data,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )

        capacity_provider = ecs.AsgCapacityProvider(
            self,
            "ReservedCapacityProvider",
            auto_scaling_group=asg,
            # We manage instance count via the density test; keep termination
            # protection off so cleanup is simple during the test phase.
            enable_managed_termination_protection=False,
        )
        cluster.add_asg_capacity_provider(capacity_provider)

        # --- Idle test task definition --------------------------------------
        # Uses a public multi-arch image (nginx) as an idle stand-in so subsystem
        # C can be validated WITHOUT subsystem E (the build layer).
        log_group = logs.LogGroup(
            self,
            "IdleTaskLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # bridge mode → task-level networkMode bridge; NO task-level cpu (see docstring).
        idle_task_def = ecs.Ec2TaskDefinition(
            self,
            "IdleTaskDef",
            family=config.get(
                "Reserved", "idle_task_family", "APP_RESERVED_IDLE_FAMILY",
                fallback="reserved-idle-test",
            ),
            network_mode=ecs.NetworkMode.BRIDGE,
        )

        idle_task_def.add_container(
            "idle",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/nginx/nginx:stable"),
            # cpu=0 → NO CPU placement reservation (see docstring). Density is bounded
            # by memoryReservation only. Default 0; do NOT raise above 40 or 400/host
            # density becomes unreachable.
            cpu=config.get_int("Reserved", "task_cpu", "APP_RESERVED_TASK_CPU", fallback=0),
            # Scheduling floor (counted in placement): 128 MB × 400 = 51.2 GB < 64 GB.
            memory_reservation_mib=config.get_int(
                "Reserved", "task_mem_reservation", "APP_RESERVED_MEM_RESERVATION", fallback=128
            ),
            # Hard OOM cap = the $36/mo ceiling.
            memory_limit_mib=config.get_int(
                "Reserved", "task_mem_hard", "APP_RESERVED_MEM_HARD", fallback=2048
            ),
            essential=True,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="idle", log_group=log_group),
            port_mappings=[
                # host_port=0 → Docker assigns a dynamic ephemeral host port (bridge).
                ecs.PortMapping(container_port=80, host_port=0, protocol=ecs.Protocol.TCP)
            ],
        )

        # --- Outputs (fed into scripts/density_test.py) ---------------------
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "IdleTaskFamily", value=idle_task_def.family)
        CfnOutput(self, "VpcId", value=vpc.vpc_id)
        CfnOutput(self, "InstanceType", value=instance_type)
