"""离线测试：不联网、不写真实状态，覆盖用户点名的全部自检项。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor import lang, quotes, rules, rsshub, sources, state as state_mod, telegram  # noqa: E402
from monitor import run as monitor_run  # noqa: E402

NOW_MS = 1794945600000


def fake_item(title, url="https://example.com/1", category="", rule=""):
    item = rsshub.NewsItem(source="test", label="测试源", title=title, url=url,
                           published_ms=NOW_MS)
    item.category, item.rule = category, rule
    return item


class CategoryTests(unittest.TestCase):
    """四类新闻判定：首个命中即停，记规则名。"""

    CASES = [
        ("美联储威廉姆斯：过去几个月行动效果良好", "央行", "cb-fed"),
        ("美联储主席鲍威尔释放迄今最鹰派信号", "央行", "cb-fed"),
        ("巴克莱预计2027年市政债供给量最高达6,000亿美元", "央行", "cb-bonds"),
        ("浑水做空英伟达 质疑前高管与超微电脑前员工存在关联", "重大事件", "ev-short"),
        ("桥水、索罗斯等机构三季度减持英伟达 华尔街对AI投资现分歧", "重大事件", "ev-investor"),
        ("报道：美国证交会正在调查英伟达信息披露行为", "重大事件", "ev-shock"),
        ("京东集团三季度营收增长3.5% 新业务收入砍半", "公司", "co-earnings"),
        ("英伟达将在盘后公布Q3财报", "公司", "co-earnings"),
        ("台积电市值跌破1.9万亿美元", "公司", "co-action"),
        ("APEC热议：全球贸易并未萎缩 只是版图已重构", "政策/贸易", "pol-trade"),
        ("南非拟寻求豁免欧盟混动汽车进口限制", "政策/贸易", "pol-trade"),
        ("美国支出法案或再推迟 民主党寻求短期权宜方案", "政策/贸易", "pol-fiscal"),
    ]

    def test_all_four_categories(self):
        seen = set()
        for title, category, _ in self.CASES:
            got, _rule = rules.classify(title)
            self.assertEqual(got, category, title)
            seen.add(got)
        self.assertEqual(seen, set(rules.CATEGORIES))

    def test_rule_name_recorded(self):
        for title, category, rule in self.CASES:
            got, hit = rules.classify(title)
            self.assertEqual((got, hit), (category, rule), title)

    def test_unclassified_dropped(self):
        self.assertEqual(rules.classify("苹果(AAPL.O)：今日已有3起服务中断事件得到解决。"), ("", ""))
        self.assertEqual(rules.classify("OpenAI首席执行官：新模型已上线"), ("", ""))


class BlockedTests(unittest.TestCase):
    """纯行情快讯、币圈、非经济杂项、非中文都要挡。"""

    MARKET = [
        "纳指跌超2%，道指跌1.7%，标普500指数跌1.65%",
        "德国DAX指数持续走低，现下跌2%",
        "富时A50期指连续夜盘收跌0.09%，报12650点。",
        "美股收盘：三大股指集体收跌",
        "国际油价17日微涨",
        "美元兑瑞郎USD/CHF突破0.84，日内涨0.04%。",
        "国内商品期货夜盘收盘 | 燃油涨1.36%，棉花涨0.94%",
        "标普500指数的11个板块全线收跌，金融板块跌1.87%",
    ]

    def test_pure_market_flash_blocked(self):
        for title in self.MARKET:
            self.assertEqual(rules.filter_reason(title), "market_quote", title)

    def test_event_with_price_kept(self):
        # 有事件信号的不是碎片行情，要留下来按四类归类
        self.assertNotEqual(rules.filter_reason("浑水做空英伟达，英伟达跌超10%"), "market_quote")
        self.assertEqual(rules.classify("台积电市值跌破1.9万亿美元")[0], "公司")

    def test_crypto_blocked(self):
        for title in ["比特币突破10万美元", "狗狗币大涨 币圈全线飘红",
                      "ETH现货ETF净流入创新高", "USDT稳定币储备曝光",
                      "Solana链上交易创纪录", "DeFi协议遭黑客攻击"]:
            self.assertEqual(rules.filter_reason(title), "crypto", title)

    def test_legal_digital_currency_not_crypto(self):
        self.assertNotEqual(rules.filter_reason("数字人民币试点范围扩容"), "crypto")
        self.assertEqual(rules.classify("央行数字货币试点范围扩容")[0], "央行")

    def test_non_economic_blocked(self):
        for title in ["马坦·卡哈纳以全票当选以色列议会议长",
                      "金正恩携妻女视察在建旅游区", "某演唱会门票今日开售"]:
            self.assertEqual(rules.filter_reason(title), "non_economic", title)

    def test_chinese_filter(self):
        self.assertTrue(lang.is_chinese("美商务部发布限制先进半导体相关措施 中方回应"))
        self.assertTrue(lang.is_chinese("小米回应雷军APEC发言紧张"))
        self.assertFalse(lang.is_chinese("US stocks rally as Fed signals rate cut"))
        self.assertFalse(lang.is_chinese("APEC"))
        self.assertEqual(rules.filter_reason("US stocks rally as Fed signals rate cut"), "not_chinese")

    def test_html_and_source_prefix_cleaned(self):
        self.assertNotIn("<b>", lang.clean_title("<b>纳指跌超2% |</b> 纳指跌超2%"))
        self.assertEqual(lang.clean_title("格隆汇11月18日｜浑水做空英伟达"), "浑水做空英伟达")

    def test_stock_earnings_detail_blocked(self):
        for title in [
            "劳氏第三季度调整后每股收益3.10美元，上年同期3.06美元，预估2.92美元",
            "英伟达2027财年Q3净利润653亿美元，去年同期319.1亿美元，市场预期604.06亿美元",
            "英伟达2027财年Q3营收1158亿美元，去年同期570亿美元，市场预期1091亿美元",
            "某公司拟3亿元-5亿元回购股份",
            "回购计划丨这家公司董事长提议回购20亿至40亿元股份",
            "某公司董事长提议回购公司股份",
        ]:
            self.assertEqual(rules.filter_reason(title), "earnings_detail", title)

    def test_big_company_action_not_blocked(self):
        for title in ["英伟达CEO黄仁勋宣布辞职", "波音CEO被罢免，公司宣布裁员1.7万人","京东集团三季度营收增长3.5% 新业务收入砍半",
                      "英伟达董事会成立国家安全与出口管制监督委员会"]:
            self.assertNotEqual(rules.filter_reason(title), "earnings_detail", title)

    def test_market_flash_with_macro_word_blocked(self):
        title = ("美联储沃勒接受采访后，美国股指期货回吐涨幅，标普500迷你期货上涨0.1%，"
                 "纳斯达克100期货上涨0.5%，道指期货下跌0.2%。")
        self.assertEqual(rules.filter_reason(title), "market_quote")
        # 真正的央行事件仍然保留
        self.assertNotEqual(rules.filter_reason("美联储宣布降息，道指涨1%"), "market_quote")
        self.assertNotEqual(rules.filter_reason("美联储威廉姆斯：行动效果良好"), "market_quote")

    def test_bond_buyback_and_survey_blocked(self):
        self.assertEqual(rules.filter_reason("某公司拟回购至多1.65亿美元的2031年到期票据"),
                         "earnings_detail")
        self.assertEqual(rules.filter_reason("花旗调查：香港每13人有一名千万富翁"), "survey")
        self.assertNotEqual(rules.filter_reason("报道：美国证交会正在调查英伟达信息披露"), "survey")


class RssHubTests(unittest.TestCase):
    """RSSHub 解析：RSS2/Atom、时间格式、多实例回退、时间窗。"""

    ROUTE = sources.RssRoute("demo", "/demo", "样例源")
    XML = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
      <item><title><![CDATA[美联储威廉姆斯：过去几个月行动效果良好]]></title>
        <link>https://a/1</link><pubDate>Tue, 17 Nov 2026 20:00:00 GMT</pubDate>
        <description>摘要</description></item>
      <item><title>纳指跌超2%</title><link>https://a/2</link>
        <pubDate>Tue, 17 Nov 2026 19:00:00 GMT</pubDate></item>
      <item><title></title><link>https://a/3</link></item>
    </channel></rss>"""

    ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>浑水做空英伟达</title><link href="https://b/1"/>
      <published>2026-11-18T05:00:00+08:00</published></entry></feed>"""

    def test_rss2_parse(self):
        items = rsshub.parse_feed(self.XML, self.ROUTE)
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].title.startswith("美联储威廉姆斯"))
        self.assertEqual(items[0].summary, "摘要")
        self.assertGreater(items[0].published_ms, 0)

    def test_atom_parse(self):
        items = rsshub.parse_feed(self.ATOM, self.ROUTE)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "浑水做空英伟达")
        self.assertEqual(items[0].url, "https://b/1")

    def test_bad_payload_raises(self):
        with self.assertRaises(ValueError):
            rsshub.parse_feed("<rss><channel></channel></rss>", self.ROUTE)
        with self.assertRaises(ValueError):
            rsshub.parse_feed("not xml", self.ROUTE)

    def test_pubdate_formats(self):
        self.assertEqual(rsshub.parse_pubdate("1794945600"), 1_794_945_600_000)
        self.assertEqual(rsshub.parse_pubdate("1794945600000"), 1_794_945_600_000)
        self.assertGreater(rsshub.parse_pubdate("Tue, 17 Nov 2026 20:00:00 GMT"), 0)
        self.assertGreater(rsshub.parse_pubdate("2026-11-18 05:00:00"), 0)
        self.assertEqual(rsshub.parse_pubdate("bad"), 0)

    def test_instance_fallback_and_time_window(self):
        # 首选实例 503 → 换第二个；只留时间窗内的条目
        xml = self.XML.replace("19:00:00 GMT", "17:00:00 GMT")
        calls = []

        def fake_fetch(url, **kwargs):
            calls.append(url)
            if "rssforever" in url:
                return rsshub.http.FetchResult(url, error_type="provider_error",
                                               error="503", status=503)
            return rsshub.http.FetchResult(url, data=xml.encode("utf-8"), status=200)

        with mock.patch.object(rsshub.http, "fetch", fake_fetch):
            result = rsshub.fetch_route(self.ROUTE, since_ms=NOW_MS - 60 * 60_000,
                                        now_ms=NOW_MS, instances=sources.RSS_INSTANCES[:2])
        self.assertTrue(result.ok)
        self.assertEqual(result.instance, "https://rss.injahow.cn")
        self.assertEqual(len(result.items), 1)  # 早于时间窗的那条被挡
        self.assertEqual(len(calls), 2)

    def test_all_instances_failed(self):
        def fake_fetch(url, **kwargs):
            return rsshub.http.FetchResult(url, error_type="provider_error", error="timeout")
        with mock.patch.object(rsshub.http, "fetch", fake_fetch):
            result = rsshub.fetch_route(self.ROUTE, since_ms=0, now_ms=NOW_MS)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "provider_error")


class QuoteBoardTests(unittest.TestCase):
    """行情看板：百分比计算、24 标的（A股独立分组）、分组格式、失败跳过。"""

    def test_change_calculation(self):
        payload = {"chart": {"result": [{"meta": {
            "regularMarketPrice": 7340.53, "chartPreviousClose": 7520.93,
        }}]}}
        q = quotes.parse_chart(payload, sources.QuoteSymbol("美股", "标普500", "^GSPC"))
        self.assertTrue(q.ok)
        self.assertAlmostEqual(q.change_pct, -2.40, places=2)

    def test_previous_close_fallback_and_error(self):
        sym = sources.QuoteSymbol("美股", "标普500", "^GSPC")
        payload = {"chart": {"result": [{"meta": {
            "regularMarketPrice": 100.0, "previousClose": 80.0,
        }}]}}
        self.assertAlmostEqual(quotes.parse_chart(payload, sym).change_pct, 25.0, places=2)
        bad = quotes.parse_chart({"chart": {"error": {"description": "Not Found"}}}, sym)
        self.assertFalse(bad.ok)

    def test_url_encoding(self):
        self.assertIn("%5EGSPC", quotes.quote_url("^GSPC"))
        self.assertIn("GC%3DF", quotes.quote_url("GC=F"))
        self.assertIn("EURUSD%3DX", quotes.quote_url("EURUSD=X"))

    def test_quote_symbols_and_groups(self):
        self.assertEqual(len(sources.active_symbols()), 24)
        self.assertEqual(sources.QUOTE_GROUPS, ["美股", "欧股", "A股", "亚太", "商品", "汇率债"])
        self.assertEqual([s.group for s in sources.QUOTE_SYMBOLS].count("美股"), 5)
        self.assertEqual([s.group for s in sources.QUOTE_SYMBOLS].count("欧股"), 3)
        self.assertEqual([s.group for s in sources.QUOTE_SYMBOLS].count("A股"), 5)
        self.assertEqual([s.group for s in sources.QUOTE_SYMBOLS].count("亚太"), 3)
        self.assertEqual([s.group for s in sources.QUOTE_SYMBOLS].count("商品"), 4)
        self.assertEqual([s.group for s in sources.QUOTE_SYMBOLS].count("汇率债"), 4)
        a_share_codes = {s.code for s in sources.QUOTE_SYMBOLS if s.group == "A股"}
        self.assertEqual(a_share_codes, {"000001.SS", "399001.SZ", "000300.SS",
                                          "399006.SZ", "000688.SS"})
        asia_codes = {s.code for s in sources.QUOTE_SYMBOLS if s.group == "亚太"}
        self.assertEqual(asia_codes, {"^HSI", "^KS11", "^N225"})

    def test_board_format(self):
        board = quotes.format_board([
            quotes.Quote("美股", "标普", "^GSPC", change_pct=-2.4, ok=True),
            quotes.Quote("商品", "黄金", "GC=F", change_pct=0.89, ok=True),
            quotes.Quote("欧股", "德国DAX", "^GDAXI", ok=False, error="x"),
        ])
        self.assertEqual(board[0], "【行情走向】")
        self.assertTrue(any("标普 -2.40%" in line for line in board))
        self.assertTrue(any("黄金 +0.89%" in line for line in board))
        self.assertFalse(any(line.startswith("欧股：") for line in board))


class MessageFormatTests(unittest.TestCase):
    """按类别分组、每条一行带链接、空类别不显示、末尾固定看板。"""

    def test_grouping_and_count(self):
        items = [
            fake_item("美联储威廉姆斯：行动效果良好", "https://a/1", "央行"),
            fake_item("浑水做空英伟达", "https://a/2", "重大事件"),
            fake_item("浑水再做空超微电脑", "https://a/3", "重大事件"),
            fake_item("APEC热议全球贸易", "https://a/4", "政策/贸易"),
        ]
        msg = rules.build_message(items, ["美股：标普 -2.40%"])
        self.assertTrue(msg.startswith("全球经济 · 本轮 4 条"))
        self.assertIn("【央行】", msg)
        self.assertIn("【重大事件】", msg)
        self.assertIn("【政策/贸易】", msg)
        self.assertNotIn("【公司】", msg)
        self.assertTrue(msg.rstrip().endswith("美股：标普 -2.40%"))
        self.assertEqual(msg.count("https://a/"), 4)

    def test_each_item_on_one_line(self):
        items = [fake_item("浑水做空英伟达", "https://a/2", "重大事件")]
        msg = rules.build_message(items, ["美股：标普 -2.40%"])
        line = [l for l in msg.splitlines() if "浑水做空" in l][0]
        self.assertIn("https://a/2", line)
        self.assertEqual(len([l for l in msg.splitlines() if "浑水做空" in l]), 1)


class CapTests(unittest.TestCase):
    """10 条上限可配置。"""

    def test_default_ten(self):
        self.assertEqual(rules.MAX_ITEMS, 10)

    def test_round_robin_keeps_all_categories(self):
        titles = [
            ("美联储威廉姆斯：过去几个月行动效果良好", "https://c/1"),
            ("浑水做空英伟达", "https://e/1"),
            ("京东集团三季度营收增长3.5% 新业务收入砍半", "https://o/1"),
            ("APEC热议：全球贸易并未萎缩", "https://p/1"),
        ]
        fixture = {"routes": [{"source": "test", "label": "测试源", "items": [
            {"title": t, "url": u, "published_ms": NOW_MS - i * 1000}
            for i, (t, u) in enumerate(titles)]}], "quotes": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(fixture, fh, ensure_ascii=False)
            state = os.path.join(tmp, "state.json")
            report = os.path.join(tmp, "report.json")
            monitor_run.run(fixtures=path, state_path=state, report_path=report, now_ms=NOW_MS)
            with open(report, encoding="utf-8") as fh:
                data = json.load(fh)
        self.assertEqual({d["category"] for d in data["selected"]}, set(rules.CATEGORIES))

    def test_configurable_cap_through_run(self):
        fixture = {"routes": [], "quotes": []}
        items = []
        for i in range(15):
            chars = "".join(chr(0x4E00 + i * 8 + off) for off in range(4))
            items.append({"title": f"{chars[:2]}公司{chars[2:]}集团并购", "url": f"https://x/{i}",
                          "published_ms": NOW_MS - i * 1000})
        fixture["routes"] = [{"source": "test", "label": "测试源", "items": items}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cap.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(fixture, fh, ensure_ascii=False)
            state = os.path.join(tmp, "state.json")
            report = os.path.join(tmp, "report.json")
            with mock.patch.object(rules, "MAX_ITEMS", 3):
                code = monitor_run.run(fixtures=path, state_path=state, report_path=report,
                                       now_ms=NOW_MS)
            self.assertEqual(code, 0)
            with open(report, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(len(data["selected"]), 3)
            self.assertTrue(data["message"].startswith("全球经济 · 本轮 3 条"))


class UrlNormalizeTests(unittest.TestCase):
    """URL 归一化：治重复推送的第一道防线。"""

    def test_same_article_different_writings(self):
        a = "http://www.Yicai.com/brief/103445410.html?utm_source=weixin#part"
        b = "https://www.yicai.com/brief/103445410.html/?spm=a&ref=b&from=c&share_source=d"
        self.assertEqual(lang.normalize_url(a), lang.normalize_url(b))

    def test_scheme_host_case_and_trailing_slash(self):
        self.assertEqual(lang.normalize_url("HTTP://Example.COM/a/b/"),
                         lang.normalize_url("https://example.com/a/b"))
        self.assertEqual(lang.normalize_url("https://www.example.com/x"),
                         lang.normalize_url("https://example.com/x"))

    def test_tracking_params_removed_others_kept(self):
        self.assertEqual(lang.normalize_url("https://e.com/a?utm_source=x&id=7"),
                         lang.normalize_url("https://e.com/a?id=7"))
        self.assertNotEqual(lang.normalize_url("https://e.com/a?id=7"),
                            lang.normalize_url("https://e.com/a?id=8"))

    def test_different_articles_not_merged(self):
        self.assertNotEqual(lang.normalize_url("https://e.com/a/1"),
                            lang.normalize_url("https://e.com/a/2"))


class BigramTests(unittest.TestCase):
    """中文二元组相似度：纯标准库，不装分词库。"""

    def test_chinese_bigrams(self):
        self.assertEqual(lang.bigrams("比特币"), {"比特", "特币"})

    def test_stopwords_and_numbers(self):
        self.assertEqual(lang.bigrams("the 25% rise"), {"25%", "rise"})
        self.assertIn("3.5%", lang.bigrams("营收增长3.5%"))

    def test_similarity_value(self):
        sim = lang.bigram_similarity("美国就业报告强劲增长", "强劲增长的美国就业报告数据")
        self.assertGreaterEqual(sim, rules.SAME_EVENT_SIMILARITY)

    def test_same_event_same_day_merged(self):
        self.assertTrue(rules.same_event("美国就业报告强劲增长", NOW_MS,
                                          "强劲增长的美国就业报告数据", NOW_MS))

    def test_different_day_not_merged(self):
        self.assertFalse(rules.same_event("美国就业报告强劲增长", NOW_MS,
                                           "强劲增长的美国就业报告数据", NOW_MS + 86_400_000))

    def test_different_number_or_region_not_merged(self):
        self.assertFalse(rules.same_event("英国央行宣布加息25个基点", NOW_MS,
                                           "英国央行宣布降息25个基点", NOW_MS))
        self.assertFalse(rules.same_event("美国10年期国债收益率上涨5个基点", NOW_MS,
                                           "德国10年期国债收益率上涨5个基点", NOW_MS))
        self.assertFalse(rules.same_event("美国10月CPI同比上涨3.1%", NOW_MS,
                                           "美国10月CPI同比上涨3.2%", NOW_MS))
        self.assertFalse(rules.same_event("法国11月制造业PMI为48.2", NOW_MS,
                                           "德国11月制造业PMI为48.2", NOW_MS))

    def test_more_complete_kept(self):
        class Item:
            def __init__(self, title):
                self.title = title
                self.item = type("S", (), {"summary": "", "published_ms": NOW_MS})()
        self.assertTrue(rules.more_complete(Item("短标题更完整一些"), Item("短标题")))
        self.assertFalse(rules.more_complete(Item("短标题"), Item("短标题更完整一些")))


class SourceStateTests(unittest.TestCase):
    """源状态：ok / empty / partial_failure / failure。"""

    def test_route_states(self):
        empty = rsshub.RouteResult("demo", "样例", True, "ok", items=[])
        failed = rsshub.RouteResult("demo", "样例", False, "provider_error")
        good = rsshub.RouteResult("demo", "样例", True, "ok",
                                  items=[rsshub.NewsItem("demo", "样例", "标题一二三四五六",
                                                          "https://a/1", NOW_MS)])
        self.assertEqual(rsshub.route_state(empty), "empty")
        self.assertEqual(rsshub.route_state(failed), "failure")
        self.assertEqual(rsshub.route_state(good), "ok")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.report = os.path.join(self.tmp.name, "report.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_with(self, routes):
        with mock.patch.object(monitor_run, "fetch_all", return_value=routes), \
             mock.patch.object(quotes, "fetch_board", return_value=([], [])):
            return monitor_run.run(state_path=self.state, report_path=self.report, now_ms=NOW_MS)

    def test_all_empty_is_not_failure(self):
        routes = [rsshub.RouteResult(r.id, r.label, True, "ok", items=[]) for r in sources.active_routes()]
        code = self._run_with(routes)
        self.assertEqual(code, 0)
        with open(self.report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["source_state"], "empty")
        self.assertFalse(data["all_sources_failed"])

    def test_some_empty_some_ok(self):
        routes = []
        for index, route in enumerate(sources.active_routes()):
            items = [] if index == 0 else [fake_item("美联储威廉姆斯：行动效果良好", "https://a/1")]
            routes.append(rsshub.RouteResult(route.id, route.label, True, "ok", items=items))
        code = self._run_with(routes)
        self.assertEqual(code, 0)
        with open(self.report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["source_state"], "ok")
        self.assertEqual(data["source_states"][sources.active_routes()[0].id], "empty")

    def test_partial_failure_state(self):
        good = rsshub.RouteResult("caixin-latest", "财新", True, "ok",
                                  items=[fake_item("浑水做空英伟达", "https://a/1")])
        bad = rsshub.RouteResult("jin10", "金十", False, "provider_error", error="timeout")
        code = self._run_with([good, bad])
        self.assertEqual(code, 1)
        with open(self.report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["source_state"], "partial_failure")


class MergeTests(unittest.TestCase):
    """两道防线：先按归一化 URL，再按标题相似度。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.report = os.path.join(self.tmp.name, "report.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, items):
        fixture = {"routes": [{"source": "test", "label": "测试源", "items": items}], "quotes": []}
        path = os.path.join(self.tmp.name, "f.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, ensure_ascii=False)
        monitor_run.run(fixtures=path, state_path=self.state, report_path=self.report, now_ms=NOW_MS)
        with open(self.report, encoding="utf-8") as fh:
            return json.load(fh)

    def test_same_url_different_writing_kept_one(self):
        data = self._run([
            {"title": "国家能源局印发流域水风光一体化发展指导意见",
             "url": "https://www.yicai.com/brief/1.html?utm_source=x", "published_ms": NOW_MS},
            {"title": "国家能源局印发流域水风光一体化发展指导意见",
             "url": "http://yicai.com/brief/1.html#frag", "published_ms": NOW_MS - 1000},
        ])
        self.assertEqual(len(data["selected"]), 1)
        self.assertEqual(data["stats"]["duplicated_url"], 1)

    def test_keeps_the_most_complete_one(self):
        data = self._run([
            {"title": "欧洲央行提高银行最低准备金比例", "url": "https://e.com/a", "published_ms": NOW_MS},
            {"title": "欧洲央行提高银行最低准备金比例并讨论后续调整",
             "url": "https://e.com/b", "published_ms": NOW_MS - 1000},
        ])
        self.assertEqual(len(data["selected"]), 1)
        self.assertIn("后续调整", data["selected"][0]["title"])

    def test_bigram_only_merge(self):
        data = self._run([
            {"title": "美联储就业报告强劲增长", "url": "https://e.com/x", "published_ms": NOW_MS},
            {"title": "强劲增长的美联储就业报告数据", "url": "https://e.com/y", "published_ms": NOW_MS},
        ])
        self.assertEqual(len(data["selected"]), 1)


class DedupTests(unittest.TestCase):
    """去重静默：推过的不再推、干跑不写 seen、失败进 pending。"""

    def test_same_event_merged_to_one(self):
        # 同一份文件被两家各报一次，只留一条
        left = "国家能源局：探索流域水风光一体化与绿色氢氨醇协同发展"
        right = "国家能源局印发《关于加快推动流域水风光一体化发展的指导意见》"
        self.assertTrue(lang.same_title(left, right))
        self.assertTrue(lang.same_title("国家能源局印发流域水风光一体化发展指导意见",
                                        "国家能源局发布流域水风光一体化发展指导意见"))

    def test_opposite_events_not_merged(self):
        self.assertFalse(lang.same_title("英国央行宣布加息25个基点", "英国央行宣布降息25个基点"))
        self.assertFalse(lang.same_title("美国参议院通过支出法案", "美国众议院通过支出法案"))
        self.assertFalse(lang.same_title("汉堡王中国业务出售给CPE源峰", "星巴克中国业务出售给博裕资本"))
        self.assertFalse(lang.same_title("浑水做空英伟达", "浑水做空超微电脑"))
        # 数字、季度、地区不同就不是同一件事，不能吞并。
        self.assertFalse(lang.same_title("美国10月CPI同比上涨3.1%", "美国10月CPI同比上涨3.2%"))
        self.assertFalse(lang.same_title("英伟达第三季度营收570亿美元", "英伟达第四季度营收570亿美元"))
        self.assertFalse(lang.same_title("法国11月制造业PMI为48.2", "德国11月制造业PMI为48.2"))
        self.assertFalse(lang.same_title("OPEC+决定下月增产13.7万桶/日",
                                        "OPEC+决定下月减产13.7万桶/日"))

    def test_run_keeps_only_one_of_same_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = {"routes": [{"source": "test", "label": "测试源", "items": [
                {"title": "国家能源局印发流域水风光一体化发展指导意见",
                 "url": "https://a/1", "published_ms": NOW_MS},
                {"title": "国家能源局发布流域水风光一体化发展指导意见",
                 "url": "https://a/2", "published_ms": NOW_MS - 1000},
            ]}], "quotes": []}
            path = os.path.join(tmp, "f.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(fixture, fh, ensure_ascii=False)
            state = os.path.join(tmp, "state.json")
            report = os.path.join(tmp, "report.json")
            monitor_run.run(fixtures=path, state_path=state, report_path=report, now_ms=NOW_MS)
            with open(report, encoding="utf-8") as fh:
                data = json.load(fh)
        self.assertEqual(len(data["selected"]), 1)
        self.assertEqual(data["stats"]["duplicated"], 1)

    def test_mark_and_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.State(os.path.join(tmp, "state.json"))
            original = fake_item("浑水做空英伟达", "https://x/1", "重大事件")
            self.assertFalse(st.known_event(original))
            st.mark_event(original)
            st.save()
            again = state_mod.State(os.path.join(tmp, "state.json")).load()
            rewritten = fake_item("浑水做空英伟达 质疑高管关联", "https://x/1?utm=1", "重大事件")
            self.assertTrue(again.known_event(rewritten))
            self.assertEqual(lang.normalize_url("https://x/1?utm=1"),
                             lang.normalize_url("https://x/1"))

    def test_dry_run_does_not_mark_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = {"routes": [{"source": "test", "label": "测试源", "items": [
                {"title": "美联储威廉姆斯：行动效果良好", "url": "https://a/1",
                 "published_ms": NOW_MS}]}], "quotes": []}
            path = os.path.join(tmp, "f.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(fixture, fh, ensure_ascii=False)
            state = os.path.join(tmp, "state.json")
            code = monitor_run.run(fixtures=path, state_path=state, now_ms=NOW_MS)
            self.assertEqual(code, 0)
            with open(state, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["seen"], [])

    def test_seen_items_go_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = {"routes": [{"source": "test", "label": "测试源", "items": [
                {"title": "美联储威廉姆斯：行动效果良好", "url": "https://a/1",
                 "published_ms": NOW_MS}]}], "quotes": []}
            path = os.path.join(tmp, "f.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(fixture, fh, ensure_ascii=False)
            state = os.path.join(tmp, "state.json")
            report = os.path.join(tmp, "report.json")
            st = state_mod.State(state)
            st.mark_event(fake_item("美联储威廉姆斯：行动效果良好", "https://a/1", "央行"))
            st.save()
            code = monitor_run.run(fixtures=path, state_path=state, report_path=report,
                                   now_ms=NOW_MS)
            self.assertEqual(code, 0)
            with open(report, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["selected"], [])
            self.assertEqual(data["message"], "")
            self.assertEqual(data["skipped_seen"], 1)

    def test_pending_survives_push_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.State(os.path.join(tmp, "state.json"))
            st.add_pending("待重推", "测试")
            st.save()
            again = state_mod.State(os.path.join(tmp, "state.json")).load()
            self.assertEqual(len(again.pending), 1)


class RunExitCodeTests(unittest.TestCase):
    """全源失败单独告警（退出码 2），部分失败隔离（退出码 1）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.report = os.path.join(self.tmp.name, "report.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_sources_failed_exit_two(self):
        bad = [rsshub.RouteResult(name=r.id, label=r.label, ok=False,
                                  status="provider_error", error="timeout")
               for r in sources.active_routes()]
        with mock.patch.object(monitor_run, "fetch_all", return_value=bad), \
             mock.patch.object(quotes, "fetch_board", return_value=([], [])):
            code = monitor_run.run(state_path=self.state, report_path=self.report, now_ms=NOW_MS)
        self.assertEqual(code, 2)
        with open(self.report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertTrue(data["all_sources_failed"])
        self.assertEqual(data["message"], "")

    def test_partial_failure_is_isolated(self):
        good = rsshub.RouteResult(name="caixin-latest", label="财新", ok=True, status="ok",
                                  items=[fake_item("浑水做空英伟达", "https://a/1")])
        bad = rsshub.RouteResult(name="jin10", label="金十", ok=False,
                                 status="provider_error", error="timeout")
        with mock.patch.object(monitor_run, "fetch_all", return_value=[good, bad]), \
             mock.patch.object(quotes, "fetch_board", return_value=([], [])):
            code = monitor_run.run(state_path=self.state, report_path=self.report, now_ms=NOW_MS)
        self.assertEqual(code, 1)
        with open(self.report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertFalse(data["all_sources_failed"])
        self.assertEqual(len(data["selected"]), 1)

    def test_send_without_secret_is_config_error(self):
        good = rsshub.RouteResult(name="caixin-latest", label="财新", ok=True, status="ok",
                                  items=[fake_item("浑水做空英伟达", "https://a/1")])
        env = {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(quotes, "fetch_board", return_value=([], [])), \
             mock.patch.object(monitor_run, "fetch_all", return_value=[good]):
            code = monitor_run.run(state_path=self.state, send=True, now_ms=NOW_MS)
        self.assertEqual(code, 5)


class TelegramTests(unittest.TestCase):
    """推送细节：关链接预览、空消息与超长拒绝、无凭据拒绝。"""

    def test_payload_disables_preview(self):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok":true,"result":{}}'

        def fake_urlopen(req, timeout):
            captured["data"] = json.loads(req.data.decode("utf-8"))
            return FakeResp()

        with mock.patch.object(telegram.urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(telegram.send_message("token", "chat", "hello"))
        self.assertTrue(captured["data"]["disable_web_page_preview"])
        self.assertEqual(captured["data"]["text"], "hello")

    def test_empty_and_missing_credentials(self):
        with self.assertRaises(telegram.PushError):
            telegram.send_message("t", "c", "")
        with self.assertRaises(telegram.PushError):
            telegram.send_message("", "", "hello")


if __name__ == "__main__":
    unittest.main()
