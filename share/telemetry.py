# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.

import datetime as dt
import json
import os
import time
from abc import ABCMeta
from dataclasses import dataclass
from enum import Enum
from queue import Empty, Queue
from threading import Thread
from typing import Any, Dict, List, Optional, Protocol, TypeVar, Union

import urllib3

from share import shared_logger

# -------------------------------------------------------
# Helpers
# -------------------------------------------------------


def strtobool(val: str) -> bool:
    """Convert a string representation of truth to true (1) or false (0).
    True values are 'y', 'yes', 't', 'true', 'on', and '1'; false values
    are 'n', 'no', 'f', 'false', 'off', and '0'.  Raises ValueError if
    'val' is anything else.
    """
    val = val.lower()
    if val in ("y", "yes", "t", "true", "on", "1"):
        return True
    elif val in ("n", "no", "f", "false", "off", "0"):
        return False
    else:
        raise ValueError("invalid truth value {!r}".format(val))


def is_telemetry_enabled() -> bool:
    """Check if telemetry is enabled."""
    deployment_id = os.environ.get("TELEMETRY_DEPLOYMENT_ID", "")
    if not deployment_id:
        shared_logger.debug("Telemetry is disabled. TELEMETRY_DEPLOYMENT_ID is not set.")
        return False
    
    enabled = os.environ.get("TELEMETRY_ENABLED", "no")
    try:
        return strtobool(enabled)
    except ValueError:
        shared_logger.debug(f"Telemetry is disabled. Invalid value for TELEMETRY_ENABLED: {enabled}")
        return False


def get_telemetry_endpoint() -> str:
    """Get the telemetry endpoint."""
    return os.environ.get("TELEMETRY_ENDPOINT", "https://telemetry.elastic.co/v3/send/esf")


# -------------------------------------------------------
# Models
# -------------------------------------------------------


class WithExceptionTelemetryEnum(Enum):
    """Enum for telemetry exception data."""

    EXCEPTION_RAISED = "EXCEPTION_RAISED"
    EXCEPTION_IGNORED = "EXCEPTION_IGNORED"


@dataclass
class FunctionContext:
    """The function execution context."""

    function_id: str
    function_version: str
    execution_id: str
    cloud_region: str
    cloud_provider: str
    memory_limit_in_mb: str


class TelemetryData:
    """Telemetry data class"""

    function_id: str = ""
    function_version: str = ""
    execution_id: str = ""
    cloud_provider: str = ""
    cloud_region: str = ""
    memory_limit_in_mb: str = ""

    #
    # We want to collect the unique inputs and their outputs in a data structure
    # like this one to avoid duplicates:
    #
    # {
    #   "input-123": {
    #     "type": "s3-sqs",
    #     "outputs": [
    #       "elasticsearch",
    #       "logstash"
    #     ]
    #   }
    # }
    #
    # The data is sent to the telemetry endpoint using a different structure
    # to make analysis easier.
    #
    inputs: Dict[str, dict[str, Union[str, List[str]]]] = {}

    # More fields will be collected in the future
    start_time: str = ""
    end_time: str = ""
    to_be_continued: bool = False
    with_exception: Optional[WithExceptionTelemetryEnum] = None

    def add_input(self, input_id: str, input_type: str, outputs: List[str]) -> None:
        """Add an input to the telemetry."""
        self.inputs[input_id] = {"type": input_type, "outputs": outputs}


class ProtocolTelemetryEvent(Protocol):
    """
    Protocol for Telemetry Command components
    """

    def merge_with(self, telemetry_data: TelemetryData) -> TelemetryData:
        pass  # pragma: no cover


TelemetryEventType = TypeVar("TelemetryEventType", bound=ProtocolTelemetryEvent)


# -------------------------------------------------------
# Events
# -------------------------------------------------------

# List of telemetry events
#
# | -------------------- | --------------------------------------------------------------- |
# | Event                | Description                                                     |
# | -------------------- | --------------------------------------------------------------- |
# | FunctionStartedEvent | Occurs at the start of the function execution.                  |
# | InputSelectedEvent   | Occurs when the input is selected to process an incoming event. |
# | EventProcessedEvent  | Occurs when the event is processed successfully.                |
# | FunctionEndedEvent   | Occurs at the end of the function execution.                    |
# | -------------------- | --------------------------------------------------------------- |
#


class CommonTelemetryEvent(metaclass=ABCMeta):
    """
    Common class for Telemetry Command components
    arn:partition:service:region:account-id:resource-id
    arn:partition:service:region:account-id:resource-type/resource-id
    arn:partition:service:region:account-id:resource-type:resource-id
    """


class FunctionStartedEvent(CommonTelemetryEvent):
    """FunctionStartedEvent represents the start of the function execution."""

    def __init__(self, ctx: FunctionContext) -> None:
        self.function_id = ctx.function_id
        self.function_version = ctx.function_version
        self.cloud_provider = ctx.cloud_provider
        self.cloud_region = ctx.cloud_region
        self.execution_id = ctx.execution_id
        self.memory_limit_in_mb = ctx.memory_limit_in_mb

    def merge_with(self, telemetry_data: TelemetryData) -> TelemetryData:
        """Merge the current event details with the telemetry data"""
        telemetry_data.function_id = self.function_id
        telemetry_data.function_version = self.function_version
        telemetry_data.cloud_provider = self.cloud_provider
        telemetry_data.cloud_region = self.cloud_region
        telemetry_data.execution_id = self.execution_id
        telemetry_data.memory_limit_in_mb = self.memory_limit_in_mb
        telemetry_data.start_time = dt.datetime.utcnow().strftime("%s.%f")

        return telemetry_data


class InputSelectedEvent(CommonTelemetryEvent):
    """Happens when the input is selected to process an incoming event."""

    def __init__(self, input_id: str, input_type: str, outputs: List[str]) -> None:
        self.input_id = input_id
        self.input_type = input_type
        self.outputs = outputs

    def merge_with(self, telemetry_data: TelemetryData) -> TelemetryData:
        """Merge the current event details with the telemetry data"""

        telemetry_data.add_input(self.input_id, self.input_type, self.outputs)

        return telemetry_data


class EventProcessedEvent(CommonTelemetryEvent):
    """Happens when the event is processed successfully."""

    def merge_with(self, telemetry_data: TelemetryData) -> TelemetryData:
        """Merge the current event details with the telemetry data"""
        return telemetry_data


class FunctionEndedEvent(CommonTelemetryEvent):
    """FunctionEndedEvent represents the end of the function execution."""

    def __init__(self, with_exception: Optional[WithExceptionTelemetryEnum], to_be_continued: bool) -> None:
        self.with_exception = with_exception
        self.to_be_continued = to_be_continued

    def merge_with(self, telemetry_data: TelemetryData) -> TelemetryData:
        """Merge the current event details with the telemetry data"""
        telemetry_data.to_be_continued = self.to_be_continued
        telemetry_data.end_time = dt.datetime.utcnow().strftime("%s.%f")

        return telemetry_data


# -------------------------------------------------------
# Event triggers
# -------------------------------------------------------


def function_started_telemetry(ctx: FunctionContext) -> None:
    """Triggers the `FunctionStartedEvent` telemetry event."""
    if not is_telemetry_enabled():
        return

    _events_queue.put(FunctionStartedEvent(ctx))


def input_selected_telemetry(_id: str, _type: str, _outputs: List[str]) -> None:
    """Triggers the `InputSelectedEvent` telemetry event."""
    if not is_telemetry_enabled():
        return

    telemetry_event = InputSelectedEvent(
        _id,
        _type,
        _outputs,
    )
    _events_queue.put(telemetry_event)


def event_processed_telemetry() -> None:
    """Triggers the `EventProcessedEvent` telemetry event."""
    if not is_telemetry_enabled():
        return

    _events_queue.put(EventProcessedEvent())


def function_ended_telemetry(
    exception_ignored: bool = False, exception_raised: bool = False, to_be_continued: bool = False
) -> None:
    """Triggers the `FunctionEndedEvent` telemetry event."""
    if not is_telemetry_enabled():
        return

    with_exception = None
    if exception_ignored:
        with_exception = WithExceptionTelemetryEnum.EXCEPTION_IGNORED
    elif exception_raised:
        with_exception = WithExceptionTelemetryEnum.EXCEPTION_RAISED

    _events_queue.put(FunctionEndedEvent(with_exception=with_exception, to_be_continued=to_be_continued))


# -------------------------------------------------------
# Worker Thread
# -------------------------------------------------------


class TelemetryWorker(Thread):
    """The TelemetryWorker sends the telemetry data to the telemetry endpoint.

    The worker waits for events to be added to the queue and then sends
    the telemetry data to the telemetry endpoint."""

    def __init__(self, queue: Queue[ProtocolTelemetryEvent]) -> None:
        Thread.__init__(self)
        self.queue = queue
        self.telemetry_data = TelemetryData()
        self.telemetry_client: urllib3.PoolManager = urllib3.PoolManager(
            timeout=urllib3.Timeout(total=3.0),
            retries=False,  # we can't afford to retry on failure
        )

    def _send_telemetry(self) -> None:
        """Sends the telemetry data to the telemetry endpoint."""

        telemetry_data: dict[str, Any] = {
            "function_id": self.telemetry_data.function_id,
            "function_version": self.telemetry_data.function_version,
            "execution_id": self.telemetry_data.execution_id,
            "cloud_provider": self.telemetry_data.cloud_provider,
            "cloud_region": self.telemetry_data.cloud_region,
            "memory_limit_in_mb": self.telemetry_data.memory_limit_in_mb,
        }

        if self.telemetry_data.inputs:
            # turn the inputs into a list of dicts, it's easier
            # to work with in Kibana
            _inputs = [
                {"id": k, "type": v["type"], "outputs": v["outputs"]} for k, v in self.telemetry_data.inputs.items()
            ]
            telemetry_data.update(
                {
                    "inputs": _inputs,
                }
            )

        try:
            endpoint = get_telemetry_endpoint()
            encoded_data = json.dumps(telemetry_data).encode("utf-8")
            r = self.telemetry_client.request(  # type: ignore
                "POST",
                endpoint,
                body=encoded_data,
                headers={
                    "X-Elastic-Cluster-ID": self.telemetry_data.function_id,
                    "X-Elastic-Stack-Version": self.telemetry_data.function_version,
                    "Content-Type": "application/json",
                },
            )
            shared_logger.info(f"telemetry data sent (http status: {r.status})")

        except Exception as e:
            shared_logger.info(f"telemetry data not sent: {e}")

    def _process_event(self, event: ProtocolTelemetryEvent) -> None:
        """Process telemetry event"""

        self.telemetry_data = event.merge_with(self.telemetry_data)
        if isinstance(event, EventProcessedEvent):
            self._send_telemetry()

    def run(self) -> None:
        """The worker waits for events to be added to the queue and then sends
        the telemetry data to the telemetry endpoint."""

        while True:
            try:
                event: ProtocolTelemetryEvent = self.queue.get(block=False)
                self._process_event(event)
            except Empty:
                time.sleep(1)
                continue


# -------------------------------------------------------
# Internal variables and functions
# -------------------------------------------------------


def telemetry_init() -> None:
    """Ensure the worker is started.

    If the worker is already exists, it is a no-op.
    """
    global _worker

    if _worker is None:
        _worker = TelemetryWorker(_events_queue)
        # the worker dies when main thread (only non-daemon thread) exits.
        _worker.daemon = True
        _worker.start()


# The queue is used to communicate between the main thread
# and the worker thread.
#
# The main thread adds events to the queue and the worker
# thread reads them.
_events_queue: Queue[ProtocolTelemetryEvent] = Queue()

# Worker thread that sends the telemetry data to the telemetry
# endpoint.
_worker: Optional[TelemetryWorker] = None

if is_telemetry_enabled():
    # If telemetry is enabled when the module is loaded,
    # we start the worker.
    #
    # You can also start the worker later by
    # calling telemetry_init(), for example for
    # testing purposes.
    telemetry_init()
