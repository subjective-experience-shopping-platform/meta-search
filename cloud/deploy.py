"""Build and deploy the dedicated Lambda, then seed its single S3 catalog if absent.

Run: uv run --with 'boto3[crt]' python cloud/deploy.py
The local MCP never imports boto3 or Docker dependencies.
"""

import argparse
import hashlib
import json
import re
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
sys.path[:0] = [str(ROOT / "src"), str(CLOUD)]


def apply(cf, name, template):
    body = json.dumps(template)
    try:
        stack = cf.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as error:
        if "does not exist" not in str(error):
            raise
        cf.create_stack(StackName=name, TemplateBody=body, Capabilities=["CAPABILITY_IAM"])
        waiter = "stack_create_complete"
    else:
        if stack["StackStatus"] == "ROLLBACK_COMPLETE":
            cf.delete_stack(StackName=name)
            cf.get_waiter("stack_delete_complete").wait(StackName=name)
            return apply(cf, name, template)
        try:
            cf.update_stack(StackName=name, TemplateBody=body, Capabilities=["CAPABILITY_IAM"])
            waiter = "stack_update_complete"
        except ClientError as error:
            if "No updates are to be performed" not in str(error):
                raise
            return cf.describe_stacks(StackName=name)["Stacks"][0]
    cf.get_waiter(waiter).wait(StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 180})
    return cf.describe_stacks(StackName=name)["Stacks"][0]


def storage_template(bucket, repository):
    return {
        "Resources": {
            "Catalog": {
                "Type": "AWS::S3::Bucket",
                "DeletionPolicy": "Retain",
                "Properties": {
                    "BucketName": bucket,
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    },
                    "BucketEncryption": {
                        "ServerSideEncryptionConfiguration": [
                            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    },
                },
            },
            "Repository": {
                "Type": "AWS::ECR::Repository",
                "Properties": {
                    "RepositoryName": repository,
                    "ImageTagMutability": "MUTABLE",
                    "RepositoryPolicyText": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "lambda.amazonaws.com"},
                                "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                            }
                        ],
                    },
                },
            },
        }
    }


def function_template(bucket, image, name):
    def get(key, attr):
        return {"Fn::GetAtt": [key, attr]}

    return {
        "Resources": {
            "Logs": {
                "Type": "AWS::Logs::LogGroup",
                "Properties": {"LogGroupName": "/aws/lambda/" + name, "RetentionInDays": 7},
            },
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "lambda.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                            }
                        ],
                    },
                    "Policies": [
                        {
                            "PolicyName": "CatalogAndLogs",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": ["s3:GetObject", "s3:PutObject"],
                                        "Resource": f"arn:aws:s3:::{bucket}/latest.sqlite",
                                    },
                                    {
                                        "Effect": "Allow",
                                        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                                        "Resource": get("Logs", "Arn"),
                                    },
                                ],
                            },
                        }
                    ],
                },
            },
            "Function": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "FunctionName": name,
                    "PackageType": "Image",
                    "Architectures": ["x86_64"],
                    "Code": {"ImageUri": image},
                    "Role": get("Role", "Arn"),
                    "MemorySize": 3008,
                    "Timeout": 900,
                    "EphemeralStorage": {"Size": 1024},
                    "Environment": {"Variables": {"CATALOG_BUCKET": bucket}},
                },
            },
            "Url": {
                "Type": "AWS::Lambda::Url",
                "Properties": {
                    "TargetFunctionArn": {"Ref": "Function"},
                    "AuthType": "NONE",
                    "InvokeMode": "BUFFERED",
                },
            },
            "UrlPermission": {
                "Type": "AWS::Lambda::Permission",
                "Properties": {
                    "FunctionName": {"Ref": "Function"},
                    "Action": "lambda:InvokeFunctionUrl",
                    "Principal": "*",
                    "FunctionUrlAuthType": "NONE",
                },
            },
            "InvokePermission": {
                "Type": "AWS::Lambda::Permission",
                "Properties": {
                    "FunctionName": {"Ref": "Function"},
                    "Action": "lambda:InvokeFunction",
                    "Principal": "*",
                    "InvokedViaFunctionUrl": True,
                },
            },
        },
        "Outputs": {"Url": {"Value": get("Url", "FunctionUrl")}},
    }


def seed_catalog(s3, bucket, source, settings):
    from handler import merge_units, open_catalog

    from meta_search.db import database, init_schema, transaction
    from meta_search.normalization import export_table, pending_units

    try:
        s3.head_object(Bucket=bucket, Key="latest.sqlite")
        return False
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            raise
    path = CLOUD / ".build/bootstrap.sqlite"
    path.parent.mkdir(exist_ok=True)
    path.unlink(missing_ok=True)
    with database(settings.db_path) as local:
        init_schema(local)
        units = pending_units(local)
        table = export_table(local)
    cloud = open_catalog(path)
    with cloud:
        merge_units(cloud, source, units)
        for a in table["aspects"]:
            cloud.execute(
                "INSERT OR IGNORE INTO aspect_clusters VALUES(?,?,?)",
                (a["aspect_cluster_id"], a["category_id"], a["label"]),
            )
            cloud.execute(
                "INSERT INTO exact_aspect_matches VALUES(?,?,?)",
                (a["category_id"], a["raw_aspect"], a["aspect_cluster_id"]),
            )
        for s in table["states"]:
            cloud.execute(
                "INSERT OR IGNORE INTO state_clusters VALUES(?,?,?)",
                (s["state_cluster_id"], s["aspect_cluster_id"], s["label"]),
            )
            cloud.execute(
                "INSERT INTO exact_state_matches VALUES(?,?,?,?)",
                (s["category_id"], s["aspect_cluster_id"], s["raw_status"], s["state_cluster_id"]),
            )
    cloud.close()
    s3.upload_file(str(path), bucket, "latest.sqlite")
    with database(settings.db_path) as local, transaction(local):
        local.executemany(
            "UPDATE opinion_units SET cloud_synced=1 WHERE unit_id=?", [(u["unit_id"],) for u in units]
        )
    return len(units)


def rename_source(s3, bucket, previous, source):
    """Move the owner's existing inputs to the new identity in the same latest DB."""
    if not previous or previous == source:
        return 0
    path = CLOUD / ".build/rename-source.sqlite"
    path.parent.mkdir(exist_ok=True)
    try:
        s3.download_file(bucket, "latest.sqlite", str(path))
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return 0
        raise
    conn = sqlite3.connect(path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM units WHERE source=?", (previous,)).fetchone()[0]
        if not count:
            return 0
        if conn.execute("SELECT COUNT(*) FROM units WHERE source=?", (source,)).fetchone()[0]:
            raise ValueError(f"이미 데이터가 있는 사용자 식별값입니다: {source}")
        with conn:
            conn.execute(
                "UPDATE units SET source=?,review_id=? || substr(review_id,?) WHERE source=?",
                (source, source, len(previous) + 1, previous),
            )
        assert conn.execute("SELECT COUNT(*) FROM units WHERE source=?", (source,)).fetchone()[0] == count
    finally:
        conn.close()
    s3.upload_file(str(path), bucket, "latest.sqlite")
    return count


def owner_source(whitelist_path, token_path, default_source, alias=None):
    digest = hashlib.sha256(token_path.read_text().strip().encode()).hexdigest()
    whitelist = json.loads(whitelist_path.read_text()) if whitelist_path.exists() else {}
    previous = whitelist.get(digest)
    source = alias if alias is not None else previous or default_source
    if not isinstance(source, str) or not source.strip():
        raise ValueError("사용자 식별값은 비어 있지 않은 문자열이어야 합니다")
    whitelist[digest] = source
    whitelist_path.write_text(json.dumps(whitelist, indent=2) + "\n")
    return source, previous, len(whitelist)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--owner-alias", help="본인 식별값. 생략하면 기존 whitelist 값을 유지합니다")
    args = parser.parse_args()
    from meta_search.config import load_settings

    settings = load_settings(ROOT / "config.toml")
    session = boto3.Session(region_name=args.region)
    identity = session.client("sts").get_caller_identity()
    account = identity["Account"]
    bucket = f"meta-search-clustering-{account}-{args.region}"
    name = "meta-search-clustering"
    token_path = settings.data_dir / settings.remote_normalization["token_file"]
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(48) + "\n")
    token_path.chmod(0o600)
    whitelist_path = CLOUD / "allowed_tokens.json"
    source, previous, whitelist_count = owner_source(
        whitelist_path, token_path, identity["Arn"], args.owner_alias
    )
    cf, ecr, s3 = (session.client(k) for k in ("cloudformation", "ecr", "s3"))
    print("Creating dedicated S3/ECR storage", flush=True)
    apply(cf, name + "-storage", storage_template(bucket, name))
    repository = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]["repositoryUri"]
    image = repository + ":latest"
    auth = ecr.get_authorization_token()["authorizationData"][0]
    import base64

    password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)[1]
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", auth["proxyEndpoint"]],
        input=password,
        text=True,
        check=True,
    )
    print("Building Lambda image (model/dependencies stay in cloud)", flush=True)
    subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "--provenance=false", "-t", image, str(CLOUD)],
        check=True,
    )
    subprocess.run(["docker", "push", image], check=True)
    image_digest = ecr.describe_images(repositoryName=name, imageIds=[{"imageTag": "latest"}])[
        "imageDetails"
    ][0]["imageDigest"]
    # Deploy the resolved digest so updating :latest really updates Lambda. No catalog versions/aliases.
    image_uri = repository + "@" + image_digest
    stack = apply(cf, name, function_template(bucket, image_uri, name))
    url = next(o["OutputValue"] for o in stack["Outputs"] if o["OutputKey"] == "Url")
    text = settings.config_path.read_text()
    text = re.sub(r"(?m)^url = .*$", lambda _: "url = " + json.dumps(url), text)
    settings.config_path.write_text(text)
    renamed = rename_source(s3, bucket, previous, source)
    seeded = seed_catalog(s3, bucket, source, settings)
    report = {
        "function": name,
        "url": url,
        "bucket": bucket,
        "region": args.region,
        "image_uri": image_uri,
        "owner": source,
        "seeded_units": seeded,
        "whitelist_entries": whitelist_count,
        "renamed_units": renamed,
    }
    (CLOUD / ".build/deployment.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
