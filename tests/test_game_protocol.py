from __future__ import annotations

import struct
import unittest

from roco_mine_mini_service.game_protocol import (
    FARM_SEED_INVENTORY,
    MANOR_GROUND_INFO,
    MANOR_LAND_COUNT,
    MANOR_PLANT_REAP,
    MANOR_PLANT_SOW,
    PARADISE_SPIRIT_LIST,
    RECOMMEND_ROOM_REPLY,
    TIME_PAUSE,
    Packet,
    PacketAssembler,
    build_farm_seed_inventory_request,
    build_manor_query_request,
    build_manor_reap_request,
    build_manor_sow_request,
    build_packet,
    build_paradise_spirit_list_request,
    build_recommend_room_request,
    build_time_pause_request,
    choose_least_populated_room,
    parse_farm_seed_inventory_response,
    parse_manor_query_response,
    parse_manor_reap_response,
    parse_manor_sow_response,
    parse_packet,
    parse_paradise_spirit_list_response,
    parse_recommended_rooms,
    parse_time_pause_response,
)


def successful_body(data: bytes = b"", message: bytes = b"") -> bytes:
    return struct.pack(">iH", 0, len(message)) + message + data


def room_record(
    room_index: int,
    people: int,
    *,
    limit: int = 500,
    status: int = 0,
    zone_id: int = 7,
) -> bytes:
    return struct.pack(
        ">BHBHHBIIHB",
        0,
        room_index,
        status,
        limit,
        people,
        0,
        0x0B9FD9AC,
        zone_id,
        443,
        0,
    )


def farm_land_record(
    ground_id: int,
    *,
    ground_status: int = 2,
    seed_id: int = 0,
    plant_status: int = 0,
    has_fruit: bool = False,
) -> bytes:
    return struct.pack(
        ">BBIBIIBBBBBBB",
        ground_id,
        ground_status,
        seed_id,
        plant_status,
        120,
        300,
        5,
        2,
        0,
        0,
        int(has_fruit),
        1,
        0,
    )


def protobuf_varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        encoded.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(encoded)


def protobuf_int(field: int, value: int) -> bytes:
    return protobuf_varint(field << 3) + protobuf_varint(value)


def protobuf_bytes(field: int, value: bytes) -> bytes:
    return (
        protobuf_varint((field << 3) | 2)
        + protobuf_varint(len(value))
        + value
    )


class GameProtocolTests(unittest.TestCase):
    def test_packet_header_matches_kotlin_adf_layout(self) -> None:
        packet = build_packet(0x00030025, 12345678, struct.pack(">I", 2121))

        parsed = parse_packet(packet)

        self.assertEqual(packet[:2], b"\x95\x27")
        self.assertEqual(parsed.command, 0x00030025)
        self.assertEqual(parsed.uin, 12345678)
        self.assertEqual(parsed.body, struct.pack(">I", 2121))

    def test_packet_assembler_handles_noise_and_fragmentation(self) -> None:
        first = build_packet(1, 12345678, b"first")
        second = build_packet(2, 12345678, b"second")
        assembler = PacketAssembler()

        self.assertEqual(assembler.feed(b"noise" + first[:13]), [])
        packets = assembler.feed(first[13:] + second)

        self.assertEqual(packets, [first, second])

    def test_recommend_request_has_version_one_and_64_byte_key(self) -> None:
        request = parse_packet(
            build_recommend_room_request(12345678, "A" * 64)
        )

        self.assertEqual(request.version, 1)
        self.assertEqual(len(request.body), 72)
        self.assertEqual(request.body[:8], struct.pack(">HHHH", 2, 0, 0, 0))
        self.assertEqual(request.body[8:], b"A" * 64)

    def test_least_populated_available_recommended_room_is_selected(self) -> None:
        records = b"".join(
            [
                room_record(5, 188),
                room_record(244, 98),
                room_record(145, 80, status=1),
                room_record(52, 116),
            ]
        )
        packet = Packet(
            version=0,
            command=RECOMMEND_ROOM_REPLY,
            uin=12345678,
            serial=0,
            checksum=0,
            body=successful_body(struct.pack(">H", 4) + records),
        )

        selected = choose_least_populated_room(parse_recommended_rooms(packet))

        self.assertEqual(selected.room_index, 244)
        self.assertEqual(selected.room_person, 98)

    def test_time_pause_request_and_response_use_protobuf(self) -> None:
        request = parse_packet(build_time_pause_request(12345678, True))
        response = Packet(
            version=0,
            command=TIME_PAUSE,
            uin=12345678,
            serial=0,
            checksum=0,
            body=b"\x0a\x02\x08\x00\x10\x7b",
        )

        self.assertEqual(request.body[:3], b"\x08\x01\x10")
        self.assertEqual(parse_time_pause_response(response), 123)

    def test_manor_query_parses_exactly_sixteen_protocol_lands(self) -> None:
        prefix = bytearray(76)
        struct.pack_into(">H", prefix, 4, 42)
        lands = b"".join(
            farm_land_record(
                ground_id,
                seed_id=0x0601004A if ground_id < 8 else 0,
                plant_status=4 if ground_id == 0 else 1,
                has_fruit=ground_id == 0,
            )
            for ground_id in range(MANOR_LAND_COUNT)
        )
        response = Packet(
            version=0,
            command=MANOR_GROUND_INFO,
            uin=12345678,
            serial=0,
            checksum=0,
            body=successful_body(bytes(prefix) + lands),
        )

        request = parse_packet(build_manor_query_request(12345678))
        manor = parse_manor_query_response(response)

        self.assertEqual(request.command, MANOR_GROUND_INFO)
        self.assertEqual(request.body, struct.pack(">I", 12345678))
        self.assertEqual(manor.manor_level, 42)
        self.assertEqual(len(manor.lands), 16)
        self.assertEqual([land.ground_id for land in manor.lands], list(range(16)))
        self.assertTrue(manor.lands[0].has_fruit)
        self.assertTrue(manor.lands[8].empty)

    def test_farm_seed_inventory_uses_reference_item_type_and_layout(self) -> None:
        response = Packet(
            version=0,
            command=FARM_SEED_INVENTORY,
            uin=12345678,
            serial=0,
            checksum=0,
            body=successful_body(
                struct.pack(">H", 3)
                + struct.pack(">IH", 0x0601004A, 8)
                + struct.pack(">IH", 0x0601004B, 0)
                + struct.pack(">IH", 0x0601004C, 3)
            ),
        )

        request = parse_packet(build_farm_seed_inventory_request(12345678))
        seeds = parse_farm_seed_inventory_response(response)

        self.assertEqual(request.command, FARM_SEED_INVENTORY)
        self.assertEqual(request.body, bytes((6, 1)))
        self.assertEqual(
            [(seed.seed_id, seed.count) for seed in seeds],
            [(0x0601004A, 8), (0x0601004C, 3)],
        )

    def test_manor_sow_and_reap_match_reference_command_layouts(self) -> None:
        sow_request = parse_packet(
            build_manor_sow_request(12345678, 0x0601004A, 7)
        )
        reap_request = parse_packet(
            build_manor_reap_request(
                12345678,
                7,
                pskey="P" * 32,
                skey="S" * 16,
            )
        )
        skey_reap_request = parse_packet(
            build_manor_reap_request(
                12345678,
                7,
                pskey="",
                skey="S" * 16,
            )
        )
        planted_land = farm_land_record(
            7,
            seed_id=0x0601004A,
            plant_status=1,
        )
        sow_response = Packet(
            version=0,
            command=MANOR_PLANT_SOW,
            uin=12345678,
            serial=0,
            checksum=0,
            body=successful_body(struct.pack(">H", 9) + planted_land),
        )
        harvested_land = farm_land_record(7)
        reap_response = Packet(
            version=0,
            command=MANOR_PLANT_REAP,
            uin=12345678,
            serial=0,
            checksum=0,
            body=successful_body(
                struct.pack(">IIHHB", 12345678, 0, 0, 12, 5)
                + harvested_land
                + struct.pack(">BH", 0, 0)
            ),
        )

        sow = parse_manor_sow_response(sow_response)
        reap = parse_manor_reap_response(reap_response)

        self.assertEqual(sow_request.command, MANOR_PLANT_SOW)
        self.assertEqual(sow_request.body, struct.pack(">IB", 0x0601004A, 7))
        self.assertEqual(reap_request.command, MANOR_PLANT_REAP)
        self.assertEqual(len(reap_request.body), 39)
        self.assertEqual(reap_request.body[:5], struct.pack(">IB", 12345678, 7))
        self.assertEqual(reap_request.body[5:7], struct.pack(">H", 32))
        self.assertEqual(reap_request.body[7:], b"P" * 32)
        self.assertEqual(skey_reap_request.body[5:7], struct.pack(">H", 16))
        self.assertEqual(skey_reap_request.body[7:], b"S" * 16)
        self.assertEqual(sow.experience, 9)
        self.assertEqual(sow.land.seed_id, 0x0601004A)
        self.assertEqual(reap.result, 0)
        self.assertEqual(reap.experience, 12)
        self.assertEqual(reap.fruit_count, 5)
        self.assertEqual(reap.land.ground_id, 7)
        with self.assertRaisesRegex(ValueError, "ground id"):
            build_manor_sow_request(12345678, 0x0601004A, 16)
        with self.assertRaisesRegex(ValueError, "seed id"):
            build_manor_sow_request(12345678, 0, 0)

    def test_paradise_spirit_list_matches_kotlin_protobuf_layout(self) -> None:
        return_info = protobuf_int(1, 0) + protobuf_bytes(2, b"ok")
        body = b"".join(
            [
                protobuf_bytes(1, return_info),
                protobuf_int(2, 9),
                protobuf_int(3, 321),
                protobuf_int(4, 30),
                protobuf_int(5, 5),
                protobuf_int(6, 10),
                protobuf_int(7, 5),
                protobuf_int(8, 101),
                protobuf_int(8, 202),
                protobuf_int(8, 303),
            ]
        )
        response = Packet(
            version=0,
            command=PARADISE_SPIRIT_LIST,
            uin=12345678,
            serial=0,
            checksum=0,
            body=body,
        )

        request = parse_packet(build_paradise_spirit_list_request(12345678))
        paradise = parse_paradise_spirit_list_response(response)

        self.assertEqual(request.command, PARADISE_SPIRIT_LIST)
        self.assertEqual(request.body, b"")
        self.assertEqual(paradise.level, 9)
        self.assertEqual(paradise.experience, 321)
        self.assertEqual(paradise.adventure_limit, 5)
        self.assertEqual(paradise.spirit_ids, (101, 202, 303))


if __name__ == "__main__":
    unittest.main()
