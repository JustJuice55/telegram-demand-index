"""Contract of the daily rebuild: publish a real index or nothing."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import shutil
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COUNTRIES = ["ru", "uz", "ae", "th", "tr", "ua", "by", "vn", "kz", "kg", "de"]
NICHES = ["services", "goods", "real-estate", "jobs", "freight"]


def payload(n_countries: int = 11) -> dict:
    items = []
    for ci, c in enumerate(COUNTRIES[:n_countries]):
        for ni, niche in enumerate(NICHES):
            items.append({
                "country": c, "niche": niche,
                "requests7d": 1000 - ci * 50 - ni * 100,
                "requestsPrev7d": 500 - ci * 20 - ni * 40 + (60 if niche == "jobs" else 0),
                "requests30d": 3000, "chats": 40 + ni,
                "topChats": [{"username": f"{c}_{niche}_chat", "title": "x", "requests30d": 9}],
            })
    return {"computedAt": "2026-11-06T01:30:06+00:00", "windowDays": 7,
            "method": "test", "items": items}


def pages(p: dict, drop: tuple[str, str] | None = None) -> list[dict]:
    return [{"niche": i["niche"], "country": i["country"]} for i in p["items"]
            if (i["niche"], i["country"]) != drop]


class RepoCopy:
    def __enter__(self) -> Path:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        shutil.copy(ROOT / "README.md", root / "README.md")
        return root

    def __exit__(self, *exc) -> None:
        self.tmp.cleanup()


class TestBuild(unittest.TestCase):
    def test_a_thin_payload_leaves_yesterday_untouched(self):
        with RepoCopy() as root:
            update.build(payload(), pages(payload()), root)
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            for bad in ({"computedAt": "2026-11-07", "items": []},
                        {"computedAt": "2026-11-07", "items": payload(2)["items"]},
                        {"items": payload()["items"]},
                        "<html>502</html>"):
                with self.assertRaises(update.PayloadError):
                    update.build(bad, pages(payload()), root)
            after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_an_empty_page_map_refuses_instead_of_publishing_unlinked(self):
        with RepoCopy() as root:
            with self.assertRaises(update.PayloadError):
                update.build(payload(), [], root)
            self.assertFalse((root / "data").exists())

    def test_a_market_without_a_page_is_named_but_not_linked(self):
        p = payload()
        with RepoCopy() as root:
            update.build(p, pages(p, drop=("goods", "ru")), root)
            readme = (root / "README.md").read_text()
            self.assertNotIn("/intent/goods/ru?", readme)
            self.assertIn("/intent/services/ru?", readme)
            self.assertIn("Russia · Goods", readme)
            rows = list(csv.DictReader(io.StringIO((root / "data/latest.csv").read_text())))
            goods_ru = next(r for r in rows if r["country"] == "ru" and r["niche"] == "goods")
            self.assertEqual(goods_ru["url"], "")

    def test_every_link_carries_the_campaign_tag(self):
        p = payload()
        block = update.render(p, update.validate(p), update.page_set(pages(p)))
        for line in block.splitlines():
            for chunk in line.split("](")[1:]:
                url = chunk.split(")")[0]
                if url.startswith("https://tgden.com"):
                    self.assertIn("utm_campaign=demand-index", url)

    def test_rebuild_keeps_the_hand_written_parts_and_one_block(self):
        with RepoCopy() as root:
            update.build(payload(), pages(payload()), root)
            update.build(payload(), pages(payload()), root)
            readme = (root / "README.md").read_text()
            self.assertEqual(readme.count(update.START), 1)
            self.assertEqual(readme.count("### Biggest markets this week"), 1)
            self.assertIn("## Method", readme)
            self.assertIn("Compare shares, not raw counts", readme)

    def test_history_gets_one_file_per_day(self):
        with RepoCopy() as root:
            update.build(payload(), pages(payload()), root)
            nxt = payload()
            nxt["computedAt"] = "2026-11-07T01:30:00+00:00"
            update.build(nxt, pages(nxt), root)
            names = sorted(p.name for p in (root / "data/history").iterdir())
            self.assertEqual(names, ["2026-11-06.json", "2026-11-07.json"])
            self.assertEqual(json.loads((root / "data/latest.json").read_text())["computedAt"],
                             nxt["computedAt"])


class TestNumbers(unittest.TestCase):
    def test_share_is_inside_the_country_and_sums_to_one(self):
        rs = update.rows(update.validate(payload()))
        for c in COUNTRIES:
            total = sum(r["share7d"] for r in rs if r["country"] == c)
            self.assertAlmostEqual(total, 1.0)

    def test_coverage_growth_alone_does_not_show_as_gaining_share(self):
        p = payload()
        for i in p["items"]:
            i["requestsPrev7d"] = i["requests7d"] // 2   # every market doubled together
        rs = update.rows(update.validate(p))
        self.assertTrue(all(abs(r["shareDeltaPp"]) < 0.5 for r in rs))
        block = update.render(p, update.validate(p), update.page_set(pages(p)))
        self.assertNotIn("### Gaining share", block)

    def test_a_small_market_does_not_make_the_gaining_list(self):
        p = payload()
        for i in p["items"]:
            i["requestsPrev7d"] = i["requests7d"]
        small = next(i for i in p["items"] if i["country"] == "de" and i["niche"] == "jobs")
        small["requests7d"], small["requestsPrev7d"] = 250, 80   # big jump, tiny market
        big = next(i for i in p["items"] if i["country"] == "ru" and i["niche"] == "freight")
        big["requests7d"] = 900                                  # was 600, same week before
        block = update.render(p, update.validate(p), update.page_set(pages(p)))
        gaining = block.split("### Gaining share")[1].split("###")[0]
        self.assertIn("Russia · Freight", gaining)
        self.assertNotIn("Germany · Jobs", gaining)

    def test_flags_and_names(self):
        self.assertEqual(update.flag("ae"), "🇦🇪 ")
        self.assertEqual(update.flag("ab"), "")
        self.assertEqual(update.country_name("uz"), "Uzbekistan")
        self.assertEqual(update.country_name("zz"), "ZZ")


class TestSnapshotDay(unittest.TestCase):
    """computedAt names data/history/<day>.json, so it must be a real date."""

    def test_a_full_timestamp_and_a_plain_day_agree(self):
        for raw in ("2026-11-17", "2026-11-17T01:30:06+00:00", "2026-11-17T00:00:00Z"):
            self.assertEqual(update.snapshot_day({"computedAt": raw}), "2026-11-17")

    def test_anything_that_is_not_a_calendar_date_is_refused(self):
        for bad in ("9999-99-99", "2026-13-01", "..-..-../x", "", "yesterday", None, 20261117,
                    ["2026-11-17"]):
            with self.assertRaises(update.PayloadError, msg=repr(bad)):
                update.snapshot_day({"computedAt": bad})

    def test_a_date_that_is_not_a_date_writes_nothing(self):
        with RepoCopy() as root:
            p = payload()
            p["computedAt"] = "..-..-../2026-11-06"   # two hyphens, so the old check passed it
            with self.assertRaises(update.PayloadError):
                update.build(p, pages(p), root)
            self.assertFalse((root / "data").exists())
            self.assertEqual((root / "README.md").read_text(), (ROOT / "README.md").read_text())


class TestOrdering(unittest.TestCase):
    """A cached or backdated payload must not publish yesterday over today."""

    def test_an_older_payload_does_not_rewrite_a_newer_snapshot(self):
        with RepoCopy() as root:
            new = payload()
            new["computedAt"] = "2026-11-07T01:30:00+00:00"
            update.build(new, pages(new), root)
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            old = payload()
            old["computedAt"] = "2026-11-06T01:30:00+00:00"
            for i in old["items"]:
                i["requests7d"] = 1
            with self.assertRaises(update.PayloadError) as caught:
                update.build(old, pages(old), root)
            self.assertIn("2026-11-07", str(caught.exception))
            after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_the_same_day_can_be_rebuilt(self):
        with RepoCopy() as root:
            update.build(payload(), pages(payload()), root)
            self.assertEqual(update.build(payload(), pages(payload()), root), "2026-11-06")
            self.assertEqual(len(list((root / "data/history").iterdir())), 1)


class TestPayloadShape(unittest.TestCase):
    def assertRefused(self, mutate) -> None:
        p = payload()
        mutate(p)
        with self.assertRaises(update.PayloadError):
            update.validate(p)

    def test_a_market_listed_twice_is_refused(self):
        # Two rows for one market double its country's denominator and print twice.
        def mutate(p):
            p["items"].append(dict(p["items"][0]))
        self.assertRefused(mutate)

    def test_a_market_listed_twice_in_another_case_is_refused(self):
        def mutate(p):
            twin = dict(p["items"][0])
            twin["country"] = twin["country"].upper()
            p["items"].append(twin)
        self.assertRefused(mutate)

    def test_a_boolean_is_not_a_count(self):
        def mutate(p):
            p["items"][0]["requests7d"] = True
        self.assertRefused(mutate)

    def test_an_item_that_is_not_an_object_is_refused(self):
        def mutate(p):
            p["items"][0] = "ru/services"
        self.assertRefused(mutate)

    def test_a_topchat_entry_must_be_an_object(self):
        # Validation said yes and render then died with AttributeError.
        def mutate(p):
            p["items"][0]["topChats"] = ["ru_services"]
        self.assertRefused(mutate)

    def test_a_topchat_username_must_be_a_string(self):
        def mutate(p):
            p["items"][0]["topChats"] = [{"username": ["ru_services"]}]
        self.assertRefused(mutate)

    def test_a_topchat_without_a_username_still_renders(self):
        p = payload()
        p["items"][0]["topChats"] = [{"title": "no username here"}]
        block = update.render(p, update.validate(p), update.page_set(pages(p)))
        self.assertIn("| — |", block)


class TestFetch(unittest.TestCase):
    """An API that is down, or answering HTML, is a refusal and not a traceback."""

    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/json"):
                    self.answer(200, b'{"ok": true}')
                elif self.path.startswith("/html"):
                    self.answer(200, b"<html>502</html>")
                else:
                    self.answer(502, b"bad gateway")

            def answer(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args, **kwargs) -> None:  # silence the request log
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_json_is_returned(self):
        self.assertEqual(update.fetch(f"{self.base}/json"), {"ok": True})

    def test_a_502_is_a_refusal_naming_the_status(self):
        with self.assertRaises(update.PayloadError) as caught:
            update.fetch(f"{self.base}/week")
        self.assertIn("502", str(caught.exception))

    def test_an_html_body_is_a_refusal(self):
        with self.assertRaises(update.PayloadError) as caught:
            update.fetch(f"{self.base}/html")
        self.assertIn("did not answer JSON", str(caught.exception))

    def test_a_closed_port_is_a_refusal(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(update.PayloadError) as caught:
            update.fetch(f"http://127.0.0.1:{port}/week")
        self.assertIn("unreachable", str(caught.exception))


class TestCli(unittest.TestCase):
    def test_pages_without_input_is_refused(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(err):
            update.main(["--pages", str(ROOT / "README.md")])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("go together", err.getvalue())

    def test_input_without_pages_is_refused(self):
        p = payload()
        with RepoCopy() as root:
            week = root / "week.json"
            week.write_text(json.dumps(p))
            err = io.StringIO()
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(err):
                update.main(["--input", str(week)])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("go together", err.getvalue())

    def test_a_missing_input_file_is_a_refusal_with_a_message(self):
        p = payload()
        with RepoCopy() as root:
            geo = root / "geo.json"
            geo.write_text(json.dumps(pages(p)))
            err = io.StringIO()
            with mock.patch.object(update, "ROOT", root), contextlib.redirect_stderr(err):
                code = update.main(["--input", str(root / "missing.json"), "--pages", str(geo)])
            self.assertEqual(code, 1)
            self.assertTrue(err.getvalue().startswith("refused to publish:"), err.getvalue())

    def test_an_input_file_that_is_not_json_is_a_refusal(self):
        p = payload()
        with RepoCopy() as root:
            week, geo = root / "week.json", root / "geo.json"
            week.write_text("<html>502</html>")
            geo.write_text(json.dumps(pages(p)))
            err = io.StringIO()
            with mock.patch.object(update, "ROOT", root), contextlib.redirect_stderr(err):
                code = update.main(["--input", str(week), "--pages", str(geo)])
            self.assertEqual(code, 1)
            self.assertIn("is not JSON", err.getvalue())

    def test_an_offline_rebuild_writes_the_index(self):
        p = payload()
        with RepoCopy() as root:
            week, geo = root / "week.json", root / "geo.json"
            week.write_text(json.dumps(p))
            geo.write_text(json.dumps(pages(p)))
            with mock.patch.object(update, "ROOT", root), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(update.main(["--input", str(week), "--pages", str(geo)]), 0)
            self.assertTrue((root / "data/history/2026-11-06.json").exists())
            rows = list(csv.DictReader(io.StringIO((root / "data/latest.csv").read_text())))
            self.assertEqual(len(rows), len(p["items"]))
            self.assertIn("### Biggest markets this week", (root / "README.md").read_text())


class TestNames(unittest.TestCase):
    def test_every_country_code_in_the_published_data_has_a_name(self):
        codes: set[str] = set()
        for path in [ROOT / "data/latest.json", *sorted((ROOT / "data/history").glob("*.json"))]:
            codes |= {i["country"] for i in json.loads(path.read_text(encoding="utf-8"))["items"]}
        self.assertTrue(codes)
        unnamed = sorted(c for c in codes if c not in update.COUNTRIES)
        self.assertEqual(unnamed, [], f"codes published without a name: {unnamed}")

    def test_the_recent_codes_are_named(self):
        self.assertEqual(update.country_name("kh"), "Cambodia")
        self.assertEqual(update.country_name("ng"), "Nigeria")
        self.assertEqual(update.flag("kh"), "\N{REGIONAL INDICATOR SYMBOL LETTER K}"
                                          "\N{REGIONAL INDICATOR SYMBOL LETTER H} ")


if __name__ == "__main__":
    unittest.main()
