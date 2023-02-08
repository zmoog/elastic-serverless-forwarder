# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.

import base64
import datetime
import gzip
import hashlib
import importlib
import os
import random
import ssl
import string
import sys
import time
from copy import deepcopy
from io import BytesIO
from typing import Any, Callable, Optional, Union
from unittest import TestCase

import docker
import localstack.utils.aws.aws_stack
import mock
import pytest
from botocore.client import BaseClient as BotoBaseClient
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from docker.models.containers import Container
from elasticsearch import Elasticsearch
from localstack.utils.aws import aws_stack
from localstack.utils.aws import resources as aws_resources
from OpenSSL import crypto as OpenSSLCrypto

from handlers.aws.exceptions import (
    ConfigFileException,
    InputConfigException,
    OutputConfigException,
    ReplayHandlerException,
    TriggerTypeException,
)
from main_aws import handler
from share import Input, json_dumper, json_parser


class ContextMock:
    def __init__(self, remaining_time_in_millis: int = 0):
        self._remaining_time_in_millis = remaining_time_in_millis

    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789:function:elastic-serverless-forwarder"
    aws_request_id = "aws_request_id"
    memory_limit_in_mb = "512"
    function_version = "v0.0.0"

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_time_in_millis


class MockContent:
    SECRETS_MANAGER_MOCK_DATA: dict[str, dict[str, str]] = {
        "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets": {
            "type": "SecretString",
            "data": json_dumper(
                {
                    "url": "mock_elastic_url",
                    "username": "mock_elastic_username",
                    "password": "mock_elastic_password",
                    "empty": "",
                }
            ),
        },
        "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret": {
            "type": "SecretString",
            "data": "mock_plain_text_sqs_arn",
        },
        "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret_not_str_byte": {
            "type": "SecretString",
            "data": b"i am not a string",  # type:ignore
        },
        "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret_not_str_int": {
            "type": "SecretString",
            "data": 2021,  # type:ignore
        },
        "arn:aws:secretsmanager:eu-central-1:123456789:secret:binary_secret": {
            "type": "SecretBinary",
            "data": "bW9ja19uZ2lueC5sb2c=",
        },
        "arn:aws:secretsmanager:eu-central-1:123456789:secret:empty_secret": {"type": "SecretString", "data": ""},
    }

    @staticmethod
    def _get_aws_sm_client(region_name: str) -> mock.MagicMock:
        client = mock.Mock()
        client.get_secret_value = MockContent.get_secret_value
        return client

    @staticmethod
    def get_secret_value(SecretId: str) -> Optional[dict[str, Union[bytes, str]]]:
        secrets = MockContent.SECRETS_MANAGER_MOCK_DATA.get(SecretId)

        if secrets is None:
            raise ClientError(
                {
                    "Error": {
                        "Message": "Secrets Manager can't find the specified secret.",
                        "Code": "ResourceNotFoundException",
                    }
                },
                "GetSecretValue",
            )

        if secrets["type"] == "SecretBinary":
            return {"SecretBinary": base64.b64decode(secrets["data"])}
        elif secrets["type"] == "SecretString":
            return {"SecretString": secrets["data"]}

        return None


_now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
_dummy_lambda_event: dict[str, Any] = {
    "Records": [
        {
            "messageId": "dummy_message_id",
            "receiptHandle": "dummy_receipt_handle",
            "body": json_dumper(
                {
                    "Records": [
                        {
                            "eventVersion": "2.1",
                            "eventSource": "aws:s3",
                            "awsRegion": "eu-central-1",
                            "eventTime": _now,
                            "eventName": "ObjectCreated:Put",
                            "userIdentity": {"principalId": "dummy_principal_id"},
                            "requestParameters": {"sourceIPAddress": "dummy_source_ip_address"},
                            "responseElements": {
                                "x-amz-request-id": "dummy_request_id",
                                "x-amz-id-2": "dummy_request_id_2",
                            },
                            "s3": {
                                "s3SchemaVersion": "1.0",
                                "configurationId": "sqs_event",
                                "bucket": {
                                    "name": "dummy_bucket_name",
                                    "ownerIdentity": {"principalId": "dummy_principal_id"},
                                    "arn": "arn:aws:s3:::dummy_bucket_name",
                                },
                                "object": {
                                    "key": "file.log",
                                    "size": 27,
                                    "eTag": "",
                                    "sequencer": "",
                                },
                            },
                        }
                    ]
                }
            ),
            "attributes": {
                "ApproximateReceiveCount": "1",
                "SentTimestamp": _now,
                "SenderId": "dummy_sender_id",
                "ApproximateFirstReceiveTimestamp": _now,
            },
            "messageAttributes": {
                "config": {
                    "stringValue": "inputs:\n  - type: s3-sqs"
                    "\n    id: arn:aws:sqs:eu-central-1:123456789:sqs-queue\n    outputs:"
                    "\n      - type: elasticsearch\n        args:"
                    "\n          cloud_id: cloud_id:bG9jYWxob3N0OjkyMDAkMA==\n          api_key: api_key\n"
                },
                "originalEventSourceARN": {"stringValue": "arn:aws:sqs:eu-central-1:123456789:sqs-queue"},
                "originalLastEndingOffset": {"stringValue": "32"},
            },
            "md5OfBody": "dummy_hash",
            "eventSource": "aws:sqs",
            "eventSourceARN": "arn:aws:sqs:eu-central-1:123456789:s3-sqs-queue",
            "awsRegion": "eu-central-1",
        }
    ]
}


def _get_queue_url_mock(QueueName: str, QueueOwnerAWSAccountId: str) -> dict[str, Any]:
    return {"QueueUrl": ""}


def _send_message(QueueUrl: str, MessageBody: str, MessageAttributes: dict[str, Any]) -> None:
    pass


_sqs_client_mock = mock.MagicMock()
_sqs_client_mock.get_queue_url = _get_queue_url_mock
_sqs_client_mock.send_message = _send_message


def _head_object(Bucket: str, Key: str) -> dict[str, Any]:
    return {"ContentType": "ContentType", "ContentLength": 0}


def _get_object(Bucket: str, Key: str, Range: str) -> dict[str, Any]:
    content = (
        b"inputs:\n"
        b"  - type: s3-sqs\n"
        b"    id: arn:aws:sqs:eu-central-1:123456789:s3-sqs-queue\n"
        b"    outputs:\n"
        b"      - type: elasticsearch\n"
        b"        args:\n"
        b"          cloud_id: cloud_id:bG9jYWxob3N0OjkyMDAkMA==\n"
        b"          api_key: api_key\n"
        b"  - type: sqs\n"
        b"    id: arn:aws:sqs:eu-central-1:123456789:sqs-queue\n"
        b"    outputs:\n"
        b"      - type: elasticsearch\n"
        b"        args:\n"
        b"          cloud_id: cloud_id:bG9jYWxob3N0OjkyMDAkMA==\n"
        b"          api_key: api_key\n"
        b"  - type: dummy\n"
        b"    id: arn:aws:dummy:eu-central-1:123456789:input\n"
        b"    outputs:\n"
        b"      - type: elasticsearch\n"
        b"        args:\n"
        b"          cloud_id: cloud_id:bG9jYWxob3N0OjkyMDAkMA==\n"
        b"          api_key: api_key\n"
        b"  - type: s3-sqs\n"
        b"    id: arn:aws:sqs:eu-central-1:123456789:s3-sqs-queue-with-dummy-output\n"
        b"    outputs:\n"
        b"      - type: output_type\n"
        b"        args:\n"
        b"          output_arg: output_arg"
    )

    content_body = BytesIO(content)
    content_length = len(content)
    return {"Body": StreamingBody(content_body, content_length), "ContentLength": content_length}


def _download_fileobj(Bucket: str, Key: str, Fileobj: BytesIO) -> None:
    if Key == "please raise":
        raise Exception("raised")


_s3_client_mock = mock.MagicMock()
_s3_client_mock.head_object = _head_object
_s3_client_mock.download_fileobj = _download_fileobj
_s3_client_mock.get_object = _get_object


def _describe_log_streams(*args: Any, **kwargs: Any) -> dict[str, Any]:
    if "nextToken" not in kwargs:
        next_token = "0"
    else:
        next_token = "0" * (len(kwargs["nextToken"]) + 1)

    if len(next_token) > 2:
        log_group_name = kwargs["logGroupName"]

        if kwargs["logStreamNamePrefix"] == "logStreamNotMatching":
            log_stream_name = "let_not_match"
        else:
            log_stream_name = kwargs["logStreamNamePrefix"]

        return {
            "logStreams": [
                {"logStreamName": "string", "arn": "string"},
                {
                    "logStreamName": log_stream_name,
                    "arn": f"arn:aws:logs:us-east-1:000000000000:log-group:{log_group_name}:{log_stream_name}",
                },
            ]
        }

    return {
        "logStreams": [
            {"logStreamName": "another_string", "arn": "another_string"},
            {"logStreamName": "another_string_2", "arn": "another_string_2"},
        ],
        "nextToken": next_token,
    }


_cloudwatch_logs_client = mock.Mock()
_cloudwatch_logs_client.describe_log_streams = _describe_log_streams


def _apm_capture_serverless() -> Any:
    def wrapper(func: Any) -> Any:
        def decorated(*args: Any, **kwds: Any) -> Any:
            return func(*args, **kwds)

        return decorated

    return wrapper


def _get_hex_prefix(src: str) -> str:
    return hashlib.sha3_384(src.encode("UTF8")).hexdigest()


def reload_handlers_aws_handler() -> None:
    os.environ["ELASTIC_APM_ACTIVE"] = "ELASTIC_APM_ACTIVE"
    os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "AWS_LAMBDA_FUNCTION_NAME"

    from handlers.aws.utils import get_cloudwatch_logs_client, get_sqs_client

    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    _ = get_cloudwatch_logs_client()
    _ = get_sqs_client()

    mock.patch("handlers.aws.utils.get_cloudwatch_logs_client", lambda: _cloudwatch_logs_client).start()
    mock.patch("handlers.aws.utils.get_sqs_client", lambda: _sqs_client_mock).start()

    handlers_aws_handler = sys.modules["handlers.aws.handler"]
    importlib.reload(handlers_aws_handler)


def revert_handlers_aws_handler() -> None:
    if "AWS_DEFAULT_REGION" in os.environ:
        del os.environ["AWS_DEFAULT_REGION"]

    if "ELASTIC_APM_ACTIVE" in os.environ:
        del os.environ["ELASTIC_APM_ACTIVE"]

    if "AWS_LAMBDA_FUNCTION_NAME" in os.environ:
        del os.environ["AWS_LAMBDA_FUNCTION_NAME"]

    handlers_aws_handler = sys.modules["handlers.aws.handler"]
    importlib.reload(handlers_aws_handler)


@pytest.mark.unit
class TestTelemetry(TestCase):
    @mock.patch("share.config._available_output_types", new=["elasticsearch", "output_type"])
    @mock.patch(
        "share.config._available_input_types", new=["cloudwatch-logs", "s3-sqs", "sqs", "kinesis-data-stream", "dummy"]
    )
    @mock.patch("handlers.aws.handler.get_sqs_client", lambda: _sqs_client_mock)
    @mock.patch("storage.S3Storage._s3_client", _s3_client_mock)
    @mock.patch("handlers.aws.utils.apm_capture_serverless", _apm_capture_serverless)
    @mock.patch(
        "handlers.aws.utils._available_triggers",
        new={"aws:s3": "s3-sqs", "aws:sqs": "sqs", "aws:kinesis": "kinesis-data-stream", "dummy": "s3-sqs"},
    )
    def test_lambda_telemetry(self) -> None:
        reload_handlers_aws_handler()

        ctx = ContextMock()
        os.environ["S3_CONFIG_FILE"] = "s3://s3_config_file_bucket/s3_config_file_object_key"
        lambda_event = deepcopy(_dummy_lambda_event)
        del lambda_event["Records"][0]["messageAttributes"]["originalEventSourceARN"]
        assert handler(lambda_event, ctx) == "completed"  # type:ignore

        # wait for the telemetry to propagate
        time.sleep(3)


@pytest.mark.unit
class TestLambdaHandlerNoop(TestCase):
    @mock.patch("share.config._available_output_types", new=["elasticsearch", "output_type"])
    @mock.patch(
        "share.config._available_input_types", new=["cloudwatch-logs", "s3-sqs", "sqs", "kinesis-data-stream", "dummy"]
    )
    @mock.patch("handlers.aws.handler.get_sqs_client", lambda: _sqs_client_mock)
    @mock.patch("storage.S3Storage._s3_client", _s3_client_mock)
    @mock.patch("handlers.aws.utils.apm_capture_serverless", _apm_capture_serverless)
    @mock.patch(
        "handlers.aws.utils._available_triggers",
        new={"aws:s3": "s3-sqs", "aws:sqs": "sqs", "aws:kinesis": "kinesis-data-stream", "dummy": "s3-sqs"},
    )
    def test_lambda_handler_noop(self) -> None:
        reload_handlers_aws_handler()

        with self.subTest("no originalEventSourceARN in messageAttributes"):
            ctx = ContextMock()
            os.environ["S3_CONFIG_FILE"] = "s3://s3_config_file_bucket/s3_config_file_object_key"
            lambda_event = deepcopy(_dummy_lambda_event)
            del lambda_event["Records"][0]["messageAttributes"]["originalEventSourceARN"]
            assert handler(lambda_event, ctx) == "completed"  # type:ignore

        with self.subTest("no input defined for cloudwatch_logs"):
            ctx = ContextMock()
            os.environ["S3_CONFIG_FILE"] = "s3://s3_config_file_bucket/s3_config_file_object_key"
            lambda_event = {"awslogs": {"data": json_dumper({"logGroup": "logGroup", "logStream": "logStream"})}}
            assert handler(lambda_event, ctx) == "completed"  # type:ignore

        with self.subTest("output not elasticsearch from payload config"):
            ctx = ContextMock()
            event = {
                "Records": [
                    {
                        "eventSourceARN": "arn:aws:sqs:eu-central-1:123456789:replay-queue",
                        "receiptHandle": "receiptHandle",
                        "body": '{"output_type": "output_type", "output_args": {},'
                        '"event_input_id": "arn:aws:sqs:eu-central-1:123456789:s3-sqs-queue", '
                        '"event_payload": {"_id": "_id"}}',
                        "messageAttributes": {
                            "config": {
                                "stringValue": "inputs:\n"
                                "  - type: s3-sqs\n"
                                "    id: arn:aws:sqs:eu-central-1:123456789:s3-sqs-queue\n"
                                "    outputs:\n"
                                "      - type: output_type\n"
                                "        args:\n"
                                "          output_arg: output_arg"
                            }
                        },
                    }
                ]
            }
            assert handler(event, ctx) == "replayed"  # type:ignore

        with self.subTest("no input defined for cloudwatch_logs in continuing queue"):
            ctx = ContextMock()
            os.environ["S3_CONFIG_FILE"] = "s3://s3_config_file_bucket/s3_config_file_object_key"
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event["Records"][0]["messageAttributes"]["originalEventSourceARN"] = {
                "stringValue": "arn:aws:logs:eu-central-1:123456789:log-group:test-not-existing-esf-loggroup:*"
            }
            assert handler(lambda_event, ctx) == "completed"  # type:ignore

        with self.subTest("no output type elasticsearch in continuing queue"):
            ctx = ContextMock()
            os.environ["S3_CONFIG_FILE"] = "s3://s3_config_file_bucket/s3_config_file_object_key"
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event["Records"][0][
                "eventSourceARN"
            ] = "arn:aws:sqs:eu-central-1:123456789:s3-sqs-queue-with-dummy-output"
            del lambda_event["Records"][0]["messageAttributes"]["originalEventSourceARN"]
            assert handler(lambda_event, ctx) == "completed"  # type:ignore

        with self.subTest("no input type for output type elasticsearch in continuing queue"):
            ctx = ContextMock()
            os.environ["S3_CONFIG_FILE"] = "s3://s3_config_file_bucket/s3_config_file_object_key"
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event["Records"][0]["eventSource"] = "dummy"
            lambda_event["Records"][0]["eventSourceARN"] = "arn:aws:dummy:eu-central-1:123456789:input"
            del lambda_event["Records"][0]["messageAttributes"]["originalEventSourceARN"]
            assert handler(lambda_event, ctx) == "completed"  # type:ignore

        with self.subTest("no input defined for kinesis-data-stream"):
            ctx = ContextMock()
            os.environ["S3_CONFIG_FILE"] = "s3://s3_config_file_bucket/s3_config_file_object_key"
            lambda_event = {
                "Records": [
                    {
                        "eventSource": "aws:kinesis",
                        "kinesis": {"data": ""},
                        "eventSourceARN": "arn:aws:kinesis:eu-central-1:123456789:stream/test-esf-kinesis-stream",
                    }
                ]
            }
            assert handler(lambda_event, ctx) == "completed"  # type:ignore

        with self.subTest("body is neither replay queue nor s3-sqs"):
            ctx = ContextMock()
            os.environ["S3_CONFIG_FILE"] = "s3://s3_config_file_bucket/s3_config_file_object_key"
            os.environ["SQS_REPLAY_URL"] = "https://sqs.us-east-2.amazonaws.com/123456789012/replay_queue"
            os.environ["SQS_CONTINUE_URL"] = "https://sqs.us-east-2.amazonaws.com/123456789012/continue_queue"
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event["Records"][0]["body"] = json_dumper({"Records": [{"key": "value"}]})
            lambda_event["Records"][0]["eventSourceARN"] = "arn:aws:sqs:eu-central-1:123456789:sqs-queue"
            del lambda_event["Records"][0]["messageAttributes"]["originalEventSourceARN"]
            assert handler(lambda_event, ctx) == "completed"  # type:ignore

        with self.subTest("raising cannot find cloudwatch_logs ARN"):
            ctx = ContextMock()
            os.environ["S3_CONFIG_FILE"] = "s3://s3_config_file_bucket/s3_config_file_object_key"
            lambda_event = {
                "awslogs": {"data": json_dumper({"logGroup": "logGroup", "logStream": "logStreamNotMatching"})}
            }
            assert (
                handler(lambda_event, ctx) == "exception raised: "  # type:ignore
                "ValueError('Cannot find cloudwatch log stream ARN')"
            )

        with self.subTest("raising unexpected exception"):
            ctx = ContextMock()
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = "please raise"

            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert handler(lambda_event, ctx) == "exception raised: Exception('raised')"  # type:ignore

        with self.subTest("raising unexpected exception apm client not Nome"):
            with mock.patch("handlers.aws.utils.get_apm_client", lambda: mock.MagicMock()):
                ctx = ContextMock()
                lambda_event = deepcopy(_dummy_lambda_event)
                lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
                lambda_event_body["Records"][0]["s3"]["object"]["key"] = "please raise"

                lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

                assert handler(lambda_event, ctx) == "exception raised: Exception('raised')"  # type:ignore


@pytest.mark.unit
class TestDiscoverIntegrationScope(TestCase):
    def test_discover_integration_scope(self) -> None:
        from handlers.aws.utils import discover_integration_scope

        input_s3 = Input(input_type="s3-sqs", input_id="id", integration_scope_discoverer=discover_integration_scope)

        with self.subTest("discover_integration_scope no integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "generic"

        with self.subTest("discover_integration_scope aws.cloudtrail integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
                "AWSLogs/aws-account-id/CloudTrail/region/"
                "yyyy/mm/dd/aws-account-id_CloudTrail_region_end-time_random-string.log.gz"
            )
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "aws.cloudtrail"

        with self.subTest("discover_integration_scope aws.cloudtrail digest integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
                "AWSLogs/aws-account-id/CloudTrail-Digest/region/"
                "yyyy/mm/dd/aws-account-id_CloudTrail-Digest_region_end-time_random-string.log.gz"
            )
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert (
                input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "aws.cloudtrail-digest"
            )

        with self.subTest("discover_integration_scope aws.cloudtrail insight integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
                "AWSLogs/aws-account-id/CloudTrail-Insight/region/"
                "yyyy/mm/dd/aws-account-id_CloudTrail-Insight_region_end-time_random-string.log.gz"
            )
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "aws.cloudtrail"

        with self.subTest("discover_integration_scope aws.cloudwatch_logs integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = "exportedlogs/111-222-333/2021-12-28/hash/file.gz"
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "aws.cloudwatch_logs"

        with self.subTest("discover_integration_scope aws.elb_logs integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
                "AWSLogs/aws-account-id/elasticloadbalancing/"
                "region/yyyy/mm/dd/"
                "aws-account-id_elasticloadbalancing_region_load-balancer-id_end-time_ip-address_random-string.log.gz"
            )
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "aws.elb_logs"

        with self.subTest("discover_integration_scope aws.firewall_logs integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
                "AWSLogs/aws-account-id/network-firewall/" "log-type/Region/firewall-name/timestamp/"
            )
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "aws.firewall_logs"

        with self.subTest("discover_integration_scope aws.waf integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
                "AWSLogs/account-id/" "WAFLogs/Region/web-acl-name/YYYY/MM/dd/HH/mm"
            )
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "aws.waf"

        with self.subTest("discover_integration_scope aws.vpcflow integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
                "AWSLogs/id/vpcflowlogs/" "region/date_vpcflowlogs_region_file.log.gz"
            )
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "aws.vpcflow"

        with self.subTest("discover_integration_scope unknown integration scope"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = "random_hash"
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "generic"

        with self.subTest("discover_integration_scope records not in event"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            del lambda_event_body["Records"]
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "generic"

        with self.subTest("discover_integration_scope s3 key not in record"):
            lambda_event = {"Records": [{"body": '{"Records": [{}]}'}]}

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "generic"

        with self.subTest("discover_integration_scope empty s3"):
            lambda_event = deepcopy(_dummy_lambda_event)
            lambda_event_body = json_parser(lambda_event["Records"][0]["body"])
            lambda_event_body["Records"][0]["s3"]["object"]["key"] = ""
            lambda_event["Records"][0]["body"] = json_dumper(lambda_event_body)

            assert input_s3.discover_integration_scope(lambda_event=lambda_event, at_record=0) == "generic"


@pytest.mark.unit
class TestLambdaHandlerFailure(TestCase):
    def setUp(self) -> None:
        revert_handlers_aws_handler()

    @mock.patch("share.secretsmanager._get_aws_sm_client", new=MockContent._get_aws_sm_client)
    @mock.patch("handlers.aws.handler.get_sqs_client", lambda: _mock_awsclient(service_name="sqs"))
    def test_lambda_handler_failure(self) -> None:
        dummy_event: dict[str, Any] = {
            "Records": [
                {
                    "eventSource": "aws:sqs",
                    "eventSourceARN": "arn:aws:sqs",
                },
            ]
        }

        event_with_config: dict[str, Any] = {
            "Records": [
                {
                    "messageAttributes": {
                        "config": {"stringValue": "ADD_CONFIG_STRING_HERE", "dataType": "String"},
                        "originalEventSourceARN": {
                            "stringValue": "dummy_aws_sqs",
                            "dataType": "String",
                        },
                    },
                    "md5OfBody": "randomhash",
                    "eventSource": "aws:sqs",
                    "eventSourceARN": "arn:aws:sqs",
                    "awsRegion": "eu-central-1",
                }
            ]
        }

        with self.subTest("Invalid s3 uri apm client not None"):
            with mock.patch("handlers.aws.utils.get_apm_client", lambda: mock.MagicMock()):
                with self.assertRaisesRegex(ConfigFileException, "Invalid s3 uri provided: ``"):
                    os.environ["S3_CONFIG_FILE"] = ""
                    ctx = ContextMock()

                    handler(dummy_event, ctx)  # type:ignore

        with self.subTest("Invalid s3 uri"):
            with self.assertRaisesRegex(ConfigFileException, "Invalid s3 uri provided: ``"):
                os.environ["S3_CONFIG_FILE"] = ""
                ctx = ContextMock()

                handler(dummy_event, ctx)  # type:ignore

        with self.subTest("Invalid s3 uri no bucket and key"):
            with self.assertRaisesRegex(ConfigFileException, "Invalid s3 uri provided: `s3://`"):
                os.environ["S3_CONFIG_FILE"] = "s3://"
                ctx = ContextMock()

                handler(dummy_event, ctx)  # type:ignore

        with self.subTest("Invalid s3 uri no key"):
            with self.assertRaisesRegex(ConfigFileException, "Invalid s3 uri provided: `s3://bucket`"):
                os.environ["S3_CONFIG_FILE"] = "s3://bucket"
                ctx = ContextMock()

                handler(dummy_event, ctx)  # type:ignore

        with self.subTest("no Records in event"):
            with self.assertRaisesRegex(TriggerTypeException, "Not supported trigger"):
                ctx = ContextMock()
                event: dict[str, Any] = {}

                handler(event, ctx)  # type:ignore

        with self.subTest("empty Records in event"):
            with self.assertRaisesRegex(TriggerTypeException, "Not supported trigger"):
                ctx = ContextMock()
                event = {"Records": []}

                handler(event, ctx)  # type:ignore

        with self.subTest("no eventSource in Records in event"):
            with self.assertRaisesRegex(TriggerTypeException, "Not supported trigger"):
                ctx = ContextMock()
                event = {"Records": [{}]}

                handler(event, ctx)  # type:ignore

        with self.subTest("no valid eventSource in Records in event"):
            with self.assertRaisesRegex(TriggerTypeException, "Not supported trigger"):
                ctx = ContextMock()
                event = {"Records": [{"eventSource": "invalid"}]}

                handler(event, ctx)  # type:ignore

        with self.subTest("no eventSource in body Records in event"):
            with self.assertRaisesRegex(TriggerTypeException, "Not supported trigger"):
                ctx = ContextMock()
                event = {"Records": [{"body": ""}]}

                handler(event, ctx)  # type:ignore

        with self.subTest("no valid eventSource in body Records in event"):
            with self.assertRaisesRegex(TriggerTypeException, "Not supported trigger"):
                ctx = ContextMock()
                event = {"Records": [{"body": "", "eventSource": "invalid"}]}

                handler(event, ctx)  # type:ignore

        with self.subTest("empty config in body Records in event"):
            with self.assertRaisesRegex(ConfigFileException, "Empty config"):
                ctx = ContextMock()
                event = {
                    "Records": [
                        {
                            "body": '{"output_type": "", "output_args": "", "event_payload": ""}',
                            "messageAttributes": {"config": {"stringValue": ""}},
                        }
                    ]
                }
                handler(event, ctx)  # type:ignore

        with self.subTest("no valid matching input id from payload config"):
            with self.assertRaisesRegex(InputConfigException, "Cannot load input for input id input_id"):
                ctx = ContextMock()
                event = {
                    "Records": [
                        {
                            "eventSourceARN": "arn:aws:sqs:eu-central-1:123456789:replay-queue",
                            "receiptHandle": "receiptHandle",
                            "body": '{"output_type": "", "output_args": "", "event_input_id": "input_id", '
                            '"event_payload": ""}',
                            "messageAttributes": {
                                "config": {
                                    "stringValue": "inputs:\n"
                                    "  - type: s3-sqs\n"
                                    "    id: arn:aws:sqs:eu-central-1:123456789:s3-sqs-queue\n"
                                    "    outputs:\n"
                                    "      - type: elasticsearch\n"
                                    "        args:\n"
                                    "          cloud_id: cloud_id\n"
                                    "          api_key: api_key"
                                }
                            },
                        }
                    ]
                }
                handler(event, ctx)  # type:ignore

        with self.subTest("no valid matching output id from payload config"):
            with self.assertRaisesRegex(OutputConfigException, "Cannot load output of type output_type"):
                ctx = ContextMock()
                event = {
                    "Records": [
                        {
                            "eventSourceARN": "arn:aws:sqs:eu-central-1:123456789:replay-queue",
                            "receiptHandle": "receiptHandle",
                            "body": '{"output_type": "output_type", "output_args": "", '
                            '"event_input_id": "arn:aws:sqs:eu-central-1:123456789:s3-sqs-queue", '
                            '"event_payload": ""}',
                            "messageAttributes": {
                                "config": {
                                    "stringValue": "inputs:\n"
                                    "  - type: s3-sqs\n"
                                    "    id: arn:aws:sqs:eu-central-1:123456789:s3-sqs-queue\n"
                                    "    outputs:\n"
                                    "      - type: elasticsearch\n"
                                    "        args:\n"
                                    "          cloud_id: cloud_id\n"
                                    "          api_key: api_key"
                                }
                            },
                        }
                    ]
                }
                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: arn format too long"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Invalid arn format: "
                "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret:THIS:IS:INVALID",
            ):
                ctx = ContextMock()
                config_yml: str = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret:THIS:IS:INVALID"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """
                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: empty region"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Must be provided region in arn: " "arn:aws:secretsmanager::123456789:secret:plain_secret",
            ):
                ctx = ContextMock()
                # BEWARE region is empty at id
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager::123456789:secret:plain_secret"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: empty secrets manager name"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Must be provided secrets manager name in arn: "
                "arn:aws:secretsmanager:eu-central-1:123456789:secret:",
            ):
                ctx = ContextMock()
                # BEWARE empty secrets manager name at id
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: cannot use both plain text and key/value pairs"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "You cannot have both plain text and json key for the same "
                "secret: arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username",
            ):
                ctx = ContextMock()
                # BEWARE using es_secrets plain text for elasticsearch_url and es_secrets:username for username
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secrets"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: empty secret key"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Error for secret "
                "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:: key must "
                "not be empty",
            ):
                ctx = ContextMock()
                # BEWARE empty key at elasticsearch_url
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: secret does not exist"):
            with self.assertRaisesRegex(
                ConfigFileException,
                r"An error occurred \(ResourceNotFoundException\) when calling "
                "the GetSecretValue operation: Secrets Manager can't find the specified secret.",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:DOES_NOT_EXIST"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: empty plain secret value"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Error for secret "
                "arn:aws:secretsmanager:eu-central-1:123456789:secret:empty_secret: must "
                "not be empty",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:empty_secret"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: empty key/value secret value"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Error for secret "
                "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:empty: must "
                "not be empty",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:empty"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: plain text used as key/value"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Error for secret "
                "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret:SHOULD_NOT_HAVE_A_KEY: "
                "expected to be keys/values pair",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret:SHOULD_NOT_HAVE_A_KEY"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: key does not exist in secret manager"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Error for secret "
                "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:I_DO_NOT_EXIST: "
                "key not found",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:I_DO_NOT_EXIST"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: plain text secret not str"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Error for secret "
                "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret_not_str_byte: "
                "expected to be a string",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret_not_str_byte"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("invalid secretsmanager: json TypeError risen"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "Expected string or C-contiguous bytes-like object while parsing "
                "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret_not_str_int",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret_not_str_int"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("tags not list"):
            with self.assertRaisesRegex(
                ConfigFileException, "`tags` must be provided as list for input mock_plain_text_sqs_arn"
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret"
                        tags: "tag1"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("each tag must be of type str"):
            with self.assertRaisesRegex(
                ConfigFileException,
                r"Each tag in `tags` must be provided as string for input "
                r"mock_plain_text_sqs_arn, given: \['tag1', 2, 'tag3'\]",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret"
                        tags:
                          - "tag1"
                          - 2
                          - "tag3"
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("expand_event_list_from_field not str"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "`expand_event_list_from_field` must be provided as string for input mock_plain_text_sqs_arn",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret"
                        expand_event_list_from_field: 0
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore

        with self.subTest("json_content_type not valid"):
            with self.assertRaisesRegex(
                ConfigFileException,
                "`json_content_type` must be one of ndjson,single,disabled "
                "for input mock_plain_text_sqs_arn: whatever given",
            ):
                ctx = ContextMock()
                config_yml = """
                    inputs:
                      - type: "s3-sqs"
                        id: "arn:aws:secretsmanager:eu-central-1:123456789:secret:plain_secret"
                        json_content_type: whatever
                        outputs:
                          - type: "elasticsearch"
                            args:
                              elasticsearch_url: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:url"
                              username: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:username"
                              password: "arn:aws:secretsmanager:eu-central-1:123456789:secret:es_secrets:password"
                              es_datastream_name: "logs-redis.log-default"
                """

                event = deepcopy(event_with_config)
                event["Records"][0]["messageAttributes"]["config"]["stringValue"] = config_yml

                handler(event, ctx)  # type:ignore


def _mock_awsclient(service_name: str, region_name: str = "") -> BotoBaseClient:
    if not region_name:
        return aws_stack.connect_to_service(service_name)

    return aws_stack.connect_to_service(service_name, region_name=region_name)


def _wait_for_fingerprint(host: str, port: str) -> str:
    while True:
        try:
            pem_server_certificate: str = ssl.get_server_certificate((host, int(port)))
            openssl_certificate = OpenSSLCrypto.load_certificate(
                OpenSSLCrypto.FILETYPE_PEM, pem_server_certificate.encode("utf-8")
            )
        except Exception:
            time.sleep(1)
        else:
            return str(openssl_certificate.digest("sha256").decode())


def _wait_for_container(container: Container, port: str) -> None:
    while port not in container.ports or len(container.ports[port]) == 0 or "HostPort" not in container.ports[port][0]:
        container.reload()
        time.sleep(1)


def _wait_for_localstack_service(wait_function: Callable[[], None]) -> None:
    while True:
        try:
            wait_function()
        except Exception:
            time.sleep(1)
        else:
            break


def _create_secrets(secret_name: str, secret_data: dict[str, str], localstack_host_port: str) -> Any:
    client = aws_stack.connect_to_service(
        "secretsmanager", region_name="eu-central-1", endpoint_url=f"http://localhost:{localstack_host_port}"
    )
    client.create_secret(Name=secret_name, SecretString=json_dumper(secret_data))

    return client.describe_secret(SecretId=secret_name)["ARN"]


def _upload_content_to_bucket(content: Union[bytes, str], content_type: str, bucket_name: str, key_name: str) -> None:
    client = aws_stack.connect_to_service("s3")

    client.create_bucket(Bucket=bucket_name, ACL="public-read-write")
    client.put_object(Bucket=bucket_name, Key=key_name, Body=content, ContentType=content_type)


def _event_from_sqs_message(queue_attributes: dict[str, Any]) -> tuple[dict[str, Any], str]:
    sqs_client = aws_stack.connect_to_service("sqs")
    collected_messages: list[dict[str, Any]] = []
    sent_timestamp: str = ""

    while True:
        try:
            messages = sqs_client.receive_message(
                QueueUrl=queue_attributes["QueueUrl"], AttributeNames=["All"], MessageAttributeNames=["All"]
            )
            assert "Messages" in messages
            assert len(messages["Messages"]) == 1
            original_message = messages["Messages"][0]

            message: dict[str, Any] = {}
            for key in original_message:
                new_value = deepcopy(original_message[key])
                camel_case_key = "".join([key[0].lower(), key[1:]])
                message[camel_case_key] = new_value

            if "messageAttributes" in message:
                for attribute in message["messageAttributes"]:
                    new_attribute = deepcopy(message["messageAttributes"][attribute])
                    for attribute_key in message["messageAttributes"][attribute]:
                        camel_case_key = "".join([attribute_key[0].lower(), attribute_key[1:]])
                        new_attribute[camel_case_key] = new_attribute[attribute_key]
                        new_attribute[attribute_key] = ""

                    message["messageAttributes"][attribute] = new_attribute

            sent_timestamp = str(int(message["attributes"]["SentTimestamp"]))

            message["eventSource"] = "aws:sqs"
            message["eventSourceARN"] = queue_attributes["QueueArn"]

            collected_messages.append(message)
        except Exception:
            break

    return dict(Records=collected_messages), sent_timestamp


def _create_cloudwatch_logs_stream(group_name: str, stream_name: str) -> Any:
    logs_client = aws_stack.connect_to_service("logs")
    logs_client.create_log_stream(logGroupName=group_name, logStreamName=stream_name)

    return logs_client.describe_log_streams(logGroupName=group_name, logStreamNamePrefix=stream_name)["logStreams"][0]


def _create_cloudwatch_logs_group(group_name: str) -> Any:
    logs_client = aws_stack.connect_to_service("logs")
    logs_client.create_log_group(logGroupName=group_name)


def _event_to_cloudwatch_logs(group_name: str, stream_name: str, messages_body: list[str]) -> None:
    now = int(datetime.datetime.utcnow().strftime("%s")) * 1000
    logs_client = aws_stack.connect_to_service("logs")
    logs_client.put_log_events(
        logGroupName=group_name,
        logStreamName=stream_name,
        logEvents=[
            {"timestamp": now + (n * 1000), "message": message_body} for n, message_body in enumerate(messages_body)
        ],
    )


def _event_from_cloudwatch_logs(group_name: str, stream_name: str) -> tuple[dict[str, Any], list[str], list[int]]:
    logs_client = aws_stack.connect_to_service("logs")
    collected_log_event_ids: list[str] = []
    collected_log_event_timestamp: list[int] = []
    collected_log_events: list[dict[str, Any]] = []

    events = logs_client.get_log_events(logGroupName=group_name, logStreamName=stream_name)

    assert "events" in events
    for event in events["events"]:
        event_id = "".join(random.choices(string.digits, k=56))
        log_event = {
            "id": event_id,
            "timestamp": event["timestamp"],
            "message": event["message"],
        }

        collected_log_events.append(log_event)
        collected_log_event_ids.append(event_id)
        collected_log_event_timestamp.append(int(float(event["timestamp"])))

    data_json = json_dumper(
        {
            "messageType": "DATA_MESSAGE",
            "owner": "000000000000",
            "logGroup": group_name,
            "logStream": stream_name,
            "subscriptionFilters": ["a-subscription-filter"],
            "logEvents": collected_log_events,
        }
    )

    data_gzip = gzip.compress(data_json.encode("UTF-8"))
    data_base64encoded = base64.b64encode(data_gzip)

    return {"awslogs": {"data": data_base64encoded}}, collected_log_event_ids, collected_log_event_timestamp


def _event_from_kinesis_records(records: dict[str, Any], stream_attribute: dict[str, Any]) -> dict[str, Any]:
    assert "Records" in records

    new_records: list[dict[str, Any]] = []
    for original_record in records["Records"]:
        kinesis_record = {}

        for key in original_record:
            new_value = deepcopy(original_record[key])
            camel_case_key = "".join([key[0].lower(), key[1:]])
            if isinstance(new_value, bytes):
                new_value = new_value.decode("utf-8")

            kinesis_record[camel_case_key] = new_value

        new_records.append(
            {
                "kinesis": kinesis_record,
                "eventSource": "aws:kinesis",
                "eventSourceARN": stream_attribute["StreamDescription"]["StreamARN"],
            }
        )

    return dict(Records=new_records)


def _event_to_sqs_message(queue_attributes: dict[str, Any], message_body: str) -> None:
    sqs_client = aws_stack.connect_to_service("sqs")

    sqs_client.send_message(
        QueueUrl=queue_attributes["QueueUrl"],
        MessageBody=message_body,
    )


def _s3_event_to_sqs_message(
    queue_attributes: dict[str, Any], filenames: list[str], single_message: bool = True
) -> None:
    sqs_client = aws_stack.connect_to_service("sqs")

    records = []
    for filename in filenames:
        records.append(
            {
                "eventVersion": "2.1",
                "eventSource": "aws:s3",
                "awsRegion": "eu-central-1",
                "eventTime": "2021-09-08T18:34:25.042Z",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "s3SchemaVersion": "1.0",
                    "configurationId": "test-bucket",
                    "bucket": {
                        "name": "test-bucket",
                        "arn": "arn:aws:s3:::test-bucket",
                    },
                    "object": {
                        "key": f"{filename}",
                    },
                },
            }
        )

    if single_message:
        sqs_client.send_message(
            QueueUrl=queue_attributes["QueueUrl"],
            MessageBody=json_dumper({"Records": records}),
        )
    else:
        for record in records:
            sqs_client.send_message(
                QueueUrl=queue_attributes["QueueUrl"],
                MessageBody=json_dumper({"Records": [record]}),
            )


class IntegrationTestCase(TestCase):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(IntegrationTestCase, self).__init__(*args, **kwargs)

        self._services: list[str] = []
        self._queues: list[dict[str, str]] = []
        self._kinesis_streams: list[str] = []
        self._cloudwatch_logs_groups: list[dict[str, str]] = []
        self._expand_event_list_from_field: str = ""
        self._ssl_fingerprint_mismatch: bool = False

    @staticmethod
    def _create_sqs_queue(queue_name: str) -> dict[str, str]:
        sqs_client = aws_stack.connect_to_service("sqs")
        queue_url = sqs_client.create_queue(QueueName=queue_name)["QueueUrl"]
        queue_arn = sqs_client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]

        return {
            "QueueArn": queue_arn,
            "QueueUrl": queue_url,
            "QueueUrlPath": queue_url.replace(os.environ["SQS_BACKEND"], "https://sqs.us-east-1.amazonaws.com"),
        }

    def setUp(self) -> None:
        revert_handlers_aws_handler()

        docker_client = docker.from_env()
        localstack.utils.aws.aws_stack.BOTO_CLIENTS_CACHE = {}

        self._localstack_container = docker_client.containers.run(
            "localstack/localstack",
            detach=True,
            environment=[f"SERVICES={','.join(self._services)}"],
            ports={"4566/tcp": None},
        )

        _wait_for_container(self._localstack_container, "4566/tcp")

        self._LOCALSTACK_HOST_PORT: str = self._localstack_container.ports["4566/tcp"][0]["HostPort"]

        services_wait_method = {
            "logs": "describe_log_groups",
            "s3": "list_buckets",
            "sqs": "list_queues",
            "secretsmanager": "list_secrets",
            "kinesis": "list_streams",
        }

        self._BACKEND = {}
        for service in self._services:
            backend_env = f"{service.upper()}_BACKEND"
            self._BACKEND[service] = os.environ.get(backend_env, "")
            os.environ[backend_env] = f"http://localhost:{self._LOCALSTACK_HOST_PORT}"
            _wait_for_localstack_service(
                aws_stack.connect_to_service(service_name=service).__getattribute__(services_wait_method[service])
            )

        self._ELASTIC_USER: str = "elastic"
        self._ELASTIC_PASSWORD: str = "password"

        self._secret_arn = _create_secrets(
            "es_secrets",
            {"username": self._ELASTIC_USER, "password": self._ELASTIC_PASSWORD},
            self._LOCALSTACK_HOST_PORT,
        )

        self._elastic_container = docker_client.containers.run(
            "docker.elastic.co/elasticsearch/elasticsearch:7.16.3",
            entrypoint="sleep",
            command="infinity",
            detach=True,
            ports={"9200/tcp": None},
        )

        exit_code, output = self._elastic_container.exec_run(
            cmd="elasticsearch-certutil cert --silent --name localhost --dns localhost --keep-ca-key "
            "--out /usr/share/elasticsearch/elasticsearch-ssl-http.zip --self-signed --ca-pass '' --pass ''"
        )
        assert exit_code == 0

        exit_code, output = self._elastic_container.exec_run(
            cmd="unzip /usr/share/elasticsearch/elasticsearch-ssl-http.zip -d /usr/share/elasticsearch/config/certs/"
        )
        assert exit_code == 0

        self._elastic_container.exec_run(
            cmd="/bin/tini -- /usr/local/bin/docker-entrypoint.sh",
            detach=True,
            environment=[
                "ES_JAVA_OPTS=-Xms1g -Xmx1g",
                f"ELASTIC_PASSWORD={self._ELASTIC_PASSWORD}",
                "xpack.security.enabled=true",
                "discovery.type=single-node",
                "network.bind_host=0.0.0.0",
                "logger.org.elasticsearch=DEBUG",
                "xpack.security.http.ssl.enabled=true",
                "xpack.security.http.ssl.keystore.path=/usr/share/elasticsearch/config/certs/localhost/localhost.p12",
            ],
        )

        _wait_for_container(self._elastic_container, "9200/tcp")

        self._ES_HOST_PORT: str = self._elastic_container.ports["9200/tcp"][0]["HostPort"]

        ssl_assert_fingerprint = _wait_for_fingerprint("localhost", self._ES_HOST_PORT)
        assert len(ssl_assert_fingerprint) > 0

        self._es_client = Elasticsearch(
            hosts=[f"localhost:{self._ES_HOST_PORT}"],
            scheme="https",
            http_auth=(self._ELASTIC_USER, self._ELASTIC_PASSWORD),
            ssl_assert_fingerprint=ssl_assert_fingerprint,
            verify_certs=False,
            timeout=30,
            max_retries=10,
            retry_on_timeout=True,
            raise_on_error=False,
            raise_on_exception=False,
        )

        while not self._es_client.ping():
            time.sleep(1)

        while True:
            cluster_health = self._es_client.cluster.health(wait_for_status="green")
            if "status" in cluster_health and cluster_health["status"] == "green":
                break

            time.sleep(1)

        if self._ssl_fingerprint_mismatch:
            ssl_assert_fingerprint += ":AA"

        self._config_yaml: str = """
            inputs:
        """

        self._kinesis_streams_info = {}
        self._kinesis_client = aws_stack.connect_to_service("kinesis")
        for kinesis_stream in self._kinesis_streams:
            self._kinesis_streams_info[kinesis_stream] = self._kinesis_client.describe_stream(
                StreamName=aws_resources.create_kinesis_stream(kinesis_stream).stream_name
            )

            self._config_yaml += f"""
              - type: "kinesis-data-stream"
                id: "{self._kinesis_streams_info[kinesis_stream]["StreamDescription"]["StreamARN"]}"
                exclude:
                  - "excluded"
                tags:
                  - "tag1"
                  - "tag2"
                  - "tag3"
                outputs:
                  - type: "elasticsearch"
                    args:
                      elasticsearch_url: "https://localhost:{self._ES_HOST_PORT}"
                      ssl_assert_fingerprint: {ssl_assert_fingerprint}
                      username: "{self._secret_arn}:username"
                      password: "{self._secret_arn}:password"
                    """

            if self._expand_event_list_from_field:
                self._config_yaml += f"""
                expand_event_list_from_field: {self._expand_event_list_from_field}
                """

            kinesis_waiter = self._kinesis_client.get_waiter("stream_exists")
            while True:
                try:
                    kinesis_waiter.wait(
                        StreamName=self._kinesis_streams_info[kinesis_stream]["StreamDescription"]["StreamName"]
                    )
                except Exception:
                    time.sleep(1)
                else:
                    break

        self._cloudwatch_logs_groups_info = {}
        for cloudwatch_logs_group in self._cloudwatch_logs_groups:
            _create_cloudwatch_logs_group(group_name=cloudwatch_logs_group["group_name"])
            self._cloudwatch_logs_groups_info[cloudwatch_logs_group["stream_name"]] = _create_cloudwatch_logs_stream(
                group_name=cloudwatch_logs_group["group_name"], stream_name=cloudwatch_logs_group["stream_name"]
            )

            arn_for_input_id: str = self._cloudwatch_logs_groups_info[cloudwatch_logs_group["stream_name"]]["arn"]
            if "input_id_type" in cloudwatch_logs_group and cloudwatch_logs_group["input_id_type"] == "group_arn":
                log_group_arn_components = arn_for_input_id.split(":")
                arn_for_input_id = f"{':'.join(log_group_arn_components[:-2])}:*"

                different_stream_name = f"{cloudwatch_logs_group['stream_name']}-different"
                _create_cloudwatch_logs_stream(
                    group_name=cloudwatch_logs_group["group_name"], stream_name=different_stream_name
                )

            self._config_yaml += f"""
              - type: "cloudwatch-logs"
                id: "{arn_for_input_id}"
                exclude:
                  - "excluded"
                tags:
                  - "tag1"
                  - "tag2"
                  - "tag3"
                outputs:
                  - type: "elasticsearch"
                    args:
                      elasticsearch_url: "https://localhost:{self._ES_HOST_PORT}"
                      ssl_assert_fingerprint: {ssl_assert_fingerprint}
                      username: "{self._secret_arn}:username"
                      password: "{self._secret_arn}:password"
                """

            if self._expand_event_list_from_field:
                self._config_yaml += f"""
                expand_event_list_from_field: {self._expand_event_list_from_field}
                """

        self._queues_info = {}
        for queue in self._queues:
            self._queues_info[queue["name"]] = self._create_sqs_queue(queue["name"])

            if "type" not in queue:
                continue

            self._config_yaml += f"""
              - type: {queue["type"]}
                id: "{self._queues_info[queue["name"]]["QueueArn"]}"
                exclude:
                  - "excluded"
                tags:
                  - "tag1"
                  - "tag2"
                  - "tag3"
                outputs:
                  - type: "elasticsearch"
                    args:
                      elasticsearch_url: "https://localhost:{self._ES_HOST_PORT}"
                      ssl_assert_fingerprint: {ssl_assert_fingerprint}
                      username: "{self._secret_arn}:username"
                      password: "{self._secret_arn}:password"
                    """

            if self._expand_event_list_from_field:
                self._config_yaml += f"""
                expand_event_list_from_field: {self._expand_event_list_from_field}
                """

        self._continuing_queue_info = self._create_sqs_queue(queue_name="continuing-queue")
        self._replay_queue_info = self._create_sqs_queue(queue_name="replay-queue")

        _upload_content_to_bucket(
            content=self._config_yaml,
            content_type="text/plain",
            bucket_name="config-bucket",
            key_name="folder/config.yaml",
        )

        os.environ["S3_CONFIG_FILE"] = "s3://config-bucket/folder/config.yaml"
        os.environ["SQS_CONTINUE_URL"] = self._continuing_queue_info["QueueUrl"]
        os.environ["SQS_REPLAY_URL"] = self._replay_queue_info["QueueUrl"]

    def tearDown(self) -> None:
        for backend_env in self._BACKEND:
            os.environ[backend_env] = self._BACKEND[backend_env]

        del os.environ["S3_CONFIG_FILE"]
        del os.environ["SQS_CONTINUE_URL"]
        del os.environ["SQS_REPLAY_URL"]

        self._elastic_container.stop()
        self._elastic_container.remove()

        self._localstack_container.stop()
        self._localstack_container.remove()


@pytest.mark.integration
class TestLambdaHandlerSuccessMixedInput(IntegrationTestCase):
    def setUp(self) -> None:
        self._services = ["logs", "s3", "sqs", "secretsmanager"]
        self._queues = [
            {"name": "source-s3-sqs-queue", "type": "s3-sqs"},
            {"name": "source-sqs-queue", "type": "sqs"},
            {"name": "source-no-conf-queue"},
        ]
        self._cloudwatch_logs_groups = [
            {"group_name": "source-group", "stream_name": "source-stream", "input_id_type": "group_arn"}
        ]

        super(TestLambdaHandlerSuccessMixedInput, self).setUp()

        self._first_log_entry: str = (
            "{\n"
            '   "@timestamp": "2021-12-28T11:33:08.160Z",\n'
            '   "log.level": "info",\n'
            '   "message": "trigger"\n'
            "}\n"
            "\n"
        )

        self._second_log_entry: str = (
            "{\n"
            '    "ecs": {\n'
            '        "version": "1.6.0"\n'
            "    },\n"
            '    "log": {\n'
            '        "logger": "root",\n'
            '        "origin": {\n'
            '            "file": {\n'
            '                "line": 30,\n'
            '                "name": "handler.py"\n'
            "            },\n"
            '            "function": "lambda_handler"\n'
            "        },\n"
            '        "original": "trigger"\n'
            "    }\n"
            "}\n"
            "\n"
        )

        self._third_log_entry: str = (
            "{\n" '    "another": "continuation",\n' '    "from": "the",\n' '    "continuing": "queue"\n' "}\n" "\n"
        )

        self._cloudwatch_log: str = self._first_log_entry + self._second_log_entry + self._third_log_entry
        self._first_s3_log: str = self._first_log_entry + self._second_log_entry
        self._second_s3_log: str = self._third_log_entry

        _upload_content_to_bucket(
            content=gzip.compress(self._first_s3_log.encode("UTF-8")),
            content_type="application/x-gzip",
            bucket_name="test-bucket",
            key_name="exportedlog/uuid/yyyy-mm-dd-[$LATEST]hash/000000.gz",
        )

        _upload_content_to_bucket(
            content=gzip.compress(self._second_s3_log.encode("UTF-8")),
            content_type="application/x-gzip",
            bucket_name="test-bucket",
            key_name="exportedlog/uuid/yyyy-mm-dd-[$LATEST]hash/000001.gz",
        )

        mock.patch("storage.S3Storage._s3_client", new=_mock_awsclient(service_name="s3")).start()
        mock.patch("handlers.aws.handler.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch("handlers.aws.utils.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch(
            "handlers.aws.utils.get_cloudwatch_logs_client", lambda: _mock_awsclient(service_name="logs")
        ).start()
        mock.patch(
            "share.secretsmanager._get_aws_sm_client",
            lambda region_name: _mock_awsclient(service_name="secretsmanager", region_name=region_name),
        ).start()

    def tearDown(self) -> None:
        super(TestLambdaHandlerSuccessMixedInput, self).tearDown()

    @mock.patch("handlers.aws.handler._completion_grace_period", 1)
    def test_lambda_handler_replay(self) -> None:
        first_filename: str = "exportedlog/uuid/yyyy-mm-dd-[$LATEST]hash/000000.gz"
        second_filename: str = "exportedlog/uuid/yyyy-mm-dd-[$LATEST]hash/000001.gz"

        _s3_event_to_sqs_message(
            queue_attributes=self._queues_info["source-s3-sqs-queue"], filenames=[first_filename, second_filename]
        )
        event_s3, _ = _event_from_sqs_message(queue_attributes=self._queues_info["source-s3-sqs-queue"])
        bucket_arn: str = "arn:aws:s3:::test-bucket"
        event_time = int(
            datetime.datetime.strptime("2021-09-08T18:34:25.042Z", "%Y-%m-%dT%H:%M:%S.%fZ").timestamp() * 1000
        )

        hash_first = _get_hex_prefix(f"{bucket_arn}-{first_filename}")
        hash_second = _get_hex_prefix(f"{bucket_arn}-{second_filename}")
        prefix_s3_first = f"{event_time}-{hash_first}"
        prefix_s3_second = f"{event_time}-{hash_second}"

        _event_to_sqs_message(queue_attributes=self._queues_info["source-sqs-queue"], message_body=self._cloudwatch_log)
        event_sqs, timestamp = _event_from_sqs_message(queue_attributes=self._queues_info["source-sqs-queue"])

        message_id = event_sqs["Records"][0]["messageId"]
        hash_sqs = _get_hex_prefix(f"source-sqs-queue-{message_id}")
        prefix_sqs: str = f"{timestamp}-{hash_sqs}"

        _event_to_cloudwatch_logs(
            group_name="source-group",
            stream_name="source-stream",
            messages_body=[self._first_log_entry + self._second_log_entry],
        )
        (
            event_cloudwatch_logs,
            event_ids_cloudwatch_logs,
            event_timestamps_cloudwatch_logs,
        ) = _event_from_cloudwatch_logs(group_name="source-group", stream_name="source-stream")

        _event_to_cloudwatch_logs(
            group_name="source-group", stream_name="source-stream-different", messages_body=[self._third_log_entry]
        )
        (
            event_cloudwatch_logs_different,
            event_ids_cloudwatch_logs_different,
            event_timestamps_cloudwatch_logs_different,
        ) = _event_from_cloudwatch_logs(group_name="source-group", stream_name="source-stream-different")

        hash_cw_logs = _get_hex_prefix(f"source-group-source-stream-{event_ids_cloudwatch_logs[0]}")
        prefix_cloudwatch_logs = f"{event_timestamps_cloudwatch_logs[0]}-{hash_cw_logs}"

        hash_cw_logs_different = _get_hex_prefix(
            f"source-group-" f"source-stream-different-{event_ids_cloudwatch_logs_different[0]}"
        )
        prefix_cloudwatch_logs_different = f"{event_timestamps_cloudwatch_logs_different[0]}-{hash_cw_logs_different}"

        # Create an expected id for s3-sqs so that es.send will fail
        self._es_client.index(
            index="logs-generic-default",
            op_type="create",
            id=f"{prefix_s3_first}-000000000000",
            document={"@timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        )

        # Create an expected id for sqs so that es.send will fail
        self._es_client.index(
            index="logs-generic-default",
            op_type="create",
            id=f"{prefix_sqs}-000000000000",
            document={"@timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        )

        # Create an expected id for cloudwatch-logs so that es.send will fail
        self._es_client.index(
            index="logs-generic-default",
            op_type="create",
            id=f"{prefix_cloudwatch_logs}-000000000000",
            document={"@timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        )

        self._es_client.indices.refresh(index="logs-generic-default")

        res = self._es_client.search(
            index="logs-generic-default",
            query={
                "ids": {
                    "values": [
                        f"{prefix_s3_first}-000000000098",
                        f"{prefix_sqs}-000000000098",
                        f"{prefix_cloudwatch_logs}-000000000098",
                    ]
                }
            },
        )
        assert res["hits"]["total"] == {"value": 0, "relation": "eq"}

        ctx = ContextMock(remaining_time_in_millis=2)

        first_call = handler(event_s3, ctx)  # type:ignore

        assert first_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")
        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_s3_first}-000000000098"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._second_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 98,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{first_filename}"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{first_filename}"},
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_s3_second}-000000000000"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._third_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{second_filename}"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{second_filename}"},
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        second_call = handler(event_sqs, ctx)  # type:ignore

        assert second_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")
        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_sqs}-000000000098"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._second_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 98,
            "file": {"path": self._queues_info["source-sqs-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "sqs": {"name": "source-sqs-queue", "message_id": message_id}
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_sqs}-000000000399"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._third_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 399,
            "file": {"path": self._queues_info["source-sqs-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "sqs": {"name": "source-sqs-queue", "message_id": message_id}
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        third_call = handler(event_cloudwatch_logs, ctx)  # type:ignore

        assert third_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")
        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_cloudwatch_logs}-000000000098"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._second_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 98,
            "file": {"path": "source-group/source-stream"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "cloudwatch": {
                "log_group": "source-group",
                "log_stream": "source-stream",
                "event_id": event_ids_cloudwatch_logs[0],
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        fourth_call = handler(event_cloudwatch_logs_different, ctx)  # type:ignore

        assert fourth_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")
        res = self._es_client.search(
            index="logs-generic-default",
            query={"ids": {"values": [f"{prefix_cloudwatch_logs_different}-000000000000"]}},
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._third_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": "source-group/source-stream-different"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "cloudwatch": {
                "log_group": "source-group",
                "log_stream": "source-stream-different",
                "event_id": event_ids_cloudwatch_logs_different[0],
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        replayed_events, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        with self.assertRaises(ReplayHandlerException):
            handler(replayed_events, ctx)  # type:ignore

        self._es_client.indices.refresh(index="logs-generic-default")

        # Remove the expected id for s3-sqs so that it can be replayed
        self._es_client.delete_by_query(
            index="logs-generic-default", body={"query": {"ids": {"values": [f"{prefix_s3_first}-000000000000"]}}}
        )

        # Remove the expected id for sqs so that it can be replayed
        self._es_client.delete_by_query(
            index="logs-generic-default", body={"query": {"ids": {"values": [f"{prefix_sqs}-000000000000"]}}}
        )

        # Remove the expected id for cloudwatch logs so that it can be replayed
        self._es_client.delete_by_query(
            index="logs-generic-default",
            body={"query": {"ids": {"values": [f"{prefix_cloudwatch_logs}-000000000000"]}}},
        )

        self._es_client.indices.refresh(index="logs-generic-default")

        ctx = ContextMock(remaining_time_in_millis=0)

        # implicit wait for the message to be back on the queue
        time.sleep(35)
        replayed_events, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        fifth_call = handler(replayed_events, ctx)  # type:ignore

        assert fifth_call == "replayed"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 7

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_s3_first}-000000000000"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._first_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{first_filename}"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{first_filename}"},
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        ctx = ContextMock(remaining_time_in_millis=2)

        # implicit wait for the message to be back on the queue
        time.sleep(35)
        replayed_events, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        sixth_call = handler(replayed_events, ctx)  # type:ignore

        assert sixth_call == "replayed"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 9

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_sqs}-000000000000"]}}
        )
        assert res["hits"]["hits"][0]["_source"]["message"] == self._first_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": self._queues_info["source-sqs-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "sqs": {"name": "source-sqs-queue", "message_id": message_id}
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_cloudwatch_logs}-000000000000"]}}
        )
        assert res["hits"]["hits"][0]["_source"]["message"] == self._first_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {"offset": 0, "file": {"path": "source-group/source-stream"}}
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "cloudwatch": {
                "log_group": "source-group",
                "log_stream": "source-stream",
                "event_id": event_ids_cloudwatch_logs[0],
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

    @mock.patch("handlers.aws.handler._completion_grace_period", 1)
    def test_lambda_handler_continuing(self) -> None:
        first_filename: str = "exportedlog/uuid/yyyy-mm-dd-[$LATEST]hash/000000.gz"
        second_filename: str = "exportedlog/uuid/yyyy-mm-dd-[$LATEST]hash/000001.gz"
        ctx = ContextMock()

        _s3_event_to_sqs_message(
            queue_attributes=self._queues_info["source-s3-sqs-queue"],
            filenames=[first_filename, second_filename],
            single_message=False,
        )

        s3_events, _ = _event_from_sqs_message(queue_attributes=self._queues_info["source-s3-sqs-queue"])
        bucket_arn: str = "arn:aws:s3:::test-bucket"
        event_time = int(
            datetime.datetime.strptime("2021-09-08T18:34:25.042Z", "%Y-%m-%dT%H:%M:%S.%fZ").timestamp() * 1000
        )

        hash_s3_first = _get_hex_prefix(f"{bucket_arn}-{first_filename}")
        hash_s3_second = _get_hex_prefix(f"{bucket_arn}-{second_filename}")
        prefix_s3_first = f"{event_time}-{hash_s3_first}"
        prefix_s3_second = f"{event_time}-{hash_s3_second}"

        _event_to_sqs_message(queue_attributes=self._queues_info["source-sqs-queue"], message_body=self._cloudwatch_log)
        event_sqs, timestamp = _event_from_sqs_message(queue_attributes=self._queues_info["source-sqs-queue"])

        _event_to_sqs_message(
            queue_attributes=self._queues_info["source-no-conf-queue"], message_body=self._cloudwatch_log
        )
        event_no_config, _ = _event_from_sqs_message(queue_attributes=self._queues_info["source-no-conf-queue"])

        message_id = event_sqs["Records"][0]["messageId"]
        hash_sqs = _get_hex_prefix(f"source-sqs-queue-{message_id}")
        prefix_sqs: str = f"{timestamp}-{hash_sqs}"

        _event_to_cloudwatch_logs(
            group_name="source-group",
            stream_name="source-stream",
            messages_body=[self._first_log_entry + self._second_log_entry],
        )
        (
            event_cloudwatch_logs,
            event_ids_cloudwatch_logs,
            event_timestamps_cloudwatch_logs,
        ) = _event_from_cloudwatch_logs(group_name="source-group", stream_name="source-stream")

        _event_to_cloudwatch_logs(
            group_name="source-group", stream_name="source-stream-different", messages_body=[self._third_log_entry]
        )
        (
            event_cloudwatch_logs_different,
            event_ids_cloudwatch_logs_different,
            event_timestamps_cloudwatch_logs_different,
        ) = _event_from_cloudwatch_logs(group_name="source-group", stream_name="source-stream-different")

        hash_logs = _get_hex_prefix(f"source-group-source-stream-{event_ids_cloudwatch_logs[0]}")
        prefix_cloudwatch_logs = f"{event_timestamps_cloudwatch_logs[0]}-{hash_logs}"

        hash_logs_different = _get_hex_prefix(
            f"source-group-" f"source-stream-different-{event_ids_cloudwatch_logs_different[0]}"
        )
        prefix_cloudwatch_logs_different = f"{event_timestamps_cloudwatch_logs_different[0]}-{hash_logs_different}"

        first_call = handler(s3_events, ctx)  # type:ignore

        assert first_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 1

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_s3_first}-000000000000"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._first_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{first_filename}"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{first_filename}"},
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        second_call = handler(event_sqs, ctx)  # type:ignore

        assert second_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 2

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_sqs}-000000000000"]}}
        )
        assert res["hits"]["hits"][0]["_source"]["message"] == self._first_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": self._queues_info["source-sqs-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "sqs": {"name": "source-sqs-queue", "message_id": message_id}
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        third_call = handler(event_cloudwatch_logs, ctx)  # type:ignore

        assert third_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_cloudwatch_logs}-000000000000"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._first_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {"offset": 0, "file": {"path": "source-group/source-stream"}}
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "cloudwatch": {
                "log_group": "source-group",
                "log_stream": "source-stream",
                "event_id": event_ids_cloudwatch_logs[0],
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        fourth_call = handler(event_cloudwatch_logs_different, ctx)  # type:ignore

        assert fourth_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 4

        res = self._es_client.search(
            index="logs-generic-default",
            query={"ids": {"values": [f"{prefix_cloudwatch_logs_different}-000000000000"]}},
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._third_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": "source-group/source-stream-different"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "cloudwatch": {
                "log_group": "source-group",
                "log_stream": "source-stream-different",
                "event_id": event_ids_cloudwatch_logs_different[0],
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        continued_events, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        continued_events["Records"].append(event_no_config["Records"][0])

        fifth_call = handler(continued_events, ctx)  # type:ignore

        assert fifth_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 4

        continued_events, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        sixth_call = handler(continued_events, ctx)  # type:ignore

        assert sixth_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 5

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_s3_first}-000000000098"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._second_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 98,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{first_filename}"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{first_filename}"},
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        ctx = ContextMock(remaining_time_in_millis=2)

        continued_events, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        seventh_call = handler(continued_events, ctx)  # type:ignore

        assert seventh_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 9

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_sqs}-000000000098"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._second_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 98,
            "file": {"path": self._queues_info["source-sqs-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "sqs": {"name": "source-sqs-queue", "message_id": message_id}
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_cloudwatch_logs}-000000000098"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._second_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 98,
            "file": {"path": "source-group/source-stream"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "cloudwatch": {
                "log_group": "source-group",
                "log_stream": "source-stream",
                "event_id": event_ids_cloudwatch_logs[0],
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_s3_second}-000000000000"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._third_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{second_filename}"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{second_filename}"},
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_sqs}-000000000399"]}}
        )

        assert res["hits"]["hits"][0]["_source"]["message"] == self._third_log_entry.rstrip("\n")

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 399,
            "file": {"path": self._queues_info["source-sqs-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "sqs": {"name": "source-sqs-queue", "message_id": message_id}
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]


@pytest.mark.integration
class TestLambdaHandlerSuccessKinesisDataStream(IntegrationTestCase):
    def setUp(self) -> None:
        self._services = ["kinesis", "s3", "sqs", "secretsmanager"]
        self._kinesis_streams = ["source-kinesis"]
        self._expand_event_list_from_field = "logEvents"

        super(TestLambdaHandlerSuccessKinesisDataStream, self).setUp()

        self._first_log_entry = {
            "id": "event_id",
            "timestamp": 1655272038305,
            "message": '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}',
        }
        self._second_log_entry = {
            "id": "event_id",
            "timestamp": 1655272138305,
            "message": '{"ecs": {"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, '
            '"name": "handler.py"}, "function": "lambda_handler"}, "original": "trigger"}',
        }
        self._third_log_entry = {
            "id": "event_id",
            "timestamp": 1655272338305,
            "message": '{"@timestamp": "2022-02-02T12:40:45.690Z", "log.level": "warning", "message": "no namespace '
            'set in config: using `default`", "ecs": {"version": "1.6.0"}}',
        }

        mock.patch("storage.S3Storage._s3_client", _mock_awsclient(service_name="s3")).start()
        mock.patch("handlers.aws.handler.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch("handlers.aws.utils.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch(
            "share.secretsmanager._get_aws_sm_client",
            lambda region_name: _mock_awsclient(service_name="secretsmanager", region_name=region_name),
        ).start()

    def tearDown(self) -> None:
        super(TestLambdaHandlerSuccessKinesisDataStream, self).tearDown()

    def test_lambda_handler_continuing(self) -> None:
        self._kinesis_client.put_records(
            Records=[
                {
                    "PartitionKey": "PartitionKey",
                    "Data": base64.b64encode(
                        json_dumper(
                            {
                                "messageType": "DATA_MESSAGE",
                                "owner": "000000000000",
                                "logGroup": "group_name",
                                "logStream": "stream_name",
                                "subscriptionFilters": ["a-subscription-filter"],
                                "logEvents": [self._first_log_entry, self._second_log_entry],
                            }
                        ).encode("utf-8")
                    ),
                },
                {
                    "PartitionKey": "PartitionKey",
                    "Data": base64.b64encode(
                        json_dumper(
                            {
                                "messageType": "DATA_MESSAGE",
                                "owner": "000000000000",
                                "logGroup": "group_name",
                                "logStream": "stream_name",
                                "subscriptionFilters": ["a-subscription-filter"],
                                "logEvents": [
                                    {
                                        "id": "event_id",
                                        "timestamp": 1655272238305,
                                        "message": '{"excluded": "by filter"}',
                                    },
                                    self._third_log_entry,
                                ],
                            }
                        ).encode("utf-8")
                    ),
                },
            ],
            StreamName=self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
        )

        shards_paginator = self._kinesis_client.get_paginator("list_shards")
        shards_available = [
            shard
            for shard in shards_paginator.paginate(
                StreamName=self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
                ShardFilter={"Type": "FROM_TRIM_HORIZON", "Timestamp": datetime.datetime(2015, 1, 1)},
                PaginationConfig={"MaxItems": 1, "PageSize": 1},
            )
        ]

        assert len(shards_available) == 1 and len(shards_available[0]["Shards"]) == 1

        shard_iterator = self._kinesis_client.get_shard_iterator(
            StreamName=self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
            ShardId=shards_available[0]["Shards"][0]["ShardId"],
            ShardIteratorType="TRIM_HORIZON",
            Timestamp=datetime.datetime(2015, 1, 1),
        )

        records = self._kinesis_client.get_records(ShardIterator=shard_iterator["ShardIterator"], Limit=2)

        ctx = ContextMock()
        kinesis_event = _event_from_kinesis_records(
            records=records, stream_attribute=self._kinesis_streams_info["source-kinesis"]
        )
        timestamp_first = datetime.datetime(2014, 12, 29).timestamp()
        timestamp_second = datetime.datetime(2014, 12, 28).timestamp()
        kinesis_event["Records"][0]["kinesis"]["approximateArrivalTimestamp"] = timestamp_first
        kinesis_event["Records"][1]["kinesis"]["approximateArrivalTimestamp"] = timestamp_second

        first_call = handler(kinesis_event, ctx)  # type:ignore

        assert first_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 1

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 1, "relation": "eq"}

        assert res["hits"]["hits"][0]["_source"]["message"] == json_dumper(self._first_log_entry)

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamARN"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "kinesis": {
                "type": "stream",
                "partition_key": "PartitionKey",
                "name": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
                "sequence_number": kinesis_event["Records"][0]["kinesis"]["sequenceNumber"],
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        second_call = handler(event, ctx)  # type:ignore

        assert second_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 2

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 2, "relation": "eq"}

        assert res["hits"]["hits"][1]["_source"]["message"] == json_dumper(self._second_log_entry)

        assert res["hits"]["hits"][1]["_source"]["log"] == {
            "offset": 285,
            "file": {"path": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamARN"]},
        }
        assert res["hits"]["hits"][1]["_source"]["aws"] == {
            "kinesis": {
                "type": "stream",
                "partition_key": "PartitionKey",
                "name": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
                "sequence_number": kinesis_event["Records"][0]["kinesis"]["sequenceNumber"],
            }
        }
        assert res["hits"]["hits"][1]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][1]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        third_call = handler(event, ctx)  # type:ignore

        assert third_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 2

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        fourth_call = handler(event, ctx)  # type:ignore

        assert fourth_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 3, "relation": "eq"}

        assert res["hits"]["hits"][2]["_source"]["message"] == json_dumper(self._third_log_entry)

        assert res["hits"]["hits"][2]["_source"]["log"] == {
            "offset": 239,
            "file": {"path": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamARN"]},
        }
        assert res["hits"]["hits"][2]["_source"]["aws"] == {
            "kinesis": {
                "type": "stream",
                "partition_key": "PartitionKey",
                "name": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
                "sequence_number": kinesis_event["Records"][1]["kinesis"]["sequenceNumber"],
            }
        }
        assert res["hits"]["hits"][2]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][2]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        fifth_call = handler(event, ctx)  # type:ignore

        assert fifth_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3

        while "NextShardIterator" in records:
            records = self._kinesis_client.get_records(ShardIterator=records["NextShardIterator"], Limit=2)
            assert not records["Records"]
            break

    @mock.patch("handlers.aws.handler._completion_grace_period", 1)
    def test_lambda_handler_replay(self) -> None:
        partition_key: str = "PartitionKey"
        self._kinesis_client.put_records(
            Records=[
                {
                    "PartitionKey": partition_key,
                    "Data": base64.b64encode(
                        json_dumper(
                            {
                                "messageType": "DATA_MESSAGE",
                                "owner": "000000000000",
                                "logGroup": "group_name",
                                "logStream": "stream_name",
                                "subscriptionFilters": ["a-subscription-filter"],
                                "logEvents": [self._first_log_entry, self._second_log_entry],
                            }
                        ).encode("utf-8")
                    ),
                },
                {
                    "PartitionKey": partition_key,
                    "Data": base64.b64encode(
                        json_dumper(
                            {
                                "messageType": "DATA_MESSAGE",
                                "owner": "000000000000",
                                "logGroup": "group_name",
                                "logStream": "stream_name",
                                "subscriptionFilters": ["a-subscription-filter"],
                                "logEvents": [
                                    {
                                        "id": "event_id",
                                        "timestamp": 1655272238305,
                                        "message": '{"excluded": "by filter"}',
                                    },
                                    self._third_log_entry,
                                    {},
                                ],
                            }
                        ).encode("utf-8")
                    ),
                },
            ],
            StreamName=self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
        )

        shards_paginator = self._kinesis_client.get_paginator("list_shards")
        shards_available = [
            shard
            for shard in shards_paginator.paginate(
                StreamName=self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
                ShardFilter={"Type": "FROM_TRIM_HORIZON", "Timestamp": datetime.datetime(2015, 1, 1)},
                PaginationConfig={"MaxItems": 1, "PageSize": 1},
            )
        ]

        assert len(shards_available) == 1 and len(shards_available[0]["Shards"]) == 1

        shard_iterator = self._kinesis_client.get_shard_iterator(
            StreamName=self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
            ShardId=shards_available[0]["Shards"][0]["ShardId"],
            ShardIteratorType="TRIM_HORIZON",
            Timestamp=datetime.datetime(2015, 1, 1),
        )

        records = self._kinesis_client.get_records(ShardIterator=shard_iterator["ShardIterator"], Limit=2)

        ctx = ContextMock(remaining_time_in_millis=2)
        event = _event_from_kinesis_records(
            records=records, stream_attribute=self._kinesis_streams_info["source-kinesis"]
        )
        timestamp_first = datetime.datetime(2014, 12, 29).timestamp()
        timestamp_second = datetime.datetime(2014, 12, 28).timestamp()
        event["Records"][0]["kinesis"]["approximateArrivalTimestamp"] = timestamp_first
        event["Records"][1]["kinesis"]["approximateArrivalTimestamp"] = timestamp_second

        stream_name: str = self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"]

        sequence_number_first_record = event["Records"][0]["kinesis"]["sequenceNumber"]
        hash_first_record = _get_hex_prefix(f"stream-{stream_name}-{partition_key}-{sequence_number_first_record}")
        prefix_first_record: str = f"{int(timestamp_first * 1000)}-{hash_first_record}"

        sequence_number_second_record = event["Records"][1]["kinesis"]["sequenceNumber"]
        hash_second_record = _get_hex_prefix(f"stream-{stream_name}-{partition_key}-{sequence_number_second_record}")
        prefix_second_record: str = f"{int(timestamp_second * 1000)}-{hash_second_record}"

        # Create an expected id so that es.send will fail
        self._es_client.index(
            index="logs-generic-default",
            op_type="create",
            id=f"{prefix_first_record}-000000000285",
            document={"@timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        )
        self._es_client.indices.refresh(index="logs-generic-default")

        res = self._es_client.search(
            index="logs-generic-default", query={"ids": {"values": [f"{prefix_first_record}-000000000000"]}}
        )

        assert res["hits"]["total"] == {"value": 0, "relation": "eq"}

        first_call = handler(event, ctx)  # type:ignore

        assert first_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")
        res = self._es_client.search(
            index="logs-generic-default",
            query={"ids": {"values": [f"{prefix_first_record}-000000000000", f"{prefix_second_record}-000000000160"]}},
        )

        assert res["hits"]["total"] == {"value": 2, "relation": "eq"}

        assert res["hits"]["hits"][0]["_source"]["message"] == json_dumper(self._first_log_entry)

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamARN"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "kinesis": {
                "type": "stream",
                "partition_key": partition_key,
                "name": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
                "sequence_number": event["Records"][0]["kinesis"]["sequenceNumber"],
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        assert res["hits"]["hits"][1]["_source"]["message"] == json_dumper(self._third_log_entry)

        assert res["hits"]["hits"][1]["_source"]["log"] == {
            "offset": 160,
            "file": {"path": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamARN"]},
        }
        assert res["hits"]["hits"][1]["_source"]["aws"] == {
            "kinesis": {
                "type": "stream",
                "partition_key": partition_key,
                "name": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
                "sequence_number": event["Records"][1]["kinesis"]["sequenceNumber"],
            }
        }
        assert res["hits"]["hits"][1]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][1]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        while "NextShardIterator" in records:
            records = self._kinesis_client.get_records(ShardIterator=records["NextShardIterator"], Limit=2)
            assert not records["Records"]
            break

        replay_event, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)

        with self.assertRaises(ReplayHandlerException):
            handler(replay_event, ctx)  # type:ignore

        # Remove the expected id so that it can be replayed
        self._es_client.delete_by_query(
            index="logs-generic-default",
            body={"query": {"ids": {"values": [f"{prefix_first_record}-000000000285"]}}},
        )
        self._es_client.indices.refresh(index="logs-generic-default")

        # implicit wait for the message to be back on the queue
        time.sleep(35)
        replay_event, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        third_call = handler(replay_event, ctx)  # type:ignore

        assert third_call == "replayed"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 3, "relation": "eq"}

        assert res["hits"]["hits"][2]["_source"]["message"] == json_dumper(self._second_log_entry)

        assert res["hits"]["hits"][2]["_source"]["log"] == {
            "offset": 285,
            "file": {"path": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamARN"]},
        }
        assert res["hits"]["hits"][2]["_source"]["aws"] == {
            "kinesis": {
                "type": "stream",
                "partition_key": partition_key,
                "name": self._kinesis_streams_info["source-kinesis"]["StreamDescription"]["StreamName"],
                "sequence_number": event["Records"][0]["kinesis"]["sequenceNumber"],
            }
        }
        assert res["hits"]["hits"][2]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][2]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]


@pytest.mark.integration
class TestLambdaHandlerSuccessS3SQS(IntegrationTestCase):
    def setUp(self) -> None:
        self._services = ["s3", "sqs", "secretsmanager"]
        self._queues = [{"name": "source-queue", "type": "s3-sqs"}]

        super(TestLambdaHandlerSuccessS3SQS, self).setUp()

        self._first_cloudtrail_record: bytes = (
            b"{\n"
            b'    "eventVersion": "1.0",\n'
            b'    "userIdentity": {\n'
            b'        "type": "IAMUser",\n'
            b'        "principalId": "EX_PRINCIPAL_ID",\n'
            b'        "arn": "arn:aws:iam::123456789012:user/Alice",\n'
            b'        "accessKeyId": "EXAMPLE_KEY_ID",\n'
            b'        "accountId": "123456789012",\n'
            b'        "userName": "Alice"\n'
            b"    },\n"
            b'    "eventTime": "2014-03-06T21:22:54Z",\n'
            b'    "eventSource": "ec2.amazonaws.com",\n'
            b'    "eventName": "StartInstances",\n'
            b'    "awsRegion": "us-east-2",\n'
            b'    "sourceIPAddress": "205.251.233.176",\n'
            b'    "userAgent": "ec2-api-tools 1.6.12.2",\n'
            b'    "requestParameters": {"instancesSet": {"items": [{"instanceId": "i-ebeaf9e2"}]}},\n'
            b'    "responseElements": {"instancesSet": {"items": [{\n'
            b'        "instanceId": "i-ebeaf9e2",\n'
            b'        "currentState": {\n'
            b'            "code": 0,\n'
            b'            "name": "pending"\n'
            b"        },\n"
            b'        "previousState": {\n'
            b'            "code": 80,\n'
            b'            "name": "stopped"\n'
            b"        }\n"
            b"    }]}}\n"
            b"}\n"
        )

        self._second_cloudtrail_record: bytes = (
            b"{\n"
            b'    "eventVersion": "1.0",\n'
            b'    "userIdentity": {\n'
            b'        "type": "IAMUser",\n'
            b'        "principalId": "EX_PRINCIPAL_ID",\n'
            b'        "arn": "arn:aws:iam::123456789012:user/Alice",\n'
            b'        "accountId": "123456789012",\n'
            b'        "accessKeyId": "EXAMPLE_KEY_ID",\n'
            b'        "userName": "Alice",\n'
            b'        "sessionContext": {"attributes": {\n'
            b'            "mfaAuthenticated": "false",\n'
            b'            "creationDate": "2014-03-25T18:45:11Z"\n'
            b"        }}\n"
            b"    },\n"
            b'    "eventTime": "2014-03-25T21:08:14Z",\n'
            b'    "eventSource": "iam.amazonaws.com",\n'
            b'    "eventName": "AddUserToGroup",\n'
            b'    "awsRegion": "us-east-2",\n'
            b'    "sourceIPAddress": "127.0.0.1",\n'
            b'    "userAgent": "AWSConsole",\n'
            b'    "requestParameters": {\n'
            b'        "userName": "Bob",\n'
            b'        "groupName": "admin"\n'
            b"    },\n"
            b'    "responseElements": null\n'
            b"}\n"
        )

        self._third_cloudtrail_record: bytes = (
            b"{\n"
            b'    "eventVersion": "1.04",\n'
            b'    "userIdentity": {\n'
            b'        "type": "IAMUser",\n'
            b'        "principalId": "EX_PRINCIPAL_ID",\n'
            b'        "arn": "arn:aws:iam::123456789012:user/Alice",\n'
            b'        "accountId": "123456789012",\n'
            b'        "accessKeyId": "EXAMPLE_KEY_ID",\n'
            b'        "userName": "Alice"\n'
            b"    },\n"
            b'    "eventTime": "2016-07-14T19:15:45Z",\n'
            b'    "eventSource": "cloudtrail.amazonaws.com",\n'
            b'    "eventName": "UpdateTrail",\n'
            b'    "awsRegion": "us-east-2",\n'
            b'    "sourceIPAddress": "205.251.233.182",\n'
            b'    "userAgent": "aws-cli/1.10.32 Python/2.7.9 Windows/7 botocore/1.4.22",\n'
            b'    "errorCode": "TrailNotFoundException",\n'
            b'    "errorMessage": "Unknown trail: myTrail2 for the user: 123456789012",\n'
            b'    "requestParameters": {"name": "myTrail2"},\n'
            b'    "responseElements": null,\n'
            b'    "requestID": "5d40662a-49f7-11e6-97e4-d9cb6ff7d6a3",\n'
            b'    "eventID": "b7d4398e-b2f0-4faa-9c76-e2d316a8d67f",\n'
            b'    "eventType": "AwsApiCall",\n'
            b'    "recipientAccountId": "123456789012"\n'
            b"}\n"
        )

        self._fourth_cloudtrail_record: bytes = (
            b"{\n"
            b'     "eventVersion": "1.0",\n'
            b'     "userIdentity": {\n'
            b'         "type": "IAMUser",\n'
            b'         "principalId": "EX_PRINCIPAL_ID",\n'
            b'         "arn": "arn:aws:iam::123456789012:user/Alice",\n'
            b'         "accountId": "123456789012",\n'
            b'         "accessKeyId": "EXAMPLE_KEY_ID",\n'
            b'         "userName": "Alice"\n'
            b"     },\n"
            b'     "eventTime": "2014-03-25T20:17:37Z",\n'
            b'     "eventSource": "iam.amazonaws.com",\n'
            b'     "eventName": "CreateRole",\n'
            b'     "awsRegion": "us-east-2",\n'
            b'     "sourceIPAddress": "127.0.0.1",\n'
            b'     "userAgent": "aws-cli/1.3.2 Python/2.7.5 Windows/7",\n'
            b'     "requestParameters": {\n'
            b'         "assumeRolePolicyDocument": "{\\n  \\"Version\\": \\">2012-10-17\\",\\n  \\"Statement\\": [\\n'
            b'    {\\n      \\"Sid\\": \\"\\",     \\n\\"Effect\\": \\"Allow\\",\\n      \\"Principal\\": {\\n'
            b'     \\"AWS\\": \\n\\"arn:aws:iam::210987654321:root\\"\\n      },\\n      \\"Action\\":'
            b' \\"sts:AssumeRole\\"\\n    }\\n  ]\\n}",'
            b'         "roleName": "TestRole"\n'
            b"     },\n"
            b'     "responseElements": {\n'
            b'         "role": {\n'
            b'          "assumeRolePolicyDocument": "%7B%0A%20%20%22Version%22%3A%20%222012-10-17%22%2C%0A%20%20%22'
            b"Statement%22%3A%20%5B%0A%20%20%20%20%7B%0A%20%20%20%20%20%20%22Sid%22%3A%20%22%22%2C%0A%20%20%20%20%20%20"
            b"%22Effect%22%3A%20%22Allow%22%2C%0A%20%20%20%20%20%20%22Principal%22%3A%20%7B%0A%20%20%20%20%20%20%20%20"
            b"%22AWS%22%3A%20%22arn%3Aaws%3Aiam%3A%3A803981987763%3Aroot%22%0A%20%20%20%20%20%20%7D%2C%0A%20%20%20%20"
            b'%20%20%22Action%22%3A%20%22sts%3AAssumeRole%22%0A%20%20%20%20%7D%0A%20%20%5D%0A%7D",\n'
            b'          "roleName": "TestRole",\n'
            b'          "roleId": "AROAIUU2EOWSWPGX2UJUO",\n'
            b'          "arn": "arn:aws:iam::123456789012:role/TestRole",\n'
            b'          "createDate": "Mar 25, 2014 8:17:37 PM",\n'
            b'          "path": "/excluded"\n'
            b"         }\n"
            b"     }\n"
            b"}\n"
        )

        self._fifth_cloudtrail_record: bytes = (
            b"[{\n"
            b'    "eventVersion": "1.0",\n'
            b'    "userIdentity": {\n'
            b'        "type": "IAMUser",\n'
            b'        "principalId": "EX_PRINCIPAL_ID",\n'
            b'        "arn": "arn:aws:iam::123456789012:user/Alice",\n'
            b'        "accountId": "123456789012",\n'
            b'        "accessKeyId": "EXAMPLE_KEY_ID",\n'
            b'        "userName": "Alice"\n'
            b"    },\n"
            b'    "eventTime": "2014-03-24T21:11:59Z",\n'
            b'    "eventSource": "iam.amazonaws.com",\n'
            b'    "eventName": "CreateUser",\n'
            b'    "awsRegion": "us-east-2",\n'
            b'    "sourceIPAddress": "127.0.0.1",\n'
            b'    "userAgent": "aws-cli/1.3.2 Python/2.7.5 Windows/7",\n'
            b'    "requestParameters": {"userName": "Bob"},\n'
            b'    "responseElements": {"user": {\n'
            b'        "createDate": "Mar 24, 2014 9:11:59 PM",\n'
            b'        "userName": "Bob",\n'
            b'        "arn": "arn:aws:iam::123456789012:user/Bob",\n'
            b'        "path": "/",\n'
            b'        "userId": "EXAMPLEUSERID"\n'
            b"    }}\n"
            b"}]\n"
        )

        cloudtrail_log: bytes = (
            b'{"Records": [' + self._first_cloudtrail_record + b",\n" + self._second_cloudtrail_record + b"]}\n"
            b'{"Records": [' + self._third_cloudtrail_record + b",\n" + self._fourth_cloudtrail_record + b"]}\n"
            b'{"Records": []}\n'
            b'{"Records": [' + self._fifth_cloudtrail_record + b"]}\n"
        )

        _upload_content_to_bucket(
            content=gzip.compress(cloudtrail_log),
            content_type="application/x-gzip",
            bucket_name="test-bucket",
            key_name="AWSLogs/aws-account-id/CloudTrail/region/yyyy/mm/dd/"
            "aws-account-id_CloudTrail_region_end-time_random-string.log.gz",
        )

        cloudtrail_digest_log_with_exclude: bytes = (
            b'{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}\n\n{"excluded": '
            b'"by filter"}\n{"ecs": {"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, '
            b'"name": "handler.py"}, "function": "lambda_handler"}, "original": "trigger"}}\n{"another": '
            b'"continuation", "from": "the", "continuing": "queue"}\n{"replayed": "as well"}'
        )

        _upload_content_to_bucket(
            content=gzip.compress(cloudtrail_digest_log_with_exclude),
            content_type="application/x-gzip",
            bucket_name="test-bucket",
            key_name="AWSLogs/aws-account-id/CloudTrail-Digest/region/yyyy/mm/dd/"
            "aws-account-id_CloudTrail-Digest_region_end-time_random-string.log.gz",
        )

        mock.patch("storage.S3Storage._s3_client", _mock_awsclient(service_name="s3")).start()
        mock.patch("handlers.aws.handler.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch("handlers.aws.utils.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch(
            "share.secretsmanager._get_aws_sm_client",
            lambda region_name: _mock_awsclient(service_name="secretsmanager", region_name=region_name),
        ).start()

    def tearDown(self) -> None:
        super(TestLambdaHandlerSuccessS3SQS, self).tearDown()

    @mock.patch("handlers.aws.handler._completion_grace_period", 1)
    def test_lambda_handler_replay(self) -> None:
        filename: str = (
            "AWSLogs/aws-account-id/CloudTrail-Digest/region/yyyy/mm/dd/"
            "aws-account-id_CloudTrail-Digest_region_end-time_random-string.log.gz"
        )

        event_time = int(
            datetime.datetime.strptime("2021-09-08T18:34:25.042Z", "%Y-%m-%dT%H:%M:%S.%fZ").timestamp() * 1000
        )

        hash_s3 = _get_hex_prefix(f"arn:aws:s3:::test-bucket-{filename}")
        prefix_s3: str = f"{event_time}-{hash_s3}"
        # Create an expected id so that es.send will fail
        self._es_client.index(
            index="logs-aws.cloudtrail-default",
            op_type="create",
            id=f"{prefix_s3}-000000000000",
            document={"@timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        )
        self._es_client.index(
            index="logs-aws.cloudtrail-default",
            op_type="create",
            id=f"{prefix_s3}-000000000345",
            document={"@timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        )
        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")

        ctx = ContextMock(remaining_time_in_millis=2)

        _s3_event_to_sqs_message(queue_attributes=self._queues_info["source-queue"], filenames=[filename])
        event, _ = _event_from_sqs_message(queue_attributes=self._queues_info["source-queue"])

        first_call = handler(event, ctx)  # type:ignore

        assert first_call == "completed"

        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")

        res = self._es_client.search(
            index="logs-aws.cloudtrail-default",
            query={"ids": {"values": [f"{prefix_s3}-000000000113", f"{prefix_s3}-000000000279"]}},
        )
        assert res["hits"]["total"] == {"value": 2, "relation": "eq"}

        assert (
            res["hits"]["hits"][0]["_source"]["message"]
            == '{"ecs": {"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, "name": '
            '"handler.py"}, "function": "lambda_handler"}, "original": "trigger"}}'
        )

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 113,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{filename}"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{filename}"},
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "aws-cloudtrail", "tag1", "tag2", "tag3"]

        assert (
            res["hits"]["hits"][1]["_source"]["message"]
            == '{"another": "continuation", "from": "the", "continuing": "queue"}'
        )

        assert res["hits"]["hits"][1]["_source"]["log"] == {
            "offset": 279,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{filename}"},
        }
        assert res["hits"]["hits"][1]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{filename}"},
            },
        }
        assert res["hits"]["hits"][1]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][1]["_source"]["tags"] == ["forwarded", "aws-cloudtrail", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        with self.assertRaises(ReplayHandlerException):
            handler(event, ctx)  # type:ignore

        # Remove the expected ids so that they can be replayed
        self._es_client.delete_by_query(
            index="logs-aws.cloudtrail-default",
            body={"query": {"ids": {"values": [f"{prefix_s3}-000000000000", f"{prefix_s3}-000000000345"]}}},
        )
        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")

        # implicit wait for the message to be back on the queue
        time.sleep(35)
        event, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        third_call = handler(event, ctx)  # type:ignore

        assert third_call == "replayed"

        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")
        assert self._es_client.count(index="logs-aws.cloudtrail-default")["count"] == 4

        res = self._es_client.search(index="logs-aws.cloudtrail-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 4, "relation": "eq"}
        assert (
            res["hits"]["hits"][2]["_source"]["message"]
            == '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}'
        )

        assert res["hits"]["hits"][2]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{filename}"},
        }
        assert res["hits"]["hits"][2]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{filename}"},
            }
        }
        assert res["hits"]["hits"][2]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][2]["_source"]["tags"] == ["forwarded", "aws-cloudtrail", "tag1", "tag2", "tag3"]

        assert res["hits"]["hits"][3]["_source"]["message"] == '{"replayed": "as well"}'

        assert res["hits"]["hits"][3]["_source"]["log"] == {
            "offset": 345,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{filename}"},
        }
        assert res["hits"]["hits"][2]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{filename}"},
            }
        }
        assert res["hits"]["hits"][3]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][3]["_source"]["tags"] == ["forwarded", "aws-cloudtrail", "tag1", "tag2", "tag3"]

    def test_lambda_handler_continuing(self) -> None:
        filename: str = (
            "AWSLogs/aws-account-id/CloudTrail/region/yyyy/mm/dd/"
            "aws-account-id_CloudTrail_region_end-time_random-string.log.gz"
        )

        ctx = ContextMock()
        _s3_event_to_sqs_message(queue_attributes=self._queues_info["source-queue"], filenames=[filename])
        event, _ = _event_from_sqs_message(queue_attributes=self._queues_info["source-queue"])

        first_call = handler(event, ctx)  # type:ignore

        assert first_call == "continuing"

        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")
        assert self._es_client.count(index="logs-aws.cloudtrail-default")["count"] == 1

        res = self._es_client.search(index="logs-aws.cloudtrail-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 1, "relation": "eq"}

        assert res["hits"]["hits"][0]["_source"]["message"] == json_dumper(json_parser(self._first_cloudtrail_record))

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{filename}"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{filename}"},
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "aws-cloudtrail", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        second_call = handler(event, ctx)  # type:ignore

        assert second_call == "continuing"

        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")
        assert self._es_client.count(index="logs-aws.cloudtrail-default")["count"] == 2

        res = self._es_client.search(index="logs-aws.cloudtrail-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 2, "relation": "eq"}

        assert res["hits"]["hits"][1]["_source"]["message"] == json_dumper(json_parser(self._second_cloudtrail_record))

        assert res["hits"]["hits"][1]["_source"]["log"] == {
            "offset": 837,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{filename}"},
        }
        assert res["hits"]["hits"][1]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{filename}"},
            }
        }
        assert res["hits"]["hits"][1]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][1]["_source"]["tags"] == ["forwarded", "aws-cloudtrail", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        third_call = handler(event, ctx)  # type:ignore

        assert third_call == "continuing"

        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")
        assert self._es_client.count(index="logs-aws.cloudtrail-default")["count"] == 3

        res = self._es_client.search(index="logs-aws.cloudtrail-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 3, "relation": "eq"}

        assert res["hits"]["hits"][2]["_source"]["message"] == json_dumper(json_parser(self._third_cloudtrail_record))

        assert res["hits"]["hits"][2]["_source"]["log"] == {
            "offset": 1674,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{filename}"},
        }
        assert res["hits"]["hits"][2]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{filename}"},
            }
        }
        assert res["hits"]["hits"][2]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][2]["_source"]["tags"] == ["forwarded", "aws-cloudtrail", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        fourth_call = handler(event, ctx)  # type:ignore

        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")
        assert self._es_client.count(index="logs-aws.cloudtrail-default")["count"] == 3

        assert fourth_call == "continuing"

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        fifth_call = handler(event, ctx)  # type:ignore

        assert fifth_call == "continuing"

        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")
        assert self._es_client.count(index="logs-aws.cloudtrail-default")["count"] == 4

        res = self._es_client.search(index="logs-aws.cloudtrail-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 4, "relation": "eq"}

        assert res["hits"]["hits"][3]["_source"]["message"] == json_dumper(json_parser(self._fifth_cloudtrail_record))

        assert res["hits"]["hits"][3]["_source"]["log"] == {
            "offset": 4325,
            "file": {"path": f"https://test-bucket.s3.eu-central-1.amazonaws.com/{filename}"},
        }
        assert res["hits"]["hits"][3]["_source"]["aws"] == {
            "s3": {
                "bucket": {"name": "test-bucket", "arn": "arn:aws:s3:::test-bucket"},
                "object": {"key": f"{filename}"},
            }
        }
        assert res["hits"]["hits"][3]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "eu-central-1",
        }

        assert res["hits"]["hits"][3]["_source"]["tags"] == ["forwarded", "aws-cloudtrail", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        sixth_call = handler(event, ctx)  # type:ignore

        assert sixth_call == "completed"

        self._es_client.indices.refresh(index="logs-aws.cloudtrail-default")
        assert self._es_client.count(index="logs-aws.cloudtrail-default")["count"] == 4


@pytest.mark.integration
class TestLambdaHandlerSuccessSQS(IntegrationTestCase):
    def setUp(self) -> None:
        self._services = ["s3", "sqs", "secretsmanager"]
        self._queues = [{"name": "source-queue", "type": "sqs"}]
        self._expand_event_list_from_field = "notExistingField"

        super(TestLambdaHandlerSuccessSQS, self).setUp()

        mock.patch("storage.S3Storage._s3_client", _mock_awsclient(service_name="s3")).start()
        mock.patch("handlers.aws.handler.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch("handlers.aws.utils.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch(
            "share.secretsmanager._get_aws_sm_client",
            lambda region_name: _mock_awsclient(service_name="secretsmanager", region_name=region_name),
        ).start()

    def tearDown(self) -> None:
        super(TestLambdaHandlerSuccessSQS, self).tearDown()

    @mock.patch("handlers.aws.handler._completion_grace_period", 1)
    def test_lambda_handler_replay(self) -> None:
        cloudwatch_log: str = (
            '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}\n\n{"excluded": '
            '"by filter"}\n{"ecs": {"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, '
            '"name": "handler.py"}, "function": "lambda_handler"}, "original": "trigger"}}\n{"another": '
            '"continuation", "from": "the", "continuing": "queue"}'
        )

        _event_to_sqs_message(queue_attributes=self._queues_info["source-queue"], message_body=cloudwatch_log)

        event, timestamp = _event_from_sqs_message(queue_attributes=self._queues_info["source-queue"])

        message_id = event["Records"][0]["messageId"]
        hash_sqs = _get_hex_prefix(f"source-queue-{message_id}")
        prefix: str = f"{timestamp}-{hash_sqs}"

        # Create an expected id so that es.send will fail
        self._es_client.index(
            index="logs-generic-default",
            op_type="create",
            id=f"{prefix}-000000000000",
            document={"@timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        )
        self._es_client.indices.refresh(index="logs-generic-default")

        ctx = ContextMock(remaining_time_in_millis=2)

        first_call = handler(event, ctx)  # type:ignore

        assert first_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")

        res = self._es_client.search(
            index="logs-generic-default",
            query={"ids": {"values": [f"{prefix}-000000000113", f"{prefix}-000000000279"]}},
        )
        assert res["hits"]["total"] == {"value": 2, "relation": "eq"}

        assert (
            res["hits"]["hits"][0]["_source"]["message"]
            == '{"ecs": {"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, "name": '
            '"handler.py"}, "function": "lambda_handler"}, "original": "trigger"}}'
        )

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 113,
            "file": {"path": self._queues_info["source-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {"sqs": {"name": "source-queue", "message_id": message_id}}
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        assert (
            res["hits"]["hits"][1]["_source"]["message"]
            == '{"another": "continuation", "from": "the", "continuing": "queue"}'
        )

        assert res["hits"]["hits"][1]["_source"]["log"] == {
            "offset": 279,
            "file": {"path": self._queues_info["source-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][1]["_source"]["aws"] == {"sqs": {"name": "source-queue", "message_id": message_id}}
        assert res["hits"]["hits"][1]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][1]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)

        with self.assertRaises(ReplayHandlerException):
            handler(event, ctx)  # type:ignore

        # Remove the expected id so that it can be replayed
        self._es_client.delete_by_query(
            index="logs-generic-default", body={"query": {"ids": {"values": [f"{prefix}-000000000000"]}}}
        )
        self._es_client.indices.refresh(index="logs-generic-default")

        # implicit wait for the message to be back on the queue
        time.sleep(35)
        event, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        third_call = handler(event, ctx)  # type:ignore

        assert third_call == "replayed"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 3, "relation": "eq"}
        assert (
            res["hits"]["hits"][2]["_source"]["message"]
            == '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}'
        )

        assert res["hits"]["hits"][2]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": self._queues_info["source-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][2]["_source"]["aws"] == {"sqs": {"name": "source-queue", "message_id": message_id}}
        assert res["hits"]["hits"][2]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][2]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

    def test_lambda_handler_continuing(self) -> None:
        ctx = ContextMock()

        cloudwatch_log: str = (
            '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}\n{"ecs": '
            '{"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, "name": "handler.py"}, '
            '"function": "lambda_handler"}, "original": "trigger"}}\n{"another": "continuation", "from": "the", '
            '"continuing": "queue"}\n'
        )

        _event_to_sqs_message(queue_attributes=self._queues_info["source-queue"], message_body=cloudwatch_log)

        event, _ = _event_from_sqs_message(queue_attributes=self._queues_info["source-queue"])

        first_call = handler(event, ctx)  # type:ignore

        assert first_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 1

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 1, "relation": "eq"}

        assert (
            res["hits"]["hits"][0]["_source"]["message"]
            == '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}'
        )

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": self._queues_info["source-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "sqs": {"name": "source-queue", "message_id": event["Records"][0]["messageId"]}
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        second_call = handler(event, ctx)  # type:ignore

        assert second_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 2

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 2, "relation": "eq"}

        assert (
            res["hits"]["hits"][1]["_source"]["message"]
            == '{"ecs": {"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, "name": '
            '"handler.py"}, "function": "lambda_handler"}, "original": "trigger"}}'
        )

        assert res["hits"]["hits"][1]["_source"]["log"] == {
            "offset": 86,
            "file": {"path": self._queues_info["source-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][1]["_source"]["aws"] == {
            "sqs": {
                "name": "source-queue",
                "message_id": event["Records"][0]["messageAttributes"]["originalMessageId"]["stringValue"],
            }
        }
        assert res["hits"]["hits"][1]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][1]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        third_call = handler(event, ctx)  # type:ignore

        assert third_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 3, "relation": "eq"}

        assert (
            res["hits"]["hits"][2]["_source"]["message"]
            == '{"another": "continuation", "from": "the", "continuing": "queue"}'
        )

        assert res["hits"]["hits"][2]["_source"]["log"] == {
            "offset": 252,
            "file": {"path": self._queues_info["source-queue"]["QueueUrlPath"]},
        }
        assert res["hits"]["hits"][2]["_source"]["aws"] == {
            "sqs": {
                "name": "source-queue",
                "message_id": event["Records"][0]["messageAttributes"]["originalMessageId"]["stringValue"],
            }
        }
        assert res["hits"]["hits"][2]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][2]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        fourth_call = handler(event, ctx)  # type:ignore

        assert fourth_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3


@pytest.mark.integration
class TestLambdaHandlerSuccessCloudWatchLogs(IntegrationTestCase):
    def setUp(self) -> None:
        self._services = ["logs", "s3", "sqs", "secretsmanager"]
        self._cloudwatch_logs_groups = [{"group_name": "source-group", "stream_name": "source-stream"}]
        self._expand_event_list_from_field = "expandFromList"

        super(TestLambdaHandlerSuccessCloudWatchLogs, self).setUp()

        mock.patch("storage.S3Storage._s3_client", _mock_awsclient(service_name="s3")).start()
        mock.patch("handlers.aws.handler.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch("handlers.aws.utils.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch(
            "handlers.aws.utils.get_cloudwatch_logs_client", lambda: _mock_awsclient(service_name="logs")
        ).start()
        mock.patch(
            "share.secretsmanager._get_aws_sm_client",
            lambda region_name: _mock_awsclient(service_name="secretsmanager", region_name=region_name),
        ).start()

    def tearDown(self) -> None:
        super(TestLambdaHandlerSuccessCloudWatchLogs, self).tearDown()

    @mock.patch("handlers.aws.handler._completion_grace_period", 1)
    def test_lambda_handler_replay(self) -> None:
        cloudwatch_log: str = (
            '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}\n\n{"excluded": '
            '"by filter"}\n{"ecs": {"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, '
            '"name": "handler.py"}, "function": "lambda_handler"}, "original": "trigger"}}\n{"another": '
            '"continuation", "from": "the", "continuing": "queue"}'
        )

        _event_to_cloudwatch_logs(
            group_name="source-group", stream_name="source-stream", messages_body=[cloudwatch_log]
        )

        event, event_ids, event_timestamps = _event_from_cloudwatch_logs(
            group_name="source-group", stream_name="source-stream"
        )

        hash_logs = _get_hex_prefix(f"source-group-source-stream-{event_ids[0]}")
        prefix: str = f"{event_timestamps[0]}-{hash_logs}"

        # Create an expected id so that es.send will fail
        self._es_client.index(
            index="logs-generic-default",
            op_type="create",
            id=f"{prefix}-000000000000",
            document={"@timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        )
        self._es_client.indices.refresh(index="logs-generic-default")

        ctx = ContextMock(remaining_time_in_millis=2)

        first_call = handler(event, ctx)  # type:ignore

        assert first_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")

        res = self._es_client.search(
            index="logs-generic-default",
            query={"ids": {"values": [f"{prefix}-000000000113", f"{prefix}-000000000279"]}},
        )
        assert res["hits"]["total"] == {"value": 2, "relation": "eq"}

        assert (
            res["hits"]["hits"][0]["_source"]["message"]
            == '{"ecs": {"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, "name": '
            '"handler.py"}, "function": "lambda_handler"}, "original": "trigger"}}'
        )

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 113,
            "file": {"path": "source-group/source-stream"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "cloudwatch": {
                "log_group": "source-group",
                "log_stream": "source-stream",
                "event_id": event_ids[0],
            }
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        assert (
            res["hits"]["hits"][1]["_source"]["message"]
            == '{"another": "continuation", "from": "the", "continuing": "queue"}'
        )

        assert res["hits"]["hits"][1]["_source"]["log"] == {
            "offset": 279,
            "file": {"path": "source-group/source-stream"},
        }
        assert res["hits"]["hits"][1]["_source"]["aws"] == {
            "cloudwatch": {"log_group": "source-group", "log_stream": "source-stream", "event_id": event_ids[0]}
        }
        assert res["hits"]["hits"][1]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][1]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)

        with self.assertRaises(ReplayHandlerException):
            handler(event, ctx)  # type:ignore

        # Remove the expected id so that it can be replayed
        self._es_client.delete_by_query(
            index="logs-generic-default", body={"query": {"ids": {"values": [f"{prefix}-000000000000"]}}}
        )
        self._es_client.indices.refresh(index="logs-generic-default")

        # implicit wait for the message to be back on the queue
        time.sleep(35)
        event, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        third_call = handler(event, ctx)  # type:ignore

        assert third_call == "replayed"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 3, "relation": "eq"}
        assert (
            res["hits"]["hits"][2]["_source"]["message"]
            == '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}'
        )

        assert res["hits"]["hits"][2]["_source"]["log"] == {"offset": 0, "file": {"path": "source-group/source-stream"}}
        assert res["hits"]["hits"][2]["_source"]["aws"] == {
            "cloudwatch": {"log_group": "source-group", "log_stream": "source-stream", "event_id": event_ids[0]}
        }
        assert res["hits"]["hits"][2]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][2]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

    def test_lambda_handler_continuing(self) -> None:
        ctx = ContextMock()

        cloudwatch_log: str = json_dumper(
            {
                "expandFromList": [
                    {"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"},
                    {
                        "ecs": {"version": "1.6.0"},
                        "log": {
                            "logger": "root",
                            "origin": {"file": {"line": 30, "name": "handler.py"}, "function": "lambda_handler"},
                            "original": "trigger",
                        },
                    },
                    {"another": "continuation", "from": "the", "continuing": "queue"},
                ]
            }
        )

        _event_to_cloudwatch_logs(
            group_name="source-group", stream_name="source-stream", messages_body=[cloudwatch_log, "excluded"]
        )

        event, event_ids, _ = _event_from_cloudwatch_logs(group_name="source-group", stream_name="source-stream")
        first_call = handler(event, ctx)  # type:ignore

        assert first_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 1

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 1, "relation": "eq"}
        assert (
            res["hits"]["hits"][0]["_source"]["message"]
            == '{"@timestamp":"2021-12-28T11:33:08.160Z","log.level":"info","message":"trigger"}'
        )

        assert res["hits"]["hits"][0]["_source"]["log"] == {
            "offset": 0,
            "file": {"path": "source-group/source-stream"},
        }
        assert res["hits"]["hits"][0]["_source"]["aws"] == {
            "cloudwatch": {"log_group": "source-group", "log_stream": "source-stream", "event_id": event_ids[0]}
        }
        assert res["hits"]["hits"][0]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][0]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        second_call = handler(event, ctx)  # type:ignore

        assert second_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 2

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 2, "relation": "eq"}

        assert (
            res["hits"]["hits"][1]["_source"]["message"]
            == '{"ecs":{"version":"1.6.0"},"log":{"logger":"root","origin":{"file":{"line":30,"name":"handler.py"},'
            '"function":"lambda_handler"},"original":"trigger"}}'
        )

        assert res["hits"]["hits"][1]["_source"]["log"] == {
            "offset": 104,
            "file": {"path": "source-group/source-stream"},
        }
        assert res["hits"]["hits"][1]["_source"]["aws"] == {
            "cloudwatch": {"log_group": "source-group", "log_stream": "source-stream", "event_id": event_ids[0]}
        }
        assert res["hits"]["hits"][1]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][1]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        third_call = handler(event, ctx)  # type:ignore

        assert third_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3

        res = self._es_client.search(index="logs-generic-default", sort="_seq_no")
        assert res["hits"]["total"] == {"value": 3, "relation": "eq"}

        assert (
            res["hits"]["hits"][2]["_source"]["message"]
            == '{"another":"continuation","from":"the","continuing":"queue"}'
        )

        assert res["hits"]["hits"][2]["_source"]["log"] == {
            "offset": 208,
            "file": {"path": "source-group/source-stream"},
        }
        assert res["hits"]["hits"][2]["_source"]["aws"] == {
            "cloudwatch": {"log_group": "source-group", "log_stream": "source-stream", "event_id": event_ids[0]}
        }
        assert res["hits"]["hits"][2]["_source"]["cloud"] == {
            "account": {"id": "000000000000"},
            "provider": "aws",
            "region": "us-east-1",
        }

        assert res["hits"]["hits"][2]["_source"]["tags"] == ["forwarded", "generic", "tag1", "tag2", "tag3"]

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        fourth_call = handler(event, ctx)  # type:ignore

        assert fourth_call == "continuing"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        fifth_call = handler(event, ctx)  # type:ignore

        assert fifth_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")
        assert self._es_client.count(index="logs-generic-default")["count"] == 3


@pytest.mark.integration
class TestLambdaHandlerFailureSSLFingerprint(IntegrationTestCase):
    def setUp(self) -> None:
        self._services = ["s3", "sqs", "secretsmanager"]
        self._queues = [{"name": "source-queue", "type": "sqs"}]
        self._ssl_fingerprint_mismatch = True

        super(TestLambdaHandlerFailureSSLFingerprint, self).setUp()

        mock.patch("storage.S3Storage._s3_client", _mock_awsclient(service_name="s3")).start()
        mock.patch("handlers.aws.handler.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch("handlers.aws.utils.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch(
            "share.secretsmanager._get_aws_sm_client",
            lambda region_name: _mock_awsclient(service_name="secretsmanager", region_name=region_name),
        ).start()

    def tearDown(self) -> None:
        super(TestLambdaHandlerFailureSSLFingerprint, self).tearDown()

    def test_lambda_handler_ssl_fingerprint_mismatch(self) -> None:
        ctx = ContextMock()

        cloudwatch_log: str = (
            '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}\n{"ecs": '
            '{"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, "name": "handler.py"}, '
            '"function": "lambda_handler"}, "original": "trigger"}}\n{"another": "continuation", "from": "the", '
            '"continuing": "queue"}\n'
        )

        _event_to_sqs_message(queue_attributes=self._queues_info["source-queue"], message_body=cloudwatch_log)

        event, timestamp = _event_from_sqs_message(queue_attributes=self._queues_info["source-queue"])

        first_call = handler(event, ctx)  # type:ignore

        assert first_call == "continuing"

        assert self._es_client.indices.exists(index="logs-generic-default") is False

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        second_call = handler(event, ctx)  # type:ignore

        assert second_call == "continuing"

        assert self._es_client.indices.exists(index="logs-generic-default") is False

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        third_call = handler(event, ctx)  # type:ignore

        assert third_call == "continuing"

        assert self._es_client.indices.exists(index="logs-generic-default") is False

        event, _ = _event_from_sqs_message(queue_attributes=self._continuing_queue_info)
        fourth_call = handler(event, ctx)  # type:ignore

        assert fourth_call == "completed"

        assert self._es_client.indices.exists(index="logs-generic-default") is False

        events, _ = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        assert len(events["Records"]) == 3

        first_body: dict[str, Any] = json_parser(events["Records"][0]["body"])
        second_body: dict[str, Any] = json_parser(events["Records"][1]["body"])
        third_body: dict[str, Any] = json_parser(events["Records"][2]["body"])

        assert (
            first_body["event_payload"]["message"]
            == '{"@timestamp": "2021-12-28T11:33:08.160Z", "log.level": "info", "message": "trigger"}'
        )

        assert (
            second_body["event_payload"]["message"]
            == '{"ecs": {"version": "1.6.0"}, "log": {"logger": "root", "origin": {"file": {"line": 30, "name": '
            '"handler.py"}, "function": "lambda_handler"}, "original": "trigger"}}'
        )

        assert (
            third_body["event_payload"]["message"]
            == '{"another": "continuation", "from": "the", "continuing": "queue"}'
        )


@pytest.mark.integration
class TestLambdaHandlerFailureESNoMatchingActionFailed(IntegrationTestCase):
    def setUp(self) -> None:
        self._services = ["s3", "sqs", "secretsmanager"]
        self._queues = [{"name": "source-queue", "type": "sqs"}]

        super(TestLambdaHandlerFailureESNoMatchingActionFailed, self).setUp()

        mock.patch("storage.S3Storage._s3_client", _mock_awsclient(service_name="s3")).start()
        mock.patch("handlers.aws.handler.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch("handlers.aws.utils.get_sqs_client", lambda: _mock_awsclient(service_name="sqs")).start()
        mock.patch(
            "share.secretsmanager._get_aws_sm_client",
            lambda region_name: _mock_awsclient(service_name="secretsmanager", region_name=region_name),
        ).start()

    def tearDown(self) -> None:
        super(TestLambdaHandlerFailureESNoMatchingActionFailed, self).tearDown()

    @mock.patch("handlers.aws.handler._completion_grace_period", 1)
    def test_lambda_handler_no_es_matching_action_failed(self) -> None:
        message: str = "a message"
        fingerprint: str = "DUEwoALOve1Y9MtPCfT7IJGU3IQ="

        # Create an expected id so that es.send will fail
        self._es_client.index(
            index="logs-generic-default",
            op_type="create",
            id=fingerprint,
            document={"@timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
        )

        processors = {
            "processors": [
                {
                    "fingerprint": {
                        "fields": ["message"],
                        "target_field": "_id",
                    }
                }
            ]
        }

        # Add a pipeline that will generate the same _id
        self._es_client.ingest.put_pipeline(id="id_fingerprint_pipeline", body=processors)
        self._es_client.indices.put_settings(
            index="logs-generic-default", body={"index.default_pipeline": "id_fingerprint_pipeline"}
        )

        self._es_client.indices.refresh(index="logs-generic-default")

        assert self._es_client.count(index="logs-generic-default")["count"] == 1

        _event_to_sqs_message(queue_attributes=self._queues_info["source-queue"], message_body=message)

        event, _ = _event_from_sqs_message(queue_attributes=self._queues_info["source-queue"])

        ctx = ContextMock(remaining_time_in_millis=2)

        first_call = handler(event, ctx)  # type:ignore

        assert first_call == "completed"

        self._es_client.indices.refresh(index="logs-generic-default")

        assert self._es_client.count(index="logs-generic-default")["count"] == 1

        res = self._es_client.search(index="logs-generic-default")
        assert "message" not in res["hits"]["hits"][0]["_source"]

        event, timestamp = _event_from_sqs_message(queue_attributes=self._replay_queue_info)
        assert not event["Records"]
        assert not timestamp
