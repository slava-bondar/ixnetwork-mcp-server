"""Traffic item models.

Unknown keys are rejected. An endpoint is any configuration object (ipv4/ipv6 stack, network
group, dhcp client, device group, topology ...) referenced by name, xpath or href.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class TrafficType(str, Enum):
    RAW = "raw"
    IPV4 = "ipv4"
    IPV6 = "ipv6"
    ETHERNET_VLAN = "ethernetVlan"


class FrameRateType(str, Enum):
    PERCENT_LINE_RATE = "percentLineRate"
    FRAMES_PER_SECOND = "framesPerSecond"
    BITS_PER_SECOND = "bitsPerSecond"
    INTERPACKET_GAP = "interPacketGap"


class FrameSizeType(str, Enum):
    FIXED = "fixed"
    INCREMENT = "increment"
    RANDOM = "random"
    QUAD_GAUSSIAN = "quadGaussian"
    WEIGHTED_PAIRS = "weightedPairs"


class TransmissionType(str, Enum):
    CONTINUOUS = "continuous"
    FIXED_FRAME_COUNT = "fixedFrameCount"
    FIXED_DURATION = "fixedDuration"
    BURST_AUTO = "auto"
    BURST_CUSTOM = "custom"
    BURST_FIXED_DURATION = "burstFixedDuration"


class FrameRateConfig(_M):
    type: FrameRateType = FrameRateType.PERCENT_LINE_RATE
    rate: float = Field(10.0, ge=0)


class FrameSizeConfig(_M):
    type: FrameSizeType = FrameSizeType.FIXED
    fixed_size: int = Field(512, ge=60, le=16000)
    min_size: int | None = Field(None, ge=60)
    max_size: int | None = Field(None, le=16000)
    step_size: int | None = None


class TransmissionConfig(_M):
    type: TransmissionType = TransmissionType.CONTINUOUS
    frame_count: int | None = None
    duration: int | None = None
    min_gap_bytes: int | None = None
    inter_burst_gap: int | None = None
    inter_burst_gap_units: Literal["nanoseconds", "microseconds", "milliseconds", "seconds", "bytes"] = "nanoseconds"
    burst_packet_count: int | None = None
    repeat_burst: int | None = None


class EgressTrackingConfig(_M):
    enabled: bool = False
    encapsulation: str | None = None
    field_offset: str | None = None
    custom_offset_bits: int | None = None
    custom_width_bits: int | None = None


class FlowTrackingConfig(_M):
    track_by: list[str] = Field(default_factory=lambda: ["trackingenabled0"])
    field_width: Literal["eightBits", "sixteenBits", "thirtyTwoBits"] = "thirtyTwoBits"
    protocol_offset: str | None = None
    offset: int | None = None
    egress: EgressTrackingConfig | None = None


class PacketHeaderField(_M):
    field_name: str  # display name or fieldTypeId (describe_packet_headers lists them)
    value_type: Literal["singleValue", "increment", "decrement", "random", "valueList"] = "singleValue"
    single_value: str | None = None
    start_value: str | None = None
    step_value: str | None = None
    count: int | None = None
    value_list: list[str] | None = None


class PacketHeader(_M):
    protocol: str  # template stackTypeId or display name, e.g. udp, tcp, vxlan, mpls, icmpv4
    fields: list[PacketHeaderField] = Field(default_factory=list)


class ConfigElementConfig(_M):
    frame_rate: FrameRateConfig = Field(default_factory=FrameRateConfig)
    frame_size: FrameSizeConfig = Field(default_factory=FrameSizeConfig)
    transmission: TransmissionConfig = Field(default_factory=TransmissionConfig)
    stack: list[PacketHeader] = Field(default_factory=list)


class EndpointRef(_M):
    """One endpoint. Prefer ``ref`` (name of an ipv4/ipv6 stack, network group, dhcp client,
    device group or topology). The legacy keys scope by names: topology + device_group,
    device_group + network_group; vport is for raw traffic."""
    ref: str | None = None
    topology: str | None = None
    device_group: str | None = None
    network_group: str | None = None
    vport: str | None = None

    @model_validator(mode="after")
    def _require_one(self) -> "EndpointRef":
        legacy = (self.topology, self.device_group, self.network_group, self.vport)
        if self.ref is None and all(v is None for v in legacy):
            raise ValueError("endpoint needs 'ref' (or topology / device_group / network_group / vport)")
        if self.ref is not None and any(v is not None for v in legacy):
            raise ValueError("use either 'ref' or the topology/device_group/network_group/vport keys")
        return self


class MulticastDestinationConfig(_M):
    """"225.0.0.1", "225.0.0.1/0.0.0.1/4" (group/step/count) or {group, step, count}."""
    group: str
    count: int = 1
    step: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _parse_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            parts = v.split("/")
            return {"group": parts[0], "step": parts[1] if len(parts) > 1 else None,
                    "count": int(parts[2]) if len(parts) > 2 else 1}
        return v


class EndpointSetConfig(_M):
    sources: list[EndpointRef] = Field(min_length=1)
    destinations: list[EndpointRef] = Field(default_factory=list)
    multicast_destinations: list[MulticastDestinationConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dest(self) -> "EndpointSetConfig":
        if bool(self.destinations) == bool(self.multicast_destinations):
            raise ValueError("give either destinations (unicast) or multicast_destinations")
        return self


class TrafficItemConfig(_M):
    name: str
    traffic_type: TrafficType = TrafficType.IPV4
    bi_directional: bool = False
    self_destined: bool = False
    src_dest_mesh: Literal["oneToOne", "manyToMany", "fullMesh"] = "oneToOne"
    enabled: bool = True
    endpoint_sets: list[EndpointSetConfig] = Field(min_length=1)
    config_elements: list[ConfigElementConfig] = Field(default_factory=lambda: [ConfigElementConfig()])
    flow_tracking: FlowTrackingConfig = Field(default_factory=FlowTrackingConfig)

    @model_validator(mode="after")
    def _mcast_unidirectional(self) -> "TrafficItemConfig":
        if self.bi_directional and any(es.multicast_destinations for es in self.endpoint_sets):
            raise ValueError("bi_directional must be false for multicast traffic")
        return self

    def config_element_for(self, index: int) -> ConfigElementConfig:
        return self.config_elements[min(index, len(self.config_elements) - 1)] if self.config_elements \
            else ConfigElementConfig()
