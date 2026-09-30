"""Veille juridique notariale : collecte des sources officielles, classement par
mots-clés, page web + flux RSS. Aucun texte n'est réécrit : on affiche le titre,
la date et le sommaire publiés par la source, avec lien vers l'original.
Résumé IA : optionnel, désactivé sans ANTHROPIC_API_KEY, toujours étiqueté."""
import os, json, html, re, time, datetime as dt
import requests, feedparser

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(DOCS, "items.json")
TODAY = dt.date.today()
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "14"))  # 14 par défaut ; passez LOOKBACK_DAYS=180 pour un remplissage initial de 6 mois
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

ARTICLE_RE = re.compile(
    r"articles?\s+((?:\d+(?:-\d+)?(?:\s*(?:,|et)\s*)?)+)\s+(?:du|de la|des)\s+(code[^,.;\n]{0,45})",
    re.IGNORECASE)

def extract_articles(text):
    found, seen = [], set()
    for m in ARTICLE_RE.finditer(text or ""):
        label = f"art. {m.group(1).strip()} {m.group(2).strip().rstrip(chr(39)+chr(46))}"
        label = re.sub(r"\s+", " ", label)[:60]
        if label.lower() not in seen:
            seen.add(label.lower()); found.append(label)
    return found[:5]

_MOIS = {"janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,"juillet":7,
         "août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12,"decembre":12}
_DATE_TITRE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(_MOIS) + r")\s+(\d{4})", re.IGNORECASE)

def date_from_title(title):
    """Extrait une date française 'DD mois YYYY' directement du titre, en secours quand l'API ne la fournit pas."""
    m = _DATE_TITRE_RE.search(title or "")
    if not m: return None
    try:
        return f"{int(m.group(3)):04d}-{_MOIS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    except Exception:
        return None

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
    nb_jo = min(max(LOOKBACK_DAYS, 7) + 5, 200)  # marge de sécurité, plafonné pour ne pas saturer l'API
    conts = requests.post(f"{base}/consult/lastNJo", headers=h, json={"nbElement": nb_jo}, timeout=60).json()
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
            if r.status_code >= 400:
                log(f"Judilibre '{q}' : échec {r.status_code} — {(r.text or '')[:200]}"); continue
        except Exception as e:
            log("Judilibre", q, e); continue
        for x in r.json().get("results", []):
            if x["id"] in seen: continue
            themes = x.get("themes") or []
            title = f'Cass. {x.get("chamber","")} — {x.get("decision_date","")} — n° {x.get("number","")}'.replace("  ", " ")
            summ = x.get("summary") or ""
            chamber = (x.get("chamber") or "").lower()
            mentions_notaire = "notair" in (title + " " + summ + " " + " ".join(themes)).lower()
            # Hors sujet pour un notaire : décisions purement pénales, sociales ou prud'homales,
            # sauf si un notaire est explicitement en cause (ex : faute professionnelle).
            if any(k in chamber for k in ("crim", "sociale", "prud")) and not mentions_notaire:
                continue
            seen.add(x["id"])
            ab = " | ".join(filter(None, [("Thèmes : " + " ; ".join(themes)) if themes else "", "Sommaire officiel : " + summ if summ else "",
                                          "Solution : " + x["solution"] if x.get("solution") else ""]))
            pub = x.get("publication") or []
            out.append({"id": "JUDI-" + x["id"], "source": "Cour de cassation (Judilibre)", "type": "Jurisprudence",
                        "date": x.get("decision_date", str(TODAY)), "title": title, "abstract": ab,
                        "url": f"https://www.courdecassation.fr/decision/{x['id']}", "_keep": True,
                        "bulletin": bool(pub) and any(p in ("b", "r") for p in pub)})
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
                out.append({"id": "LF-" + tid, "source": label, "type": "Jurisprudence",
                            "date": date_from_title(title) or _d(x.get("date")) or str(TODAY),
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

def parse_ai_json(txt):
    """Extrait un objet JSON de la réponse du modèle, même s'il est entouré de ```json ... ```."""
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.lower().startswith("json"): txt = txt[4:]
    try:
        d = json.loads(txt.strip())
        if isinstance(d, dict) and d.get("resume"):
            return {"resume": str(d.get("resume", ""))[:500],
                    "contexte": (str(d.get("contexte", "")).strip()[:300] or None),
                    "points": [str(p)[:180] for p in (d.get("points_cles") or []) if str(p).strip()][:5],
                    "qui": (str(d.get("qui_est_concerne", "")).strip()[:200] or None),
                    "portee": (str(d.get("vigilance", "")).strip()[:300] or None)}
    except Exception:
        pass
    return None

def ai_summary(item):
    """Résumé structuré optionnel, basé UNIQUEMENT sur le texte fourni : un résumé court,
    2-4 points clés factuels, et une phrase de portée pratique pour un notaire.
    Utilise Google Gemini (niveau gratuit) si GEMINI_API_KEY est présent,
    sinon l'API Anthropic si ANTHROPIC_API_KEY est présent (payant), sinon rien.
    Toujours étiqueté "IA, à vérifier" côté affichage ; jamais présenté comme le texte officiel."""
    if len(item["title"]) < 15: return None
    has_abstract = bool(item.get("abstract"))
    consigne_min = ("Seul le titre est disponible ici, sans sommaire détaillé : limite-toi à ce que le titre permet "
                     "raisonnablement de déduire. Remplis au minimum \"resume\" (reformulation factuelle du titre), et "
                     "laisse les autres champs en chaîne vide ou liste vide si le titre seul ne suffit pas à les "
                     "renseigner sans risque d'invention.") if not has_abstract else (
                     "Sois aussi précis et complet que le texte source le permet, sans délayer.")
    prompt = ("Tu prépares une fiche de synthèse à destination d'un notaire français, en t'appuyant STRICTEMENT et "
              "UNIQUEMENT sur le texte ci-dessous (titre" + (" + sommaire officiel" if has_abstract else " seul, sans sommaire") +
              "). N'invente et n'ajoute AUCUN fait, numéro d'article, date, chiffre, nom ou conséquence juridique qui n'y "
              f"figure pas explicitement. {consigne_min} Chaque champ rempli doit apporter une information réelle, jamais "
              "une reformulation vide. Laisse un champ en chaîne vide plutôt que d'inventer. Réponds STRICTEMENT en JSON, "
              "sans aucun texte autour, avec exactement ces clés :\n"
              '{"resume": "1-3 phrases reformulant précisément le contenu et sa portée juridique", '
              '"contexte": "1-2 phrases sur ce que ce texte/cette décision modifie ou clarifie par rapport à l\'état du '
              'droit antérieur, UNIQUEMENT si le texte source le précise explicitement, sinon chaîne vide", '
              '"points_cles": ["fait précis et concret 1 (si disponible)", "fait précis 2 (si disponible)"], '
              '"qui_est_concerne": "1 phrase courte sur les personnes, actes ou situations concernés, seulement si explicite, sinon chaîne vide", '
              '"vigilance": "1-2 phrases au conditionnel sur ce qu\'un notaire devrait vérifier, adapter dans ses actes, ou '
              'signaler à ses clients, seulement si le texte le permet clairement, sinon chaîne vide"}\n'
              "Réponds exactement INSUFFISANT seulement si le titre lui-même est trop vague pour en tirer un résumé factuel.\n\n"
              "Titre : " + item["title"] + "\n" + item.get("abstract", ""))

    txt = call_ai(prompt, max_tokens=550)
    if not txt or txt.startswith("INSUFFISANT"): return None
    return parse_ai_json(txt) or {"resume": txt[:400], "points": [], "portee": None}

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
        arts = extract_articles(it["title"] + " " + it.get("abstract", ""))
        if arts: it["articles"] = arts
        store[it["id"]] = it; added += 1
    items_preview = sorted(store.values(), key=lambda i: (i["date"], i["first_seen"]), reverse=True)
    digest = weekly_digest(items_preview)  # en premier : c'est la fonctionnalité la plus utile, elle ne doit jamais manquer de quota
    configured = [k for k in ("groq", "gemini", "anthropic")
                  if os.getenv({"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[k])]
    n, ai_batch = 0, int(os.getenv("AI_BATCH", "25"))
    for it in sorted(store.values(), key=lambda i: i["date"], reverse=True):
        if n >= ai_batch: break
        if configured and set(configured) <= _DEAD_PROVIDERS:
            log("Tous les fournisseurs IA configurés sont à quota ou en erreur : arrêt anticipé des fiches pour ce run.")
            break
        if "ai" not in it:
            it["ai"] = ai_summary(it); n += 1
    items = sorted(store.values(), key=lambda i: (i["date"], i["first_seen"]), reverse=True)
    json.dump(items, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"date": str(TODAY), "by_matiere": digest or {}}, open(os.path.join(DOCS, "digest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    write_html(items, digest); write_rss(items)
    new_today = [i for i in items if i["first_seen"] == str(TODAY)]
    notify(new_today)
    log(f"{added} nouveaux éléments, {len(items)} au total.")

def notify(new_items):
    """Envoie une notification gratuite via ntfy.sh si NTFY_TOPIC est configuré.
    ntfy.sh est un service gratuit et open-source : voir https://ntfy.sh"""
    topic = os.getenv("NTFY_TOPIC")
    if not topic or not new_items: return
    top = sorted(new_items, key=lambda i: i["date"], reverse=True)[:5]
    body = "\n".join(f"• {i['title'][:90]}" for i in top)
    if len(new_items) > 5: body += f"\n… et {len(new_items) - 5} de plus"
    try:
        requests.post(f"https://ntfy.sh/{topic}", timeout=20, data=body.encode("utf-8"),
            headers={"Title": f"Veille notariale : {len(new_items)} nouveauté(s)".encode("utf-8"),
                     "Tags": "scales", "Click": "https://ntfy.sh"})
    except Exception as e:
        log("Notification ntfy", e)

_DEAD_PROVIDERS = set()  # fournisseurs déjà en échec définitif sur ce run : on ne les rappelle plus, pour ne pas perdre de temps

def _post_with_retry(url, provider, **kw):
    """Réessaie en cas de 429 (quota) ou 503 (serveur saturé) : 2 essais max, pause courte.
    Toute autre erreur (400, 401, 404...) indique un problème de configuration, pas un aléa réseau :
    on n'insiste pas, on marque le fournisseur mort pour le reste du run et on log le détail exact."""
    delay = 6
    r = None
    for attempt in range(2):
        r = requests.post(url, **kw)
        if r.status_code not in (429, 503):
            break
        wait = delay * (attempt + 1)
        log(f"IA ({provider}) : {r.status_code}, nouvel essai dans {wait}s ({attempt+1}/2)")
        time.sleep(wait)
    if r.status_code >= 400:
        body = (r.text or "")[:300].replace("\n", " ")
        log(f"IA ({provider}) : échec {r.status_code} — {body}")
        if r.status_code != 503:
            # 400/401/404 (config) ET 429 persistant après nouvelles tentatives (quota épuisé, pas juste un pic
            # de vitesse) : dans les deux cas, inutile de continuer à solliciter ce fournisseur pour ce run.
            _DEAD_PROVIDERS.add(provider)
    return r

def call_ai(prompt, max_tokens=700):
    """Appel générique au modèle disponible (Groq prioritaire — quota gratuit généreux et stable —,
    puis Gemini, puis Anthropic en dernier repli). Retourne le texte brut ou None.
    Un fournisseur qui échoue avec une erreur de configuration (pas un simple embouteillage) est
    écarté pour le reste du run, afin de ne pas perdre de temps à répéter le même échec."""
    time.sleep(4)
    qkey = os.getenv("GROQ_API_KEY")
    if qkey and "groq" not in _DEAD_PROVIDERS:
        try:
            r = _post_with_retry("https://api.groq.com/openai/v1/chat/completions", "Groq", timeout=60,
                headers={"content-type": "application/json", "Authorization": f"Bearer {qkey.strip()}"},
                json={"model": "openai/gpt-oss-120b", "temperature": 0.2, "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]})
            if r.status_code < 400:
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log("IA (Groq) exception", e); _DEAD_PROVIDERS.add("groq")

    gkey = os.getenv("GEMINI_API_KEY")
    if gkey and "gemini" not in _DEAD_PROVIDERS:
        try:
            r = _post_with_retry("https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent",
                "Gemini", timeout=60, headers={"content-type": "application/json", "x-goog-api-key": gkey.strip()},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2}})
            if r.status_code < 400:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            log("IA (Gemini) exception", e); _DEAD_PROVIDERS.add("gemini")

    akey = os.getenv("ANTHROPIC_API_KEY")
    if akey and "anthropic" not in _DEAD_PROVIDERS:
        try:
            r = _post_with_retry("https://api.anthropic.com/v1/messages", "Anthropic", timeout=60,
                headers={"x-api-key": akey.strip(), "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
            if r.status_code < 400:
                return r.json()["content"][0]["text"].strip()
        except Exception as e:
            log("IA (Anthropic) exception", e); _DEAD_PROVIDERS.add("anthropic")
    return None

def weekly_digest(items):
    """Brief hebdomadaire : un paragraphe de synthèse par matière, sur les 7 derniers jours.
    Basé UNIQUEMENT sur les titres/sommaires déjà collectés ; regroupe sans inventer de fait nouveau."""
    if not (os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")): return None
    cutoff = (TODAY - dt.timedelta(days=7)).isoformat()
    recent = [i for i in items if i["date"] >= cutoff or i["first_seen"] >= cutoff]
    if len(recent) < 3: return None
    by_mat = {}
    for i in recent:
        for m in i["matieres"]:
            if m in ("Texte majeur à examiner", "Autres"): continue
            by_mat.setdefault(m, []).append(i)
    digest = {}
    for mat, its in sorted(by_mat.items(), key=lambda kv: -len(kv[1]))[:8]:
        if len(its) < 2: continue
        lst = "\n".join(f"- {i['title']} ({i['type']}, {i['date']})" + (f" : {i['abstract'][:180]}" if i.get("abstract") else "") for i in its[:12])
        prompt = ("Tu rédiges la synthèse hebdomadaire d'une veille juridique pour des notaires, sur la matière "
                   f"« {mat} ». Voici la liste des publications de la semaine dans cette matière, avec leur type et leur "
                   "date. En t'appuyant UNIQUEMENT sur les éléments listés ci-dessous — sans inventer aucun fait qui n'y "
                   "figure pas —, réponds en 3 phrases COURTES et COMPLÈTES maximum (jamais coupée en milieu de phrase), "
                   "chacune sur sa propre ligne, séparées par un retour à la ligne : la 1ère résume factuellement ce qui "
                   "s'est passé, la 2e donne un détail concret marquant, la 3e (optionnelle) est une phrase de vigilance "
                   "pratique pour un notaire. Reste concis : mieux vaut une phrase de moins qu'une phrase coupée. Réponds "
                   "uniquement avec ces phrases, sans titre ni introduction.\n\n" + lst)
        txt = call_ai(prompt, max_tokens=450)
        if txt and not txt.startswith("INSUFFISANT"): digest[mat] = txt[:900]
    return digest or None

def write_rss(items):
    e = html.escape
    rows = "".join(f"<item><title>{e(i['title'])}</title><link>{e(i['url'])}</link><guid>{e(i['id'])}</guid>"
                   f"<description>{e(i.get('abstract',''))}</description><category>{e(', '.join(i['matieres']))}</category></item>" for i in items[:100])
    open(os.path.join(DOCS, "feed.xml"), "w", encoding="utf-8").write(
        f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Veille juridique notariale</title>'
        f'<link>.</link><description>Textes, jurisprudence et doctrine</description>{rows}</channel></rss>')

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Veille juridique notariale</title><link rel="alternate" type="application/rss+xml" href="feed.xml">
<link rel="manifest" href="manifest.json"><meta name="theme-color" content="#14213d">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Veille notariale"><link rel="apple-touch-icon" href="icon.png">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f2f0e9;--fg:#1d2433;--card:#fff;--mut:#666f80;--bd:#e3e0d3;--navy:#14213d;--navy2:#26426b;--gold:#b8893b;--jur:#26426b;--txt:#8c2f39;--doc:#2f7a5b;--ring:#b8893b55;--side:#181f30}
:root[data-theme="dark"]{--bg:#0e1320;--fg:#e9ecf3;--card:#171f33;--mut:#9aa3b5;--bd:#28324a;--gold:#d9a95a;--jur:#7fa6e0;--txt:#e58a94;--doc:#6fcfa3;--ring:#d9a95a55;--side:#0b0f1a}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1320;--fg:#e9ecf3;--card:#171f33;--mut:#9aa3b5;--bd:#28324a;--gold:#d9a95a;--jur:#7fa6e0;--txt:#e58a94;--doc:#6fcfa3;--ring:#d9a95a55;--side:#0b0f1a}}
*{box-sizing:border-box}html{scroll-padding-top:70px}
body{margin:0;background:var(--bg);color:var(--fg);font:14.5px/1.6 Inter,system-ui,sans-serif;overflow-x:hidden}
a{color:inherit} button{font-family:inherit}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:var(--bd);border-radius:99px}

.shell{display:flex;min-height:100vh}

/* ===== Sidebar ===== */
.sidebar{width:252px;flex:none;background:var(--side);color:#dfe4ee;position:sticky;top:0;height:100vh;overflow-y:auto;padding:18px 14px calc(18px + env(safe-area-inset-bottom,0px));z-index:40}
.brandmini{display:flex;align-items:center;gap:9px;margin-bottom:16px;padding:0 4px}
.brandmini .mark{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--gold),#8a5f22);display:flex;align-items:center;justify-content:center;font:700 16px 'Playfair Display',Georgia,serif;color:#1d1400;flex:none}
.brandmini .tt{font:700 15px 'Playfair Display',Georgia,serif;line-height:1.1}.brandmini .tt small{display:block;font:500 10px Inter;color:#8b93a8;letter-spacing:.04em;text-transform:uppercase}
.ministats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:16px}
.mstat{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:9px;padding:7px 4px;text-align:center}
.mstat b{display:block;font:700 15px 'Playfair Display',Georgia,serif;color:#f1d9a6}.mstat span{font-size:8.5px;color:#8b93a8;text-transform:uppercase;letter-spacing:.04em}
.navlbl{font:700 10px Inter;letter-spacing:.1em;text-transform:uppercase;color:#6b7690;margin:16px 6px 6px}
.navitem{display:flex;width:100%;align-items:center;gap:8px;text-align:left;background:none;border:none;color:#c7cede;padding:7px 8px;border-radius:8px;cursor:pointer;font:600 13px Inter}
.navitem:hover{background:rgba(255,255,255,.06)}
.navitem.on{background:linear-gradient(135deg,var(--gold),#8a5f22);color:#1d1400}
.navitem .n{margin-left:auto;font:700 11px Inter;opacity:.65}.navitem.on .n{opacity:.8}
.navitem .dot{width:7px;height:7px;border-radius:99px;flex:none}
.themebtn{margin-top:18px;display:flex;gap:7px;align-items:center;justify-content:center;width:100%;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:#c7cede;padding:8px;border-radius:9px;cursor:pointer;font:600 12px Inter}
.themebtn:hover{background:rgba(255,255,255,.12)}

/* ===== Backdrop mobile ===== */
.backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:35}
.backdrop.show{display:block}

/* ===== Colonne principale ===== */
.content{flex:1;min-width:0}
.topbar{position:sticky;top:0;z-index:30;background:var(--bg);border-bottom:1px solid var(--bd);padding:11px 20px;display:flex;gap:10px;align-items:center;box-shadow:0 2px 8px rgba(20,33,61,.04)}
.burger{display:none;background:none;border:1px solid var(--bd);border-radius:8px;padding:8px 10px;cursor:pointer;color:var(--fg);font-size:15px}
.search{position:relative;flex:1;max-width:480px}
.search input{width:100%;padding:10px 13px 10px 38px;border:1px solid var(--bd);border-radius:10px;font:14.5px Inter,system-ui;background:var(--card);color:var(--fg);outline:none}
.search input:focus{border-color:var(--gold);box-shadow:0 0 0 3px var(--ring)}
.search svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);width:15px;height:15px;stroke:var(--mut);fill:none;stroke-width:2}
.breadcrumb{font:600 12px Inter;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.breadcrumb b{color:var(--fg)}
.rssmini{margin-left:auto;flex:none;font:600 12px Inter;color:var(--gold);text-decoration:none;white-space:nowrap}

main{max-width:920px;margin:0;padding:18px 20px 34px}
#brief-wrap{margin-bottom:4px}
.brief{background:linear-gradient(160deg,var(--navy),var(--navy2));border-radius:16px;padding:18px 18px 6px;margin-bottom:18px;box-shadow:0 8px 22px rgba(20,33,61,.16);color:#fff}
.brief h2{font:700 18px 'Playfair Display',Georgia,serif;margin:0 0 2px}
.brief .sub2{font-size:11.5px;color:#cbd5e6;margin-bottom:10px}
.brief details{border-top:1px solid rgba(255,255,255,.15);padding:10px 0}
.brief summary{cursor:pointer;font:600 13.5px Inter;list-style:none;display:flex;justify-content:space-between;align-items:center}
.brief summary::-webkit-details-marker{display:none}.brief summary:after{content:"+";font-size:16px;color:#e3c58c}
.brief details[open] summary:after{content:"–"}
.brief p{font-size:12.5px;line-height:1.55;color:#e7ecf5;margin:6px 0 2px 2px}

.daysep{display:flex;align-items:center;gap:9px;margin:18px 2px 2px;color:var(--mut)}
.daysep:first-of-type{margin-top:0}
.daysep .lbl{font:700 11px Inter;letter-spacing:.09em;text-transform:uppercase;color:var(--gold)}
.daysep:before,.daysep:after{content:"";height:1px;flex:1;background:var(--bd)}

#list{display:flex;flex-direction:column;gap:11px}

.card{background:var(--card);border:1px solid var(--bd);border-radius:13px;padding:14px;box-shadow:0 2px 8px rgba(20,33,61,.04);position:relative;overflow:hidden;display:flex;gap:11px;align-items:flex-start}
.card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c)}
.card.t-Jurisprudence{--c:var(--jur)}.card.t-Texte{--c:var(--txt)}.card.t-Doctrine{--c:var(--doc)}
.ico{flex:none;width:36px;height:36px;border-radius:9px;display:flex;align-items:center;justify-content:center;font:700 16px 'Playfair Display',Georgia,serif;color:#fff;background:var(--c)}
.body{min-width:0;overflow-wrap:anywhere;flex:1}
.top{display:flex;flex-wrap:wrap;gap:5px 7px;align-items:center;margin-bottom:6px}
.badge{font:700 9.5px Inter;text-transform:uppercase;letter-spacing:.06em;padding:2px 8px;border-radius:99px;color:var(--c);border:1px solid var(--c)}
.badge.bull{background:linear-gradient(135deg,var(--gold),#8a5f22);border-color:transparent;color:#1d1400}
.badge.new{background:var(--gold);border-color:var(--gold);color:#14213d}
.date{font-size:11px;color:var(--mut);font-weight:600;margin-left:auto}
.card a.t{display:block;font:700 15.5px/1.32 'Playfair Display',Georgia,serif;color:var(--fg);text-decoration:none}.card a.t:hover{color:var(--gold)}
.src{font-size:11px;color:var(--mut);margin-top:3px}
.abs{font-size:13px;margin-top:7px;opacity:.92}
.toggle{background:none;border:none;color:var(--gold);font:600 11.5px Inter;cursor:pointer;padding:4px 0;margin-top:2px}
.absfull{display:none}.card.exp .absfull{display:block}.card.exp .abssum{display:none}

.fiche{margin-top:9px;border-radius:10px;background:linear-gradient(180deg,rgba(47,122,91,.07),rgba(47,122,91,.03));border:1px solid rgba(47,122,91,.28);padding:10px 11px}
.fiche .tag2{display:inline-block;font:700 9.5px Inter;letter-spacing:.06em;text-transform:uppercase;color:var(--doc);background:rgba(47,122,91,.14);padding:2px 8px;border-radius:6px;margin-bottom:6px}
.fiche .resume{font-size:13px;margin:0 0 7px}
.fsec{margin-top:6px;padding-top:6px;border-top:1px dashed var(--bd)}
.fsec .h{font:700 10.5px Inter;color:var(--fg);display:flex;align-items:center;gap:5px;margin-bottom:2px}
.fsec p{margin:0;font-size:12.5px}.fsec ul{margin:2px 0 0;padding-left:16px}.fsec li{font-size:12.5px;margin:1px 0}
.vig{margin-top:7px;padding:7px 9px;border-radius:8px;background:rgba(184,137,59,.12);border-left:3px solid var(--gold);font-size:12px}
.vig b{color:var(--gold);display:block;font-size:10px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px}

.artchips{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.artchip{font:700 10px Inter;padding:2px 8px;border-radius:6px;background:var(--bg);border:1px solid var(--bd);color:var(--mut)}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.tag{font-size:10.5px;font-weight:600;padding:2px 9px;border-radius:99px;background:rgba(184,137,59,.13);border:1px solid rgba(184,137,59,.35)}
.more{display:inline-flex;align-items:center;gap:4px;margin-top:9px;font:700 11.5px Inter;color:var(--gold);text-decoration:none}
footer{padding:16px 20px calc(24px + env(safe-area-inset-bottom,0px));font-size:11px;color:var(--mut);border-top:1px solid var(--bd)}

@media(max-width:860px){
  .sidebar{position:fixed;left:0;top:0;transform:translateX(-100%);transition:transform .2s ease;width:78vw;max-width:290px;box-shadow:6px 0 24px rgba(0,0,0,.3)}
  .sidebar.open{transform:translateX(0)}
  .burger{display:inline-block}
  main{padding:14px 14px 28px}
}
</style></head><body>
<div class="shell">
<div class="backdrop" id="backdrop"></div>
<aside class="sidebar" id="sidebar">
  <div class="brandmini"><div class="mark">§</div><div class="tt">Veille notariale<small>Actualité juridique</small></div></div>
  <div class="ministats"><div class="mstat"><b id="n1">0</b><span>Total</span></div><div class="mstat"><b id="n2">0</b><span>7j</span></div><div class="mstat"><b id="n3">0</b><span>Matières</span></div></div>
  <div class="navlbl">Type de source</div><div id="navtype"></div>
  <div class="navlbl">Matière</div><div id="navmat"></div>
  <button class="themebtn" id="themebtn">🌗 <span id="themelabel">Thème auto</span></button>
</aside>
<div class="content">
  <div class="topbar">
    <button class="burger" id="burger">☰</button>
    <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg><input id="q" placeholder="Rechercher : donation, indivision, Cass.…"></div>
    <div class="breadcrumb" id="breadcrumb"></div>
    <a class="rssmini" href="feed.xml">Flux RSS</a>
  </div>
  <main><div id="brief-wrap"></div><div id="list"></div></main>
  <footer>Mise à jour <span id="upd">__UPD__</span>. Contenus repris des sources officielles (titre, date, sommaire publiés par la source). Seul le texte du lien fait foi : vérifiez toujours sur Légifrance, Judilibre ou la source d'origine avant tout usage professionnel. Les fiches automatiques sont générées par IA à partir du seul texte de la source et peuvent contenir des erreurs.</footer>
</div>
</div>
<script>const D=__DATA__;const DIGEST=__DIGEST__;let mat="",typ="";const $=id=>document.getElementById(id);
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const M=[...new Set(D.flatMap(i=>i.matieres))].sort(),T=[...new Set(D.map(i=>i.type))];
const IC={Jurisprudence:"⚖",Texte:"§",Doctrine:"✎"};
const DOT={Jurisprudence:"var(--jur)",Texte:"var(--txt)",Doctrine:"var(--doc)"};
const cut7=Date.now()-7*864e5,isNew=i=>new Date(i.first_seen).getTime()>=cut7;
$("n1").textContent=D.length;$("n2").textContent=D.filter(isNew).length;$("n3").textContent=M.length;

/* Thème : auto / clair / sombre, mémorisé */
const THEMES=["auto","light","dark"],LBL={auto:"Thème auto",light:"Thème clair",dark:"Thème sombre"};
let theme="auto"; try{theme=localStorage.getItem("veille-theme")||"auto"}catch(e){}
function applyTheme(){document.documentElement.setAttribute("data-theme",theme==="auto"?"":theme);$("themelabel").textContent=LBL[theme];}
applyTheme();
$("themebtn").onclick=()=>{theme=THEMES[(THEMES.indexOf(theme)+1)%3];try{localStorage.setItem("veille-theme",theme)}catch(e){}applyTheme();};

/* Sidebar mobile */
function openSidebar(){$("sidebar").classList.add("open");$("backdrop").classList.add("show")}
function closeSidebar(){$("sidebar").classList.remove("open");$("backdrop").classList.remove("show")}
$("burger").onclick=openSidebar; $("backdrop").onclick=closeSidebar;

function renderBrief(){
  const b=$("brief-wrap"); if(!DIGEST||!DIGEST.by_matiere||!Object.keys(DIGEST.by_matiere).length){return}
  const entries=Object.entries(DIGEST.by_matiere);
  const fmt=t=>t.split(/\\n+/).map(s=>s.trim()).filter(Boolean).map(s=>`<p>• ${esc(s)}</p>`).join("");
  b.innerHTML=`<div class="brief"><h2>📋 Brief de la semaine</h2><div class="sub2">Synthèse automatique (IA) des 7 derniers jours, par matière — généré le ${fd(DIGEST.date)} · à vérifier au besoin sur les fiches ci-dessous</div>
  ${entries.map(([m,t],idx)=>`<details ${idx===0?"open":""}><summary>${esc(m)}</summary>${fmt(t)}</details>`).join("")}</div>`;
}

function nav(){
  const cntType=k=>D.filter(i=>i.type===k).length, cntMat=k=>D.filter(i=>i.matieres.includes(k)).length;
  $("navtype").innerHTML = `<button class="navitem ${!mat&&!typ?"on":""}" data-k="__all" data-v="">Toutes les sources <span class="n">${D.length}</span></button>` +
    T.map(t=>`<button class="navitem ${typ===t?"on":""}" data-k="type" data-v="${esc(t)}"><span class="dot" style="background:${DOT[t.split(' ')[0]]||'var(--mut)'}"></span>${esc(t)} <span class="n">${cntType(t)}</span></button>`).join("");
  $("navmat").innerHTML = M.map(m=>`<button class="navitem ${mat===m?"on":""}" data-k="mat" data-v="${esc(m)}">${esc(m)} <span class="n">${cntMat(m)}</span></button>`).join("");
  document.querySelectorAll(".navitem").forEach(b=>b.onclick=()=>{
    const k=b.dataset.k,v=b.dataset.v;
    if(k==="__all"){mat="";typ=""}else if(k==="type"){typ=(typ===v?"":v);mat=""}else{mat=(mat===v?"":v);typ=""}
    nav(); draw(); if(window.innerWidth<=860) closeSidebar();
  });
  $("breadcrumb").innerHTML = (mat||typ) ? `Filtré : <b>${esc(mat||typ)}</b>` : `<b>Toutes les publications</b>`;
}

const fd=d=>{try{return new Date(d).toLocaleDateString("fr-FR",{day:"numeric",month:"short",year:"numeric"})}catch(e){return d}};
function important(i){return i.bulletin || i.matieres.includes("Texte majeur à examiner")}
function absBlock(i){
  const t=esc(i.abstract||""); if(!t) return "";
  if(important(i)||t.length<=220) return `<div class="abs">${t}</div>`;
  return `<div class="abssum abs">${t.slice(0,220)}…</div><div class="absfull abs">${t}</div><button class="toggle" data-t="1">Voir le sommaire complet ▾</button>`;
}
function ficheBlock(i){
  if(!i.ai || !i.ai.resume) return "";
  const a=i.ai; let h=`<div class="fiche"><span class="tag2">Fiche IA — à vérifier</span><p class="resume">${esc(a.resume)}</p>`;
  if(a.contexte) h+=`<div class="fsec"><div class="h">🔍 Contexte</div><p>${esc(a.contexte)}</p></div>`;
  if(a.points&&a.points.length) h+=`<div class="fsec"><div class="h">✓ Ce qu'il faut retenir</div><ul>${a.points.map(p=>`<li>${esc(p)}</li>`).join("")}</ul></div>`;
  if(a.qui) h+=`<div class="fsec"><div class="h">👤 Qui est concerné</div><p>${esc(a.qui)}</p></div>`;
  if(a.portee) h+=`<div class="vig"><b>⚠ Vigilance pratique</b>${esc(a.portee)}</div>`;
  return h+"</div>";
}
function dayGroup(i){
  const d=new Date(i.date), today=new Date(); today.setHours(0,0,0,0);
  const diff=Math.round((today-new Date(d.getFullYear(),d.getMonth(),d.getDate()))/864e5);
  if(diff<=0) return "Aujourd'hui"; if(diff===1) return "Hier"; if(diff<=7) return "Cette semaine";
  if(diff<=31) return "Ce mois-ci"; return "Plus ancien";
}
function draw(){
  const q=$("q").value.toLowerCase();
  const r=D.filter(i=>(!mat||i.matieres.includes(mat))&&(!typ||i.type===typ)&&(!q||(i.title+i.abstract+(i.ai?i.ai.resume||"":"")).toLowerCase().includes(q))).slice(0,400);
  let lastGroup=null, html="";
  for(const i of r){
    const g=dayGroup(i);
    if(g!==lastGroup){ html+=`<div class="daysep"><span class="lbl">${g}</span></div>`; lastGroup=g; }
    const k=i.type.split(" ")[0];
    html+=`<div class="card t-${esc(k)}${important(i)?" exp":""}"><div class="ico">${IC[k]||"§"}</div><div class="body">
    <div class="top"><span class="badge">${esc(i.type)}</span>${i.bulletin?'<span class="badge bull">Bulletin</span>':""}${isNew(i)?'<span class="badge new">Nouveau</span>':""}<span class="date">${fd(i.date)}</span></div>
    <a class="t" href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a><div class="src">${esc(i.source)}</div>
    ${absBlock(i)}${ficheBlock(i)}
    ${i.articles&&i.articles.length?`<div class="artchips">${i.articles.map(a=>`<span class="artchip">${esc(a)}</span>`).join("")}</div>`:""}
    <div class="tags">${i.matieres.map(m=>`<span class="tag">${esc(m)}</span>`).join("")}</div>
    <a class="more" href="${esc(i.url)}" target="_blank" rel="noopener">Consulter la source officielle →</a></div></div>`;
  }
  $("list").innerHTML=html||'<p style="color:var(--mut)">Aucun résultat.</p>';
  document.querySelectorAll(".toggle").forEach(b=>b.onclick=()=>{b.closest(".card").classList.toggle("exp");b.remove()});
}
$("q").oninput=draw; renderBrief(); nav(); draw();
</script></body></html>"""

def write_html(items, digest=None):
    dig = {"date": str(TODAY), "by_matiere": digest or {}}
    page = PAGE.replace("__UPD__", dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y %H:%M UTC")).replace(
        "__DATA__", json.dumps(items, ensure_ascii=False).replace("</", "<\\/")).replace(
        "__DIGEST__", json.dumps(dig, ensure_ascii=False).replace("</", "<\\/"))
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(page)

if __name__ == "__main__":
    main()
