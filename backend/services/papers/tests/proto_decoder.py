"""gRPC-Web protobuf 解码器 v2（稳健版，支持中文）。"""
from __future__ import annotations
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def decode_varint(b, i):
    val = 0; shift = 0
    while True:
        if i >= len(b): raise IndexError
        byte = b[i]; i += 1
        val |= (byte & 0x7F) << shift
        if not (byte & 0x80): break
        shift += 7
    return val, i

def is_text(val: bytes) -> bool:
    """启发式：是否为可读文本（中文/英文/数字/标点）。"""
    if not val or len(val) < 1:
        return False
    try:
        txt = val.decode('utf-8')
    except UnicodeDecodeError:
        return False
    if len(txt) == 0:
        return False
    # 控制字符占比（换行/制表除外）
    control = sum(1 for ch in txt if ord(ch) < 32 and ch not in '\n\t\r')
    if control / len(txt) > 0.1:
        return False
    # 可打印 + CJK + 常见符号
    printable = sum(1 for ch in txt if (32 <= ord(ch) < 127) or ord(ch) >= 0x2E80)
    return printable / max(1, len(txt)) > 0.6

def parse_fields(b, depth=0, max_depth=5):
    i = 0; fields = []
    while i < len(b):
        try:
            tag, i = decode_varint(b, i)
        except IndexError:
            break
        field = tag >> 3; wt = tag & 7
        entry = {"field": field, "wt": wt}
        try:
            if wt == 0:
                val, i = decode_varint(b, i)
                entry["kind"] = "varint"; entry["val"] = val
            elif wt == 2:
                ln, i = decode_varint(b, i)
                val = b[i:i+ln]; i += ln
                entry["len"] = ln
                if is_text(val):
                    entry["kind"] = "str"; entry["val"] = val.decode('utf-8')
                elif depth < max_depth and ln < 100000:
                    entry["kind"] = "msg"
                    entry["children"] = parse_fields(val, depth+1, max_depth)
                else:
                    entry["kind"] = "bytes"; entry["val"] = val[:20].hex()
            elif wt == 5:
                entry["kind"] = "fixed32"; entry["val"] = b[i:i+4].hex(); i += 4
            elif wt == 1:
                entry["kind"] = "fixed64"; entry["val"] = b[i:i+8].hex(); i += 8
            else:
                entry["kind"] = f"wt{wt}"; i += 1
        except (IndexError, ValueError):
            entry["kind"] = "trunc"; i += 1
        fields.append(entry)
    return fields

def strip_grpc_web(data):
    msgs = []; i = 0
    while i + 5 <= len(data):
        flag = data[i]; ln = int.from_bytes(data[i+1:i+5], 'big')
        msgs.append(data[i+5:i+5+ln]); i += 5 + ln
        if flag != 0: break
    return msgs

def dump(fields, depth=0, out=None, limit=200):
    if out is None: out = []
    for f in fields:
        pad = '  ' * depth
        if f["kind"] == "varint":
            out.append(f'{pad}f{f["field"]} = {f["val"]}')
        elif f["kind"] == "str":
            v = f["val"].replace('\n', '\\n')
            out.append(f'{pad}f{f["field"]} = "{v[:80]}"')
        elif f["kind"] == "msg":
            out.append(f'{pad}f{f["field"]} msg({f["len"]}B)')
            if len(out) < limit:
                dump(f["children"], depth+1, out, limit)
        else:
            out.append(f'{pad}f{f["field"]} {f["kind"]} {f.get("val","")[:40]}')
        if len(out) >= limit:
            out.append('  ...(截断)'); break
    return out

if __name__ == "__main__":
    data = open(sys.argv[1], 'rb').read()
    msgs = strip_grpc_web(data)
    print(f"gRPC-Web 帧数: {len(msgs)}, 总 {len(data)}B")
    for idx, m in enumerate(msgs[:1]):
        print(f"帧 {idx} 顶层:")
        fields = parse_fields(m)
        for line in dump(fields, limit=int(sys.argv[2]) if len(sys.argv) > 2 else 150):
            print(line)
