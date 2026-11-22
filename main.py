"""全球经济新闻监控入口。

python main.py --self-check                         离线自检（不联网、不写状态）
python main.py --fixtures tests/fixtures/sample.json --dry-run
python main.py --list-sources                       列出财经路由、实例与行情标的
python main.py --dry-run --candidates               真实抓源；只打印不推送（当前唯一允许的联网模式）
python main.py --send                               用户明确说「接推送」之后才用
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitor import lang, quotes, rules, rsshub, sources, state as state_mod, telegram  # noqa: E402

NOW_MS = 1794945600000


def _self_check() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # 源清单
    active = sources.active_routes()
    check(len(active) == 5, f"财经路由应为 5，实际 {len(active)}")
    check(len(sources.active_symbols()) == 24, "行情标的应为 24 个")
    check(sources.RSS_INSTANCES[0] == "https://rsshub.rssforever.com", "首选实例不对")
    check("rsshub.app" not in " ".join(sources.RSS_INSTANCES), "官方 403 实例混进了实例表")
    check("cls-telegraph" in sources.EXCLUDED_RSS, "财联社 503 路由没有留档排除原因")

    # RSSHub 解析（RSS2 + 时间过滤字段）
    route = sources.RssRoute("demo", "/demo", "样例源")
    xml = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
      <item><title><![CDATA[美联储威廉姆斯：过去几个月行动效果良好]]></title>
        <link>https://a/1</link><pubDate>Tue, 17 Nov 2026 20:00:00 GMT</pubDate></item>
      <item><title>纳指跌超2%</title><link>https://a/2</link>
        <pubDate>Tue, 17 Nov 2026 19:00:00 GMT</pubDate></item>
      <item><title></title><link>https://a/3</link></item>
    </channel></rss>"""
    items = rsshub.parse_feed(xml, route)
    check(len(items) == 2, f"RSS 解析应留 2 条，实际 {len(items)}")
    check(items[0].title.startswith("美联储威廉姆斯"), "CDATA 标题没解出来")
    check(items[0].published_ms > 0, "pubDate 没解出毫秒时间戳")
    try:
        rsshub.parse_feed("<rss><channel></channel></rss>", route)
        check(False, "无 item 的响应应抛错换实例")
    except ValueError:
        pass
    check(rsshub.parse_pubdate("1794945600") == 1_794_945_600_000, "秒级时间戳换算错误")
    check(rsshub.parse_pubdate("2026-11-18 05:00:00") > 0, "中文时间格式没解析出来")
    check(rsshub.parse_pubdate("not a date") == 0, "坏时间应返回 0")

    # 中文过滤与 HTML 清洗
    check(lang.is_chinese("京东集团三季度营收增长3.5%"), "中文标题被挡")
    check(lang.is_chinese("小米回应雷军APEC发言紧张"), "夹英文的中文被挡")
    check(not lang.is_chinese("US stocks rally as Fed signals rate cut"), "纯英文没挡住")
    check(not lang.is_chinese("APEC"), "纯缩写没挡住")
    check("<b>" not in lang.clean_title("<b>纳指跌超2% |</b> 纳指跌超2%"), "HTML 标签没洗掉")
    check(lang.clean_title("格隆汇11月18日｜浑水做空英伟达") == "浑水做空英伟达", "源前缀没洗掉")

    # URL 归一化：同一篇文章的不同写法归一后相同（治重复推送的第一道防线）
    link_a = "http://www.Yicai.com/brief/103445410.html?utm_source=weixin#part"
    link_b = "https://www.yicai.com/brief/103445410.html/?spm=a&ref=b&from=c&share_source=d"
    check(lang.normalize_url(link_a) == lang.normalize_url(link_b), "URL 归一化没生效")
    check(lang.normalize_url(link_a) != lang.normalize_url("https://www.yicai.com/brief/103445411.html"),
          "不同文章被判成同一篇")

    # 中文二元组：纯标准库切分，不装分词库
    check(lang.bigrams("比特币") == {"比特", "特币"}, "中文二元组切分错误")
    check(lang.bigrams("the 25% rise") == {"25%", "rise"}, "英文停用词或数字百分比处理错误")
    check(lang.bigram_similarity("美国就业报告强劲增长", "强劲增长的美国就业报告数据")
          >= rules.SAME_EVENT_SIMILARITY, "二元组相似度算错了")
    check(rules.same_event("美国就业报告强劲增长", NOW_MS, "强劲增长的美国就业报告数据", NOW_MS),
          "同一天的同一件事没被并成一条")
    check(not rules.same_event("美国就业报告强劲增长", NOW_MS,
                               "强劲增长的美国就业报告数据", NOW_MS + 86_400_000),
          "跨天的内容被误并成一条")
    check(not rules.same_event("英国央行宣布加息25个基点", NOW_MS,
                               "英国央行宣布降息25个基点", NOW_MS),
          "加息/降息被误并成一条")
    check(not rules.same_event("美国10月CPI同比上涨3.1%", NOW_MS,
                               "美国10月CPI同比上涨3.2%", NOW_MS),
          "数字不同的两条被误并成一条")
    check(not rules.same_event("法国11月制造业PMI为48.2", NOW_MS,
                               "德国11月制造业PMI为48.2", NOW_MS),
          "地区不同的两条被误并成一条")
    check(not rules.same_event("OPEC+决定下月增产13.7万桶/日", NOW_MS,
                               "OPEC+决定下月减产13.7万桶/日", NOW_MS),
          "增产/减产被误并成一条")

    # 同一件事只留信息最全的一条
    class _Item:
        def __init__(self, title, summary="", ms=NOW_MS):
            self.title = title
            self.item = type("S", (), {"summary": summary, "published_ms": ms})()
    check(rules.more_complete(_Item("短标题更完整一些"), _Item("短标题")), "没选出信息更全的一条")
    check(not rules.more_complete(_Item("短标题"), _Item("短标题更完整一些")), "选错了信息更全的一条")

    # 源状态：empty = 请求成功但 0 条（正常，不算故障）；failure = 抓取失败
    check(rsshub.route_state(rsshub.RouteResult("demo", "样例", True, "ok", items=[])) == "empty",
          "0 条的源没有被标成 empty")
    check(rsshub.route_state(rsshub.RouteResult("demo", "样例", False, "provider_error")) == "failure",
          "抓取失败的源没有被标成 failure")
    ok_route = rsshub.RouteResult("demo", "样例", True, "ok",
                                   items=[rsshub.NewsItem("demo", "样例", "标题一二三四五六",
                                                           "https://a/1", NOW_MS)])
    check(rsshub.route_state(ok_route) == "ok", "有数据的源没有被标成 ok")

    # 四类判定（首个命中即停，记规则名）
    cases = [
        ("美联储威廉姆斯：过去几个月行动效果良好", "央行"),
        ("巴克莱预计2027年市政债供给量最高达6,000亿美元", "央行"),
        ("浑水做空英伟达", "重大事件"),
        ("桥水、索罗斯等机构三季度减持英伟达 华尔街对AI投资现分歧", "重大事件"),
        ("京东集团三季度营收增长3.5% 京东外卖、海外等新业务收入砍半", "公司"),
        ("英伟达将在盘后公布Q3财报", "公司"),
        ("APEC热议：全球贸易并未萎缩 只是版图已重构", "政策/贸易"),
        ("支出法案或再推迟，美国民主党寻求短期权宜方案", "政策/贸易"),
    ]
    for title, category in cases:
        got, rule = rules.classify(title)
        check(got == category, f"归类错误「{title}」→ {got or '未归类'}")
        check(bool(rule), f"归类后没有记规则名：{title}")

    # 同事件去重：同一份文件被两家各报一次只留一条；相反事件不能误合并
    check(lang.same_title("国家能源局：探索流域水风光一体化与绿色氢氨醇协同发展",
                          "国家能源局印发《关于加快推动流域水风光一体化发展的指导意见》"),
          "同一份文件的两种写法没被并成一条")
    check(not lang.same_title("英国央行宣布加息25个基点", "英国央行宣布降息25个基点"),
          "加息/降息被误当同一件事")
    check(not lang.same_title("美国参议院通过支出法案", "美国众议院通过支出法案"),
          "参议院/众议院被误当同一件事")

    # 个股财报数字细节挡掉，大动作留住
    for title in ["劳氏第三季度调整后每股收益3.10美元，上年同期3.06美元，预估2.92美元",
                  "英伟达2027财年Q3净利润653亿美元，去年同期319.1亿美元",
                  "某公司拟3亿元-5亿元回购股份"]:
        check(rules.filter_reason(title) == "earnings_detail", f"个股财报数字没挡住：{title}")
    check(rules.filter_reason("京东集团三季度营收增长3.5% 新业务收入砍半") != "earnings_detail",
          "有大动作的营收消息被误当碎片挡掉")

    # 纯行情快讯、币圈、非经济杂项都要挡
    blocked_quotes = [
        "纳指跌超2%，道指跌1.7%，标普500指数跌1.65%",
        "富时A50期指连续夜盘收跌0.09%，报12650点。",
        "美股收盘：三大股指集体收跌",
        "国际油价17日微涨",
        "国内商品期货夜盘收盘 | 燃油涨1.36%，棉花涨0.94%",
    ]
    for title in blocked_quotes:
        check(rules.filter_reason(title) == "market_quote", f"纯行情快讯没挡住：{title}")
    for title in ["比特币突破10万美元", "狗狗币大涨 币圈全线飘红",
                  "ETH现货ETF净流入创新高", "USDT 稳定币储备曝光"]:
        check(rules.filter_reason(title) == "crypto", f"币圈内容没挡住：{title}")
    check(rules.filter_reason("数字人民币试点扩容") != "crypto", "央行数字货币被误当币圈挡掉")
    for title in ["马坦·卡哈纳以全票当选以色列议会议长", "金正恩携妻女视察在建旅游区"]:
        check(rules.filter_reason(title) == "non_economic", f"非经济杂项没挡住：{title}")

    # 行情看板：range=5d 必须用 close 数组最后两个有效值算单日涨跌。
    # chartPreviousClose=7561.67 是区间开始前的收盘，拿它算会得到 -8.61% 的 5 天累计。
    payload = {"chart": {"result": [{
        "meta": {"regularMarketPrice": 6910.39, "chartPreviousClose": 7561.67, "symbol": "^GSPC"},
        "indicators": {"quote": [{"close": [
            7416.58984375, 7340.52978515625, 7323.6298828125,
            7175.830078125, 6910.39013671875]}]},
    }]}}
    quote = quotes.parse_chart(payload, sources.QuoteSymbol("美股", "标普500", "^GSPC"))
    check(quote.ok and abs(quote.change_pct - (-3.70)) < 0.01,
          f"单日涨跌计算错误：{quote.change_pct:.2f}%")
    check(abs(quote.previous_close - 7175.830078125) < 0.01,
          "昨收取错了：要用 close 倒数第二个有效值，不能用 chartPreviousClose")
    check(quotes.quote_url("^GSPC").endswith("%5EGSPC?interval=1d&range=5d"), "行情代码没做 URL 编码")
    check(quotes.quote_url("GC=F").find("GC%3DF") >= 0, "期货代码没编码")
    board = quotes.format_board([
        quotes.Quote("美股", "标普", "^GSPC", change_pct=-2.4, ok=True),
        quotes.Quote("商品", "黄金", "GC=F", change_pct=0.89, ok=True),
        quotes.Quote("欧股", "德国DAX", "^GDAXI", ok=False, error="x"),
    ])
    check(board[0] == "【行情走向】", "看板标题不对")
    check(any(line.startswith("美股：标普 -2.40%") for line in board), "看板百分比格式不对")
    check(any(line.startswith("商品：黄金 +0.89%") for line in board), "看板正数格式不对")
    check(not any(line.startswith("欧股：") for line in board), "失败标的没从看板跳过")

    # 类别分组格式：空类别不显示、每条带链接、末尾固定看板
    class FakeItem:
        def __init__(self, title, url, category):
            self.title, self.url, self.category = title, url, category
    message = rules.build_message([
        FakeItem("美联储威廉姆斯：过去几个月行动效果良好", "https://a/1", "央行"),
        FakeItem("浑水做空英伟达", "https://a/2", "重大事件"),
    ], ["美股：标普 -2.40%"])
    check(message.startswith("全球经济 · 本轮 2 条"), "消息头不对")
    check("【央行】" in message and "【重大事件】" in message, "类别标题缺失")
    check("【公司】" not in message and "【政策/贸易】" not in message, "空类别没隐藏")
    check("https://a/2" in message, "原文链接没带上")
    check("【行情走向】" in message, "末尾行情看板缺失")

    # 10 条上限可配置
    check(rules.MAX_ITEMS == 10, f"10 条上限应为 10，实际 {rules.MAX_ITEMS}")

    # 去重静默 + 待重推队列
    st = state_mod.State("state/selfcheck-state.json")
    check(not st.known_event(FakeItem("浑水做空英伟达", "https://x/1", "重大事件")), "没推过的被当成见过")
    st.mark_event(FakeItem("浑水做空英伟达", "https://x/1", "重大事件"))
    check(st.known_event(FakeItem("浑水做空英伟达 质疑高管关联", "https://x/1?utm=1", "重大事件")),
          "改写标题+追踪参数的同一条没被去重")
    st.add_pending("待重推消息")
    check(len(st.pending) == 1, "pending 队列没写入")

    if failures:
        for item in failures:
            print("自检失败：" + item, file=sys.stderr)
        return 5
    print(f"自检通过：{len(active)} 个财经路由／{len(sources.active_symbols())} 个行情标的；"
          "RSS 解析、中文过滤、URL 归一化、中文二元组、四类判定、纯行情/币圈/杂项/财报数字挡掉、"
          "同事件去重、源状态 empty、看板计算（含 A股独立分组）、分组格式、10 条上限、去重静默均正常")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="全球经济新闻监控")
    parser.add_argument("--dry-run", action="store_true", help="只打印候选，不发送（默认）")
    parser.add_argument("--send", action="store_true", help="显式启用 Telegram 推送（需用户确认）")
    parser.add_argument("--self-check", action="store_true", help="离线自检，不联网不写状态")
    parser.add_argument("--list-sources", action="store_true", help="列出路由、实例与行情代码")
    parser.add_argument("--fixtures", default="", help="本地 JSON 样例，替代真实网络")
    parser.add_argument("--state", default="state/state.json", help="状态文件路径")
    parser.add_argument("--report", default="", help="运行报告路径")
    parser.add_argument("--candidates", action="store_true", help="打印每个候选的类别与命中规则")
    parser.add_argument("--check-telegram", action="store_true",
                        help="只读自查凭据：token 有效性、bot 名字、chat 能不能找到（不发消息）")
    args = parser.parse_args()

    if args.self_check:
        return _self_check()
    if args.check_telegram:
        token, chat_id = telegram.credentials()
        report = telegram.check_credentials(token, chat_id)
        print(f"token 有效：{report['token_ok']}｜bot：@{report['bot_username'] or '-'}"
              f"（id {report['bot_id'] or '-'}）")
        print(f"chat 可用：{report['chat_ok']}｜类型：{report['chat_type'] or '-'}"
              f"｜名称：{report['chat_title'] or '-'}")
        if report["error"]:
            print(f"问题：{report['error']}")
        return 0 if (report["token_ok"] and report["chat_ok"]) else 5
    if args.list_sources:
        for base in sources.RSS_INSTANCES:
            for route in sources.active_routes():
                print(f"rss\t{route.id}\t{route.label}\t{base}{route.path}")
        for symbol in sources.active_symbols():
            print(f"quote\t{symbol.group}\t{symbol.name}\t{quotes.quote_url(symbol.code)}")
        return 0
    if args.send and args.dry_run:
        print("--send 与 --dry-run 不能同时用", file=sys.stderr)
        return 5

    from monitor import run as monitor_run

    return monitor_run.run(fixtures=args.fixtures, state_path=args.state,
                           report_path=args.report, send=args.send,
                           print_candidates=args.candidates)


if __name__ == "__main__":
    sys.exit(main())
