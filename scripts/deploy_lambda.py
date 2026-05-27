#!/usr/bin/env python3
"""
Deploy a Lambda container image as a Function URL backend and register
its subdomain mapping into the WebRouter DynamoDB table.

Configuration is read from ../config.ini (next to the project root).
Environment variables (APP_*) take precedence, matching CDK behaviour.
"""
import argparse
import configparser
import os
import random
import string
import sys
import time
from pathlib import Path

import boto3


def _load_config() -> configparser.ConfigParser:
    config_path = Path(__file__).resolve().parent.parent / "config.ini"
    if not config_path.exists():
        sys.exit(
            f"config.ini not found at {config_path}. "
            f"Copy config.ini.example to config.ini and edit it first."
        )
    parser = configparser.ConfigParser()
    parser.read(config_path)
    return parser


def _cfg(parser: configparser.ConfigParser, section: str, key: str, env_var: str) -> str:
    """Get config value, env var wins."""
    if os.getenv(env_var):
        return os.getenv(env_var)
    return parser.get(section, key)


def main() -> None:
    arg_parser = argparse.ArgumentParser(
        description="Deploy a Lambda container image and register a subdomain mapping."
    )
    arg_parser.add_argument("--image", required=True, help="ECR image URI to deploy")
    arg_parser.add_argument(
        "--name",
        help="Function name prefix (defaults to LambdaTest.function_prefix in config.ini)",
    )
    args = arg_parser.parse_args()

    config = _load_config()

    account_id = _cfg(config, "AWS", "account_id", "APP_ACCOUNT_ID")
    region = _cfg(config, "AWS", "region", "APP_REGION")
    stack_name = _cfg(config, "CDK", "stack_name", "APP_STACK_NAME")
    role_name = _cfg(config, "Lambda", "execution_role_name", "APP_LAMBDA_ROLE_NAME")
    table_short_name = _cfg(config, "DynamoDB", "table_name", "APP_DYNAMODB_TABLE")
    dynamodb_table = f"{stack_name}-{table_short_name}"
    domain_name = _cfg(config, "CloudFront", "domain_name", "APP_DOMAIN_NAME").lstrip("*.")

    function_prefix = args.name or _cfg(
        config, "LambdaTest", "function_prefix", "APP_LAMBDA_TEST_FUNCTION_PREFIX"
    )
    random_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    function_name = f"{function_prefix}-{random_suffix}"
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    tags = {
        "project": "sample-web-application-host",
        "environment": "test",
        "managed_by": "script",
    }

    lambda_client = boto3.client("lambda", region_name=region)
    dynamodb_client = boto3.client("dynamodb", region_name=region)
    iam_client = boto3.client("iam")

    print("=" * 50)
    print("🚀 Deploying Lambda function")
    print("=" * 50)
    print()

    start_time = time.time()

    # 0. Ensure IAM role exists
    print("🔍 Checking IAM role...")
    role_check_start = time.time()
    try:
        iam_client.get_role(RoleName=role_name)
        print(f"✅ Role {role_name} exists ({time.time() - role_check_start:.2f}s)")
    except iam_client.exceptions.NoSuchEntityException:
        print(f"⚠️  Role {role_name} missing, creating...")
        iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=(
                '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
                '"Principal":{"Service":"lambda.amazonaws.com"},'
                '"Action":"sts:AssumeRole"}]}'
            ),
            Description="Lambda execution role for test functions",
        )
        iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        print(f"✅ Role {role_name} created ({time.time() - role_check_start:.2f}s)")
    print()

    # 1. Create Lambda function
    print("⚡ Creating Lambda function...")
    create_start = time.time()
    lambda_client.create_function(
        FunctionName=function_name,
        PackageType="Image",
        Code={"ImageUri": args.image},
        Role=role_arn,
        Timeout=30,
        MemorySize=512,
        Description="Container image deployed by deploy_lambda.py",
        Tags=tags,
    )
    print(f"✅ Function created ({time.time() - create_start:.2f}s)")
    print()

    # 2. Wait for function to be active
    print("⏳ Waiting for function to be active...")
    wait_start = time.time()
    lambda_client.get_waiter("function_active").wait(FunctionName=function_name)
    print(f"✅ Function active ({time.time() - wait_start:.2f}s)")
    print()

    # 3. Create Function URL
    print("🌐 Creating Function URL (AWS_IAM auth)...")
    url_start = time.time()
    url_response = lambda_client.create_function_url_config(
        FunctionName=function_name, AuthType="AWS_IAM"
    )
    function_url = url_response["FunctionUrl"]
    print(f"✅ Function URL created ({time.time() - url_start:.2f}s)")
    print()

    # 4. Register subdomain mapping
    print("💾 Checking DynamoDB table...")
    ddb_start = time.time()
    subdomain = function_name
    try:
        dynamodb_client.describe_table(TableName=dynamodb_table)
        dynamodb_client.put_item(
            TableName=dynamodb_table,
            Item={
                "subdomain": {"S": subdomain},
                "target_url": {"S": function_url.rstrip("/")},
            },
        )
        print(f"✅ DynamoDB mapping added ({time.time() - ddb_start:.2f}s)")
        access_url = f"https://{subdomain}.{domain_name}/"
    except dynamodb_client.exceptions.ResourceNotFoundException:
        print(
            f"⚠️  DynamoDB table {dynamodb_table} not found, skipping mapping "
            f"({time.time() - ddb_start:.2f}s)"
        )
        access_url = "N/A (deploy the CDK stack first)"
    print()

    total_time = time.time() - start_time
    print("=" * 50)
    print("✅ Deployment complete")
    print("=" * 50)
    print()
    print(f"  Total time:    {total_time:.2f}s")
    print(f"  Function:      {function_name}")
    print(f"  Subdomain:     {subdomain}")
    print(f"  Function URL:  {function_url}")
    print(f"  Access URL:    {access_url}")
    print(f"  Auth:          AWS_IAM")
    print()


if __name__ == "__main__":
    main()
