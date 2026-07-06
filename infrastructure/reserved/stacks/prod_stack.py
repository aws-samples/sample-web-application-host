"""
ReservedProdStack — single self-contained CDK stack for the FULL production link:

    User → CloudFront (VPC Origin + Origin Request Policy, no Lambda@Edge)
         → internal NLB (L4/TCP)
         → Envoy (L7 host routing)
         → tenant container (ECS on EC2, bridge)

Spec: docs/superpowers/specs/2026-07-06-reserved-mode-migration-design.md (§2.1, ADR-1/2/4)

Everything is created by CDK in ONE stack — VPC, ECS cluster, EC2 ASG, ALL security
groups, NLB, Envoy service, CloudFront, DynamoDB, tenant task def — so intra-stack
object references wire the security groups together with no cross-stack cycles, no
config-file resource IDs, and no post-deploy CLI. This replaces the earlier split
(runtime + multi-tenant) stacks.

Design decisions carried over (verified earlier):
  * container cpu=0 → no CPU placement reservation; density bounded by memory (§4.C)
  * bridge networking + userland-proxy disabled (high-density port-collision fix)
  * app_routes: subdomain → host_ip:host_port (bridge dynamic port, NOT task IP, §5.1)
  * Envoy dynamic config via route-sync sidecar polling app_routes (ADR-5)
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
    aws_dynamodb as dynamodb,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_ecr as ecr,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
)
from constructs import Construct


class ReservedProdStack(Stack):
    """Full CloudFront→NLB→Envoy→tenant production link, one self-contained stack."""

    def __init__(self, scope: Construct, construct_id: str, config, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        for k, v in config.get_tags().items():
            Tags.of(self).add(k, v)

        envoy_host_port = config.get_int(
            "Reserved", "envoy_host_port", "APP_RESERVED_ENVOY_PORT", fallback=10000)

        # ------------------------------------------------------------------
        # Networking: dedicated VPC (3 AZ). NLB is internal; instances private.
        # ------------------------------------------------------------------
        vpc = ec2.Vpc(
            self, "Vpc", max_azs=3, nat_gateways=1,
            ip_addresses=ec2.IpAddresses.cidr("10.30.0.0/16"),
        )

        # ------------------------------------------------------------------
        # ECS on EC2 cluster + Graviton ASG (subsystem C, density-validated).
        # ------------------------------------------------------------------
        cluster = ecs.Cluster(
            self, "Cluster", vpc=vpc,
            cluster_name=config.get(
                "Reserved", "cluster_name", "APP_RESERVED_CLUSTER_NAME",
                fallback="reserved-mode-cluster"),
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        instance_type = config.get(
            "Reserved", "instance_type", "APP_RESERVED_INSTANCE_TYPE",
            fallback="m6g.4xlarge")
        machine_image = ecs.EcsOptimizedImage.amazon_linux2023(
            hardware_type=ecs.AmiHardwareType.ARM)

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            f"echo 'ECS_CLUSTER={cluster.cluster_name}' >> /etc/ecs/ecs.config",
            "echo 'ECS_ENABLE_TASK_IAM_ROLE=true' >> /etc/ecs/ecs.config",
            "echo 'ECS_RESERVED_MEMORY=1024' >> /etc/ecs/ecs.config",
            "echo 'ECS_ENGINE_TASK_CLEANUP_WAIT_DURATION=1h' >> /etc/ecs/ecs.config",
            "echo 'ECS_NUM_IMAGES_DELETE_PER_CYCLE=5' >> /etc/ecs/ecs.config",
            "sysctl -w net.ipv4.ip_local_port_range='16384 65535' || true",
            # High-density bridge fix: disable Docker userland-proxy (port collisions).
            "mkdir -p /etc/docker",
            "echo '{\"userland-proxy\": false}' > /etc/docker/daemon.json",
            "systemctl restart docker || service docker restart || true",
        )

        asg = autoscaling.AutoScalingGroup(
            self, "Asg", vpc=vpc,
            instance_type=ec2.InstanceType(instance_type),
            machine_image=machine_image,
            min_capacity=config.get_int("Reserved", "asg_min", "APP_RESERVED_ASG_MIN", fallback=1),
            max_capacity=config.get_int("Reserved", "asg_max", "APP_RESERVED_ASG_MAX", fallback=3),
            desired_capacity=config.get_int("Reserved", "asg_desired", "APP_RESERVED_ASG_DESIRED", fallback=1),
            user_data=user_data,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        capacity_provider = ecs.AsgCapacityProvider(
            self, "CapacityProvider", auto_scaling_group=asg,
            enable_managed_termination_protection=False)
        cluster.add_asg_capacity_provider(capacity_provider)

        # ------------------------------------------------------------------
        # DynamoDB app_routes (subdomain → host_ip:host_port), spec §5.1.
        # ------------------------------------------------------------------
        app_routes = dynamodb.Table(
            self, "AppRoutes", table_name="reserved-app-routes",
            partition_key=dynamodb.Attribute(name="subdomain", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        app_routes.add_global_secondary_index(
            index_name="updated_at-index",
            partition_key=dynamodb.Attribute(name="gsi_bucket", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="updated_at", type=dynamodb.AttributeType.STRING),
        )

        log_group = logs.LogGroup(
            self, "Logs", retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY)

        # ------------------------------------------------------------------
        # Envoy task (L7 routing) + route-sync sidecar. Pre-built arm64 images.
        # ------------------------------------------------------------------
        envoy_task_role = iam.Role(
            self, "EnvoyTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        app_routes.grant_read_data(envoy_task_role)
        envoy_task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["ecs:DescribeTasks", "ecs:ListTasks",
                     "ecs:DescribeContainerInstances", "ec2:DescribeInstances"],
            resources=["*"]))

        envoy_task_def = ecs.Ec2TaskDefinition(
            self, "EnvoyTaskDef", family="reserved-envoy",
            network_mode=ecs.NetworkMode.BRIDGE, task_role=envoy_task_role)
        shared_volume = "envoy-config"
        envoy_task_def.add_volume(name=shared_volume)

        sync_repo = ecr.Repository.from_repository_name(self, "SyncRepo", "reserved-route-sync")
        envoy_repo = ecr.Repository.from_repository_name(self, "EnvoyRepo", "reserved-envoy")

        sync = envoy_task_def.add_container(
            "route-sync",
            image=ecs.ContainerImage.from_ecr_repository(sync_repo, tag="latest"),
            memory_reservation_mib=128, essential=False,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="route-sync", log_group=log_group),
            environment={
                "APP_ROUTES_TABLE": app_routes.table_name,
                "AWS_REGION": config.get("AWS", "region", "APP_REGION"),
                "ENVOY_CONFIG_DIR": "/etc/envoy-dynamic",
                "POLL_INTERVAL_SECONDS": "3",
                "TENANT_DOMAIN": config.get("CloudFront", "domain_name", "APP_DOMAIN_NAME").lstrip("*."),
            },
        )
        sync.add_mount_points(ecs.MountPoint(
            container_path="/etc/envoy-dynamic", source_volume=shared_volume, read_only=False))

        envoy = envoy_task_def.add_container(
            "envoy",
            image=ecs.ContainerImage.from_ecr_repository(envoy_repo, tag="latest"),
            memory_reservation_mib=256, essential=True,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="envoy", log_group=log_group),
            port_mappings=[ecs.PortMapping(
                container_port=10000, host_port=envoy_host_port, protocol=ecs.Protocol.TCP)],
        )
        envoy.add_mount_points(ecs.MountPoint(
            container_path="/etc/envoy-dynamic", source_volume=shared_volume, read_only=True))
        envoy.add_container_dependencies(ecs.ContainerDependency(
            container=sync, condition=ecs.ContainerDependencyCondition.START))

        envoy_service = ecs.Ec2Service(
            self, "EnvoyService", cluster=cluster, task_definition=envoy_task_def,
            desired_count=1, min_healthy_percent=0, max_healthy_percent=100,
            # Fail fast instead of a 3h CloudFormation wait if Envoy can't start.
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=False))

        # ------------------------------------------------------------------
        # internal NLB (L4/TCP) → Envoy. NLB needs an SG to be a VPC Origin.
        # ------------------------------------------------------------------
        nlb_sg = ec2.SecurityGroup(
            self, "NlbSg", vpc=vpc, allow_all_outbound=True,
            description="internal NLB for Reserved ingress (CloudFront VPC Origin)")
        nlb = elbv2.NetworkLoadBalancer(
            self, "IngressNlb", vpc=vpc, internet_facing=False,
            security_groups=[nlb_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS))
        nlb_listener = nlb.add_listener("Tcp", port=80, protocol=elbv2.Protocol.TCP)
        nlb_listener.add_targets(
            "EnvoyTargets", port=envoy_host_port, protocol=elbv2.Protocol.TCP,
            targets=[envoy_service.load_balancer_target(container_name="envoy", container_port=10000)],
            health_check=elbv2.HealthCheck(
                protocol=elbv2.Protocol.HTTP, path="/", port=str(envoy_host_port),
                healthy_http_codes="200,404",
                interval=Duration.seconds(10), healthy_threshold_count=2))

        # Intra-stack wiring: allow NLB → EC2 instances on the Envoy host port.
        # asg.connections is the instance SG (CDK-created), referenced natively.
        asg.connections.allow_from(nlb_sg, ec2.Port.tcp(envoy_host_port), "NLB to Envoy")
        # NLB (with SG) also health-checks targets from the NLB SG; same rule covers it.

        # ------------------------------------------------------------------
        # CloudFront: VPC Origin → NLB, Origin Request Policy forwards Host.
        # ------------------------------------------------------------------
        cert = acm.Certificate.from_certificate_arn(
            self, "Cert", config.get("CloudFront", "certificate_arn", "APP_CERTIFICATE_ARN"))
        domain = config.get("CloudFront", "domain_name", "APP_DOMAIN_NAME")

        vpc_origin = origins.VpcOrigin.with_network_load_balancer(
            nlb, protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY, http_port=80)

        orp = cloudfront.OriginRequestPolicy(
            self, "ForwardHost", origin_request_policy_name="ReservedForwardHost",
            header_behavior=cloudfront.OriginRequestHeaderBehavior.all(),
            cookie_behavior=cloudfront.OriginRequestCookieBehavior.all(),
            query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.all())

        distribution = cloudfront.Distribution(
            self, "Cdn",
            default_behavior=cloudfront.BehaviorOptions(
                origin=vpc_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=orp),
            domain_names=[domain], certificate=cert,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021)

        # ------------------------------------------------------------------
        # Tenant app task def (distinguishable nginx: returns tenant_id+hostname).
        # ------------------------------------------------------------------
        tenant_task_def = ecs.Ec2TaskDefinition(
            self, "TenantAppTaskDef", family="reserved-tenant-app",
            network_mode=ecs.NetworkMode.BRIDGE)
        tenant_task_def.add_container(
            "app",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/nginx/nginx:stable"),
            cpu=0, memory_reservation_mib=128, memory_limit_mib=2048, essential=True,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="tenant", log_group=log_group),
            environment={"TENANT_ID": "unset"},
            entry_point=["/bin/sh", "-c"],
            command=[
                'echo "{\\"tenant_id\\":\\"$TENANT_ID\\",\\"hostname\\":\\"$HOSTNAME\\"}" '
                '> /usr/share/nginx/html/index.html; nginx -g "daemon off;"'],
            port_mappings=[ecs.PortMapping(container_port=80, host_port=0, protocol=ecs.Protocol.TCP)])

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "AppRoutesTable", value=app_routes.table_name)
        CfnOutput(self, "TenantTaskFamily", value=tenant_task_def.family)
        CfnOutput(self, "NlbDnsName", value=nlb.load_balancer_dns_name)
        CfnOutput(self, "CloudFrontDomain", value=distribution.distribution_domain_name)
        CfnOutput(self, "EnvoyHostPort", value=str(envoy_host_port))
        CfnOutput(self, "DnsTarget",
                  value=f"CNAME {domain} -> {distribution.distribution_domain_name}")
