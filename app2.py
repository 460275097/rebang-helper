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
import json
import os
import re
import socket
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
APP_VER = "4.8"
AUTHOR = "七度"

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
      cat="综合"):
    return {"id": sid, "name": name, "url": url, "parser": parser, "sel": sel or {},
            "count": count, "enabled": enabled, "note": note, "search": search, "cat": cat}


# 分类：综合 / 财经 / 娱乐 / 科技 / 社区（与 rebang.today 的版块划分对齐）
CATS = ["综合", "财经", "娱乐", "科技", "社区"]


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
      search="https://search.douban.com/movie/subject_search?search_text={q}", cat="娱乐"),
    S("huxiu", "虎嗅", _MIRROR + "5VaobgvAj1", "tophub", note="热文榜",
      search="https://www.huxiu.com/search.html?s={q}", cat="科技"),
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
    S("smzdm", "什么值得买", "https://post.smzdm.com/", "html",
      {"link": "h5.z-feed-title a"},
      note="好文/好价", search="https://search.smzdm.com/?c=home&s={q}", cat="综合"),
    S("guancha", "观风闻", "https://www.guancha.cn/", "html",
      {"link": "h4.module-title a"},
      note="风闻社区", search="https://www.guancha.cn/search?q={q}", cat="综合"),
    S("52pojie", "吾爱破解", _MIRROR + "NKGoRAzel6", "tophub", note="今日热帖",
      search="https://www.52pojie.cn/search.php?mod=forum&srchtxt={q}", cat="社区"),
    S("10jqka", "同花顺", "https://news.10jqka.com.cn/", "html",
      {"link": "span.arc-title a"},
      note="财经要闻", search="https://search.10jqka.com.cn/search?q={q}", cat="财经"),
    S("xueqiu", "雪球", "https://xueqiu.com/", "html", {},
      enabled=False, note="站点有风控校验，建议在设置里换成自己的雪球接口", cat="财经"),
    S("landian", "蓝点网", "https://www.landiannews.com/", "html",
      {"block": "article.post-list-main", "link": "h2 a"},
      note="科技资讯", search="https://www.landiannews.com/?s={q}", cat="科技"),
    S("appinn", "小众软件", "https://www.appinn.com/", "html",
      {"link": "h2.post-title a"},
      note="软件推荐", search="https://www.appinn.com/?s={q}", cat="科技"),
    S("cls", "财联社", "https://www.cls.cn/telegraph", "html",
      {"block": "div.telegraph-content-box", "link": "a"},
      enabled=False, note="页面为动态渲染，默认关；可在设置里换成自己的接口", cat="财经"),
    S("huanqiu", "环球网", "https://www.huanqiu.com/", "html",
      {"block": "dl.thrnewslist dd", "link": "a"},
      note="要闻", search="https://www.huanqiu.com/search?q={q}", cat="综合"),
    S("iqiyi", "爱奇艺", "https://www.iqiyi.com/", "html", {}, enabled=False,
      note="榜单为动态渲染，需自行填可用地址", cat="娱乐"),
    S("youpin", "小米有品", "https://www.xiaomiyoupin.com/", "html", {}, enabled=False,
      note="榜单为动态渲染，需自行填可用地址", cat="综合"),
    S("gamersky", "游民星空", "https://www.gamersky.com/", "html",
      {"block": "li.li3", "link": "a"},
      note="游戏资讯", search="https://so.gamersky.com/all?s={q}", cat="娱乐"),
    S("oschina", "开源中国", "https://www.oschina.net/news", "html",
      {"block": "div.event", "link": "a"},
      note="开源资讯", search="https://www.oschina.net/search?scope=news&q={q}", cat="科技"),
    S("github", "GitHub今日上升", "https://github.com/trending?since=daily", "github",
      note="按今日新增 Star 排序", search="https://github.com/search?q={q}", cat="科技"),
    S("dangdang", "当当图书榜",
      "http://bang.dangdang.com/books/bestsellers/01.00.00.00.00.00-recent7-0-0-1-1",
      "html", {"block": "ul.bang_list li", "link": "div.name a", "heat": "div.star"},
      count=20, note="近7日畅销榜", search="http://search.dangdang.com/?key={q}", cat="综合"),
    S("ifanr", "爱范儿", "https://www.ifanr.com/", "html",
      {"block": "div.article-item--list", "link": "h3 a"},
      note="科技资讯", search="https://www.ifanr.com/?s={q}", cat="科技"),
    S("d423", "423Down", "https://www.423down.com/", "d423", count=20, note="软件更新",
      search="https://www.423down.com/search/{q}", cat="科技"),
    S("ypojie", "易破解", "https://www.ypojie.com/", "ypojie", count=20, note="软件更新",
      search="https://www.ypojie.com/?s={q}", cat="科技"),
    S("cr173", "下载吧", "https://www.cr173.com/", "cr173", count=20, note="软件更新",
      search="https://so.cr173.com/?q={q}", cat="科技"),
    S("baicaio", "微线报", "https://www.baicaio.com/", "baicaio", count=20,
      note="原 hxzxb.com 已下线，改用同类线报源", search="https://www.baicaio.com/search/?keyword={q}", cat="综合"),
    S("36kr", "36氪", _MIRROR + "Q1Vd5Ko85R", "tophub", note="24小时热榜",
      search="https://36kr.com/search/articles/{q}", cat="科技"),
    S("weread", "微信读书", _MIRROR + "4KvxEX0dkx", "tophub", count=20, note="总榜",
      search="https://weread.qq.com/", cat="综合"),
    S("tieba", "百度贴吧", _MIRROR + "Om4ejxvxEN", "tophub", note="热议榜",
      search="https://tieba.baidu.com/f/search/res?qw={q}", cat="社区"),
    S("360hot", "360热搜", "https://m.so.com/hotnews", "html", {}, enabled=False,
      note="原接口已失效，需自行填写可用地址", cat="综合"),
    S("xiaohongshu", "小红书", "https://www.xiaohongshu.com/explore", "html", {}, enabled=False,
      note="需登录态，建议在设置里换成自己的接口地址", cat="社区"),

    # ---------------- V3.2 新增：科技 ----------------
    S("juejin", "掘金", "https://api.juejin.cn/content_api/v1/content/article_rank?category_id=1&type=hot",
      "juejin", count=20, note="综合热榜", search="https://juejin.cn/search?query={q}", cat="科技"),
    S("ithome_rss", "IT之家·最新", "https://www.ithome.com/rss/", "rss", count=30,
      note="官方 RSS 实时资讯", search="https://www.ithome.com/search/?q={q}", cat="科技"),
    S("sspai_rss", "少数派·最新", "https://sspai.com/feed", "rss", count=20,
      note="官方 RSS", search="https://sspai.com/search/post/{q}", cat="科技"),
    S("kr36_hot", "36氪·热榜", _MIRROR + "Q1Vd5Ko85R", "tophub", count=20,
      note="24 小时热榜", search="https://www.36kr.com/search/articles/{q}", cat="科技"),
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
      cat="娱乐"),
    S("douyin_ent", "抖音·娱乐榜", _MIRROR + "DpQvNABoNE", "tophub", count=20,
      note="娱乐热点", cat="娱乐"),
    S("bili_rss", "B站·热门", "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
      "bilibili", count=30, note="排行榜", search="https://search.bilibili.com/all?keyword={q}",
      cat="娱乐"),
    S("ifeng_ent", "凤凰·娱乐", "https://ent.ifeng.com/", "html", count=20,
      note="娱乐资讯", cat="娱乐"),

    # ---------------- V3.2 新增：社区 ----------------
    S("v2ex", "V2EX·热门", _MIRROR + "Om4ejxvxEN", "tophub", count=20,
      note="技术社区热帖", search="https://www.google.com/search?q=site:v2ex.com+{q}", cat="社区"),
    S("douban_group_hot", "豆瓣·热门小组", "https://www.douban.com/group/explore", "html",
      count=20, note="小组精选", search="https://www.douban.com/group/search?cat=1013&q={q}",
      cat="社区"),
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
    S("cls_news", "财联社", "https://news.so.com/ns?q=%E8%B4%A2%E8%81%94%E7%A4%BE&pn=1", "360news",
      count=20, note="经360新闻聚合（官网反爬）", search="https://www.cls.cn/searchPage?keyword={q}", cat="财经"),
    S("bloomberg", "彭博社", "https://news.so.com/ns?q=%E5%BD%AD%E5%8D%9A%E7%A4%BE&pn=1", "360news",
      count=20, note="经360新闻聚合（官网境外）", search="https://news.so.com/ns?q={q}", cat="财经"),
    S("taoguba", "淘股吧", "https://news.so.com/ns?q=%E6%B7%98%E8%82%A1%E5%90%A7&pn=1", "360news",
      count=20, note="经360新闻聚合（社区页JS渲染）", search="https://www.tgb.cn/search?type=news&keywords={q}", cat="财经"),
    S("gov_cn", "国务院文件", "https://www.gov.cn/", "html", count=20,
      note="中国政府网·国务院最新文件与要闻", search="https://sousuo.www.gov.cn/sousuo/search.shtml?code=17da70961a7&searchWord={q}", cat="财经"),
    S("sse_news", "上交所", "http://www.sse.com.cn/", "sse", count=20,
      note="新闻发布会与公告", search="http://query.sse.com.cn/search/portalSearch.do?keyword={q}", cat="财经"),
    S("modelscope", "ModelScope模型榜", "https://modelscope.cn/api/v1/dolphin/models", "modelscope",
      count=30, note="魔搭社区·热门模型", search="https://modelscope.cn/models?name={q}", cat="财经"),
    S("sputnik", "卫星通讯社·中国", "https://sputniknews.cn/china/", "html", count=20,
      note="俄罗斯卫星通讯社中文版", search="https://sputniknews.cn/search/?query={q}", cat="财经"),
]

DEFAULT_SETTINGS = {
    "view": "image",         # image / text
    "theme": "light",        # light / dark
    "perSource": 30,         # 全局每源条数（源可单独覆盖）
    "autoRefresh": 10,       # 分钟，0=关闭
    "fontFamily": "",        # 空=系统默认
    "fontScale": 1.0,
    "bgImage": "",           # 背景图（本地文件路径或 data URL）
    "bgOpacity": 0.14,
    "closeAction": "tray",   # tray=关闭退到托盘, exit=直接退出
    "ttsRate": 1.0,
    "sortAgg": "heat",
    "openMode": "app",
    "showOnlyOk": True,      # 仅显示抓取成功的源
    "startupCheck": True,    # 启动时强制刷新并自动隐藏失败源
    "accent": "",            # 主题配色：空=经典红 / blue / green / violet / cyan / rose / amber
    "translate": "auto",
}


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
        self.settings = deep_copy(DEFAULT_SETTINGS)
        self.sources = deep_copy(DEFAULT_SOURCES)
        self.favorites = []
        self.history = []
        self._lock = threading.Lock()
        self.load()

    def load(self):
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
        if isinstance(raw.get("sources"), list) and raw["sources"]:
            self.sources = self._norm_sources(raw["sources"])
            if raw.get("presetVer") != APP_VER:
                # 版本升级：内置源的抓取规则随程序更新，用户的开关/顺序/条数保持不变
                self._refresh_builtins()
                self.save()
        else:
            self.sources = deep_copy(DEFAULT_SOURCES)
        self.favorites = raw.get("favorites") or []
        self.history = raw.get("history") or []

    @staticmethod
    def _norm_sources(srcs):
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
                "count": max(1, min(50, int(s.get("count") or 30))),
                "enabled": bool(s.get("enabled", True)),
                "note": str(s.get("note") or "")[:80],
                "search": str(s.get("search") or ""),
                "cat": (str(s.get("cat") or "").strip() or "综合")
                       if str(s.get("cat") or "").strip() in CATS else "综合",
            })
        return out

    @staticmethod
    def _merge_new(srcs):
        return srcs

    def _refresh_builtins(self):
        preset = {s["id"]: s for s in DEFAULT_SOURCES}
        have = {s["id"] for s in self.sources}
        for s in self.sources:
            d = preset.get(s["id"])
            if not d:
                continue
            s["url"], s["parser"] = d["url"], d["parser"]
            s["sel"], s["note"] = deep_copy(d["sel"]), d["note"]
            s["search"] = d["search"]
            s["name"] = d["name"]
            s["cat"] = d.get("cat") or "综合"
            if not s.get("count"):
                s["count"] = d["count"]
        # 版本升级时自动补上新增的内置源（用户原有的开关/顺序/条数保持不变）
        for d in DEFAULT_SOURCES:
            if d["id"] not in have:
                self.sources.append(deep_copy(d))

    def save(self):
        with self._lock:
            payload = {"version": 2, "app": APP_VER, "presetVer": APP_VER,
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
        if isinstance(data.get("sources"), list):
            self.sources = self._norm_sources(data["sources"])
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
    for k, v in attrs:
        if k not in node.attrs:
            return False
        if v and v not in (node.attrs.get(k) or ""):
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


def _opener(referer=None):
    key = referer or ""
    if key in _OPENERS:
        return _OPENERS[key]
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
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


def fetch_bytes(url, referer=None, timeout=14):
    op = _opener(referer)
    req = urllib.request.Request(url)
    if referer:
        req.add_header("Referer", referer)
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
        out.append(mk_item(repo, "https://github.com/" + a.get("href").strip("/"),
                           to_heat(m.group(1)) if m else None,
                           "https://github.com/%s.png?size=96" % owner, extra,
                           "https://github.com/"))
    return finish(out)


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


def generic_json(url, base):
    data = http_json(url)
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


NAV_WORDS = set("""首页 主页 更多 查看更多 登录 注册 退出 上一页 下一页 下载 详情 评论
关于我们 联系我们 网站地图 返回顶部 广告 投稿 客户端 排行榜 全部 搜索 立即下载 查看详情
新闻 体育 娱乐 财经 科技 汽车 房产 旅游 教育 时尚 健康 军事 图片 视频 论坛 博客 专题""".split())


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


def generic_html(url, base):
    html = http_text(url)
    root = parse_html(html)
    groups = {}
    for a in root.find_all("a"):
        href = a.get("href") or ""
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if href.lower().endswith((".jpg", ".png", ".gif", ".zip", ".exe", ".apk", ".mp4")):
            continue
        t = a.text(80)
        if len(t) < 5 or t in NAV_WORDS:
            continue
        p = a.parent
        if p is None:
            continue
        key = (p.tag, " ".join(sorted(p.cls.split()))[:40])
        groups.setdefault(key, []).append(a)
    if not groups:
        raise RuntimeError("页面里没找到列表结构")
    key, anchors = max(groups.items(), key=lambda kv: len(kv[1]))
    if len(anchors) < 3:
        raise RuntimeError("可识别条目太少（%d）" % len(anchors))
    out = []
    for a in anchors[:100]:
        p = a.parent
        block = p.text(400)
        out.append(mk_item(re.sub(r"^\d+[\.、\s]*", "", a.text(120)), a.get("href"),
                           _heat_from(block, a.text(120)), node_img(p), "", base or url))
    return finish(out)


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


PARSERS = {
    "baidu": p_baidu, "weibo": p_weibo, "bilibili": p_bilibili, "github": p_github,
    "d423": p_d423, "ypojie": p_ypojie, "cr173": p_cr173, "baicaio": p_baicaio,
    "toutiao": p_toutiao, "tophub": p_tophub, "qqnews": p_qqnews, "ithome": p_ithome,
    "sspai": p_sspai, "zhihudaily": p_zhihudaily,
    "rss": p_rss, "juejin": p_juejin, "douban_movie": p_douban_movie, "yicai": p_yicai,
    "360news": p_360news, "sse": p_sse, "modelscope": p_modelscope,
}


def run_parser(src, url=None):
    url = url or src.get("url") or ""
    if not url:
        raise RuntimeError("未填写地址")
    pid = (src.get("parser") or "auto").lower()
    base = "%s://%s" % (urllib.parse.urlparse(url).scheme, urllib.parse.urlparse(url).netloc)
    sel = src.get("sel") or {}

    def _once():
        if sel:
            # 有些站点（如值得买）带上 Referer 反而会被风控，先裸请求再带来源重试
            html = http_text(url, timeout=18)
            if len(html) < 2500:
                html = http_text(url, referer=base + "/", timeout=18)
            if len(html) < 2500:
                raise RuntimeError("页面被拦截或内容过短（%d 字节）" % len(html))
            return parse_by_sel(parse_html(html), url, sel)
        if pid in PARSERS:
            return PARSERS[pid](url)
        if pid == "json":
            return generic_json(url, base)
        if pid == "html":
            return generic_html(url, base)
        try:
            return generic_json(url, base)
        except Exception:
            return generic_html(url, base)

    try:
        items = _once()
    except Exception as first:
        # 部分站点校验 Referer/Cookie：先访问首页预热，再重试一次
        try:
            fetch_bytes(base + "/", timeout=10)
            items = _once()
        except Exception:
            raise first
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


def refresh_source(src, force=False):
    sid = src["id"]
    cur = STORE.get(sid)
    if not force and cur and time.time() - cur["ts"] < REFRESH_GAP and cur["items"]:
        return cur
    try:
        items = run_parser(src)
        STORE.put(sid, items, "")
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        log("[%s] 失败: %s" % (src.get("name"), msg))
        old = STORE.get(sid) or {}
        STORE.put(sid, old.get("items", []), msg)
    return STORE.get(sid)


def refresh_many(sources, force=False):
    if not sources:
        return
    with ThreadPoolExecutor(max_workers=min(16, len(sources))) as ex:
        list(ex.map(lambda s: refresh_source(s, force), sources))
    STORE.save()


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


def translate_text(text):
    text = (text or "").strip()
    if not text:
        return ""
    if text in TRANS_CACHE:
        return TRANS_CACHE[text]
    if not re.search(r"[A-Za-z]{3}", text):
        return text
    for fn in (_t_google, _t_mymemory):
        try:
            r = fn(text)
            if r and not r.lower().startswith("myMemory".lower()) and len(r) > 1:
                TRANS_CACHE[text] = r
                return r
        except Exception:
            continue
    return ""


def translate_many(texts):
    texts = [t for t in (texts or []) if isinstance(t, str) and t.strip()][:60]
    out = {}
    if not texts:
        return out
    with ThreadPoolExecutor(max_workers=4) as ex:
        for src, dst in zip(texts, ex.map(translate_text, texts)):
            if dst:
                out[src] = dst
    return out


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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path, qs = p.path, urllib.parse.parse_qs(p.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/config":
                self._json({"app": APP_VER, "author": AUTHOR, "name": APP_NAME,
                            "settings": CFG.settings, "sources": CFG.sources,
                            "favorites": CFG.favorites, "history": CFG.history,
                            "dataDir": data_dir()})
            elif path == "/api/hot":
                ids = [x for x in (qs.get("ids", [""])[0] or "").split(",") if x]
                force = qs.get("force", ["0"])[0] == "1"
                srcs = [s for s in CFG.sources if s["id"] in ids and s["enabled"]]
                refresh_many(srcs, force=force)
                out = {}
                for s in srcs:
                    cur = STORE.get(s["id"]) or {}
                    out[s["id"]] = {"items": (cur.get("items") or [])[:120],
                                    "error": cur.get("error", ""), "ts": cur.get("ts", 0)}
                self._json({"ok": True, "sources": out, "now": time.time()})
            elif path == "/api/test":
                url = qs.get("url", [""])[0]
                pid = qs.get("parser", ["auto"])[0]
                items = run_parser({"url": url, "parser": pid}, url)
                self._json({"ok": True, "count": len(items), "sample": items[:5]})
            elif path == "/api/fav":
                self._json({"ok": True, "favorites": CFG.favorites})
            elif path == "/api/history":
                self._json({"ok": True, "history": CFG.history})
            elif path == "/api/translate":
                texts = json.loads(qs.get("texts", ["[]"])[0])
                self._json({"ok": True, "map": translate_many(texts)})
            elif path == "/img":
                got = img_fetch(qs.get("u", [""])[0], qs.get("r", [""])[0])
                if not got:
                    self._send(404, b"", "image/png")
                    return
                self._send(200, got[0], got[1], {"Cache-Control": "public, max-age=3600"})
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

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8") or "{}")
        except Exception:
            body = {}
        try:
            if p.path == "/api/config":
                res = CFG.update(body)
                valid = {s["id"] for s in CFG.sources}
                with STORE.lock:
                    for k in list(STORE.data):
                        if k not in valid:
                            STORE.data.pop(k, None)
                self._json({"ok": True, **res})
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
    """找到标题含软件名的可见窗口并置前（成功返回 True）"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        found = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_ssize_t, ctypes.c_ssize_t)

        def _cb(hwnd, _lp):
            try:
                if u.IsWindowVisible(hwnd):
                    n = u.GetWindowTextLengthW(hwnd)
                    if n:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        u.GetWindowTextW(hwnd, buf, n + 1)
                        if APP_NAME in buf.value:
                            found.append(hwnd)
            except Exception:
                pass
            return 1

        u.EnumWindows(CB(_cb), 0)
        if found:
            hwnd = found[0]
            u.ShowWindow(hwnd, 9)  # SW_RESTORE：最小化则还原
            u.SetForegroundWindow(hwnd)
            return True
    except Exception as e:
        log("聚焦已有窗口失败: %r" % (e,))
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
        try:
            _LOCAL_OPENER.open(
                "http://127.0.0.1:%d/api/show" % SINGLE_PORT, timeout=4)
        except Exception:
            pass
        log("检测到已有实例在运行，本次启动退出")
        sys.exit(0)
    return free_port()


def open_window(url):
    if os.environ.get("REBANG_NO_BROWSER"):
        return True
    mode = CFG.settings.get("openMode", "app")
    if mode == "app":
        cands = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        ]
        for exe in cands:
            if exe and os.path.exists(exe):
                try:
                    import subprocess
                    subprocess.Popen([exe, "--app=" + url, "--window-size=1320,880",
                                      "--disable-features=Translate"], close_fds=True)
                    return True
                except Exception as e:
                    log("启动浏览器失败: %r" % (e,))
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


def main():
    global HTTPD
    port = acquire_port()
    bind = os.environ.get("REBANG_BIND") or "127.0.0.1"
    HTTPD = ThreadingHTTPServer((bind, port), Handler)
    HTTPD.daemon_threads = True
    url = "http://%s:%d/" % (bind if bind != "0.0.0.0" else "127.0.0.1", port)
    log("启动 %s v%s @ %s" % (APP_NAME, APP_VER, url))
    threading.Thread(target=lambda: refresh_many([s for s in CFG.sources if s["enabled"]]),
                     daemon=True).start()
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
