import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import replace_core as rc


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "work", "site")
        os.makedirs(self.root)
        rc.HOME = os.path.join(self.tmp.name, "pfr-home")

    def w(self, rel, text, mode=None):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
        if mode:
            os.chmod(p, mode)
        return p

    def r(self, rel):
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read().decode("utf-8")

    def run_replace(self, find, rep, **kw):
        flags = {k: v for k, v in kw.items() if k in ("regex", "case", "whole_word")}
        s = rc.scan(self.root, find, rep, **flags)
        selection = {f["path"]: {"hash": f["hash"], "indices": None} for f in s["files"]}
        return rc.apply(self.root, find, rep, selection, **flags)


class Matching(Base):
    def test_plain_case_insensitive_by_default(self):
        self.w("a.txt", "Hello hello HELLO")
        self.assertEqual(rc.scan(self.root, "hello", "x")["total"], 3)

    def test_case_sensitive(self):
        self.w("a.txt", "Hello hello HELLO")
        self.assertEqual(rc.scan(self.root, "hello", "x", case=True)["total"], 1)

    def test_plain_text_is_literal(self):
        self.w("a.txt", "price (1.50) and 1x50")
        self.assertEqual(rc.scan(self.root, "1.50", "X")["total"], 1)

    def test_whole_word(self):
        self.w("a.txt", "cat concat cat.")
        self.assertEqual(rc.scan(self.root, "cat", "x", whole_word=True)["total"], 2)

    def test_regex_groups_dollar_and_backslash(self):
        self.w("a.txt", "john smith")
        self.run_replace(r"(\w+) (\w+)", "$2, $1", regex=True)
        self.assertEqual(self.r("a.txt"), "smith, john")
        self.run_replace(r"(\w+), (\w+)", r"\2 \1", regex=True)
        self.assertEqual(self.r("a.txt"), "john smith")

    def test_named_group_and_dollar_escape(self):
        self.w("a.txt", "cost 5")
        self.run_replace(r"(?P<n>\d+)", "$$${n}.00", regex=True)
        self.assertEqual(self.r("a.txt"), "cost $5.00")

    def test_replacement_is_not_regex_in_plain_mode(self):
        self.w("a.txt", "a")
        self.run_replace("a", r"\1 $1 \n")
        self.assertEqual(self.r("a.txt"), r"\1 $1 \n")

    def test_bad_regex_and_bad_group(self):
        with self.assertRaises(rc.ReplaceError):
            rc.scan(self.root, "(", "x", regex=True)
        with self.assertRaises(rc.ReplaceError):
            rc.scan(self.root, "a", "$3", regex=True)
        with self.assertRaises(rc.ReplaceError):
            rc.scan(self.root, "", "x")

    def test_multiline_regex(self):
        self.w("a.txt", "one\ntwo\nthree")
        self.run_replace(r"one\ntwo", "X", regex=True)
        self.assertEqual(self.r("a.txt"), "X\nthree")

    def test_line_numbers_and_snippets(self):
        self.w("a.txt", "a\nb\nfoo bar\n")
        m = rc.scan(self.root, "foo", "baz")["files"][0]["matches"][0]
        self.assertEqual((m["line"], m["col"]), (3, 1))
        self.assertEqual(m["before"][1], "foo")
        self.assertEqual(m["after"][1], "baz")
        self.assertEqual(m["before"][2], " bar")


class Files(Base):
    def test_skips_binary_git_and_node_modules(self):
        self.w("a.txt", "needle")
        self.w("b.bin", b"needle\x00\x01")
        self.w(".git/config", "needle")
        self.w("node_modules/x/i.js", "needle")
        self.assertEqual([f["path"] for f in rc.scan(self.root, "needle", "x")["files"]], ["a.txt"])

    def test_non_utf8_is_skipped_and_reported(self):
        self.w("l.txt", b"caf\xe9 needle")
        s = rc.scan(self.root, "needle", "x")
        self.assertEqual(s["files"], [])
        self.assertTrue(any(k["path"] == "l.txt" for k in s["skipped"]))

    def test_include_exclude_globs(self):
        self.w("a.html", "needle")
        self.w("b.md", "needle")
        self.w("sub/c.html", "needle")
        got = sorted(f["path"] for f in rc.scan(self.root, "needle", "x", include="*.html")["files"])
        self.assertEqual(got, ["a.html", "sub/c.html"])
        got = sorted(f["path"] for f in rc.scan(self.root, "needle", "x", exclude="sub/*,*.md")["files"])
        self.assertEqual(got, ["a.html"])

    def test_refuses_home_and_filesystem_root(self):
        for bad in (os.sep, os.path.expanduser("~")):
            with self.assertRaises(rc.ReplaceError):
                rc.check_root(bad)


class Replace(Base):
    def test_replace_preserves_crlf_mode_unicode(self):
        p = self.w("a.txt", "café X\r\nline X\r\n", mode=0o640)
        self.run_replace("X", "Y")
        self.assertEqual(self.r("a.txt"), "café Y\r\nline Y\r\n")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)

    def test_nothing_else_touched(self):
        self.w("a.txt", "X")
        p = self.w("b.txt", "keep")
        before = os.stat(p).st_mtime_ns
        self.run_replace("X", "Y")
        self.assertEqual(os.stat(p).st_mtime_ns, before)

    def test_per_match_selection(self):
        self.w("a.txt", "X X X")
        s = rc.scan(self.root, "X", "Y")["files"][0]
        rc.apply(self.root, "X", "Y", {"a.txt": {"hash": s["hash"], "indices": [0, 2]}})
        self.assertEqual(self.r("a.txt"), "Y X Y")

    def test_changed_since_preview_is_skipped(self):
        self.w("a.txt", "X")
        s = rc.scan(self.root, "X", "Y")["files"][0]
        self.w("a.txt", "X changed")
        res = rc.apply(self.root, "X", "Y", {"a.txt": {"hash": s["hash"], "indices": None}})
        self.assertEqual(res["changed"], 0)
        self.assertEqual(len(res["conflicts"]), 1)
        self.assertEqual(self.r("a.txt"), "X changed")

    def test_path_escape_rejected(self):
        with self.assertRaises(rc.ReplaceError):
            rc.apply(self.root, "X", "Y", {"../outside.txt": {"indices": None}})

    def test_empty_replacement_deletes(self):
        self.w("a.txt", "a-DEL-b")
        self.run_replace("-DEL-", "")
        self.assertEqual(self.r("a.txt"), "ab")


class History(Base):
    def test_back_forward_and_diff(self):
        self.w("a.txt", "one")
        self.w("b.txt", "one")
        self.assertEqual(self.run_replace("one", "two")["version"], 1)
        self.assertEqual(self.run_replace("two", "three")["version"], 2)
        self.assertEqual((self.r("a.txt"), self.r("b.txt")), ("three", "three"))
        rc.goto(self.root, 1)
        self.assertEqual(self.r("a.txt"), "two")
        rc.goto(self.root, 0)
        self.assertEqual((self.r("a.txt"), self.r("b.txt")), ("one", "one"))
        rc.goto(self.root, 2)
        self.assertEqual(self.r("a.txt"), "three")
        h = rc.history(self.root)
        self.assertEqual((h["head"], len(h["versions"])), (2, 2))
        d = rc.diff(self.root, 1)
        self.assertEqual({f["path"] for f in d["files"]}, {"a.txt", "b.txt"})
        self.assertTrue(any(line == "+two" for line in d["files"][0]["lines"]))

    def test_new_replace_after_back_discards_forward_branch(self):
        self.w("a.txt", "one")
        self.run_replace("one", "two")
        self.run_replace("two", "three")
        rc.goto(self.root, 1)
        self.run_replace("two", "other")
        h = rc.history(self.root)
        self.assertEqual((h["head"], [v["replace"] for v in h["versions"]]), (2, ["two", "other"]))
        self.assertEqual(self.r("a.txt"), "other")

    def test_outside_edit_blocks_navigation_unless_forced(self):
        self.w("a.txt", "one")
        self.run_replace("one", "two")
        self.w("a.txt", "edited by hand")
        res = rc.goto(self.root, 0)
        self.assertTrue(res["needs_force"])
        self.assertEqual(self.r("a.txt"), "edited by hand")
        rc.goto(self.root, 0, force=True)
        self.assertEqual(self.r("a.txt"), "one")

    def test_history_is_per_folder(self):
        self.w("a.txt", "one")
        self.run_replace("one", "two")
        other = os.path.join(self.tmp.name, "work", "other")
        os.makedirs(other)
        self.assertEqual(rc.history(other)["versions"], [])

    def test_survives_new_process(self):
        self.w("a.txt", "one")
        self.run_replace("one", "two")
        import importlib
        importlib.reload(rc)
        rc.HOME = os.path.join(self.tmp.name, "pfr-home")
        rc.goto(self.root, 0)
        self.assertEqual(self.r("a.txt"), "one")

    def test_failed_write_rolls_back(self):
        self.w("a.txt", "X")
        self.w("b.txt", "X")
        s = rc.scan(self.root, "X", "Y")
        sel = {f["path"]: {"hash": f["hash"], "indices": None} for f in s["files"]}
        real, calls = rc._atomic, {"n": 0}

        def flaky(path, data, mode=None):
            if not path.startswith(rc.HOME):
                calls["n"] += 1
                if calls["n"] == 2 and data == b"Y":
                    raise OSError("disk full")
            return real(path, data, mode)

        rc._atomic = flaky
        try:
            with self.assertRaises(OSError):
                rc.apply(self.root, "X", "Y", sel)
        finally:
            rc._atomic = real
        self.assertEqual((self.r("a.txt"), self.r("b.txt")), ("X", "X"))
        self.assertEqual(rc.history(self.root)["versions"], [])


class Exclusions(Base):
    def test_protected_text_is_never_changed(self):
        self.w("a.txt", "JetBrains makes a brain tool. brain!")
        s = rc.scan(self.root, "brain", "mind", protect="JetBrains")
        self.assertEqual((s["total"], s["excluded"]), (2, 1))
        sel = {f["path"]: {"hash": f["hash"], "indices": None} for f in s["files"]}
        rc.apply(self.root, "brain", "mind", sel, protect="JetBrains")
        self.assertEqual(self.r("a.txt"), "JetBrains makes a mind tool. mind!")

    def test_protect_is_case_insensitive_unless_match_case(self):
        self.w("a.txt", "jetbrains brain")
        loose = rc.scan(self.root, "brain", "x", protect="JetBrains")
        self.assertEqual((loose["total"], loose["excluded"]), (1, 1))      # "jetbrains" is protected despite different case
        strict = rc.scan(self.root, "brain", "x", protect="JetBrains", case=True)
        self.assertEqual((strict["total"], strict["excluded"]), (2, 0))    # with Match case, "jetbrains" is not "JetBrains"

    def test_skip_lines(self):
        self.w("a.txt", "keep Acme here\n# NOTE do not edit: Acme\nAcme again\n")
        s = rc.scan(self.root, "Acme", "Corp", skip_lines="do not edit")
        self.assertEqual((s["total"], s["excluded"]), (2, 1))
        sel = {f["path"]: {"hash": f["hash"], "indices": None} for f in s["files"]}
        rc.apply(self.root, "Acme", "Corp", sel, skip_lines="do not edit")
        self.assertEqual(self.r("a.txt"), "keep Corp here\n# NOTE do not edit: Acme\nCorp again\n")

    def test_indices_agree_between_scan_and_apply(self):
        self.w("a.txt", "x PROTECT x x")
        s = rc.scan(self.root, "x", "y", protect="PROTECT", whole_word=True)["files"][0]
        self.assertEqual(s["count"], 3)
        rc.apply(self.root, "x", "y", {"a.txt": {"hash": s["hash"], "indices": [0, 2]}}, whole_word=True, protect="PROTECT")
        self.assertEqual(self.r("a.txt"), "y PROTECT x y")

    def test_exclusions_recorded_in_history(self):
        self.w("a.txt", "a b")
        s = rc.scan(self.root, "a", "z", skip_lines="zzz")["files"][0]
        rc.apply(self.root, "a", "z", {"a.txt": {"hash": s["hash"], "indices": None}}, skip_lines="zzz")
        self.assertEqual(rc.history(self.root)["versions"][0]["skip_lines"], ["zzz"])


if __name__ == "__main__":
    unittest.main()
