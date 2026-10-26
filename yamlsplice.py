#!/usr/bin/env python3
"""yamlsplice —— 按路径读写 YAML，只改你点的那几个字节。

设计前提：YAML 文件是人的资产，不是程序的中间结果。所以这里不做
「解析成树 → 再序列化」这件事，而是先把每个值在原文里的字节位置记下来，
改的时候只替换那一段，其余字节原封不动。

这样换来的好处是：注释、空行、键顺序、缩进宽度、引号风格、锚点、
Unicode 键名、CRLF、文件末尾有没有换行——全都不需要「支持」，
因为它们从来没被读进去再写出来过。

只用标准库。Python 3.8+。
"""

from __future__ import annotations

import bisect
import json
import re
import sys

__version__ = "1.0.0"

USAGE = """\
yamlsplice —— 按路径读写 YAML，不动其他字节

用法:
  yamlsplice get <path> <file> [--json]
  yamlsplice set <path> <value> <file> [-i | --in-place] [--raw]
  yamlsplice del <path> <file> [-i | --in-place]

路径写法:
  services.web.image       嵌套键
  services.web.ports[0]    数组下标（负数从末尾数）
  services.web.ports[+]    追加到数组末尾（只用于 set）
  services["a.b"]          键里带点或空格时用方括号
  .                        整个文档

选项:
  -i, --in-place   直接改写文件（默认输出到标准输出）
  --json           以 JSON 输出 get 的结果
  --raw            把 set 的值当成纯字符串，不做类型推断
  -h, --help       显示这段说明
  --version        显示版本号

退出码:
  0  成功
  1  文件读不了或 YAML 解析失败
  2  路径不存在
  3  参数用法错误
"""


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class YamlSpliceError(Exception):
    """所有对外错误都从这里派生，CLI 负责翻译成人话。"""

    exit_code = 1


class ParseError(YamlSpliceError):
    exit_code = 1


class PathError(YamlSpliceError):
    exit_code = 2


class UsageError(YamlSpliceError):
    exit_code = 3


# --------------------------------------------------------------------------
# 行模型：把原文切成行，每行记住自己在原文里的绝对偏移
# --------------------------------------------------------------------------


class Line:
    __slots__ = ("start", "text", "eol", "indent", "trivia")

    def __init__(self, start, text, eol):
        self.start = start
        self.text = text
        self.eol = eol
        stripped = text.lstrip(" ")
        self.indent = len(text) - len(stripped)
        self.trivia = stripped == "" or stripped.startswith("#")

    @property
    def end(self):
        return self.start + len(self.text)

    @property
    def stop(self):
        """这一行连同换行符结束的位置。"""
        return self.start + len(self.text) + len(self.eol)

    @property
    def body(self):
        return self.text[self.indent:]

    def __repr__(self):
        return "Line(%d, %r)" % (self.start, self.text)


LINE_BREAK = re.compile(r"\r\n|\r|\n")


def split_lines(text):
    """按 \\n / \\r\\n / \\r 切行，保留每行的换行符原样。"""
    lines = []
    pos = 0
    for m in LINE_BREAK.finditer(text):
        lines.append(Line(pos, text[pos:m.start()], m.group()))
        pos = m.end()
    if pos < len(text):
        lines.append(Line(pos, text[pos:], ""))
    return lines


# --------------------------------------------------------------------------
# 节点：只有种类、风格和「我在原文里占哪几个字节」
# --------------------------------------------------------------------------


class Node:
    __slots__ = ("kind", "style", "start", "end", "entries", "items", "slot")

    def __init__(self, kind, style, start, end, entries=None, items=None):
        self.kind = kind          # map / seq / scalar
        self.style = style        # plain/single/double/literal/folded/flow/alias/null
        self.start = start
        self.end = end
        self.entries = entries    # map 用
        self.items = items        # seq 用
        self.slot = (start, end)  # 父节点给的「可以整体替换的范围」

    def __repr__(self):
        return "Node(%s/%s, %d:%d)" % (self.kind, self.style, self.start, self.end)


class Entry:
    __slots__ = ("key", "key_raw", "key_start", "node", "line_index", "indent")

    def __init__(self, key, key_raw, key_start, node, line_index, indent):
        self.key = key
        self.key_raw = key_raw
        self.key_start = key_start
        self.node = node
        self.line_index = line_index
        self.indent = indent

    def __repr__(self):
        return "Entry(%r -> %r)" % (self.key, self.node)


# --------------------------------------------------------------------------
# 扫描小工具
# --------------------------------------------------------------------------

DQ_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028",
    "P": "\u2029",
}


def _is_seq_dash(content):
    return content == "-" or content.startswith("- ") or content.startswith("-\t")


def find_colon(text):
    """找键值分隔的那个冒号：深度 0、引号外、后面跟空格或行尾。"""
    quote = None
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if quote:
            if quote == '"' and c == "\\":
                i += 2
                continue
            if c == quote:
                if quote == "'" and i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            i += 1
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t"):
            return None
        elif c == ":" and depth == 0:
            if i + 1 == n or text[i + 1] in " \t":
                return i
        i += 1
    return None


def scan_quoted(text, i):
    """从 i 处的引号开始，返回闭引号之后的下标。"""
    quote = text[i]
    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if quote == '"':
            if c == "\\":
                j += 2
                continue
            if c == '"':
                return j + 1
        else:
            if c == "'":
                if j + 1 < n and text[j + 1] == "'":
                    j += 2
                    continue
                return j + 1
        j += 1
    raise ParseError("第 %d 个字符处的引号没有闭合" % (i + 1))


def scan_flow(text, i):
    """从 [ 或 { 开始做括号配对，返回配对结束之后的下标。"""
    opener = text[i]
    closer = "]" if opener == "[" else "}"
    depth = 0
    j = i
    n = len(text)
    while j < n:
        c = text[j]
        if c in "\"'":
            j = scan_quoted(text, j)
            continue
        if c == "#" and (j == i or text[j - 1] in " \t\r\n"):
            # 注释跑到行尾
            while j < n and text[j] not in "\r\n":
                j += 1
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                if c != closer:
                    raise ParseError("方括号和大括号没有配对（第 %d 个字符）" % (j + 1))
                return j + 1
        j += 1
    raise ParseError("%s 没有闭合" % opener)


def decode_double(raw):
    """raw 是带引号的原文，解出里面的字符串。"""
    body = raw[1:-1]
    out = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        e = body[i]
        if e in DQ_ESCAPES:
            out.append(DQ_ESCAPES[e])
            i += 1
        elif e in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[e]
            hexpart = body[i + 1:i + 1 + width]
            try:
                out.append(chr(int(hexpart, 16)))
            except ValueError:
                raise ParseError("转义序列 \\%s%s 不是合法的十六进制" % (e, hexpart))
            i += 1 + width
        elif e == "\n":
            # 双引号里的续行：吃掉换行和后续缩进
            i += 1
            while i < n and body[i] in " \t":
                i += 1
        else:
            out.append(e)
            i += 1
    return "".join(out)


def decode_single(raw):
    return raw[1:-1].replace("''", "'")


def decode_scalar(raw, style):
    if style == "double":
        return decode_double(raw)
    if style == "single":
        return decode_single(raw)
    return raw


def needs_quotes(s):
    """这段文本能不能不加引号直接写进 YAML。"""
    if s == "":
        return True
    if s[0] in "-?:,[]{}#&*!|>'\"%@` ":
        return True
    if s[-1] in " \t":
        return True
    if s.endswith(":"):
        return True
    if ": " in s or " #" in s:
        return True
    if any(c in s for c in "\r\n\t"):
        return True
    if any(ord(c) < 0x20 for c in s):
        return True
    if s.lower() in ("true", "false", "yes", "no", "on", "off", "null", "~"):
        return True
    if looks_like_number(s):
        return True
    return False


def double_quote(s):
    out = ['"']
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif c == "\r":
            out.append("\\r")
        elif ord(c) < 0x20:
            out.append("\\x%02x" % ord(c))
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def single_quote(s):
    return "'" + s.replace("'", "''") + "'"


INT_RE = re.compile(r"[-+]?\d+\Z")
FLOAT_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?\Z")


def looks_like_number(s):
    return bool(INT_RE.match(s) or FLOAT_RE.match(s))


def coerce_plain(s):
    """把 plain 标量翻译成 Python 值，给 --json 用。"""
    low = s.lower()
    if s in ("~", "null", "Null", "NULL") or low == "null":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if INT_RE.match(s):
        try:
            return int(s, 10)
        except ValueError:
            return s
    if FLOAT_RE.match(s) and ("." in s or "e" in low):
        try:
            return float(s)
        except ValueError:
            return s
    return s


# --------------------------------------------------------------------------
# 解析器：只记位置，不做完整语义
# --------------------------------------------------------------------------


class Parser:
    def __init__(self, text):
        self.text = text
        self.lines = split_lines(text)
        self.line_starts = [ln.start for ln in self.lines]
        for idx, ln in enumerate(self.lines):
            body = ln.text[ln.indent:]
            if body.startswith("\t"):
                raise ParseError(
                    "第 %d 行用制表符缩进。YAML 只认空格，请把 tab 换成空格再试"
                    % (idx + 1)
                )

    # -- 定位小工具 -------------------------------------------------------

    def line_of(self, offset):
        if not self.line_starts:
            return 0
        idx = bisect.bisect_right(self.line_starts, offset) - 1
        return max(0, min(idx, len(self.lines) - 1))

    def skip_trivia(self, i):
        while i < len(self.lines) and self.lines[i].trivia:
            i += 1
        return i

    def is_doc_marker(self, ln):
        s = ln.text.strip()
        return ln.indent == 0 and (s == "---" or s == "..." or s.startswith("--- "))

    # -- 入口 -------------------------------------------------------------

    def parse(self):
        i = self.skip_trivia(0)
        if i >= len(self.lines):
            return None
        ln = self.lines[i]
        if ln.text.strip() == "---":
            i = self.skip_trivia(i + 1)
        elif ln.text.startswith("--- "):
            raise ParseError(
                "第 %d 行把内容写在了 --- 后面，这种写法不支持；"
                "请把内容换到下一行" % (i + 1)
            )
        if i >= len(self.lines):
            return None
        node, _ = self.parse_block(i, self.lines[i].indent)
        return node

    # -- 块节点 -----------------------------------------------------------

    def parse_block(self, i, indent):
        i = self.skip_trivia(i)
        if i >= len(self.lines):
            return None, i
        ln = self.lines[i]
        if self.is_doc_marker(ln):
            return None, i
        if ln.indent < indent:
            return None, i
        indent = ln.indent
        content = ln.body
        if _is_seq_dash(content):
            return self.parse_seq(i, indent)
        if find_colon(content) is not None:
            return self.parse_map(i, indent)
        return self.parse_plain_block(i, indent)

    def parse_map(self, i, indent, first_col=None):
        entries = []
        start = None
        first = first_col is not None
        while True:
            if first:
                col = first_col
                ln = self.lines[i]
                first = False
            else:
                i = self.skip_trivia(i)
                if i >= len(self.lines):
                    break
                ln = self.lines[i]
                if self.is_doc_marker(ln):
                    break
                if ln.indent != indent:
                    if ln.indent > indent:
                        raise ParseError(
                            "第 %d 行缩进比同级条目深，无法判断它属于谁；"
                            "请检查这一段的缩进" % (i + 1)
                        )
                    break
                col = indent
            content = ln.text[col:]
            if _is_seq_dash(content):
                break
            colon = find_colon(content)
            if colon is None:
                break
            key_raw = content[:colon].rstrip()
            key = decode_key(key_raw)
            key_abs = ln.start + col
            colon_abs = key_abs + colon
            line_end = ln.start + len(ln.text)
            k = colon_abs + 1
            while k < line_end and self.text[k] == " ":
                k += 1
            tail = self.text[k:line_end]
            key_line_index = i
            if tail == "" or tail.startswith("#"):
                node, i = self._block_value(i, indent, k)
            else:
                node, i = self.parse_inline(i, k, indent)
            node.slot = (colon_abs + 1, node.end)
            if start is None:
                start = key_abs
            entries.append(Entry(key, key_raw, key_abs, node, key_line_index, indent))
        if start is None:
            start = self.lines[i].start if i < len(self.lines) else len(self.text)
        end = entries[-1].node.end if entries else start
        return Node("map", "block", start, end, entries=entries), i

    def _block_value(self, i, indent, after_colon):
        """冒号后面是空的：值要么在下面缩进，要么是个同缩进的序列，要么是 null。"""
        nxt = self.skip_trivia(i + 1)
        if nxt < len(self.lines):
            ln = self.lines[nxt]
            if not self.is_doc_marker(ln):
                if ln.indent > indent:
                    node, i = self.parse_block(nxt, ln.indent)
                    if node is not None:
                        return node, i
                elif ln.indent == indent and _is_seq_dash(ln.body):
                    return self.parse_seq(nxt, indent)
        node = Node("scalar", "null", after_colon, after_colon)
        return node, i + 1

    def parse_seq(self, i, indent):
        items = []
        start = None
        while True:
            i = self.skip_trivia(i)
            if i >= len(self.lines):
                break
            ln = self.lines[i]
            if self.is_doc_marker(ln):
                break
            if ln.indent != indent:
                if ln.indent > indent:
                    raise ParseError(
                        "第 %d 行缩进比同级条目深，无法判断它属于谁；"
                        "请检查这一段的缩进" % (i + 1)
                    )
                break
            content = ln.body
            if not _is_seq_dash(content):
                break
            dash_abs = ln.start + indent
            line_end = ln.start + len(ln.text)
            k = dash_abs + 1
            while k < line_end and self.text[k] == " ":
                k += 1
            if k >= line_end or self.text[k] == "#":
                nxt = self.skip_trivia(i + 1)
                if nxt < len(self.lines) and self.lines[nxt].indent > indent:
                    node, i = self.parse_block(nxt, self.lines[nxt].indent)
                    if node is None:
                        node = Node("scalar", "null", k, k)
                        i = nxt
                else:
                    node = Node("scalar", "null", k, k)
                    i = i + 1
            else:
                col = k - ln.start
                if find_colon(self.text[k:line_end]) is not None:
                    node, i = self.parse_map(i, col, first_col=col)
                else:
                    node, i = self.parse_inline(i, k, indent)
            node.slot = (dash_abs + 1, node.end)
            if start is None:
                start = dash_abs
            items.append(node)
        if start is None:
            start = self.lines[i].start if i < len(self.lines) else len(self.text)
        end = items[-1].end if items else start
        return Node("seq", "block", start, end, items=items), i

    def parse_plain_block(self, i, indent):
        """整个块就是一个裸标量（罕见，但文档里会有）。"""
        ln = self.lines[i]
        end = ln.end
        j = i + 1
        while j < len(self.lines):
            nxt = self.lines[j]
            if nxt.text.strip() == "":
                break
            if nxt.indent <= indent:
                break
            end = nxt.end
            j += 1
        return Node("scalar", "plain", ln.start + indent, end), j

    # -- 行内值 -----------------------------------------------------------

    def parse_inline(self, i, start_abs, parent_indent):
        text = self.text
        ln = self.lines[i]
        line_end = ln.end
        pos = start_abs
        # 跳过锚点与标签，它们只是前缀，值本身在后面
        while True:
            m = re.match(r"[&!][^\s]+", text[pos:line_end])
            if not m:
                break
            pos += m.end()
            while pos < line_end and text[pos] == " ":
                pos += 1
        if pos >= line_end or text[pos] == "#":
            nxt = self.skip_trivia(i + 1)
            if nxt < len(self.lines) and self.lines[nxt].indent > parent_indent:
                node, j = self.parse_block(nxt, self.lines[nxt].indent)
                if node is not None:
                    node.start = pos
                    return node, j
            node = Node("scalar", "null", pos, pos)
            return node, i + 1

        ch = text[pos]
        if ch in "|>":
            return self.parse_block_scalar(i, pos, parent_indent)
        if ch == "*":
            m = re.match(r"\*[^\s,\]\}]+", text[pos:line_end])
            end = pos + (m.end() if m else 1)
            return Node("scalar", "alias", pos, end), i + 1
        if ch in "[{":
            end = scan_flow(text, pos)
            kind = "seq" if ch == "[" else "map"
            j = self.line_of(end - 1)
            return Node(kind, "flow", pos, end), j + 1
        if ch in "\"'":
            end = scan_quoted(text, pos)
            style = "double" if ch == '"' else "single"
            j = self.line_of(end - 1)
            return Node("scalar", style, pos, end), j + 1
        return self.parse_plain(i, pos, parent_indent)

    def parse_plain(self, i, pos, parent_indent):
        """裸标量。可能是多行（YAML 允许折行续写），所以要看下一行。"""
        text = self.text
        ln = self.lines[i]
        line_end = ln.end
        # 去掉行尾注释
        end = line_end
        j = pos
        while j < line_end:
            if text[j] == "#" and j > pos and text[j - 1] in " \t":
                end = j
                while end > pos and text[end - 1] in " \t":
                    end -= 1
                break
            j += 1
        # 多行裸标量：下一行缩进更深且不是键、不是序列项
        k = i + 1
        while k < len(self.lines):
            nxt = self.lines[k]
            if nxt.text.strip() == "" or nxt.indent <= parent_indent:
                break
            body = nxt.body
            if body.startswith("#") or _is_seq_dash(body) or find_colon(body) is not None:
                break
            end = nxt.end
            k += 1
        return Node("scalar", "plain", pos, end), k

    def parse_block_scalar(self, i, pos, parent_indent):
        text = self.text
        ln = self.lines[i]
        header = text[pos:ln.end]
        m = re.match(r"([|>])([1-9]?)([+-]?)([1-9]?)", header)
        if not m:
            raise ParseError("第 %d 行的块标量写法看不懂：%r" % (i + 1, header))
        indicator = m.group(1)
        explicit = m.group(2) or m.group(4)
        body_indent = parent_indent + int(explicit) if explicit else None
        last_end = ln.end
        j = i + 1
        while j < len(self.lines):
            nxt = self.lines[j]
            if nxt.text.strip() == "":
                # 空行可能只是块和下一段之间的分隔，先记着位置，别急着算进块里
                j += 1
                continue
            limit = body_indent if body_indent is not None else parent_indent
            if nxt.indent > limit:
                last_end = nxt.end
                j += 1
                continue
            break
        style = "literal" if indicator == "|" else "folded"
        return Node("scalar", style, pos, last_end), j


def decode_key(key_raw):
    if len(key_raw) >= 2 and key_raw[0] == '"' and key_raw[-1] == '"':
        return decode_double(key_raw)
    if len(key_raw) >= 2 and key_raw[0] == "'" and key_raw[-1] == "'":
        return decode_single(key_raw)
    return key_raw


# --------------------------------------------------------------------------
# 值的读取：把节点翻译成 Python 值
# --------------------------------------------------------------------------


def node_text(node, text):
    return text[node.start:node.end]


def fold_plain(raw):
    """多行裸标量按 YAML 的折行规则拼回一行；空行变真换行。"""
    if "\n" not in raw and "\r" not in raw:
        return raw
    parts = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    pending_newline = 0
    for i, part in enumerate(parts):
        piece = part.strip()
        if i == 0:
            out.append(piece)
            continue
        if piece == "":
            pending_newline += 1
            continue
        out.append("\n" * pending_newline if pending_newline else " ")
        out.append(piece)
        pending_newline = 0
    return "".join(out)


def scalar_value(node, text):
    raw = node_text(node, text)
    style = node.style
    if style == "null":
        return None
    if style in ("plain",):
        return coerce_plain(fold_plain(raw).strip()) if raw.strip() != "" else None
    if style == "alias":
        return raw
    if style == "double":
        return decode_double(raw)
    if style == "single":
        return decode_single(raw)
    if style in ("literal", "folded"):
        return decode_block(raw, style, node.start, text)
    if style == "flow":
        return parse_flow(raw)
    return raw


def decode_block(raw, style, offset, text):
    """块标量：raw 是从指示符开始到块结束的原文。"""
    header_end = raw.find("\n")
    if header_end < 0:
        return ""
    header = raw[:header_end]
    m = re.match(r"([|>])([1-9]?)([+-]?)([1-9]?)", header)
    chomp = (m.group(3) or "") if m else ""
    lines = raw[header_end + 1:].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body_indent = None
    for ln in lines:
        if ln.strip():
            body_indent = len(ln) - len(ln.lstrip(" "))
            break
    if body_indent is None:
        body_indent = 0
    body = [ln[body_indent:] if len(ln) >= body_indent else ln for ln in lines]
    if style == "literal":
        value = "\n".join(body)
    else:
        out = []
        prev_blank = True
        for ln in body:
            if ln == "":
                out.append("\n")
                prev_blank = True
                continue
            if out and not prev_blank:
                out.append(" ")
            elif out and prev_blank and out[-1] != "\n":
                out.append("\n")
            out.append(ln)
            prev_blank = False
        value = "".join(out)
    if chomp == "-":
        value = value.rstrip("\n")
    elif chomp == "+":
        value = value + "\n"
    else:
        value = value.rstrip("\n") + "\n" if value.strip() else ""
    return value


class FlowReader:
    """只认 [ ] { } 和标量的小解析器，够读 flow 集合就行。"""

    def __init__(self, s):
        self.s = s
        self.i = 0

    def read(self):
        v = self.value()
        return v

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def value(self):
        self.ws()
        if self.i >= len(self.s):
            return None
        c = self.s[self.i]
        if c == "[":
            return self.seq()
        if c == "{":
            return self.map()
        if c in "\"'":
            return self.quoted()
        return self.plain()

    def seq(self):
        self.i += 1
        out = []
        while True:
            self.ws()
            if self.i >= len(self.s):
                break
            if self.s[self.i] == "]":
                self.i += 1
                break
            out.append(self.value())
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
        return out

    def map(self):
        self.i += 1
        out = {}
        while True:
            self.ws()
            if self.i >= len(self.s):
                break
            if self.s[self.i] == "}":
                self.i += 1
                break
            key = self.value()
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ":":
                self.i += 1
                val = self.value()
            else:
                val = None
            out[str(key)] = val
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
        return out

    def quoted(self):
        start = self.i
        end = scan_quoted(self.s, start)
        raw = self.s[start:end]
        self.i = end
        return decode_scalar(raw, "double" if raw[0] == '"' else "single")

    def plain(self):
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in ",]}\r\n":
            if self.s[self.i] == ":" and self.i + 1 < len(self.s) and self.s[self.i + 1] in " \t":
                break
            self.i += 1
        raw = self.s[start:self.i].strip()
        return coerce_plain(raw)


def parse_flow(raw):
    try:
        return FlowReader(raw).read()
    except (ParseError, IndexError):
        return raw


def to_jsonable(node, text, depth=0):
    if node is None:
        return None
    if depth > 200:
        raise ParseError("嵌套太深，可能是循环引用")
    if node.kind == "map":
        if node.style == "flow":
            return parse_flow(node_text(node, text))
        return {e.key: to_jsonable(e.node, text, depth + 1) for e in node.entries}
    if node.kind == "seq":
        if node.style == "flow":
            return parse_flow(node_text(node, text))
        return [to_jsonable(it, text, depth + 1) for it in node.items]
    return scalar_value(node, text)


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


def parse_path(path):
    """把 'a.b[0]["x.y"]' 拆成 [(key, ...), (index, 0), ...]。"""
    if path is None:
        return []
    p = path.strip()
    if p in ("", ".", "$"):
        return []
    if p.startswith("$."):
        p = p[2:]
    elif p.startswith("."):
        p = p[1:]
    tokens = []
    i = 0
    n = len(p)
    while i < n:
        c = p[i]
        if c == ".":
            i += 1
            continue
        if c == "[":
            j = p.find("]", i)
            if j < 0:
                raise UsageError("路径里的 [ 没有闭合：%s" % path)
            inner = p[i + 1:j].strip()
            if inner == "+":
                tokens.append(("append", None))
            elif len(inner) >= 2 and inner[0] in "\"'":
                tokens.append(("key", decode_scalar(inner, "double" if inner[0] == '"' else "single")))
            elif re.fullmatch(r"-?\d+", inner):
                tokens.append(("index", int(inner)))
            elif inner == "":
                raise UsageError("路径里出现了空的 []：%s" % path)
            else:
                tokens.append(("key", inner))
            i = j + 1
            continue
        j = i
        while j < n and p[j] not in ".[":
            j += 1
        name = p[i:j].strip()
        if name:
            tokens.append(("key", name))
        i = j
    if not tokens:
        raise UsageError("路径是空的：%r" % path)
    return tokens


def describe_children(node):
    if node is None:
        return "（这里是空的）"
    if node.kind == "map":
        if node.style == "flow":
            return "（flow 映射，先用 set 展开才能逐键访问）"
        return "、".join(e.key for e in node.entries) or "（空映射）"
    if node.kind == "seq":
        if node.style == "flow":
            return "（flow 数组，先用 set 展开才能逐项访问）"
        return "%d 项，下标 0..%d" % (len(node.items), len(node.items) - 1)
    return "（这是个标量，没有子项）"


def resolve(root, tokens):
    """返回 (node, parent, token)。找不到就抛 PathError，并说清这一层有什么。"""
    node = root
    parent = None
    for tok in tokens:
        kind, val = tok
        parent = node
        if node is None:
            raise PathError("路径断在空值上：上一层的值是 null，没有子项")
        if kind == "key":
            if node.kind != "map" or node.style == "flow":
                raise PathError(
                    "想取键 %r，但上一层是%s，不是块映射。这一层有：%s"
                    % (val, "数组" if node.kind == "seq" else "标量", describe_children(node))
                )
            found = None
            for e in node.entries:
                if e.key == val:
                    found = e
                    break
            if found is None:
                hint = ""
                dotted = [e.key for e in node.entries if "." in e.key or "[" in e.key]
                if dotted:
                    hint = "\n提示：名字里带点或方括号的键要用方括号包起来，例如 %s" % (
                        "、".join('["%s"]' % k for k in dotted[:3])
                    )
                raise PathError(
                    "这一层没有键 %r。有的键是：%s%s"
                    % (val, describe_children(node), hint)
                )
            node = found.node
        elif kind == "index":
            if node.kind != "seq" or node.style == "flow":
                raise PathError(
                    "想取下标 %d，但上一层是%s，不是数组"
                    % (val, "映射" if node.kind == "map" else "标量")
                )
            idx = val if val >= 0 else len(node.items) + val
            if idx < 0 or idx >= len(node.items):
                raise PathError(
                    "下标 %d 越界。这个数组只有 %d 项（下标 0..%d）"
                    % (val, len(node.items), len(node.items) - 1)
                )
            node = node.items[idx]
        else:
            raise PathError("这个位置不能追加：只有 set 支持 [+]")
    return node, parent, tokens[-1] if tokens else None


# --------------------------------------------------------------------------
# 渲染新值
# --------------------------------------------------------------------------


def infer(raw, raw_mode):
    """判断用户给的值是什么类型。返回 (kind, value)。"""
    if raw_mode:
        return ("str", raw)
    s = raw.strip()
    if s in ("~", "null", "Null", "NULL"):
        return ("null", None)
    low = s.lower()
    if low in ("true", "false"):
        return ("bool", low == "true")
    if INT_RE.match(s):
        return ("int", s)
    if FLOAT_RE.match(s) and ("." in s or "e" in low):
        return ("float", s)
    if s[:1] in ("[", "{") and s[-1:] in ("]", "}"):
        try:
            scan_flow(s, 0)
        except ParseError:
            return ("str", raw)
        return ("flow", s)
    return ("str", raw)


def render_block_string(s, indicator, indent, eol, step):
    keep = s.endswith("\n")
    body = s[:-1] if keep else s
    pad = " " * (indent + step)
    lines = body.split("\n")
    head = indicator if keep else indicator + "-"
    rendered = [pad + ln if ln else "" for ln in lines]
    return head + eol + eol.join(rendered)


def render_value(raw, old, indent, eol, step, raw_mode=False):
    """把用户输入渲染成可以塞进原文那一段的 YAML 文本。"""
    kind, val = infer(raw, raw_mode)
    if kind in ("int", "float", "bool"):
        return val if isinstance(val, str) else ("true" if val else "false")
    if kind == "null":
        return "null"
    if kind == "flow":
        return val

    # 字符串：先沿用原来的引号/块风格，改完看起来还是同一个文件
    style = old.style if old is not None else "plain"
    s = val
    if style == "double":
        return double_quote(s)
    if style == "single":
        return single_quote(s)
    if style == "literal":
        return render_block_string(s, "|", indent, eol, step)
    if style == "folded":
        # 折行块里塞多行内容，折回去就不是原样了，改用 | 保住内容
        if "\n" in s:
            return render_block_string(s, "|", indent, eol, step)
        return render_block_string(s, ">", indent, eol, step)
    if "\n" in s:
        return render_block_string(s, "|", indent, eol, step)
    if needs_quotes(s):
        return double_quote(s)
    return s


# --------------------------------------------------------------------------
# 编辑：所有改动都是 (start, end, replacement) 的拼接
# --------------------------------------------------------------------------


def apply_edits(text, edits):
    """从后往前替换，这样前面的偏移不会失效。"""
    edits = sorted(edits, key=lambda e: (e[0], e[1]))
    for a, b in zip(edits, edits[1:]):
        if a[1] > b[0]:
            raise YamlSpliceError("内部错误：两处改动重叠了")
    out = text
    for start, end, repl in reversed(edits):
        out = out[:start] + repl + out[end:]
    return out


def detect_step(lines):
    """猜这个文件用几格缩进：看所有相邻缩进差里最小的正数。"""
    seen = {}
    prev = None
    for ln in lines:
        if ln.trivia:
            continue
        if prev is not None and ln.indent > prev:
            d = ln.indent - prev
            seen[d] = seen.get(d, 0) + 1
        prev = ln.indent
    if not seen:
        return 2
    return min(seen, key=lambda d: (-seen[d], d))


def doc_eol(text):
    i = text.find("\n")
    if i > 0 and text[i - 1] == "\r":
        return "\r\n"
    return "\n"


def line_eol(lines, index):
    if 0 <= index < len(lines) and lines[index].eol:
        return lines[index].eol
    return doc_eol("\n".join(ln.text for ln in lines) + "\n")


def _attached_comment_start(parser, line_index, indent):
    """把紧贴在键上方、缩进不浅于键的注释行也算作这一条。"""
    start = parser.lines[line_index].start
    j = line_index - 1
    while j >= 0:
        ln = parser.lines[j]
        if ln.text.strip().startswith("#") and ln.indent >= indent:
            start = ln.start
            j -= 1
            continue
        break
    return start


def _entry_delete_span(parser, start_abs, end_abs):
    """删一条：从它（含上方注释）到它自己那行结束，再吃掉后面的空行。"""
    end_line = parser.line_of(end_abs)
    stop = parser.lines[end_line].stop
    j = end_line + 1
    while j < len(parser.lines) and parser.lines[j].text.strip() == "":
        stop = parser.lines[j].stop
        j += 1
    return (start_abs, stop)


def cmd_get(text, path, as_json=False):
    parser = Parser(text)
    root = parser.parse()
    tokens = parse_path(path)
    if root is None:
        if not tokens:
            return text
        raise PathError("这个文件里没有内容，取不到 %s" % path)
    if not tokens:
        node = root
    else:
        node, _, _ = resolve(root, tokens)
    if as_json:
        return json.dumps(to_jsonable(node, text), ensure_ascii=False, indent=2) + "\n"
    if not tokens:
        return text
    if node.kind in ("map", "seq"):
        if node.style == "flow":
            return node_text(node, text) + "\n"
        # 从这一块第一行所在行的行首开始打印，缩进才不会被切掉
        start = parser.lines[parser.line_of(node.start)].start
        return text[start:node.end] + "\n"
    val = scalar_value(node, text)
    if val is None:
        return "null\n"
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False) + "\n"
    return str(val) + ("\n" if not str(val).endswith("\n") else "")


def cmd_set(text, path, raw_value, raw_mode=False):
    parser = Parser(text)
    root = parser.parse()
    if root is None:
        raise PathError("这个文件里没有内容，没法按路径写入")
    tokens = parse_path(path)
    if not tokens:
        raise UsageError("set 需要一个具体路径，不能是整个文档")
    eol = doc_eol(text)
    step = detect_step(parser.lines)

    # 先试着按原路径找到值：找到就是原地替换
    try:
        node, parent, last = resolve(root, tokens)
        indent = _indent_of(parser, parent, node)
        new = render_value(raw_value, node, indent, eol, step, raw_mode)
        return apply_edits(text, [(node.slot[0], node.slot[1], " " + new)])
    except PathError:
        pass

    # 找不到：只有「父级存在、最后一段是键或追加」才允许新建
    parent_tokens = tokens[:-1]
    kind, val = tokens[-1]
    parent = root
    if parent_tokens:
        parent, _, _ = resolve(root, parent_tokens)

    if kind == "append":
        return _append_to_seq(parser, text, parent, raw_value, eol, step, raw_mode)
    if kind != "key":
        raise PathError("只有键可以新建；下标要落在已有范围里，或用 [+] 追加")

    if parent is None:
        raise PathError("父级是 null，没法往里加键")
    if parent.kind == "map" and parent.style == "flow":
        return _add_to_flow_map(parser, text, parent, val, raw_value, eol, step, raw_mode)
    if parent.kind != "map":
        raise PathError(
            "想加键 %r，但父级是%s，不是映射。这一层有：%s"
            % (val, "数组" if parent.kind == "seq" else "标量", describe_children(parent))
        )
    return _add_to_block_map(parser, text, parent, val, raw_value, eol, step, raw_mode)


def _indent_of(parser, parent, node):
    """节点所在行的缩进，用来对齐块标量的内容。"""
    if parent is not None and parent.kind == "map" and parent.style != "flow":
        for e in parent.entries:
            if e.node is node:
                return e.indent
    line = parser.line_of(node.start)
    return parser.lines[line].indent


def _add_to_block_map(parser, text, parent, key, raw_value, eol, step, raw_mode):
    if not parent.entries:
        raise PathError("父级是空映射，位置没法推断；请先给它写一个键")
    last = parent.entries[-1]
    indent = last.indent
    ln = parser.lines[parser.line_of(last.node.end)]
    key_text = key if not needs_quotes(key) else double_quote(key)
    new = render_value(raw_value, None, indent, eol, step, raw_mode)
    # 接在最后一条的同一行末尾：文件末尾没有换行时，这个 eol 正好把它补上
    piece = eol + " " * indent + key_text + ": " + new
    return apply_edits(text, [(ln.end, ln.end, piece)])


def _add_to_flow_map(parser, text, parent, key, raw_value, eol, step, raw_mode):
    close = parent.end - 1
    body = text[parent.start + 1:close].strip()
    new = render_value(raw_value, None, 0, eol, step, raw_mode)
    key_text = key if not needs_quotes(key) else double_quote(key)
    if body:
        piece = ", %s: %s" % (key_text, new)
    else:
        piece = "%s: %s" % (key_text, new)
    return apply_edits(text, [(close, close, piece)])


def _append_to_seq(parser, text, parent, raw_value, eol, step, raw_mode):
    if parent is None or parent.kind != "seq":
        raise PathError("[+] 只能用在数组上")
    if parent.style == "flow":
        new = render_value(raw_value, None, 0, eol, step, raw_mode)
        close = parent.end - 1
        body = text[parent.start + 1:close].strip()
        piece = (", " + new) if body else new
        return apply_edits(text, [(close, close, piece)])
    if not parent.items:
        raise PathError("这是个空数组，位置没法推断")
    last = parent.items[-1]
    last_line = parser.line_of(last.end)
    ln = parser.lines[last_line]
    indent = parser.lines[parser.line_of(parent.start)].indent
    new = render_value(raw_value, None, indent, eol, step, raw_mode)
    piece = eol + " " * indent + "- " + new
    return apply_edits(text, [(ln.end, ln.end, piece)])


def cmd_del(text, path):
    parser = Parser(text)
    root = parser.parse()
    if root is None:
        raise PathError("这个文件里没有内容")
    tokens = parse_path(path)
    if not tokens:
        raise UsageError("del 需要一个具体路径，不能是整个文档")
    node, parent, last = resolve(root, tokens)

    if parent is None:
        # 删的是文档根，等于清空
        return ""
    if parent.kind == "map" and parent.style == "block":
        entry = None
        for e in parent.entries:
            if e.node is node:
                entry = e
                break
        if entry is None:
            raise PathError("内部错误：找不到要删的条目")
        start = _attached_comment_start(parser, entry.line_index, entry.indent)
        span = _entry_delete_span(parser, start, entry.node.end)
        return apply_edits(text, [(span[0], span[1], "")])
    if parent.kind == "seq" and parent.style == "block":
        start = node.start
        # 序列项的起点是 "-"，往前退一格
        dash = text.rfind("-", max(0, node.start - 4), node.start + 1)
        if dash >= 0:
            start = dash
        span = _entry_delete_span(parser, start, node.end)
        return apply_edits(text, [(span[0], span[1], "")])
    raise PathError(
        "只能删块映射里的键或块数组里的项；flow 写法（[1, 2] / {a: 1}）请整体重写"
    )


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------


def read_input(path):
    if path == "-":
        data = sys.stdin.buffer.read()
    else:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise YamlSpliceError("读不了 %s：%s" % (path, exc.strerror or exc))
    bom = b""
    if data.startswith(b"\xef\xbb\xbf"):
        bom = data[:3]
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise YamlSpliceError(
            "文件不是 UTF-8 编码（第 %d 字节处）。请先转成 UTF-8 再处理" % exc.start
        )
    return bom, text


def write_output(path, bom, text, in_place):
    payload = bom + text.encode("utf-8")
    if in_place:
        try:
            with open(path, "wb") as fh:
                fh.write(payload)
        except OSError as exc:
            raise YamlSpliceError("写不了 %s：%s" % (path, exc.strerror or exc))
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    if argv[0] == "--version":
        sys.stdout.write("yamlsplice %s\n" % __version__)
        return 0

    verb = argv[0]
    rest = argv[1:]
    flags = set()
    positional = []
    for a in rest:
        if a in ("-i", "--in-place", "--json", "--raw"):
            flags.add(a)
        elif a in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return 0
        elif a.startswith("--") and a != "-":
            raise UsageError("不认识的选项：%s" % a)
        else:
            positional.append(a)

    in_place = bool(flags & {"-i", "--in-place"})
    as_json = "--json" in flags
    raw_mode = "--raw" in flags

    try:
        if verb == "get":
            if len(positional) != 2:
                raise UsageError("get 需要 <path> <file> 两个参数")
            path, filename = positional
            if in_place:
                raise UsageError("get 不会改文件，去掉 -i")
            bom, text = read_input(filename)
            out = cmd_get(text, path, as_json)
            write_output(filename, bom, out, False)
            return 0
        if verb == "set":
            if len(positional) != 3:
                raise UsageError("set 需要 <path> <value> <file> 三个参数")
            path, value, filename = positional
            if filename == "-" and in_place:
                raise UsageError("从标准输入读的时候没法 -i，请用重定向")
            bom, text = read_input(filename)
            out = cmd_set(text, path, value, raw_mode)
            write_output(filename, bom, out, in_place)
            return 0
        if verb == "del":
            if len(positional) != 2:
                raise UsageError("del 需要 <path> <file> 两个参数")
            path, filename = positional
            if filename == "-" and in_place:
                raise UsageError("从标准输入读的时候没法 -i，请用重定向")
            bom, text = read_input(filename)
            out = cmd_del(text, path)
            write_output(filename, bom, out, in_place)
            return 0
        raise UsageError("不认识的命令：%s（只有 get / set / del）" % verb)
    except YamlSpliceError as exc:
        sys.stderr.write("yamlsplice: %s\n" % exc)
        return exc.exit_code
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
