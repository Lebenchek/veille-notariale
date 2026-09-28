"""Veille juridique notariale : collecte des sources officielles, classement par
mots-clés, page web + flux RSS. Aucun texte n'est réécrit : on affiche le titre,
la date et le sommaire publiés par la source, avec lien vers l'original.
Résumé IA : optionnel, désactivé sans ANTHROPIC_API_KEY, toujours étiqueté."""
import os, json, html, datetime as dt
import requests, feedparser

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(DOCS, "items.json")
TODAY = dt.date.today()
LOOKBACK_DAYS = 14
UA = {"User-Agent": "veille-notariale/1.0"}

MATIERES = {
 "Successions & libéralités": ["succession", "donation", "testament", "legs", "héritier", "hériter", "libéralité", "réserve héréditaire", "assurance-vie", "assurance vie", "indivision", "partage", "usufruit", "nue-propriété", "défunt", "de cujus", "mandat à effet posthume", "fiducie"],
 "Famille & régimes matrimoniaux": ["régime matrimonial", "contrat de mariage", "divorce", "pacs", "concubinage", "mariage", "époux", "conjoint", "filiation", "adoption", "autorité parentale", "mineur", "majeur protégé", "tutelle", "curatelle", "habilitation familiale", "mandat de protection future", "prestation compensatoire", "communauté"],
 "Immobilier & copropriété": ["vente immobilière", "immobilier", "immeuble", "copropriété", "syndic", "bail", "hypothèque", "publicité foncière", "cadastre", "urbanisme", "préemption", "servitude", "lotissement", "vefa", "vente en l'état futur", "promesse de vente", "compromis", "diagnostic", "terrain", "propriété", "foncier", "usucapion", "mitoyenn", "construction", "état descriptif de division"],
 "Sociétés & patrimoine professionnel": ["société civile", "sci ", "sarl", "sas ", "société", "cession de parts", "cession de titres", "fonds de commerce", "holding", "pacte d'associés", "associé", "gérant", "dirigeant", "entreprise", "transmission d'entreprise", "bail commercial", "bail rural", "gfa", "gaec", "exploitant agricole"],
 "Fiscalité": ["droits de mutation", "droits d'enregistrement", "plus-value", "impôt", "fiscal", "ifi", "taxe", "bofip", "dmto", "exonération", "abattement", "pinel", "lmnp", "tva", "csg", "prélèvements sociaux", "donation-partage", "pacte dutreil", "dutreil", "abus de droit"],
 "Droit international privé & européen": ["international", "règlement (ue)", "règlement ue", "européen", "certificat successoral européen", "loi applicable", "conflit de lois", "exequatur", "convention de la haye", "étranger", "non-résident", "nationalité", "apostille"],
 "Profession, déontologie & actes": ["notaire", "notarial", "office notarial", "acte authentique", "authentique", "acte notarié", "minutier", "déontologie", "csn", "chambre des notaires", "fichier central", "tracfin", "blanchiment", "devoir de conseil", "responsabilité du notaire", "signature électronique", "procuration", "vente à distance", "clause", "mandat", "sûreté", "cautionnement", "gage", "saisie", "surendettement", "prescription", "responsabilité civile"],
}
ALWAYS_TYPES = ("loi", "ordonnance")  # toujours conservées (à examiner) si pas de mot-clé

def classify(text):
    t = (text or "").lower()
    return [m for m, kws in MATIERES.items() if any(k in t for k in kws)]

def log(*a): print("[veille]", *a, flush=True)

def piste_token():
    cid, sec = os.getenv("PISTE_CLIENT_ID"), os.getenv("PISTE_CLIENT_SECRET")
    if not (cid and sec):
        log("Identifiants PISTE absents : Légifrance et Judilibre ignorés."); return None
    r = requests.post("https://oauth.piste.gouv.fr/api/oauth/token", data={
        "grant_type": "client_credentials", "client_id": cid, "client_secret": sec, "scope": "openid"}, timeout=30)
    r.raise_for_status(); return r.json()["access_token"]

def src_legifrance(tok):
    base = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", **UA}
    out = []
    conts = requests.post(f"{base}/consult/lastNJo", headers=h, json={"nbElement": 7}, timeout=60).json()
    for c in conts.get("containers", []):
        cid = c.get("id")
        d = c.get("dateParution") or c.get("datePubli")
        date = dt.datetime.utcfromtimestamp(d/1000).date().isoformat() if isinstance(d, (int, float)) else str(TODAY)
        try:
            jo = requests.post(f"{base}/consult/jorfCont", headers=h, json={"id": cid, "pageNumber": 1, "pageSize": 200}, timeout=60).json()
        except Exception as e:
            log("jorfCont", cid, e); continue
        for it in jo.get("items", []) or []:
            tid = it.get("id") or ""
            title = it.get("title") or it.get("titre") or ""
            if not tid.startswith("JORFTEXT") or not title: continue
            kind = title.split(" ")[0].lower()
            out.append({"id": tid, "source": "Légifrance (JORF)", "type": "Texte officiel", "date": date,
                        "title": title, "abstract": "", "url": f"https://www.legifrance.gouv.fr/jorf/id/{tid}",
                        "_kind": kind})
    return out

JUDILIBRE_QUERIES = ["notaire", "succession", "donation", "testament", "régime matrimonial", "divorce", "partage indivision",
                     "vente immobilière", "copropriété", "bail", "hypothèque", "publicité foncière", "société civile",
                     "cession de parts", "PACS", "assurance-vie", "usufruit", "prescription acquisitive", "acte authentique", "devoir de conseil"]

def src_judilibre(tok):
    h = {"Authorization": f"Bearer {tok}", **UA}
    url = "https://api.piste.gouv.fr/cassation/judilibre/v1.0/search"
    start = (TODAY - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    seen, out = set(), []
    for q in JUDILIBRE_QUERIES:
        try:
            r = requests.get(url, headers=h, timeout=60, params={"query": q, "date_start": start, "date_type": "creation",
                             "sort": "date", "order": "desc", "page_size": 50, "publication": ["b", "r", "l", "c"]})
            r.raise_for_status()
        except Exception as e:
            log("Judilibre", q, e); continue
        for x in r.json().get("results", []):
            if x["id"] in seen: continue
            seen.add(x["id"])
            themes = x.get("themes") or []
            title = f'Cass. {x.get("chamber","")} — {x.get("decision_date","")} — n° {x.get("number","")}'.replace("  ", " ")
            summ = x.get("summary") or ""
            ab = " | ".join(filter(None, [("Thèmes : " + " ; ".join(themes)) if themes else "", "Sommaire officiel : " + summ if summ else "",
                                          "Solution : " + x["solution"] if x.get("solution") else ""]))
            out.append({"id": "JUDI-" + x["id"], "source": "Cour de cassation (Judilibre)", "type": "Jurisprudence",
                        "date": x.get("decision_date", str(TODAY)), "title": title, "abstract": ab,
                        "url": f"https://www.courdecassation.fr/decision/{x['id']}", "_keep": True})
    return out


def _d(v):
    try:
        if isinstance(v, (int, float)): return dt.datetime.fromtimestamp(v/1000, dt.timezone.utc).date().isoformat()
        return str(v)[:10] if v else None
    except Exception: return None

def src_fonds(tok):
    """Conseil constitutionnel et Conseil d'Etat via l'API Légifrance (même compte PISTE)."""
    base = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", **UA}
    start = (TODAY - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    out, seen = [], set()
    for fond, label, path in (("CONSTIT", "Conseil constitutionnel", "cons"), ("CETAT", "Conseil d'État", "ceta")):
        for q in JUDILIBRE_QUERIES:
            body = {"fond": fond, "recherche": {"champs": [{"typeChamp": "ALL", "operateur": "ET", "criteres": [
                {"typeRecherche": "UN_DES_MOTS", "valeur": q, "operateur": "ET"}]}],
                "filtres": [{"facette": "DATE_DECISION", "dates": {"start": start, "end": str(TODAY)}}],
                "pageNumber": 1, "pageSize": 50, "operateur": "ET", "sort": "DATE_DESC", "typePagination": "DEFAUT"}}
            try:
                r = requests.post(f"{base}/search", headers=h, json=body, timeout=60); r.raise_for_status()
            except Exception as e:
                log(label, q, e); continue
            for x in r.json().get("results", []) or []:
                t = (x.get("titles") or [{}])[0]; tid, title = t.get("id"), t.get("title")
                if not tid or not title or tid in seen: continue
                seen.add(tid)
                out.append({"id": "LF-" + tid, "source": label, "type": "Jurisprudence", "date": _d(x.get("date")) or str(TODAY),
                            "title": title, "abstract": "", "url": f"https://www.legifrance.gouv.fr/{path}/id/{tid}", "_hint": q})
    return out

def src_rss():
    out, p = [], os.path.join(ROOT, "feeds.txt")
    if not os.path.exists(p): return out
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"): continue
        try: name, url, keep = (line.split("|") + ["0"])[:3]
        except ValueError: continue
        try:
            f = feedparser.parse(url, agent=UA["User-Agent"])
            if not f.entries: log("Flux vide/inaccessible :", name); continue
        except Exception as e:
            log("RSS", name, e); continue
        for e in f.entries[:60]:
            t = struct = e.get("published_parsed") or e.get("updated_parsed")
            date = dt.date(*t[:3]).isoformat() if t else str(TODAY)
            out.append({"id": "RSS-" + (e.get("id") or e.get("link")), "source": name, "type": "Doctrine / actualité",
                        "date": date, "title": e.get("title", ""), "abstract": html.unescape(e.get("summary", ""))[:600],
                        "url": e.get("link", ""), "_keep": keep.strip() == "1"})
    return out

def ai_summary(item):
    """Résumé optionnel, 2 phrases max, basé UNIQUEMENT sur le texte fourni.
    Utilise Google Gemini (niveau gratuit) si GEMINI_API_KEY est présent,
    sinon l'API Anthropic si ANTHROPIC_API_KEY est présent (payant), sinon rien."""
    if not (item.get("abstract") or len(item["title"]) > 60): return None
    prompt = ("Reformule en 2 phrases maximum, en français, le texte ci-dessous, en t'appuyant UNIQUEMENT sur ce texte. "
              "N'ajoute aucune information, aucun numéro d'article, aucune date qui n'y figure pas. "
              "Si le texte est insuffisant, réponds exactement : INSUFFISANT.\n\nTitre : " + item["title"] + "\n" + item.get("abstract", ""))

    gkey = os.getenv("GEMINI_API_KEY")
    if gkey:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gkey}",
                timeout=60, headers={"content-type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": 200, "temperature": 0.2}})
            r.raise_for_status()
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            return None if txt.startswith("INSUFFISANT") else txt
        except Exception as e:
            log("Résumé IA (Gemini)", e); return None

    akey = os.getenv("ANTHROPIC_API_KEY")
    if akey:
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                headers={"x-api-key": akey, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            txt = r.json()["content"][0]["text"].strip()
            return None if txt.startswith("INSUFFISANT") else txt
        except Exception as e:
            log("Résumé IA (Anthropic)", e); return None

    return None

def main():
    os.makedirs(DOCS, exist_ok=True)
    store = {}
    if os.path.exists(DATA):
        store = {i["id"]: i for i in json.load(open(DATA, encoding="utf-8"))}
    new = []
    try:
        tok = piste_token()
        if tok:
            for fn in (src_legifrance, src_judilibre, src_fonds):
                try: new += fn(tok)
                except Exception as e: log(fn.__name__, "échec :", e)
    except Exception as e:
        log("PISTE :", e)
    new += src_rss()
    added = 0
    for it in new:
        if it["id"] in store: continue
        mats = classify(it["title"] + " " + it.get("abstract", "")) or (classify(it.get("_hint", "")) if it.get("_hint") else [])
        if not mats and it.get("_kind") in ALWAYS_TYPES: mats = ["Texte majeur à examiner"]
        if not mats and it.pop("_keep", False): mats = ["Autres"]
        if not mats: continue
        it.pop("_keep", None); it.pop("_kind", None); it.pop("_hint", None)
        it["matieres"], it["first_seen"] = mats, str(TODAY)
        store[it["id"]] = it; added += 1
    n = 0
    for it in sorted(store.values(), key=lambda i: i["date"], reverse=True):
        if n >= 25: break
        if "ai" not in it and it["first_seen"] == str(TODAY):
            it["ai"] = ai_summary(it); n += 1
    items = sorted(store.values(), key=lambda i: (i["date"], i["first_seen"]), reverse=True)
    json.dump(items, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    write_html(items); write_rss(items)
    log(f"{added} nouveaux éléments, {len(items)} au total.")

def write_rss(items):
    e = html.escape
    rows = "".join(f"<item><title>{e(i['title'])}</title><link>{e(i['url'])}</link><guid>{e(i['id'])}</guid>"
                   f"<description>{e(i.get('abstract',''))}</description><category>{e(', '.join(i['matieres']))}</category></item>" for i in items[:100])
    open(os.path.join(DOCS, "feed.xml"), "w", encoding="utf-8").write(
        f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Veille juridique notariale</title>'
        f'<link>.</link><description>Textes, jurisprudence et doctrine</description>{rows}</channel></rss>')

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Veille juridique notariale</title><link rel="alternate" type="application/rss+xml" href="feed.xml">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="manifest" href="manifest.json"><meta name="theme-color" content="#14213d">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Veille notariale"><link rel="apple-touch-icon" href="icon.png">
<style>:root{--bg:#f3f0e8;--fg:#1d2433;--card:#fff;--mut:#6b7280;--bd:#e5e0d3;--navy:#14213d;--navy2:#26426b;--gold:#b8893b;--jur:#26426b;--txt:#8c2f39;--doc:#2f7a5b}
@media(prefers-color-scheme:dark){:root{--bg:#0f1420;--fg:#e9ecf3;--card:#182033;--mut:#98a2b3;--bd:#27314a;--gold:#d9a95a;--jur:#7fa6e0;--txt:#e58a94;--doc:#6fcfa3}}
*{box-sizing:border-box}html{scroll-padding-top:env(safe-area-inset-top,0px)}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 Inter,system-ui,sans-serif;overflow-x:hidden}
header{position:relative;overflow:hidden;color:#fff;padding:calc(30px + env(safe-area-inset-top,0px)) 18px 26px;
background:radial-gradient(600px 240px at 90% -10%,rgba(217,169,90,.35),transparent 70%),repeating-linear-gradient(45deg,rgba(255,255,255,.035) 0 2px,transparent 2px 14px),linear-gradient(135deg,#0d1730,#26426b)}
.w{max-width:860px;margin:auto;position:relative}.eyebrow{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:#e3c58c;font-weight:600}
h1{font:800 34px/1.1 'Playfair Display',Georgia,serif;margin:8px 0 10px;letter-spacing:.01em}
.orn{display:flex;align-items:center;gap:12px;color:#e3c58c;margin:0 0 12px}.orn:before,.orn:after{content:"";height:1px;flex:1;background:linear-gradient(90deg,transparent,#e3c58c)}.orn:after{transform:scaleX(-1)}
.sub{color:#cbd5e6;font-size:13.5px}.sub a{color:#e3c58c}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px}
.stat{background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.18);border-radius:14px;padding:11px 8px;text-align:center;backdrop-filter:blur(6px)}
.stat b{display:block;font:700 24px 'Playfair Display',Georgia,serif;color:#f1d9a6}.stat span{font-size:10.5px;color:#cbd5e6;text-transform:uppercase;letter-spacing:.08em}
.band{height:5px;background:linear-gradient(90deg,var(--gold),#f1d9a6,var(--gold))}
main{max-width:860px;margin:auto;padding:22px 14px 30px}
.search{position:relative}.search input{width:100%;padding:14px 16px 14px 46px;border:1px solid var(--bd);border-radius:14px;font:16px Inter,system-ui;background:var(--card);color:var(--fg);outline:none;box-shadow:0 2px 8px rgba(20,33,61,.06)}
.search input:focus{border-color:var(--gold);box-shadow:0 0 0 3px rgba(184,137,59,.22)}
.search svg{position:absolute;left:16px;top:50%;transform:translateY(-50%);width:18px;height:18px;stroke:var(--mut);fill:none;stroke-width:2}
.chips{display:flex;gap:8px;margin:14px 0 4px;overflow-x:auto;padding:2px 2px 8px;scrollbar-width:none}.chips::-webkit-scrollbar{display:none}
.chip{flex:none;border:1px solid var(--bd);background:var(--card);color:var(--fg);padding:8px 14px;border-radius:99px;font:500 13px Inter,system-ui;cursor:pointer;transition:.15s}
.chip.on{background:linear-gradient(135deg,var(--navy),var(--navy2));color:#fff;border-color:transparent}
@media(prefers-color-scheme:dark){.chip.on{background:var(--gold);color:#14213d}}
.lbl{font:700 12px Inter;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);margin:14px 2px 4px}
.card{display:grid;grid-template-columns:44px minmax(0,1fr);gap:14px;background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 4px 14px rgba(20,33,61,.06);position:relative;overflow:hidden}
.card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c)}
.card.t-Jurisprudence{--c:var(--jur)}.card.t-Texte{--c:var(--txt)}.card.t-Doctrine{--c:var(--doc)}
.ico{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;font:700 20px 'Playfair Display',Georgia,serif;color:#fff;background:var(--c)}
.body{min-width:0;overflow-wrap:anywhere}
.top{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;margin-bottom:8px}
.badge{font:600 10.5px Inter,system-ui;text-transform:uppercase;letter-spacing:.07em;padding:3px 9px;border-radius:99px;color:var(--c);border:1px solid var(--c)}
.badge.new{background:var(--gold);border-color:var(--gold);color:#14213d}.date{font-size:12.5px;color:var(--mut);font-weight:500}
.card a.t{display:block;font:700 17px/1.35 'Playfair Display',Georgia,serif;color:var(--fg);text-decoration:none}.card a.t:hover{color:var(--gold)}
.src{font-size:12.5px;color:var(--mut);margin-top:4px}.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.tag{font-size:11.5px;padding:3px 10px;border-radius:99px;background:rgba(184,137,59,.13);border:1px solid rgba(184,137,59,.35)}
.abs{font-size:14px;margin-top:8px;opacity:.92}.ai{margin-top:10px;padding:9px 12px;border-radius:10px;background:rgba(38,66,107,.08);border:1px dashed var(--navy2);font-size:13px;color:var(--mut)}
.more{display:inline-block;margin-top:10px;font:600 13px Inter;color:var(--gold);text-decoration:none}
footer{max-width:860px;margin:0 auto;padding:10px 16px calc(28px + env(safe-area-inset-bottom,0px));font-size:12px;color:var(--mut);border-top:1px solid var(--bd)}
</style></head><body>
<header><div class="w"><div class="eyebrow">Actualité juridique</div><h1>Veille notariale</h1><div class="orn">§</div>
<div class="sub">Textes · Jurisprudence · Doctrine — sources officielles<br>Mise à jour <span id="upd">__UPD__</span> · <a href="feed.xml">Flux RSS</a></div>
<div class="stats"><div class="stat"><b id="n1">0</b><span>Éléments</span></div><div class="stat"><b id="n2">0</b><span>Nouveaux 7 j</span></div><div class="stat"><b id="n3">0</b><span>Matières</span></div></div></div></header><div class="band"></div>
<main><div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg><input id="q" placeholder="Rechercher : donation, indivision, Cass.…"></div><div class="chips" id="ch"></div><div class="lbl">Dernières publications</div><div id="list"></div></main>
<footer>Contenus repris des sources officielles (titre, date, sommaire publiés par la source). Seul le texte du lien fait foi : vérifiez toujours sur Légifrance, Judilibre ou la source d'origine avant tout usage professionnel. Les résumés automatiques sont générés par IA à partir du seul texte de la source et peuvent contenir des erreurs.</footer>
<script>const D=__DATA__;let mat="",typ="";const $=id=>document.getElementById(id);
const M=[...new Set(D.flatMap(i=>i.matieres))].sort(),T=[...new Set(D.map(i=>i.type))];
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const IC={Jurisprudence:"⚖",Texte:"§",Doctrine:"✎"};
const cut=Date.now()-7*864e5,isNew=i=>new Date(i.first_seen).getTime()>=cut;
$("n1").textContent=D.length;$("n2").textContent=D.filter(isNew).length;$("n3").textContent=M.length;
function chips(){$("ch").innerHTML=["Tout",...T,...M].map(x=>`<button class="chip ${(x==="Tout"&&!mat&&!typ)||x===mat||x===typ?"on":""}" data-x="${esc(x)}">${esc(x)}</button>`).join("");
document.querySelectorAll(".chip").forEach(b=>b.onclick=()=>{const x=b.dataset.x;if(x==="Tout"){mat="";typ=""}else if(M.includes(x)){mat=mat===x?"":x}else{typ=typ===x?"":x}chips();draw()})}
const fd=d=>{try{return new Date(d).toLocaleDateString("fr-FR",{day:"numeric",month:"short",year:"numeric"})}catch(e){return d}};
function draw(){const q=$("q").value.toLowerCase();const r=D.filter(i=>(!mat||i.matieres.includes(mat))&&(!typ||i.type===typ)&&(!q||(i.title+i.abstract).toLowerCase().includes(q))).slice(0,300);
$("list").innerHTML=r.map(i=>{const k=i.type.split(" ")[0];return `<div class="card t-${esc(k)}"><div class="ico">${IC[k]||"§"}</div><div class="body"><div class="top"><span class="badge">${esc(i.type)}</span>${isNew(i)?'<span class="badge new">Nouveau</span>':""}<span class="date">${fd(i.date)}</span></div>
<a class="t" href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a><div class="src">${esc(i.source)}</div>
${i.abstract?`<div class="abs">${esc(i.abstract)}</div>`:""}${i.ai?`<div class="ai"><b>Résumé automatique (IA), à vérifier :</b> ${esc(i.ai)}</div>`:""}
<div class="tags">${i.matieres.map(m=>`<span class="tag">${esc(m)}</span>`).join("")}</div><a class="more" href="${esc(i.url)}" target="_blank" rel="noopener">Consulter la source officielle →</a></div></div>`}).join("")||'<p style="color:var(--mut)">Aucun résultat.</p>'}
$("q").oninput=draw;chips();draw();</script></body></html>"""

def write_html(items):
    page = PAGE.replace("__UPD__", dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y %H:%M UTC")).replace(
        "__DATA__", json.dumps(items, ensure_ascii=False).replace("</", "<\\/"))
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(page)

if __name__ == "__main__":
    main()
