"""
Multi-tenant test stack — subsystem B (Envoy L7 routing) + test ingress.

Spec: docs/superpowers/specs/2026-07-06-reserved-mode-migration-design.md §4.B, §5.1

Purpose: give the density-validated runtime (subsystem C) what it needs to serve
REAL multi-tenant traffic, so the multi-tenant-test-plan.md cases can run:
  * app_routes DynamoDB table (subdomain -> host_ip:host_port), spec §5.1 semantics
  * Envoy task (L7 host-based routing) with a route-sync sidecar that polls
    app_routes and writes Envoy dynamic config (EDS/RDS over file)
  * public ALB -> Envoy (AUTHORIZED test simplification; prod uses CloudFront->NLB)

This deploys INTO the existing ReservedRuntimeStack cluster/VPC (imported), so it
reuses the density-tested instance rather than standing up new capacity.

NOTE: this is a TEST harness, not the production B implementation. It proves the
multi-tenant routing model end-to-end; the prod Envoy (xDS evolution, NLB ingress,
TLS) is tracked in the spec.
"""
from aws_cdk import (
    Stack,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Tags,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_dynamodb as dynamodb,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_ecr as ecr,
)
from constructs import Construct


class ReservedMultiTenantTestStack(Stack):
    """Envoy L7 routing + public ALB ingress + app_routes, for multi-tenant tests."""

    def __init__(self, scope: Construct, construct_id: str, config, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        for k, v in config.get_tags().items():
            Tags.of(self).add(k, v)

        # --- Import the runtime stack's VPC + cluster (reuse density-tested capacity) ---
        vpc = ec2.Vpc.from_lookup(
            self, "RuntimeVpc",
            tags={"aws:cloudformation:stack-name": config.get(
                "Reserved", "runtime_stack_name", "APP_RESERVED_RUNTIME_STACK",
                fallback="ReservedRuntimeStack")},
        )

        cluster_name = config.get(
            "Reserved", "cluster_name", "APP_RESERVED_CLUSTER_NAME",
            fallback="reserved-mode-cluster")
        cluster = ecs.Cluster.from_cluster_attributes(
            self, "RuntimeCluster", cluster_name=cluster_name, vpc=vpc,
            security_groups=[],
        )

        # --- app_routes table (spec §5.1: subdomain -> host_ip:host_port) ---
        # KEY SEMANTIC (Codex-verified): bridge mode → host EC2 private IP + dynamic
        # host port, NOT task IP. GSI on updated_at for incremental route sync.
        app_routes = dynamodb.Table(
            self, "AppRoutes",
            table_name="reserved-app-routes",
            partition_key=dynamodb.Attribute(
                name="subdomain", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        app_routes.add_global_secondary_index(
            index_name="updated_at-index",
            partition_key=dynamodb.Attribute(
                name="gsi_bucket", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="updated_at", type=dynamodb.AttributeType.STRING),
        )

        log_group = logs.LogGroup(
            self, "MtTestLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Envoy task role: read app_routes + ECS describe for discovery ---
        envoy_task_role = iam.Role(
            self, "EnvoyTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        app_routes.grant_read_data(envoy_task_role)
        envoy_task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["ecs:DescribeTasks", "ecs:ListTasks",
                     "ecs:DescribeContainerInstances", "ec2:DescribeInstances"],
            resources=["*"],
        ))

        # --- Envoy task (bridge, fixed host port so ALB can target it) ---
        # Envoy listens on a known host port; ALB target group points at the
        # instance:port. Route-sync sidecar polls app_routes → writes Envoy
        # dynamic config to a shared volume.
        envoy_port = config.get_int(
            "Reserved", "envoy_host_port", "APP_RESERVED_ENVOY_PORT", fallback=10000)

        envoy_task_def = ecs.Ec2TaskDefinition(
            self, "EnvoyTaskDef",
            family="reserved-envoy",
            network_mode=ecs.NetworkMode.BRIDGE,
            task_role=envoy_task_role,
        )

        shared_volume = "envoy-config"
        envoy_task_def.add_volume(name=shared_volume)

        # Route-sync sidecar: polls app_routes, renders Envoy config to shared vol.
        # Uses a small python image; the sync logic is mounted via a command that
        # generates config. For the test harness we render a static-ish CDS/RDS
        # snapshot each poll (full refresh — fine at test scale).
        # Reference pre-built arm64 images from ECR repos (built+pushed out of band
        # via docker buildx). Avoids the CDK container-asset bootstrap repo, which
        # this account's bootstrap does not include.
        sync_repo = ecr.Repository.from_repository_name(
            self, "RouteSyncRepo", "reserved-route-sync")
        envoy_repo = ecr.Repository.from_repository_name(
            self, "EnvoyRepo", "reserved-envoy")

        sync = envoy_task_def.add_container(
            "route-sync",
            image=ecs.ContainerImage.from_ecr_repository(sync_repo, tag="latest"),
            memory_reservation_mib=128,
            essential=False,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="route-sync", log_group=log_group),
            environment={
                "APP_ROUTES_TABLE": app_routes.table_name,
                "AWS_REGION": config.get("AWS", "region", "APP_REGION"),
                "ENVOY_CONFIG_DIR": "/etc/envoy-dynamic",
                "POLL_INTERVAL_SECONDS": "3",
                "TENANT_DOMAIN": config.get(
                    "CloudFront", "domain_name", "APP_DOMAIN_NAME").lstrip("*."),
            },
        )
        sync.add_mount_points(ecs.MountPoint(
            container_path="/etc/envoy-dynamic", source_volume=shared_volume,
            read_only=False))

        # Envoy proxy container.
        envoy = envoy_task_def.add_container(
            "envoy",
            image=ecs.ContainerImage.from_ecr_repository(envoy_repo, tag="latest"),
            memory_reservation_mib=256,
            essential=True,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="envoy", log_group=log_group),
            port_mappings=[ecs.PortMapping(
                container_port=10000, host_port=envoy_port, protocol=ecs.Protocol.TCP)],
        )
        envoy.add_mount_points(ecs.MountPoint(
            container_path="/etc/envoy-dynamic", source_volume=shared_volume,
            read_only=True))

        # Envoy admin port for /stats, /clusters, /config_dump during tests.
        envoy.add_port_mappings(ecs.PortMapping(
            container_port=9901, host_port=9901, protocol=ecs.Protocol.TCP))

        # --- Envoy ECS Service (1 replica for the test) ---
        envoy_service = ecs.Ec2Service(
            self, "EnvoyService",
            cluster=cluster,
            task_definition=envoy_task_def,
            desired_count=1,
            min_healthy_percent=0,   # single instance test; allow replace
            max_healthy_percent=100,
        )

        # --- Public ALB -> Envoy (AUTHORIZED test simplification) ---
        # Prod ingress is CloudFront->NLB->Envoy; for the test we expose a public
        # ALB directly to Envoy's host port so we can curl tenant Hosts.
        alb_sg = ec2.SecurityGroup(
            self, "AlbSg", vpc=vpc, allow_all_outbound=True,
            description="Public ALB for multi-tenant test ingress")
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "test HTTP ingress")

        alb = elbv2.ApplicationLoadBalancer(
            self, "TestAlb", vpc=vpc, internet_facing=True, security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )
        listener = alb.add_listener("Http", port=80, open=True)

        # Target the Envoy host port on the container instance. Because Envoy runs
        # on a fixed host port in bridge mode, we register the instance:port.
        listener.add_targets(
            "EnvoyTargets",
            port=envoy_port,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[envoy_service.load_balancer_target(
                container_name="envoy", container_port=10000)],
            health_check=elbv2.HealthCheck(
                path="/", port=str(envoy_port),
                healthy_http_codes="200,404",  # 404 = Envoy up but Host unknown
                interval=Duration.seconds(10), healthy_threshold_count=2),
        )

        # NOTE: the ALB SG -> instance Envoy-port ingress rule is applied
        # operationally after deploy (the runtime instance SG is owned by the
        # runtime stack; mutating it cross-stack is fragile). The deploy runbook
        # adds: authorize-security-group-ingress on the instance SG from alb_sg
        # for tcp/<envoy_port> and tcp/9901. AlbSgId is output below.
        CfnOutput(self, "AlbSecurityGroupId", value=alb_sg.security_group_id)

        # --- Tenant app task definition (distinguishable nginx) ---
        # Returns {"tenant_id","hostname"} so tests can assert routing correctness.
        # TENANT_ID injected per-run via run-task container overrides (register script).
        tenant_task_def = ecs.Ec2TaskDefinition(
            self, "TenantAppTaskDef",
            family="reserved-tenant-app",
            network_mode=ecs.NetworkMode.BRIDGE,
        )
        tenant_task_def.add_container(
            "app",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/nginx/nginx:stable"),
            cpu=0,  # no CPU placement reservation (spec §4.C)
            memory_reservation_mib=128,
            memory_limit_mib=2048,  # $36 OOM cap
            essential=True,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="tenant", log_group=log_group),
            environment={"TENANT_ID": "unset"},  # overridden per task at run-time
            entry_point=["/bin/sh", "-c"],
            command=[
                'echo "{\\"tenant_id\\":\\"$TENANT_ID\\",\\"hostname\\":\\"$HOSTNAME\\"}" '
                '> /usr/share/nginx/html/index.html; nginx -g "daemon off;"'
            ],
            port_mappings=[ecs.PortMapping(
                container_port=80, host_port=0, protocol=ecs.Protocol.TCP)],
        )

        CfnOutput(self, "AppRoutesTable", value=app_routes.table_name)
        CfnOutput(self, "EnvoyHostPort", value=str(envoy_port))
        CfnOutput(self, "ClusterNameOut", value=cluster_name)
        CfnOutput(self, "AlbDnsName", value=alb.load_balancer_dns_name)
        CfnOutput(self, "TenantTaskFamily", value=tenant_task_def.family)
