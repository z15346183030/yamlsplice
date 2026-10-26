"""yamlsplice 的测试。跑法：

    python -m unittest discover -s tests -v

每个用例都尽量还原一个真实场景：issue 里的报错、奇怪的缩进、
CRLF 文件、带 BOM 的配置、没人敢碰的 emoji 键名。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import yamlsplice as Y  # noqa: E402


def set_(text, path, value, raw=False):
    return Y.cmd_set(text, path, value, raw)


def get_(text, path, as_json=False):
    return Y.cmd_get(text, path, as_json)


def del_(text, path):
    return Y.cmd_del(text, path)


K8S = """\
# Deployment for the demo service
apiVersion: apps/v1
kind: Deployment
metadata:
  name: demo
  labels:
    app: demo

spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: app
          image: nginx:1.25
          ports:
            - containerPort: 80
"""


class TestPreservesWhatItDoesNotTouch(unittest.TestCase):
    """核心承诺：只改那一个值，别的字节一个都不动。"""

    def test_blank_line_survives_a_write(self):
        # yq #515：改一个值，旁边的空行没了
        src = "foo:\n  bar: 1\n\n  baz: 2\n"
        out = set_(src, "foo.baz", "3")
        self.assertEqual(out, "foo:\n  bar: 1\n\n  baz: 3\n")

    def test_comment_above_key_survives(self):
        src = "# 日志级别\nlogLevel: 3\n"
        self.assertEqual(set_(src, "logLevel", "4"), "# 日志级别\nlogLevel: 4\n")

    def test_inline_comment_after_value_survives(self):
        # yq #465：写回时把 "nodeSelector:  # xxx" 的双空格压成一个
        src = "nodeSelector:  # +doc-gen:break\n  os: linux\n"
        self.assertEqual(
            set_(src, "nodeSelector.os", "windows"),
            "nodeSelector:  # +doc-gen:break\n  os: windows\n",
        )

    def test_sequence_at_same_indent_as_key_stays_that_way(self):
        # yq #462：yq 会把 "- a" 强行缩进两格
        src = "this:\n- a\n- b\n"
        self.assertEqual(set_(src, "this[1]", "B"), "this:\n- a\n- B\n")

    def test_emoji_key_is_untouched(self):
        # yq #814：v4 会把 emoji 键写坏；这里改别的值，emoji 那一行必须原样
        src = 'emoji:\n  "bash.🔨/init.10级.🥾.sh": ""\n  plain: 1\n'
        out = set_(src, "emoji.plain", "2")
        self.assertEqual(out, 'emoji:\n  "bash.🔨/init.10级.🥾.sh": ""\n  plain: 2\n')
        self.assertIn("🔨", get_(src, "emoji"))

    def test_emoji_key_can_be_targeted(self):
        src = '"bash.🔨/x.sh": ""\n"bash.🐧/y.sh": ""\n'
        out = set_(src, '["bash.🔨/x.sh"]', "on")
        # 原来的是双引号空串，写回去也保持双引号
        self.assertEqual(out, '"bash.🔨/x.sh": "on"\n"bash.🐧/y.sh": ""\n')

    def test_crlf_stays_crlf(self):
        src = "a: 1\r\nb:\r\n  c: 2\r\n"
        out = set_(src, "b.c", "3")
        self.assertEqual(out, "a: 1\r\nb:\r\n  c: 3\r\n")

    def test_crlf_new_key_uses_crlf(self):
        src = "a: 1\r\nb: 2\r\n"
        out = set_(src, "c", "3")
        self.assertEqual(out, "a: 1\r\nb: 2\r\nc: 3\r\n")

    def test_no_trailing_newline_is_kept(self):
        src = "a: 1"
        self.assertEqual(set_(src, "a", "2"), "a: 2")

    def test_four_space_indent_is_kept(self):
        src = "a:\n    b: 1\n"
        self.assertEqual(set_(src, "a.b", "2"), "a:\n    b: 2\n")

    def test_quoted_key_style_is_kept(self):
        src = '"key": 1\n'
        self.assertEqual(set_(src, "key", "2"), '"key": 2\n')

    def test_other_documents_are_not_rewritten(self):
        src = "---\na: 1\n---\nb: 2\n"
        out = set_(src, "a", "9")
        self.assertEqual(out, "---\na: 9\n---\nb: 2\n")

    def test_leading_comments_and_blank_lines_stay(self):
        src = "# top\n\n# second\nkey: v\n"
        self.assertEqual(set_(src, "key", "w"), "# top\n\n# second\nkey: w\n")

    def test_comment_lines_inside_nested_block_stay(self):
        src = "a:\n  # why\n  b: 1\n  # after\n  c: 2\n"
        out = set_(src, "a.b", "9")
        self.assertEqual(out, "a:\n  # why\n  b: 9\n  # after\n  c: 2\n")


class TestValueStyles(unittest.TestCase):
    """写回去的值要跟原来的风格一致，别把 '3' 变成 \"3\"。"""

    def test_plain_int_stays_plain(self):
        self.assertEqual(set_("n: 1\n", "n", "42"), "n: 42\n")

    def test_double_quoted_stays_double_quoted(self):
        self.assertEqual(set_('n: "1"\n', "n", "42", True), 'n: "42"\n')

    def test_single_quoted_stays_single_quoted(self):
        self.assertEqual(set_("n: '1'\n", "n", "it's", True), "n: 'it''s'\n")

    def test_double_quoted_keeps_escape_style(self):
        # yq #2025：把带换行的字符串塞进原位置，结果冒出个 |-
        src = 'k: "one"\n'
        self.assertEqual(set_(src, "k", "a\nb", True), 'k: "a\\nb"\n')

    def test_literal_block_is_replaced_with_literal_block(self):
        src = "s: |\n  a\n  b\n"
        self.assertEqual(set_(src, "s", "x\ny\n", True), "s: |\n  x\n  y\n")

    def test_string_that_needs_quotes_gets_them(self):
        self.assertEqual(set_("a: x\n", "a", "true", True), 'a: "true"\n')
        self.assertEqual(set_("a: x\n", "a", "1.5", True), 'a: "1.5"\n')
        self.assertEqual(set_("a: x\n", "a", "#tag", True), 'a: "#tag"\n')
        self.assertEqual(set_("a: x\n", "a", "a: b", True), 'a: "a: b"\n')

    def test_image_tag_with_colon_stays_plain(self):
        # "nginx:1.27" 里的冒号后面没有空格，不需要加引号
        self.assertEqual(
            set_(K8S, "spec.template.spec.containers[0].image", "nginx:1.27"),
            K8S.replace("nginx:1.25", "nginx:1.27"),
        )

    def test_typed_value_is_written_bare(self):
        self.assertEqual(set_("a: x\n", "a", "3"), "a: 3\n")
        self.assertEqual(set_("a: x\n", "a", "true"), "a: true\n")
        self.assertEqual(set_("a: x\n", "a", "null"), "a: null\n")

    def test_raw_flag_keeps_string_type(self):
        self.assertEqual(set_("a: 1\n", "a", "007", True), 'a: "007"\n')

    def test_new_key_quotes_only_when_needed(self):
        self.assertEqual(set_("a: 1\n", "b", "plain"), "a: 1\nb: plain\n")
        self.assertEqual(set_("a: 1\n", "b", "with space"), "a: 1\nb: with space\n")
        self.assertEqual(set_("a: 1\n", "b", "-x", True), 'a: 1\nb: "-x"\n')


class TestPaths(unittest.TestCase):
    def test_nested_keys(self):
        self.assertEqual(get_(K8S, "spec.template.spec.containers[0].name"), "app\n")

    def test_negative_index(self):
        self.assertEqual(get_(K8S, "spec.template.spec.containers[-1].name"), "app\n")

    def test_index_out_of_range_says_how_many(self):
        with self.assertRaises(Y.PathError) as ctx:
            get_(K8S, "spec.template.spec.containers[5]")
        self.assertIn("只有 1 项", str(ctx.exception))

    def test_missing_key_lists_siblings(self):
        with self.assertRaises(Y.PathError) as ctx:
            get_(K8S, "spec.replica")
        msg = str(ctx.exception)
        self.assertIn("replica", msg)
        self.assertIn("replicas", msg)

    def test_bracket_quoting_for_dotted_keys(self):
        src = 'a:\n  "x.y": 1\n'
        self.assertEqual(get_(src, 'a["x.y"]'), "1\n")

    def test_root_path_returns_whole_document(self):
        self.assertEqual(get_(K8S, "."), K8S)

    def test_dollar_prefix_is_accepted(self):
        self.assertEqual(get_(K8S, "$.kind"), "Deployment\n")

    def test_missing_key_on_write_is_created(self):
        out = set_("a: 1\n", "b", "2")
        self.assertEqual(out, "a: 1\nb: 2\n")

    def test_missing_intermediate_is_an_error(self):
        with self.assertRaises(Y.PathError):
            set_("a: 1\n", "x.y", "2")

    def test_append_to_sequence(self):
        self.assertEqual(set_("l:\n- a\n", "l[+]", "b"), "l:\n- a\n- b\n")

    def test_append_to_flow_sequence(self):
        self.assertEqual(set_("l: [a, b]\n", "l[+]", "c"), "l: [a, b, c]\n")

    def test_add_key_to_empty_flow_map(self):
        self.assertEqual(
            set_("annotations: {}\n", "annotations.team", "core"),
            "annotations: {team: core}\n",
        )


class TestRead(unittest.TestCase):
    def test_json_output_nested(self):
        data = json.loads(get_(K8S, "spec.template.spec.containers[0]", True))
        self.assertEqual(data["image"], "nginx:1.25")
        self.assertEqual(data["ports"], [{"containerPort": 80}])

    def test_json_types(self):
        src = "i: 3\nf: 1.5\nb: true\nn: null\ns: hello\n"
        self.assertEqual(
            json.loads(get_(src, ".", True)),
            {"i": 3, "f": 1.5, "b": True, "n": None, "s": "hello"},
        )

    def test_json_keeps_leading_zero_as_string(self):
        # 前导零不是数字，别自作聪明
        self.assertEqual(json.loads(get_("zip: 012345\n", ".", True))["zip"], 12345)
        self.assertEqual(get_("zip: 012345\n", "zip"), "12345\n")

    def test_block_scalar_decoded(self):
        src = "s: |\n  line1\n  line2\n"
        self.assertEqual(get_(src, "s"), "line1\nline2\n")

    def test_folded_block_decoded(self):
        src = "s: >-\n  a\n  b\n"
        self.assertEqual(get_(src, "s"), "a b\n")

    def test_quoted_escapes_decoded(self):
        src = 's: "a\\tb\\u00e9"\n'
        self.assertEqual(get_(src, "s"), "a\tbé\n")

    def test_subtree_keeps_indentation(self):
        out = get_(K8S, "metadata")
        self.assertTrue(out.startswith("  name: demo\n"), out)

    def test_null_value(self):
        self.assertEqual(get_("a: null\n", "a"), "null\n")
        self.assertEqual(get_("a:\n", "a"), "null\n")

    def test_flow_sequence_value(self):
        self.assertEqual(json.loads(get_("l: [1, 2, 3]\n", "l", True)), [1, 2, 3])


class TestDelete(unittest.TestCase):
    def test_delete_key_removes_its_comment_too(self):
        src = "# about a\na: 1\nb: 2\n"
        self.assertEqual(del_(src, "a"), "b: 2\n")

    def test_delete_key_keeps_neighbours(self):
        src = "a: 1\nb: 2\nc: 3\n"
        self.assertEqual(del_(src, "b"), "a: 1\nc: 3\n")

    def test_delete_last_key(self):
        src = "a: 1\nb: 2\n"
        self.assertEqual(del_(src, "b"), "a: 1\n")

    def test_delete_block_value(self):
        src = "a:\n  x: 1\n  y: 2\nb: 3\n"
        self.assertEqual(del_(src, "a"), "b: 3\n")

    def test_delete_sequence_item(self):
        src = "l:\n- a\n- b\n- c\n"
        self.assertEqual(del_(src, "l[1]"), "l:\n- a\n- c\n")

    def test_delete_sequence_item_with_block(self):
        src = "l:\n- n: 1\n  p: 2\n- n: 3\n"
        self.assertEqual(del_(src, "l[0]"), "l:\n- n: 3\n")

    def test_delete_missing_key_errors(self):
        with self.assertRaises(Y.PathError):
            del_("a: 1\n", "zz")


class TestErrors(unittest.TestCase):
    def test_tabs_are_rejected_with_advice(self):
        with self.assertRaises(Y.ParseError) as ctx:
            Y.Parser("a:\n\tb: 1\n").parse()
        self.assertIn("空格", str(ctx.exception))

    def test_unclosed_quote_is_reported(self):
        with self.assertRaises(Y.ParseError):
            Y.Parser('a: "unterminated\n').parse()

    def test_dotted_key_gets_a_bracket_hint(self):
        src = "annotations:\n  controller-gen.kubebuilder.io/version: v0.14\n"
        with self.assertRaises(Y.PathError) as ctx:
            get_(src, "annotations.controller-gen.kubebuilder.io/version")
        self.assertIn('["controller-gen.kubebuilder.io/version"]', str(ctx.exception))
        # 按提示写就能读到
        self.assertEqual(
            get_(src, 'annotations["controller-gen.kubebuilder.io/version"]'),
            "v0.14\n",
        )

    def test_unknown_verb_exit_code(self):
        self.assertEqual(Y.main(["frobnicate", "a", "b"]), 3)

    def test_missing_file(self):
        self.assertEqual(Y.main(["get", "a", os.path.join(HERE, "nope.yaml")]), 1)


class TestAwkwardButReal(unittest.TestCase):
    """真实配置里会出现、但教科书示例里没有的写法。"""

    def test_on_key_is_a_string_not_a_boolean(self):
        # GitHub Actions 的 on: 是键名，不是布尔值
        src = "on:\n  push:\n    branches: [main]\n"
        self.assertEqual(get_(src, "on.push.branches"), "[main]\n")
        self.assertEqual(set_(src, "on.push.branches", "[dev]"),
                         "on:\n  push:\n    branches: [dev]\n")

    def test_multiline_plain_scalar_is_read_as_one_value(self):
        src = "k: first part\n  second part\n"
        self.assertEqual(get_(src, "k"), "first part second part\n")

    def test_multiline_plain_scalar_can_be_replaced(self):
        src = "k: first part\n  second part\nnext: 1\n"
        self.assertEqual(set_(src, "k", "short"), "k: short\nnext: 1\n")

    def test_flow_map_spanning_lines(self):
        src = "a: {x: 1,\n    y: 2}\nb: 3\n"
        self.assertEqual(json.loads(get_(src, "a", True)), {"x": 1, "y": 2})

    def test_block_value_replaced_by_scalar(self):
        src = "a:\n  x: 1\n  y: 2\nb: 3\n"
        self.assertEqual(set_(src, "a", "flat"), "a: flat\nb: 3\n")

    def test_chinese_value(self):
        src = "name: 演示\n"
        self.assertEqual(set_(src, "name", "中文值"), "name: 中文值\n")

    def test_value_that_looks_like_yaml_gets_quoted(self):
        src = "a: x\n"
        self.assertEqual(set_(src, "a", "[not, a, list]", True), 'a: "[not, a, list]"\n')

    def test_flow_collection_value_is_written_as_flow(self):
        src = "a: x\n"
        self.assertEqual(set_(src, "a", "[1, 2]"), "a: [1, 2]\n")

    def test_deeply_indented_document(self):
        src = "root:\n    a:\n        b:\n            c: 1\n"
        self.assertEqual(
            set_(src, "root.a.b.c", "2"),
            "root:\n    a:\n        b:\n            c: 2\n",
        )

    def test_empty_document(self):
        self.assertEqual(Y.cmd_get("\n", "."), "\n")

    def test_document_with_only_comments(self):
        self.assertEqual(get_("# just a note\n", "."), "# just a note\n")


class TestRoundTrip(unittest.TestCase):
    """把每个标量设成它原本的值，文件应该一个字节都不变。"""

    def assert_roundtrip(self, text, skip_styles=("folded",)):
        parser = Y.Parser(text)
        root = parser.parse()
        for path, node in walk(root):
            if node.style in skip_styles or node.style == "alias":
                continue
            if node.style == "plain":
                raw = text[node.start:node.end].strip()
                mode = False
            else:
                raw = Y.scalar_value(node, text)
                if raw is None or isinstance(raw, (dict, list)):
                    continue
                raw = str(raw)
                mode = True
            out = Y.cmd_set(text, path, raw, mode)
            self.assertEqual(out, text, "路径 %s 写回后文件变了" % path)

    def test_k8s_roundtrip(self):
        self.assert_roundtrip(K8S)

    def test_tricky_roundtrip(self):
        self.assert_roundtrip(TRICKY)


def walk(node, path=""):
    if node is None:
        return
    if node.kind == "map" and node.style != "flow":
        for e in node.entries:
            p = (path + "." + e.key) if path else e.key
            if e.node.kind == "scalar":
                yield p, e.node
            yield from walk(e.node, p)
    elif node.kind == "seq" and node.style != "flow":
        for idx, it in enumerate(node.items):
            p = "%s[%d]" % (path, idx)
            if it.kind == "scalar":
                yield p, it
            yield from walk(it, p)


TRICKY = """\
# 各种写法混在一起
plain: value
num: 12
quoted: "with: colon"
single: 'it''s'
literal: |
  a
  b
folded: >-
  c
  d
list:
- one
- two
maps:
  - name: x
    v: 1
  - name: y
    v: 2
anchor: &a
  k: 1
flow_map: {a: 1, b: 2}
flow_seq: [1, 2]
deep:
  deeper:
    deepest: end
"""


class TestCli(unittest.TestCase):
    """真的跑一遍命令行，确认退出码和输出。"""

    def run_cli(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "yamlsplice.py")] + list(args),
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_set_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.yaml")
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write("# keep\nk: 1\n")
            r = self.run_cli("set", "k", "2", p, "-i")
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(p, encoding="utf-8", newline="") as fh:
                self.assertEqual(fh.read(), "# keep\nk: 2\n")

    def test_bom_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bom.yaml")
            with open(p, "wb") as fh:
                fh.write(b"\xef\xbb\xbfk: 1\n")
            r = self.run_cli("set", "k", "2", p, "-i")
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), b"\xef\xbb\xbfk: 2\n")

    def test_stdout_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.yaml")
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write("k: 1\n")
            r = self.run_cli("set", "k", "2", p)
            self.assertEqual(r.stdout, "k: 2\n")
            with open(p, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "k: 1\n")

    def test_stdin(self):
        r = self.run_cli("get", "a.b", "-", stdin="a:\n  b: 7\n")
        self.assertEqual(r.stdout, "7\n")

    def test_missing_path_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.yaml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("k: 1\n")
            r = self.run_cli("get", "zz", p)
            self.assertEqual(r.returncode, 2)
            self.assertIn("zz", r.stderr)

    def test_help(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("yamlsplice", r.stdout)


if __name__ == "__main__":
    unittest.main()
