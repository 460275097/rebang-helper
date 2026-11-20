# -*- coding: utf-8 -*-
"""
热榜助手 V2.0
作者：七度

纯 Python 标准库实现的多站点热榜聚合阅读器：
  - 后端：内置 HTTP 服务，40+ 新闻源抓取/解析、收藏、浏览记录、整页翻译
  - 前端：内嵌单页界面（有图/无图、朗读、关键词搜索、主题背景、源拖拽排序）
  - 关闭窗口可退到系统托盘常驻（Win32 托盘，纯 ctypes）
  - 无第三方依赖，可 PyInstaller 打包为单文件 exe
"""

import base64
import gzip
import hashlib, html as _html, json
import os
import re
import socket
import subprocess
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "热榜助手"
APP_VER = "8.0.7"
PRESET_VER = "8.0.7"   # 内置源清单版本：变化时启动会把内置源的新地址/说明同步进用户已有配置
AUTHOR = "七度"
# 安装包下载页（云托管静态页，手机浏览器可直接下载 exe/apk）
DL_PAGE_URL = "https://quickest-ocelot-d9fti0mr03ae447f-1518118034.tcloudbaseapp.com/dl/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 路径

def exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    env = os.environ.get("REBANG_DATA")
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME + "V2")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return exe_dir()


CONFIG_PATH = os.path.join(data_dir(), "热榜助手配置.json")
CACHE_PATH = os.path.join(data_dir(), "热榜缓存.json")
LOG_PATH = os.path.join(data_dir(), "热榜助手.log")


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------- 源清单

def S(sid, name, url, parser="auto", sel=None, count=30, enabled=True, note="", search="",
      cat="综合", alt=None):
    return {"id": sid, "name": name, "url": url, "parser": parser, "sel": sel or {},
            "count": count, "enabled": enabled, "note": note, "search": search, "cat": cat,
            "alt": [str(a).strip() for a in (alt or []) if str(a or "").strip()][:5]}


def split_cat(raw):
    """V8.0.2：一个源可以同时属于多个分类，源里的 cat 用逗号分隔（如「开源,资源」）"""
    parts = re.split(r"[,，、;；/]+", str(raw or ""))
    out = []
    for p in parts:
        p = p.strip()[:CAT_MAX]
        if p and p not in out and p not in ("全部", "总排行", "地区新闻", "本地"):
            out.append(p)
    return out


def join_cat(raw):
    """把任意写法的分类字段规范成「主分类,次分类…」字符串"""
    cs = split_cat(raw)
    return ",".join(cs[:4]) if cs else "综合"


# 分类：综合 / 财经 / 娱乐 / 科技 / 社区 / 小说（M8.2：国际新闻、币圈整类下线）
CATS = ["综合", "财经", "娱乐", "科技", "社区", "小说", "资源", "Agent",
        "医学顶刊", "运动科学", "经济期刊", "开源", "推广"]

CAT_MAX = 12          # 单个分类名最大长度
CAT_MAXN = 60         # 最多分类数


def norm_cats(raw, fallback=None):
    """清洗用户自定义分类：去空、去重、限长、限量；始终保证有「综合」。"""
    out = []
    if isinstance(raw, (list, tuple)):
        for c in raw:
            c = str(c or "").strip()[:CAT_MAX]
            if not c or c in ("全部", "总排行", "地区新闻", "本地"):
                continue
            if c not in out:
                out.append(c)
            if len(out) >= CAT_MAXN:
                break
    if not out:
        out = list(fallback or CATS)
    if "综合" not in out:
        out.insert(0, "综合")
    return out


_MIRROR = "https://tophub.today/n/"

DEFAULT_SOURCES = [
    S("baidu", "百度热搜", "https://top.baidu.com/board?tab=realtime", "baidu", note="百度实时热搜",
      search="https://www.baidu.com/s?wd={q}", cat="综合"),
    S("weibo", "微博热搜", "https://weibo.com/ajax/side/hotSearch", "weibo", note="官方接口 + 镜像兜底",
      search="https://s.weibo.com/weibo?q={q}", cat="娱乐"),
    S("zhihu", "知乎热榜", _MIRROR + "mproPpoq6O", "tophub", note="热榜镜像",
      search="https://www.zhihu.com/search?q={q}", cat="综合"),
    S("zhihudaily", "知乎日报", _MIRROR + "KMZd7VOvrO", "tophub",
      note="每日精选", search="https://www.zhihu.com/search?q={q}", cat="综合"),
    S("bilibili", "哔哩哔哩", "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
      "bilibili", note="全站排行榜", search="https://search.bilibili.com/all?keyword={q}", cat="娱乐"),
    S("douyin", "抖音", _MIRROR + "DpQvNABoNE", "tophub", count=20, note="热点总榜",
      search="https://www.douyin.com/search/{q}", cat="娱乐"),
    S("toutiao", "今日头条", "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
      "toutiao", note="头条热榜", search="https://so.toutiao.com/search?keyword={q}", cat="综合"),
    S("qqnews", "腾讯新闻", "https://r.inews.qq.com/gw/event/hot_ranking_list?page_size=50",
      "qqnews", note="热点榜", search="https://so.html5.qq.com/page/real/search_news?docid=&query={q}", cat="综合"),
    S("ithome", "IT之家", "https://api.ithome.com/json/newslist/news", "ithome", note="最新资讯",
      search="https://so.ithome.com/s/{q}", cat="科技"),
    S("hupu", "虎扑", _MIRROR + "G47o8weMmN", "tophub", note="步行街热帖",
      search="https://search.hupu.com/search?q={q}", cat="娱乐"),
    S("douban_group", "豆瓣社区", _MIRROR + "WYKd6jdaPj", "tophub", note="小组讨论精选",
      search="https://www.douban.com/search?q={q}", cat="社区"),
    S("douban_movie", "豆瓣书影音", _MIRROR + "mDOvnyBoEB", "tophub", note="新片榜",
      search="https://search.douban.com/movie/subject_search?search_text={q}", cat="小说"),
    S("sspai", "少数派", "https://sspai.com/api/v1/article/index/page/get?limit=20&offset=0",
      "sspai", note="热门文章", search="https://sspai.com/search/post/{q}", cat="科技"),
    S("thepaper", "澎湃新闻", _MIRROR + "wWmoO5Rd4E", "tophub", note="热榜",
      search="https://www.thepaper.cn/searchResult?inpsearch={q}", cat="综合"),
    S("ifeng", "凤凰新闻", "https://www.ifeng.com/", "html",
      {"block": "div.index_news_list_p78su", "link": "a"},
      note="要闻", search="https://search.ifeng.com/sofeng/search.action?q={q}", cat="综合"),
    S("netease", "网易新闻", "https://news.163.com/", "html",
      {"link": "ul.newsdata_list a"},
      note="要闻", search="https://www.163.com/search?keyword={q}", cat="综合"),
    S("dapenti", "喷嚏网", "https://www.dapenti.com/", "html",
      {"block": "div.section-content", "link": "h3 a"},
      note="喷嚏图卦/资讯", search="https://www.dapenti.com/", cat="娱乐"),
    S("guancha", "观风闻", "https://www.guancha.cn/", "html",
      {"link": "h4.module-title a"},
      note="风闻社区", search="https://www.guancha.cn/search?q={q}", cat="综合"),
    S("52pojie", "吾爱破解", "https://www.52pojie.cn/forum.php?mod=forumdisplay&fid=16"
      "&filter=typeid&typeid=232", "52pojie", count=30,
      note="软件安全 › 精品软件区 › [Android]", 
      search="https://www.52pojie.cn/search.php?mod=forum&srchtxt={q}", cat="资源"),
    S("10jqka", "同花顺", "https://news.10jqka.com.cn/", "html",
      {"link": "span.arc-title a"},
      note="财经要闻", search="https://search.10jqka.com.cn/search?q={q}", cat="财经"),
    S("landian", "蓝点网", "https://www.landiannews.com/", "html",
      {"block": "article.post-list-main", "link": "h2 a"},
      note="科技资讯", search="https://www.landiannews.com/?s={q}", cat="科技"),
    S("appinn", "小众软件", "https://www.appinn.com/", "html",
      {"link": "h2.post-title a"},
      note="软件推荐", search="https://www.appinn.com/?s={q}", cat="科技"),
    S("huanqiu", "环球网", "https://www.huanqiu.com/", "html",
      {"block": "dl.thrnewslist dd", "link": "a"},
      note="要闻", search="https://www.huanqiu.com/search?q={q}", cat="综合"),
    S("gamersky", "游民星空", "https://www.gamersky.com/", "html",
      {"block": "li.li3", "link": "a"},
      note="游戏资讯", search="https://so.gamersky.com/all?s={q}", cat="娱乐"),
    S("oschina", "开源中国", "https://www.oschina.net/news", "html",
      {"block": "div.event", "link": "a"},
      note="开源资讯", search="https://www.oschina.net/search?scope=news&q={q}", cat="科技"),
    S("github", "GitHub今日上升", "https://github.com/trending?since=daily", "github",
      note="按今日新增 Star 排序", search="https://github.com/search?q={q}", cat="开源,资源"),
    S("dangdang", "当当图书榜",
      "http://bang.dangdang.com/books/bestsellers/01.00.00.00.00.00-recent7-0-0-1-1",
      "html", {"block": "ul.bang_list li", "link": "div.name a", "heat": "div.star"},
      count=20, note="近7日畅销榜", search="http://search.dangdang.com/?key={q}", cat="小说"),
    S("ifanr", "爱范儿", "https://www.ifanr.com/", "html",
      {"block": "div.article-item--list", "link": "h3 a"},
      note="科技资讯", search="https://www.ifanr.com/?s={q}", cat="科技"),
    S("d423", "423Down", "https://www.423down.com/", "d423", count=20, note="软件更新",
      search="https://www.423down.com/search/{q}", cat="资源"),
    S("ypojie", "易破解", "https://www.ypojie.com/", "ypojie", count=20, note="软件更新",
      search="https://www.ypojie.com/?s={q}", cat="资源"),
    S("cr173", "下载吧", "https://www.cr173.com/", "cr173", count=20, note="软件更新",
      search="https://so.cr173.com/?q={q}", cat="资源"),
    # V8.0.2：删除「微线报」（baicaio）、「虎嗅」、「Apifox API 中心」、「GoodNovel」、
    # 「Property Week」，并记入 REMOVED_SOURCES，老配置升级时一并清掉
    # V7.8.1：微信读书榜单页（https://weread.qq.com/#rank）是服务端渲染，无需登录即可拿到
    # 「大家都在读 / 近期热读」书单 —— 换成专用解析器后实测可抓 40+ 条
    S("weread", "微信读书·读书榜", "https://weread.qq.com/#rank",
      "weread", count=20, enabled=False, note="微信读书热门书单（实测可抓，建议开启）",
      search="https://weread.qq.com/", cat="小说"),
    S("tieba", "百度贴吧", _MIRROR + "Om4ejxvxEN", "tophub", note="热议榜",
      search="https://tieba.baidu.com/f/search/res?qw={q}", cat="社区"),
    S("xiaohongshu", "小红书", "https://www.xiaohongshu.com/explore", "html", {}, enabled=False,
      note="需登录态，建议在设置里换成自己的接口地址", cat="社区"),

    # ---------------- V3.2 新增：科技 ----------------
    S("juejin", "掘金", "https://api.juejin.cn/content_api/v1/content/article_rank?category_id=1&type=hot",
      "juejin", count=20, note="综合热榜", search="https://juejin.cn/search?query={q}", cat="科技"),
    S("ithome_rss", "IT之家·最新", "https://www.ithome.com/rss/", "rss", count=30,
      note="官方 RSS 实时资讯", search="https://www.ithome.com/search/?q={q}", cat="科技"),
    S("sspai_rss", "少数派·最新", "https://sspai.com/feed", "rss", count=20,
      note="官方 RSS", search="https://sspai.com/search/post/{q}", cat="科技"),
    # M5.7/V7.4：36氪热榜的 tophub 镜像已失效，改抓官方「快讯」页（云端实测 20 条）
    S("kr36_hot", "36氪·快讯", "https://www.36kr.com/newsflashes/", "html", count=20,
      note="36氪官方快讯流", search="https://www.36kr.com/search/articles/{q}", cat="科技"),
    # M5.7/V7.4：用户新增——百度热搜（云端实测 51 条）
    S("baidu_hot", "百度热搜", "https://top.baidu.com/board?tab=realtime", "html", count=20,
      note="百度实时热搜榜", search="https://www.baidu.com/s?wd={q}", cat="综合"),
    # M5.7/V7.4：用户新增——哔哩哔哩热门（官方 API，云端实测 20 条；「每周必看」接口有风控 -352 不可用）
    S("bili_hot", "哔哩哔哩·热门", "https://api.bilibili.com/x/web-interface/popular?ps=20",
      "json", count=20, note="B站综合热门视频", search="", cat="娱乐"),
    S("landian_rss", "蓝点网·最新", "https://www.landiannews.com/feed", "rss", count=20,
      note="官方 RSS", cat="科技"),

    # ---------------- V3.2 新增：财经 ----------------
    S("eastmoney", "东方财富", "https://finance.eastmoney.com/", "html", count=25,
      note="财经要闻", search="https://so.eastmoney.com/news/s?keyword={q}", cat="财经"),
    S("yicai", "第一财经", "https://www.yicai.com/api/ajax/getlatest?page=1&pagesize=20", "yicai",
      count=20, note="最新资讯", search="https://www.yicai.com/search?keys={q}", cat="财经"),
    S("jiemian", "界面新闻", "https://www.jiemian.com/lists/2.html", "html", count=25,
      note="财经频道", search="https://www.jiemian.com/search.html?keyword={q}", cat="财经"),
    S("sina_fin", "新浪财经", "https://finance.sina.com.cn/", "html", count=25,
      note="财经要闻", search="https://search.sina.com.cn/?q={q}", cat="财经"),

    # ---------------- V3.2 新增：娱乐 ----------------
    S("douban_movie_hot", "豆瓣·热门电影", "https://movie.douban.com/j/search_subjects"
      "?type=movie&tag=%E7%83%AD%E9%97%A8&sort=recommend&page_limit=20&page_start=0",
      "douban_movie", count=20, note="热门推荐", search="https://movie.douban.com/explore#!type=movie&q={q}",
      cat="娱乐", alt=["https://rsshub.rssforever.com/douban/movie/weekly|rss"]),
    S("douyin_ent", "抖音·娱乐榜", _MIRROR + "DpQvNABoNE", "tophub", count=20,
      note="娱乐热点", cat="娱乐"),
    S("bili_rss", "B站·热门", "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
      "bilibili", count=30, note="排行榜", search="https://search.bilibili.com/all?keyword={q}",
      cat="娱乐"),
    S("ifeng_ent", "凤凰·娱乐", "https://ent.ifeng.com/", "html", count=20,
      note="娱乐资讯", cat="娱乐"),

    # ---------------- V3.2 新增：社区 ----------------
    S("v2ex", "V2EX·热门", "https://www.v2ex.com/api/topics/hot.json", "json", count=20,
      note="技术社区热帖（官方接口；部分地区直连不了可换自己的镜像）", search="https://www.google.com/search?q=site:v2ex.com+{q}", cat="社区"),

    S("tieba_hot", "贴吧·热议", _MIRROR + "Om4ejxvxEN", "tophub", count=20,
      note="热议榜", search="https://tieba.baidu.com/f/search/res?qw={q}", cat="社区"),
    S("hupu_bxj", "虎扑·步行街", _MIRROR + "G47o8weMmN", "tophub", count=20,
      note="步行街热帖", search="https://search.hupu.com/bxj?q={q}", cat="社区"),

    # ---------------- V3.2 新增：综合 ----------------
    S("chinanews_rss", "中国新闻网·滚动", "https://www.chinanews.com.cn/rss/scroll-news.xml",
      "rss", count=25, note="官方 RSS 滚动新闻", search="https://sou.chinanews.com.cn/search.do?q={q}",
      cat="综合"),
    S("thepaper_rss", "澎湃新闻·热榜", _MIRROR + "wWmoO5Rd4E", "tophub", count=25,
      note="热榜", search="https://www.thepaper.cn/searchResult?inpsearch={q}", cat="综合"),
    S("zaobao", "联合早报", "https://www.zaobao.com/realtime/china", "html", count=20,
      note="中国即时新闻", cat="综合"),

    # ---------------- V4.2 新增：股票财经 ----------------
    S("bloomberg", "彭博社", "https://news.so.com/ns?q=%E5%BD%AD%E5%8D%9A%E7%A4%BE&pn=1", "360news",
      count=20, note="经360新闻聚合（官网境外）", search="https://news.so.com/ns?q={q}", cat="财经"),
    S("taoguba", "淘股吧", "https://news.so.com/ns?q=%E6%B7%98%E8%82%A1%E5%90%A7&pn=1", "360news",
      count=20, note="经360新闻聚合（社区页JS渲染）", search="https://www.tgb.cn/search?type=news&keywords={q}", cat="财经"),
    S("gov_cn", "国务院文件", "https://www.gov.cn/", "html", count=20,
      note="中国政府网·国务院最新文件与要闻", search="https://sousuo.www.gov.cn/sousuo/search.shtml?code=17da70961a7&searchWord={q}", cat="财经"),
    S("sse_news", "上交所", "http://www.sse.com.cn/", "sse", count=20,
      note="新闻发布会与公告", search="http://query.sse.com.cn/search/portalSearch.do?keyword={q}", cat="财经"),
    S("modelscope", "ModelScope模型榜", "https://modelscope.cn/api/v1/dolphin/models", "modelscope",
      count=30, note="魔搭社区·热门模型", search="https://modelscope.cn/models?name={q}", cat="Agent"),
    S("sputnik", "卫星通讯社·中国", "https://sputniknews.cn/china/", "html", count=20,
      note="俄罗斯卫星通讯社中文版", search="https://sputniknews.cn/search/?query={q}", cat="财经"),

    # ---------------- 小说排行（国内 + 海外网文/电子书） ----------------
    S("nov_shuqi", "七猫小说排行", "https://www.qimao.com/paihang/", "html", count=25,
      note="七猫免费小说·排行榜（书旗官网为JS渲染抓不到，用七猫代替）", search="", cat="小说"),
    S("nov_weread", "纵横小说排行", "https://www.zongheng.com/rank/", "html", count=25,
      note="纵横中文网·排行榜（微信读书为JS渲染抓不到，用纵横代替）", search="", cat="小说"),
    S("nov_fanqie", "番茄小说排行", "https://fanqienovel.com/rank", "html", count=25,
      note="番茄免费小说·排行榜", search="", cat="小说"),
    S("nov_fanshu", "帆书热门", "https://www.dushu365.com", "html", count=25,
      note="帆书（原樊登读书）·热门好书", search="", cat="小说"),
    S("nov_dangdang", "当当网排行", "http://bang.dangdang.com/books/bestsellers/01.00.00.00.00.00-recent7-0-0-1-1", "html",
      sel={"block": "ul.bang_list li", "link": "div.name a", "heat": "div.star"}, count=25,
      note="当当·畅销热卖榜（近7日）", search="http://search.dangdang.com/?key={q}", cat="小说"),
    S("nov_wattpad", "Wattpad", "https://www.wattpad.com", "html", count=25,
      note="全球最大社交网文（青春/言情/同人）", search="https://www.wattpad.com/search/{q}", cat="小说"),

    S("nov_tapas", "Tapas", "https://tapas.io", "html", count=25,
      note="都市/奇幻/浪漫连载", search="https://tapas.io/search?query={q}", cat="小说"),
    S("nov_ao3", "AO3 同人", "https://archiveofourown.org", "html", count=25,
      note="Archive of Our Own 同人创作大站", search="https://archiveofourown.org/works/search?work_search[query]={q}", cat="小说"),
    # Kindle Vella（亚马逊 2025 年已停运）与 XinXii（站点改版后无可提取的图书列表）
    # 已从内置源移除；老用户配置里保留的不受影响。
    S("nov_smashwords", "Smashwords", "https://www.smashwords.com", "html", count=25,
      note="独立作者电子书分发", search="https://www.smashwords.com/search/?query={q}", cat="小说"),
    # 豆瓣·热门小组（403 反爬）、WebNovel（403）、古登堡计划（503 反爬）已删，见 REMOVED_SOURCES
    S("nov_overdrive", "OverDrive / Libby", "https://overdrive.com", "html", count=25,
      note="图书馆借阅电子书", search="", cat="小说"),
    S("nov_foxnovel", "FoxNovel", "https://www.foxnovel.com", "html", count=25,
      note="海外多语种网文", search="", cat="小说"),

    # ---------------- Agent（AI 智能体/模型竞技榜，M4.6 用户指定） ----------------
    S("agent_aiclub", "AI·Club Agent 榜", "https://www.ai-club.cn/rank/arena/agent", "html", count=20,
      note="AI Club 竞技场·Agent 排行（Elo 分）", search="", cat="Agent"),
    S("agent_aitnt", "AITNT Agent 排行", "https://www.aitntnews.com/arena/agent/", "html", count=20,
      note="AI TNT News·Agent 竞技场", search="", cat="Agent"),
    S("agent_atease", "临境 Arena Agent 榜", "https://arena.atease.dev/agent/", "html", count=25,
      note="临境竞技场·Agent 模型排行", search="", cat="Agent"),
    S("app_seadata", "SeaData Google Play 榜", "https://www.seadata.ai/rankings/google-play", "html", count=25, enabled=False,
      note="动态渲染且榜单需登录，暂无法抓取", search="", cat="科技"),
    S("app_socialpeta", "SocialPeta Google Play 榜", "https://socialpeta.com/zh/downloads-revenue-charts/google-play/apps", "html", count=30,
      note="Google Play 应用下载/收入榜", search="", cat="财经"),
    # V7.8.1：微软商店换到「Top free apps」分类页（服务端渲染），实测可抓 23 条应用名
    S("app_msstore", "微软商店热门应用", "https://apps.microsoft.com/store/category/Productivity?hl=zh-CN&gl=CN", "html", count=25, enabled=False,
      note="Top Free 应用下载榜（实测可抓，建议开启）", search="", cat="科技"),
    S("autohome_rank", "汽车之家销量榜", "https://www.autohome.com.cn/rank/1/", "autohome", count=20,
      note="车系月销榜（销量为热度）", search="https://so.autohome.com.cn/z?q={q}", cat="综合"),
    S("stats_gov", "国家统计局·数据发布", "https://www.stats.gov.cn/sj/zxfb/", "stats", count=30,
      note="首页›数据›数据发布（新闻标题）", search="https://so.stats.gov.cn/search?q={q}", cat="财经"),
    S("chinanews_site", "中国新闻网", "https://www.chinanews.com.cn/", "html",
      sel={"block": "div.rdph-list li", "link": "a"}, count=25,
      note="中新热榜", search="https://sou.chinanews.com.cn/search.do?q={q}", cat="综合"),
    S("cctv_news", "央视网新闻", "https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/news_1.jsonp?cp=1", "jsonp", count=30,
      note="央视网·要闻（官方接口）", search="https://search.cctv.com/search.php?qtext={q}", cat="综合"),
    S("chinacom_news", "中国网·要闻", "http://news.china.com.cn/node_7247300.htm", "html", count=30,
      note="国家重点新闻网站·要闻", search="http://search.china.com.cn/search?q={q}", cat="综合"),

    S("sucaizy", "素材资源", "https://www.sucaizy.com/", "sucaizy", count=20,
      note="最新发布·软件与素材资源", search="https://www.sucaizy.com/?s={q}", cat="资源"),
    S("guohe", "果核剥壳", "https://www.ghxi.com/feed", "rss", count=15,
      note="果核剥壳·绿色软件/破解资源更新", search="https://www.ghxi.com/?s={q}", cat="资源"),
    S("oscar_films", "奥斯卡·历届最佳影片", "", "oscar", count=98,
      note="第1届(1927/28)至第98届(2025)历届最佳影片（内置数据，离线可用）", search="", cat="娱乐"),

    # ---------------- 综合 · 票房 ----------------
    # M5.2/V6.9：piaofang.maoyan.com 是 JS 渲染的空壳页，本机与云端都取不到列表；
    # 换成猫眼主站「正在热映」列表页（静态 HTML，云端实测 18 条，含类型/主演/上映时间）
    S("maoyan_box", "猫眼·正在热映", "https://www.maoyan.com/films?showType=2", "html",
      count=20, note="猫眼电影·正在热映（含类型/主演/上映时间）", search="", cat="娱乐"),
    # M5.2/V6.9：淘票票「正在热映」列表（云端实测 93 条，带评分/导演/主演）——票房位可用替代
    S("taopiaopiao", "淘票票·正在热映", "https://www.taopiaopiao.com/showList.htm?cn=310100",
      "html", count=20, note="淘票票·正在热映（上海站，含评分/导演/主演）",
      search="", cat="娱乐"),
    # M5.2/V6.9：BoxOfficeMojo 全球票房总榜（云端实测 99 条，英文原始榜位）
    S("bom_chart", "BoxOfficeMojo·全球票房榜",
      "https://www.boxofficemojo.com/chart/top_lifetime_gross/", "html", count=20,
      note="BoxOfficeMojo 全球票房总榜（英文）", search="", cat="娱乐"),
]

# ---------------------------------------------------------------- M8.1：下线源与默认顺序
# 用户要求三端移除的源：已从清单剔除，同时会从用户旧配置里彻底删掉
REMOVED_SOURCES = (
    "zhihudaily",        # 知乎日报
    "36kr",              # 36氪
    "kr36_hot",          # 36氪·快讯
    "thepaper_rss",      # 澎湃新闻·热榜
    "douban_group",      # 豆瓣社区
    "douban_movie",      # 豆瓣书影音
    # M8.2：用户要求三端整类下线「国际新闻」「币圈」
    "intl_cnn", "intl_nyt", "intl_trends24", "intl_youtube", "intl_wapo",
    "intl_wsj", "intl_abcus", "intl_bbc", "intl_guardian", "intl_reuters",
    "intl_telegraph", "intl_economist", "intl_cbc", "intl_globemail", "intl_ctv",
    "intl_abcau", "intl_aus", "intl_newscomau", "intl_cna", "intl_straits",
    "intl_dw", "intl_lemonde",
    "cry_coindesk", "cry_cointelegraph", "cry_cryptopanic", "cry_theblock",
    "cry_decrypt", "cry_newsbtc", "cry_beincrypto",
    # M8.3：用户点名的 10 个源一并下线
    "agent_apirank", "nov_reedsy", "nov_feedbooks", "nov_novelcat", "nov_bnnoook",
    # M8.4：用户要求删掉「抓不到/已失效」的 34 个源（原名 _DEAD_SRC 清单 + 国务院新闻办）
    "scio_gov",
    "sourceforge_blog", "codeberg",
    "nmjc", "zhnk", "zhwk", "zhped", "zhbl", "zhcv", "zhjh", "zhsj", "yiigle",
    "pkuyxb", "fduyxb", "scuyxb", "sysuyxb", "hjmun", "hjkd",
    "scichina_life", "pumc", "wanfang_med", "cnki_med",
    "cjsm_cn", "sportinfo",
    "ceq", "iwec", "cqte", "cjryj",
    "gitlab", "gitee", "gitcode", "coding", "osdn", "launchpad",
    # M9.1：整类下线的 5 个分类 + 点名删除的 27 个实测失败源
    "macro_xinhua", "ind_miit",     # M9.1：把目录/源名当新闻标题抓（实测）
    "agri_spkx",
    "agri_zuowu",
    "ai_baai",
    "auto_autocar",
    "brand_36kr",
    "brand_chinagoods",
    "brand_digitaling",
    "brand_huxiu",
    "brand_iyiou",
    "brand_kuaibao",
    "brand_prn",
    "brand_retailplanet",
    "brand_star",
    "brand_wgsn",
    "chem_cuihua",
    "chem_ranliao",
    "cross_100ec",
    "cross_amz123",
    "cross_chwang",
    "cross_cifnews",
    "cross_crunchbase",
    "cross_dny123",
    "cross_gd",
    "cross_mjzj",
    "cross_singlewindow",
    "cross_wto",
    "cs_scichina",
    "hist_behistorical",
    "ind_huxiu",
    "ind_iyiou",
    "law_fsyj",
    "mat_gsytb",
    "pharm_cjnc",
    "pharm_zgyxzz",
    "pharm_zyyl",
    "phil_ajs",
    "phil_ars",
    "phil_cssn",
    "phil_ethics",
    "phil_jp",
    "phil_pr",
    "phil_soc",
    "phil_whz",
    "phil_xsj",
    "phil_zxyj",
    "phys_inspire",
    "psy_nps",
    "psy_science",
    "pub_xdyfyx",
    "pub_zhyfyx",
    "real_jsb",
    "space_avic",
    "space_breakingdefense",
    "space_casc",
    "space_casic",
    "space_cmse",
    "space_cnsa",
    "space_dsti",
    "space_news",
    "space_norinco",
    "space_spacenews",
    "sport_bbc",
    "sport_cctv5",
    "sport_daily",
    "sport_dongqiudi",
    "sport_espn",
    "sport_gov",
    "sport_hupu",
    "sport_olympics",
    "sport_skysports",
    "sport_zhibo8",
    # M9.2：用户实测抓取失败清单（11-17）：反爬 403/412/503 或站点失效；能换通道的已换，换不了的 36 个删除
    "auto_autonews",
    "bio_cbes",
    "bio_europepmc",
    "bio_sciencedirect",
    "chem_hgxb",
    "cs_joc",
    "douban_group_hot",
    "energy_powertech",
    "energy_rew",
    "energy_sgcc",
    "energy_solar",
    "env_ecology",
    "hist_research",
    "hist_shorpy",
    "law_fxyj",
    "law_xzfx",
    "law_zwfx",
    "mat_pmse",
    "neuro_cjnd",
    "neuro_nsr",
    "nov_gutenberg",
    "nov_webnovel",
    "pharm_cnj",
    "phys_astro",
    "psy_progress",
    "psy_psychonomic",
    "real_crb",
    "real_jijiantong",
    "urban_lalavista",
    "urban_planning",
    "urban_upf",
    "vc_dealroom",
    "vc_itjuzi",
    "vc_lieyun",
    "vc_pencil",
    # V7.9.14：用户要求删除「产业赛道」分类及其下全部新闻源（国资委迁入宏观政策，不在此列）
    "ind_yicai",
    "ind_caixin",
    "ind_cnii",
    "ind_36kr",
    "ind_techcrunch",
    "ind_mit",
    "bmj",   # V8.0.0：The BMJ 英国医学杂志，用户点名删除
    # V8.0.2：用户点名删除的 5 个源
    "huxiu", "baicaio", "nov_goodnovel", "agent_apifox", "real_propertyweek",
)

# M8.2：整类下线的分类（源按分类一并清掉，老配置里的分类名也会被摘除）
DROP_CATS = ("国际新闻", "币圈", "医疗期刊", "航天军工", "消费品牌", "跨境出海", "体育赛事",
             "哲学社会刊", "产业赛道")   # V8.0.0：产业赛道整类下线（国资委迁入宏观政策）


def strip_drop_cats(cats):
    """剔掉已整类下线的分类名：老配置、旧客户端回传、云端同步都可能把它们带回来"""
    return [c for c in (cats or []) if str(c or "").strip() not in DROP_CATS]

# 默认源顺序：决定侧边栏、看板、总排行里各源出现的先后（未列出的保持原有相对顺序）
SOURCE_ORDER = [
    "weibo", "toutiao", "baidu", "qqnews", "bilibili", "zaobao",
    "netease", "thepaper", "zhihu", "huanqiu", "guancha", "ifeng", "github",
    "douyin", "ithome", "xiaohongshu",
]

DEFAULT_SOURCES = [s for s in DEFAULT_SOURCES if s["id"] not in REMOVED_SOURCES]
_SRC_RANK = {sid: i for i, sid in enumerate(SOURCE_ORDER)}
DEFAULT_SOURCES.sort(key=lambda s: _SRC_RANK.get(s["id"], len(SOURCE_ORDER)))


# ---------------------------------------------------------------- 期刊 / 开源（M5.0）
# 付费墙期刊站（NEJM/Lancet/JAMA/Nature 系/Elsevier 系）几乎都挡爬虫，
# 统一走 Crossref 公开元数据取「该刊最新发表的论文」；能拿到 RSS 的优先用 RSS（更及时）。
# M5.2/V6.9：带 mailto 进 Crossref「礼貌池」，云端批量抓 12 个期刊源的 429 明显缓解
CROSSREF_API = ("https://api.crossref.org/journals/%s/works"
                "?rows=20&sort=published&order=desc"
                "&select=title,DOI,published,published-online,issued"
                "&mailto=rebang-helper@outlook.com")

# (id, 显示名, ISSN, 分类)
_JOURNALS = [
    # ---- 国际医学顶刊 ----
    ("nejm", "NEJM 新英格兰医学杂志", "0028-4793", "医学顶刊"),
    ("lancet", "The Lancet 柳叶刀", "0140-6736", "医学顶刊"),
    ("jama", "JAMA 美国医学会杂志", "0098-7484", "医学顶刊"),
    # V7.9.14：「The BMJ 英国医学杂志」按用户要求删除
    ("natmed", "Nature Medicine 自然·医学", "1546-170X", "医学顶刊"),
    ("cellmed", "Cell Reports Medicine", "2666-3791", "医学顶刊"),
    ("annintmed", "Annals of Internal Medicine", "0003-4819", "医学顶刊"),
    ("circulation", "Circulation 循环", "0009-7322", "医学顶刊"),
    ("gastro", "Gastroenterology 胃肠病学", "0016-5085", "医学顶刊"),
    ("blood", "Blood 血液学", "0006-4971", "医学顶刊"),
    # ---- 运动科学（国际） ----
    ("ajsm", "AJSM 美国运动医学杂志", "0363-5465", "运动科学"),
    ("msse", "MSSE 运动医学与科学", "0195-9131", "运动科学"),
    ("sportsmed", "Sports Medicine 运动医学综述", "0112-1642", "运动科学"),
    ("jospt", "JOSPT 骨科与运动物理治疗", "1938-1344", "运动科学"),
    ("kssta", "KSSTA 膝外科学与运动创伤", "0942-2056", "运动科学"),
    ("cjsm", "CJSM 临床运动医学", "1050-642X", "运动科学"),
    ("jat", "J Athletic Training", "1062-6050", "运动科学"),
    # ---- 经济期刊（国际顶刊） ----
    ("aer", "AER 美国经济评论", "0002-8282", "经济期刊"),
    ("qje", "QJE 经济学季刊", "0033-5533", "经济期刊"),
    ("jpe", "JPE 政治经济学杂志", "0022-3808", "经济期刊"),
    ("econometrica", "Econometrica 计量经济学", "1468-0262", "经济期刊"),
    ("restud", "REStud 经济研究评论", "1467-937X", "经济期刊"),
    ("rfs", "RFS 金融研究评论", "1465-7368", "经济期刊"),
]
for _sid, _nm, _issn, _cat in _JOURNALS:
    DEFAULT_SOURCES.append(S(
        _sid, _nm, CROSSREF_API % _issn, "crossref", count=20,
        note="最新论文 · Crossref 元数据（付费墙站也能取到标题）", cat=_cat))

# RSS / API 类（实测可抓，默认开启）
DEFAULT_SOURCES += [
    S("pubmed", "PubMed 文献检索", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
      "?db=pubmed&term=%22journal+article%22%5Bpt%5D&retmax=20&sort=pub_date&retmode=json",
      "pubmed", count=20, note="PubMed 最新收录的期刊论文（E-utilities）", cat="医学顶刊"),

    S("bjsm_rss", "BJSM 英国运动医学杂志", CROSSREF_API % "0306-3674", "crossref",
      count=20, note="全球最权威运动医学刊 · 官网 RSS 已失效，改走 Crossref 元数据", cat="运动科学"),
    S("jsms", "JSMS 运动科学与医学", "https://rss.sciencedirect.com/publication/science/14402440",
      "rss", count=20, note="澳大利亚运动医学 · Elsevier RSS", cat="运动科学"),
    S("ptsport", "Physical Therapy in Sport",
      "https://rss.sciencedirect.com/publication/science/1466853X", "rss", count=20,
      note="Elsevier RSS", cat="运动科学"),
    S("carm", "中国康复医学杂志", "http://www.carm.org.cn/", "auto", count=20,
      note="国内康复医学核心刊（HTML 列表页）", cat="运动科学"),

    S("jfe", "JFE 金融经济学", "https://rss.sciencedirect.com/publication/science/0304405X",
      "rss", count=20, note="Elsevier RSS", cat="经济期刊"),
    S("jpube", "J Public Economics", "https://rss.sciencedirect.com/publication/science/00472727",
      "rss", count=20, note="Elsevier RSS", cat="经济期刊"),
    S("jde", "J Development Economics", "https://rss.sciencedirect.com/publication/science/03043878",
      "rss", count=20, note="Elsevier RSS", cat="经济期刊"),
    S("jie", "J International Economics", "https://rss.sciencedirect.com/publication/science/00221996",
      "rss", count=20, note="Elsevier RSS", cat="经济期刊"),
    S("mwm", "管理世界", "http://www.mwm.net.cn/", "auto", count=20,
      note="CSSCI 权威刊（HTML 列表页）", cat="经济期刊"),

    S("oschina_news", "开源中国·资讯", "https://www.oschina.net/news/rss", "rss", count=20,
      note="OSCHINA 官方资讯 RSS", search="https://www.oschina.net/search?scope=news&q={q}",
      cat="开源"),
    # M5.2/V6.9：SourceForge 官方博客被 Cloudflare 挡（云端 403），换成三个云端实测可通的替代
    S("github_blog", "GitHub Blog", "https://github.blog/feed/", "rss", count=15,
      note="GitHub 官方博客（云端实测 10 条）", search="", cat="开源"),
    S("lwn", "LWN 内核周报", "https://lwn.net/headlines/rss", "rss", count=15,
      note="LWN.net 头条（云端实测 15 条）", search="", cat="开源"),
    S("phoronix", "Phoronix 硬件开源", "https://www.phoronix.com/rss.php", "rss", count=20,
      note="Phoronix Linux 硬件/驱动资讯（云端实测 32 条）", search="", cat="开源"),
    S("gitea_blog", "Gitea 发布", "https://blog.gitea.io/index.xml", "rss", count=15,
      note="轻量自建 Git 平台版本动态", cat="开源"),
    S("atlassian_blog", "Atlassian（Bitbucket）动态", "https://blog.atlassian.com/rss/", "rss",
      count=15, note="Bitbucket 所属平台官方博客", cat="开源"),
]

# ---------------------------------------------------------------- 学术期刊官网（V7.9.3）
# 用户点名要的「经济期刊」补充源：不是热榜站，要的是当期目录 / 最新论文 / 通知公告里的
# 文章题目，按网站自己的发布时间排序（不按热度、不按点击）。
DEFAULT_SOURCES += [
    S("econ_iwep", "世界经济与政治研究所", "http://ejournaliwep.cssn.cn/", "iwep", count=20,
      note="最新文章（《世界经济》《国际经济评论》等），按发布时间排序", cat="经济期刊"),
    S("econ_erj", "经济研究", "https://api.ajcass.com/api/SiteWebApi/GetCurrentPeriod"
      "?JournalID=201803050001", "ajcass", count=20,
      note="当期目录（官方接口，含卷期与作者），按当期目录顺序", cat="经济期刊",
      search="https://erj.ajcass.com/"),
    S("econ_jryj", "金融研究", "http://www.jryj.org.cn/CN/1002-7246/current.shtml", "jryj", count=20,
      note="当期目录（含出版日期），只取文章题目", cat="经济期刊"),
    S("econ_cie", "中国工业经济", "https://ciejournal.ajcass.com/", "cie", count=20,
      note="当期目录（文章页），只取文章题目", cat="经济期刊"),
    S("econ_cfrn", "金融学术（CFRN）", "https://www.cfrn.com.cn/lw/index.html", "cfrn", count=20,
      note="「最新论文」栏目文章", cat="经济期刊"),
    S("econ_nopss", "哲学社会科学·通知公告",
      "http://www.nopss.gov.cn/GB/219469/index.html", "nopss", count=20,
      note="通知公告栏目，按发布时间倒序", cat="经济期刊"),
    S("econ_gjjryj", "国际金融研究", "http://www.gjjryj.org.cn/CN/home", "gjjryj", count=20,
      note="最新当期目录（含年/期），只取中文标题", cat="经济期刊"),
    # 以下 2 个站是纯前端 / JS 人机校验：改用本机浏览器内核渲染后取内容（V7.9.6）
    S("econ_las", "智能制造·智库报告",
      "https://portal.las.ac.cn/reportFront/reportForIndustry?serverId=45", "las", count=20,
      note="中科院文献情报中心智库报告（浏览器渲染抓取，按时间倒序）", cat="经济期刊"),
    S("econ_nstl", "全球产业统计（NSTL）", "https://nstl.facisp.cn/", "nstl", count=20,
      note="NSTL 产业统计·资源推荐（数据看板站，条目为馆藏报告）", cat="经济期刊"),
    S("econ_kd", "国家自然科学基金·成果检索（经济）",
      "https://kd.nsfc.cn/resultSearchList?s=%E7%BB%8F%E6%B5%8E", "kd",
      count=20, note="基金成果题录（走站点检索接口，按发表年份倒序）", cat="经济期刊"),
    S("econ_kd_re", "国家自然科学基金·成果检索（房地产）",
      "https://kd.nsfc.cn/resultSearchList?s=%E6%88%BF%E5%9C%B0%E4%BA%A7", "kd", count=20,
      note="基金成果题录（走站点检索接口，按发表年份倒序）", cat="经济期刊"),
    S("econ_sci_jr", "SciEngine 检索·金融",
      "https://www.sciengine.com/plat/search?queryField_a=%E9%87%91%E8%9E%8D", "sci", count=20,
      note="SciEngine 检索结果（走站点检索接口，按出版日期倒序）", cat="经济期刊"),
    S("econ_sci_jj", "SciEngine 检索·经济",
      "https://www.sciengine.com/plat/search?queryField_a=%E7%BB%8F%E6%B5%8E", "sci", count=20,
      note="SciEngine 检索结果（走站点检索接口，按出版日期倒序）", cat="经济期刊"),
    S("econ_sci_sy", "SciEngine 检索·商业",
      "https://www.sciengine.com/plat/search?queryField_a=%E5%95%86%E4%B8%9A", "sci", count=20,
      note="SciEngine 检索结果（走站点检索接口，按出版日期倒序）", cat="经济期刊"),
]

# ---------------------------------------------------------------- M9.2/V7.9.13：新增分类（M9.1 基础上清理失效源）
# 数据格式：(分类名, [(源id, 显示名, 官网地址, 解析器, 备注), ...])
# 解析器写 issn:XXXX-XXXX 的走 Crossref 公开元数据（付费墙期刊站也能取到最新论文标题）
_NEW_CATS = [
    ('宏观政策', [
        ('macro_gov', '中国政府网', 'https://www.gov.cn/', 'html', '国务院政策文件·解读·公报'),
        ('macro_ndrc', '国家发改委', 'https://www.ndrc.gov.cn/', 'html', '宏观政策·产业规划·价格'),
        ('macro_pbc', '中国人民银行', 'https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html',
         'pbc', '沟通交流·新闻栏目（新闻发布页题目）'),
        ('macro_mof', '财政部', 'http://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/',
         'mof', '新闻报道·政策发布'),
        ('macro_cctv', '央视新闻', 'https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/economy_zixun_1.jsonp',
         'cctv_econ', '经济频道·经济资讯'),
        ('macro_yicai', '第一财经', 'https://www.yicai.com/', 'html', '政策市场化解解读'),
        ('macro_caixin', '财新', 'https://economy.caixin.com/', 'caixin', '经济频道·头条与要闻'),
        # V7.9.14：国资委从「产业赛道」迁入宏观政策
        ('ind_sasac', '国资委', 'http://www.sasac.gov.cn/', 'html', '重大产业项目·产业链'),
    ]),
    ('前沿 AI', [
        ('ai_aiera', '新智元', 'https://aiera.com.cn/', 'html', '站点JS渲染·暂不可用', False),
        ('ai_infoq', 'InfoQ', 'https://www.infoq.cn/', 'html', 'AI 工程·技术落地'),
        ('ai_arxiv', 'arXiv', 'http://export.arxiv.org/rss/cs.AI', 'rss', '论文一手源头·官方RSS'),
        ('ai_openreview', 'OpenReview', 'https://openreview.net/', 'html', '论文评审与前沿讨论'),
        ('ai_openai', 'OpenAI Blog', 'https://openai.com/news/rss.xml', 'rss', '官方研究与产品更新·换官方 RSS'),
        ('ai_google', 'Google AI Blog', 'https://blog.google/rss/', 'rss', '谷歌 AI 前沿·官方RSS'),
        ('ai_venturebeat', 'VentureBeat', 'https://venturebeat.com/', 'html', 'AI 产业分析'),
        ('ai_verge', 'The Verge', 'https://www.theverge.com/', 'html', 'AI·产品动态'),
        ('ai_mit', 'MIT Tech Review', 'https://www.technologyreview.com/', 'html', '长期技术趋势'),
    ]),
    ('生物前沿', [
        ('bio_nature', 'Nature', 'https://www.nature.com/', 'issn:0028-0836', '生命科学顶刊'),
        ('bio_science', 'Science', 'https://www.science.org/', 'issn:0036-8075', '综合科学前沿'),
        ('bio_cell', 'Cell', 'https://www.cell.com/', 'issn:0092-8674', '细胞与生命突破'),
        ('bio_frontiers', 'Frontiers in Cell Dev Bio', 'https://api.crossref.org/journals/2296-634X/works?rows=20&sort=published&order=desc', 'crossref', '开放获取生物医学·CellDevBio'),
        ('bio_bmc', 'BMC Biology', 'https://api.crossref.org/journals/1741-7007/works?rows=20&sort=published&order=desc', 'crossref', '生命科学期刊·BMCBiology'),
        ('bio_geneng', 'GEN 生物技术', 'https://www.genengnews.com/', 'html', '站点屏蔽抓取·暂不可用', False),
        ('bio_stat', 'STAT News', 'https://www.statnews.com/', 'html', '医疗前沿深度'),
    ]),
    ('心理学刊', [
        ('psy_acta', '心理学报', 'https://journal.psych.ac.cn/xlxb/CN/0439-755X/home.shtml', 'html', '国内权威心理顶刊'),
        ('psy_frontiers', 'Frontiers in Psychology', 'https://api.crossref.org/journals/1664-1078/works?rows=20&sort=published&order=desc', 'crossref', '国际开放心理刊·CrossRef'),
        ('psy_review', 'Psychological Review', 'https://psycnet.apa.org/', 'issn:0033-295X', '经典综述刊'),
        ('psy_annual', 'Annual Review of Psychology', 'https://www.annualreviews.org/journal/psych', 'issn:0066-4308', '年度综述'),
        ('psy_jpsp', 'JPSP 人格与社会心理', 'https://www.apa.org/pubs/journals/psp', 'issn:0022-3514', '社会人格顶刊'),
        ('psy_bbs', 'Behavioral and Brain Sciences', 'https://www.cambridge.org/core/journals/behavioral-and-brain-sciences', 'issn:0140-525X', '心理与神经跨学科'),
    ]),
    ('历史深度', [
        ('hist_qinghistory', '中华文史网', 'https://www.qinghistory.cn/', 'html', '权威历史综合站'),
        ('hist_nlc', '国家图书馆', 'https://www.nlc.cn/web/dsb_zx/gtxw/index.shtml', 'html', '国图新闻'),
        ('hist_modern', '近代史研究', 'http://jds.cass.cn/', 'html', '近代史权威刊'),
        ('hist_natgeo', 'National Geographic History', 'https://www.nationalgeographic.com/history', 'html', '世界史深度'),
        ('hist_historynet', 'HistoryNet', 'https://www.historynet.com/', 'html', '历史专题'),
        ('hist_fordham', 'Fordham 史料库', 'https://sourcebooks.fordham.edu/', 'html', '互联网历史史料库'),
        ('hist_channel', 'Smithsonian 历史', 'https://www.smithsonianmag.com/rss/latest_articles/', 'rss', '历史专题报道·SmithsonianRSS'),
    ]),
    ('楼市基建', [
        ('real_mohurd', '住建部', 'https://www.mohurd.gov.cn/', 'html', '房地产·基建顶层政策'),
        ('real_mohurdic', '住建部信息中心', 'https://www.mohurdic.org.cn/', 'html', '住建·城市基建'),
        ('real_fangchan', '中房网', 'http://www.fangchan.com/news/', 'fangchan', '中国房协·新闻资讯'),
        ('real_leju', '乐居财经', 'https://www.leju.com/', 'html', '楼市·房企动态'),
        ('real_guandian', '观点地产网', 'https://www.guandian.cn/', 'html', '地产与基建观点'),
        ('real_constructiondive', 'Construction Dive', 'https://www.constructiondive.com/feeds/news/', 'rss', '海外基建资讯·换 RSS'),
    ]),
    ('能源电力', [
        ('energy_nea', '国家能源局', 'https://www.nea.gov.cn/', 'html', '能源顶层政策与统计'),
        ('energy_cpnn', '中国能源新闻网', 'https://cpnn.com.cn/', 'html', '能源行业权威媒体'),
        ('energy_cp', '中国电力报', 'https://www.cpnn.com.cn/', 'html', '电力专业资讯'),
        ('energy_cnenergynews', '中国能源网', 'https://www.cnenergynews.cn/', 'html', '能源产业动态'),
        ('energy_nea_cn', '中国核能行业协会', 'https://www.china-nea.cn/', 'html', '核能前沿'),
        ('energy_global', 'Energy Global', 'https://www.energyglobal.com/news/', 'html', '全球能源产业·News'),
    ]),
    ('创业投融资', [
        ('vc_pedaily', '投资界', 'https://www.pedaily.cn/', 'html', '线下创投门户'),
        ('vc_chinaventure', '投中网', 'https://www.chinaventure.com.cn/', 'html', '创投深度与数据'),
        ('vc_cyzone', '创业邦', 'https://www.cyzone.cn/', 'html', '早期融资'),
        ('vc_news_cyzone', '新芽', 'https://news.cyzone.cn/', 'html', '投融资讯'),
        ('vc_crunchbase', 'TechCrunch 融资', 'https://techcrunch.com/feed/', 'rss', '海外融资·TechCrunchRSS'),
        ('vc_vnext', '科技成长路线图', 'https://www.v-next.cn/', 'html', '站点JS渲染·暂不可用', False),
    ]),
    ('学术顶刊', [
        ('acad_nature', 'Nature', 'https://www.nature.com/', 'issn:0028-0836', '综合科学顶刊'),
        ('acad_science', 'Science', 'https://www.science.org/', 'issn:0036-8075', '综合科学顶刊'),
        ('acad_cell', 'Cell', 'https://www.cell.com/', 'issn:0092-8674', '生命科学顶刊'),
        ('acad_lancet', 'The Lancet', 'https://www.thelancet.com/', 'issn:0140-6736', '医学顶刊'),
        ('acad_jama', 'JAMA', 'https://jamanetwork.com/', 'issn:0098-7484', '医学顶刊'),
        ('acad_pnas', 'PNAS', 'https://www.pnas.org/', 'issn:0027-8424', '美国国家科学院院刊'),
        ('acad_advmat', 'Advanced Materials', 'https://onlinelibrary.wiley.com/journal/15214095', 'issn:0935-9648', '材料顶刊'),
        ('acad_acs_nano', 'ACS Nano', 'https://pubs.acs.org/journal/ancac3', 'issn:1936-0851', '纳米材料'),
        ('acad_qje', 'QJE 经济学季刊', 'https://academic.oup.com/qje', 'issn:0033-5533', '经济学顶刊'),
        ('acad_psychrev', 'Psychological Review', 'https://psycnet.apa.org/', 'issn:0033-295X', '心理学顶刊'),
    ]),
    ('汽车产业', [
        ('auto_caam', '中国汽车工业协会', 'http://www.caam.org.cn/hyzx', 'html', '行业动态'),
        ('auto_cnautonews', '中国汽车报网', 'http://www.cnautonews.com/', 'html', '站点改版·暂不可用', False),
        ('auto_dongchedi', '懂车帝', 'https://www.dongchedi.com/', 'html', '新车·新能源资讯'),
        ('auto_autohome', '汽车之家', 'https://www.autohome.com.cn/', 'html', '汽车垂直门户'),
        ('auto_pcauto', '太平洋汽车网', 'https://www.pcauto.com.cn/', 'html', '汽车垂直资讯'),
        ('auto_gasgoo', '盖世汽车', 'https://www.gasgoo.com/robot/news/', 'html', '资讯·机器人与供应链'),
        ('auto_sae', '中国汽车工程学会', 'https://www.sae-china.org/', 'html', '站点JS渲染·暂不可用', False),
        ('auto_topspeed', 'TopSpeed', 'https://www.topspeed.com/', 'html', '站点关闭·暂不可用', False),
    ]),
    ('材料科学刊', [
        ('mat_natmat', 'Nature Materials', 'https://www.nature.com/nmat', 'issn:1476-1122', '材料顶刊'),
        ('mat_advmat', 'Advanced Materials', 'https://onlinelibrary.wiley.com/journal/15214095', 'issn:0935-9648', '材料顶刊'),
        ('mat_acs_nano', 'ACS Nano', 'https://pubs.acs.org/journal/ancac3', 'issn:1936-0851', '纳米材料'),
        ('mat_today', 'Materials Today', 'https://www.journals.elsevier.com/materials-today', 'issn:1369-7021', '材料综述'),
        ('mat_natnano', 'Nature Nanotechnology', 'https://www.nature.com/nnano', 'issn:1748-3387', '纳米科技'),
        ('mat_aem', 'Advanced Energy Materials', 'https://onlinelibrary.wiley.com/journal/16146840', 'issn:1614-6832', '能源材料'),
        ('mat_frontiers', 'Frontiers in Materials', 'https://www.frontiersin.org/journals/materials', 'issn:2296-8016', '开放获取材料刊'),
        ('mat_jmst', 'J. Mater. Sci. Technol.', 'https://www.journals.elsevier.com/journal-of-materials-science-and-technology', 'issn:1005-0302', '材料科学与技术'),
    ]),
    ('环境生态刊', [
        ('env_natsustain', 'Nature Sustainability', 'https://www.nature.com/natsustain', 'issn:2398-9629', '可持续顶刊'),
        ('env_est', 'Environmental Science & Technology', 'https://pubs.acs.org/journal/esthag', 'issn:0013-936X', '环境科学顶刊'),
        ('env_pollution', 'Environmental Pollution', 'https://www.journals.elsevier.com/environmental-pollution', 'issn:0269-7491', '环境污染'),
        ('env_natwater', 'Nature Water', 'https://www.nature.com/nwater', 'issn:2731-6084', '水资源顶刊'),
        ('env_nclimate', 'Nature Climate Change', 'https://www.nature.com/nclimate', 'issn:1758-678X', '气候变化'),
        ('env_waterres', 'Water Research', 'https://www.journals.elsevier.com/water-research', 'issn:0043-1354', '水环境顶刊'),
        ('env_hjkx', '环境科学学报', 'https://www.hjkx.ac.cn/', 'html', '国内环境科学核心刊'),
        ('env_ehp', 'Environmental Health Perspectives', 'https://ehp.niehs.nih.gov/', 'issn:0091-6765', '环境健康'),
        ('env_gcb', 'Global Change Biology', 'https://onlinelibrary.wiley.com/journal/13652486', 'issn:1354-1013', '全球变化生物学'),
    ]),
    ('公共卫生刊', [
        ('pub_lancet_ph', 'Lancet Public Health', 'https://www.thelancet.com/journals/lanpub', 'issn:2468-2667', '公共卫生顶刊'),
        ('pub_ajph', 'American Journal of Public Health', 'https://ajph.aphapublications.org/', 'issn:0090-0036', '美国公共卫生刊'),
        ('pub_bmc_ph', 'BMC Public Health', 'https://bmcpublichealth.biomedcentral.com/', 'issn:1471-2458', '开放获取公卫刊'),
        ('pub_epi', 'Epidemiology', 'https://journals.lww.com/epidemiology', 'issn:1044-3983', '流行病学'),
        ('pub_frontiers', 'Frontiers in Public Health', 'https://www.frontiersin.org/journals/public-health', 'issn:2296-2565', '开放获取公卫刊'),
        ('pub_jeph', 'J. Epidemiology and Global Health', 'https://www.journals.elsevier.com/journal-of-epidemiology-and-global-health', 'issn:2210-6006', '全球健康'),
        ('pub_prevmed', 'Preventive Medicine', 'https://www.journals.elsevier.com/preventive-medicine', 'issn:0091-7435', '预防医学'),
        ('pub_phr', 'Public Health Reports', 'https://www.publichealthreports.org/', 'issn:0033-3549', '公共卫生报告'),
    ]),
    ('神经科学刊', [
        ('neuro_natneuro', 'Nature Neuroscience', 'https://www.nature.com/nneuro', 'issn:1097-6256', '神经科学顶刊'),
        ('neuro_neuron', 'Neuron', 'https://www.cell.com/neuron', 'issn:0896-6273', '神经科学顶刊'),
        ('neuro_jn', 'Journal of Neuroscience', 'https://www.jneurosci.org/', 'issn:0270-6474', '神经科学会刊'),
        ('neuro_frontiers', 'Frontiers in Neuroscience', 'https://www.frontiersin.org/journals/neuroscience', 'issn:1662-453X', '开放获取神经刊'),
        ('neuro_nrn', 'Nature Reviews Neuroscience', 'https://www.nature.com/nrn', 'issn:1471-003X', '神经综述'),
        ('neuro_cercor', 'Cerebral Cortex', 'https://academic.oup.com/cercor', 'issn:1047-3211', '大脑皮层'),
        ('neuro_molbrain', 'Molecular Brain', 'https://molecularbrain.biomedcentral.com/', 'issn:1756-6606', '分子脑科学'),
        ('neuro_brain', 'Brain', 'https://academic.oup.com/brain', 'issn:0006-8950', '神经病学顶刊'),
    ]),
    ('物理天文刊', [
        ('phys_natphys', 'Nature Physics', 'https://www.nature.com/nphys', 'issn:1745-2473', '物理顶刊'),
        ('phys_prl', 'Physical Review Letters', 'https://journals.aps.org/prl', 'issn:0031-9007', '物理顶刊'),
        ('phys_wulixb', '物理学报', 'https://api.crossref.org/journals/1000-3290/works?rows=20&sort=published&order=desc', 'crossref', '国内物理核心刊·CrossRef'),
        ('phys_aa', 'Astronomy & Astrophysics', 'https://www.aanda.org/', 'issn:0004-6361', '天文学顶刊'),
        ('phys_nastron', 'Nature Astronomy', 'https://www.nature.com/nastron', 'issn:2397-3366', '天文学顶刊'),
        ('phys_prx', 'Physical Review X', 'https://journals.aps.org/prx', 'issn:2160-3308', '开放获取物理刊'),
        ('phys_mnras', 'MNRAS', 'https://academic.oup.com/mnras', 'issn:0035-8711', '皇家天文学会月报'),
        ('phys_cqg', 'Classical and Quantum Gravity', 'https://iopscience.iop.org/journal/02649381', 'issn:0264-9381', '引力与量子引力'),
    ]),
    ('化学化工刊', [
        ('chem_natchem', 'Nature Chemistry', 'https://www.nature.com/nchem', 'issn:1755-4330', '化学顶刊'),
        ('chem_jacs', 'JACS', 'https://pubs.acs.org/journal/jacsat', 'issn:0002-7863', '化学顶刊'),
        ('chem_anal', 'Analytical Chemistry', 'https://pubs.acs.org/journal/ancham', 'issn:0003-2700', '分析化学顶刊'),
        ('chem_chemrxiv', 'ChemRxiv', 'https://api.crossref.org/works?filter=prefix:10.26434&rows=20&sort=published&order=desc&select=title,DOI,published,published-online,issued,container-title', 'crossref', '化学预印本·换 Crossref 预印本'),
        ('chem_huaxue', '化学学报', 'http://www.ccspublishing.org.cn/news?abbreviation=kexuedongtai', 'html', '科学动态·文章列表'),
        ('chem_angew', 'Angewandte Chemie', 'https://onlinelibrary.wiley.com/journal/15213773', 'issn:1433-7851', '化学顶刊'),
        ('chem_iecr', 'Ind. & Eng. Chem. Research', 'https://pubs.acs.org/journal/iecred', 'issn:0888-5885', '工业化学工程'),
    ]),
    ('农林食品刊', [
        ('agri_natfood', 'Nature Food', 'https://www.nature.com/nfood', 'issn:2662-1355', '食品顶刊'),
        ('agri_jafc', 'J. Agricultural and Food Chemistry', 'https://pubs.acs.org/journal/jafcau', 'issn:0021-8561', '农食化学顶刊'),
        ('agri_foodchem', 'Food Chemistry', 'https://www.journals.elsevier.com/food-chemistry', 'issn:0308-8146', '食品化学顶刊'),
        ('agri_tcsae', '农业工程学报', 'http://www.tcsae.org/', 'html', '站点JS渲染·暂不可用', False),
        ('agri_jfp', 'Journal of Food Protection', 'https://www.jfoodprotection.org/', 'issn:0362-028X', '食品安全'),
        ('agri_jas', 'Journal of Agricultural Science', 'https://www.cambridge.org/core/journals/journal-of-agricultural-science', 'issn:0021-8596', '农业科学'),
        ('agri_aes', '农业环境科学学报', 'https://www.aes.org.cn/', 'html', '站点JS渲染·暂不可用', False),
        ('agri_foodfunction', 'Food & Function', 'https://pubs.rsc.org/en/journals/journalissues/c9fo', 'issn:2042-6496', '食品功能'),
    ]),
    ('计算机理论刊', [
        ('cs_acmcs', 'ACM Computing Surveys', 'https://dl.acm.org/journal/csur', 'issn:0360-0300', '计算机综述顶刊'),
        ('cs_arxiv', 'arXiv 计算机理论', 'http://export.arxiv.org/rss/cs.CC', 'rss', '预印本一手源头·官方RSS'),
        ('cs_ieee_it', 'IEEE Trans. Information Theory', 'https://ieeexplore.ieee.org/xpl/RecentIssue.jsp?punumber=18', 'issn:0018-9448', '信息论顶刊'),
        ('cs_toc', 'Theory of Computing', 'https://api.crossref.org/journals/1557-3958/works?rows=20&sort=published&order=desc', 'crossref', '计算理论开放刊·CrossRef'),
        ('cs_jacm', 'Journal of the ACM', 'https://dl.acm.org/journal/jacm', 'issn:0004-5411', '计算机顶刊'),
        ('cs_jos', '软件学报', 'https://www.jos.org.cn/', 'html', '国内软件顶刊'),
        ('cs_siam', 'SIAM Journal on Computing', 'https://epubs.siam.org/journal/sico', 'issn:0097-5397', '计算理论顶刊'),
        ('cs_cc', 'Computational Complexity', 'https://link.springer.com/journal/37', 'issn:1016-3328', '计算复杂性'),
    ]),
    ('法学与治理刊', [
        ('law_hlr', 'Harvard Law Review', 'https://harvardlawreview.org/', 'issn:0017-811X', '法学顶刊'),
        ('law_zgfx', '中国法学', 'https://zgfx.cbpt.cnki.net/', 'html', '站点JS渲染·暂不可用', False),
        ('law_ejil', 'European Journal of International Law', 'https://ejil.org/', 'issn:0938-5428', '国际法顶刊'),
        ('law_jilp', 'J. International Law and Politics', 'https://www.jilp.org/', 'html', '站点无列表·暂不可用', False),
        ('law_ajil', 'American Journal of International Law', 'https://www.asil.org/ajil', 'issn:0002-9300', '国际法顶刊'),
        ('law_icon', 'I·CON 国际宪法学杂志', 'https://www.ijcon.org/', 'issn:1474-2640', '宪法学'),
    ]),
    ('城市规划刊', [
        ('urban_us', 'Urban Studies', 'https://journals.sagepub.com/home/usj', 'issn:0042-0980', '城市研究顶刊'),
        ('urban_lup', 'Landscape and Urban Planning', 'https://www.journals.elsevier.com/landscape-and-urban-planning', 'issn:0169-2046', '景观与城市规划'),
        ('urban_cities', 'Cities', 'https://www.journals.elsevier.com/cities', 'issn:0264-2751', '城市研究'),
        ('urban_planners', '规划师', 'http://www.planners.com.cn/magazine.html', 'planners',
         '最新一期·杂志目录导读'),
        ('urban_jupd', 'J. Urban Planning and Development', 'https://ascelibrary.org/journal/jupddm', 'issn:0733-9488', '城市规划与开发'),
        ('urban_mur', '现代城市研究', 'http://www.mur.cn/Columns/list120.html', 'mur',
         '检索浏览·土地与房地产'),
        ('urban_ijurr', 'IJURR', 'https://onlinelibrary.wiley.com/journal/14682427', 'issn:0309-1317', '城市与区域研究'),
    ]),
    ('药学与药理刊', [
        ('pharm_natmed', 'Nature Medicine', 'https://www.nature.com/nmed', 'issn:1078-8956', '医学顶刊'),
        ('pharm_pharmrev', 'Pharmacological Reviews', 'https://pharmrev.aspetjournals.org/', 'issn:0031-6997', '药理综述顶刊'),
        ('pharm_jmc', 'Journal of Medicinal Chemistry', 'https://pubs.acs.org/journal/jmed', 'issn:0022-2623', '药物化学顶刊'),
        ('pharm_jpa', 'Journal of Pharmaceutical Analysis', 'https://www.journals.elsevier.com/journal-of-pharmaceutical-analysis', 'issn:2095-1779', '药物分析'),
        ('pharm_jco', 'Journal of Clinical Oncology', 'https://ascopubs.org/journal/jco', 'issn:0732-183X', '肿瘤临床顶刊'),
        ('pharm_nrco', 'Nature Reviews Clinical Oncology', 'https://www.nature.com/nrclinonc', 'issn:1759-4774', '肿瘤综述顶刊'),
    ]),
]



# 隐藏分类：侧边栏不显示、源默认关闭不抓取（用户 11-17 指定）
HIDDEN_CATS = ('前沿 AI', '生物前沿', '心理学刊', '历史深度', '能源电力', '创业投融资', '学术顶刊',
               '汽车产业', '材料科学刊', '公共卫生刊', '神经科学刊', '物理天文刊', '化学化工刊',
               '农林食品刊', '计算机理论刊', '法学与治理刊', '药学与药理刊')   # V8.0.0：共 17 个

_NEW_SRC_IDS = set()
for _c, _rows in _NEW_CATS:
    if _c not in CATS:
        CATS.append(_c)
    for _row in _rows:
        _sid, _nm, _u, _p, _note = _row[:5]
        _off = len(_row) > 5 and _row[5] is False   # V8.1：第 6 位 False = 源本身不可用，强制关闭
        if _sid in _NEW_SRC_IDS or any(x['id'] == _sid for x in DEFAULT_SOURCES):
            continue
        _NEW_SRC_IDS.add(_sid)
        if _p.startswith('issn:'):
            _u, _p = CROSSREF_API % _p[5:], 'crossref'
        _hid = _c in HIDDEN_CATS      # 隐藏分类：不显示也不抓取
        if _hid:
            _note = _note + '·分类已隐藏，默认关闭'
        DEFAULT_SOURCES.append(S(_sid, _nm, _u, _p, count=20,
                                 enabled=(not _hid) and (not _off),
                                 note=_note, cat=_c))
# M5.6/V7.3：官方下架的源（用户点名去除）；任何旧配置里残留的都会在加载时被清掉
_REMOVED_SRC = frozenset(["smzdm", "xueqiu", "cls", "cls_news", "iqiyi", "youpin", "360hot",
                          "erj", "jryj", "cie",          # V7.9.3：这三个已换成 econ_* 专用解析源
                          # M8.1：用户要求三端移除
                          "zhihudaily", "36kr", "kr36_hot", "thepaper_rss",
                          "douban_group", "douban_movie"])
# M8.4：原来「抓不到但挂着」的那批源已被用户点名删除（见 REMOVED_SOURCES），
# 清单清空；保留这个集合给以后需要强制停用的源用。
_FORCE_OFF = frozenset()


DEFAULT_SETTINGS = {
    "view": "image",         # image / text
    "theme": "light",        # light / dark
    "perSource": 20,         # 全局每源条数（源可单独覆盖）
    "autoRefresh": 10,       # 分钟，0=关闭
    "fontFamily": "",        # 空=系统默认
    "fontScale": 1.0,
    "bgImage": "",           # 背景图（本地文件路径或 data URL）
    "bgOpacity": 0.14,
    "closeAction": "tray",   # tray=关闭退到托盘, exit=直接退出
    "ttsRate": 1.25,
    "sortAgg": "heat",
    "openMode": "app",
    # V8.0.6：浏览器选择——daily=日常浏览器（正常 profile，插件/书签全在，推荐）
    # sys=系统默认 / edge / chrome / custom（extBrowserPath）/ app=独立隔离小窗口（旧行为）
    "extBrowser": "daily",
    "extBrowserPath": "",    # 自定义浏览器 exe 完整路径
    "showOnlyOk": True,      # 仅显示抓取成功的源
    "startupCheck": True,    # 启动时强制刷新并自动隐藏失败源
    "accent": "",            # 主题配色：空=经典红 / blue / green / violet / cyan / rose / amber
    "translate": "off",      # V7.9.14：翻译默认关闭
    "region": "",            # 地区新闻：省份或「省份 城市」，空=关闭
    "boardCount": 15,        # 总览看板每源条数
    "boardCols": 4,          # 看板列数：0=自动，1~12
    "boardRows": 2,          # 看板行数：1~6
    "orderVer": 0,           # 源默认顺序迁移标记（内部用）
    "srcLocked": False,      # 锁定新闻源：开关不可改，失败源不自动隐藏
    "cats": list(CATS),      # 新闻源分类（用户可自行增删改）
    "hideCats": list(HIDDEN_CATS),  # 侧边栏隐藏的分类（源保留但关闭不抓取）
    "unhideCats": [],        # 用户手动「恢复显示」过的分类，后端不再自动压回 hideCats
    "srcDeleted": [],        # 用户主动删掉的内置源 id：版本升级不再自动补回
    "cookies": {},           # M5.8/V7.5：用户自供 Cookie（{"域名后缀": "k=v; ..."}，仅桌面注入）
    "notifySec": 0,          # 抓取失败通知条：0=不通知（默认关闭），可选 3~20 秒
    "titleTrans": False,     # 英文标题翻译行默认关闭（可在设置里开启）
    # V7.9.3：推广分类页（推荐页）——展示形式与文案都可定制
    "adStyle": "card",       # card 大卡片 / banner 横幅 / row 列表行
    "adWarm": "你的支持，是我一直更新下去的最大动力 ❤",
    "adWarmSub": "如果这里的内容帮到了你，也欢迎把这块位置留给值得被看见的产品。",
    "adTitle": "作者其他软件 · 美拍姿势库（安卓 APK）",
    "adDesc": "1700+ 宫格图片，拒绝拍照尴尬，记录每一份美好。",
    "adUrl": "https://pan.baidu.com/s/5gZ0hq4QQPHVSmFj_XYwnnA",
    "adContact": "Q&V：460275097",
    # V8.0.5：题目缓存（AI 数据源）——每天一个文件夹，JSONL 逐行落盘，可被 AI/智能体直接读取分析
    "titlesDir": "",         # 题目缓存根目录，空=默认 data_dir()/titles
    "titlesKeep": 0,         # 历史保留天数：0=永久保留，可选 30/90/365
}


# ---------------------------------------------------------------- 腾讯云 CloudBase（M8.0 账号与云同步）
# 纯标准库 HTTP 客户端：邮箱注册/登录/验证码/找回密码 + 配置 JSON 云同步
# 接口文档：https://docs.cloudbase.net/http-api/auth/*
CB_ENV = "quickest-ocelot-d9fti0mr03ae447f"
CB_BASE = "https://%s.api.tcloudbasegateway.com" % CB_ENV
# 匿名访问（反馈提交/公开列表）用的 publishable key（公开性质，可随客户端分发）
CB_PUBKEY = (
    "eyJhbGciOiJSUzI1NiIsImtpZCI6IjNhNDAwMWE4LTEzMWItNDM4My1iZDY1LTQ2YmQ5OGM3N2UwZCJ9."
    "eyJpc3MiOiJodHRwczovL3F1aWNrZXN0LW9jZWxvdC1kOWZ0aTBtcjAzYWU0NDdmLmFwLXNoYW5naGFpLnRjYi1hcGkudGVuY2VudGNsb3VkYXBpLmNvbSIsInN1YiI6ImFub24iLCJhdWQiOiJxdWlja2VzdC1vY2Vsb3QtZDlmdGkwbXIwM2FlNDQ3ZiIsImV4cCI6NDA5ODEzODU5NSwiaWF0IjoxNzk0NDU1Mzk1LCJub25jZSI6ImxZQ2h3NTREU3BlRXJjTUthd1BMdEEiLCJhdF9oYXNoIjoibFlDaHc1NERTcGVFcmNNS2F3UEx0QSIsIm5hbWUiOiJBbm9ueW1vdXMiLCJzY29wZSI6ImFub255bW91cyIsInByb2plY3RfaWQiOiJxdWlja2VzdC1vY2Vsb3QtZDlmdGkwbXIwM2FlNDQ3ZiIsIm1ldGEiOnsicGxhdGZvcm0iOiJQdWJsaXNoYWJsZUtleSJ9LCJyb2xlIjoiYW5vbiIsImlzX2Fub255bW91cyI6dHJ1ZSwiYXBwX21ldGFkYXRhIjp7InByb3ZpZGVyIjoiYW5vbnltb3VzIiwicHJvdmlkZXJzIjpbImFub255bW91cyJdfSwidXNlcl9tZXRhZGF0YSI6eyJuYW1lIjoiQW5vbnltb3VzIn0sInVzZXJfdHlwZSI6IiIsImNsaWVudF90eXBlIjoiY2xpZW50X3VzZXIiLCJpc19zeXN0ZW1fYWRtaW4iOmZhbHNlfQ."
    "szuraJYjk8--IbESgWZLWM-GcmzOEhgs6MY8VH0MKnTptz8vACdygmc-20b-j52aQk9ptPyZTHIUK7eR0tO1hQcEWzWi46pA0bYUMnz_RYkykTuf5_rrgK1GeU7ybkoiTApmBB4L59hYkARQVjEetfqV_naajJQXCIpIsp3Ug8vVmt0JKj1WW7YsXIhd77tns19g-VFsHX2st7d_cF15e7Xg1G9CVz4-j7dUySOiahZe2gvKN014FE5KIciOaPBsWljm_2PUvoAl-ex6-elX5why5o7i47rPGlKNAmGjr2ulURjPxPqDyIlvdzODq79YVkuu5HtglipMJkc6tp2ncg"
)
AUTH_PATH = os.path.join(data_dir(), "热榜助手账号.json")
FEEDBACK_ADMIN_EMAIL = "460275097@qq.com"   # 管理员：登录此邮箱可审核建议/反馈、改推广位
# V7.9.4：推广位（推荐页广告）只允许作者改。这些字段存服务端，所有客户端只读。
AD_KEYS = ("adStyle", "adWarm", "adWarmSub", "adTitle", "adDesc", "adUrl", "adContact")


_ADMIN_CACHE = {"ts": 0.0, "ok": False}


def is_admin():
    """当前进程是否处于「作者本人已登录」状态。
    必须是有效登录态（token 可用或能续期），不能只看残留的用户信息，
    否则作者登录过一次后服务端会一直以为是管理员，谁都能改推广位。"""
    now = time.time()
    if now - _ADMIN_CACHE["ts"] < 20:
        return bool(_ADMIN_CACHE["ok"])
    ok = False
    try:
        u = ACCOUNT.user or {}
        if (u.get("email") or "").strip().lower() == FEEDBACK_ADMIN_EMAIL.lower():
            if ACCOUNT.access_token and now < (ACCOUNT.expires_at or 0):
                ok = True
            else:
                ok = bool(ACCOUNT.resume())
    except Exception:
        ok = False
    _ADMIN_CACHE["ts"], _ADMIN_CACHE["ok"] = now, ok
    return ok


def ad_block():
    """推广位内容（作者在服务端设定，所有客户端拉取同一份）"""
    s = CFG.settings or {}
    return {k: s.get(k, DEFAULT_SETTINGS.get(k, "")) for k in AD_KEYS}


def _cb_headers(token=None, extra=None):
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": UA, "x-device-id": _device_id()}
    if token:
        h["Authorization"] = "Bearer " + token
    elif CB_PUBKEY:
        h["Authorization"] = "Bearer " + CB_PUBKEY   # 匿名角色（anon）
    if extra:
        h.update(extra)
    return h


def _cb_request(method, path, body=None, token=None, extra=None, timeout=15):
    """CloudBase HTTP API 统一封装。返回 (ok, data_dict)；data_dict 含 error 描述。"""
    url = CB_BASE + path
    data = json.dumps(body or {}).encode("utf-8") if method in ("POST", "PATCH", "PUT") else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=_cb_headers(token, extra))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            out = json.loads(raw.decode("utf-8") or "{}") if raw else {}
        return True, out
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            out = {}
        desc = out.get("error_description") or out.get("error") or ("HTTP %s" % e.code)
        return False, {"error": desc, "code": e.code, "raw": out}
    except Exception as e:
        return False, {"error": str(e) or e.__class__.__name__, "code": 0}


def _device_id():
    """设备码：识别设备，让不同电脑/手机的本地设置互不影响。"""
    d = data_dir()
    p = os.path.join(d, "设备码.txt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            did = f.read().strip()
        if did:
            return did
    except Exception:
        pass
    did = "dev_" + hashlib.md5((socket.gethostname() + str(time.time())).encode()).hexdigest()[:20]
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(did)
    except Exception:
        pass
    return did


class Account:
    """登录态与云端配置同步。凭证存本地 auth 文件，重启自动续期（记住登录状态）。"""

    def __init__(self):
        self.user = None          # {sub, email, nickname}
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = 0
        self._load()

    # ---- 凭证持久化 ----
    def _load(self):
        try:
            with open(AUTH_PATH, "r", encoding="utf-8") as f:
                d = json.loads(f.read())
            self.user = d.get("user")
            self.access_token = d.get("access_token") or ""
            self.refresh_token = d.get("refresh_token") or ""
            self.expires_at = d.get("expires_at") or 0
        except Exception:
            pass

    def _save(self):
        try:
            with open(AUTH_PATH, "w", encoding="utf-8") as f:
                json.dump({"user": self.user, "access_token": self.access_token,
                           "refresh_token": self.refresh_token,
                           "expires_at": self.expires_at}, f, ensure_ascii=False)
        except Exception:
            pass

    def _clear(self):
        self.user, self.access_token, self.refresh_token, self.expires_at = None, "", "", 0
        try:
            os.remove(AUTH_PATH)
        except Exception:
            pass

    def _apply_token(self, r):
        """登录/注册/刷新响应 → 保存凭证。"""
        self.access_token = r.get("access_token") or ""
        self.refresh_token = r.get("refresh_token") or self.refresh_token
        self.expires_at = time.time() + int(r.get("expires_in") or 7200) - 120
        self._save()

    # ---- 静默续期（下次打开软件自动登录） ----
    def resume(self):
        if not self.refresh_token:
            return False
        if self.access_token and time.time() < self.expires_at:
            return bool(self.user)
        # 尝试两种官方刷新端点（以实际生效者为准，见 /auth/v1/* 文档）
        ok, r = _cb_request("POST", "/auth/v1/token",
                            {"grant_type": "refresh_token", "refresh_token": self.refresh_token})
        if not ok or not r.get("access_token"):
            ok, r = _cb_request("POST", "/auth/v1/refresh_access_token",
                                {"refresh_token": self.refresh_token})
        if ok and r.get("access_token"):
            self._apply_token(r)
            return bool(self.user)
        log("账号续期失败：%s" % (r.get("error") or "unknown"))
        return False

    # ---- 注册 / 登录 / 找回密码 ----
    def send_code(self, email, exist_only=False):
        """发送邮箱验证码。exist_only=True 用于找回密码（账号必须已注册）。"""
        ok, r = _cb_request("POST", "/auth/v1/verification",
                            {"email": email, "target": "USER" if exist_only else "ANY"})
        if not ok:
            return False, r.get("error") or "发送失败"
        return True, {"vid": r.get("verification_id"), "is_user": bool(r.get("is_user"))}

    def _verify_code(self, vid, code):
        ok, r = _cb_request("POST", "/auth/v1/verification/verify",
                            {"verification_id": vid, "verification_code": str(code)})
        if not ok or not r.get("verification_token"):
            return None, r.get("error") or "验证码错误"
        return r.get("verification_token"), None

    def register(self, email, code, vid, password, nickname):
        vt, err = self._verify_code(vid, code)
        if err:
            return False, err
        body = {"email": email, "verification_token": vt, "password": password}
        if nickname:
            body["username"] = nickname[:24]
        ok, r = _cb_request("POST", "/auth/v1/signup", body)
        if not ok:
            return False, r.get("error") or "注册失败"
        self._apply_token(r)
        self.user = {"sub": r.get("sub"), "email": email, "nickname": nickname}
        self._save()
        return True, {}

    def login(self, email, password):
        # 邮箱密码登录：官方 signin 的 username 位置接受邮箱
        ok, r = _cb_request("POST", "/auth/v1/signin", {"username": email, "password": password})
        if not ok or not r.get("access_token"):
            ok2, r2 = _cb_request("POST", "/auth/v1/signin", {"email": email, "password": password})
            if ok2 and r2.get("access_token"):
                ok, r = ok2, r2
        if not ok or not r.get("access_token"):
            return False, r.get("error") or "登录失败"
        self._apply_token(r)
        self.user = {"sub": r.get("sub"), "email": email}
        self._save()
        # 拉昵称（可选，失败不影响登录）
        ok2, me = _cb_request("GET", "/auth/v1/user/me", token=self.access_token)
        if ok2 and isinstance(me.get("user_metadata"), dict) and me["user_metadata"].get("name"):
            self.user["nickname"] = me["user_metadata"]["name"]
            self._save()
        return True, {}

    def logout(self):
        if self.access_token:
            _cb_request("POST", "/auth/v1/signout", {}, token=self.access_token)
        self._clear()

    def reset_password(self, email, code, vid, new_password):
        """找回密码：邮箱验证码校验通过 → 直接设置新密码。
        策略：① 先试专用重置端点；② 兜底用 verification_token 免密登录后改密。"""
        vt, err = self._verify_code(vid, code)
        if err:
            return False, err
        ok, r = _cb_request("POST", "/auth/v1/user/password/reset",
                            {"verification_token": vt, "new_password": new_password,
                             "confirm_password": new_password})
        if ok:
            return True, {}
        # 兜底：验证码免密登录（这本身也是官方登录方式），登录后修改密码
        ok2, r2 = _cb_request("POST", "/auth/v1/signin", {"verification_token": vt})
        if ok2 and r2.get("access_token"):
            self._apply_token(r2)
            self.user = {"sub": r2.get("sub"), "email": email}
            self._save()
            ok3, r3 = _cb_request("PATCH", "/auth/v1/user/password",
                                  {"new_password": new_password,
                                   "confirm_password": new_password},
                                  token=self.access_token)
            if ok3:
                return True, {}
            return False, r3.get("error") or "新密码设置失败"
        return False, r.get("error") or "重置失败"

    # ---- 配置云同步（绑定账号的配置 JSON） ----
    def _ensure_token(self):
        if not self.user:
            return False
        if self.access_token and time.time() < self.expires_at:
            return True
        return self.resume()

    def cloud_push(self, settings, favorites=None, custom_sources=None):
        """把该账号的设置习惯上传云端（user_config 表，RLS 按用户隔离）。
        节约云存储：自动同步只传 settings + favorites；只有手动「备份到云端」
        才额外上传新增（非内置）的新闻源。浏览记录只存本地，不上传。"""
        if not self._ensure_token():
            return False, "未登录或登录已过期"
        # V7.9.4：推广位不跟着账号云同步（作者在服务端唯一维护，避免账号配置把它覆盖掉）
        # V8.0.2：窗口形式（openMode）属于本机习惯，不进云端，避免换端登录后被改成浏览器模式
        # V8.0.6：extBrowser/extBrowserPath 同理（每台电脑装的浏览器不一样）
        settings = {k: v for k, v in (settings or {}).items()
                    if k not in AD_KEYS and k not in ("openMode", "extBrowser", "extBrowserPath")}
        payload = {"settings": settings, "favorites": favorites or [],
                   "ver": APP_VER, "ts": time.time(), "device": _device_id()}
        if custom_sources:
            payload["custom_sources"] = custom_sources
        body = {"user_id": self.user.get("sub"), "config": payload,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        # PostgREST 风格 upsert：user_id 主键冲突时更新
        ok, r = _cb_request("POST", "/v1/rdb/rest/user_config", body, token=self.access_token,
                            extra={"Prefer": "resolution=merge-duplicates"})
        if ok:
            return True, {}
        return False, r.get("error") or "上传失败"

    def cloud_pull(self):
        """拉取当前账号的云端配置 JSON；云端无配置时返回 (True, None)。"""
        if not self._ensure_token():
            return False, "未登录或登录已过期"
        uid = urllib.parse.quote(str(self.user.get("sub") or ""))
        ok, r = _cb_request("GET", "/v1/rdb/rest/user_config?user_id=eq.%s&select=config" % uid,
                            token=self.access_token)
        if not ok:
            return False, r.get("error") or "下载失败"
        rows = r if isinstance(r, list) else (r.get("data") or [])
        if not rows:
            return True, None
        cfg = rows[0].get("config") or {}
        return True, cfg


ACCOUNT = Account()


# 微信收款码（作者提供，内置默认；用户可在“关于”页替换或清除）
QR_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYn"
    "KSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgo"
    "KCgoKCgoKCj/wAARCAJ9ApYDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUF"
    "BAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVW"
    "V1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi"
    "4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAEC"
    "AxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVm"
    "Z2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq"
    "8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD6pooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooo"
    "oAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKK"
    "KKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAC"
    "iiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiig"
    "AooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooo"
    "oAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKK"
    "KKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAC"
    "iiigAooooAKKKKACiiigArwr9p/4l+Ifh3/wjX/CNy2yfb/tPnedCJM7PK249PvtXutfK37c/wDzJP8A2+/+29AHn/8Aw0t8Qv8A"
    "n403/wAAx/jR/wANL/EP/n50z/wEH+NeKUUAe1/8NL/EP/n50z/wEH+NH/DS/wAQ/wDn50z/AMBB/jXilFAHtf8Aw0v8Q/8An50z"
    "/wABB/jR/wANL/EP/n50z/wEH+NeKUUAe1/8NL/EP/n50z/wEH+NH/DS/wAQ/wDn50z/AMBB/jXilFAHtf8Aw0v8Q/8An50z/wAB"
    "B/jR/wANL/EP/n50z/wEH+NeKUUAe1/8NL/EP/n50z/wEH+NH/DS/wAQ/wDn50z/AMBB/jXilFAH3f8AszfELXfiFoWs3XiOS3ea"
    "1uUijMMQjG0rk59a9lr5r/Yh/wCRV8S/9fsf/ouvpSgD5Y+PPxs8X+DPiVqGiaJNZLYwxQsgltg7ZaMMefqa88/4aW+If/Pxpv8A"
    "4Br/AI1V/av/AOS2av8A9cbf/wBFLXj9AH6b+AtTuNa8DeHdVvipu77Tra5mKrgF3iVmwOwyTWzcMUgkcdVUkflXMfCX/klfg3/s"
    "DWf/AKJSulvP+PSb/cP8qAPhz/hpf4hf8/Om/wDgGP8AGj/hpf4hf8/Om/8AgGP8a8UooA9r/wCGl/iF/wA/Om/+AY/xo/4aX+IX"
    "/Pzpv/gGP8a8UooA9r/4aX+IX/Pzpv8A4Bj/ABo/4aX+IX/Pzpv/AIBj/GvFKKAPa/8Ahpf4hf8APzpv/gGP8aP+Gl/iF/z86b/4"
    "Bj/GvFKKAPa/+Gl/iF/z86b/AOAY/wAaP+Gl/iF/z86b/wCAY/xrxSigD2v/AIaX+IX/AD86b/4Bj/Gj/hpf4hf8/Om/+AY/xrxS"
    "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"
    "j4h/8+2m/wDgYP8ACvumigD5s/Zu+EHirwD46vNV8QxWaWkunSWymGcOd5kjYcemENfSdFFABRRRQB8yftD/AAa8WeOviCNX0CKz"
    "ez+xxQ5muAh3KWzx+IrzH/hmj4h/8++m/wDgYP8ACvumigD4W/4Zo+If/Pvpv/gYP8KP+GaPiH/z76b/AOBg/wAK+6aKAPhb/hmj"
    "4h/8++m/+Bg/wrd8B/s9+O9F8c+HdVvrfTxaWOo21zMUugSESVWbAxycA19mUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFA"
    "BRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRR"
    "QAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUU"
    "UUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAF"
    "FFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFA"
    "BRRRQAUUUUAFFFFABRRRQAUUUUAFFFFAH//ZVfv0CQAAAABKMJcL0H4OS1B4Yv2fLETZ"
)
DEFAULT_SETTINGS["qrImage"] = "data:image/jpeg;base64," + QR_B64


def deep_copy(o):
    return json.loads(json.dumps(o, ensure_ascii=False))


class Config:
    def __init__(self):
        self.is_new = False      # 本次启动前没有本地配置文件（全新安装）
        self.settings = deep_copy(DEFAULT_SETTINGS)
        self.sources = deep_copy(DEFAULT_SOURCES)
        self.favorites = []
        self.history = []
        self._lock = threading.Lock()
        self.load()

    def load(self):
        try:
            self.is_new = not os.path.exists(CONFIG_PATH)
        except Exception:
            self.is_new = False
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            self.save()
            return
        except Exception as e:
            log("读取配置失败: %r" % (e,))
            return
        raw_st = raw.get("settings") or {}
        for k, v in raw_st.items():
            if k in DEFAULT_SETTINGS:
                self.settings[k] = v
        if "qrImage" not in raw_st and DEFAULT_SETTINGS.get("qrImage"):
            # 旧配置升级：补上内置收款码（用户清除后不会恢复）
            self.settings["qrImage"] = DEFAULT_SETTINGS["qrImage"]
        # V7.9：通知条默认改为 10 秒；旧默认 4 秒不在可选项里 → 视为未设置，跟着升级
        try:
            _ns = int(self.settings.get("notifySec"))
        except Exception:
            _ns = 4
        if _ns not in (0, 3, 5, 8, 10, 15, 20):
            self.settings["notifySec"] = 10
            self.save()
        # V8.0.5：题目缓存配置清洗（目录长度限制 / 保留天数只认 0/30/90/365）
        _td = str(self.settings.get("titlesDir") or "").strip()
        if _td != str(self.settings.get("titlesDir") or ""):
            self.settings["titlesDir"] = _td
        try:
            _tk = int(self.settings.get("titlesKeep") or 0)
        except Exception:
            _tk = 0
        if _tk not in (0, 30, 90, 365):
            _tk = 0
        if _tk != self.settings.get("titlesKeep") or _td != self.settings.get("titlesDir"):
            self.settings["titlesKeep"] = _tk
            self.settings["titlesDir"] = _td
            self.save()
        if isinstance(raw.get("sources"), list) and raw["sources"]:
            self.sources = self._norm_sources(raw["sources"])
            # M8.1：下线源无条件从旧配置里删除（不依赖 presetVer 是否变化）
            _keep = [s for s in self.sources if s.get("id") not in REMOVED_SOURCES]
            if len(_keep) != len(self.sources):
                self.sources = _keep
                _gone = list(self.settings.get("srcDeleted") or [])
                for _sid in REMOVED_SOURCES:
                    if _sid not in _gone:
                        _gone.append(_sid)
                self.settings["srcDeleted"] = _gone
                self.save()
            if raw.get("presetVer") != PRESET_VER:
                # 版本升级：内置源的抓取规则随程序更新，用户的开关/顺序/条数保持不变
                self._refresh_builtins()
                self.save()
        else:
            self.sources = deep_copy(DEFAULT_SOURCES)
        self.favorites = raw.get("favorites") or []
        self.history = raw.get("history") or []
        # M8.2：一次性摘掉「国际新闻」「币圈」两个分类（含隐藏清单里的记录）
        # 每次加载都无条件剔除（旧客户端保存设置时会把它们塞回来，所以不能只做一次）
        _c = strip_drop_cats(self.settings.get("cats"))
        if _c != list(self.settings.get("cats") or []):
            self.settings["cats"] = _c
            self.save()
        if not self.settings.get("catDropVer"):
            self.settings["hideCats"] = strip_drop_cats(self.settings.get("hideCats"))
            self.settings["catDropVer"] = 1
            self.save()
        # V8.0.6：浏览器选择清洗（脏值回退 daily）
        if self.settings.get("extBrowser") not in ("daily", "sys", "edge", "chrome", "custom", "app"):
            self.settings["extBrowser"] = "daily"
            self.save()
        _ebp = str(self.settings.get("extBrowserPath") or "").strip()
        if _ebp != str(self.settings.get("extBrowserPath") or ""):
            self.settings["extBrowserPath"] = _ebp
            self.save()
        # M9.1：隐藏分类每次加载都强制生效——旧客户端/云同步把它们塞回来也照样压下去
        #       （侧边栏不显示 + 源关闭不抓取；用户想用某个隐藏分类需在设置里重新开启）
        _h = list(self.settings.get("hideCats") or [])
        _unh = [c for c in (self.settings.get("unhideCats") or []) if isinstance(c, str)]
        _m = [c for c in HIDDEN_CATS if c not in _h and c not in _unh]
        if _m:
            self.settings["hideCats"] = _h + _m
            self.save()
        # 按 hideCats 实际内容关闭源：用户点「恢复显示」把分类移出 hideCats 后，源不再被强关
        _hs = set(self.settings.get("hideCats") or []) - {"推广"}   # 推广分类不可隐藏
        if _hs != set(self.settings.get("hideCats") or []):
            self.settings["hideCats"] = [c for c in (self.settings.get("hideCats") or []) if c != "推广"]
            self.save()
        _hc = False
        for _s2 in self.sources:
            # V8.0.2：一个源可以有多个分类，只要还有一个分类是显示的就不关
            _sc = split_cat(_s2.get("cat"))
            if _sc and all(c in _hs for c in _sc) and _s2.get("enabled"):
                _s2["enabled"] = False
                _hc = True
        if _hc:
            self.save()
        # 老配置升级：源用到的分类若不在清单里（如新增「Agent」），自动纳入并持久化
        cats = list(self.settings.get("cats") or [])
        changed = False
        # V8.0.2：「GitHub今日上升」同时属于「开源」「资源」
        for s in self.sources:
            if s.get("id") == "github":
                _gc = split_cat(s.get("cat"))
                for c in ("开源", "资源"):
                    if c not in _gc:
                        _gc.append(c)
                _gj = ",".join(_gc)
                if _gj != (s.get("cat") or ""):
                    s["cat"] = _gj
                    changed = True
        for s in self.sources:
            for c in split_cat(s.get("cat")):
                if c and c not in cats:
                    cats.append(c)
                    changed = True
        cats = strip_drop_cats(cats)
        if changed or cats != list(self.settings.get("cats") or []):
            self.settings["cats"] = cats
            self.save()
        # V7.9.3：「推广」分类是推荐页，不挂在任何源上，老配置升级时单独补进来
        if "推广" not in (self.settings.get("cats") or []):
            c2 = list(self.settings.get("cats") or [])
            c2.append("推广")
            self.settings["cats"] = c2
            self.save()
        # M8.1：源默认顺序一次性迁移（按用户指定顺序；之后手动拖动仍保留）
        if not self.settings.get("orderVer"):
            _ord = {sid: i for i, sid in enumerate(SOURCE_ORDER)}
            self.sources.sort(key=lambda s: _ord.get(s.get("id"), len(SOURCE_ORDER)))
            self.settings["orderVer"] = 1
            self.save()

    @staticmethod
    def _norm_sources(srcs):
        # V8.0.0：国资委随「产业赛道」一起下线时迁入宏观政策，别跟着整类清掉
        srcs = [s for s in (srcs or [])]
        for _s in srcs:
            if isinstance(_s, dict) and _s.get("id") == "ind_sasac":
                _c = [c for c in split_cat(_s.get("cat")) if c not in DROP_CATS]
                if _c != split_cat(_s.get("cat")):
                    _s["cat"] = join_cat(_c + ["宏观政策"]) if _c else "宏观政策"
        # M8.2：整类下线的分类（国际新闻/币圈）连源带分类一起清掉
        srcs = [s for s in (srcs or [])
                if isinstance(s, dict)
                and s.get("id") not in _REMOVED_SRC
                and not any(c in DROP_CATS for c in split_cat(s.get("cat")))]
        out, used = [], set()
        for s in srcs:
            if not isinstance(s, dict) or not s.get("url"):
                continue
            sid = str(s.get("id") or "").strip() or "s%d" % int(time.time() * 1000)
            if sid in used:
                sid += "_1"
            used.add(sid)
            out.append({
                "id": sid, "name": str(s.get("name") or "未命名")[:24],
                "url": str(s["url"]).strip(), "parser": str(s.get("parser") or "auto"),
                "sel": s.get("sel") or {},
                # V8.1：校准元数据（校准地址/时间/样例哈希），随配置保存与云同步
                "calib": s.get("calib") or {},
                # V8.1：检索结果页选择器（校准向导对含 {q} 的地址校准后保存）
                "search_sel": s.get("search_sel") or {},
                "count": max(1, min(50, int(s.get("count") or 30))),
                "enabled": bool(s.get("enabled", True)),
                "note": str(s.get("note") or "")[:80],
                "search": str(s.get("search") or ""),
                # M5.8/V7.5：备用通道（主地址失败依次尝试），如 RSSHub 公共实例
                "alt": [str(a).strip()[:300] for a in (s.get("alt") or []) if str(a or "").strip()][:5],
                # V6.2：分类不再限定内置清单，用户可自定义任意分类
                # V8.0.2：支持一个源多个分类（逗号分隔，最多 4 个）
                "cat": join_cat(s.get("cat")),
            })
        return out

    @staticmethod
    def _merge_new(srcs):
        return srcs

    def _refresh_builtins(self):
        preset = {s["id"]: s for s in DEFAULT_SOURCES}
        have = {s["id"] for s in self.sources}
        gone = set(self.settings.get("srcDeleted") or [])
        # M8.1：下线源从用户配置里彻底删除，并记入 srcDeleted 防止被补回
        keep = [s for s in self.sources if s.get("id") not in REMOVED_SOURCES
                and not any(c in DROP_CATS for c in split_cat(s.get("cat")))]
        if len(keep) != len(self.sources):
            self.sources = keep
        for sid in REMOVED_SOURCES:
            gone.add(sid)
        self.settings["srcDeleted"] = sorted(gone)
        # M8.1：首次升级按新的默认顺序重排（用户之后手动拖动仍会保留）
        if not self.settings.get("orderVer"):
            self.sources.sort(key=lambda s: _SRC_RANK.get(s.get("id"), len(SOURCE_ORDER)))
            self.settings["orderVer"] = 1
        for s in self.sources:
            d = preset.get(s["id"])
            if not d:
                continue
            # V8.0.5：用户用「校」锁定的源（calib 有记录）不再被内置清单覆盖，
            # 否则每次版本升级都会把校准好的地址和提取规则重置回默认。
            _cb = s.get("calib") if isinstance(s.get("calib"), dict) else {}
            if _cb.get("url") or _cb.get("at") or (_cb.get("sample") or 0) > 1:
                if s["id"] in _FORCE_OFF:
                    s["enabled"] = False
                if not s.get("name"):
                    s["name"] = d["name"]
                continue
            s["url"], s["parser"] = d["url"], d["parser"]
            if s["id"] in _FORCE_OFF:      # M5.7/V7.4：需登录态的源，启动时强制关闭
                s["enabled"] = False
            s["sel"], s["note"] = deep_copy(d["sel"]), d["note"]
            s["search"] = d["search"]
            s["name"] = d["name"]
            # 分类统一由内置清单管理（用户要求：内置源跟着官方分类走）
            s["cat"] = d.get("cat") or "综合"
            if not s.get("count"):
                s["count"] = d["count"]
        # 版本升级时自动补上新增的内置源（用户删除过的不再补回）
        for d in DEFAULT_SOURCES:
            if d["id"] not in have and d["id"] not in gone:
                self.sources.append(deep_copy(d))

    def save(self):
        with self._lock:
            payload = {"version": 2, "app": APP_VER, "presetVer": PRESET_VER,
                       "settings": self.settings,
                       "sources": self.sources, "favorites": self.favorites,
                       "history": self.history}
            try:
                tmp = CONFIG_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                os.replace(tmp, CONFIG_PATH)
            except Exception as e:
                log("保存配置失败: %r" % (e,))

    def update(self, data):
        if isinstance(data.get("settings"), dict):
            for k, v in data["settings"].items():
                if k in DEFAULT_SETTINGS:
                    self.settings[k] = v
            self.settings["cats"] = strip_drop_cats(norm_cats(self.settings.get("cats")))
            # V7.9.14：「推广」是广告位分类，不允许隐藏/删除（只能调整排序）
            if "推广" not in self.settings["cats"]:
                self.settings["cats"].append("推广")
            _uh = self.settings.get("unhideCats")
            self.settings["unhideCats"] = [str(x)[:24] for x in _uh if isinstance(x, str)] if isinstance(_uh, list) else []
            _hh = self.settings.get("hideCats")
            self.settings["hideCats"] = ([str(x)[:24] for x in _hh if isinstance(x, str)]
                                         if isinstance(_hh, list) else [])
            self.settings["hideCats"] = [c for c in self.settings["hideCats"] if c != "推广"]
            # V8.0.1：旧客户端 / 旧本机副本提交上来的清单，不能把「版本默认隐藏」的分类弄丢
            _cats = set(self.settings.get("cats") or [])
            _lost = [c for c in HIDDEN_CATS
                     if c not in self.settings["hideCats"]
                     and c not in self.settings["unhideCats"]
                     and (not _cats or c in _cats)]
            if _lost:
                self.settings["hideCats"] = list(self.settings["hideCats"]) + _lost
            _ck = self.settings.get("cookies")          # M5.8/V7.5：Cookie 只收 str->str，最多 10 项
            if not isinstance(_ck, dict):
                _ck = {}
            self.settings["cookies"] = {str(k)[:64]: str(v)[:2000]
                                        for k, v in list(_ck.items())[:10] if str(v or "").strip()}
        if isinstance(data.get("sources"), list):
            old = {s["id"] for s in self.sources}
            builtin = {s["id"] for s in DEFAULT_SOURCES}
            self.sources = self._norm_sources(data["sources"])
            new_ids = {s["id"] for s in self.sources}
            gone = list(self.settings.get("srcDeleted") or [])
            for sid in (old & builtin) - new_ids:      # 用户主动删掉的内置源
                if sid not in gone:
                    gone.append(sid)
            for sid in list(gone):                      # 又被加回来就取消记录
                if sid in new_ids:
                    gone.remove(sid)
            self.settings["srcDeleted"] = gone
            cats = self.settings["cats"]                # 源用到的自定义分类自动纳入清单
            for s in self.sources:
                for c in split_cat(s.get("cat")):
                    if c not in cats and len(cats) < CAT_MAXN:
                        cats.append(c)
            self.settings["cats"] = strip_drop_cats(cats)
        self.save()
        return {"settings": self.settings, "sources": self.sources}

    # ---- 收藏 / 历史 ----
    def fav_toggle(self, item, action=None):
        key = (item.get("url") or "") + "|" + (item.get("title") or "")
        exists = next((i for i, f in enumerate(self.favorites)
                       if (f.get("url") or "") + "|" + (f.get("title") or "") == key), None)
        if action == "remove" or (action is None and exists is not None):
            if exists is not None:
                self.favorites.pop(exists)
        else:
            if exists is None:
                self.favorites.insert(0, {
                    "title": (item.get("title") or "")[:160],
                    "url": item.get("url") or "",
                    "img": item.get("img") or "",
                    "src": item.get("src") or item.get("srcName") or "",
                    "heatText": item.get("heatText") or "",
                    "ts": time.time(),
                })
        self.favorites = self.favorites[:600]
        self.save()
        return self.favorites

    def add_history(self, items):
        if not isinstance(items, list):
            return
        for it in items[:20]:
            if not isinstance(it, dict) or not it.get("title"):
                continue
            self.history.insert(0, {
                "title": it.get("title")[:160], "url": it.get("url") or "",
                "src": it.get("srcName") or it.get("src") or "", "ts": time.time()})
        # 去重（同标题只留最近一条）
        seen, out = set(), []
        for h in self.history:
            k = h["title"] + h["url"]
            if k in seen:
                continue
            seen.add(k)
            out.append(h)
        self.history = out[:400]
        self.save()


CFG = Config()

# ---------------------------------------------------------------- 简易 DOM

VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())


class Node:
    __slots__ = ("tag", "attrs", "parent", "children")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.children = []

    def get(self, key, default=""):
        return self.attrs.get(key, default) or default

    @property
    def cls(self):
        return (self.attrs.get("class") or "").lower()

    def has_cls(self, name):
        return name.lower() in self.cls.split()

    def text(self, limit=1000):
        buf = []
        self._collect(buf)
        return re.sub(r"\s+", " ", "".join(buf)).strip()[:limit]

    def _collect(self, buf):
        for c in self.children:
            if len(buf) > 400:
                return
            if isinstance(c, str):
                buf.append(c)
            elif c.tag not in ("script", "style", "noscript"):
                c._collect(buf)

    def walk(self):
        for c in self.children:
            if not isinstance(c, str):
                yield c
                for x in c.walk():
                    yield x

    def find_all(self, tag=None, cls=None):
        for n in self.walk():
            if tag and n.tag != tag:
                continue
            if cls and not n.has_cls(cls):
                continue
            yield n

    def first(self, tag=None, cls=None):
        for n in self.find_all(tag, cls):
            return n
        return None


def parse_html(text):
    # 这些标签在真实网页里经常省略结束标签，必须隐式闭合，否则整页会退化成一个巨型节点
    IMPLIED = {"li": ("li",), "p": ("p",), "tr": ("tr", "td", "th"),
               "td": ("td", "th"), "th": ("td", "th"),
               "dt": ("dt", "dd"), "dd": ("dt", "dd"), "option": ("option",)}

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.root = Node("#root")
            self.stack = [self.root]

        def _close_implied(self, tag):
            closes = IMPLIED.get(tag)
            if not closes:
                return
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].tag in closes:
                    del self.stack[i:]
                    break

        def handle_starttag(self, tag, attrs):
            self._close_implied(tag)
            n = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
            self.stack[-1].children.append(n)
            if tag not in VOID_TAGS:
                self.stack.append(n)

        def handle_startendtag(self, tag, attrs):
            n = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
            self.stack[-1].children.append(n)

        def handle_endtag(self, tag):
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].tag == tag:
                    del self.stack[i:]
                    break

        def handle_data(self, data):
            self.stack[-1].children.append(data)

    p = P()
    try:
        p.feed(text)
        p.close()
    except Exception:
        pass
    return p.root


# ---------------------------------------------------------------- 选择器

_SEL_RE = re.compile(r"^([a-zA-Z0-9*]+)?((?:\.[\w-]+)*)((?:\[[^\]]*\])*)$")
_ATTR_RE = re.compile(r"\[([\w-]+)(?:([~*^$|]?=)\s*[\"']?([^\"'\]]*)[\"']?)?\]")


def _sel_step(step):
    m = _SEL_RE.match(step.strip())
    if not m:
        return None
    tag = (m.group(1) or "").lower() or None
    classes = [c for c in (m.group(2) or "").split(".") if c]
    attrs = re.findall(r"\[([\w-]+)(?:\s*=\s*[\"']?([^\"'\]]*)[\"']?)?\]", m.group(3) or "")
    return tag, classes, attrs


def _match(node, step):
    tag, classes, attrs = step
    if tag and tag != "*" and node.tag != tag:
        return False
    if classes and not all(node.has_cls(c) for c in classes):
        return False
    for k, op, v in attrs:
        if k not in node.attrs:
            return False
        val = node.attrs.get(k) or ""
        if op in ("*=", "~="):        # 包含
            if v and v not in val:
                return False
        elif op in ("^=",):           # 前缀
            if v and not val.startswith(v):
                return False
        elif op in ("$=",):           # 后缀
            if v and not val.endswith(v):
                return False
        elif op in ("=", "|="):
            if v and v != val and not val.startswith(v + "-"):
                return False
        elif v and v not in val:
            return False
    return True


def select_all(root, path):
    steps = [s for s in re.split(r"\s*>\s*|\s+", (path or "").strip()) if s]
    parsed = []
    for s in steps:
        p = _sel_step(s)
        if not p:
            return []
        parsed.append(p)
    cur = [root]
    for step in parsed:
        nxt = []
        for n in cur:
            kids = [c for c in n.children if not isinstance(c, str) and _match(c, step)]
            if kids:
                nxt.extend(kids)
            else:
                nxt.extend([d for d in n.walk() if _match(d, step)])
        cur = nxt
        if not cur:
            break
    return cur


def select_one(root, path):
    r = select_all(root, path)
    return r[0] if r else None


def node_img(node):
    for im in node.find_all("img"):
        for k in ("src", "data-src", "data-original", "data-lazy-src", "data-echo"):
            v = im.get(k)
            if v and not v.startswith("data:") and "placeholder" not in v and "lazyload" not in v:
                return v
    return ""


# ---------------------------------------------------------------- 抓取

_OPENERS = {}


def _opener(referer=None, no_verify=False):
    key = (referer or "") + ("|nv" if no_verify else "")
    if key in _OPENERS:
        return _OPENERS[key]
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if no_verify:
        # 证书链不完整的站点（大量 .gov.cn / 期刊官网）：跳过证书校验重试
        try:
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        except Exception:
            pass
    op = urllib.request.build_opener(*handlers)
    op.addheaders = [
        ("User-Agent", UA),
        ("Accept", "text/html,application/json,application/xhtml+xml,*/*;q=0.8"),
        ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
        ("Accept-Encoding", "gzip, deflate"),
    ]
    if referer:
        op.addheaders.append(("Referer", referer))
    _OPENERS[key] = op
    return op




def _user_cookie_for(url):
    """M5.8/V7.5：用户自供 Cookie（仅桌面版）。settings.cookies = {"域名后缀": "k=v; ..."}，
    按域名后缀最长匹配注入到抓取请求。"""
    try:
        ck = CFG.settings.get("cookies")
    except Exception:
        return ""
    if not isinstance(ck, dict) or not ck:
        return ""
    host = (urllib.parse.urlsplit(url).netloc or "").lower()
    best = ""
    for dom, v in ck.items():
        d = str(dom or "").lower().lstrip(".").strip()
        if not d:
            continue
        if (host == d or host.endswith("." + d)) and len(d) > len(best):
            best = str(v or "").strip()
    return best

# M5.8/V7.5：域名级限速（礼貌抓取）——同一站点两次请求至少间隔 _DOM_GAP 秒
_DOM_LK = threading.Lock()
_DOM_LAST = {}
_DOM_GAP = 0.6


def _domain_throttle(url):
    """学 tophub：按域名排队限速，避免 Crossref/百度这类一并发就 429 的站点被限流。"""
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except Exception:
        return
    if not netloc:
        return
    while True:
        with _DOM_LK:
            now = time.time()
            wait = _DOM_GAP - (now - _DOM_LAST.get(netloc, 0))
            if wait <= 0:
                _DOM_LAST[netloc] = now
                return
        time.sleep(min(wait, 1.0))


def _is_cert_error(e):
    """V7.8：识别 SSL 证书类错误（证书链不完整 / 自签名），触发免校验重试"""
    while e is not None:
        if isinstance(e, ssl.SSLError) or type(e).__name__ in (
                "SSLCertVerificationError", "CertificateError"):
            return True
        reason = getattr(e, "reason", None)
        if reason is e:
            break
        e = reason
        if not isinstance(e, Exception):
            break
    return False


def fetch_bytes(url, referer=None, timeout=14):
    _domain_throttle(url)     # M5.8/V7.5：域名级限速
    req = urllib.request.Request(url)
    if referer:
        req.add_header("Referer", referer)
    _ck = _user_cookie_for(url)      # M5.8/V7.5：用户自供 Cookie（按域名后缀匹配）
    if _ck:
        req.add_header("Cookie", _ck)
    try:
        op = _opener(referer)
        with op.open(req, timeout=timeout) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            ctype = (r.headers.get("Content-Type") or "").lower()
            m = re.search(r"charset=([\w-]+)", ctype)
            charset = m.group(1) if m else None
    except Exception as e:
        if not _is_cert_error(e):
            raise
        _record_cert_issue(url, e)   # V7.8.1：记录证书问题站点，供「设置 → 证书修复」展示
        # V7.8：证书验证失败 → 免校验重试（救活 .gov.cn、期刊官网等自签/断链证书站点）
        op = _opener(referer, no_verify=True)
        with op.open(req, timeout=timeout) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            ctype = (r.headers.get("Content-Type") or "").lower()
            m = re.search(r"charset=([\w-]+)", ctype)
            charset = m.group(1) if m else None
    if enc == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    return raw, charset


def decode_bytes(raw, charset=None):
    if charset:
        try:
            return raw.decode(charset, errors="ignore")
        except Exception:
            pass
    m = re.search(rb'charset=["\']?\s*([\w-]{3,12})', raw[:3000], re.I)
    if m:
        try:
            return raw.decode(m.group(1).decode("ascii", "ignore"), errors="ignore")
        except Exception:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def http_text(url, referer=None, timeout=14):
    raw, cs = fetch_bytes(url, referer, timeout)
    return decode_bytes(raw, cs)


_MOBILE_UA = ("Mozilla/5.0 (Linux; Android 13; M2102J2SC) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")


def fetch_page_robust(url, timeout=12):
    """V8.0.6：校准/测试用韧性抓取——一次失败不放弃，按策略阶梯重试，
    并把每一步失败原因带回去（前端能显示「为什么失败」而不是笼统的网络错误）。
    阶梯：默认 UA → 免证书校验 → 手机 UA → https 降级 http（仅 TLS 类错误）。
    返回 (html, err)；html 为空串表示全部失败，err 是给人看的失败原因串。"""
    errs = []
    # ① 默认（带 Cookie/UA/gzip 的常规通道）
    try:
        return http_text(url, timeout=timeout), ""
    except Exception as e:
        errs.append(_net_err_desc(e))
    # ② 证书链问题（.gov.cn、期刊官网常见）：免校验重试
    try:
        op = _opener(None, no_verify=True)
        req = urllib.request.Request(url)
        with op.open(req, timeout=timeout) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
        return decode_bytes(raw), ""
    except Exception as e:
        errs.append("免证书校验: " + _net_err_desc(e))
    # ③ 手机 UA（部分站对桌面 UA 返回 403/空壳）
    try:
        return _get_html(url, extra={"User-Agent": _MOBILE_UA}, timeout=timeout), ""
    except Exception as e:
        errs.append("手机UA: " + _net_err_desc(e))
    # ④ https 打不通（TLS 握手失败/老加密套件）→ 试 http
    if url.lower().startswith("https://"):
        try:
            u2 = "http://" + url[len("https://"):]
            return http_text(u2, timeout=timeout), ""
        except Exception as e:
            errs.append("http降级: " + _net_err_desc(e))
    return "", "；".join(errs)


def _net_err_desc(e):
    """把网络异常翻译成人话（前端直接显示）"""
    s = str(e) or e.__class__.__name__
    low = s.lower()
    if isinstance(e, socket.timeout) or "timed out" in low or "timeout" in low:
        return "连接超时（网站响应太慢或被墙）"
    if "ssl" in low or "certificate" in low or "cert" in low:
        return "HTTPS 证书异常"
    if "getaddrinfo failed" in low or "name or service not known" in low:
        return "域名解析失败（地址拼错或站点已下线）"
    if "403" in s:
        return "网站拒绝访问（403，可能反爬）"
    if "404" in s:
        return "页面不存在（404）"
    if "reset" in low:
        return "连接被重置（网站或网络中断）"
    return e.__class__.__name__ + ": " + s[:80]


# ------------------------------------------------------------ 证书修复（V7.8.1）
# 抓取层已用「免校验重试」兜底，这里额外给用户一个可视化面板：
# 记录哪些站点证书链有问题、各自需要哪一级证书、下载/安装状态如何。

CERT_ISSUES = {}
CERT_PATH = os.path.join(data_dir(), "cert_fix.json")
CERT_DIR = os.path.join(data_dir(), "certs")


def _cert_load():
    global CERT_ISSUES
    try:
        with open(CERT_PATH, "r", encoding="utf-8") as f:
            CERT_ISSUES = json.load(f) or {}
    except Exception:
        CERT_ISSUES = {}


def _cert_save():
    try:
        with open(CERT_PATH, "w", encoding="utf-8") as f:
            json.dump(CERT_ISSUES, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("写 cert_fix.json 失败: %r" % (e,))


def _record_cert_issue(url, err):
    try:
        host = (urllib.parse.urlparse(url).netloc or "").split(":")[0].lower()
        if not host:
            return
        e = CERT_ISSUES.get(host) or {}
        e["err"] = str(err)[:200]
        e["ts"] = time.time()
        CERT_ISSUES[host] = e
        _cert_save()
    except Exception:
        pass


def _ps_b64(script):
    import base64 as _b
    return _b.b64encode(script.encode("utf-16-le")).decode("ascii")


def cert_chain_probe(host):
    """用 Windows 自身加密栈探测站点证书链（PowerShell SslStream + X509Chain），
    返回 {ok, chain:[{subject,issuer,notAfter,thumb,der,selfSigned}]} 或 {ok:False,err}。
    不做任何系统修改，只读。"""
    ps = r'''
$hn = '%HOST%'
try {
  $tcp = New-Object Net.Sockets.TcpClient($hn, 443)
  $ss = New-Object Net.Security.SslStream($tcp.GetStream(), $false, ({ $true }))
  $ss.AuthenticateAsClient($hn)
  $leaf = New-Object Security.Cryptography.X509Certificates.X509Certificate2($ss.RemoteCertificate)
  $ch = New-Object Security.Cryptography.X509Certificates.X509Chain
  $ch.ChainPolicy.VerificationFlags = [Security.Cryptography.X509Certificates.X509VerificationFlags]::AllowUnknownCertificateAuthority
  $ch.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
  [void]$ch.Build($leaf)
  $list = @()
  foreach ($el in $ch.ChainElements) {
    $c = $el.Certificate
    $list += @{ subject=$c.Subject; issuer=$c.Issuer; notAfter=$c.NotAfter.ToString('yyyy-MM-dd'); thumb=$c.Thumbprint; der=[Convert]::ToBase64String($c.RawData); selfSigned=($c.Subject -eq $c.Issuer) }
  }
  $ss.Dispose(); $tcp.Dispose()
  Write-Output (@{ ok=$true; chain=@($list) } | ConvertTo-Json -Depth 4 -Compress)
} catch {
  Write-Output (@{ ok=$false; err=$_.Exception.Message } | ConvertTo-Json -Depth 3 -Compress)
}
'''.replace("%HOST%", host)
    try:
        import subprocess
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-EncodedCommand", _ps_b64(ps)],
                           capture_output=True, timeout=30)
        out = r.stdout.decode("utf-8", errors="ignore").strip()
        if not out:
            return {"ok": False, "err": (r.stderr.decode("utf-8", errors="ignore")[:120] or "无输出")}
        d = json.loads(out)
        if d.get("ok") and isinstance(d.get("chain"), dict):   # 单元素链 ConvertTo-Json 会变对象
            d["chain"] = [d["chain"]]
        return d
    except Exception as e:
        return {"ok": False, "err": str(e)[:160]}


def cert_chain_install(host):
    """把探测到的证书链逐级导出为 .cer（记录下载状态），再用 certutil 装入当前用户的
    中级证书颁发机构（CA）存储，让浏览器等其它软件也能正常访问这些站点。"""
    import subprocess
    e = CERT_ISSUES.get(host)
    if not e or not e.get("chain"):
        return False, "请先点「检测证书链」"
    os.makedirs(CERT_DIR, exist_ok=True)
    installed, saved = 0, 0
    for i, c in enumerate(e["chain"]):
        if c.get("selfSigned"):
            c["saved"] = c.get("saved", False)
            continue   # 根证书由系统自带，不覆盖安装
        try:
            import base64 as _b
            der = _b.b64decode(c["der"])
            path = os.path.join(CERT_DIR, "%s_%d.cer" % (re.sub(r"[^\w.-]", "_", host), i))
            with open(path, "wb") as f:
                f.write(der)
            c["saved"] = True
            c["file"] = path
            saved += 1
        except Exception:
            c["saved"] = c.get("saved", False)
    for i, c in enumerate(e["chain"]):
        if c.get("selfSigned") or not c.get("file"):
            continue
        try:
            r = subprocess.run(["certutil", "-addstore", "-user", "CA", c["file"]],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                c["installed"] = True
                installed += 1
        except Exception:
            pass
    e["downloaded"] = saved > 0
    e["installedTs"] = time.time() if installed else e.get("installedTs", 0)
    _cert_save()
    need = sum(1 for c in e["chain"] if not c.get("selfSigned"))
    return True, "已下载 %d/%d 级证书，安装成功 %d 级（装入当前用户「中级证书颁发机构」）" % (
        saved, need, installed)


def cert_issues_payload():
    items = []
    for host, e in CERT_ISSUES.items():
        chain = [{k: c.get(k) for k in ("subject", "issuer", "notAfter", "selfSigned", "saved", "installed")}
                 for c in (e.get("chain") or [])]
        items.append({"host": host, "err": e.get("err", ""), "ts": e.get("ts", 0),
                      "downloaded": e.get("downloaded", False), "installedTs": e.get("installedTs", 0),
                      "chain": chain})
    items.sort(key=lambda x: -x["ts"])
    return items


_cert_load()   # 启动时恢复已记录的证书问题站点


def http_json(url, referer=None, timeout=14, data=None):
    if data is not None:
        req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with _opener(referer).open(req, timeout=timeout) as r:
            return json.loads(decode_bytes(r.read()))
    return json.loads(http_text(url, referer, timeout))


# ---------------------------------------------------------------- 工具

NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([万亿wWkK])?")


def to_heat(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    m = NUM_RE.search(str(s).replace(",", ""))
    if not m:
        return None
    v = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("万", "w"):
        v *= 10000
    elif unit == "亿":
        v *= 100000000
    elif unit == "k":
        v *= 1000
    return int(v)


def fmt_heat(n):
    if not n:
        return ""
    n = int(n)
    if n >= 100000000:
        return ("%.1f亿" % (n / 100000000.0)).replace(".0亿", "亿")
    if n >= 10000:
        return ("%.1f万" % (n / 10000.0)).replace(".0万", "万")
    return str(n)


def abs_url(base, href):
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    return urllib.parse.urljoin(base, href)


TITLE_KEYS = ["word", "title", "Title", "name", "name_cn", "query", "hot_word", "keyword",
              "index_title", "queryWord", "text", "desc"]
URL_KEYS = ["url", "Url", "URL", "link", "href", "mobil_url", "wapUrl", "rawUrl",
            "indexUrl", "short_link_v2", "share_url", "detail_url"]
HEAT_KEYS = ["hotScore", "hotValue", "HotValue", "hot", "heat", "score", "hot_num", "num",
             "view", "stat.view", "hotness", "popularity", "count", "read"]
IMG_KEYS = ["img", "pic", "image", "Image", "thumbnail", "cover", "picUrl", "pic_url",
            "thumb", "avatar", "imgsrc"]


def pick(d, keys):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, "", [], {}):
            return d[k]
    for k in keys:
        if "." in k and isinstance(d, dict):
            cur = d
            ok = True
            for part in k.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and cur not in (None, ""):
                return cur
    return None


def clean_text(s):
    if s is None:
        return ""
    if isinstance(s, dict):
        s = pick(s, IMG_KEYS) or pick(s, URL_KEYS) or ""
    if not isinstance(s, str):
        s = str(s)
    s = re.sub(r"<[^>]{1,200}>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
         .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", s).strip()


def img_url(u, referer=""):
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    if not u.startswith("http"):
        return ""
    q = "/img?u=" + urllib.parse.quote(u, safe="")
    if referer:
        q += "&r=" + urllib.parse.quote(referer, safe="")
    return q


def mk_item(title, url, heat=None, img="", extra="", base=""):
    title = clean_text(title)
    if not title:
        return None
    url = clean_text(url)
    img = clean_text(img)
    return {"title": title[:160], "url": abs_url(base, url) if base else url,
            "heat": int(heat) if heat else None,
            "img": img_url(img, base) if img else "",
            "extra": clean_text(extra)[:70]}


def finish(items, sort_by_heat=True):
    seen, out = set(), []
    for it in items:
        if not it:
            continue
        key = re.sub(r"[\s\W]+", "", it["title"])[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    if sort_by_heat and sum(1 for i in out if i.get("heat")) >= max(3, len(out) // 2):
        out.sort(key=lambda x: x.get("heat") or 0, reverse=True)
    for i, it in enumerate(out, 1):
        it["rank"] = i
        it["heatText"] = fmt_heat(it.get("heat"))
    return out


# ---------------------------------------------------------------- 专用解析

def p_baidu(url):
    html = http_text(url, referer="https://top.baidu.com/")
    m = re.search(r"<!--s-data:(.*?)-->", html, re.S)
    items = []
    if m:
        data = json.loads(m.group(1))
        for card in (data.get("data", {}).get("cards") or []):
            for it in (card.get("content") or []):
                items.append(mk_item(it.get("word") or it.get("query"),
                                     it.get("rawUrl") or it.get("url"),
                                     to_heat(it.get("hotScore")), it.get("img") or "",
                                     (it.get("desc") or "")[:50]))
    if not items:
        data = http_json("https://top.baidu.com/api/board?platform=wise&tab=realtime",
                         referer="https://top.baidu.com/")
        for card in (data.get("data", {}).get("cards") or []):
            for it in (card.get("content") or []):
                items.append(mk_item(it.get("word"), it.get("url")))
    return finish(items)


def _weibo_official(url):
    try:
        fetch_bytes("https://weibo.com/", timeout=10)
    except Exception:
        pass
    data = http_json(url, referer="https://weibo.com/", timeout=12)
    arr = (data.get("data") or {}).get("realtime") or []
    out = []
    for it in arr:
        word = it.get("word") or it.get("note") or ""
        if not word or it.get("is_ad"):
            continue
        q = it.get("word_scheme") or ("#%s#" % word if it.get("is_band") else word)
        out.append(mk_item(word, "https://s.weibo.com/weibo?q=" + urllib.parse.quote(q),
                           to_heat(it.get("num") or it.get("raw_hot")), "",
                           it.get("label_name") or ""))
    return finish(out)


def _weibo_mobile():
    u = ("https://m.weibo.cn/api/container/getIndex?containerid="
         "106003type%3D25%26t%3D3%26disable_hot%3D1%26filter_type%3Drealtimehot")
    data = http_json(u, referer="https://m.weibo.cn/", timeout=12)
    out = []
    for c in ((data.get("data") or {}).get("cards") or []):
        for it in (c.get("card_group") or []):
            word = it.get("desc")
            if not word:
                continue
            out.append(mk_item(word, "https://s.weibo.com/weibo?q=" + urllib.parse.quote(word),
                               to_heat(it.get("desc_extr")), ""))
    return finish(out)


def _weibo_tophub():
    return p_tophub("https://tophub.today/n/KqndgxeLl9")


def p_weibo(url):
    errs = []
    for fn in (lambda: _weibo_official(url), _weibo_mobile, _weibo_tophub):
        try:
            items = fn()
            if items:
                return items
        except Exception as e:
            errs.append(repr(e))
    raise RuntimeError("微博接口全部失败: " + "; ".join(errs[:2]))


def p_bilibili(url):
    try:
        fetch_bytes("https://www.bilibili.com/", timeout=10)
    except Exception:
        pass
    data = http_json(url, referer="https://www.bilibili.com/v/popular/rank/all", timeout=14)
    lst = (data.get("data") or {}).get("list")
    if not lst:
        data = http_json("https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1",
                         referer="https://www.bilibili.com/")
        lst = (data.get("data") or {}).get("list") or []
    out = []
    for it in lst:
        st = it.get("stat") or {}
        out.append(mk_item(it.get("title"),
                           it.get("short_link_v2") or ("https://www.bilibili.com/video/" + str(it.get("bvid") or "")),
                           to_heat(st.get("view")), it.get("pic"),
                           " · ".join(x for x in [(it.get("owner") or {}).get("name") or "",
                                                  it.get("tname") or ""] if x)))
    return finish(out)


def p_github(url):
    html, last = "", None
    for tmo in (28, 38):
        try:
            html = http_text(url, referer="https://github.com/", timeout=tmo)
            break
        except Exception as e:
            last = e
            time.sleep(1)
    if not html:
        raise last or RuntimeError("GitHub 抓取失败")
    root = parse_html(html)
    out = []
    for art in root.find_all("article", cls="Box-row"):
        h2 = art.first("h2")
        a = h2.first("a") if h2 else None
        if not a:
            continue
        repo = a.text().replace(" ", "")
        if "/" not in repo:
            continue
        owner = repo.split("/")[0]
        body = art.text(600)
        m = re.search(r"([\d,]+)\s*stars?\s*today", body, re.I)
        lang = ""
        for sp in art.find_all("span"):
            if sp.get("itemprop") == "programmingLanguage":
                lang = sp.text(24)
                break
        p = art.first("p")
        desc = p.text(120) if p else ""
        extra = " · ".join(x for x in [lang, desc] if x)
        it = mk_item(repo, "https://github.com/" + a.get("href").strip("/"),
                     to_heat(m.group(1)) if m else None,
                     "https://github.com/%s.png?size=96" % owner, extra,
                     "https://github.com/")
        if it and desc:
            it["desc"] = desc[:150]   # 英文简介单独带出，供界面悬停显示中文翻译
        out.append(it)
    return finish(out)


def p_crossref(url):
    """Crossref 期刊最新论文：/journals/<ISSN>/works?rows=N&sort=published&order=desc

    NEJM / Lancet / JAMA / Nature 系 / Elsevier 系这些期刊站几乎都挡爬虫（Cloudflare 403），
    但 Crossref 是公开的元数据 API，能稳定拿到「该刊最新发表的论文标题 + DOI」。
    坑：Crossref 的 title 是**数组**，直接 str() 会带上方括号（云端实测出现过
    "['TAVR in Patients ...']" 这种标题），这里必须取首元素。"""
    data = http_json(url, timeout=20)
    items = ((data or {}).get("message") or {}).get("items") or []
    out = []
    for it in items:
        t = it.get("title")
        title = (t[0] if t else "") if isinstance(t, list) else (t or "")
        title = re.sub(r"\s+", " ", str(title)).strip()
        doi = str(it.get("DOI") or "").strip()
        if not title or not doi:
            continue
        # 过滤非论文条目：卷首、勘误、编委会名单等
        if re.match(r"(?i)^(back ?matter|front ?matter|issue information|correction|erratum)", title):
            continue
        day = ""
        for k in ("published", "published-online", "issued"):
            parts = (it.get(k) or {}).get("date-parts")
            if parts and parts[0] and parts[0][0]:
                seq = parts[0]
                day = "-".join("%04d" % seq[0] if i == 0 else "%02d" % int(x)
                               for i, x in enumerate(seq) if x)
                break
        out.append(mk_item(title, "https://doi.org/" + doi, None, "", day))
    if not out:
        raise RuntimeError("Crossref 未返回论文条目")
    return finish(out, sort_by_heat=False)


def p_pubmed(url):
    """PubMed E-utilities：esearch 拿 PMID 列表，esummary 拿标题。

    PubMed 本身没有可解析的列表页（前端是 JS），必须走两步 JSON 接口。
    url 里带 esearch 的查询串（term / retmax / sort），解析器自己补第二步。"""
    data = http_json(url, timeout=20)
    ids = ((data or {}).get("esearchresult") or {}).get("idlist") or []
    ids = [str(i) for i in ids if i][:20]
    if not ids:
        raise RuntimeError("PubMed 未返回 PMID")
    s = http_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                  "?db=pubmed&retmode=json&id=" + ",".join(ids), timeout=20)
    res = (s or {}).get("result") or {}
    out = []
    for pid in ids:
        it = res.get(pid) or {}
        title = re.sub(r"\s+", " ", str(it.get("title") or "")).strip().rstrip(".")
        if not title:
            continue
        src = str(it.get("fulljournalname") or it.get("source") or "")
        day = str(it.get("pubdate") or "")
        out.append(mk_item(title, "https://pubmed.ncbi.nlm.nih.gov/%s/" % pid,
                           None, "", " · ".join(x for x in [src, day] if x)))
    if not out:
        raise RuntimeError("PubMed 未解析到条目")
    return finish(out, sort_by_heat=False)


def p_gitea(url):
    """Gitea/Forgejo 公开 API：/api/v1/repos/search?sort=stars|updated&order=desc

    用途：Codeberg 这类实例的页面上没有服务端渲染的热门榜，只有 API 支持
    「按标星 / 最近更新」排序 —— 正好满足「按星标或上升量排行」这个要求。"""
    data = http_json(url, timeout=20)
    rows = (data or {}).get("data") or []
    out = []
    for r in rows:
        name = str(r.get("full_name") or "").strip()
        if not name:
            continue
        stars = r.get("stars_count") or 0
        desc = str(r.get("description") or "").strip()
        out.append(mk_item(name, str(r.get("html_url") or ("https://codeberg.org/" + name)),
                           int(stars) if stars else None, "", desc[:70]))
    if not out:
        raise RuntimeError("Gitea API 未返回仓库")
    return finish(out, sort_by_heat=False)



def p_tophub(url):
    html = http_text(url, referer="https://tophub.today/", timeout=18)
    root = parse_html(html)
    out = []
    for tr in root.find_all("tr"):
        tds = [c for c in tr.children if not isinstance(c, str) and c.tag == "td"]
        if len(tds) < 3:
            continue
        a, best = None, 0
        for td in tds[1:]:
            for x in td.find_all("a"):
                ln = len(x.text(80))
                if ln > best:
                    a, best = x, ln
        if not a or best < 2:
            continue
        heat_txt = ""
        for td in tds:
            t = td.text(24)
            if re.search(r"(热度|万|亿)", t) and not re.fullmatch(r"\d+\.", t):
                heat_txt = t
                break
        out.append(mk_item(a.text(120), a.get("href"), to_heat(heat_txt),
                           node_img(tr), heat_txt, "https://tophub.today/"))
    if len(out) < 3:
        raise RuntimeError("镜像页没有解析到榜单（可能改版或限流）")
    return finish(out)


def p_qqnews(url):
    data = http_json(url, referer="https://news.qq.com/", timeout=14)
    nl = []
    for blk in (data.get("idlist") or []):
        nl.extend(blk.get("newslist") or [])
    out = []
    for it in nl:
        he = it.get("hotEvent") or {}
        if not it.get("url"):
            continue
        thumb = (it.get("thumbnails") or [None])[0] or ""
        out.append(mk_item(it.get("title") or he.get("title"), it.get("url"),
                           to_heat(he.get("hotScore")) or to_heat(it.get("readCount")),
                           thumb, it.get("source") or ""))
    return finish(out)


def p_ithome(url):
    data = http_json(url, referer="https://www.ithome.com/", timeout=14)
    out = []
    for it in (data.get("newslist") or []):
        out.append(mk_item(it.get("title"), it.get("url"),
                           to_heat(it.get("hitcount")), it.get("image") or "",
                           (it.get("description") or "")[:40]))
    return finish(out)


def p_sspai(url):
    data = http_json(url, referer="https://sspai.com/", timeout=14)
    out = []
    for it in (data.get("data") or []):
        banner = it.get("banner") or ""
        if banner and not banner.startswith("http"):
            banner = "https://cdn.sspai.com/" + banner.lstrip("/")
        out.append(mk_item(it.get("title"), "https://sspai.com/post/%s" % it.get("id"),
                           to_heat(it.get("like_count")) or to_heat(it.get("view_count")),
                           banner, (it.get("summary") or "")[:40]))
    return finish(out)


def _rss_unescape(txt):
    """RSS 文本清理：去 CDATA、解实体、去标签"""
    if not txt:
        return ""
    txt = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", txt, flags=re.S)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = (txt.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
              .replace("&apos;", "'").replace("&#39;", "'").replace("&nbsp;", " ")
              .replace("&amp;", "&"))
    # V8.1：数字实体（&#8216; 等）也解掉——TechCrunch 等站的标题全是数字实体
    txt = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)) if int(m.group(1), 16) < 0x110000 else "", txt)
    txt = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x110000 else "", txt)
    return re.sub(r"\s+", " ", txt).strip()


def p_rss(url):
    """通用 RSS/Atom 解析：适配各站官方 feed"""
    xml = http_text(url, timeout=18)
    if len(xml) < 200:
        raise RuntimeError("RSS 内容过短（%d 字节）" % len(xml))
    out = []
    # RSS 2.0
    for blk in re.findall(r"<item\b[^>]*>(.*?)</item>", xml, re.S):
        def g(tag):
            m = re.search(r"<%s\b[^>]*>(.*?)</%s>" % (tag, tag), blk, re.S)
            return m.group(1) if m else ""
        title = _rss_unescape(g("title"))
        link = _rss_unescape(g("link")) or _rss_unescape(
            (re.search(r'<guid[^>]*isPermaLink="true"[^>]*>(.*?)</guid>', blk, re.S) or [None, ""])[1])
        if not title or not link.startswith("http"):
            continue
        img = ""
        m = re.search(r'<enclosure[^>]+url="([^"]+)"', blk) or re.search(r'<media:content[^>]+url="([^"]+)"', blk)
        if m:
            img = m.group(1)
        elif re.search(r'<img[^>]+src="([^"]+)"', g("description")):
            img = re.search(r'<img[^>]+src="([^"]+)"', g("description")).group(1)
        out.append(mk_item(title, link, None, img, ""))
    # Atom
    if not out:
        for blk in re.findall(r"<entry\b[^>]*>(.*?)</entry>", xml, re.S):
            t = re.search(r"<title\b[^>]*>(.*?)</title>", blk, re.S)
            l = re.search(r'<link[^>]+href="([^"]+)"', blk)
            if t and l:
                out.append(mk_item(_rss_unescape(t.group(1)), l.group(1), None, "", ""))
    if not out:
        raise RuntimeError("RSS 中未找到条目")
    return finish(out)


def p_juejin(url):
    """掘金热榜"""
    data = http_json(url, referer="https://juejin.cn/", timeout=14)
    out = []
    for it in (data.get("data") or []):
        c = it.get("content") or {}
        cid = c.get("content_id") or it.get("content_id")
        if not c.get("title"):
            continue
        cnt = it.get("content_counter") or {}
        heat = to_heat(cnt.get("hot_rank")) or to_heat(cnt.get("view"))
        out.append(mk_item(c.get("title"), "https://juejin.cn/post/%s" % cid, heat, "", ""))
    return finish(out)


def p_douban_movie(url):
    """豆瓣热门电影（JSON）"""
    data = http_json(url, referer="https://movie.douban.com/", timeout=14)
    out = []
    for it in (data.get("subjects") or []):
        t = (it.get("title") or "").strip()
        if not t:
            continue
        rate = it.get("rate") or ""
        out.append(mk_item("%s%s" % (t, ("  豆瓣 %s 分" % rate) if rate and rate != "0" else ""),
                           it.get("url"), None, it.get("cover") or "", ""))
    return finish(out)


def p_yicai(url):
    """第一财经最新资讯（JSON 数组）"""
    data = http_json(url, referer="https://www.yicai.com/", timeout=14)
    arr = data if isinstance(data, list) else (data.get("data") or [])
    out = []
    for it in arr:
        t = (it.get("NewsTitle") or it.get("Title") or "").strip()
        nid = it.get("NewsID") or it.get("ID")
        if not t or not nid:
            continue
        out.append(mk_item(t, "https://www.yicai.com/news/%s.html" % nid,
                           to_heat(it.get("CommentCount")), it.get("NewsImage") or "", ""))
    return finish(out)


def p_zhihudaily(url):
    data = http_json(url, referer="https://daily.zhihu.com/", timeout=14)
    out = []
    for it in (data.get("stories") or []):
        imgs = it.get("images") or []
        out.append(mk_item(it.get("title"), it.get("url") or
                           ("https://daily.zhihu.com/story/%s" % it.get("id")),
                           None, imgs[0] if imgs else "", it.get("hint") or ""))
    return finish(out, sort_by_heat=False)


def p_toutiao(url):
    data = http_json(url, referer="https://www.toutiao.com/", timeout=14)
    out = []
    for it in (data.get("data") or []):
        out.append(mk_item(it.get("Title") or it.get("QueryWord"), it.get("Url"),
                           to_heat(it.get("HotValue")), it.get("Image") or ""))
    return finish(out)


def p_d423(url):
    html = http_text(url, referer="https://www.423down.com/", timeout=16)
    root = parse_html(html)
    holders = []
    for ul in root.find_all("ul", cls="excerpt"):
        holders += [c for c in ul.children if not isinstance(c, str) and c.tag == "li"]
    if not holders:
        holders = [li for li in root.find_all("li") if "post-" in li.cls]
    out = []
    for li in holders:
        h2 = li.first("h2")
        a = h2.first("a") if h2 else None
        if not a:
            continue
        img = ""
        pic = li.first("a", cls="pic")
        if pic:
            im = pic.first("img")
            if im:
                img = im.get("src") or im.get("data-src") or ""
        tm = li.first("span", cls="time")
        note = li.first("div", cls="note")
        out.append(mk_item(a.get("title") or a.text(120), a.get("href"), None, img,
                           " · ".join(x for x in [(tm.text(20) if tm else ""),
                                                  (note.text(40) if note else "")] if x),
                           "https://www.423down.com/"))
    return finish(out, sort_by_heat=False)


def p_ypojie(url):
    html = http_text(url, referer="https://www.ypojie.com/", timeout=16)
    root = parse_html(html)
    out = []
    for art in root.find_all("article"):
        if "excerpt" not in art.cls:
            continue
        h2 = art.first("h2")
        a = h2.first("a") if h2 else None
        if not a or not re.search(r"/\d+\.html", a.get("href") or ""):
            continue
        img = ""
        for im in art.find_all("img"):
            cand = im.get("data-src") or im.get("src") or ""
            if cand and "thumbnail.png" not in cand:
                img = cand
                break
        note = art.first("p", cls="note")
        tm = art.first("time") or art.first("span", cls="time")
        out.append(mk_item(a.get("title") or a.text(120), a.get("href"), None, img,
                           " · ".join(x for x in [(tm.text(20) if tm else ""),
                                                  (note.text(40) if note else "")] if x),
                           "https://www.ypojie.com/"))
    return finish(out, sort_by_heat=False)


def p_cr173(url):
    html = http_text(url, referer="https://www.cr173.com/", timeout=16)
    root = parse_html(html)
    out = []
    for a in root.find_all("a"):
        if not a.has_cls("a_tit"):
            continue
        href = a.get("href") or ""
        if "/soft/" not in href:
            continue
        li = a.parent
        img, extra = "", ""
        if li:
            for im in li.find_all("img"):
                src = im.get("src") or ""
                if "qr.612.com" in src:
                    continue
                img = src
                break
            sp = li.first("span")
            if sp:
                extra = sp.text(30)
        out.append(mk_item(a.text(120), href, None, img, extra, "https://www.cr173.com/"))
    return finish(out, sort_by_heat=False)


def p_baicaio(url):
    html = http_text(url, referer="https://www.baicaio.com/", timeout=16)
    root = parse_html(html)
    out = []
    for li in root.find_all("li"):
        if "transverse" not in li.cls:
            continue
        a = None
        for cand in li.find_all("a"):
            if "/item/" in (cand.get("href") or ""):
                a = cand
                break
        if not a:
            continue
        img = ""
        for im in li.find_all("img"):
            src = im.get("src") or im.get("data-src") or ""
            if src and "lazy" not in src:
                img = src
                break
        extra = ""
        for sp in li.find_all("span"):
            t = sp.text(24)
            if "值" in t or "评论" in t or "元" in t:
                extra = t
                break
        out.append(mk_item(a.get("title") or a.text(120), a.get("href"), None, img, extra,
                           "https://www.baicaio.com/"))
    return finish(out, sort_by_heat=False)


# ---------------------------------------------------------------- 通用解析

def _json_lists(obj, path="", depth=0, acc=None):
    acc = [] if acc is None else acc
    if depth > 6:
        return acc
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            acc.append((path, obj))
        for i, v in enumerate(obj[:3]):
            _json_lists(v, path + "[%d]" % i, depth + 1, acc)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _json_lists(v, path + "/" + k if path else k, depth + 1, acc)
    return acc


def _json_extract(data, base):
    """从任意 JSON（含裸数组/嵌套）里挑最像榜单的数组并转成条目。"""
    best, best_score = None, -1
    for path, arr in _json_lists(data):
        sample, score = arr[0], 0
        if pick(sample, TITLE_KEYS):
            score += 3
        if pick(sample, URL_KEYS):
            score += 2
        if pick(sample, HEAT_KEYS):
            score += 2
        score += min(len(arr), 30) / 10.0
        if score > best_score:
            best, best_score = arr, score
    if not best:
        raise RuntimeError("JSON 中没找到榜单数组")
    out = []
    for it in best:
        title = pick(it, TITLE_KEYS)
        if not title:
            continue
        out.append(mk_item(str(title), pick(it, URL_KEYS) or "", to_heat(pick(it, HEAT_KEYS)),
                           pick(it, IMG_KEYS) or "", "", base))
    return finish(out)


def generic_json(url, base):
    data = http_json(url)
    return _json_extract(data, base)


def p_jsonp(url):
    """JSONP 接口：callback(...) 包裹的 JSON，如央视新闻 news(...)。"""
    txt = http_text(url)
    i, j = txt.find("("), txt.rindex(")")
    if i < 0 or j <= i:
        raise RuntimeError("不是 JSONP 格式")
    data = json.loads(txt[i + 1:j])
    return _json_extract(data, "%s://%s" % (urllib.parse.urlparse(url).scheme,
                                            urllib.parse.urlparse(url).netloc))


def p_autohome(url):
    """汽车之家销量榜：data-rank-num 区块内 img alt=车系名、span 为月销量。"""
    html = http_text(url)
    base = "https://www.autohome.com.cn"
    out = []
    for m in re.finditer(r'data-rank-num="(\d+)"(.*?)(?=data-rank-num="|\Z)', html, re.S):
        rank, blk = m.group(1), m.group(2)
        nm = re.search(r'alt="([^"]{2,30})"', blk)
        if not nm:
            continue
        sal = re.search(r'tw-font-medium">(\d{2,})</span>', blk)
        out.append(mk_item(nm.group(1), "", to_heat(sal.group(1)) if sal else None, "", "", base))
        out[-1]["rank"] = int(rank) if str(rank).isdigit() else len(out)
    if len(out) < 3:
        raise RuntimeError("页面里没找到榜单结构")
    return finish(out)


# 奥斯卡最佳影片历届得主（第1届 1927/28 至第98届 2025），内置静态数据，三端离线可用。
# 格式：届数|颁奖年份|中文译名|英文原名
_OSCAR_WINNERS = """1|1927/28|翼|Wings
2|1928/29|百老汇的旋律|The Broadway Melody
3|1929/30|西线无战事|All Quiet on the Western Front
4|1930/31|壮志千秋|Cimarron
5|1931/32|大饭店|Grand Hotel
6|1932/33|乱世春秋|Cavalcade
7|1934|一夜风流|It Happened One Night
8|1935|叛舰喋血记|Mutiny on the Bounty
9|1936|歌舞大王齐格飞|The Great Ziegfeld
10|1937|左拉传|The Life of Emile Zola
11|1938|浮生若梦|You Can't Take It with You
12|1939|乱世佳人|Gone with the Wind
13|1940|蝴蝶梦|Rebecca
14|1941|青山翠谷|How Green Was My Valley
15|1942|忠勇之家|Mrs. Miniver
16|1943|卡萨布兰卡|Casablanca
17|1944|与我同行|Going My Way
18|1945|失去的周末|The Lost Weekend
19|1946|黄金时代|The Best Years of Our Lives
20|1947|君子协定|Gentleman's Agreement
21|1948|王子复仇记|Hamlet
22|1949|当代奸雄|All the King's Men
23|1950|彗星美人|All About Eve
24|1951|花都舞影|An American in Paris
25|1952|戏王之王|The Greatest Show on Earth
26|1953|乱世忠魂|From Here to Eternity
27|1954|码头风云|On the Waterfront
28|1955|马蒂|Marty
29|1956|八十天环游地球|Around the World in 80 Days
30|1957|桂河大桥|The Bridge on the River Kwai
31|1958|金粉世界|Gigi
32|1959|宾虚|Ben-Hur
33|1960|桃色公寓|The Apartment
34|1961|西区故事|West Side Story
35|1962|阿拉伯的劳伦斯|Lawrence of Arabia
36|1963|汤姆·琼斯|Tom Jones
37|1964|窈窕淑女|My Fair Lady
38|1965|音乐之声|The Sound of Music
39|1966|四季之人|A Man for All Seasons
40|1967|炎热的夜晚|In the Heat of the Night
41|1968|雾都孤儿|Oliver!
42|1969|午夜牛郎|Midnight Cowboy
43|1970|巴顿将军|Patton
44|1971|法国贩毒网|The French Connection
45|1972|教父|The Godfather
46|1973|骗中骗|The Sting
47|1974|教父2|The Godfather Part II
48|1975|飞越疯人院|One Flew Over the Cuckoo's Nest
49|1976|洛奇|Rocky
50|1977|安妮·霍尔|Annie Hall
51|1978|猎鹿人|The Deer Hunter
52|1979|克莱默夫妇|Kramer vs. Kramer
53|1980|普通人|Ordinary People
54|1981|烈火战车|Chariots of Fire
55|1982|甘地传|Gandhi
56|1983|母女情深|Terms of Endearment
57|1984|莫扎特传|Amadeus
58|1985|走出非洲|Out of Africa
59|1986|野战排|Platoon
60|1987|末代皇帝|The Last Emperor
61|1988|雨人|Rain Man
62|1989|为黛西小姐开车|Driving Miss Daisy
63|1990|与狼共舞|Dances with Wolves
64|1991|沉默的羔羊|The Silence of the Lambs
65|1992|杀无赦|Unforgiven
66|1993|辛德勒的名单|Schindler's List
67|1994|阿甘正传|Forrest Gump
68|1995|勇敢的心|Braveheart
69|1996|英国病人|The English Patient
70|1997|泰坦尼克号|Titanic
71|1998|莎翁情史|Shakespeare in Love
72|1999|美国丽人|American Beauty
73|2000|角斗士|Gladiator
74|2001|美丽心灵|A Beautiful Mind
75|2002|芝加哥|Chicago
76|2003|指环王3：王者归来|The Lord of the Rings: The Return of the King
77|2004|百万美元宝贝|Million Dollar Baby
78|2005|撞车|Crash
79|2006|无间行者|The Departed
80|2007|老无所依|No Country for Old Men
81|2008|贫民窟的百万富翁|Slumdog Millionaire
82|2009|拆弹部队|The Hurt Locker
83|2010|国王的演讲|The King's Speech
84|2011|艺术家|The Artist
85|2012|逃离德黑兰|Argo
86|2013|为奴十二年|12 Years a Slave
87|2014|鸟人|Birdman
88|2015|聚焦|Spotlight
89|2016|月光男孩|Moonlight
90|2017|水形物语|The Shape of Water
91|2018|绿皮书|Green Book
92|2019|寄生虫|Parasite
93|2020|无依之地|Nomadland
94|2021|健听女孩|CODA
95|2022|瞬息全宇宙|Everything Everywhere All at Once
96|2023|奥本海默|Oppenheimer
97|2024|阿诺拉|Anora
98|2025|一战再战|One Battle After Another"""


def p_oscar(url=None):
    """奥斯卡·历届最佳影片（内置静态数据）。"""
    base = "https://movie.douban.com"
    out = []
    for line in _OSCAR_WINNERS.splitlines():
        parts = (line.split("|") + ["", "", ""])[:4]
        n, yr, cn, en = parts
        q = urllib.parse.quote(cn or en)
        out.append(mk_item("第%s届(%s)《%s》" % (n, yr, cn),
                           "https://www.douban.com/search?q=" + q, None, "", en, base))
        out[-1]["rank"] = int(n) if n.isdigit() else len(out)
    return finish(out)


NAV_WORDS = set("""首页 主页 更多 查看更多 登录 注册 退出 上一页 下一页 下载 详情 评论
关于我们 联系我们 网站地图 返回顶部 广告 投稿 客户端 排行榜 全部 搜索 立即下载 查看详情
新闻 体育 娱乐 财经 科技 汽车 房产 旅游 教育 时尚 健康 军事 图片 视频 论坛 博客 专题""".split())
# V8.1：英文导航词补充（打辅助用，主力是结构排除+统计评分）
NAV_WORDS.update("""about home more login sign register search contact privacy terms faq help
archive subscribe advertise careers jobs feedback sitemap english chinese mobile app
read comments share tweet facebook next prev back submit editors picks newest popular
trending latest archive articles videos podcasts newsletters magazine webinars members""".split())
NAV_WORDS.update("""服务协议 隐私政策 法律声明 免责声明 使用条款 用户协议 注册协议
关于我们 联系方式 加入我们 网站声明 版权声明 网站导航""".split())


def _nav_word(t):
    """导航词判断：去掉书名号/引号后再比对（《用户注册服务协议》→ 用户注册服务协议）"""
    t = re.sub(r"[《》「」『』\"'“”]", "", t).strip()
    if t in NAV_WORDS:
        return True
    # V8.1：中文站点信息页短链接（关于/用户/隐私…开头的协议声明类，≤14 字）
    if len(t) <= 14 and re.match(r"^(关于|用户|隐私|法律|免责|使用|网站|版权|服务|注册|登录|投稿|广告)", t) \
            and re.search(r"(协议|声明|条款|须知|公约|规则|指南)$", t):
        return True
    # V8.1：ICP 备案号/版权页脚（京ICP证040089号-1、ICP备xxxxx号 等）
    return bool(re.search(r"(ICP ?[证备]|备案号|版权所有|©|Copyright)", t, re.I))


def _heat_from(text, title=""):
    """从区块文本中提取热度。
    title 传入条目标题：先把标题从文本中剔除，再校验匹配片段不是标题的一部分——
    否则标题里的「150亿人民币」「2026.1万」等数字会被误判成阅读量。"""
    t = clean_text(title) if title else ""
    if t and t in text:
        text = text.replace(t, " ")
    patterns = (r"(\d+(?:\.\d+)?)\s*(万|亿)\s*(?:热度|浏览|点击|次|阅读|评论)?",
                r"(\d[\d,]{3,})\s*(?:条评论|浏览|阅读|次|\s*热度)?")
    for pat in patterns:
        for m in re.finditer(pat, text):
            frag = m.group(0).strip()
            if t and (frag in t or m.group(1) in t):
                continue  # 片段出自标题（如「150亿人民币」），跳过
            return to_heat(m.group(1))
    return None


def parse_by_sel(root, url, sel):
    base = "%s://%s" % (urllib.parse.urlparse(url).scheme, urllib.parse.urlparse(url).netloc)
    out = []
    block = sel.get("block")
    link_sel = sel.get("link") or "a"
    heat_sel = sel.get("heat")
    desc_sel = sel.get("desc")
    if block:
        for b in select_all(root, block)[:120]:
            a = select_one(b, link_sel)
            if a is None:
                for cand in b.find_all("a"):
                    if len(cand.text(60)) >= 6:
                        a = cand
                        break
            if a is None:
                continue
            title = a.get("title") or a.text(120)
            if len(clean_text(title)) < 5:
                continue
            heat = None
            if heat_sel:
                h = select_one(b, heat_sel)
                if h:
                    heat = _heat_from(h.text(40), title)
            if heat is None:
                heat = _heat_from(b.text(200), title)
            desc = select_one(b, desc_sel).text(60) if desc_sel and select_one(b, desc_sel) else ""
            out.append(mk_item(title, a.get("href"), heat, node_img(b), desc, base))
    else:
        for a in select_all(root, link_sel)[:150]:
            title = a.get("title") or a.text(120)
            if len(clean_text(title)) < 5:
                continue
            holder, img, heat, desc = a.parent, "", None, ""
            for _ in range(3):
                if holder is None:
                    break
                if not img:
                    img = node_img(holder)
                if not desc:
                    t = holder.text(120)
                    if len(t) > len(clean_text(title)) + 8:
                        desc = t.replace(clean_text(title), "", 1).strip()[:60]
                if heat is None:
                    heat = _heat_from(holder.text(160), title)
                if img and heat:
                    break
                holder = holder.parent
            out.append(mk_item(title, a.get("href"), heat, img, desc, base))
    return finish(out)


# ---------------- V8.1 识别机制：让通用解析提取「新闻题目」而不是导航/分类目录 ----------------
# 四层防线：①DOM 结构排除 ②导航词/URL 形态过滤 ③组级统计评分（替代最大组取胜）④菜单自检宁报错

_NAV_ZONE_RE = re.compile(
    r"(^|[^a-z0-9])(nav|navbar|topbar|menu|submenu|dropdown|sidebar|aside|breadcrumb|crumbs|"
    r"footer|pager|pagination|site-header|page-header|main-header|masthead|toolbar|"
    r"social|share|widget|banner|advert|advertise|copyright|sitemap|skip-link|site-info)([^a-z0-9]|$)")
_NAV_TAGS = ("nav", "footer", "aside")


def _eff_len(t):
    """有效长度：中日韩字符按 2 计，便于中英文站统一判「标题够不够长」"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in t)


def _in_nav_zone(a, up=5):
    """①结构排除：锚点若在 nav/footer/aside 标签或导航样式容器内（向上最多 5 层），判为导航区"""
    n, d = a.parent, 0
    while n is not None and d < up:
        if n.tag in _NAV_TAGS:
            return True
        ident = (n.cls + " " + (n.get("id") or "").lower())
        if ident.strip() and _NAV_ZONE_RE.search(ident):
            return True
        n, d = n.parent, d + 1
    return False


def _grp_stats(texts):
    eff = [_eff_len(t) for t in texts]
    n = len(eff)
    med = sorted(eff)[n // 2]
    mean = sum(eff) / n
    sd = (sum((x - mean) ** 2 for x in eff) / n) ** 0.5
    return med, sd


def _common_words(texts):
    """组内所有标题共享的词（菜单高度模板化：如都带 Newest / 都是栏目名）"""
    sets = [set(w.lower() for w in re.findall(r"[a-zA-Z\u4e00-\u9fff]{2,}", t)) for t in texts]
    if not sets:
        return set()
    com = set(sets[0])
    for s in sets[1:]:
        com &= s
    return com


def _headline_score(anchors, texts, kind):
    """③组级评分：越像「新闻标题列表」分越高；越像菜单/栏目目录分越低"""
    n = len(texts)
    if n < 3:
        return -999.0
    med, sd = _grp_stats(texts)
    score = min(med, 60.0) / 10.0          # 标题偏长加分（0~6）
    score += min(sd, 30.0) / 10.0          # 长短差异：菜单高度均匀，标题参差（0~3）
    rich = sum(1 for t in texts if re.search(r"[\d【】「」《》！？?\"“”「：:，,|]|…|——", t)) / n
    score += rich * 2.0                    # 标题常含数字/标点（0~2）
    com = _common_words(texts)
    if n >= 4 and com:
        score -= min(len(com), 8) * 0.8    # 全组共享同一批词 → 模板化菜单，重罚
    hrefs = [a.get("href") or "" for a in anchors]
    uniq = len(set(hrefs)) / n
    score += uniq * 2.0                    # 重复链接惩罚
    if kind == "cls" and n >= 5:
        score += 1.0                       # 同 class 签名的重复标记 = 列表条目，加分
    if med < 12 and sd < 4:
        score -= 4.0                       # 又短又整齐：典型导航/栏目
    return score


_MENU_MIN_SCORE = 3.0


def _looks_like_menu(texts):
    med, sd = _grp_stats(texts)
    return med < 14 and sd < 4 and len(texts) >= 5


def _html_candidate_groups(root):
    """收集页面候选列表组。kind='cls' 按锚点 class 签名分组（解决每卡片唯一父元素被拆散），
    kind='parent' 按父元素分组（兼容旧逻辑）。返回 [(score, kind, key, anchors, texts)]"""
    cls_g, par_g = {}, {}
    for a in root.find_all("a"):
        href = a.get("href") or ""
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if href.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip",
                                  ".exe", ".apk", ".mp4", ".pdf", ".rar")):
            continue
        t = clean_text(a.text(120))
        if len(t) < 5 or _nav_word(t):
            continue
        if _in_nav_zone(a):          # ①结构排除
            continue
        p = a.parent
        if p is None:
            continue
        ckey = (a.cls or "")[:70] or "nocls"
        pkey = p.tag + "|" + " ".join(sorted(p.cls.split()))[:40]
        cls_g.setdefault(ckey, []).append(a)
        par_g.setdefault(pkey, []).append(a)
    groups = []
    for kind, g in (("cls", cls_g), ("par", par_g)):
        for key, anchors in g.items():
            # 去重（同页图片+标题双链接指向同一 URL 的情况）
            seen, ua, ut = set(), [], []
            for a in anchors:
                sig = (clean_text(a.text(120)), a.get("href") or "")
                if sig in seen:
                    continue
                seen.add(sig)
                ua.append(a)
                ut.append(re.sub(r"^\d+[\.、\s]*", "", clean_text(a.text(120))).strip())
            if len(ua) < 3:
                continue
            groups.append((_headline_score(ua, ut, kind), kind, key, ua, ut))
    groups.sort(key=lambda x: -x[0])
    groups = [(sc, kind, key, a[:150], t[:150]) for sc, kind, key, a, t in groups]
    return groups


def _extract_from_group(anchors, texts, base, url):
    out = []
    for a, t in zip(anchors, texts):
        if not t:
            continue
        p = a.parent
        block = p.text(400) if p is not None else ""
        out.append(mk_item(t, a.get("href"), _heat_from(block, t), node_img(p), "", base or url))
    return finish(out)


def generic_html(url, base):
    html, ferr = fetch_page_robust(url)
    if not html:
        raise RuntimeError("页面抓取失败：" + (ferr or "网络异常"))
    root = parse_html(html)
    groups = _html_candidate_groups(root)
    if not groups:
        raise RuntimeError("页面里没找到列表结构")
    best = groups[0]
    # ④自检：最高分组太像菜单时，尝试次优分组；全都不像 → 宁可报错不显示假数据
    chosen = None
    for sc, kind, key, ua, ut in groups[:5]:
        if sc >= _MENU_MIN_SCORE and not _looks_like_menu(ut):
            chosen = (kind, key, ua, ut)
            break
    if chosen is None:
        raise RuntimeError("未能识别新闻列表：页面内容疑似导航栏/分类目录"
                           "（条目过短且雷同），请用「校准」选择正确列表或改用子页面地址")
    kind, key, ua, ut = chosen
    return _extract_from_group(ua, ut, base, url)


def _sel_from_group(kind, anchor, parent):
    """把候选组换算成 parse_by_sel 可用的选择器（锁定用，保证预览即所得）"""
    if kind == "cls":
        cl = " ".join((anchor.cls or "").split())
        if not cl:
            return None
        sel_str = "a" + "".join("." + c for c in cl.split())
        return {"link": sel_str}
    cl = " ".join((parent.cls or "").split())
    if not cl:
        return None
    sel_str = parent.tag + "".join("." + c for c in cl.split())
    return {"block": sel_str, "link": "a"}


def html_candidates(url, limit=6):
    """V8.1 校准向导后端：返回页面上最像新闻列表的候选组（含选择器提案+10 条预览）"""
    if "{q}" in url:
        # 检索地址校准：用样例关键词实际请求搜索结果页
        url = url.replace("{q}", urllib.parse.quote("新闻"))
    html, ferr = fetch_page_robust(url)
    if not html:
        raise RuntimeError("页面抓取失败：" + (ferr or "网络异常"))
    root = parse_html(html)
    groups = _html_candidate_groups(root)
    base = "%s://%s" % (urllib.parse.urlparse(url).scheme, urllib.parse.urlparse(url).netloc)
    out, used = [], set()
    for sc, kind, key, ua, ut in groups:
        if len(out) >= limit:
            break
        sel = _sel_from_group(kind, ua[0], ua[0].parent)
        if sel is None:
            continue
        sig = json.dumps(sel, sort_keys=True)
        if sig in used:
            continue
        try:
            preview = parse_by_sel(root, url, sel)[:10]
        except Exception:
            continue
        if len(preview) < 3:
            continue
        used.add(sig)
        out.append({"kind": kind, "count": len(ua), "score": round(sc, 1),
                    "menu": bool(_looks_like_menu(ut)), "sel": sel,
                    "sample": [t[:60] for t in ut[:5]],
                    "preview": [{"title": clean_text(i.get("title", ""))[:60],
                                 "url": i.get("url", "")} for i in preview]})
    return out


def p_360news(url):
    """360 新闻搜索结果页（news.so.com/ns?q=关键词）——地区新闻用"""
    html = http_text(url, timeout=18)
    if len(html) < 3000:
        raise RuntimeError("360 新闻页内容过短")
    out = []
    for m in re.finditer(
            r'<li[^>]*class="[^"]*res-list[^"]*"[^>]*data-url="([^"]+)"([\s\S]*?)</li>', html):
        link = m.group(1).replace("&amp;", "&")
        blk = m.group(2)
        tm = re.search(r'title="([^"]+)"', blk)
        if not tm:
            continue
        title = tm.group(1)
        # title 属性反转义
        title = (title.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
                      .replace("&#39;", "'").replace("&nbsp;", " ").replace("&amp;", "&"))
        sm = re.search(r'<cite class="sitename">([^<]+)</cite>', blk)
        site = sm.group(1).strip() if sm else ""
        if len(clean_text(title)) < 5 or not link.startswith("http"):
            continue
        out.append(mk_item(title, link, None, "", site))
    out = [it for it in out if it]
    if not out:
        raise RuntimeError("360 新闻页未解析到条目")
    return finish(out)


def p_sse(url):
    """上交所：首页新闻发布会/新闻内容页"""
    html = http_text(url, timeout=18)
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"]*mediacenter[^"]*/c/[^"]+)"[^>]*title="([^"]{8,90})"', html):
        link, title = m.group(1), m.group(2)
        if link in seen:
            continue
        seen.add(link)
        out.append(mk_item(_rss_unescape(title), urllib.parse.urljoin("http://www.sse.com.cn/", link),
                           None, "", "上交所"))
    if not out:
        for m in re.finditer(r'<a[^>]+href="([^"]*mediacenter[^"]*/c/[^"]+)"[^>]*>\s*([^<]{8,90})\s*</a>', html):
            link, title = m.group(1), clean_text(m.group(2))
            if link in seen or not title:
                continue
            seen.add(link)
            out.append(mk_item(title, urllib.parse.urljoin("http://www.sse.com.cn/", link), None, "", "上交所"))
    if not out:
        raise RuntimeError("上交所首页未解析到新闻")
    return finish(out)


def p_weread(url):
    """微信读书·读书榜：https://weread.qq.com/#rank 服务端渲染页面，
    从「大家都在读 / 近期热读」卡片里提取 书名/作者/链接（无需登录）。"""
    html = http_text(url or "https://weread.qq.com/#rank", timeout=18)
    if len(html) < 3000:
        raise RuntimeError("微信读书页面加载不完整（%d 字节）" % len(html))
    out = []
    seen = set()
    # ① 榜单行：<a class="wr_index_page_mini_bookInfo" href="…"> → 序号 / 书名 / 作者 / 推荐值
    pat_rank = re.compile(
        r'href="(https://weread\.qq\.com/web/reader/[0-9a-f]+)"[\s\S]{0,1500}?'
        r'wr_index_page_mini_bookInfo_content_idx">(\d{1,2})</div>'
        r'[\s\S]{0,600}?wr_index_page_mini_bookInfo_content_title_name">([^<]{2,60})</span>')
    for m in pat_rank.finditer(html):
        link, idx, title = m.group(1), m.group(2), clean_text(m.group(3))
        if not title or title in seen:
            continue
        seen.add(title)
        win = html[m.start():m.start() + 2600]
        am = re.search(r'wr_index_page_mini_bookInfo_content_author"[^>]*>([\s\S]{0,80}?)</div>', win)
        author = clean_text(re.sub(r"<!--[\s\S]*?-->", "", am.group(1))) if am else ""
        rm = re.search(r"推荐值\s*([\d.]+)%", win)
        it = mk_item(title, link, int(float(rm.group(1)) * 100) if rm else None, "",
                     (author + (" · 推荐值 " + rm.group(1) + "%" if rm else "")).strip(" ·"),
                     "https://weread.qq.com")
        if idx.isdigit():
            it["rank"] = int(idx)
        out.append(it)
    # ② 推荐卡片：<a class="wr_suggestion_card_wrapper" href="…"> → 书名 / 作者
    pat = re.compile(
        r'href="(https://weread\.qq\.com/web/reader/[0-9a-f]+)"[\s\S]{0,900}?'
        r'wr_suggestion_card_content_title"[^>]*>([^<]{2,60})</div>'
        r'[\s\S]{0,400}?wr_suggestion_card_content_author"[^>]*>([^<]{0,40})<')
    for m in pat.finditer(html):
        link, title, author = m.group(1).strip(), clean_text(m.group(2)), clean_text(m.group(3))
        if not title or title in seen:
            continue
        seen.add(title)
        out.append(mk_item(title, link, None, "", author, "https://weread.qq.com"))
    if not out:
        # 兜底：抓不到卡片时退回通用解析
        return parse_by_sel(parse_html(html), url or "https://weread.qq.com/#rank", {})
    return finish(out)


def p_modelscope(url):
    """ModelScope 魔搭社区：热门模型榜（PUT 接口）"""
    body = json.dumps({"PageSize": 30, "PageNumber": 1, "SortBy": "Default"}).encode("utf-8")
    req = urllib.request.Request(
        url or "https://modelscope.cn/api/v1/dolphin/models", data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 Chrome/126 Safari/537.36"},
        method="PUT")
    with urllib.request.urlopen(req, timeout=18) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    data = json.loads(decode_bytes(raw))
    models = ((data.get("Data") or {}).get("Model") or {}).get("Models") or []
    out = []
    for m in models:
        path = (m.get("Path") or "").strip()
        if not path:
            continue
        name = clean_text(m.get("ChineseName") or m.get("Name") or path)[:80]
        dl, lk = m.get("Downloads"), m.get("Likes")
        heat = to_heat(dl) or to_heat(lk)
        extra = "下载 %s · 收藏 %s" % (format(dl or 0, ","), format(lk or 0, ","))
        out.append(mk_item(name, "https://modelscope.cn/models/" + path, heat, "", extra))
    if not out:
        raise RuntimeError("ModelScope 未解析到模型")
    return finish(out)


# ---------------------------------------------------------------- 学术期刊官网（V7.9.3）
# 这一批不是"热榜"站：要的是「当期目录 / 最新论文 / 通知公告」里的文章题目，
# 按网站自己的发布时间排序（不加热度、不按点击量），所以统一 finish(sort_by_heat=False)。

def _strip_tags(s):
    return clean_text(_html.unescape(re.sub(r"<[^>]+>", " ", s or "")))


def _get_html(url, extra=None, timeout=18):
    """少数站点按 Accept 头返回不同版本（不带 Accept 会拿到被转义的空壳），这里可自定义头"""
    hdrs = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    if extra:
        hdrs.update(extra)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    return decode_bytes(raw)


def _cn_cut(s):
    """「中文标题English Title」→ 只留中文标题（中文后紧跟英文字母处截断）"""
    s = _strip_tags(s).replace("*", " ").strip()
    m = re.search(r"[\u4e00-\u9fff][，。、；：？！）】”’]?\s*[A-Za-z]", s)
    if m and m.start() >= 4:
        return s[:m.start() + 1].strip()
    return s


def p_iwep(url):
    """世界经济与政治研究所（社科经济）：最新文章，日期在链接里 t20221226"""
    html = http_text(url, timeout=18)
    rows = re.findall(r'href="(\./qkjj/[^"]*?t(\d{8})[^"]*)"[^>]*>(.*?)</a>', html, re.S)
    got, seen = [], set()
    for href, d, raw in rows:
        t = _strip_tags(raw)
        if len(t) < 8 or t.startswith(("摘要", "【内容提要", "关键词", "作者")):
            continue
        if ("目录" in t) or ("征稿" in t) or ("启事" in t) or t.startswith("《"):
            continue
        if t in seen:
            continue
        seen.add(t)
        date = "%s-%s-%s" % (d[:4], d[4:6], d[6:])
        it = mk_item(t, abs_url(url, href), None, "", date, base=url)
        if it:
            got.append((date, it))
    if not got:
        raise RuntimeError("世界经济与政治研究所 未解析到文章")
    got.sort(key=lambda x: x[0], reverse=True)
    return finish([x[1] for x in got], sort_by_heat=False)


def p_ajcass(url):
    """ajcass 平台（经济研究）：官方 JSON 接口返回当期目录"""
    data = http_json(url, referer="https://erj.ajcass.com/", timeout=18)
    lst = ((data or {}).get("data") or {}).get("issueInfoList") or []
    out = []
    for a in lst:
        t = clean_text(a.get("title") or "")
        if len(t) < 6:
            continue
        yvi = clean_text(a.get("yearVolumeIssue") or "")
        authors = clean_text(a.get("authors") or "")
        extra = " · ".join([x for x in (yvi, authors) if x])[:70]
        out.append(mk_item(t, "https://erj.ajcass.com/", None, "", extra))
    if not out:
        raise RuntimeError("经济研究 当期目录为空")
    return finish(out, sort_by_heat=False)


def p_jryj(url):
    """金融研究：当期目录（附出版日期），文章标题 + 作者"""
    html = http_text(url, timeout=18)
    m = re.search(r"(\d{4})年\s*,\s*第\s*(\d+)\s*期.*?出版日期[：:]\s*(\d{4}-\d{2}-\d{2})", html, re.S)
    issue = ("%s年第%s期" % (m.group(1), m.group(2))) if m else ""
    pubdate = m.group(3) if m else ""
    extra = " · ".join([x for x in (issue, pubdate) if x])
    rows = re.findall(
        r'href="(?:(?:https?:)?//www\.jryj\.org\.cn)?(/CN/abstract/abstract\d+\.shtml)"[^>]*>(.*?)</a>',
        html, re.S)
    out, seen = [], set()
    for href, raw in rows:
        t = _strip_tags(raw)
        if len(t) < 6 or t in seen or t.startswith(("摘要", "正文", "参考文献")):
            continue
        seen.add(t)
        out.append(mk_item(t, abs_url(url, href), None, "", extra, base=url))
    if not out:
        raise RuntimeError("金融研究 当期目录未解析到文章")
    return finish(out, sort_by_heat=False)


def p_cie(url):
    """中国工业经济：当期目录（/Magazine/Show?id=xxx 为单篇文章）"""
    html = http_text(url, timeout=18)
    extra = "当期目录"
    rows = re.findall(r'href="(/Magazine/Show\?id=\d+)"[^>]*>(.*?)</a>', html, re.S)
    out, seen = [], set()
    for href, raw in rows:
        t = _strip_tags(raw)
        if len(t) < 6 or t.startswith(("【", "下载全文", "摘要")) or t in seen:
            continue
        seen.add(t)
        out.append(mk_item(t, abs_url(url, href), None, "", extra, base=url))
    if not out:
        raise RuntimeError("中国工业经济 当期目录未解析到文章")
    return finish(out, sort_by_heat=False)


def p_cfrn(url):
    """金融学术（CFRN）：最新论文列表（/lw/17021.html）"""
    html = _get_html(url)
    rows = re.findall(r"""href=['"](/lw/\d{4,6}\.html?)['"][^>]*>(.*?)</a>""", html, re.S)
    out, seen = [], set()
    for href, raw in rows:
        t = _strip_tags(raw)
        t = re.sub(r"\s{2,}", " ", t).strip()
        if len(t) < 8 or t in seen:
            continue
        seen.add(t)
        out.append(mk_item(t, abs_url(url, href), None, "", "最新论文", base=url))
    if not out:
        raise RuntimeError("金融学术 最新论文未解析到文章")
    return finish(out, sort_by_heat=False)


def p_nopss(url):
    """全国哲学社会科学工作办公室：通知公告（标题 + [发布时间]）"""
    html = http_text(url, timeout=18)
    rows = re.findall(
        r"<li>\s*<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>\s*<em>\[([^\]]+)\]</em>",
        html, re.S)
    out, seen = [], set()
    junk = ("网站首页", "本办概况", "政策制度", "基金管理", "基金专刊", "成果集萃",
            "资助期刊", "高端智库", "社团工作", "资料下载", "京ICP", "更多")
    for href, raw, dt in rows:
        t = _strip_tags(raw)
        if len(t) < 6 or t in seen or any(j in t for j in junk):
            continue
        seen.add(t)
        out.append((clean_text(dt), mk_item(t, abs_url(url, href), None, "", clean_text(dt), base=url)))
    if not out:
        raise RuntimeError("哲学社会科学 通知公告未解析到内容")
    out.sort(key=lambda x: x[0], reverse=True)
    return finish([x[1] for x in out], sort_by_heat=False)


def p_gjjryj(url):
    """国际金融研究：当期目录（URL 里带年/卷/期）"""
    html = http_text(url, timeout=18)
    rows = re.findall(
        r'href="(http://www\.gjjryj\.org\.cn/CN/Y(\d{4})/V\d+/I(\d+)/\d+)"[^>]*>(.*?)</a>',
        html, re.S)
    out, seen = [], set()
    for href, yr, iss, raw in rows:
        t = _cn_cut(raw)
        if len(t) < 6 or t in seen:
            continue
        seen.add(t)
        extra = "%s年第%s期" % (yr, iss)
        out.append(mk_item(t, href, None, "", extra, base=url))
    if not out:
        raise RuntimeError("国际金融研究 当期目录未解析到文章")
    return finish(out, sort_by_heat=False)


# ---------------------------------------------------------------- 前端渲染站点：直接调站点自己的接口
# V7.9.6：SciEngine / 国家自然科学基金成果库 这类站，浏览器里能看到内容，但 HTML 里是空的
# （页面由 JS 渲染）。它们的内部接口其实是公开的，直接调接口就能拿到和浏览器一样的题目。

_DES_KEY = b"IFROMC86"          # kd.nsfc.cn 前端硬编码的 DES 密钥（ECB / PKCS5）


def _des_json(b64txt):
    """kd.nsfc.cn 的检索结果是 base64(DES-ECB) 密文，用它自己的密钥解开"""
    try:
        import _des                                  # 随程序分发的纯 Python DES（无需装库）
    except Exception:
        import pyDes as _des                          # 兜底：环境里装了 pyDes 也行
    d = _des.des(_DES_KEY, _des.ECB, padmode=_des.PAD_PKCS5)
    txt = d.decrypt(base64.b64decode(b64txt)).decode("utf-8", "replace")
    return json.loads(txt)


def _post_multipart(api, fields, referer, timeout=30):
    """按站点前端的方式发 multipart/form-data（SciEngine 检索只认这种）"""
    bd = "----WebKitFormBoundary" + os.urandom(8).hex()
    lines = []
    for k, v in fields.items():
        lines.append("--" + bd)
        lines.append('Content-Disposition: form-data; name="%s"' % k)
        lines.append("")
        lines.append(str(v))
    lines.append("--" + bd + "--")
    lines.append("")
    body = "\r\n".join(lines).encode("utf-8")
    origin = "/".join(referer.split("/")[:3])
    req = urllib.request.Request(api, data=body, headers={
        "User-Agent": UA, "Accept": "application/json, text/plain, */*",
        "Content-Type": "multipart/form-data; boundary=" + bd,
        "Origin": origin, "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return decode_bytes(r.read())


_BROWSER_CACHE = {"path": None, "tried": False}


def _find_browser():
    """找一个能用的 Chromium 内核浏览器（Chrome / Edge / Chromium），用于渲染纯前端站点"""
    if _BROWSER_CACHE["tried"]:
        return _BROWSER_CACHE["path"]
    _BROWSER_CACHE["tried"] = True
    cands = [os.environ.get("REBANG_CHROME") or ""]
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                      os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    else:
        cands += ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
                  "/usr/bin/google-chrome-stable", "/snap/bin/chromium"]
    for p in cands:
        try:
            if p and os.path.isfile(p):
                _BROWSER_CACHE["path"] = p
                break
        except Exception:
            pass
    return _BROWSER_CACHE["path"]


def _render_dom(url, wait_ms=15000, timeout=45):
    """用无头浏览器把页面跑一遍，再把渲染后的 HTML 拿回来（对付 JS 渲染 + JS 人机校验的站）"""
    exe = _find_browser()
    if not exe:
        raise RuntimeError("这个站必须用浏览器内核渲染，但本机没找到 Chrome / Edge")
    import tempfile, shutil
    prof = tempfile.mkdtemp(prefix="rbdom_")
    cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
           "--disable-extensions", "--mute-audio", "--no-sandbox",
           "--user-data-dir=" + prof,
           "--user-agent=" + UA,
           "--disable-blink-features=AutomationControlled",
           "--lang=zh-CN", "--virtual-time-budget=%d" % wait_ms,
           "--dump-dom", url]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout)
        out = decode_bytes(r.stdout or b"")
    except subprocess.TimeoutExpired:
        raise RuntimeError("浏览器渲染超时（%s）" % url)
    finally:
        try:
            shutil.rmtree(prof, ignore_errors=True)
        except Exception:
            pass
    if len(out) < 500:
        raise RuntimeError("浏览器没取到页面内容")
    return out


def p_las(url):
    """中科院文献情报中心「智库报告」：站点有 JS 人机校验，普通请求返回 468，只能渲染后取 DOM"""
    html = _render_dom(url, 15000)
    rows = re.findall(
        r'<em class="title-time">([\d-]{8,10})</em>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    out, seen = [], set()
    for date, href, raw in rows:
        t = _strip_tags(raw)
        t = re.sub(r"\s{2,}", " ", t).strip()
        if len(t) < 6 or t in seen:
            continue
        seen.add(t)
        out.append((date, mk_item(t, _html.unescape(href), None, "", date[:7], base=url)))
    if not out:
        raise RuntimeError("智库报告页没解析到条目（站点可能改了结构）")
    out.sort(key=lambda x: x[0], reverse=True)
    return finish([x[1] for x in out], sort_by_heat=False)


def p_nstl(url):
    """NSTL 产业统计数据库：React 前端，首页「资源推荐」是馆藏报告卡片"""
    html = _render_dom(url, 12000)
    arts = re.findall(r'<article>(.*?)</article>', html, re.S)
    out, seen = [], set()
    for a in arts:
        m1 = re.search(r'<h3[^>]*>(.*?)</h3>', a, re.S)
        if not m1:
            continue
        t = _strip_tags(m1.group(1))
        if len(t) < 4 or t in seen:
            continue
        seen.add(t)
        m2 = re.search(r'class="resource-search"[^>]*href="([^"]+)"', a) or \
            re.search(r'href="([^"]+)"[^>]*class="resource-search"', a)
        link = _html.unescape(m2.group(1)) if m2 else url
        m3 = re.search(r"出版来源：\s*([^<]+)", a)
        m4 = re.search(r"出版年份：\s*([^<]+)", a)
        if not (m3 or m4):
            continue                      # 介绍性 section 也会用 <article>，这里只留真正的资源卡片
        extra = " · ".join([x for x in ((m3.group(1).strip() if m3 else ""),
                                        (m4.group(1).strip() if m4 else "")) if x])
        out.append(mk_item(t, link, None, "", extra))
    if not out:
        raise RuntimeError("NSTL 首页没解析到资源条目")
    return finish(out, sort_by_heat=False)


SCI_API = "https://www.sciengine.com/sci-open/api/v1/open/SciSearch/searchNew"


def p_sci(url):
    """SciEngine 检索：POST /sci-open/api/v1/open/SciSearch/searchNew（返回 JSON，按出版日期倒序）"""
    m = re.search(r"[?&]queryField_a=([^&#]+)", url)
    kw = urllib.parse.unquote(m.group(1)) if m else "经济"
    kw = (kw or "经济").strip() or "经济"
    ref = "https://www.sciengine.com/plat/search?queryField_a=" + urllib.parse.quote(kw)
    txt = _post_multipart(SCI_API, {"queryField_a": kw, "page": "1", "size": "30"}, ref)
    try:
        j = json.loads(txt)
    except Exception:
        raise RuntimeError("SciEngine 接口返回格式异常")
    recs = ((j.get("data") or {}).get("records") or [])
    if not recs:
        raise RuntimeError("SciEngine 检索「%s」没有结果" % kw)
    rows = []
    for r in recs:
        title = _cn_cut(r.get("titleZh") or "") or clean_text(r.get("titleEn") or "")
        doi = (r.get("doi") or "").strip()
        if len(title) < 4 or not doi:
            continue
        date = ""
        try:
            date = time.strftime("%Y-%m-%d", time.localtime(int(r.get("pubDate") or 0) / 1000))
        except Exception:
            date = ""
        extra = " · ".join([x for x in ((r.get("journalTitleZh") or "").strip(), date[:7]) if x])
        rows.append((date, mk_item(title, "https://www.sciengine.com/doi/" + doi, None, "", extra)))
    if not rows:
        raise RuntimeError("SciEngine 检索「%s」未解析到文章" % kw)
    rows.sort(key=lambda x: x[0], reverse=True)          # 按发布时间倒序，不按热度
    return finish([x[1] for x in rows], sort_by_heat=False)


KD_API = "https://kd.nsfc.cn/api/baseQuery/search"


def p_kd(url):
    """国家自然科学基金·成果检索：POST /api/baseQuery/search，响应为 DES 密文，解出来就是题录"""
    m = re.search(r"[?&]s=([^&#]+)", url)
    kw = urllib.parse.unquote(m.group(1)) if m else "经济"
    kw = (kw or "经济").strip() or "经济"
    body = {"authors": "", "baseSearchSign": "", "bookName": "", "fuzzyKeyword": kw,
            "grantCode": "", "issuedBy": "", "journalName": "", "length": 30,
            "orderBy": "paperVisits", "orderType": "desc", "orgCode": "", "patentNo": "",
            "patentType": "", "prjZhTitle": "", "proceedingName": "", "publishYear": "",
            "publisher": "", "rewardRank": "", "rewardTypeName": "", "startPoint": 0,
            "subjectCode1": "", "zhTitle": "", "hasFullText": False, "issuedByScreenin": ""}
    ref = "https://kd.nsfc.cn/resultSearchList?s=" + urllib.parse.quote(kw)
    arr = []
    for start in (0, 10):                      # 接口一页只回 10 条，翻两页凑够 20
        body["startPoint"] = start
        try:
            req = urllib.request.Request(KD_API, data=json.dumps(body).encode("utf-8"), headers={
                "User-Agent": UA, "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://kd.nsfc.cn", "Referer": ref})
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = decode_bytes(r.read()).strip()
            if raw.startswith("{"):
                j = json.loads(raw)            # 偶尔不加密，兼容一下
            else:
                j = _des_json(raw)
            page = ((j.get("data") or {}).get("all_achievement") or [])
        except Exception:
            if start == 0:
                raise
            break                              # 第二页拿不到不影响第一页
        if not page:
            break
        arr += page
        if len(page) < 10:
            break
    if not arr:
        raise RuntimeError("基金成果检索「%s」没有结果" % kw)
    rows = []
    for r in arr:
        title = _cn_cut(r.get("zhTitle") or "") or clean_text(r.get("enTitle") or "")
        if len(title) < 4:
            continue
        rid = (r.get("id") or "").strip()
        link = ("https://kd.nsfc.cn/resultDetails?id=" + urllib.parse.quote(rid)) if rid else ref
        extra = " · ".join([x for x in ((r.get("publishYear") or "").strip(),
                                        (r.get("journalName") or "").strip()) if x])
        rows.append(((r.get("publishYear") or ""), mk_item(title, link, None, "", extra)))
    if not rows:
        raise RuntimeError("基金成果检索「%s」未解析到题录" % kw)
    rows.sort(key=lambda x: x[0], reverse=True)           # 按发表年份倒序
    return finish([x[1] for x in rows], sort_by_heat=False)


# ---------------- V7.9.14：权威站点专用解析（只取栏目里的新闻题目，别把导航/目录当新闻） ----------------
def p_pbc(url):
    """中国人民银行·沟通交流>新闻：表格列表，题目 + span.hui12 日期"""
    html = http_text(url, referer="https://www.pbc.gov.cn/", timeout=20)
    out = []
    for m in re.finditer(r'<a href="(/[^"]+)"[^>]*\btitle="([^"]+)"[^>]*>[^<]*</a>\s*'
                         r'</font>\s*<span class="hui12">([\d-]{8,10})</span>', html):
        out.append(mk_item(_rss_unescape(m.group(2)), m.group(1), None, "",
                           m.group(3), "https://www.pbc.gov.cn/"))
    if not out:
        raise RuntimeError("央行新闻栏目未解析到条目")
    return finish(out, sort_by_heat=False)


def p_mof(url):
    """财政部·新闻报道>政策发布：ul.xwfb_listbox 的 li>a[title]+span 日期"""
    html = http_text(url, referer="http://www.mof.gov.cn/", timeout=20)
    out = []
    for m in re.finditer(r'<li>\s*<a href="([^"]+)"[^>]*\btitle=[\'"]([^\'"]+)[\'"][^>]*>'
                         r'[^<]*</a>\s*<span>([\d-]{8,10})</span>\s*</li>', html):
        out.append(mk_item(_rss_unescape(m.group(2)), m.group(1), None, "",
                           m.group(3), "http://www.mof.gov.cn/"))
    if not out:
        raise RuntimeError("财政部政策发布栏目未解析到条目")
    return finish(out, sort_by_heat=False)


def p_caixin_econ(url):
    """财新·经济频道：h3/h4 里的正文标题（头条 + 要闻列表），带发布时间与配图"""
    html = http_text(url, referer="https://www.caixin.com/", timeout=20)
    out, seen = [], set()
    for m in re.finditer(r'<h[34]><a href="(https://economy\.caixin\.com/[^"]+)"[^>]*>(.*?)</a>', html):
        link = m.group(1)
        if link in seen:
            continue
        seen.add(link)
        t = _rss_unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        win = html[m.end():m.end() + 500]
        dm = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})", win)
        imgs = re.findall(r'src="(https://img\.caixin\.com/[^"]+)"', html[max(0, m.start() - 900):m.start()])
        out.append(mk_item(t, link, None, imgs[-1] if imgs else "",
                           dm.group(1) if dm else ""))
    if not out:
        raise RuntimeError("财新经济频道未解析到条目")
    return finish(out, sort_by_heat=False)


def p_cctv_econ(url):
    """央视经济频道：走站点自己的经济资讯 JSONP（页面正文是 JS 加载的）"""
    txt = http_text(url, referer="https://jingji.cctv.com/", timeout=20)
    i, j = txt.find("("), txt.rindex(")")
    if i < 0 or j <= i:
        raise RuntimeError("不是 JSONP 格式")
    data = json.loads(txt[i + 1:j])
    rows = (data.get("data") or {}).get("list") or []
    out = []
    for it in rows:
        out.append(mk_item(it.get("title"), it.get("url"), None, it.get("image") or "",
                           (it.get("focus_date") or "")[:16]))
    if not out:
        raise RuntimeError("央视经济资讯接口未返回条目")
    return finish(out, sort_by_heat=False)


def p_fangchan(url):
    """中房网·新闻：只取 /news/<栏目>/<日期>/<id>.html 这类正文链接，按日期倒序"""
    html = http_text(url, referer="http://www.fangchan.com/", timeout=20)
    out, seen = [], set()
    for m in re.finditer(r"<a href='(/news/\d+/(\d{4}-\d{2}-\d{2})/[^']+\.html)'"
                         r"(?:[^>]*title='([^']*)')?[^>]*>([^<]*)</a>", html):
        link, day = m.group(1), m.group(2)
        if link in seen:
            continue
        title = _rss_unescape((m.group(3) or m.group(4) or "").strip())
        if len(title) < 6:
            continue
        seen.add(link)
        out.append((day, mk_item(title, link, None, "", day, "http://www.fangchan.com")))
    if not out:
        raise RuntimeError("中房网新闻页未解析到条目")
    out.sort(key=lambda x: x[0], reverse=True)
    return finish([x[1] for x in out], sort_by_heat=False)


def p_mur(url):
    """现代城市研究·检索浏览（土地与房地产）：条目为 <b>题目</b> [作者] + >>详细内容 链接"""
    html = http_text(url, referer="http://www.mur.cn/", timeout=20)
    out = []
    for m in re.finditer(r'<label>\s*·\s*<b>(.*?)</b>(?:\s*\[<span class="author">(.*?)</span>\])?'
                         r'[\s\S]{0,4000}?<div class="more"><a href="([^"]+)"', html):
        out.append(mk_item(_rss_unescape(m.group(1)).strip(), m.group(3), None, "",
                           (m.group(2) or "").strip(), "http://www.mur.cn"))
    if not out:
        raise RuntimeError("现代城市研究检索页未解析到条目")
    return finish(out, sort_by_heat=False)


def p_planners(url):
    """规划师：首页/杂志页找最新一期 → 打开该期「杂志目录导读」取栏目下的文章题目"""
    idx = http_text(url, referer="http://www.planners.com.cn/", timeout=20)
    s = re.search(r"/magazine/show/(\d+)\.html", idx)
    if not s:
        raise RuntimeError("规划师杂志页没找到最新一期")
    lab = re.search(r'title="(\d{4}年第\d+期)"', idx)
    issue = lab.group(1) if lab else ""
    page = http_text("http://www.planners.com.cn/magazine/show/%s.html" % s.group(1),
                     referer=url, timeout=20)
    out, sec = [], ""
    for m in re.finditer(r'<h4[^>]*>&middot;(.*?)</h4>|<a href="/magazine/article/(\d+)\.html">(.*?)</a>', page):
        if m.group(1):
            sec = m.group(1).strip()
            continue
        extra = " · ".join(x for x in (issue, sec) if x)
        out.append(mk_item(_rss_unescape(m.group(3)).strip(),
                           "/magazine/article/%s.html" % m.group(2), None, "", extra,
                           "http://www.planners.com.cn"))
    if not out:
        raise RuntimeError("规划师本期目录导读未解析到条目")
    return finish(out, sort_by_heat=False)


def p_stats(url):
    """国家统计局「数据发布」栏目：列表 li > a[title] + span 日期（相对链接按当前页解析）"""
    html = http_text(url, referer="https://www.stats.gov.cn/", timeout=20)
    out, seen = [], set()
    for m in re.finditer(r"<li>\s*<a[^>]+href=\"([^\"]+)\"[^>]*title='([^']{4,120})'", html):
        href, title = m.group(1), _rss_unescape(m.group(2)).strip()
        if not title or title in seen:
            continue
        seen.add(title)
        tail = html[m.end():m.end() + 600]
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", tail)
        out.append(mk_item(title, href, None, "", dm.group(1) if dm else "", url))
    if not out:
        raise RuntimeError("统计局数据发布页未解析到条目")
    return finish(out, sort_by_heat=False)


def p_52pojie(url):
    """吾爱破解版块列表（Discuz）：每帖一个 tbody，标题在 a.s.xst，回复数在 td.num"""
    html = http_text(url, referer="https://www.52pojie.cn/", timeout=20)
    out = []
    for m in re.finditer(r'<tbody id="normalthread_(\d+)">([\s\S]*?)</tbody>', html):
        blk = m.group(2)
        tm = re.search(r'class="s xst"[^>]*>([^<]{4,120})</a>', blk)
        if not tm:
            continue
        title = _rss_unescape(clean_text(tm.group(1)))
        hm = re.search(r'<td class="num">[\s\S]*?<em>(\d+)</em>', blk)
        heat = int(hm.group(1)) if hm else None
        dm = re.search(r"<em><span>(\d{4}-\d{2}-\d{2})", blk)
        out.append(mk_item(title, "thread-%s-1-1.html" % m.group(1), heat, "",
                           dm.group(1) if dm else "", url))
    if not out:
        raise RuntimeError("吾爱破解列表未解析到帖子")
    return finish(out, sort_by_heat=False)


def p_sucaizy(url):
    """素材资源：首页「最新发布」列表（posts.posts-item 里的 h2 标题）"""
    html = http_text(url, referer="https://www.sucaizy.com/", timeout=20)
    root = parse_html(html)
    out = []
    for b in select_all(root, "posts.posts-item")[:60]:
        a = select_one(b, "h2.item-heading a") or select_one(b, "h2 a")
        if a is None:
            continue
        title = a.get("title") or a.text(120)
        if len(clean_text(title)) < 5:
            continue
        heat = _heat_from(b.text(300), title)
        out.append(mk_item(title, a.get("href"), heat, node_img(b), "", url))
    if not out:
        raise RuntimeError("素材资源最新发布未解析到条目")
    return finish(out, sort_by_heat=False)


PARSERS = {
    "baidu": p_baidu, "weibo": p_weibo, "bilibili": p_bilibili, "github": p_github,    "crossref": p_crossref, "pubmed": p_pubmed, "gitea": p_gitea,
    "d423": p_d423, "ypojie": p_ypojie, "cr173": p_cr173, "baicaio": p_baicaio,
    "toutiao": p_toutiao, "tophub": p_tophub, "qqnews": p_qqnews, "ithome": p_ithome,
    "sspai": p_sspai, "zhihudaily": p_zhihudaily,
    "rss": p_rss, "juejin": p_juejin, "douban_movie": p_douban_movie, "yicai": p_yicai,
    "jsonp": p_jsonp, "autohome": p_autohome, "oscar": p_oscar,
    "360news": p_360news, "sse": p_sse, "modelscope": p_modelscope,
    "weread": p_weread,
    # V7.9.3：学术期刊官网（当期目录 / 最新论文 / 通知公告 —— 取文章题目，按发布时间排序）
    "iwep": p_iwep, "ajcass": p_ajcass, "jryj": p_jryj, "cie": p_cie,
    "cfrn": p_cfrn, "nopss": p_nopss, "gjjryj": p_gjjryj,
    # V7.9.6：前端渲染站改走官方接口（SciEngine 检索 / 基金成果检索）
    "sci": p_sci, "kd": p_kd,
    # V7.9.6：只能渲染的站（JS 人机校验 / React 前端）——用本机浏览器内核跑完再取 DOM
    "las": p_las, "nstl": p_nstl,
    # V7.9.14：权威站点专用解析（央行/财政部/财新/央视经济/中房网/现代城市研究/规划师）
    "pbc": p_pbc, "mof": p_mof, "caixin": p_caixin_econ, "cctv_econ": p_cctv_econ,
    "fangchan": p_fangchan, "mur": p_mur, "planners": p_planners,
    # V8.0.2：国家统计局数据发布 / 吾爱破解（Discuz 版块列表）
    "stats": p_stats, "52pojie": p_52pojie, "sucaizy": p_sucaizy,
}


def run_parser(src, url=None):
    url = url or src.get("url") or ""
    pid = (src.get("parser") or "auto").lower()
    if not url and pid != "oscar":   # 静态数据源可无地址
        raise RuntimeError("未填写地址")
    base = "%s://%s" % (urllib.parse.urlparse(url).scheme, urllib.parse.urlparse(url).netloc)
    sel = src.get("sel") or {}

    def _try_fetch(u, p, sl):
        """M5.8/V7.5：参数化单次抓取（主地址/备用通道共用），失败先预热首页再重试一次"""
        b = "%s://%s" % (urllib.parse.urlsplit(u).scheme, urllib.parse.urlsplit(u).netloc)

        def _once():
            if sl:
                # 有些站点（如值得买）带上 Referer 反而会被风控，先裸请求再带来源重试
                html = http_text(u, timeout=18)
                if len(html) < 2500:
                    html = http_text(u, referer=b + "/", timeout=18)
                if len(html) < 2500:
                    raise RuntimeError("页面被拦截或内容过短（%d 字节）" % len(html))
                return parse_by_sel(parse_html(html), u, sl)
            if p in PARSERS:
                return PARSERS[p](u)
            if p == "json":
                return generic_json(u, b)
            if p == "html":
                return generic_html(u, b)
            try:
                return generic_json(u, b)
            except Exception:
                return generic_html(u, b)

        try:
            return _once()
        except Exception as first:
            # 部分站点校验 Referer/Cookie：先访问首页预热，再重试一次
            try:
                fetch_bytes(b + "/", timeout=10)
                return _once()
            except Exception:
                raise first

    # M5.8/V7.5：多通道降级——主地址失败后依次尝试 alt 备用通道（"备用地址" 或 "备用地址|解析器"）
    cands = [(url, pid, sel)]
    for _a in (src.get("alt") or []):
        _a = str(_a or "").strip()
        if not _a:
            continue
        _u, _, _p = _a.partition("|")
        cands.append((_u.strip(), _p.strip() or pid, {}))
    _err = None
    for _cu, _cp, _cs in cands:
        try:
            items = _try_fetch(_cu, _cp, _cs)
            break
        except Exception as _e:
            _err = _e
    else:
        raise _err if _err else RuntimeError("全部通道失败")
    if not items:
        raise RuntimeError("解析结果为空")
    return items


# ---------------------------------------------------------------- 缓存

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.load()

    def load(self):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in (raw.get("data") or {}).items():
                if isinstance(v, dict) and v.get("items"):
                    self.data[k] = {"items": v["items"], "ts": v.get("ts", 0), "error": ""}
        except Exception:
            pass

    def save(self):
        try:
            with self.lock:
                payload = {"ts": time.time(), "data": self.data}
                # 写入也必须在锁内：多线程共用同一个 .tmp 文件会互相冲突
                tmp = CACHE_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp, CACHE_PATH)
        except Exception as e:
            log("写缓存失败: %r" % (e,))

    def get(self, sid):
        with self.lock:
            return self.data.get(sid)

    def put(self, sid, items, error=""):
        with self.lock:
            self.data[sid] = {"items": items, "ts": time.time(), "error": error}


STORE = Store()
REFRESH_GAP = 150


# ---------------------------------------------------------------- V8.0.5 题目缓存（AI 数据源）
# 每天一个文件夹 titles/YYYY-MM-DD/，每个分类一个 .jsonl + 当天合并 _all.jsonl。
# 每行一条 JSON：{"ts","cat","src","sid","title","url","heat"}，
# 供 AI / 智能体（WorkBuddy 等）直接按行流式读取做趋势分析、每日推送、标的关联等。
# 写盘走后台队列线程（2 秒一批），绝不阻塞抓取与页面显示。

_TL_LOCK = threading.Lock()
_TL_SEEN = {"day": "", "dir": "", "urls": set()}   # 当天已记录 url（重启后从磁盘补齐，跨天/换目录自动重置）
_TL_QUEUE = []                          # 待写缓冲：[(day, ts_str, cats, sname, sid, title, url, heat)]
_TL_CLEAN = {"day": ""}                 # 每天清理一次过期文件夹


def titles_dir():
    d = str(CFG.settings.get("titlesDir") or "").strip()
    if not d:
        d = os.path.join(data_dir(), "titles")
    return d


def _tl_safe_name(cat):
    name = re.sub(r'[\\/:*?"<>|\r\n]', "_", str(cat or "未分类")).strip()
    return (name or "未分类")[:40]


def _tl_load_seen(day):
    """重启后把当天已有记录的 url 读进内存，避免重复落盘"""
    urls = set()
    d = os.path.join(titles_dir(), day)
    try:
        for fn in os.listdir(d):
            if not fn.endswith(".jsonl"):
                continue
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        urls.add(json.loads(line).get("url"))
                    except Exception:
                        pass
    except Exception:
        pass
    return urls


def titles_log(src, items):
    """把一个源刚抓到的题目排入写盘队列（去重：同一天同一条 url 只记一次）"""
    try:
        if not items or not isinstance(items, list):
            return
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        cats = split_cat(src.get("cat")) or ["未分类"]
        sname = str(src.get("name") or src.get("id") or "")[:40]
        sid = str(src.get("id") or "")[:40]
        with _TL_LOCK:
            _cur_dir = titles_dir()
            if _TL_SEEN["day"] != day or _TL_SEEN.get("dir") != _cur_dir:
                # V8.0.6：换目录也要重新补齐去重集合，否则切目录后同一天会重复落盘
                _TL_SEEN["day"] = day
                _TL_SEEN["dir"] = _cur_dir
                _TL_SEEN["urls"] = _tl_load_seen(day)
            seen = _TL_SEEN["urls"]
            rows = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                u = str(it.get("url") or "").strip()
                if not u or u in seen:
                    continue
                seen.add(u)
                rows.append((day, ts, cats, sname, sid,
                             str(it.get("title") or "")[:160], u, it.get("heat")))
            if rows:
                _TL_QUEUE.extend(rows)
    except Exception:
        pass


def titles_ensure_readme():
    """缓存根目录放一份给 AI 看的数据说明，智能体读到它就知道字段怎么用"""
    try:
        root = titles_dir()
        os.makedirs(root, exist_ok=True)
        p = os.path.join(root, "README-AI.txt")
        if os.path.exists(p):
            return
        txt = (
            "热榜助手 · 题目缓存数据说明（给 AI / 智能体）\n"
            "=============================================\n"
            "目录结构：titles/YYYY-MM-DD/<分类>.jsonl（每天一个文件夹，按分类分文件）\n"
            "          titles/YYYY-MM-DD/_all.jsonl（当天全部分类合并流，一次读一天）\n"
            "文件格式：JSONL，每行一条独立的 JSON 对象，可直接按行流式解析。\n"
            "字段说明：\n"
            '  ts    抓取时间，如 "2026-11-20 09:31:05"\n'
            '  cat   新闻源所属分类（多分类用 / 连接），如 "财经"\n'
            '  src   新闻源名称，如 "华尔街见闻"\n'
            '  sid   新闻源内部 id\n'
            '  title 标题（最长 160 字）\n'
            '  url   原文链接（同一天内 url 唯一，可做主键去重）\n'
            '  heat  热度数值（可能为 null）\n'
            "使用建议：\n"
            "  · 分析某天热点：读当天 _all.jsonl 即可。\n"
            "  · 趋势分析：按日期文件夹顺序读多天，统计关键词/标的出现频率变化。\n"
            "  · 标的关联：维护关注词表（公司名/股票简称），每日扫描 title 命中情况。\n"
            "  · 提醒：标题是二手信号，分析结论适合做研究线索参考，不构成投资建议。\n"
        )
        with open(p, "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception:
        pass


def titles_cleanup():
    """按 titlesKeep 清理过期日期文件夹（0=永久保留；每天最多执行一次）"""
    try:
        keep = 0
        try:
            keep = int(CFG.settings.get("titlesKeep") or 0)
        except Exception:
            keep = 0
        if keep <= 0:
            return
        root = titles_dir()
        if not os.path.isdir(root):
            return
        cutoff = time.time() - keep * 86400
        for fn in os.listdir(root):
            d = os.path.join(root, fn)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fn) or not os.path.isdir(d):
                continue
            try:
                if os.path.getmtime(d) < cutoff:
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                    log("题目缓存清理：%s（超过 %d 天）" % (fn, keep))
            except Exception:
                pass
    except Exception:
        pass


def _tl_flush_loop():
    """后台落盘线程：2 秒一批把队列写进 JSONL，写失败只记日志不影响主流程"""
    while True:
        time.sleep(2)
        try:
            with _TL_LOCK:
                if not _TL_QUEUE:
                    continue
                batch, _TL_QUEUE[:] = list(_TL_QUEUE), []
            by_file = {}
            for day, ts, cats, sname, sid, title, url, heat in batch:
                rec = {"ts": ts, "cat": "/".join(cats), "src": sname, "sid": sid,
                       "title": title, "url": url, "heat": heat}
                line = json.dumps(rec, ensure_ascii=False)
                for c in cats:
                    by_file.setdefault((day, _tl_safe_name(c)), []).append(line)
                by_file.setdefault((day, "_all"), []).append(line)
            root = titles_dir()
            for (day, fn), lines in by_file.items():
                d = os.path.join(root, day)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, fn + ".jsonl"), "a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            today = time.strftime("%Y-%m-%d")
            if _TL_CLEAN["day"] != today:
                _TL_CLEAN["day"] = today
                titles_ensure_readme()
                titles_cleanup()
        except Exception as e:
            log("题目缓存写入失败: %r" % (e,))


threading.Thread(target=_tl_flush_loop, daemon=True).start()

# 同一源同时只允许一个线程在抓：定时预取与用户请求不再重复抓同一个源，
# 省下线程池资源，整批速度更快
_INFLIGHT = {}
_INFLIGHT_LK = threading.Lock()


_LAST_STORE_SAVE = [0.0]


def _store_save_throttled():
    """M5.8/V7.5：抓取结果落盘节流（最长 120 秒一次），deadline 快路径的结果也持久化，重启不丢"""
    try:
        if time.time() - _LAST_STORE_SAVE[0] > 120:
            _LAST_STORE_SAVE[0] = time.time()
            STORE.save()
    except Exception:
        pass


def refresh_source(src, force=False):
    sid = src["id"]
    cur = STORE.get(sid)
    # 快路径：有数据直接返回缓存；完全没数据的失败源只缓存 60 秒就允许重抓
    # （优化重新请求时间：坏源/网络抖动 1 分钟内即可自动恢复，而不是干等 150 秒）
    if not force and cur and (cur.get("items") or cur.get("error")):
        gap = REFRESH_GAP if cur.get("items") else 60
        if time.time() - cur["ts"] < gap:
            return cur
    with _INFLIGHT_LK:
        ev = _INFLIGHT.get(sid)
        owner = ev is None
        if owner:
            ev = _INFLIGHT[sid] = threading.Event()
    if not owner:
        ev.wait(25)          # 别人在抓，直接等结果，不重复抓
        return STORE.get(sid)
    items = None
    try:
        # 失败自动重试一次（网络抖动常见，重试能救回一部分源）
        for attempt in range(2):
            try:
                items = run_parser(src)
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(0.6)
                    continue
                msg = str(e) or e.__class__.__name__
                log("[%s] 失败: %s" % (src.get("name"), msg))
                old = STORE.get(sid) or {}
                STORE.put(sid, old.get("items", []), msg)
        if items is not None:
            STORE.put(sid, items, "")
            # V8.0.5：题目入缓存队列（按天 JSONL，供 AI 分析；后台线程落盘不阻塞）
            if items:
                titles_log(src, items)
    finally:
        with _INFLIGHT_LK:
            _INFLIGHT.pop(sid, None)
        ev.set()
        _store_save_throttled()      # M5.8/V7.5：结果落盘（节流）
    return STORE.get(sid)


def refresh_many(sources, force=False, deadline=None):
    """并发抓取多个源。deadline（秒）给定时：到点返回未完成的源列表，
    让调用方决定转后台继续抓，接口不必陪最慢的源干等。"""
    if not sources:
        return []
    if deadline:
        from concurrent.futures import wait as _cf_wait
        ex = ThreadPoolExecutor(max_workers=min(24, len(sources)))
        futs = [ex.submit(refresh_source, s, force) for s in sources]
        _cf_wait(futs, timeout=deadline)
        pending = [s for s, f in zip(sources, futs) if not f.done()]
        for f in futs:
            f.cancel()
        ex.shutdown(wait=False)
        return pending
    with ThreadPoolExecutor(max_workers=min(24, len(sources))) as ex:
        list(ex.map(lambda s: refresh_source(s, force), sources))
    STORE.save()
    return []


# V7.9.2：后台刷新统一走一个共享线程池 + 按源去重。
# 页面会并发发 4~5 个 /api/hot，每个请求都开 24 线程池 → 上百线程抢 GIL，
# 接口响应被拖到 1 秒以上。现在最多 16 个抓取线程，且同一个源不会重复抓。
_REFRESH_EX = ThreadPoolExecutor(max_workers=16)
_REFRESH_SET = set()
_REFRESH_LK = threading.Lock()


def refresh_bg(sources, force=False):
    if not sources:
        return 0
    todo = []
    with _REFRESH_LK:
        for s in sources:
            if s.get("id") in _REFRESH_SET:
                continue
            _REFRESH_SET.add(s["id"])
            todo.append(s)

    def run(src):
        try:
            refresh_source(src, force)
        except Exception:
            pass
        finally:
            with _REFRESH_LK:
                _REFRESH_SET.discard(src.get("id"))
    for s in todo:
        _REFRESH_EX.submit(run, s)
    return len(todo)


def _periodic_refresh():
    """桌面版定时预取：后台强制刷新全部启用源，打开即显示最新缓存，不必等抓取"""
    while True:
        time.sleep(240)
        try:
            srcs = [s for s in CFG.sources if s["enabled"]]
            log("定时预取 %d 个源" % len(srcs))
            refresh_many(srcs, force=True)
        except Exception as e:
            log("定时预取异常: %r" % (e,))


# ---------------------------------------------------------------- 翻译

TRANS_CACHE = {}


def _t_google(text):
    u = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN"
         "&dt=t&q=" + urllib.parse.quote(text))
    raw = http_text(u, timeout=10)
    data = json.loads(raw)
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def _t_mymemory(text):
    u = ("https://api.mymemory.translated.net/get?q=" + urllib.parse.quote(text[:480]) +
         "&langpair=en|zh-CN")
    data = http_json(u, timeout=10)
    return ((data.get("responseData") or {}).get("translatedText") or "").strip()


def _t_baidu_api(text, keys):
    """百度翻译开放平台（免费标准版申请 appid）：keys = 'appid|secret'，未配置走降级链"""
    if not keys:
        raise RuntimeError("百度翻译未配置密钥")
    appid, _, secret = keys.partition("|")
    if not appid or not secret:
        raise RuntimeError("百度翻译密钥格式应为 appid|secret")
    salt = str(int(time.time() * 1000))
    sign = hashlib.md5((appid + text + salt + secret).encode("utf-8")).hexdigest()
    data = http_json("https://fanyi-api.baidu.com/api/trans/vip/translate?"
                     + urllib.parse.urlencode({"q": text, "from": "auto", "to": "zh",
                                               "appid": appid, "salt": salt, "sign": sign}),
                     timeout=10)
    if data.get("error_code"):
        raise RuntimeError("百度翻译 %s" % (data.get("error_msg") or data.get("error_code")))
    return "".join(r.get("dst", "") for r in (data.get("trans_result") or []))


def _t_youdao_api(text, keys):
    """有道智云（免费额度申请 appkey）：keys = 'appkey|secret'"""
    if not keys:
        raise RuntimeError("有道翻译未配置密钥")
    appkey, _, secret = keys.partition("|")
    if not appkey or not secret:
        raise RuntimeError("有道翻译密钥格式应为 appkey|secret")
    salt = str(int(time.time() * 1000))
    cur = str(int(time.time()))
    short = text if len(text) <= 20 else text[:10] + str(len(text)) + text[-10:]
    sign = hashlib.sha256((appkey + short + salt + cur + secret).encode("utf-8")).hexdigest()
    data = http_json("https://openapi.youdao.com/api", timeout=10,
                     data=urllib.parse.urlencode({"q": text, "from": "auto", "to": "zh",
                                                  "appKey": appkey, "salt": salt, "curtime": cur,
                                                  "signType": "v3", "sign": sign}).encode())
    if str(data.get("errorCode", "0")) != "0":
        raise RuntimeError("有道翻译 errorCode=%s" % data.get("errorCode"))
    return "".join(data.get("translation") or [])


def translate_text(text, engine="google", keys=None):
    """多引擎翻译：优先所选引擎，失败自动降级（谷歌→MyMemory）；百度/有道需在设置里填密钥"""
    text = (text or "").strip()
    if not text:
        return ""
    ck = (engine or "google") + "|" + text
    if ck in TRANS_CACHE:
        return TRANS_CACHE[ck]
    if not re.search(r"[A-Za-z]{3}", text):
        return text
    keys = keys if isinstance(keys, dict) else {}
    if engine == "baidu":
        chain = [(_t_baidu_api, "baidu"), (_t_google, None), (_t_mymemory, None)]
    elif engine == "youdao":
        chain = [(_t_youdao_api, "youdao"), (_t_google, None), (_t_mymemory, None)]
    elif engine == "mymemory":
        chain = [(_t_mymemory, None), (_t_google, None)]
    else:
        chain = [(_t_google, None), (_t_mymemory, None)]
    for fn, need in chain:
        if need and not keys.get(need):
            continue
        try:
            r = fn(text, keys.get(need)) if need else fn(text)
            if r and not r.lower().startswith("mymemory") and len(r) > 1:
                TRANS_CACHE[ck] = r
                return r
        except Exception:
            continue
    return ""


def translate_many(texts, engine="google", keys=None):
    texts = [t for t in (texts or []) if isinstance(t, str) and t.strip()][:60]
    out = {}
    if not texts:
        return out
    with ThreadPoolExecutor(max_workers=4) as ex:   # M5.5/V7.2 实测 8 并发会触发上游限流反而变慢，保持 4
        for src, dst in zip(texts, ex.map(lambda t: translate_text(t, engine, keys), texts)):
            if dst:
                out[src] = dst
    return out


# ---------------- V7.8.5：离线标题翻译（Argos 插槽 + CTranslate2 预留 + 本地缓存） ----------------
# 设计：界面层只认 /api/translate(engine="auto")；引擎优先级 = 本地缓存 → Argos 离线 → CTranslate2(预留) → 在线降级链。
# 任何一步失败都静默跳过（前端不显示翻译行、不报错），绝不改动原始标题。

_TRANS_KEYS_FILE = os.path.join(data_dir(), "trans_keys.json")     # 百度/有道密钥（仅本机，不上云）
_TR_CACHE_FILE = os.path.join(data_dir(), "translate_cache.json")  # 标题翻译缓存（本机持久）
_TR_LOCK = threading.Lock()
_TR_DISK = {}          # 标题 -> 中文译文
_TR_KEYS_FILE_DATA = {}  # {baidu: "appid|secret", youdao: "appkey|secret"}


def _tr_load_all():
    global _TR_DISK, _TR_KEYS_FILE_DATA
    try:
        with open(_TR_CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _TR_DISK = {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str) and v}
    except Exception:
        _TR_DISK = {}
    try:
        with open(_TRANS_KEYS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _TR_KEYS_FILE_DATA = {k: str(v) for k, v in d.items() if isinstance(k, str)}
    except Exception:
        _TR_KEYS_FILE_DATA = {}


def _tr_save_cache():
    try:
        if len(_TR_DISK) > 8000:   # 超限只保留最新一半
            keep = list(_TR_DISK.items())[-4000:]
            _TR_DISK.clear()
            _TR_DISK.update(dict(keep))
        tmp = _TR_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_TR_DISK, f, ensure_ascii=False)
        os.replace(tmp, _TR_CACHE_FILE)
    except Exception:
        pass


def trans_keys_get():
    with _TR_LOCK:
        return dict(_TR_KEYS_FILE_DATA)


def trans_keys_set(keys):
    with _TR_LOCK:
        for k, v in (keys or {}).items():
            if k in ("baidu", "youdao") and isinstance(v, str):
                if v.strip():
                    _TR_KEYS_FILE_DATA[k] = v.strip()
                else:
                    _TR_KEYS_FILE_DATA.pop(k, None)
        try:
            tmp = _TRANS_KEYS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_TR_KEYS_FILE_DATA, f, ensure_ascii=False)
            os.replace(tmp, _TRANS_KEYS_FILE)
        except Exception:
            pass
        return dict(_TR_KEYS_FILE_DATA)


# ---------------- V7.9：离线翻译模型（Marian + CTranslate2，Argos 官方 en→zh 包） ----------------
# 模型不打进安装包（68MB），首次在「设置 → 翻译」点「下载离线翻译模型」联网下一份，
# 解压到 <数据目录>/models/ 后永久离线可用；不改动原始标题，失败静默。

OFFLINE_DIR = os.path.join(data_dir(), "models")
OFFLINE_PKG = "translate-en_zh-1_9"
OFFLINE_ROOT = os.path.join(OFFLINE_DIR, OFFLINE_PKG)
OFFLINE_MODEL_URLS = [
    "https://argos-net.com/v1/translate-en_zh-1_9.argosmodel",
]
OFFLINE_TOTAL = 70743021            # 已知体积，用于进度条初值

_OFF = {"state": "idle", "got": 0, "total": OFFLINE_TOTAL, "msg": "", "thread": None}
_OFF_LOCK = threading.Lock()


def offline_runtime():
    """运行库是否可用（ctranslate2 + sentencepiece 是否在当前程序里）。"""
    try:
        import ctranslate2  # noqa: F401
        import sentencepiece  # noqa: F401
        _OFF["rt_err"] = ""
        return True
    except Exception as e:
        _OFF["rt_err"] = "%s: %s" % (e.__class__.__name__, e)
        return False


class _CT2Engine:
    """opus-mt-en-zh + CTranslate2 int8 离线引擎；懒加载，缺模型/缺运行库时静默不可用。"""
    def __init__(self):
        self._lock = threading.Lock()
        self._tr = None
        self._sp = None
        self._prefix = []
        self._bad = ""

    def model_files(self):
        return (os.path.join(OFFLINE_ROOT, "model", "model.bin"),
                os.path.join(OFFLINE_ROOT, "sentencepiece.model"))

    def installed(self):
        m, s = self.model_files()
        return os.path.exists(m) and os.path.exists(s)

    def available(self):
        if self._tr is not None:
            return True
        return self.installed() and offline_runtime()

    def _load(self):
        if self._tr is not None:
            return True
        with self._lock:
            if self._tr is not None:
                return True
            m, s = self.model_files()
            if not (os.path.exists(m) and os.path.exists(s)):
                self._bad = "未下载离线模型"
                return False
            try:
                import ctranslate2
                import sentencepiece as spm
                cpus = os.cpu_count() or 2
                self._tr = ctranslate2.Translator(
                    os.path.dirname(m), device="cpu", compute_type="int8",
                    inter_threads=1, intra_threads=max(2, min(4, cpus)))
                self._sp = spm.SentencePieceProcessor(model_file=s)
                pf = self._sp.encode(">>cmn_Hans<<", out_type=str)
                self._prefix = pf if len(pf) == 1 else []   # 目标语言标记（中文简体）
                self._bad = ""
                return True
            except Exception as e:
                self._bad = str(e) or e.__class__.__name__
                self._tr = None
                return False

    def unload(self):
        with self._lock:
            self._tr = None
            self._sp = None

    def translate_batch(self, texts, beam_size=2):
        texts = [t for t in (texts or []) if isinstance(t, str) and t.strip()]
        if not texts or not self._load():
            return {}
        try:
            toks = [list(self._prefix) + self._sp.encode(t, out_type=str) for t in texts]
            res = self._tr.translate_batch(toks, beam_size=beam_size, max_batch_size=32,
                                           batch_type="examples", max_decoding_length=256)
        except Exception as e:
            self._bad = str(e) or e.__class__.__name__
            self.unload()
            return {}
        out = {}
        for t, r in zip(texts, res):
            try:
                hyp = [x for x in r.hypotheses[0] if x not in ("</s>", "<pad>")]
                s = self._sp.decode(hyp).replace("\u2581", " ")
                s = re.sub(r"\s{2,}", " ", s).strip().strip(" ,.")
                if s and s != t:
                    out[t] = s
            except Exception:
                continue
        return out

    def translate(self, text):
        return self.translate_batch([text], beam_size=1).get(text, "")


ARGOS = _CT2Engine()      # 名字沿用：前端与历史接口不变


def offline_local_size():
    tot = 0
    for root, _, files in os.walk(OFFLINE_ROOT):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return tot


def offline_status():
    with _OFF_LOCK:
        st, got, total, msg = _OFF["state"], _OFF["got"], _OFF["total"], _OFF["msg"]
    if st == "ready" and not ARGOS.installed():
        st = "idle"
    if st == "idle" and ARGOS.installed():
        st = "ready"
    total = int(total or OFFLINE_TOTAL)
    return {"state": st, "got": int(got), "total": total,
            "pct": max(0, min(100, int(100.0 * got / total))) if total else 0,
            "msg": msg, "installed": bool(ARGOS.installed()), "runtime": offline_runtime(),
            "size": offline_local_size(), "engine": "Marian + CTranslate2 (int8)",
            "rtErr": _OFF.get("rt_err", "")}


def _offline_download_worker():
    tmp = os.path.join(OFFLINE_DIR, "_dl.argosmodel")
    try:
        os.makedirs(OFFLINE_DIR, exist_ok=True)
        last = ""
        for url in OFFLINE_MODEL_URLS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    total = int(r.headers.get("Content-Length") or OFFLINE_TOTAL)
                    with _OFF_LOCK:
                        _OFF.update({"state": "downloading", "got": 0, "total": total,
                                     "msg": "正在下载模型"})
                    with open(tmp, "wb") as f:
                        while True:
                            chunk = r.read(262144)
                            if not chunk:
                                break
                            f.write(chunk)
                            with _OFF_LOCK:
                                _OFF["got"] += len(chunk)
                last = ""
                break
            except Exception as e:
                last = str(e) or e.__class__.__name__
        if last:
            raise RuntimeError("下载失败：" + last)
        with _OFF_LOCK:
            _OFF.update({"state": "unpacking", "msg": "正在解压安装"})
        import zipfile
        with zipfile.ZipFile(tmp) as z:
            names = z.namelist()
            root = names[0].split("/")[0] if names else OFFLINE_PKG
            if os.path.isdir(OFFLINE_ROOT):
                shutil.rmtree(OFFLINE_ROOT, ignore_errors=True)
            z.extractall(OFFLINE_DIR)
        src = os.path.join(OFFLINE_DIR, root)
        if os.path.isdir(src) and os.path.abspath(src) != os.path.abspath(OFFLINE_ROOT):
            if os.path.isdir(OFFLINE_ROOT):
                shutil.rmtree(OFFLINE_ROOT, ignore_errors=True)
            os.replace(src, OFFLINE_ROOT)
        try:
            os.remove(tmp)
        except Exception:
            pass
        ARGOS.unload()
        with _OFF_LOCK:
            _OFF.update({"state": "ready", "got": _OFF["total"], "msg": "离线模型已就绪"})
    except Exception as e:
        with _OFF_LOCK:
            _OFF.update({"state": "error", "msg": str(e) or e.__class__.__name__})
    finally:
        with _OFF_LOCK:
            _OFF["thread"] = None


def offline_start_download():
    if not offline_runtime():
        return {"ok": False, "error": "当前程序缺少离线运行库（ctranslate2 / sentencepiece），请用新版安装包"}
    with _OFF_LOCK:
        if _OFF["state"] in ("downloading", "unpacking"):
            return {"ok": True, "state": _OFF["state"]}
        _OFF.update({"state": "downloading", "got": 0, "total": OFFLINE_TOTAL,
                     "msg": "正在连接"})
        t = threading.Thread(target=_offline_download_worker, daemon=True)
        _OFF["thread"] = t
        t.start()
    return {"ok": True, "state": "downloading"}


def offline_delete():
    try:
        ARGOS.unload()
        shutil.rmtree(OFFLINE_ROOT, ignore_errors=True)
        with _OFF_LOCK:
            _OFF.update({"state": "idle", "got": 0, "msg": ""})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def offline_tr_status():
    try:
        return {"argos": bool(ARGOS.available()), "model": bool(ARGOS.installed()),
                "runtime": offline_runtime(), "state": _OFF["state"]}
    except Exception:
        return {"argos": False, "model": False, "runtime": False, "state": "idle"}


def translate_titles(texts, keys=None, engine="auto"):
    """新闻标题批量翻译：本地缓存 → 离线模型（Marian + CTranslate2）→ 在线降级链（失败重试一轮）。
    只返回成功项，失败项留给前端下一轮再补（不显示空翻译行，也不改原文）。"""
    req, seen = [], set()
    for t in (texts or []):
        if isinstance(t, str) and t.strip() and t not in seen:
            seen.add(t)
            req.append(t.strip())
    if not req:
        return {}
    out = {}
    with _TR_LOCK:
        for t in req:
            v = _TR_DISK.get(t)
            if v:
                out[t] = v
    rest = [t for t in req if t not in out]
    # 1) 离线模型（已下载时整批推理，几百毫秒出结果，不联网）
    if rest and engine in ("auto", "argos") and ARGOS.installed():
        try:
            out.update(ARGOS.translate_batch(rest, beam_size=2))
        except Exception:
            pass
        rest = [t for t in rest if t not in out]
    # 2) 在线降级链（没装离线模型 / 离线失败时）：失败再来一轮，避免「只翻前几条」
    if rest and engine in ("auto", "google", "baidu", "youdao", "mymemory"):
        eng = "google" if engine == "auto" else engine
        kk = trans_keys_get()
        if isinstance(keys, dict):
            kk.update(keys)              # 前端传入的密钥优先
        for attempt in range(2):
            if not rest:
                break
            try:
                got = translate_many(rest, eng, kk)
            except Exception:
                got = {}
            if got:
                out.update(got)
                rest = [t for t in rest if t not in out]
            if rest and attempt == 0:
                time.sleep(0.35)
    if out:
        with _TR_LOCK:
            for k, v in out.items():
                if v:
                    _TR_DISK[k] = v
            _tr_save_cache()
    return out


_tr_load_all()   # 启动即加载翻译缓存与密钥文件（静默，文件缺失不报错）


# ---------------------------------------------------------------- 页面

def load_page():
    env_ui = os.environ.get("REBANG_UI")
    if env_ui:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), env_ui)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    if os.environ.get("REBANG_DEV"):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui2.html")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    try:
        from ui_embed import HTML_B64
        return gzip.decompress(base64.b64decode(HTML_B64)).decode("utf-8")
    except Exception:
        pass
    for p in (os.path.join(exe_dir(), "ui.html"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")):
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    return "<h1>界面文件缺失</h1>"


PAGE = load_page()

# ---------------------------------------------------------------- 图片

IMG_CACHE = {}
IMG_CACHE_MAX = 420
PRIVATE_RE = re.compile(r"^(localhost|127\.|10\.|192\.168\.|169\.254\.|0\.|\[?::1)")


def img_fetch(url, referer=""):
    if not re.match(r"^https?://", url or "", re.I):
        return None
    host = urllib.parse.urlparse(url).hostname or ""
    if PRIVATE_RE.match(host):
        return None
    hit = IMG_CACHE.get(url)
    if hit and time.time() - hit[0] < 1800:
        return hit[1], hit[2]
    raw, _ = fetch_bytes(url, referer=referer or ("https://%s/" % host), timeout=12)
    low = url.lower()
    ctype = "image/jpeg"
    for ext, ct in ((".png", "image/png"), (".gif", "image/gif"), (".webp", "image/webp"),
                    (".svg", "image/svg+xml"), (".ico", "image/x-icon")):
        if low.endswith(ext):
            ctype = ct
            break
    if len(IMG_CACHE) > IMG_CACHE_MAX:
        IMG_CACHE.clear()
    IMG_CACHE[url] = (time.time(), raw, ctype)
    return raw, ctype


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "RebangHelper/" + APP_VER
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        hdr = dict(extra or {})
        # V7.9.2：文本响应走 gzip。config 137KB→19KB、hot 290KB→78KB，
        # 手机流量和首屏等待都能省一大半（图片/音频等二进制不动）
        try:
            ae = (self.headers.get("Accept-Encoding") or "").lower()
        except Exception:
            ae = ""
        if (code == 200 and len(body) >= 700 and "gzip" in ae
                and (ctype.startswith("application/json") or ctype.startswith("text/")
                     or ctype.startswith("application/javascript"))
                and "Content-Encoding" not in hdr):
            try:
                body = gzip.compress(body, 6)
                hdr["Content-Encoding"] = "gzip"
                hdr["Vary"] = "Accept-Encoding"
            except Exception:
                pass
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in hdr.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8", extra)

    def _fb_list(self, qs):
        """建议/反馈列表：mode = public(已审核) | mine(我的) | admin(审核台，仅管理员)。GET/POST 共用。"""
        mode = (qs.get("mode") or ["public"])[0]
        is_admin = bool(ACCOUNT.user and
                        ACCOUNT.user.get("email") == FEEDBACK_ADMIN_EMAIL)
        if mode == "admin" and not is_admin:
            self._json({"ok": False, "error": "仅管理员可查看审核台"})
            return
        if mode == "mine":
            if not ACCOUNT.user:
                self._json({"ok": True, "items": [], "isAdmin": False})
                return
            q = ("/v1/rdb/rest/feedback?user_id=eq.%s&order=created_at.desc&limit=100"
                 % urllib.parse.quote(str(ACCOUNT.user["sub"])))
        else:
            # admin：全部（含待审核）；public：仅已通过（含作者回复）
            q = "/v1/rdb/rest/feedback?order=created_at.desc&limit=100"
            if mode != "admin":
                q = ("/v1/rdb/rest/feedback?status=eq.approved&deleted=eq.false"
                     "&order=created_at.desc&limit=100")
        ok, r = _cb_request("GET", q,
                            token=ACCOUNT.access_token if ACCOUNT.user else None)
        if not ok:
            self._json({"ok": False, "error": r.get("error") or "加载失败"})
            return
        items = r if isinstance(r, list) else (r.get("data") or [])
        self._json({"ok": True, "items": items, "isAdmin": is_admin})

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path, qs = p.path, urllib.parse.parse_qs(p.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path in ("/dl", "/download"):
                # 手机下载页：静态托管上的安装包页面（网页版入口处也给这个短链）
                self.send_response(302)
                self.send_header("Location", DL_PAGE_URL)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/api/feedback/list":
                self._fb_list(qs)
            elif path == "/api/auth/me":
                # M8.0：当前登录态（前端启动时调用；含云端是否有配置 / 本机是否全新安装）
                logged = ACCOUNT.resume()
                has_cloud = None
                if logged:
                    okc, ccfg = ACCOUNT.cloud_pull()
                    has_cloud = bool(okc and ccfg)
                self._json({"ok": True, "logged": logged, "user": ACCOUNT.user,
                            "admin": is_admin(),
                            "hasCloudCfg": has_cloud, "fresh": bool(CFG.is_new)})
            elif path == "/api/ad":
                # V7.9.4：推广位内容（公开只读，所有端拉同一份；写入需作者登录）
                self._json({"ok": True, "ad": ad_block(), "ts": time.time()},
                           extra={"Access-Control-Allow-Origin": "*"})
            elif path == "/api/cert/list":
                # V7.8.1：证书修复面板数据（哪些站点证书链有问题、下载/安装状态）
                self._json({"ok": True, "items": cert_issues_payload()})
            elif path == "/api/offline/status":
                self._json({"ok": True, "status": offline_status()})
            elif path == "/api/trans/keys":
                # V7.8.5：读取本机保存的百度/有道密钥（启动时前端自动回填 localStorage）
                self._json({"ok": True, "keys": trans_keys_get()})
            elif path == "/api/config":
                self._json({"app": APP_VER, "author": AUTHOR, "name": APP_NAME,
                            "settings": CFG.settings, "sources": CFG.sources,
                            "favorites": CFG.favorites, "history": CFG.history,
                            "dataDir": data_dir()})
            elif path == "/api/titles/info":
                # V8.0.5：题目缓存目录信息（前端设置页显示 + 一键复制）
                # V8.0.6：任何异常都不许让前端只看到「服务未响应」——目录必须回给前端
                try:
                    d = titles_dir()
                    try:
                        os.makedirs(d, exist_ok=True)
                    except Exception:
                        pass
                    try:
                        files = sorted([x for x in os.listdir(d)
                                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x)])[-7:] \
                                if os.path.isdir(d) else []
                    except Exception:
                        files = []
                    self._json({"ok": True, "dir": d, "keep": int(CFG.settings.get("titlesKeep") or 0),
                                "days": files, "desktop": True, "dataDir": data_dir(),
                                "exists": os.path.isdir(d)})
                except Exception as e:
                    log("titles/info 失败: %r" % (e,))
                    self._json({"ok": False, "error": str(e)[:120],
                                "dir": os.path.join(data_dir(), "titles"),
                                "keep": 0, "desktop": True, "dataDir": data_dir()})
            elif path == "/api/hot":
                ids = [x for x in (qs.get("ids", [""])[0] or "").split(",") if x]
                force = qs.get("force", ["0"])[0] == "1"
                srcs = [s for s in CFG.sources if s["id"] in ids and s["enabled"]]
                # 缓存秒开：有缓存的源立即返回旧内容（哪怕已过期），
                # 过期源转后台线程刷新；只有"完全没数据"的源才现场等待抓取。
                ready, stale, out = [], [], {}
                # V7.9.2：等源的时间可调，并且最近抓失败的源不再现场等（反正还是失败），
                # 只丢后台重试——否则一个坏源能把整批首屏拖到 7 秒。
                try:
                    wait_s = max(0.0, min(6.0, float(qs.get("wait", ["2.5"])[0])))
                except Exception:
                    wait_s = 2.5
                for s in srcs:
                    cur = STORE.get(s["id"]) or {}
                    items = cur.get("items") or []
                    if (not force and not items and cur.get("error")
                            and time.time() - cur.get("ts", 0) < 90):
                        out[s["id"]] = {"items": [], "error": cur.get("error", ""),
                                        "ts": cur.get("ts", 0)}
                        stale.append(s)
                        continue
                    if force or not items:
                        ready.append(s)
                    else:
                        out[s["id"]] = {"items": items[:120],
                                        "error": cur.get("error", ""), "ts": cur.get("ts", 0)}
                        if time.time() - cur.get("ts", 0) > REFRESH_GAP:
                            stale.append(s)
                if ready:
                    # 限时抓取：最多等 7 秒，没抓完的转后台继续，接口不陪慢源干等
                    pending = refresh_many(ready, force=force, deadline=wait_s)
                    for s in ready:
                        cur = STORE.get(s["id"]) or {}
                        out[s["id"]] = {"items": (cur.get("items") or [])[:120],
                                        "error": cur.get("error", ""), "ts": cur.get("ts", 0)}
                    if pending:
                        refresh_bg(pending, force=force)
                        stale.extend(pending)
                if stale and not force:
                    refresh_bg(stale)
                self._json({"ok": True, "sources": out, "now": time.time(),
                            "stale": [s["id"] for s in stale if not force]})
            elif path == "/api/test":
                url = qs.get("url", [""])[0]
                pid = qs.get("parser", ["auto"])[0]
                sel = {}
                try:
                    sel = json.loads(qs.get("sel", [""])[0] or "{}") or {}
                except Exception:
                    sel = {}
                # V8.0.6：异常必须回 JSON 明细（以前裸抛 → 前端只看到笼统的「解析失败」）
                try:
                    items = run_parser({"url": url, "parser": pid, "sel": sel}, url)
                    self._json({"ok": True, "count": len(items), "sample": items[:5]})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)[:160]})
            elif path == "/api/browsercheck":
                # V8.0.6：探测本机可用的浏览器（设置页「检测」按钮用）
                found = {os.path.basename(p).lower(): p for p in _edge_chrome_paths()}
                self._json({"ok": True, "browsers": found,
                            "edge": found.get("msedge.exe", ""),
                            "chrome": found.get("chrome.exe", ""),
                            "custom": _browser_exe("custom")})
            elif path == "/api/candidates":
                # V8.1 校准向导：枚举页面上可能的新闻列表（候选组+选择器提案+预览）
                url = qs.get("url", [""])[0].strip()
                if not url.lower().startswith(("http://", "https://")) and "{q}" not in url:
                    self._json({"ok": False, "error": "地址无效"})
                else:
                    try:
                        self._json({"ok": True, "candidates": html_candidates(url)})
                    except Exception as e:
                        self._json({"ok": False, "error": str(e)[:120]})
            elif path == "/api/searchsrc":
                # V8.1 源内检索：按源的搜索地址+校准的 search_sel 解析检索结果
                sid = qs.get("id", [""])[0]
                q = (qs.get("q", [""])[0] or "").strip()[:40]
                s = next((x for x in CFG.sources if x["id"] == sid), None)
                if not s or not (s.get("search") or "").strip():
                    self._json({"ok": False, "error": "该源没有搜索地址"})
                elif not q:
                    self._json({"ok": False, "error": "关键词为空"})
                else:
                    try:
                        u = s["search"].replace("{q}", urllib.parse.quote(q))
                        sel = s.get("search_sel") or {}
                        if not sel:
                            # 未校准搜索页：退回通用识别（评分机制会拒绝导航栏）
                            sel = {}
                        items = run_parser({"url": u, "parser": "auto", "sel": sel}, u)
                        self._json({"ok": True, "count": len(items),
                                    "items": [{"title": clean_text(i.get("title", "")),
                                               "url": i.get("url", "")} for i in items[:30]]})
                    except Exception as e:
                        self._json({"ok": False, "error": str(e)[:120]})
            elif path == "/api/default_sources":
                self._json({"ok": True, "sources": deep_copy(DEFAULT_SOURCES)})
            elif path == "/api/tts/audio":
                # 手机端在线朗读：云端合成 mp3 → WebView <audio> 播放（双保险的第二条路）
                text = qs.get("text", [""])[0][:600]
                try:
                    rate = float(qs.get("rate", ["1.0"])[0])
                except Exception:
                    rate = 1.0
                data = tts_synth_mp3(text, rate) if text else None
                if data:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._json({"ok": False, "error": "在线语音不可用"}, code=503)
            elif path == "/api/fav":
                self._json({"ok": True, "favorites": CFG.favorites})
            elif path == "/api/history":
                self._json({"ok": True, "history": CFG.history})
            elif path == "/api/translate":
                texts = json.loads(qs.get("texts", ["[]"])[0])
                engine = qs.get("engine", ["google"])[0]
                self._json({"ok": True, "map": translate_many(texts, engine)})
            elif path == "/sdk.global.js":
                # V6.7：云服务 SDK（建议反馈上云用）；exe 由 --add-data 打包进 _MEIPASS
                cands = []
                if getattr(sys, "_MEIPASS", ""):
                    cands.append(os.path.join(sys._MEIPASS, "sdk.global.js"))
                cands.append(os.path.join(exe_dir(), "sdk.global.js"))
                got = None
                for p in cands:
                    if os.path.exists(p):
                        got = p
                        break
                if got:
                    with open(got, "rb") as f:
                        self._send(200, f.read(), "application/javascript",
                                   {"Cache-Control": "public, max-age=3600"})
                else:
                    self._send(404, b"// sdk not found", "application/javascript")
            elif path == "/img":
                got = img_fetch(qs.get("u", [""])[0], qs.get("r", [""])[0])
                if not got:
                    self._send(404, b"", "image/png")
                    return
                self._send(200, got[0], got[1], {"Cache-Control": "public, max-age=3600"})
            elif path == "/api/tts/status":
                with _TTS_LOCK:
                    p = _TTS["proc"]
                self._json({"ok": sys.platform == "win32",
                            "speaking": bool(_TTS.get("playing") or (p and p.poll() is None)),
                            "err": _TTS.get("err", ""), "engine": _TTS.get("engine", "")})
            elif path == "/api/ping":
                WATCH.touch()
                self._json({"ok": True, "tray": bool(TRAY and TRAY.ok),
                            "app": APP_NAME, "ver": APP_VER})
            elif path == "/api/show":
                # 另一个实例启动时来叫醒本实例：优先聚焦已有窗口，否则重开一个
                WATCH.touch()
                ok = focus_existing_window() or open_window(
                    "http://127.0.0.1:%d/" % (HTTPD.server_address[1] if HTTPD else 0))
                if TRAY and TRAY.ok:
                    TRAY.balloon(APP_NAME, "软件已在运行，已为你打开窗口")
                self._json({"ok": bool(ok)})
            elif path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=shutdown_server, daemon=True).start()
            elif path == "/favicon.ico":
                self._send(200, ICON_PNG, "image/png")
            elif path == "/manifest.json":
                self._send(200, json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8",
                           {"Cache-Control": "public, max-age=3600"})
            elif path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
                data = ICONS_PNG.get(path) or ICONS_PNG.get("/icon-192.png") or ICON_PNG
                self._send(200, data, "image/png", {"Cache-Control": "public, max-age=86400"})
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as e:
            log("GET %s 异常: %r" % (path, e))
            self._json({"ok": False, "error": str(e) or e.__class__.__name__}, 500)

    def do_OPTIONS(self):
        # V7.9.4：桌面版/网页版跨域拉取推广位（GET/POST /api/ad）需要预检放行
        self._send(200, b"", "text/plain; charset=utf-8",
                   {"Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": "86400"})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8") or "{}")
        except Exception:
            body = {}
        try:
            if p.path == "/api/translate":
                # V6.9/M5.2：整页翻译（POST JSON {texts, engine, keys}）；keys 由前端本地存储传入
                # V7.8.5：engine="auto" → 标题翻译管线（本地缓存 → Argos 离线 → 在线降级），失败静默
                texts = body.get("texts") or []
                engine = str(body.get("engine") or "google")
                keys = body.get("keys") if isinstance(body.get("keys"), dict) else {}
                if engine == "auto":
                    self._json({"ok": True, "map": translate_titles(texts, keys, "auto"),
                                "offline": offline_tr_status()})
                else:
                    self._json({"ok": True, "map": translate_many(texts, engine, keys)})
                return   # M5.3/V7.0：必须 return，否则落到链尾 else 再写一次 404（重复写响应）
            if p.path == "/api/trans/keys":
                # V7.8.5：百度/有道密钥持久化（仅存本机 trans_keys.json，不进配置、不跟云同步）
                ks = body.get("keys") if isinstance(body.get("keys"), dict) else None
                if ks is None:
                    self._json({"ok": True, "keys": trans_keys_get()})
                else:
                    self._json({"ok": True, "keys": trans_keys_set(ks)})
                return
            # ---------------- V7.9：离线翻译模型（下载 / 状态 / 删除） ----------------
            if p.path in ("/api/offline/status", "/api/offline/download", "/api/offline/delete"):
                if p.path == "/api/offline/download":
                    r = offline_start_download()
                    r["status"] = offline_status()
                    self._json(r)
                elif p.path == "/api/offline/delete":
                    r = offline_delete()
                    r["status"] = offline_status()
                    self._json(r)
                else:
                    self._json({"ok": True, "status": offline_status(),
                                "probe": bool(ARGOS.available())})
                return
            # ---------------- V7.8.1：证书修复 ----------------
            if p.path == "/api/cert/chain":
                host = str(body.get("host") or "").strip().lower()
                if not host or "/" in host:
                    self._json({"ok": False, "error": "缺少 host"}); return
                r = cert_chain_probe(host)
                if r.get("ok"):
                    e = CERT_ISSUES.get(host) or {}
                    old = {c.get("thumb"): c for c in (e.get("chain") or [])}
                    for c in r["chain"]:
                        oc = old.get(c.get("thumb")) or {}
                        c["saved"] = oc.get("saved", False)
                        c["installed"] = oc.get("installed", False)
                        if oc.get("file"):
                            c["file"] = oc["file"]
                    e["chain"] = r["chain"]
                    e["err"] = e.get("err") or "证书链不完整"
                    e["ts"] = time.time()
                    CERT_ISSUES[host] = e
                    _cert_save()
                    self._json({"ok": True, "items": cert_issues_payload()})
                else:
                    self._json({"ok": False, "error": r.get("err", "探测失败")})
                return
            if p.path == "/api/cert/install":
                host = str(body.get("host") or "").strip().lower()
                ok, msg = cert_chain_install(host)
                self._json({"ok": ok, "error": None if ok else msg, "msg": msg,
                            "items": cert_issues_payload()})
                return
            if p.path == "/api/cert/forget":
                host = str(body.get("host") or "").strip().lower()
                CERT_ISSUES.pop(host, None)
                _cert_save()
                self._json({"ok": True, "items": cert_issues_payload()})
                return
            if p.path == "/api/openurl":
                # V7.9.5：桌面版点源圆点 → 打开浏览器（网页版不执行，防止远端拿服务端开浏览器）
                # V8.0.6：改走 open_external——默认用户日常浏览器（插件/书签全在）
                u = str(body.get("url") or "").strip()
                if not (u.startswith("http://") or u.startswith("https://")):
                    self._json({"ok": False, "error": "地址不合法"})
                    return
                if os.environ.get("REBANG_NO_BROWSER") == "1" and not os.environ.get("REBANG_ALLOW_OPEN"):
                    self._json({"ok": False, "error": "helmet"})
                    return
                ok = open_external(u)
                if not ok:
                    try:
                        import webbrowser
                        ok = bool(webbrowser.open(u))
                    except Exception as e:
                        log("打开外链失败: %r" % (e,))
                self._json({"ok": ok})
                return
            if p.path == "/api/pickfile":
                # V8.0.6：Windows 原生「选择文件」对话框（ctypes，无 tkinter 依赖）——选自定义浏览器 exe
                if os.name != "nt":
                    self._json({"ok": False, "error": "仅桌面版支持"})
                    return
                try:
                    import ctypes
                    from ctypes import wintypes
                    buf = ctypes.create_unicode_buffer(520)
                    ofn = wintypes.OPENFILENAMEW()
                    ofn.lStructSize = ctypes.sizeof(wintypes.OPENFILENAMEW)
                    ofn.hwndOwner = None
                    ofn.lpstrFilter = "浏览器程序 (*.exe)\0*.exe\0所有文件 (*.*)\0*.*\0\0"
                    ofn.lpstrFile = buf
                    ofn.nMaxFile = 520
                    ofn.lpstrTitle = "选择浏览器可执行文件（chrome.exe / msedge.exe…）"
                    ofn.Flags = 0x00080000  # OFN_FILEMUSTEXIST
                    ok = bool(ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)))
                    self._json({"ok": ok, "path": buf.value if ok else ""})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)[:120]})
                return
            if p.path == "/api/ad":
                # V7.9.4：只有作者（管理员邮箱登录）能改推广位；改完全网客户端下次启动自动同步
                if not is_admin():
                    self._json({"ok": False, "error": "仅作者可修改推广位（请先用作者账号登录）"},
                               extra={"Access-Control-Allow-Origin": "*"})
                    return
                ad = body.get("ad") if isinstance(body.get("ad"), dict) else {}
                clean = {}
                for k in AD_KEYS:
                    if k in ad:
                        clean[k] = str(ad.get(k) or "")[:400]
                if not clean:
                    self._json({"ok": False, "error": "没有需要保存的内容"},
                               extra={"Access-Control-Allow-Origin": "*"})
                    return
                CFG.update({"settings": clean})
                log("推广位已更新：%s" % ", ".join(clean.keys()))
                self._json({"ok": True, "ad": ad_block()},
                           extra={"Access-Control-Allow-Origin": "*"})
                return
            if p.path == "/api/config":
                # V7.9.4：推广位字段是作者专属，非管理员提交时一律忽略（保留服务端现值）
                body = dict(body or {})
                if not is_admin() and isinstance(body.get("settings"), dict):
                    st = dict(body["settings"])
                    for k in AD_KEYS:
                        st.pop(k, None)
                    body["settings"] = st
                # V8.0.5：题目缓存的目录/保留天数不随云端配置覆盖（各设备本地路径不同）
                if isinstance(body.get("settings"), dict):
                    st = dict(body["settings"])
                    st.pop("titlesDir", None)
                    st.pop("titlesKeep", None)
                    body["settings"] = st
                res = CFG.update(body)
                valid = {s["id"] for s in CFG.sources}
                with STORE.lock:
                    for k in list(STORE.data):
                        if k not in valid:
                            STORE.data.pop(k, None)
                self._json({"ok": True, **res})
                # M8.0：登录状态下保存配置 → 自动上传云端（只传设置习惯+收藏，节约云存储）
                if ACCOUNT.user:
                    threading.Thread(target=ACCOUNT.cloud_push,
                                     args=(dict(CFG.settings), list(CFG.favorites)),
                                     daemon=True).start()
            # ---------------- V8.0.5：题目缓存目录（AI 数据源） ----------------
            elif p.path == "/api/titles/dir":
                # body: {dir?: 新目录（空=恢复默认）, keep?: 0/30/90/365}
                req = body or {}
                ok = True
                if "dir" in req:
                    nd = str(req.get("dir") or "").strip()
                    if nd:
                        if len(nd) > 260 or "\n" in nd or "\r" in nd:
                            self._json({"ok": False, "error": "目录路径不合法"})
                            return
                        try:
                            os.makedirs(nd, exist_ok=True)
                        except Exception as e:
                            self._json({"ok": False, "error": "无法创建目录：%s" % e})
                            return
                        if not os.access(nd, os.W_OK):
                            self._json({"ok": False, "error": "目录不可写，请换一个位置"})
                            return
                    CFG.settings["titlesDir"] = nd
                if "keep" in req:
                    try:
                        k = int(req.get("keep") or 0)
                    except Exception:
                        k = 0
                    CFG.settings["titlesKeep"] = k if k in (0, 30, 90, 365) else 0
                CFG.save()
                titles_ensure_readme()
                self._json({"ok": ok, "dir": titles_dir(),
                            "keep": int(CFG.settings.get("titlesKeep") or 0)})
            elif p.path == "/api/titles/pickdir":
                # V8.0.6：Windows 原生「选择文件夹」对话框（ctypes，无额外依赖）
                if os.name != "nt":
                    self._json({"ok": False, "error": "仅桌面版支持"})
                    return
                try:
                    import ctypes
                    from ctypes import wintypes

                    class BROWSEINFOW(ctypes.Structure):
                        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p),
                                    ("pszDisplayName", wintypes.LPWSTR), ("lpszTitle", wintypes.LPCWSTR),
                                    ("ulFlags", wintypes.UINT), ("lpfn", ctypes.c_void_p),
                                    ("lParam", wintypes.LPARAM), ("iImage", ctypes.c_int)]

                    shell32 = ctypes.windll.shell32
                    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
                    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
                    disp = ctypes.create_unicode_buffer(520)
                    bi = BROWSEINFOW()
                    bi.pszDisplayName = ctypes.cast(disp, wintypes.LPWSTR)
                    bi.lpszTitle = "选择题目缓存目录（软件会在里面按天建日期文件夹）"
                    bi.ulFlags = 0x00000040 | 0x00000010      # BIF_NEWDIALOGSTYLE | BIF_EDITBOX
                    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
                    if not pidl:
                        self._json({"ok": False, "cancel": True})
                        return
                    path = ctypes.create_unicode_buffer(520)
                    got = shell32.SHGetPathFromIDListW(pidl, path)
                    try:
                        ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
                    except Exception:
                        pass
                    self._json({"ok": bool(got), "path": path.value})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)[:120]})
                return
            elif p.path == "/api/titles/open":
                # 桌面版：资源管理器打开缓存目录（网页版无意义，前端会隐藏按钮）
                d = titles_dir()
                ok, err = False, ""
                try:
                    os.makedirs(d, exist_ok=True)
                    if os.name == "nt":
                        os.startfile(d)      # noqa: S606
                    else:
                        import subprocess
                        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])
                    ok = True
                except Exception as e:
                    err = str(e)[:120]
                    log("打开题目缓存目录失败: %r" % (e,))
                self._json({"ok": ok, "dir": d, "error": err})
            # ---------------- M8.0：账号与云同步 ----------------
            elif p.path == "/api/auth/sendcode":
                # 发送邮箱验证码。body: {email, purpose: 'register'|'reset'}
                email = str(body.get("email") or "").strip()
                if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                    self._json({"ok": False, "error": "邮箱格式不正确"})
                    return
                ok, r = ACCOUNT.send_code(email, exist_only=(body.get("purpose") == "reset"))
                self._json({"ok": ok, "error": None if ok else r, "vid": r.get("vid") if ok else "",
                            "isUser": r.get("is_user") if ok else None})
            elif p.path == "/api/auth/register":
                # 邮箱注册：密码按腾讯云 CloudBase 要求至少 8 位；昵称仅中文/字母
                email = str(body.get("email") or "").strip()
                pwd = str(body.get("password") or "")
                code = str(body.get("code") or "").strip()
                vid = str(body.get("vid") or "")
                nick = str(body.get("nickname") or "").strip()
                if not re.match(r"^(?=.*[A-Za-z])(?=.*\d).{8,16}$", pwd):
                    self._json({"ok": False, "error": "密码需为英文+数字，8~16 位"})
                    return
                if nick and not re.fullmatch(r"[\u4e00-\u9fa5A-Za-z]{1,24}", nick):
                    self._json({"ok": False, "error": "昵称只能包含中文和字母"})
                    return
                ok, r = ACCOUNT.register(email, code, vid, pwd, nick)
                self._json({"ok": ok, "error": None if ok else r, "user": ACCOUNT.user})
            elif p.path == "/api/auth/login":
                email = str(body.get("email") or "").strip()
                pwd = str(body.get("password") or "")
                ok, r = ACCOUNT.login(email, pwd)
                self._json({"ok": ok, "error": None if ok else r, "user": ACCOUNT.user})
            elif p.path == "/api/auth/logout":
                ACCOUNT.logout()
                self._json({"ok": True})
            elif p.path == "/api/auth/resetpwd":
                # 找回密码：邮箱验证码通过 → 直接设置新密码（英文+数字，8~16 位）
                email = str(body.get("email") or "").strip()
                pwd = str(body.get("password") or "")
                if not re.match(r"^(?=.*[A-Za-z])(?=.*\d).{8,16}$", pwd):
                    self._json({"ok": False, "error": "密码需为英文+数字，8~16 位"})
                    return
                ok, r = ACCOUNT.reset_password(email, str(body.get("code") or ""),
                                               str(body.get("vid") or ""), pwd)
                self._json({"ok": ok, "error": None if ok else r})
            elif p.path == "/api/cloud/push":
                # 手动备份：设置习惯 + 收藏 + 新增（非内置）新闻源一起上传；浏览记录不参与
                _dids = {s["id"] for s in DEFAULT_SOURCES}
                _custom = [s for s in CFG.sources if s.get("id") not in _dids]
                ok, r = ACCOUNT.cloud_push(dict(CFG.settings), list(CFG.favorites), _custom)
                self._json({"ok": ok, "error": None if ok else r,
                            "customCount": len(_custom)})
            elif p.path == "/api/cloud/pull":
                # 手动恢复：设置+收藏直接生效；云端新增源合并进本地（内置源不动）
                ok, r = ACCOUNT.cloud_pull()
                if not ok:
                    self._json({"ok": False, "error": r})
                    return
                if not r:
                    self._json({"ok": True, "empty": True})
                    return
                # V8.0.2：窗口形式不跟云端走（独立窗口是本机默认，别被别端的设置顶掉）
                if isinstance(r.get("settings"), dict):
                    r["settings"].pop("openMode", None)
                    r["settings"].pop("extBrowser", None)      # V8.0.6：本机习惯不跟云端
                    r["settings"].pop("extBrowserPath", None)
                res = CFG.update(r)
                if isinstance(r.get("favorites"), list):
                    CFG.favorites = r["favorites"]
                if isinstance(r.get("custom_sources"), list):
                    have = {s["id"] for s in CFG.sources}
                    for s in r["custom_sources"]:
                        if isinstance(s, dict) and s.get("id") and s["id"] not in have:
                            CFG.sources.append(CFG._norm_sources([s])[0])
                            have.add(s["id"])
                CFG.save()
                valid = {s["id"] for s in CFG.sources}
                with STORE.lock:
                    for k in list(STORE.data):
                        if k not in valid:
                            STORE.data.pop(k, None)
                self._json({"ok": True, "settings": res.get("settings"),
                            "sources": CFG.sources,
                            "favorites": CFG.favorites})
            elif p.path == "/api/feedback/submit":
                # 建议/反馈上云（登录或匿名均可提交，登录则绑定账号便于追溯）
                content = str(body.get("content") or "").strip()
                contact = str(body.get("contact") or "").strip()[:60]
                kind = str(body.get("kind") or "suggestion")
                if kind not in ("suggestion", "reply"):
                    kind = "suggestion"
                if not content or len(content) > 300:
                    self._json({"ok": False, "error": "内容为空或超过 300 字"})
                    return
                uid = (ACCOUNT.user or {}).get("sub") or ""
                email = (ACCOUNT.user or {}).get("email") or ""
                pid = None
                if kind == "reply":
                    try:
                        pid = int(str(body.get("parent_id") or ""))
                    except Exception:
                        pid = None
                row = {"user_id": uid, "email": email, "kind": kind,
                       "parent_id": pid, "content": content, "contact": contact,
                       "device": _device_id(), "ver": APP_VER}
                ok, r = _cb_request("POST", "/v1/rdb/rest/feedback", row,
                                    token=ACCOUNT.access_token if ACCOUNT.user else None)
                self._json({"ok": ok, "error": None if ok else (r.get("error") or "提交失败")})
            elif p.path == "/api/feedback/list":
                self._fb_list(qs)
            elif p.path == "/api/feedback/set":
                # 管理员审核操作：{id, status?, admin_note?, deleted?}
                if not (ACCOUNT.user and
                        ACCOUNT.user.get("email") == FEEDBACK_ADMIN_EMAIL):
                    self._json({"ok": False, "error": "仅管理员可操作"})
                    return
                fid = str(body.get("id") or "").strip()
                if not fid:
                    self._json({"ok": False, "error": "缺少 id"})
                    return
                patch = {k: body[k] for k in ("status", "admin_note", "deleted") if k in body}
                if not patch:
                    self._json({"ok": False, "error": "无变更字段"})
                    return
                ok, r = _cb_request("PATCH", "/v1/rdb/rest/feedback?id=eq.%s"
                                    % urllib.parse.quote(fid), patch,
                                    token=ACCOUNT.access_token,
                                    extra={"Prefer": "return=minimal"})
                self._json({"ok": ok, "error": None if ok else (r.get("error") or "操作失败")})
            elif p.path == "/api/fav":
                fav = CFG.fav_toggle(body.get("item") or {}, body.get("action"))
                self._json({"ok": True, "favorites": fav})
            elif p.path == "/api/history":
                CFG.add_history(body.get("items"))
                self._json({"ok": True, "history": CFG.history[:200]})
            elif p.path == "/api/history/clear":
                CFG.history = []
                CFG.save()
                self._json({"ok": True})
            elif p.path == "/api/tts/speak":
                if sys.platform != "win32":
                    self._json({"ok": False, "error": "no-playback"})
                else:
                    text = str(body.get("text") or "")[:600]
                    try:
                        rate = float(body.get("rate") or 1.0)
                    except Exception:
                        rate = 1.0
                    sapi = int(max(-10, min(10, round((rate - 1.0) * 6))))
                    _tts_kill()
                    with _TTS_LOCK:
                        gen = _TTS["gen"]
                    log("TTS speak 请求: rate=%s len=%d" % (rate, len(text)))
                    threading.Thread(target=_tts_run, args=(gen, text, sapi, rate),
                                     daemon=True).start()
                    self._json({"ok": True})
            elif p.path == "/api/tts/stop":
                _tts_kill()
                self._json({"ok": True})
            elif p.path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=shutdown_server, daemon=True).start()
            else:
                self._json({"ok": False, "error": "unknown"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)


class Watchdog(threading.Thread):
    """界面关闭后：托盘模式保留后台，否则自动退出"""

    def __init__(self, quiet=240):
        super().__init__(daemon=True)
        self.quiet = quiet
        self.last = time.time()

    def touch(self):
        self.last = time.time()

    def run(self):
        while True:
            time.sleep(10)
            idle = time.time() - self.last
            if idle > self.quiet:
                if CFG.settings.get("closeAction") == "tray" and TRAY and TRAY.ok:
                    if idle > 6 * 3600:
                        shutdown_server()
                        return
                else:
                    log("界面已关闭，自动退出")
                    shutdown_server()
                    return


WATCH = Watchdog()
HTTPD = None
TRAY = None


# ---------------- 语音朗读（本机 SAPI → 微软在线语音 edge-tts → PowerShell 兜底） ----------------
# 本机可能没装任何 TTS 语音库（SAPI5/OneCore 语音数为 0 时 Web Speech 和 SAPI 都无声），
# 此时自动改用微软在线神经网络语音（中文音质更好），在本机播放。
_TTS_LOCK = threading.Lock()
_TTS = {"proc": None, "gen": 0, "err": "", "engine": "", "playing": False}
_TTS_SAPI_OK = None   # None=未检测；True/False=本机 SAPI5 是否有可用语音


def _tts_sapi_voices():
    global _TTS_SAPI_OK
    if _TTS_SAPI_OK is None:
        try:
            import win32com.client as _wc
            v = _wc.Dispatch("SAPI.SpVoice")
            _TTS_SAPI_OK = bool(v.GetVoices().Count > 0)
        except Exception:
            _TTS_SAPI_OK = False
        log("TTS 检测本地 SAPI 语音: %s" % _TTS_SAPI_OK)
    return _TTS_SAPI_OK


def _tts_aborted(gen):
    with _TTS_LOCK:
        return _TTS["gen"] != gen


def _tts_tmpdir():
    import tempfile
    d = os.path.join(tempfile.gettempdir(), "rebang_tts")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = tempfile.gettempdir()
    return d


def tts_synth_mp3(text, rate=1.0):
    """跨平台在线合成：微软神经网络语音 → mp3 字节（带缓存）；失败返回 None。
    电脑端(Windows)与云端(Linux)通用，供 /api/tts/audio 给手机 WebView 用 <audio> 播放。"""
    try:
        import hashlib
        key = hashlib.md5(("%s|%s" % (text, round(rate, 2))).encode("utf-8")).hexdigest()
        path = os.path.join(_tts_tmpdir(), "a" + key + ".mp3")
        if not os.path.exists(path) or os.path.getsize(path) < 200:
            import asyncio
            import edge_tts
            pct = int(round((rate - 1.0) * 100))

            async def _go():
                c = edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural",
                                         rate="%+d%%" % pct)
                await c.save(path)
            asyncio.run(_go())
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        log("TTS synth 异常: %r" % (e,))
        return None


def _tts_play_sapi(text, sapi, gen):
    """引擎1：本机 SAPI5（进程内，需要系统装有语音库）"""
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        v = win32com.client.Dispatch("SAPI.SpVoice")
        v.Rate = sapi
        v.Volume = 100
        v.Speak(text, 1)  # SPF_ASYNC
        while True:
            if _tts_aborted(gen):
                try:
                    v.Speak("", 65)   # ASYNC+PURGE 立即停止
                    v.Skip("Sentence", 32767)
                except Exception:
                    pass
                return False
            try:
                st = v.Status.RunningState
            except Exception:
                st = 1
            if st == 1:  # SRPSDone 朗读完成
                return True
            time.sleep(0.1)
    except Exception as e:
        with _TTS_LOCK:
            _TTS["err"] = "本地语音不可用(%s)" % e.__class__.__name__
        log("TTS SAPI 异常: %r" % (e,))
        return False


def _tts_play_edge(text, rate, gen):
    """引擎2：微软在线神经网络语音（联网合成 mp3）→ 本机 MCI 播放"""
    try:
        import hashlib
        key = hashlib.md5(("%s|%s" % (text, round(rate, 2))).encode("utf-8")).hexdigest()
        path = os.path.join(_tts_tmpdir(), key + ".mp3")
        if not os.path.exists(path):
            import asyncio
            import edge_tts
            pct = int(round((rate - 1.0) * 100))

            async def _go():
                c = edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural",
                                         rate="%+d%%" % pct)
                await c.save(path)
            asyncio.run(_go())
        import ctypes
        winmm = ctypes.WinDLL("winmm")
        winmm.mciSendStringW('close tts', None, 0, 0)
        r = winmm.mciSendStringW('open "%s" type mpegvideo alias tts' % path, None, 0, 0)
        if r != 0:
            with _TTS_LOCK:
                _TTS["err"] = "音频播放器打开失败(%d)" % r
            return False
        winmm.mciSendStringW('play tts', None, 0, 0)
        while True:
            if _tts_aborted(gen):
                winmm.mciSendStringW('stop tts', None, 0, 0)
                winmm.mciSendStringW('close tts', None, 0, 0)
                return False
            buf = ctypes.create_unicode_buffer(64)
            winmm.mciSendStringW('status tts mode', buf, 64, 0)
            if buf.value != "playing":
                winmm.mciSendStringW('close tts', None, 0, 0)
                return True
            time.sleep(0.12)
    except Exception as e:
        with _TTS_LOCK:
            _TTS["err"] = "在线语音不可用(%s)" % e.__class__.__name__
        log("TTS edge 异常: %r" % (e,))
        return False


def _tts_play_ps(text, sapi, gen):
    """引擎3：PowerShell System.Speech 子进程兜底"""
    try:
        t = text.replace("'", "''").replace("`", "").replace("\r", " ").replace("\n", " ")
        script = ("Add-Type -AssemblyName System.Speech;"
                  "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                  "$s.Rate=%d;$s.Speak('%s');$s.Dispose()" % (sapi, t))
        with _TTS_LOCK:
            p = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _TTS["proc"] = p
        p.wait()
        return p.returncode == 0 and not _tts_aborted(gen)
    except Exception as e:
        with _TTS_LOCK:
            _TTS["err"] = "PowerShell 语音不可用(%s)" % e.__class__.__name__
        log("TTS PS 异常: %r" % (e,))
        return False


def _tts_run(gen, text, sapi, rate):
    t0 = time.time()
    with _TTS_LOCK:
        _TTS["playing"] = True
    try:
        for name, fn in (("sapi", lambda: _tts_play_sapi(text, sapi, gen)),
                         ("edge", lambda: _tts_play_edge(text, rate, gen)),
                         ("ps", lambda: _tts_play_ps(text, sapi, gen))):
            if _tts_aborted(gen):
                return
            if name == "sapi" and not _tts_sapi_voices():
                continue
            if fn():
                with _TTS_LOCK:
                    _TTS["engine"] = name
                log("TTS[%s] 完成 %.1fs" % (name, time.time() - t0))
                return
            if _tts_aborted(gen):
                return
            log("TTS[%s] 失败，尝试下一个引擎" % name)
        with _TTS_LOCK:
            if not _TTS["err"]:
                _TTS["err"] = "没有可用的语音引擎"
        log("TTS 全部引擎失败: %s" % text[:30])
    finally:
        with _TTS_LOCK:
            _TTS["playing"] = False


def _tts_kill():
    with _TTS_LOCK:
        p = _TTS["proc"]
        _TTS["gen"] += 1
        _TTS["proc"] = None
        _TTS["playing"] = False
    if p and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import ctypes
            w = ctypes.WinDLL("winmm")
            w.mciSendStringW('stop tts', None, 0, 0)
            w.mciSendStringW('close tts', None, 0, 0)
        except Exception:
            pass


def shutdown_server():
    global HTTPD
    try:
        STORE.save()
        CFG.save()
    except Exception:
        pass
    try:
        if TRAY:
            TRAY.stop()
    except Exception:
        pass
    try:
        if HTTPD:
            HTTPD.shutdown()
    except Exception:
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- 单实例

SINGLE_PORT = 8920  # 固定端口：首个实例占用，后来的实例据此发现已运行的实例

# Windows 命名互斥体：比端口探测更可靠——从进程创建到退出全程由系统持有，
# 进程崩溃也会自动释放，彻底消除“绑定探测口→关闭→正式起服务”之间的空窗竞态
_MUTEX_NAME = "RebangHelper_SingleInstance"
_MUTEX_HOLDER = []   # 仅用于保住句柄不被回收


def _mutex_held():
    """创建互斥体；若已存在（其它实例持有）返回 True，本进程随即退出"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if not h:
            return False
        _MUTEX_HOLDER.append(h)   # 句柄保持到进程退出
        return ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS
    except Exception as e:
        log("单实例互斥体创建失败: %r" % (e,))
        return False


# 本机探测必须绕过系统/环境代理，否则开了代理的电脑会探测失败导致多开
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _probe_instance(port):
    """检查 127.0.0.1:port 上跑的是不是热榜助手；是则返回信息字典，否则 None"""
    try:
        with _LOCAL_OPENER.open(
                "http://127.0.0.1:%d/api/ping" % port, timeout=2) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("app") == APP_NAME:
            return d
    except Exception:
        pass
    return None


def focus_existing_window():
    """找到标题含软件名的可见窗口并置前（成功返回 True）。
    V8.0.4：只在「标题里的版本号等于当前版本」时才聚焦——
    桌面上如果还留着旧版窗口（V8.0.2 等），以前会被当成本程序的窗口聚焦过去，
    用户看到的就是「打开还是上一代的版本号码」，所以旧版窗口一律跳过。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        found = []          # [(hwnd, title)]
        CB = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_ssize_t, ctypes.c_ssize_t)

        def _cb(hwnd, _lp):
            try:
                if u.IsWindowVisible(hwnd):
                    n = u.GetWindowTextLengthW(hwnd)
                    if n:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        u.GetWindowTextW(hwnd, buf, n + 1)
                        if APP_NAME in buf.value:
                            found.append((hwnd, buf.value))
            except Exception:
                pass
            return 1

        u.EnumWindows(CB(_cb), 0)
        if not found:
            return False

        def _ver_of(title):
            m = re.search(r"[Vv](\d+\.\d+(?:\.\d+)?)", title or "")
            return m.group(1) if m else ""

        cur = [(h, t) for h, t in found if _ver_of(t) == APP_VER]
        pick = cur[0] if cur else None
        if pick is None:
            # 没有当前版本的窗口：宁可开新窗口，也不聚焦旧版/其它程序的窗口
            log("跳过 %d 个非当前版本的窗口：%s"
                % (len(found), ", ".join(t[:28] for _, t in found[:4])))
            return False
        hwnd = pick[0]
        u.ShowWindow(hwnd, 9)  # SW_RESTORE：最小化则还原
        u.SetForegroundWindow(hwnd)
        return True
    except Exception as e:
        log("聚焦已有窗口失败: %r" % (e,))
    return False


def _port_in_use(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def _kill_stale_instance():
    """V7.3/M5.6：运行中的是「旧版本」实例 → 自动关闭它（同版本返回 False，交给唤醒逻辑）。
    顺序：礼貌调用旧实例 /api/quit → 等端口释放 → 兜底按端口找 PID 强杀。"""
    try:
        with _LOCAL_OPENER.open(
                "http://127.0.0.1:%d/api/ping" % SINGLE_PORT, timeout=4) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
        if str(j.get("ver") or "") == APP_VER:
            return False   # 同版本：不关，走正常「唤醒窗口」
    except Exception:
        return False       # ping 不通（可能不是本程序占用）：不乱杀
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/quit" % SINGLE_PORT, data=b"{}", method="POST")
        _LOCAL_OPENER.open(req, timeout=4)
    except Exception:
        pass
    for _ in range(20):            # 最多等 10 秒
        time.sleep(0.5)
        if not _port_in_use(SINGLE_PORT):
            return True
    try:                            # 兜底：netstat 找监听 PID 强杀
        import subprocess
        out = subprocess.check_output(["netstat", "-ano"], timeout=10).decode("gbk", "ignore")
        pids = set()
        for ln in out.splitlines():
            if (":%d " % SINGLE_PORT) in ln and "LISTENING" in ln:
                p = ln.rsplit(None, 1)[-1]
                if p.isdigit():
                    pids.add(p)
        for p in pids:
            subprocess.check_call(["taskkill", "/F", "/PID", p],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    for _ in range(10):
        time.sleep(0.5)
        if not _port_in_use(SINGLE_PORT):
            return True
    return False


def acquire_port():
    """端口分配 + 单实例守卫：
    1. 环境变量指定端口 → 直接用（云端/调试场景）
    2. 固定端口空闲 → 占用（首个实例）
    3. 固定端口被本程序占用 → 唤起已有实例的窗口后退出（防止多开）
    4. 固定端口被其他程序占用 → 随机端口兜底（不影响使用）
    """
    env = os.environ.get("REBANG_PORT") or os.environ.get("PORT")
    if env:
        return int(env)
    # 互斥体守卫：旧版本实例 → 自动关闭后本次接管；同版本 → 唤醒窗口后退出
    if _mutex_held():
        info = _probe_instance(SINGLE_PORT)
        if not info:
            # V8.0.4：可能是「另一个实例刚双击、还在解压/初始化，没开始监听」。
            # 以前这里直接 sys.exit(0)，用户连点两下图标就会出现「两次都没反应 / 闪退」。
            # 现在等它就绪（最多 8 秒），再唤出它的窗口。
            log("检测到已有实例的互斥体，等待它启动完成…")
            for _ in range(16):
                time.sleep(0.5)
                info = _probe_instance(SINGLE_PORT)
                if info:
                    break
            if not info:
                log("互斥体存在但 8 秒内没有实例监听端口，本次接管启动")
                info = None
        if info and _kill_stale_instance():
            log("检测到旧版本实例，已自动关闭，本次启动接管")
        elif info:
            shown = False
            try:
                _LOCAL_OPENER.open(
                    "http://127.0.0.1:%d/api/show" % SINGLE_PORT, timeout=4)
                shown = True
            except Exception:
                pass
            if not shown:
                shown = focus_existing_window()
            if not shown:
                # V8.0.4：已有实例在跑却唤不出窗口 → 直接给它开一个新窗口，
                # 绝不让用户看到「双击图标什么都没发生」。
                log("唤醒已有实例失败，改为直接打开窗口")
                try:
                    open_window("http://127.0.0.1:%d/" % SINGLE_PORT)
                    shown = True
                except Exception:
                    pass
            log("单实例守卫：已有实例在运行，本次启动退出（shown=%s）" % shown)
            sys.exit(0)
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", SINGLE_PORT))
        return SINGLE_PORT  # 绑定成功 = 我是首个实例
    except OSError:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
    # 绑定失败：看端口上是不是已经在跑热榜助手
    if _probe_instance(SINGLE_PORT):
        if _kill_stale_instance():
            log("检测到旧版本实例，已自动关闭，重试固定端口")
            s2 = socket.socket()
            try:
                s2.bind(("127.0.0.1", SINGLE_PORT))
                return SINGLE_PORT   # 旧实例已让位，本次成为首个实例
            except OSError:
                pass
            finally:
                try:
                    s2.close()
                except Exception:
                    pass
            return free_port()
        try:
            _LOCAL_OPENER.open(
                "http://127.0.0.1:%d/api/show" % SINGLE_PORT, timeout=4)
        except Exception:
            # V8.0.4：/api/show 没响应时别静默退出，用户会以为程序坏了
            if not focus_existing_window():
                try:
                    open_window("http://127.0.0.1:%d/" % SINGLE_PORT)
                except Exception:
                    pass
        log("检测到已有实例在运行，本次启动退出")
        sys.exit(0)
    return free_port()


def _edge_chrome_paths():
    """V8.0.2：Edge / Chrome 可执行文件位置（常见目录 + 注册表 App Paths 兜底）"""
    cands = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    try:
        import winreg
        for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"):
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, key) as k:
                        p = winreg.QueryValue(k, None)
                        if p:
                            cands.append(p)
                except Exception:
                    pass
    except Exception:
        pass
    out = []
    for c in cands:
        if c and os.path.exists(c) and c not in out:
            out.append(c)
    return out


def _profile_dir():
    """V8.0.4：独立窗口的浏览器用户目录。data 目录不可写时退回临时目录，
    避免 Edge 拿到一个写不进去的 --user-data-dir 直接闪退（表现为「双击没反应」）。"""
    cands = [os.path.join(data_dir(), "独立窗口"),
             os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"),
                          "rebang_win_profile")]
    for p in cands:
        try:
            os.makedirs(p, exist_ok=True)
            probe = os.path.join(p, ".rebang_w")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            return p
        except Exception as e:
            log("窗口用户目录不可用(%s): %r" % (p, e))
    return ""


def _spawn_ok(exe, args, wait=1.3):
    """启动浏览器并确认它没有立刻退出。
    V8.0.4：以前只要 Popen 不抛异常就当成功，浏览器随后闪退时窗口会「凭空消失」，
    用户看到的就是打不开 / 打开了别的东西。现在等一小会儿看进程是否还活着。"""
    try:
        p = subprocess.Popen([exe] + args, close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("启动失败 %s: %r" % (os.path.basename(exe), e))
        return False
    t0 = time.time()
    while time.time() - t0 < wait:
        if p.poll() is not None:
            log("浏览器进程提前退出(%s, code=%s)，换下一个方案"
                % (os.path.basename(exe), p.returncode))
            return False
        time.sleep(0.12)
    return True


def _default_browser_kind():
    """V8.0.6：读 Windows 的默认浏览器（https 关联），返回 'chrome'/'edge'/''。
    用户选「日常浏览器」时优先用它，没有再用 Edge 兜底。"""
    if os.name != "nt":
        return ""
    try:
        import winreg
        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            prog = str(winreg.QueryValueEx(k, "ProgId")[0] or "")
        low = prog.lower()
        if "chrome" in low:
            return "chrome"
        if "edge" in low or "msedge" in low:
            return "edge"
    except Exception:
        pass
    return ""


def _browser_exe(kind):
    """V8.0.6：按名称定位浏览器 exe。edge/chrome 走 _edge_chrome_paths，
    custom 用用户在设置里填的路径。找不到返回空串。"""
    if kind == "custom":
        p = str(CFG.settings.get("extBrowserPath") or "").strip().strip('"')
        return p if p and os.path.exists(p) else ""
    want = {"edge": "msedge.exe", "chrome": "chrome.exe"}.get(kind)
    if not want:
        return ""
    for p in _edge_chrome_paths():
        if os.path.basename(p).lower() == want:
            return p
    return ""


def _spawn_app_window(url):
    """V8.0.7：隔离「独立窗口」方案链（Edge/Chrome --app + 专用 profile）。
    主窗口默认走这里；外链选「独立窗口」也走这里。"""
    base = ["--window-size=1320,880", "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,msEdgeSidebarV2,msFeedScreenshot"]
    prof = _profile_dir()
    plans = []
    if prof:
        plans.append(["--user-data-dir=" + prof, "--app=" + url] + base)
    plans.append(["--app=" + url] + base)
    if prof:
        plans.append(["--user-data-dir=" + prof, "--new-window", url] + base)
    plans.append(["--new-window", url] + base)
    for exe in _edge_chrome_paths():
        for args in plans:
            if _spawn_ok(exe, args):
                log("已用独立窗口打开：%s %s" % (os.path.basename(exe),
                                              "app" if "--app=" + url in args else "window"))
                return True
    return False


def open_external(url):
    """V8.0.7：按设置用「用户选定的浏览器」打开外链（点新闻源「更多」等）。
    关键点：不带 --user-data-dir、不带 --app —— 直接把 URL 交给浏览器，
    打开的就是用户日常那个带插件、带书签、带登录状态的正常环境。
    daily=本机默认浏览器(Chrome/Edge 正常 profile) → sys=系统默认 → app=隔离独立窗口。
    返回 True 表示已交给浏览器（浏览器接管既有实例时进程会立刻退出，属正常）。"""
    mode = str(CFG.settings.get("extBrowser") or "daily")
    if mode == "app":
        if _spawn_app_window(url):
            return True
        log("独立窗口打不开，退回系统默认浏览器")
        try:
            import webbrowser
            return bool(webbrowser.open(url))
        except Exception:
            return False
    order = []
    if mode == "daily":
        # 先试 Windows 默认浏览器（若为 Chrome/Edge），再用其余已装浏览器兜底
        dk = _default_browser_kind()
        first = _browser_exe(dk) if dk else ""
        order = ([first] if first else []) + \
                [p for p in _edge_chrome_paths() if p != first]
    elif mode in ("edge", "chrome", "custom"):
        exe = _browser_exe(mode)
        order = [exe] if exe else []
    for exe in order:
        try:
            subprocess.Popen([exe, url], close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("已用日常浏览器打开：%s" % os.path.basename(exe))
            return True
        except Exception as e:
            log("启动浏览器失败 %s: %r" % (exe, e))
    if mode in ("daily", "sys"):
        try:
            import webbrowser
            if webbrowser.open(url):
                log("已用系统默认浏览器打开")
                return True
        except Exception as e:
            log("系统默认浏览器打开失败: %r" % (e,))
    return False


def open_window(url):
    """V8.0.7：主窗口默认「独立窗口」（隔离 --app，像普通软件一样，V8.0.4 行为）。
    只有 设置→常规→窗口形式 明确选了「默认浏览器」时才进浏览器
    （进哪个浏览器由「用哪个浏览器打开」extBrowser 决定）。
    extBrowser 只管外链跳转（/api/openurl）。"""
    if os.environ.get("REBANG_NO_BROWSER"):
        return True
    if str(CFG.settings.get("openMode") or "app") == "browser":
        if open_external(url):
            return True
        log("浏览器方式打不开，退回隔离独立窗口")
    if _spawn_app_window(url):
        return True
    log("独立窗口所有方案都失败，退回默认浏览器")
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception:
        return False


def start_tray(url):
    global TRAY
    if os.environ.get("REBANG_NO_TRAY") or CFG.settings.get("closeAction") != "tray":
        return
    try:
        from tray import Tray
        icon = None
        for cand in (os.path.join(exe_dir(), "icon.ico"),
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico"),
                     os.path.join(getattr(sys, "_MEIPASS", ""), "icon.ico")):
            if cand and os.path.exists(cand):
                icon = cand
                break
        TRAY = Tray("%s V%s · 作者：%s" % (APP_NAME, APP_VER, AUTHOR),
                    lambda: open_window(url),
                    lambda: threading.Thread(target=lambda: refresh_many(
                        [s for s in CFG.sources if s["enabled"]], force=True), daemon=True).start(),
                    shutdown_server, icon)
        if not TRAY.start():
            TRAY = None
            log("托盘创建失败，关闭窗口将直接退出")
    except Exception as e:
        TRAY = None
        log("托盘异常: %r" % (e,))


ICON_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAP0lEQVR42mP8z8Dwn4GKgIma"
    b"hsEYGAcNGgPjYAwMYmAcNGgMjIMxMIiBcdCgMTAOxsAgBsZBg8bAOBgDAwBVvwYR1M1O4wAA"
    b"AABJRU5ErkJggg==")


def _load_icon_png(name):
    for d in (exe_dir(), os.path.dirname(os.path.abspath(__file__)), getattr(sys, "_MEIPASS", "")):
        if not d:
            continue
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    return f.read()
            except Exception:
                pass
    return None


def _build_manifest():
    icons = [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
              "purpose": "any maskable"},
             {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
              "purpose": "any maskable"}]
    return {
        "name": "%s V%s" % (APP_NAME, APP_VER),
        "short_name": APP_NAME,
        "description": "全网热榜聚合阅读 · 作者：%s" % AUTHOR,
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#f2f3f5",
        "theme_color": "#e8402f",
        "icons": icons,
    }


ICONS_PNG = {k: v for k, v in (
    ("/icon-192.png", _load_icon_png("icon-192.png")),
    ("/icon-512.png", _load_icon_png("icon-512.png")),
    ("/apple-touch-icon.png", _load_icon_png("icon-192.png"))) if v}
MANIFEST = _build_manifest()


_BOOT_DONE = [False]


def _install_crash_guard():
    """V8.0.4：闪退不再「无声无息」——未捕获异常写崩溃日志；启动阶段失败弹窗告知用户。"""
    def _hook(exc_type, exc, tb):
        try:
            import traceback
            txt = "".join(traceback.format_exception(exc_type, exc, tb))
        except Exception:
            txt = repr(exc)
        try:
            with open(os.path.join(data_dir(), "崩溃日志.txt"), "a", encoding="utf-8") as f:
                f.write("\n=== %s V%s  %s ===\n%s\n"
                        % (APP_NAME, APP_VER, time.strftime("%Y-%m-%d %H:%M:%S"), txt))
        except Exception:
            pass
        log("未捕获异常: " + txt.replace("\n", " | ")[:900])
        if not _BOOT_DONE[0] and os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "热榜助手启动失败：\n\n%s\n\n日志已写入：\n%s"
                    % (str(exc)[:300], os.path.join(data_dir(), "崩溃日志.txt")),
                    "热榜助手 V%s" % APP_VER, 0x10)
            except Exception:
                pass
    sys.excepthook = _hook
    try:
        def _thook(args):
            if args.exc_type is SystemExit:
                return
            _hook(args.exc_type, args.exc_value, args.exc_traceback)
        threading.excepthook = _thook
    except Exception:
        pass


def main():
    global HTTPD
    _install_crash_guard()
    log("==== 启动 V%s (pid=%s frozen=%s) ====" % (APP_VER, os.getpid(),
                                                bool(getattr(sys, "frozen", False))))
    titles_ensure_readme()          # V8.0.5：题目缓存目录 + 给 AI 的数据说明
    port = acquire_port()
    bind = os.environ.get("REBANG_BIND") or "127.0.0.1"
    HTTPD = None
    for attempt in range(3):                    # V8.0.4：端口偶发占用时重试，别直接崩
        try:
            HTTPD = ThreadingHTTPServer((bind, port), Handler)
            break
        except OSError as e:
            log("端口 %s 绑定失败(第 %d 次): %r" % (port, attempt + 1, e))
            time.sleep(0.6)
            if attempt == 2:
                port = free_port()
    if HTTPD is None:
        raise RuntimeError("无法启动本地服务（端口 %s 一直被占用）" % port)
    HTTPD.daemon_threads = True
    url = "http://%s:%d/" % (bind if bind != "0.0.0.0" else "127.0.0.1", port)
    log("启动成功 %s v%s @ %s" % (APP_NAME, APP_VER, url))
    _BOOT_DONE[0] = True
    threading.Thread(target=lambda: refresh_many([s for s in CFG.sources if s["enabled"]]),
                     daemon=True).start()
    threading.Thread(target=_periodic_refresh, daemon=True).start()
    WATCH.start()
    start_tray(url)
    if not os.environ.get("REBANG_NO_BROWSER"):
        threading.Thread(target=open_window, args=(url,), daemon=True).start()
    try:
        HTTPD.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            STORE.save()
            CFG.save()
        except Exception:
            pass


if __name__ == "__main__":
    main()
