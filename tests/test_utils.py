import unittest

import telegram_stubs

telegram_stubs.install()

import vibes  # noqa: E402


class UtilsTests(unittest.TestCase):
    def test_safe_session_name_validation(self) -> None:
        self.assertEqual(vibes._safe_session_name("abc-DEF_123"), "abc-DEF_123")
        self.assertEqual(vibes._safe_session_name("  ok.name  "), "ok.name")
        self.assertIsNone(vibes._safe_session_name(""))
        self.assertIsNone(vibes._safe_session_name("   "))
        self.assertIsNone(vibes._safe_session_name("space name"))
        self.assertIsNone(vibes._safe_session_name("bad/char"))
        self.assertIsNone(vibes._safe_session_name("x" * 65))

    def test_truncate_text_keeps_short_text(self) -> None:
        self.assertEqual(vibes._truncate_text("hello", 10), "hello")

    def test_truncate_text_marks_truncation(self) -> None:
        text = "0123456789" * 50
        out = vibes._truncate_text(text, 80)
        self.assertIn("…(обрезано)…", out)
        self.assertIn(text[:5], out)
        self.assertIn(text[-5:], out)

    def test_cb_prefix_and_sanitization(self) -> None:
        self.assertEqual(vibes._cb("new"), f"{vibes.CB_PREFIX}:new")
        self.assertEqual(vibes._cb("a:b", "c"), f"{vibes.CB_PREFIX}:a_b:c")

    def test_parse_tokens_strips_botname(self) -> None:
        self.assertEqual(vibes._parse_tokens("/use@mybot name"), ["/use", "name"])
        self.assertEqual(vibes._parse_tokens('/new "a b" "/tmp/x y"'), ["/new", "a b", "/tmp/x y"])

    def test_get_event_type_prefers_known_keys(self) -> None:
        self.assertEqual(vibes._get_event_type({"type": "t", "event": "e"}), "t")
        self.assertEqual(vibes._get_event_type({"event": "e"}), "e")
        self.assertEqual(vibes._get_event_type({"kind": "k"}), "k")
        self.assertEqual(vibes._get_event_type({"name": "n"}), "n")
        self.assertEqual(vibes._get_event_type({}), "")

    def test_uuid_extraction_helpers(self) -> None:
        uuid = "123e4567-e89b-12d3-a456-426614174000"

        self.assertEqual(vibes._looks_like_uuid(uuid), uuid)
        self.assertIsNone(vibes._looks_like_uuid("not-a-uuid"))

        obj = {"data": {"thread": {"id": uuid}}}
        self.assertEqual(vibes._find_first_uuid(obj), uuid)

        explicit = {"thread_id": uuid}
        self.assertEqual(vibes._extract_session_id_explicit(explicit), uuid)

    def test_extract_text_delta(self) -> None:
        self.assertEqual(vibes._extract_text_delta({"delta": "a"}), "a")
        self.assertEqual(vibes._extract_text_delta({"data": {"text": "b"}}), "b")
        self.assertIsNone(vibes._extract_text_delta({"data": {"text": ""}}))

    def test_extract_tool_fields(self) -> None:
        self.assertEqual(vibes._extract_tool_command({"input": {"command": "ls"}}), "ls")
        self.assertEqual(vibes._extract_tool_command({"data": {"input": {"command": "pwd"}}}), "pwd")
        self.assertIsNone(vibes._extract_tool_command({"data": {"input": {"command": ""}}}))

        self.assertEqual(vibes._extract_tool_output({"output": "ok"}), "ok")
        self.assertEqual(vibes._extract_tool_output({"data": {"stdout": "yo"}}), "yo")
        self.assertIsNone(vibes._extract_tool_output({"data": {"stdout": ""}}))

    def test_maybe_extract_diff(self) -> None:
        self.assertEqual(vibes._maybe_extract_diff({"diff": "x"}), "x")
        self.assertEqual(vibes._maybe_extract_diff({"data": {"patch": "y"}}), "y")
        self.assertIsNone(vibes._maybe_extract_diff({"data": {"patch": "  "}}))

    def test_segment_rendering(self) -> None:
        seg = vibes.Segment(kind="text", content="<b>x</b>")
        self.assertEqual(seg.plain_len(), len("<b>x</b>"))
        self.assertEqual(seg.render_html(), "&lt;b&gt;x&lt;/b&gt;")

        code = vibes.Segment(kind="code", content="print('<hi>')\n")
        rendered = code.render_html()
        self.assertIn("<pre><code>", rendered)
        self.assertIn("&lt;hi&gt;", rendered)

    def test_sanitize_attachment_basename_blocks_path_separators_and_dot_entries(self) -> None:
        self.assertEqual(vibes._sanitize_attachment_basename(""), "file")
        self.assertEqual(vibes._sanitize_attachment_basename("."), "file")
        self.assertEqual(vibes._sanitize_attachment_basename(".."), "file")

        out = vibes._sanitize_attachment_basename("../../etc/passwd")
        self.assertNotIn("/", out)
        self.assertNotIn("\\", out)
        self.assertNotEqual(out, ".")
        self.assertNotEqual(out, "..")

        nul = vibes._sanitize_attachment_basename("a\x00b.txt")
        self.assertNotIn("\x00", nul)

    def test_sanitize_attachment_basename_enforces_max_len_and_preserves_suffix(self) -> None:
        name = ("a" * 300) + ".pdf"
        out = vibes._sanitize_attachment_basename(name)
        self.assertLessEqual(len(out), vibes.MAX_DOWNLOADED_FILENAME_LEN)
        self.assertTrue(out.endswith(".pdf"))

    def test_pick_unique_dest_path_stays_within_dir_and_increments(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as td:
            dest_dir = Path(td)
            first = vibes._pick_unique_dest_path(dest_dir, "x.txt")
            self.assertEqual(first, dest_dir / "x.txt")
            first.write_text("1", encoding="utf-8")

            second = vibes._pick_unique_dest_path(dest_dir, "x.txt")
            self.assertEqual(second, dest_dir / "x_2.txt")

            (dest_dir / "x_2.txt").write_text("2", encoding="utf-8")
            third = vibes._pick_unique_dest_path(dest_dir, "x.txt")
            self.assertEqual(third, dest_dir / "x_3.txt")

            traversal = vibes._pick_unique_dest_path(dest_dir, "../../evil.txt")
            self.assertEqual(traversal.parent.resolve(), dest_dir.resolve())

    def test_safe_resolve_path_success_and_errors(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path

        resolved, err = vibes._safe_resolve_path("")
        self.assertIsNone(resolved)
        self.assertTrue(err)

        with TemporaryDirectory() as td:
            p = Path(td)
            resolved2, err2 = vibes._safe_resolve_path(str(p))
            self.assertEqual(err2, "")
            self.assertEqual(resolved2, p.resolve())

        resolved3, err3 = vibes._safe_resolve_path("bad\x00path")
        self.assertIsNone(resolved3)
        self.assertTrue(err3)
