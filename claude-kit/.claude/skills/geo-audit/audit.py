#!/usr/bin/env python3
"""Проверка собранного сайта калаев.рф перед выкладкой.

Запуск из корня проекта после `npm run build`:
    python3 .claude/skills/geo-audit/audit.py dist
    python3 .claude/skills/geo-audit/audit.py dist --only /articles/psihosomatika-
    python3 .claude/skills/geo-audit/audit.py dist --live      # плюс проверки живого сайта

Уровни:
    ERROR    технический брак, выкладывать нельзя (код выхода 1)
    CONTENT  страница не соответствует стандарту статьи из CLAUDE.md
    WARN     желательно исправить

Только стандартная библиотека Python 3.8+.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from html.parser import HTMLParser

SITE = "https://калаев.рф"
PERSON_ID = SITE + "/about/#person"
# Формулировки квалификации, которых не должно быть нигде (сайт, llms.txt).
FORBIDDEN_PHRASES = [r"клиническ\w*\s+психолог\w*"]
# Свои площадки: ссылки на них не считаются источниками.
OWN_DOMAINS = ("калаев.рф", "xn--80aaglwh.xn--p1ai", "t.me", "vk.com", "dzen.ru",
               "b17.ru", "vc.ru", "tenchat.ru")
SOURCE_HEADING = re.compile(r"^(источники|литература|на что опирается статья)", re.I)
VISIBLE_DATE = re.compile(
    r"(обновлено|опубликовано)\s*:?\s*(\d{1,2}\s+(января|февраля|марта|апреля|мая|июня|июля|"
    r"августа|сентября|октября|ноября|декабря)\s+20\d\d|\d{2}\.\d{2}\.20\d\d)", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split())}
LEAD_MARKERS = ("если коротко", "коротко:", "короткий ответ")
STOP = set("и в во на не что как это для или при от до по из у к о об а но же ли "
           "почему когда где кто чем что-то если мой моя меня себя себе вас вам".split())


def norm_host(url):
    """Приводит https://xn--80aaglwh.xn--p1ai/... к https://калаев.рф/..."""
    m = re.match(r"(https?://)([^/]+)(.*)", url or "")
    if not m:
        return url
    host = m.group(2)
    try:
        host = host.encode("ascii").decode("idna") if "xn--" in host else host
    except UnicodeError:
        pass
    return m.group(1) + host.lower() + m.group(3)


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.metas = {}
        self.links = []
        self.ld_raw = []
        self.headings = []          # (tag, text)
        self.blocks = []            # (kind, text, attrs) в порядке появления внутри <main>
        self.hrefs = []             # (href, heading_before)
        self._cur = None
        self._buf = ""
        self._attrs = {}
        self._in_main = 0
        self._skip = 0
        self._last_heading = ""
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = a.get("name") or a.get("property") or a.get("http-equiv")
            if key:
                self.metas[key.lower()] = a.get("content", "")
        elif tag == "link":
            self.links.append((a.get("rel", ""), a.get("href", "")))
        elif tag == "main":
            self._in_main += 1
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._cur, self._buf = "ld", ""
            return
        elif tag in ("script", "style", "nav", "header", "footer", "noscript"):
            self._skip += 1
        elif tag == "a" and self._in_main and not self._skip:
            self.hrefs.append((a.get("href", ""), self._last_heading))
        if tag in ("title", "h1", "h2", "h3", "p", "li", "summary", "dt", "dd") and self._cur is None:
            self._cur, self._buf, self._attrs = tag, "", a

    def handle_endtag(self, tag):
        if self._cur == "ld" and tag == "script":
            self.ld_raw.append(self._buf)
            self._cur = None
            return
        if tag in ("script", "style", "nav", "header", "footer", "noscript") and self._skip:
            self._skip -= 1
        if tag == "main" and self._in_main:
            self._in_main -= 1
        if self._cur == tag:
            text = re.sub(r"\s+", " ", self._buf).strip()
            if tag == "title":
                self.title = text
            elif not self._skip:
                if tag in ("h1", "h2", "h3"):
                    self.headings.append((tag, text))
                    self._last_heading = text
                if self._in_main or tag == "h1":
                    self.blocks.append((tag, text, self._attrs))
                self.text_parts.append(text)
            self._cur = None

    def handle_data(self, data):
        if self._cur:
            self._buf += data


def walk_ld(obj, out):
    if isinstance(obj, dict):
        out.append(obj)
        for v in obj.values():
            walk_ld(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_ld(v, out)


def stems(text):
    words = re.findall(r"[а-яёa-z]+", text.lower())
    return {w[:5] for w in words if len(w) > 3 and w not in STOP}


def ru_date(s):
    m = re.search(r"(\d{1,2})\s+([а-я]+)\s+(20\d\d)", s)
    if m and m.group(2) in MONTHS:
        return "%s-%02d-%02d" % (m.group(3), MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"(\d{2})\.(\d{2})\.(20\d\d)", s)
    if m:
        return "%s-%s-%s" % (m.group(3), m.group(2), m.group(1))
    return None


class Report:
    def __init__(self):
        self.items = defaultdict(list)  # level -> [(where, msg)]

    def add(self, level, where, msg):
        self.items[level].append((where, msg))

    def print(self):
        total = {k: len(v) for k, v in self.items.items()}
        print("# Отчёт проверки сайта\n")
        print("ERROR: %d · CONTENT: %d · WARN: %d\n" % (
            total.get("ERROR", 0), total.get("CONTENT", 0), total.get("WARN", 0)))
        for level in ("ERROR", "CONTENT", "WARN"):
            rows = self.items.get(level)
            if not rows:
                continue
            print("## %s\n" % level)
            by_msg = defaultdict(list)
            for where, msg in rows:
                by_msg[msg].append(where)
            for msg, wheres in sorted(by_msg.items(), key=lambda x: -len(x[1])):
                print("- **%s** (%d): %s" % (msg, len(wheres), ", ".join(wheres[:12]) +
                                              (" …" if len(wheres) > 12 else "")))
            print()
        return 1 if self.items.get("ERROR") else 0


def page_kind(url_path):
    if url_path.startswith("/articles/") and url_path != "/articles/":
        return "article"
    if url_path.startswith("/guides/") and url_path != "/guides/":
        return "guide"
    if url_path.startswith("/tests/") and url_path != "/tests/":
        return "test"
    return "page"


def load_sitemaps(dist):
    urls = {}
    for name in sorted(os.listdir(dist)):
        if re.match(r"sitemap.*\.xml$", name):
            text = open(os.path.join(dist, name), encoding="utf-8").read()
            for block in re.findall(r"<url>(.*?)</url>", text, re.S):
                loc = re.search(r"<loc>(.*?)</loc>", block)
                lm = re.search(r"<lastmod>(.*?)</lastmod>", block)
                if loc:
                    urls[norm_host(loc.group(1).strip())] = lm.group(1)[:10] if lm else None
    return urls


def audit_page(rep, url_path, html_text, sitemap):
    p = Page()
    p.feed(html_text)
    kind = page_kind(url_path)
    url = SITE + url_path
    w = url_path

    robots = p.metas.get("robots", "")
    noindex = "noindex" in robots.lower()
    canon = [norm_host(h) for rel, h in p.links if "canonical" in (rel or "")]
    if not noindex:
        if not canon:
            rep.add("ERROR", w, "нет canonical")
        elif canon[0] != url:
            rep.add("ERROR", w, "canonical указывает на другой адрес")
        if not p.title:
            rep.add("ERROR", w, "нет <title>")
        if not p.metas.get("description"):
            rep.add("ERROR", w, "нет meta description")
        h1 = [t for tag, t in p.headings if tag == "h1"]
        if len(h1) != 1:
            rep.add("ERROR", w, "на странице не ровно один H1")
        if url not in sitemap:
            rep.add("ERROR", w, "страница не попала в sitemap")
    elif url in sitemap:
        rep.add("ERROR", w, "страница с noindex стоит в sitemap")

    ld = []
    for raw in p.ld_raw:
        try:
            walk_ld(json.loads(raw), ld)
        except ValueError:
            rep.add("ERROR", w, "битый JSON-LD")
    types = Counter()
    for o in ld:
        t = o.get("@type")
        for x in (t if isinstance(t, list) else [t]):
            types[x] += 1

    services = [o for o in ld if o.get("@type") == "ProfessionalService"]
    persons = [o for o in ld if o.get("@type") == "Person"]

    text = " ".join(p.text_parts)
    for pat in FORBIDDEN_PHRASES:
        if re.search(pat, text, re.I) or any(re.search(pat, json.dumps(o, ensure_ascii=False), re.I) for o in ld):
            rep.add("ERROR", w, "запрещённая формулировка квалификации (см. CLAUDE.md, «Факты об авторе»)")

    if url_path == "/about/":
        if not any(o.get("@id") == PERSON_ID for o in persons):
            rep.add("WARN", w, "у Person на /about/ нет @id %s" % PERSON_ID)

    result = {"services": [o.get("description", "") for o in services]}
    if noindex or kind == "page":
        return result

    # ---- статьи, гайды, тесты ----
    arts = [o for o in ld if o.get("@type") in ("Article", "BlogPosting", "MedicalWebPage")]
    if kind in ("article", "guide"):
        if not arts:
            rep.add("ERROR", w, "нет Article в разметке")
        for a in arts[:1]:
            dp, dm = (a.get("datePublished") or "")[:10], (a.get("dateModified") or "")[:10]
            if not dp or not dm:
                rep.add("ERROR", w, "в Article нет datePublished или dateModified")
            elif dm < dp:
                rep.add("ERROR", w, "dateModified раньше datePublished")
            lm = sitemap.get(url)
            if dm and lm != dm:
                rep.add("ERROR", w, "lastmod в sitemap не равен dateModified")
            author = a.get("author")
            author = author[0] if isinstance(author, list) and author else author
            if not isinstance(author, dict) or author.get("@id") != PERSON_ID:
                rep.add("WARN", w, "author в Article не ссылается на @id автора")
            vis = VISIBLE_DATE.findall(text)
            if not vis:
                rep.add("CONTENT", w, "нет видимой строки «Опубликовано/Обновлено: дата»")
            else:
                upd = [ru_date(m[1]) for m in vis if m[0].lower().startswith("обнов")]
                if upd and dm and upd[0] != dm:
                    rep.add("ERROR", w, "видимая дата обновления не совпадает с dateModified")
        if p.metas.get("og:type") != "article":
            rep.add("WARN", w, "og:type не article")

    og_img = p.metas.get("og:image", "")
    if og_img and not og_img.startswith("http"):
        rep.add("WARN", w, "og:image задан относительным путём")

    if kind == "article":
        words = len(text.split())
        if words < 350:
            rep.add("WARN", w, "тонкая статья (< 350 слов)")
        # прямой ответ сразу после H1
        paras = []
        seen_h1 = False
        for tag, t, attrs in p.blocks:
            if tag == "h1":
                seen_h1 = True
                continue
            if seen_h1 and tag == "p" and len(t.split()) >= 8 and not VISIBLE_DATE.search(t):
                paras.append((t, attrs))
            if len(paras) == 1:
                break
        if not paras:
            rep.add("CONTENT", w, "после H1 нет абзаца-ответа")
        else:
            lead, attrs = paras[0]
            cls = attrs.get("class") or ""
            h1 = next((t for tag, t in p.headings if tag == "h1"), p.title)
            overlap = len(stems(lead) & stems(h1))
            marked = "answer-lead" in cls or lead.lower().startswith(LEAD_MARKERS)
            if not marked:
                rep.add("CONTENT", w, "первый абзац не оформлен как ответ (class=\"answer-lead\")")
            if len(lead.split()) > 90:
                rep.add("CONTENT", w, "первый абзац длиннее 90 слов")
            if overlap < 2:
                rep.add("CONTENT", w, "первый абзац почти не повторяет тему H1 (похоже на сценку, а не ответ)")
        # источники
        ext = [h for h, head in p.hrefs if h.startswith("http") and not any(d in norm_host(h) for d in OWN_DOMAINS)]
        src_heads = [t for tag, t in p.headings if SOURCE_HEADING.match(t)]
        if not src_heads:
            rep.add("CONTENT", w, "нет блока «Источники»")
        elif len(ext) < 2:
            rep.add("CONTENT", w, "в блоке источников меньше 2 внешних ссылок")
        # FAQ
        faq_q = [o.get("name", "") for o in ld if o.get("@type") == "Question"]
        pairs = [(i, j) for i in range(len(faq_q)) for j in range(i + 1, len(faq_q))]
        if any(stems(faq_q[i]) and stems(faq_q[j]) and
               len(stems(faq_q[i]) & stems(faq_q[j])) / len(stems(faq_q[i]) | stems(faq_q[j])) >= 0.6
               for i, j in pairs):
            rep.add("WARN", w, "почти одинаковые вопросы в FAQ")
        if len(faq_q) > 6:
            rep.add("WARN", w, "в FAQ больше 6 вопросов")
        h3_q = {t for tag, t in p.headings if tag == "h3"}
        if faq_q and not all(q in h3_q or q in text for q in faq_q):
            rep.add("ERROR", w, "вопрос из FAQPage не виден на странице")
    return result


def audit_site(dist, only, rep):
    sitemap = load_sitemaps(dist)
    if not sitemap:
        rep.add("ERROR", "sitemap", "в сборке нет sitemap*.xml")
    pages = {}
    for root, dirs, files in os.walk(dist):
        rel = "/" + os.path.relpath(root, dist).replace(os.sep, "/").strip("./")
        rel = "/" if rel in ("/", "/.") else rel.rstrip("/") + "/"
        if "index.html" in files:
            pages[rel] = os.path.join(root, "index.html")
        elif any(os.path.isdir(os.path.join(root, d)) and os.path.exists(os.path.join(root, d, "index.html")) for d in dirs) \
                and not rel.startswith(("/_astro", "/fonts", "/images")):
            rep.add("WARN", rel, "у раздела нет своей страницы (на хостинге будет 403)")

    services = Counter()
    for path, f in sorted(pages.items()):
        if only and not path.startswith(only):
            continue
        res = audit_page(rep, path, open(f, encoding="utf-8").read(), sitemap)
        for d in res["services"]:
            services[d] += 1
    if len(services) > 1:
        rep.add("WARN", "ProfessionalService", "%d разных описаний услуги на разных страницах" % len(services))

    if not only:
        for url in sitemap:
            path = url[len(SITE):] if url.startswith(SITE) else None
            if path is None:
                rep.add("ERROR", url, "в sitemap адрес чужого домена или другой записи домена")
            elif path not in pages:
                rep.add("ERROR", path, "в sitemap есть адрес, которого нет в сборке")
        for u, lm in sitemap.items():
            if not lm and u.startswith(SITE):
                path = u[len(SITE):]
                level = "ERROR" if page_kind(path) in ("article", "guide") else "WARN"
                rep.add(level, path, "в sitemap нет lastmod")

    robots = os.path.join(dist, "robots.txt")
    if not os.path.exists(robots):
        rep.add("ERROR", "robots.txt", "нет robots.txt в сборке")
    else:
        rt = open(robots, encoding="utf-8").read()
        groups = re.split(r"(?im)^\s*user-agent\s*:", rt)
        for g in groups[1:]:
            agent = g.splitlines()[0].strip()
            if re.search(r"(?im)^\s*disallow\s*:\s*/\s*$", g):
                rep.add("ERROR", "robots.txt", "полный запрет для %s" % agent)
        sm = re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", rt)
        if not sm:
            rep.add("ERROR", "robots.txt", "нет строки Sitemap")
        elif any(not s.isascii() for s in sm):
            rep.add("WARN", "robots.txt", "адрес Sitemap не в punycode (https://xn--80aaglwh.xn--p1ai/...)")

    llms = os.path.join(dist, "llms.txt")
    if os.path.exists(llms):
        lt = open(llms, encoding="utf-8").read()
        for pat in FORBIDDEN_PHRASES:
            if re.search(pat, lt, re.I):
                rep.add("ERROR", "llms.txt", "запрещённая формулировка квалификации")


def audit_live(rep):
    base = "https://xn--80aaglwh.xn--p1ai"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)

    def head(url):
        try:
            r = opener.open(urllib.request.Request(url, method="GET"), timeout=20)
            return r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers
        except Exception as e:  # noqa: BLE001
            return None, str(e)

    for url, expect in [("https://www.xn--80aaglwh.xn--p1ai/", 301), (base + "/index.html", 301),
                        (base + "/articles/", 200), (base + "/nesushchestvuyushchaya-stranica/", 404)]:
        code, _ = head(url)
        if code not in (expect, 308 if expect == 301 else expect):
            rep.add("ERROR", url, "живой сайт отвечает %s, ожидалось %s" % (code, expect))
    for path in ("/robots.txt", "/sitemap-index.xml"):
        code, h = head(base + path)
        ct = h.get("Content-Type", "") if hasattr(h, "get") else ""
        if "charset" not in ct.lower():
            rep.add("WARN", path, "сервер отдаёт без charset=utf-8 (%s)" % ct)
    sm = urllib.request.urlopen(base + "/sitemap-0.xml", timeout=30).read().decode("utf-8")
    live = {norm_host(u) for u in re.findall(r"<loc>(.*?)</loc>", sm)}
    rep.add("WARN", "sitemap", "на живом сайте в sitemap %d адресов (сверьте со сборкой)" % len(live))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dist", help="папка сборки Astro (обычно dist)")
    ap.add_argument("--only", help="проверять только адреса с этим началом, например /articles/psihosomatika-")
    ap.add_argument("--live", action="store_true", help="дополнительно проверить живой сайт")
    args = ap.parse_args()
    rep = Report()
    audit_site(args.dist, args.only, rep)
    if args.live:
        audit_live(rep)
    sys.exit(rep.print())


if __name__ == "__main__":
    main()
