from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import paho.mqtt.client as mqtt
from tb_device_mqtt import TBDeviceMqttClient


LOGGER = logging.getLogger("hperfor-epa-plugin")
DEFAULT_CONFIG_PATH = Path("config.json")


@dataclass(slots=True)
class DeviceConfig:
    name: str
    hperfor_client_id: str
    hperfor_gateway_id: str
    tb_device_token: str


@dataclass(slots=True)
class HperforConfig:
    broker_address: str
    broker_port: int
    client_id: str
    username: str
    password: str


@dataclass(slots=True)
class ThingsBoardConfig:
    host: str
    port: int


@dataclass(slots=True)
class AppConfig:
    hperfor: HperforConfig
    thingsboard: ThingsBoardConfig
    devices: list[DeviceConfig]


def load_config() -> AppConfig:
    config_path = Path(os.getenv("HPERFOR_PLUGIN_CONFIG", DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Copy config.example.json to config.json and fill it first."
        )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    devices = [
        DeviceConfig(
            name=item["name"],
            hperfor_client_id=item.get("hperfor_client_id") or item["hperfor-ClientID"],
            hperfor_gateway_id=(
                item.get("hperfor_gateway_id")
                or item.get("hperfor-GatewayID")
                or item.get("hperfor_client_id")
                or item["hperfor-ClientID"]
            ),
            tb_device_token=item.get("tb_device_token") or item["tb-DeviceToken"],
        )
        for item in data["devices"]
    ]
    return AppConfig(
        hperfor=HperforConfig(**data["hperfor"]),
        thingsboard=ThingsBoardConfig(**data["thingsboard"]),
        devices=devices,
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_mid() -> str:
    return str(uuid.uuid4()).upper()


def to_snake_case(value: str) -> str:
    result: list[str] = []
    for index, char in enumerate(value):
        if char.isupper() and index > 0 and (not value[index - 1].isupper()):
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def flatten_child_payload(child: dict[str, Any]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    for key, value in child.items():
        normalized_key = to_snake_case(key)
        telemetry[normalized_key] = value
    return telemetry


def build_ack_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "result": True,
        "mid": payload.get("mid"),
        "bid": payload.get("bid"),
        "message": 0,
    }


class ThingsBoardDeviceBridge:
    def __init__(
        self,
        device: DeviceConfig,
        tb_config: ThingsBoardConfig,
        publish_command: Callable[[str, dict[str, Any]], None],
        request_device_data: Callable[[str], dict[str, Any]],
    ) -> None:
        self.device = device
        self.tb_config = tb_config
        self.publish_command = publish_command
        self.request_device_data = request_device_data
        self.client = TBDeviceMqttClient(
            tb_config.host,
            username=device.tb_device_token,
            password="",
            port=tb_config.port,
        )

    def connect(self) -> None:
        LOGGER.info(
            "Connecting ThingsBoard device %s (%s)",
            self.device.name,
            self.device.hperfor_client_id,
        )
        self.client.connect()
        self.client.set_server_side_rpc_request_handler(self._handle_rpc)

    def disconnect(self) -> None:
        try:
            self.client.disconnect()
        except Exception:
            LOGGER.exception("Failed to disconnect ThingsBoard client for %s", self.device.name)

    def send_telemetry(self, telemetry: dict[str, Any]) -> None:
        LOGGER.debug("Telemetry for %s: %s", self.device.name, telemetry)
        self.client.send_telemetry(telemetry)

    def _build_switch_payload(self, operation: int) -> dict[str, Any]:
        return {
            "bid": 202,
            "mid": build_mid(),
            "Children": [
                {
                    "ClientID": self.device.hperfor_client_id,
                    "MoterOperation": operation,
                }
            ],
        }

    def _publish_switch(self, operation: int) -> dict[str, Any]:
        payload = self._build_switch_payload(operation)
        self.publish_command(self.device.hperfor_client_id, payload)
        return payload

    def _run_switch_delay(self, target_status: str, delay_seconds: float, switch_status: int) -> None:
        LOGGER.info(
            "Executing switch_delay for %s: target_status=%s delay=%s current_switch_status=%s",
            self.device.name,
            target_status,
            delay_seconds,
            switch_status,
        )

        if target_status == "on":
            if switch_status == 1:
                self._publish_switch(2)
            time.sleep(delay_seconds)
            self._publish_switch(1)
            return

        if switch_status == 1:
            time.sleep(delay_seconds)
            self._publish_switch(2)

    def _handle_rpc(self, request_id: int, request_body: dict[str, Any]) -> None:
        LOGGER.info("Received RPC for %s: %s", self.device.name, request_body)
        try:
            method = request_body.get("method")
            params = request_body.get("params") or {}

            if method == "switch":
                status = params.get("status")
                operation = {"on": 1, "off": 2}.get(status)
                if operation is None:
                    raise ValueError("switch.status must be 'on' or 'off'")
                payload = self._publish_switch(operation)
                self.client.send_rpc_reply(
                    request_id,
                    {"success": True, "mid": payload["mid"], "bid": payload["bid"]},
                )
                return
            elif method in ["switch_delay", "screen_shutdown", "screen_startup"]:
                status = params.get("status")
                if status not in {"on", "off"}:
                    if method == "screen_shutdown":
                        status = "off"
                    elif method == "screen_startup":
                        status = "on"
                    else:
                        raise ValueError("switch_delay.status must be 'on' or 'off'")

                delay = params.get("delay")
                if not isinstance(delay, (int, float)):
                    if method == "screen_shutdown":
                        delay = 60
                    elif method == "screen_startup":
                        delay = 0
                if delay < 0:
                    delay = - delay


                device_data = self.request_device_data(self.device.hperfor_client_id)
                switch_status = device_data.get("SwitchStatus", device_data.get("switch_status"))
                if switch_status not in {0, 1}:
                    raise ValueError("Unable to determine current switch_status from bid 208 response")

                should_execute = (status == "on") or (status == "off" and switch_status == 1)
                if should_execute:
                    worker = threading.Thread(
                        target=self._run_switch_delay,
                        args=(status, float(delay), int(switch_status)),
                        daemon=True,
                        name=f"switch-delay-{self.device.hperfor_client_id}",
                    )
                    worker.start()

                self.client.send_rpc_reply(
                    request_id,
                    {
                        "success": True,
                        "bid": 208,
                        "switch_status": switch_status,
                        "scheduled": should_execute,
                        "status": status,
                        "delay": delay,
                    },
                )
                return
            elif method == "get_data":
                payload = {
                    "bid": 208,
                    "mid": build_mid(),
                    "Children": [{"ClientID": self.device.hperfor_client_id}],
                }
                self.publish_command(self.device.hperfor_client_id, payload)
                self.client.send_rpc_reply(
                    request_id,
                    {"success": True, "mid": payload["mid"], "bid": payload["bid"]},
                )
                return
            else:
                raise ValueError(f"Unsupported RPC method: {method}")
        except Exception as exc:
            LOGGER.exception("RPC handling failed for %s", self.device.name)
            self.client.send_rpc_reply(request_id, {"success": False, "error": str(exc)})


class HperforBridgeService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self._cleanup_done = False
        self._state_lock = threading.Lock()
        self._pending_data_requests: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self.devices_by_client_id = {
            device.hperfor_client_id: device for device in config.devices
        }
        self.gateway_ids = sorted({device.hperfor_gateway_id for device in config.devices})
        self.tb_bridges = {
            device.hperfor_client_id: ThingsBoardDeviceBridge(
                device=device,
                tb_config=config.thingsboard,
                publish_command=self.publish_to_hperfor,
                request_device_data=self.request_device_data,
            )
            for device in config.devices
        }
        self.hperfor_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.hperfor.client_id,
            clean_session=True,
        )
        self.hperfor_client.username_pw_set(
            config.hperfor.username,
            config.hperfor.password,
        )
        self.hperfor_client.on_connect = self._on_hperfor_connect
        self.hperfor_client.on_message = self._on_hperfor_message
        self.hperfor_client.on_disconnect = self._on_hperfor_disconnect

    def start(self) -> None:
        for bridge in self.tb_bridges.values():
            bridge.connect()

        LOGGER.info(
            "Connecting hperfor MQTT broker %s:%s",
            self.config.hperfor.broker_address,
            self.config.hperfor.broker_port,
        )
        self.hperfor_client.connect(
            self.config.hperfor.broker_address,
            self.config.hperfor.broker_port,
            keepalive=60,
        )
        self.hperfor_client.loop_start()

    def run_forever(self) -> None:
        self.start()
        while not self.stop_event.is_set():
            time.sleep(1)
        self.stop()

    def stop(self) -> None:
        if self._cleanup_done:
            return
        self.stop_event.set()
        self._cleanup_done = True
        LOGGER.info("Stopping bridge service")
        try:
            self.hperfor_client.loop_stop()
            self.hperfor_client.disconnect()
        except Exception:
            LOGGER.exception("Failed to close hperfor MQTT client cleanly")
        for bridge in self.tb_bridges.values():
            bridge.disconnect()

    def publish_to_hperfor(self, client_id: str, payload: dict[str, Any]) -> None:
        device = self.devices_by_client_id.get(client_id)
        if device is None:
            raise ValueError(f"No hperfor device mapping found for client_id={client_id}")

        topic = f"to/epa/{device.hperfor_gateway_id}"
        raw_payload = json.dumps(payload, ensure_ascii=False)
        LOGGER.info("Publish to hperfor topic=%s payload=%s", topic, raw_payload)
        result = self.hperfor_client.publish(topic, raw_payload, qos=0)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed with rc={result.rc}")

    def request_device_data(self, client_id: str, timeout: float = 10.0) -> dict[str, Any]:
        payload = {
            "bid": 208,
            "mid": build_mid(),
            "Children": [{"ClientID": client_id}],
        }
        event = threading.Event()
        response_holder: dict[str, Any] = {}
        with self._state_lock:
            self._pending_data_requests[payload["mid"]] = (event, response_holder)

        try:
            self.publish_to_hperfor(client_id, payload)
            if not event.wait(timeout):
                raise TimeoutError(f"Timed out waiting for bid 208 response for client_id={client_id}")
            child = response_holder.get("child")
            if not isinstance(child, dict):
                raise RuntimeError(f"Invalid bid 208 response for client_id={client_id}")
            return child
        finally:
            with self._state_lock:
                self._pending_data_requests.pop(payload["mid"], None)

    def _on_hperfor_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: mqtt.ReasonCode,
        _properties: Any,
    ) -> None:
        if reason_code != 0:
            LOGGER.error("Failed to connect hperfor MQTT broker: %s", reason_code)
            return
        LOGGER.info("Connected to hperfor MQTT broker")
        for gateway_id in self.gateway_ids:
            topic = f"from/epa/{gateway_id}"
            client.subscribe(topic, qos=0)
            LOGGER.info("Subscribed hperfor topic %s", topic)

    def _on_hperfor_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: Any,
        reason_code: mqtt.ReasonCode,
        _properties: Any,
    ) -> None:
        if self.stop_event.is_set():
            return
        LOGGER.warning("Disconnected from hperfor MQTT broker: %s", reason_code)

    def _on_hperfor_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        topic = message.topic
        payload_text = message.payload.decode("utf-8", errors="replace")
        LOGGER.info("Received hperfor message topic=%s payload=%s", topic, payload_text)

        gateway_id = topic.rsplit("/", 1)[-1]
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            LOGGER.exception("Invalid JSON from hperfor topic=%s", topic)
            return

        fallback_bridge, fallback_client_id = self._resolve_fallback_device(gateway_id, payload)
        if fallback_bridge is None or fallback_client_id is None:
            LOGGER.warning("No ThingsBoard mapping found for gateway_id=%s payload=%s", gateway_id, payload)
            return

        bid = payload.get("bid")
        if bid == 101:
            self.publish_to_hperfor(fallback_client_id, payload)
            fallback_bridge.send_telemetry({"heartbeat": utc_now_iso()})
            return

        if bid == 201:
            self.publish_to_hperfor(fallback_client_id, build_ack_payload(payload))
            self._forward_state_payload(
                fallback_bridge,
                payload,
                fallback_client_id=fallback_client_id,
            )
            return

        if bid in {202, 208}:
            self._forward_state_payload(
                fallback_bridge,
                payload,
                fallback_client_id=fallback_client_id,
            )
            return

        LOGGER.warning("Unsupported hperfor bid=%s payload=%s", bid, payload)

    def _resolve_fallback_device(
        self,
        gateway_id: str,
        payload: dict[str, Any],
    ) -> tuple[ThingsBoardDeviceBridge | None, str | None]:
        children = payload.get("Children")
        if isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_client_id = child.get("ClientID")
                if isinstance(child_client_id, str) and child_client_id in self.tb_bridges:
                    return self.tb_bridges[child_client_id], child_client_id

        for device in self.config.devices:
            if device.hperfor_gateway_id == gateway_id:
                return self.tb_bridges[device.hperfor_client_id], device.hperfor_client_id

        return None, None

    def _forward_state_payload(
        self,
        fallback_bridge: ThingsBoardDeviceBridge,
        payload: dict[str, Any],
        fallback_client_id: str,
    ) -> None:
        if payload.get("result") is False:
            LOGGER.warning(
                "Skip forwarding message without Children for client_id=%s: %s",
                fallback_client_id,
                payload,
            )
            return

        children = payload.get("Children")
        if not isinstance(children, list) or not children:
            LOGGER.warning(
                "Skip forwarding message without valid Children for client_id=%s: %s",
                fallback_client_id,
                payload,
            )
            return

        for child in children:
            if not isinstance(child, dict):
                LOGGER.warning(
                    "Skip non-object child for client_id=%s: %s",
                    fallback_client_id,
                    child,
                )
                continue

            child_client_id = child.get("ClientID", fallback_client_id)
            bridge = self.tb_bridges.get(child_client_id)
            if bridge is None:
                LOGGER.warning(
                    "Skip payload for unmapped client_id=%s, fallback_client_id=%s",
                    child_client_id,
                    fallback_client_id,
                )
                continue

            if payload.get("bid") == 208:
                mid = payload.get("mid")
                if isinstance(mid, str):
                    with self._state_lock:
                        pending_request = self._pending_data_requests.get(mid)
                    if pending_request is not None:
                        event, response_holder = pending_request
                        response_holder["child"] = child
                        event.set()

            telemetry = flatten_child_payload(child)
            bridge.send_telemetry(telemetry)


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = os.getenv("LOG_FILE")
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers,
    )


def main() -> int:
    setup_logging()
    try:
        config = load_config()
    except Exception as exc:
        LOGGER.error("Unable to load config: %s", exc)
        return 1

    service = HperforBridgeService(config)

    def _handle_shutdown(_signum: int, _frame: Any) -> None:
        LOGGER.info("Shutdown signal received")
        service.stop()

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        service.run_forever()
    except KeyboardInterrupt:
        service.stop()
    except Exception:
        LOGGER.exception("Bridge service crashed")
        service.stop()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
