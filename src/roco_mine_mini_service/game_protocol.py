"""Binary protocol primitives used by the directory and game servers."""

from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass

MAGIC = 0x9527
HEADER_SIZE = 20

DIR_INIT = 0x00070104
RECOMMEND_ROOM_REQUEST = 0x00070001
RECOMMEND_ROOM_REPLY = 0x00070002
ROOM_INIT_COMPLETE = 0x00010002
ENTER_ROOM = 0x00030001
SCENE_JUMP = 0x00030004
HEARTBEAT = 0x00030017
MINIGAME_START = 0x00030025
ONLINE_TIME = 0x00030033
TIME_PAUSE = 0x0003008D
FARM_SEED_INVENTORY = 0x00030023
MANOR_GROUND_INFO = 0x00120002
MANOR_PLANT_SOW = 0x00120004
MANOR_PLANT_REAP = 0x00120005
PARADISE_SPIRIT_LIST = 0x000327F6
MANOR_LAND_COUNT = 16
MANOR_LAND_SIZE = 22
MANOR_GROUNDS_OFFSET = 76


class GameProtocolError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Packet:
    version: int
    command: int
    uin: int
    serial: int
    checksum: int
    body: bytes


@dataclass(frozen=True, slots=True)
class ReturnPayload:
    code: int
    message: str
    data: bytes


@dataclass(frozen=True, slots=True)
class Room:
    room_type: int
    room_index: int
    room_status: int
    room_limit: int
    room_person: int
    room_attribute: int
    room_ip: str
    zone_id: int
    port: int
    carrier: int


@dataclass(frozen=True, slots=True)
class FarmLand:
    ground_id: int
    ground_status: int
    seed_id: int
    plant_status: int
    current_time: int
    total_time: int
    total_produce: int
    left_produce: int
    has_grass: bool
    has_insect: bool
    has_fruit: bool
    season: int
    left_row_times: int

    @property
    def unlocked(self) -> bool:
        return self.ground_status == 2

    @property
    def empty(self) -> bool:
        return self.seed_id == 0


@dataclass(frozen=True, slots=True)
class FarmSeed:
    seed_id: int
    count: int


@dataclass(frozen=True, slots=True)
class ManorGrounds:
    manor_level: int
    lands: tuple[FarmLand, ...]


@dataclass(frozen=True, slots=True)
class FarmSowResult:
    experience: int
    land: FarmLand


@dataclass(frozen=True, slots=True)
class FarmHarvestResult:
    result: int
    experience: int
    fruit_count: int
    land: FarmLand


@dataclass(frozen=True, slots=True)
class ParadiseScene:
    level: int
    experience: int
    level_limit: int
    spirit_limit: int
    train_limit: int
    adventure_limit: int
    spirit_ids: tuple[int, ...]


class PacketAssembler:
    """Reassemble ADF packets from arbitrary TCP chunks."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        packets: list[bytes] = []
        while True:
            magic_index = self._buffer.find(b"\x95\x27")
            if magic_index < 0:
                if len(self._buffer) > 1:
                    del self._buffer[:-1]
                break
            if magic_index:
                del self._buffer[:magic_index]
            if len(self._buffer) < HEADER_SIZE:
                break
            body_length = struct.unpack_from(">H", self._buffer, 18)[0]
            packet_length = HEADER_SIZE + body_length
            if len(self._buffer) < packet_length:
                break
            packets.append(bytes(self._buffer[:packet_length]))
            del self._buffer[:packet_length]
        return packets

    def reset(self) -> None:
        self._buffer.clear()


def build_packet(
    command: int,
    uin: int,
    body: bytes = b"",
    *,
    version: int = 0,
    serial: int = 0,
    checksum: int = 0,
) -> bytes:
    if not 0 <= uin <= 0xFFFF_FFFF:
        raise ValueError("UIN does not fit the game protocol")
    if len(body) > 0xFFFF:
        raise ValueError("packet body is too large")
    return struct.pack(
        ">HHIIIHH",
        MAGIC,
        version,
        command,
        uin,
        serial,
        checksum,
        len(body),
    ) + body


def parse_packet(raw: bytes) -> Packet:
    if len(raw) < HEADER_SIZE:
        raise GameProtocolError("packet is shorter than the ADF header")
    magic, version, command, uin, serial, checksum, body_length = struct.unpack_from(
        ">HHIIIHH", raw
    )
    if magic != MAGIC:
        raise GameProtocolError("invalid ADF packet magic")
    if len(raw) != HEADER_SIZE + body_length:
        raise GameProtocolError("ADF packet length mismatch")
    return Packet(
        version=version,
        command=command,
        uin=uin,
        serial=serial,
        checksum=checksum,
        body=raw[HEADER_SIZE:],
    )


def parse_return_payload(packet: Packet) -> ReturnPayload:
    if len(packet.body) < 6:
        raise GameProtocolError("response does not contain a return code")
    code, message_length = struct.unpack_from(">iH", packet.body)
    data_offset = 6 + message_length
    if len(packet.body) < data_offset:
        raise GameProtocolError("response message length is invalid")
    message = packet.body[6:data_offset].decode("gb2312", errors="replace")
    return ReturnPayload(code=code, message=message, data=packet.body[data_offset:])


def require_success(packet: Packet) -> ReturnPayload:
    result = parse_return_payload(packet)
    if result.code != 0:
        message = result.message or "game server rejected the request"
        raise GameProtocolError(f"{message} ({result.code})")
    return result


def build_recommend_room_request(uin: int, angel_key: str) -> bytes:
    body = struct.pack(">HHHH", 2, 0, 0, 0) + _fixed_text(angel_key, 64)
    return build_packet(RECOMMEND_ROOM_REQUEST, uin, body, version=1)


def parse_recommended_rooms(packet: Packet) -> list[Room]:
    if packet.command != RECOMMEND_ROOM_REPLY:
        raise GameProtocolError("not a recommended-room response")
    payload = require_success(packet).data
    if len(payload) < 2:
        raise GameProtocolError("recommended-room response is empty")
    count = struct.unpack_from(">H", payload)[0]
    position = 2
    rooms: list[Room] = []
    for _ in range(count):
        if len(payload) < position + 20:
            raise GameProtocolError("recommended-room response is truncated")
        (
            room_type,
            room_index,
            room_status,
            room_limit,
            room_person,
            room_attribute,
            raw_ip,
            zone_id,
            port,
            carrier,
        ) = struct.unpack_from(">BHBHHBIIHB", payload, position)
        position += 20
        ip_bytes = struct.pack(">I", raw_ip)[::-1]
        rooms.append(
            Room(
                room_type=room_type,
                room_index=room_index,
                room_status=room_status,
                room_limit=room_limit,
                room_person=room_person,
                room_attribute=room_attribute,
                room_ip=str(ipaddress.ip_address(ip_bytes)),
                zone_id=zone_id,
                port=port,
                carrier=carrier,
            )
        )
    return rooms


def choose_least_populated_room(rooms: list[Room]) -> Room:
    available = [
        room
        for room in rooms
        if 1 <= room.room_index <= 250
        and room.room_status == 0
        and room.room_person < room.room_limit
        and room.port > 0
        and room.zone_id > 0
    ]
    if not available:
        raise GameProtocolError("no available recommended room")
    return min(available, key=lambda room: (room.room_person, room.room_index))


def build_enter_room_request(uin: int, room_id: int, angel_key: str) -> bytes:
    body = struct.pack(">H", room_id) + _fixed_text(angel_key, 64)
    return build_packet(ENTER_ROOM, uin, body)


def parse_enter_room_response(packet: Packet) -> tuple[int, int, int]:
    payload = require_success(packet).data
    if len(payload) < 6:
        raise GameProtocolError("enter-room response is truncated")
    return struct.unpack_from(">HHH", payload)


def build_scene_jump_request(
    uin: int,
    current_scene: int,
    target_scene: int,
    *,
    version: int = 0,
) -> bytes:
    body = struct.pack(">HHIH", current_scene, target_scene, 0, version)
    return build_packet(SCENE_JUMP, uin, body)


def parse_scene_jump_response(packet: Packet) -> tuple[int, int]:
    payload = require_success(packet).data
    if len(payload) < 4:
        raise GameProtocolError("scene-jump response is truncated")
    return struct.unpack_from(">HH", payload)


def build_time_pause_request(uin: int, enabled: bool) -> bytes:
    body = _protobuf_varint_field(1, 1 if enabled else 0)
    body += _protobuf_varint_field(2, uin)
    return build_packet(TIME_PAUSE, uin, body)


def parse_time_pause_response(packet: Packet) -> int:
    fields = _parse_protobuf(packet.body)
    return_info = fields.get(1)
    if not isinstance(return_info, bytes):
        raise GameProtocolError("time-pause response has no return info")
    nested = _parse_protobuf(return_info)
    code = nested.get(1)
    if not isinstance(code, int):
        raise GameProtocolError("time-pause response has no return code")
    if code != 0:
        raise GameProtocolError(f"time-pause request failed ({code})")
    online_time = fields.get(2, 0)
    return online_time if isinstance(online_time, int) else 0


def build_online_time_request(uin: int) -> bytes:
    return build_packet(ONLINE_TIME, uin)


def parse_online_time_response(packet: Packet) -> int:
    if len(packet.body) < 10:
        raise GameProtocolError("online-time response is truncated")
    return struct.unpack_from(">H", packet.body, 8)[0]


def build_minigame_start_request(uin: int, game_id: int = 2121) -> bytes:
    return build_packet(MINIGAME_START, uin, struct.pack(">I", game_id))


def build_manor_query_request(uin: int) -> bytes:
    return build_packet(MANOR_GROUND_INFO, uin, struct.pack(">I", uin))


def parse_manor_query_response(packet: Packet) -> ManorGrounds:
    if packet.command != MANOR_GROUND_INFO:
        raise GameProtocolError("not a manor-ground response")
    payload = require_success(packet).data
    expected_size = MANOR_GROUNDS_OFFSET + MANOR_LAND_COUNT * MANOR_LAND_SIZE
    if len(payload) < expected_size:
        raise GameProtocolError("manor-ground response is truncated")
    manor_level = struct.unpack_from(">H", payload, 4)[0]
    lands = tuple(
        _parse_farm_land(payload, MANOR_GROUNDS_OFFSET + index * MANOR_LAND_SIZE)
        for index in range(MANOR_LAND_COUNT)
    )
    expected_ids = set(range(MANOR_LAND_COUNT))
    actual_ids = {land.ground_id for land in lands}
    if actual_ids != expected_ids:
        raise GameProtocolError("manor-ground response has invalid land identifiers")
    return ManorGrounds(manor_level=manor_level, lands=lands)


def build_farm_seed_inventory_request(uin: int) -> bytes:
    # Item type 6 is FARM and subtype 1 is SEED in the reference client.
    return build_packet(FARM_SEED_INVENTORY, uin, bytes((6, 1)))


def parse_farm_seed_inventory_response(packet: Packet) -> tuple[FarmSeed, ...]:
    if packet.command != FARM_SEED_INVENTORY:
        raise GameProtocolError("not a farm-seed inventory response")
    payload = require_success(packet).data
    if len(payload) < 2:
        raise GameProtocolError("farm-seed inventory response is truncated")
    count = struct.unpack_from(">H", payload)[0]
    expected_size = 2 + count * 6
    if len(payload) < expected_size:
        raise GameProtocolError("farm-seed inventory response is truncated")
    seeds = [
        FarmSeed(
            seed_id=struct.unpack_from(">I", payload, 2 + index * 6)[0],
            count=struct.unpack_from(">H", payload, 6 + index * 6)[0],
        )
        for index in range(count)
    ]
    return tuple(seed for seed in seeds if seed.seed_id > 0 and seed.count > 0)


def build_manor_sow_request(uin: int, seed_id: int, ground_id: int) -> bytes:
    _validate_seed_id(seed_id)
    _validate_ground_id(ground_id)
    return build_packet(
        MANOR_PLANT_SOW,
        uin,
        struct.pack(">IB", seed_id, ground_id),
    )


def parse_manor_sow_response(packet: Packet) -> FarmSowResult:
    if packet.command != MANOR_PLANT_SOW:
        raise GameProtocolError("not a manor-sow response")
    payload = require_success(packet).data
    if len(payload) < 2 + MANOR_LAND_SIZE:
        raise GameProtocolError("manor-sow response is truncated")
    return FarmSowResult(
        experience=struct.unpack_from(">H", payload)[0],
        land=_parse_farm_land(payload, 2),
    )


def build_manor_reap_request(
    uin: int,
    ground_id: int,
    *,
    pskey: str,
    skey: str,
) -> bytes:
    _validate_ground_id(ground_id)
    body = struct.pack(">IB", uin, ground_id)
    body += _login_sign(pskey=pskey, skey=skey)
    return build_packet(MANOR_PLANT_REAP, uin, body)


def parse_manor_reap_response(packet: Packet) -> FarmHarvestResult:
    if packet.command != MANOR_PLANT_REAP:
        raise GameProtocolError("not a manor-reap response")
    payload = require_success(packet).data
    fixed_size = 13 + MANOR_LAND_SIZE + 1 + 2
    if len(payload) < fixed_size:
        raise GameProtocolError("manor-reap response is truncated")
    result, experience, fruit_count = struct.unpack_from(">HHB", payload, 8)
    return FarmHarvestResult(
        result=result,
        experience=experience,
        fruit_count=fruit_count,
        land=_parse_farm_land(payload, 13),
    )


def build_heartbeat(uin: int) -> bytes:
    return build_packet(HEARTBEAT, uin)


def build_paradise_spirit_list_request(uin: int) -> bytes:
    return build_packet(PARADISE_SPIRIT_LIST, uin)


def parse_paradise_spirit_list_response(packet: Packet) -> ParadiseScene:
    if packet.command != PARADISE_SPIRIT_LIST:
        raise GameProtocolError("not a paradise-spirit-list response")
    fields = _parse_protobuf_fields(packet.body)
    return_info = next(
        (value for field, value in fields if field == 1),
        None,
    )
    if not isinstance(return_info, bytes):
        raise GameProtocolError("paradise response has no return info")
    nested = dict(_parse_protobuf_fields(return_info))
    code = nested.get(1)
    if not isinstance(code, int):
        raise GameProtocolError("paradise response has no return code")
    if code != 0:
        raw_message = nested.get(2, b"")
        message = (
            _decode_text(raw_message)
            if isinstance(raw_message, bytes)
            else "paradise request failed"
        )
        raise GameProtocolError(f"{message or 'paradise request failed'} ({code})")

    scalar_fields = {
        field: value
        for field, value in fields
        if field != 8 and isinstance(value, int)
    }
    spirit_ids = tuple(
        value
        for field, value in fields
        if field == 8 and isinstance(value, int) and value > 0
    )
    return ParadiseScene(
        level=int(scalar_fields.get(2, 0)),
        experience=int(scalar_fields.get(3, 0)),
        level_limit=int(scalar_fields.get(4, 0)),
        spirit_limit=int(scalar_fields.get(5, 0)),
        train_limit=int(scalar_fields.get(6, 0)),
        adventure_limit=int(scalar_fields.get(7, 0)),
        spirit_ids=spirit_ids,
    )


def _parse_farm_land(data: bytes, offset: int) -> FarmLand:
    if len(data) < offset + MANOR_LAND_SIZE:
        raise GameProtocolError("farm-land payload is truncated")
    (
        ground_id,
        ground_status,
        seed_id,
        plant_status,
        current_time,
        total_time,
        total_produce,
        left_produce,
        has_grass,
        has_insect,
        has_fruit,
        season,
        left_row_times,
    ) = struct.unpack_from(">BBIBIIBBBBBBB", data, offset)
    return FarmLand(
        ground_id=ground_id,
        ground_status=ground_status,
        seed_id=seed_id,
        plant_status=plant_status,
        current_time=current_time,
        total_time=total_time,
        total_produce=total_produce,
        left_produce=left_produce,
        has_grass=has_grass == 1,
        has_insect=has_insect == 1,
        has_fruit=has_fruit == 1,
        season=season,
        left_row_times=left_row_times,
    )


def _login_sign(*, pskey: str, skey: str) -> bytes:
    value = pskey or skey or "0123456789"
    encoded = value.encode("gb2312")
    if len(encoded) > 0xFFFF:
        raise ValueError("login sign is too long")
    return struct.pack(">H", len(encoded)) + encoded


def _validate_ground_id(ground_id: int) -> None:
    if not 0 <= ground_id < MANOR_LAND_COUNT:
        raise ValueError("ground id must be between 0 and 15")


def _validate_seed_id(seed_id: int) -> None:
    if not 1 <= seed_id <= 0xFFFF_FFFF:
        raise ValueError("seed id does not fit the game protocol")


def _fixed_text(value: str, length: int) -> bytes:
    encoded = value.encode("gb2312")
    if len(encoded) > length:
        encoded = encoded[:length]
    return encoded.ljust(length, b"\x00")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf varint cannot be negative")
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            output.append(byte | 0x80)
        else:
            output.append(byte)
            return bytes(output)


def _protobuf_varint_field(field_number: int, value: int) -> bytes:
    return _encode_varint(field_number << 3) + _encode_varint(value)


def _parse_protobuf(data: bytes) -> dict[int, int | bytes]:
    return dict(_parse_protobuf_fields(data))


def _parse_protobuf_fields(data: bytes) -> list[tuple[int, int | bytes]]:
    fields: list[tuple[int, int | bytes]] = []
    position = 0
    while position < len(data):
        tag, position = _decode_varint(data, position)
        field_number = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:
            value, position = _decode_varint(data, position)
            fields.append((field_number, value))
        elif wire_type == 2:
            length, position = _decode_varint(data, position)
            end = position + length
            if end > len(data):
                raise GameProtocolError("truncated protobuf field")
            fields.append((field_number, data[position:end]))
            position = end
        else:
            raise GameProtocolError(f"unsupported protobuf wire type {wire_type}")
    return fields


def _decode_text(value: bytes) -> str:
    for encoding in ("utf-8", "gb2312"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return value.decode("utf-8", errors="replace")


def _decode_varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < len(data) and shift <= 63:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, position
        shift += 7
    raise GameProtocolError("invalid protobuf varint")
