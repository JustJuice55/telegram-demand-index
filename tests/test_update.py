"""Contract of the daily rebuild: publish a real index or nothing."""
from __future__ import annotations

import csv
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


if __name__ == "__main__":
    unittest.main()
