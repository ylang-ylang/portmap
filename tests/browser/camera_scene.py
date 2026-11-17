"""Pad the actual CLI's grayscale PNG for emulator portrait crop-to-fill.

This is a synthetic test scene, not an alternate QR encoder. Producer pixels
are copied verbatim into a white canvas sized for the pinned emulator's crop.
The strict format assertion deliberately follows the owned producer, not arbitrary
untrusted PNG files. No image package, webcam, kernel module or device is used.
"""
import binascii
import struct
import zlib


def padded_cli_png(png):
    if png[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Expected actual CLI PNG signature')
    offset, compressed, dimensions = 8, bytearray(), None
    while offset < len(png):
        size = struct.unpack('>I', png[offset:offset + 4])[0]
        kind = png[offset + 4:offset + 8]
        data = png[offset + 8:offset + 8 + size]
        if kind == b'IHDR':
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', data)
            if width != height or depth != 8 or color != 0 or compression or filtering or interlace:
                raise ValueError('Expected unfiltered 8-bit grayscale CLI PNG')
            dimensions = width, height
        elif kind == b'IDAT':
            compressed.extend(data)
        offset += size + 12
    if dimensions is None:
        raise ValueError('CLI PNG dimensions absent')
    width, height = dimensions
    source = zlib.decompress(compressed)
    if len(source) != (width + 1) * height:
        raise ValueError('Unexpected CLI PNG rows')
    # Native 720x1280 capture left-anchors the imagefile portrait crop and
    # magnifies it. Keep the intact producer QR inside that measured viewport.
    edge = width * 4
    left = width // 4
    top = (edge - height) // 2
    white = b'\0' + b'\xff' * edge
    rows = bytearray(white * top)
    for y in range(height):
        row = source[y * (width + 1):(y + 1) * (width + 1)]
        if row[0] != 0:
            raise ValueError('Expected unfiltered CLI PNG rows')
        rows.extend(b'\0' + b'\xff' * left + row[1:] + b'\xff' * (edge - left - width))
    rows.extend(white * (edge - top - height))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', binascii.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', edge, edge, 8, 0, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b'')
