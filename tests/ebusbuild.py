"""Build valid wire-format telegrams for tests (independent of the parser under test)."""

from ebusnimbus.telegram import crc_of, escape


def master_part(src, dst, pbsb, data=b""):
    logical = bytes([src, dst]) + bytes.fromhex(pbsb) + bytes([len(data)]) + data
    return logical + bytes([crc_of(logical)])


def broadcast(src, pbsb, data=b"") -> str:
    return escape(master_part(src, 0xFE, pbsb, data)).hex()


def master_slave(src, dst, pbsb, data=b"", reply=b"", ack=0x00, final_ack=0x00) -> str:
    wire = escape(master_part(src, dst, pbsb, data)) + bytes([ack])
    rl = bytes([len(reply)]) + reply
    wire += escape(rl + bytes([crc_of(rl)])) + bytes([final_ack])
    return wire.hex()


def master_master(src, dst, pbsb, data=b"", ack=0x00) -> str:
    return (escape(master_part(src, dst, pbsb, data)) + bytes([ack])).hex()
