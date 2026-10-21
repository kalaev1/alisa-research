#!/usr/bin/env python3
"""Проставляет в sitemap*.xml сборки lastmod = dateModified каждой страницы.

Запуск после `npm run build`, до audit.py и до выкладки:
    python3 .claude/skills/geo-fix/sync_sitemap.py dist

Откуда берётся дата (по порядку): dateModified из JSON-LD страницы,
затем datePublished, затем уже стоящий lastmod. Если даты нет нигде,
lastmod не выдумывается, адрес попадает в список «без даты».
Сегодняшняя дата никогда не подставляется сама: это была бы фальшивая свежесть.
"""
import json
import os
import re
import sys

SITE_HOSTS = ("https://калаев.рф", "https://xn--80aaglwh.xn--p1ai")


def page_dates(html):
    found = {}
    for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                for k in ("dateModified", "datePublished"):
                    if k in o and k not in found:
                        found[k] = str(o[k])[:10]
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    return found.get("dateModified") or found.get("datePublished")


def local_file(dist, loc):
    for host in SITE_HOSTS:
        if loc.startswith(host):
            path = loc[len(host):].lstrip("/")
            return os.path.join(dist, path, "index.html") if not path.endswith(".html") else os.path.join(dist, path)
    return None


def main():
    dist = sys.argv[1] if len(sys.argv) > 1 else "dist"
    changed, undated = 0, []
    for name in sorted(os.listdir(dist)):
        if not re.match(r"sitemap-\d+\.xml$", name):
            continue
        fn = os.path.join(dist, name)
        xml = open(fn, encoding="utf-8").read()

        def fix(m):
            nonlocal changed
            block = m.group(0)
            loc = re.search(r"<loc>(.*?)</loc>", block).group(1).strip()
            f = local_file(dist, loc)
            date = page_dates(open(f, encoding="utf-8").read()) if f and os.path.exists(f) else None
            old = re.search(r"<lastmod>(.*?)</lastmod>", block)
            if not date:
                if not old:
                    undated.append(loc)
                return block
            if old and old.group(1)[:10] == date:
                return block
            changed += 1
            if old:
                return block.replace(old.group(0), "<lastmod>%s</lastmod>" % date)
            return block.replace("</loc>", "</loc><lastmod>%s</lastmod>" % date, 1)

        new = re.sub(r"<url>.*?</url>", fix, xml, flags=re.S)
        if new != xml:
            open(fn, "w", encoding="utf-8").write(new)
    print("lastmod обновлён у %d адресов" % changed)
    if undated:
        print("без даты (%d), добавьте даты в разметку этих страниц:" % len(undated))
        for u in undated:
            print("  " + u)


if __name__ == "__main__":
    main()
