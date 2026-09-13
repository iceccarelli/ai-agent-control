"""0038 — the runtime, as code, with the invariants that keep it single.

The book has never reached a venue because no host it ran on could. This is
that host, generated rather than clicked, so every claim about it is checkable
by a test instead of by memory.

The invariants are not AWS preferences. Each one is a way this book loses
money:

  two tasks           two engines against one account, one liquidation price
  a public IP         an unauthenticated health endpoint on the internet
  ephemeral state     a task that dies holding a hedge and returns amnesiac
  a key in the file   the failure this repository was born from
  a wildcard IAM      a compromised container that can do more than trade
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import aws_stack as aws  # noqa: E402


@pytest.fixture(scope="module")
def template():
    return aws.template()


def resources(template, type_name):
    return {k: v for k, v in template["Resources"].items()
            if v["Type"] == type_name}


def one(template, type_name):
    found = resources(template, type_name)
    assert len(found) == 1, f"{type_name}: {sorted(found)}"
    return next(iter(found.values()))


class TestOneTaskAndOnlyOne:
    def test_the_service_wants_exactly_one(self, template):
        assert one(template, "AWS::ECS::Service")["Properties"]["DesiredCount"] == 1

    def test_a_deploy_never_runs_two(self, template):
        """maximumPercent 100 means the old task stops before the new one
        starts. 200 would put two engines on one account for a minute, which
        is all it takes to open two hedges against one liquidation price."""
        deployment = one(template, "AWS::ECS::Service")["Properties"][
            "DeploymentConfiguration"]
        assert deployment["MaximumPercent"] == 100
        assert deployment["MinimumHealthyPercent"] == 0

    def test_there_is_one_container_and_it_runs_the_book(self, template):
        containers = one(template, "AWS::ECS::TaskDefinition")["Properties"][
            "ContainerDefinitions"]
        assert len(containers) == 1


class TestItIsNotOnTheInternet:
    def test_the_task_gets_no_public_ip(self, template):
        net = one(template, "AWS::ECS::Service")["Properties"][
            "NetworkConfiguration"]["AwsvpcConfiguration"]
        assert net["AssignPublicIp"] == "DISABLED"

    def test_it_runs_in_the_private_subnets(self, template):
        net = one(template, "AWS::ECS::Service")["Properties"][
            "NetworkConfiguration"]["AwsvpcConfiguration"]
        assert [s["Ref"] for s in net["Subnets"]] == ["PrivateSubnetA",
                                                      "PrivateSubnetB"]

    def test_nothing_may_connect_to_it(self, template):
        sg = template["Resources"]["TaskSecurityGroup"]["Properties"]
        assert sg.get("SecurityGroupIngress", []) == []

    def test_it_may_only_speak_https_and_to_its_own_filesystem(self, template):
        egress = template["Resources"]["TaskSecurityGroup"]["Properties"][
            "SecurityGroupEgress"]
        ports = sorted(rule["FromPort"] for rule in egress)
        assert ports == [443, 2049]
        https = [r for r in egress if r["FromPort"] == 443][0]
        assert https["ToPort"] == 443
        nfs = [r for r in egress if r["FromPort"] == 2049][0]
        assert "DestinationSecurityGroupId" in nfs

    def test_there_is_no_load_balancer(self, template):
        for kind in ("AWS::ElasticLoadBalancingV2::LoadBalancer",
                     "AWS::ElasticLoadBalancingV2::Listener"):
            assert resources(template, kind) == {}


class TestTheStateSurvivesTheTask:
    def test_the_ledger_lives_on_efs(self, template):
        container = one(template, "AWS::ECS::TaskDefinition")["Properties"][
            "ContainerDefinitions"][0]
        mount = container["MountPoints"][0]
        assert mount["ContainerPath"] == "/app/state"
        assert mount["ReadOnly"] is False

    def test_the_db_path_is_inside_the_mount(self, template):
        env = {e["Name"]: e["Value"] for e in
               one(template, "AWS::ECS::TaskDefinition")["Properties"][
                   "ContainerDefinitions"][0]["Environment"]}
        assert env["STATE_DB_PATH"].startswith("/app/state/")

    def test_the_filesystem_is_encrypted_and_retained(self, template):
        efs = one(template, "AWS::EFS::FileSystem")
        assert efs["Properties"]["Encrypted"] is True
        assert efs["DeletionPolicy"] == "Retain"

    def test_it_is_mounted_in_both_subnets(self, template):
        assert len(resources(template, "AWS::EFS::MountTarget")) == 2


class TestSecretsAreNeverInTheTemplate:
    def test_the_keys_come_from_secrets_manager(self, template):
        container = one(template, "AWS::ECS::TaskDefinition")["Properties"][
            "ContainerDefinitions"][0]
        names = {s["Name"] for s in container["Secrets"]}
        assert {"BYBIT_API_KEY", "BYBIT_API_SECRET"} <= names
        for secret in container["Secrets"]:
            assert "Ref" in secret["ValueFrom"]        # a parameter, not a value

    def test_no_key_is_in_the_environment_block(self, template):
        env = {e["Name"] for e in
               one(template, "AWS::ECS::TaskDefinition")["Properties"][
                   "ContainerDefinitions"][0]["Environment"]}
        assert not any("SECRET" in name or "API_KEY" in name for name in env)

    def test_the_execution_role_reads_only_those_two_secrets(self, template):
        policy = template["Resources"]["ExecutionRole"]["Properties"][
            "Policies"][0]["PolicyDocument"]["Statement"][0]
        assert policy["Action"] == ["secretsmanager:GetSecretValue"]
        assert [r["Ref"] for r in policy["Resource"]] == ["ApiKeySecretArn",
                                                          "ApiSecretArn"]

    def test_no_role_carries_a_wildcard(self, template):
        for name in ("ExecutionRole", "TaskRole"):
            for policy in template["Resources"][name]["Properties"].get(
                    "Policies", []):
                for statement in policy["PolicyDocument"]["Statement"]:
                    assert "*" not in json.dumps(statement["Action"])
                    if name == "TaskRole":
                        assert "*" not in json.dumps(statement["Resource"])


class TestTheBookStartsSafe:
    def test_it_is_a_carry_book_on_testnet_and_not_paper(self, template):
        env = {e["Name"]: e["Value"] for e in
               one(template, "AWS::ECS::TaskDefinition")["Properties"][
                   "ContainerDefinitions"][0]["Environment"]}
        assert env["BOOK_MODE"] == "carry"
        assert env["USE_TESTNET"] == "1"
        assert env["PAPER_TRADING"] == "0"    # PAPER refuses every carry order

    def test_the_required_carry_keys_are_set(self, template):
        env = {e["Name"]: e for e in
               one(template, "AWS::ECS::TaskDefinition")["Properties"][
                   "ContainerDefinitions"][0]["Environment"]}
        for key in ("CARRY_EXECUTION_MODE", "CARRY_BORROW_APR"):
            assert key in env
            assert "Ref" in env[key]["Value"]          # a parameter

    def test_mainnet_is_not_reachable_by_editing_one_parameter(self, template):
        """USE_TESTNET is a literal in the template, not a parameter: pointing
        this stack at mainnet is a code change and a review, not a deploy-time
        argument."""
        assert "UseTestnet" not in template["Parameters"]

    def test_the_logs_are_kept(self, template):
        group = one(template, "AWS::Logs::LogGroup")["Properties"]
        assert int(group["RetentionInDays"]) >= 30


class TestTheCheckerCanFail:
    """A checklist that cannot fail is decoration."""

    def test_the_generated_template_passes(self, template):
        assert aws.check(template) == []

    @pytest.mark.parametrize("mutate,needle", [
        (lambda t: t["Resources"]["Service"]["Properties"].__setitem__(
            "DesiredCount", 2), "DesiredCount"),
        (lambda t: t["Resources"]["Service"]["Properties"][
            "DeploymentConfiguration"].__setitem__("MaximumPercent", 200),
         "MaximumPercent"),
        (lambda t: t["Resources"]["Service"]["Properties"][
            "NetworkConfiguration"]["AwsvpcConfiguration"].__setitem__(
                "AssignPublicIp", "ENABLED"), "public"),
        (lambda t: t["Resources"]["TaskSecurityGroup"]["Properties"][
            "SecurityGroupEgress"].append(
                {"IpProtocol": "-1", "CidrIp": "0.0.0.0/0", "FromPort": 0,
                 "ToPort": 65535}), "egress"),
        (lambda t: t["Resources"]["TaskDefinition"]["Properties"][
            "ContainerDefinitions"][0]["Environment"].append(
                {"Name": "BYBIT_API_SECRET", "Value": "s3cret"}), "secret"),
        (lambda t: t["Resources"]["TaskDefinition"]["Properties"][
            "ContainerDefinitions"][0].__setitem__("MountPoints", []),
         "state"),
        (lambda t: t["Resources"]["TaskRole"]["Properties"]["Policies"][0][
            "PolicyDocument"]["Statement"][0].__setitem__("Resource", "*"),
         "wildcard"),
    ])
    def test_a_broken_invariant_is_caught(self, mutate, needle):
        broken = aws.template()
        mutate(broken)
        problems = " ".join(aws.check(broken)).lower()
        assert needle.lower() in problems, problems


class TestTheCli:
    def test_it_writes_a_template_and_checks_it(self, tmp_path, capsys):
        out = tmp_path / "stack.json"
        assert aws.main(["--out", str(out)]) == 0
        loaded = json.loads(out.read_text())
        assert loaded["Resources"]["Service"]["Properties"]["DesiredCount"] == 1
        assert "PASS" in capsys.readouterr().out

    def test_check_exits_non_zero_on_a_broken_template(self, tmp_path, capsys):
        broken = aws.template()
        broken["Resources"]["Service"]["Properties"]["DesiredCount"] = 2
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(broken))
        assert aws.main(["--check", str(path)]) == 1
        assert "FAIL" in capsys.readouterr().out

    def test_it_prints_the_one_off_connector_check_command(self, capsys):
        assert aws.main(["--run-task-command"]) == 0
        out = capsys.readouterr().out
        assert "aws ecs run-task" in out
        assert "connector_check.py" in out
        assert "--require-bybit-testnet" in out

    def test_it_arms_nothing(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "tools",
                               "aws_stack.py"), encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("place_order", "place_market", "live_authorized",
                       "LIVE_TRADING_ACK"):
            assert banned not in body


class TestTheImageCanRunTheChecks:
    """The one-off task runs connector_check from inside the VPC, so it has to
    be IN the image. Until 0038 tools/ was excluded entirely."""

    def test_the_dockerfile_ships_the_read_only_tools(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "Dockerfile"),
                  encoding="utf-8") as fh:
            text = fh.read()
        assert "tools/connector_check.py" in text
        assert "tools/session_tail.py" in text

    def test_it_still_does_not_ship_the_tree(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "Dockerfile"),
                  encoding="utf-8") as fh:
            text = fh.read()
        assert "COPY tools/ " not in text and "COPY . " not in text
