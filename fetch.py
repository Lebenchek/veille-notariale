"""Veille juridique notariale.

Sources (publiques uniquement) : Notaires de France (flux RSS), SEPAJ et LegalNews Notaires (pages d'actualités
publiques). Aucun contenu réservé aux abonnés n'est lu, aucun identifiant n'est utilisé.

Principe : le site affiche le titre, la date et un court extrait (celui que la source publie elle-même) avec un lien
vers l'original, plus une fiche de synthèse reformulée par IA, toujours étiquetée "à vérifier". Le texte intégral
d'une page n'est lu qu'en mémoire pour alimenter l'IA ; il n'est jamais enregistré ni republié.
"""
import os, json, html, re, time, datetime as dt
import requests, feedparser

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(DOCS, "items.json")
TODAY = dt.date.today()
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "45"))
AI_VERSION = 3                      # change quand le format des fiches change : les anciennes sont régénérées
ALLOWED_SOURCES = ("Notaires de France", "SEPAJ", "LegalNews")
TEASER_MAX = 320                    # longueur maximale de l'extrait affiché (celui que publie la source)
UA_BOT = {"User-Agent": "veille-notariale/2.0"}
UA_WEB = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36", "Accept-Language": "fr-FR,fr;q=0.9"}


def log(*a):
    print("[veille]", *a, flush=True)


# --------------------------------------------------------------------------------------------------
# Classement par matière (transparent, par mots-clés ; toutes les sources sont déjà ciblées notariat)
# --------------------------------------------------------------------------------------------------
MATIERES = {
    "Successions & libéralités": ["succession", "donation", "testament", "legs", "héritier", "hériter", "libéralité",
        "réserve héréditaire", "assurance-vie", "assurance vie", "clause bénéficiaire", "indivision", "partage",
        "usufruit", "nue-propriété", "défunt", "de cujus", "fiducie", "déshérence", "sans maître", "représentation"],
    "Famille & régimes matrimoniaux": ["régime matrimonial", "contrat de mariage", "divorce", "pacs", "concubinage",
        "mariage", "époux", "conjoint", "filiation", "adoption", "autorité parentale", "majeur protégé", "tutelle",
        "curatelle", "habilitation familiale", "mandat de protection", "prestation compensatoire", "communauté",
        "récompense", "obligation alimentaire", "aliments", "séparation de biens"],
    "Immobilier & copropriété": ["vente immobilière", "immobilier", "immeuble", "copropriété", "syndic", "bail",
        "locataire", "hypothèque", "publicité foncière", "cadastre", "urbanisme", "préemption", "servitude",
        "lotissement", "vefa", "promesse de vente", "compromis", "diagnostic", "terrain", "foncier", "usucapion",
        "mitoyen", "construction", "lot de copropriété", "congé", "saisie", "safer", "rural"],
    "Sociétés & patrimoine professionnel": ["société civile", "sci ", "sarl", "sas ", "société", "cession de parts",
        "cession de titres", "fonds de commerce", "holding", "pacte d'associés", "associé", "gérant", "dirigeant",
        "entreprise", "transmission d'entreprise", "bail commercial", "bail rural", "gfa", "gaec"],
    "Fiscalité": ["droits de mutation", "dmtg", "dmto", "droits d'enregistrement", "plus-value", "impôt", "fiscal",
        "ifi", "taxe", "bofip", "exonération", "abattement", "cgi", "tva", "csg", "prélèvements sociaux",
        "dutreil", "abus de droit", "rappel fiscal"],
    "Droit international privé & européen": ["international", "règlement (ue)", "européen", "certificat successoral",
        "loi applicable", "conflit de lois", "exequatur", "étranger", "non-résident", "nationalité", "apostille"],
    "Profession, déontologie & actes": ["notaire", "notarial", "office notarial", "acte authentique", "déontologie",
        "csn", "chambre des notaires", "tracfin", "blanchiment", "devoir de conseil", "responsabilité du notaire",
        "signature électronique", "procuration", "tarif", "émoluments", "formalité", "fichier central"],
    "Droit public & collectivités": ["commune", "collectivité", "domaine public", "cg3p", "expropriation",
        "préemption urbaine", "service public"],
}
# Rubriques de LegalNews Notaires (début du chemin de l'URL) -> matière
LNN_HINTS = {"personnes-famille": "Famille & régimes matrimoniaux", "patrimoine-successions": "Successions & libéralités",
             "patrimoine": "Successions & libéralités", "fiscalite": "Fiscalité", "affaires": "Sociétés & patrimoine professionnel",
             "immobilier": "Immobilier & copropriété", "droit-public": "Droit public & collectivités",
             "profession": "Profession, déontologie & actes"}


def classify(text, url=""):
    t = (text or "").lower()
    found = [m for m, kws in MATIERES.items() if any(k in t for k in kws)]
    for k, m in LNN_HINTS.items():
        if f"/{k}" in (url or "") and m not in found:
            found.append(m)
    return found or ["Autres"]


ARTICLE_RE = re.compile(r"articles?\s+((?:\d+(?:-\d+)*(?:\s*(?:,|et)\s*)?)+)\s+(?:du|de la|des)\s+(code[^,.;\n]{0,45})", re.I)


def extract_articles(text):
    found, seen = [], set()
    for m in ARTICLE_RE.finditer(text or ""):
        label = re.sub(r"\s+", " ", f"art. {m.group(1).strip()} {m.group(2).strip()}")[:60]
        if label.lower() not in seen:
            seen.add(label.lower()); found.append(label)
    return found[:6]


def clean_text(s, limit=None):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if limit else s


def cut_teaser(s):
    s = clean_text(s)
    if len(s) <= TEASER_MAX:
        return s
    cut = s[:TEASER_MAX]
    return cut[:cut.rfind(" ")].rstrip(" ,;:") + "…"


def trim_to_sentence(s):
    """Évite d'afficher une phrase coupée net : on s'arrête à la dernière phrase complète."""
    s = (s or "").strip()
    if not s or s[-1] in ".!?»)":
        return s
    k = max(s.rfind(". "), s.rfind("! "), s.rfind("? "))
    return s[:k + 1] if k > 40 else s


# --------------------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------------------
def fetch_full_text(url, max_chars=7000):
    """Lit le texte public d'une page, en mémoire uniquement (jamais enregistré ni republié), pour alimenter l'IA."""
    try:
        r = requests.get(url, headers=UA_WEB, timeout=25)
        r.raise_for_status()
        t = r.text
        m = re.search(r"(?is)<(article|main)[^>]*>(.*?)</\1>", t)
        if m:
            t = m.group(2)
        t = re.sub(r"(?is)<(script|style|nav|header|footer|noscript|aside|form).*?>.*?</\1>", " ", t)
        t = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", t)
        t = clean_text_keep_lines(t)
        return t[:max_chars] if len(t) > 150 else None
    except Exception as e:
        log("Lecture page", url[:80], e)
        return None


def clean_text_keep_lines(t):
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*", "\n", t)
    return t.strip()


def src_rss():
    """Notaires de France : flux RSS officiels listés dans feeds.txt (Nom|URL)."""
    out, p = [], os.path.join(ROOT, "feeds.txt")
    if not os.path.exists(p):
        return out
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name, url = parts[0].strip(), parts[1].strip()
        try:
            f = feedparser.parse(url, agent=UA_WEB["User-Agent"])
        except Exception as e:
            log("RSS", name, e); continue
        if not f.entries:
            log("Flux vide ou inaccessible :", name); continue
        for e in f.entries[:60]:
            t = e.get("published_parsed") or e.get("updated_parsed")
            date = dt.date(*t[:3]).isoformat() if t else str(TODAY)
            link = e.get("link", "")
            out.append({"id": "NDF-" + (e.get("id") or link), "source": "Notaires de France", "date": date,
                        "title": clean_text(e.get("title", "")), "abstract": cut_teaser(e.get("summary", "")), "url": link})
    return out


def src_sepaj():
    """SEPAJ : page publique d'actualités (titre + extrait). L'analyse complète est réservée aux abonnés : non lue."""
    url = "https://www.sepaj.fr/actualites"
    try:
        r = requests.get(url, headers=UA_WEB, timeout=30); r.raise_for_status()
    except Exception as e:
        log("SEPAJ", e); return []
    out = []
    blocks = re.split(r'(?=<a[^>]+href="[^"]*actualites/(?:actes|formalites)[^"]*"[^>]*>)', r.text)
    for b in blocks:
        m = re.search(r'<a[^>]+href="([^"]*actualites/(?:actes|formalites)[^"]*)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        href, title = m.group(1), clean_text(m.group(2))
        md = re.search(r"mise\s*à\s*jour\s*le\s*(\d{2})/(\d{2})/(\d{4})", b, re.I)
        if len(title) < 10 or not md:
            continue
        full = href if href.startswith("http") else "https://www.sepaj.fr/" + href.lstrip("/")
        out.append({"id": "SEPAJ-" + href, "source": "SEPAJ", "date": f"{md.group(3)}-{md.group(2)}-{md.group(1)}",
                    "title": title, "abstract": cut_teaser(b[md.end():]), "url": full})
    return out[:150]


def src_legalnews():
    """LegalNews Notaires : page d'accueil publique (titre + extrait). Le contenu abonné n'est pas lu."""
    url = "https://www.legalnewsnotaires.fr/home-lnn.html"
    try:
        r = requests.get(url, headers=UA_WEB, timeout=30); r.raise_for_status()
    except Exception as e:
        log("LegalNews Notaires", e); return []
    raw, out = r.text, []
    matches = list(re.finditer(r"(\d{2})\.(\d{2})\.(\d{2})\s*-\s*\d{2}:\d{2}", raw))
    for idx, md in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(raw), md.end() + 2500)
        chunk = raw[md.end():end]
        m = re.search(r'<a[^>]+href="([^"]*-\d+(?:-\d+)?\.html)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        href, title = m.group(1), clean_text(m.group(2))
        if len(title) < 10:
            continue
        full = href if href.startswith("http") else "https://www.legalnewsnotaires.fr" + href
        out.append({"id": "LNN-" + href, "source": "LegalNews", "date": f"20{md.group(3)}-{md.group(2)}-{md.group(1)}",
                    "title": title, "abstract": cut_teaser(chunk[m.end():]), "url": full})
    seen, dedup = set(), []
    for it in out:
        if it["id"] not in seen:
            seen.add(it["id"]); dedup.append(it)
    return dedup[:150]


# --------------------------------------------------------------------------------------------------
# IA : fournisseurs gratuits en cascade, arrêt immédiat quand un quota est épuisé
# --------------------------------------------------------------------------------------------------
_DEAD = set()
_QUOTA_HINTS = ("per day", "tokens per day", "tpd", "requests per day", "rpd", "billing", "exceeded your current quota")


def _providers():
    p = []
    if os.getenv("GROQ_API_KEY"):
        p += [("groq-120b", "groq", "openai/gpt-oss-120b"), ("groq-20b", "groq", "openai/gpt-oss-20b")]
    if os.getenv("GEMINI_API_KEY"):
        p.append(("gemini", "gemini", "gemini-flash-latest"))
    if os.getenv("ANTHROPIC_API_KEY"):
        p.append(("anthropic", "anthropic", "claude-haiku-4-5-20251001"))
    return p


def _retry_after(body):
    m = re.search(r"try again in\s*(?:(\d+)m)?\s*(\d+(?:\.\d+)?)s", body or "", re.I)
    return (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else None


def _request(kind, model, prompt, max_tokens):
    if kind == "groq":
        body = {"model": model, "temperature": 0.2, "max_completion_tokens": max_tokens, "reasoning_effort": "low",
                "messages": [{"role": "user", "content": prompt}]}
        return requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=120, json=body,
                             headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '').strip()}"})
    if kind == "gemini":
        return requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", timeout=120,
                             headers={"x-goog-api-key": os.getenv("GEMINI_API_KEY", "").strip()},
                             json={"contents": [{"parts": [{"text": prompt}]}],
                                   "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2}})
    return requests.post("https://api.anthropic.com/v1/messages", timeout=120,
                         headers={"x-api-key": os.getenv("ANTHROPIC_API_KEY", "").strip(), "anthropic-version": "2023-06-01"},
                         json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})


def _extract(kind, r):
    j = r.json()
    if kind == "groq":
        return (j["choices"][0]["message"].get("content") or "").strip()
    if kind == "gemini":
        return j["candidates"][0]["content"]["parts"][0]["text"].strip()
    return j["content"][0]["text"].strip()


def call_ai(prompt, max_tokens=2800):
    """Essaie les fournisseurs dans l'ordre. Quota journalier épuisé ou erreur de configuration -> fournisseur écarté
    pour tout le run, sans nouvel essai. Simple limite par minute -> une seule attente courte, puis on continue."""
    time.sleep(1.5)
    for name, kind, model in _providers():
        if name in _DEAD:
            continue
        try:
            r = _request(kind, model, prompt, max_tokens)
            if r.status_code == 400 and kind == "groq" and "reasoning" in (r.text or "").lower():
                r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=120,
                                  headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '').strip()}"},
                                  json={"model": model, "temperature": 0.2, "max_completion_tokens": max_tokens,
                                        "messages": [{"role": "user", "content": prompt}]})
            if r.status_code in (429, 503) and not any(h in (r.text or "").lower() for h in _QUOTA_HINTS):
                wait = _retry_after(r.text)
                wait = min(wait + 1, 75) if wait is not None else 12
                log(f"IA ({name}) : limite de débit, attente {wait:.0f}s puis nouvel essai")
                time.sleep(wait)
                r = _request(kind, model, prompt, max_tokens)
            if r.status_code >= 400:
                log(f"IA ({name}) : échec {r.status_code} — {(r.text or '')[:220].replace(chr(10), ' ')}")
                if r.status_code != 503:
                    _DEAD.add(name)          # quota épuisé ou erreur de configuration : on ne réessaie plus ce run-ci
                continue
            return _extract(kind, r)
        except Exception as e:
            log(f"IA ({name}) : exception", e)
            _DEAD.add(name)
    return None


def all_ai_dead():
    p = [n for n, _, _ in _providers()]
    return bool(p) and all(n in _DEAD for n in p)


def _json_from(txt):
    if not txt:
        return None
    s = txt.strip()
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        d = json.loads(s[a:b + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _lst(v, n, each):
    return [str(x).strip()[:each] for x in (v or []) if str(x).strip()][:n] if isinstance(v, list) else []


def parse_fiche(txt):
    d = _json_from(txt)
    if not d or not str(d.get("resume", "")).strip():
        return None                                   # JSON invalide ou tronqué : on n'affiche jamais de JSON brut
    sch = _lst(d.get("schema_etapes"), 8, 220)
    return {"resume": str(d["resume"]).strip()[:1600],
            "contexte": str(d.get("contexte", "")).strip()[:1200] or None,
            "points": _lst(d.get("points_cles"), 12, 420),
            "regles": _lst(d.get("regles"), 10, 420),
            "qui": str(d.get("qui_est_concerne", "")).strip()[:700] or None,
            "a_verifier": _lst(d.get("a_verifier"), 10, 360),
            "vigilance": str(d.get("vigilance", "")).strip()[:900] or None,
            "references": _lst(d.get("references"), 12, 160),
            "schema": {"titre": str(d.get("schema_titre", "")).strip()[:120], "etapes": sch} if len(sch) >= 3 else None}


def fiche_prompt(title, text, partial):
    note = ("ATTENTION : seul un extrait public est disponible (l'article complet est réservé aux abonnés). Restitue "
            "précisément ce que l'extrait permet, sans extrapoler ; laisse vides les champs que l'extrait ne permet pas "
            "de renseigner." if partial else
            "Le texte ci-dessous est l'article complet : n'omets AUCUN point juridique substantiel (règles, conditions, "
            "exceptions, délais, seuils, montants, textes, jurisprudence citée, solutions pratiques).")
    return ("Tu rédiges, pour un notaire français très exigeant, une fiche de synthèse RIGOUREUSE ET TRÈS DÉTAILLÉE d'une "
            "actualité juridique, en t'appuyant STRICTEMENT et UNIQUEMENT sur le texte fourni. Règles absolues : (1) n'invente "
            "et n'ajoute aucun fait, article, date, chiffre, nom, arrêt ou conséquence absent du texte ; (2) REFORMULE avec "
            "tes propres mots, ne recopie jamais de phrases entières (citation exceptionnelle de 15 mots maximum) ; (3) pas de "
            "formules vagues (« pourrait », « dans certains cas ») quand le texte est précis : donne la règle, la condition, "
            "le chiffre, la référence ; (4) chaque champ rempli doit être substantiel ; champ vide plutôt qu'invention. "
            f"{note}\n\nRéponds UNIQUEMENT par un objet JSON valide et COMPLET (jamais coupé), avec exactement ces clés :\n"
            '{"resume": "5 à 8 phrases : sujet, solution ou règle retenue, raisonnement, portée pratique",\n'
            ' "contexte": "état du droit ou du litige avant, ce qui change ou est tranché (si le texte le dit), sinon vide",\n'
            ' "points_cles": ["8 à 12 points précis et autonomes quand le texte le permet : règles, conditions, exceptions, délais, seuils, chiffres, textes et arrêts visés"],\n'
            ' "regles": ["conditions d\'application / régime juridique, une règle par élément, avec sa base légale quand le texte la donne"],\n'
            ' "qui_est_concerne": "personnes, actes et situations visés, avec leurs limites",\n'
            ' "a_verifier": ["actions concrètes pour le notaire UNIQUEMENT si le texte les donne ou les implique directement : vérifications, clauses, mentions, informations à donner aux parties"],\n'
            ' "vigilance": "pièges, divergences de jurisprudence, points non tranchés signalés par le texte, sinon vide",\n'
            ' "references": ["textes, articles et décisions cités dans le texte, au format court (ex. Cass. 3e civ., 24 oct. 2024, n° 23-18.067)"],\n'
            ' "schema_titre": "titre court d\'un schéma, seulement si le texte décrit une procédure, une chronologie ou un enchaînement de conditions, sinon vide",\n'
            ' "schema_etapes": ["3 à 8 étapes ou conditions successives, formulées brièvement, dans l\'ordre ; liste vide s\'il n\'y a pas de séquence réelle"]}\n'
            "Réponds exactement INSUFFISANT si le texte ne permet de rien dire d'utile.\n\n"
            f"TITRE : {title}\n\nTEXTE :\n{text}")


def make_fiche(it):
    """Retourne (fiche ou None, statut) ; statut : 'ok', 'vide' (rien d'exploitable) ou 'erreur' (à retenter)."""
    full = fetch_full_text(it["url"]) if it.get("url") else None
    text = full or it.get("abstract", "")
    if len(text) < 60:
        return None, "vide"
    partial = len(text) < 1800
    out = call_ai(fiche_prompt(it["title"], text, partial))
    if out is None:
        return None, "erreur"
    if out.strip().upper().startswith("INSUFFISANT"):
        return None, "vide"
    f = parse_fiche(out)
    if not f:
        log("Fiche IA : JSON invalide ou tronqué, fiche écartée.")
        return None, "erreur"
    f["partielle"] = partial
    return f, "ok"


def weekly_digest(items):
    """Brief : par matière, quelques phrases factuelles sur les 7 derniers jours (titres et extraits publics)."""
    if not _providers():
        return None
    cutoff = (TODAY - dt.timedelta(days=7)).isoformat()
    by = {}
    for i in items:
        if i["date"] >= cutoff:
            for m in i["matieres"]:
                if m != "Autres":
                    by.setdefault(m, []).append(i)
    digest, deadline = {}, time.time() + 150
    for mat, its in sorted(by.items(), key=lambda kv: -len(kv[1]))[:8]:
        if len(its) < 2:
            continue
        if time.time() > deadline or all_ai_dead():
            log("Brief hebdomadaire : arrêt (temps ou quota)."); break
        lst = "\n".join(f"- {i['title']} ({i['source']}, {i['date']}) : {(i['ai']['resume'] if i.get('ai') else i.get('abstract', ''))[:420]}"
                        for i in its[:12])
        txt = call_ai(
            f"Tu rédiges la synthèse hebdomadaire d'une veille juridique pour des notaires, matière « {mat} ». Voici les "
            "publications de la semaine. En t'appuyant UNIQUEMENT sur elles (aucun fait inventé), écris 3 à 6 lignes, une "
            "idée par ligne, chaque ligne étant une phrase COMPLÈTE et CONCRÈTE : la règle ou solution précise, qui est "
            "concerné, ce qu'il faut vérifier. Interdit : « cette semaine a abordé », « plusieurs sujets ». Pas de titre, "
            "pas d'introduction, pas de puces.\n\n" + lst, max_tokens=1500)
        if txt and not txt.upper().startswith("INSUFFISANT"):
            lines = [trim_to_sentence(l.strip(" •-*\t")) for l in txt.splitlines() if len(l.strip()) > 25]
            if lines:
                digest[mat] = "\n".join(lines[:6])
    return digest or None


# --------------------------------------------------------------------------------------------------
# Sorties : JSON, page web, flux RSS, notification
# --------------------------------------------------------------------------------------------------
def notify(new_items):
    topic = os.getenv("NTFY_TOPIC")
    if not topic or not new_items:
        return
    top = sorted(new_items, key=lambda i: i["date"], reverse=True)[:5]
    body = "\n".join(f"• {i['title'][:90]}" for i in top)
    if len(new_items) > 5:
        body += f"\n… et {len(new_items) - 5} de plus"
    try:
        requests.post(f"https://ntfy.sh/{topic}", timeout=20, data=body.encode("utf-8"),
                      headers={"Title": f"Veille notariale : {len(new_items)} nouveauté(s)".encode("utf-8"), "Tags": "scales"})
    except Exception as e:
        log("Notification ntfy", e)


def write_rss(items):
    e = html.escape
    rows = "".join(f"<item><title>{e(i['title'])}</title><link>{e(i['url'])}</link><guid>{e(i['id'])}</guid>"
                   f"<description>{e(i.get('abstract', ''))}</description></item>" for i in items[:100])
    open(os.path.join(DOCS, "feed.xml"), "w", encoding="utf-8").write(
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Veille juridique notariale</title>'
        f'<link>.</link><description>Actualités juridiques pour notaires</description>{rows}</channel></rss>')


def write_html(items, digest=None):
    dig = {"date": str(TODAY), "by_matiere": digest or {}}
    page = (PAGE.replace("__UPD__", dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y %H:%M UTC"))
            .replace("__DATA__", json.dumps(items, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__DIGEST__", json.dumps(dig, ensure_ascii=False).replace("</", "<\\/")))
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(page)


# --------------------------------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------------------------------
def main():
    os.makedirs(DOCS, exist_ok=True)
    store = {}
    if os.path.exists(DATA):
        store = {i["id"]: i for i in json.load(open(DATA, encoding="utf-8"))}

    # Nettoyage : seules les 3 sources retenues sont conservées ; les extraits sont ramenés à leur longueur publique ;
    # les fiches d'un ancien format (ou cassées) sont effacées pour être régénérées.
    kept = {k: i for k, i in store.items() if str(i.get("source", "")).startswith(ALLOWED_SOURCES)}
    if len(kept) != len(store):
        log(f"Nettoyage : {len(store) - len(kept)} élément(s) d'anciennes sources retiré(s).")
    store = kept
    for i in store.values():
        i["abstract"] = cut_teaser(i.get("abstract", ""))
        i.pop("type", None); i.pop("bulletin", None)
        if i.get("ai_v") != AI_VERSION:
            i.pop("ai", None); i.pop("ai_v", None)

    cutoff = (TODAY - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    added = 0
    for it in src_rss() + src_sepaj() + src_legalnews():
        if it["id"] in store or it["date"] < cutoff or not it["title"]:
            continue
        it["matieres"] = classify(it["title"] + " " + it.get("abstract", ""), it.get("url", ""))
        it["first_seen"] = str(TODAY)
        arts = extract_articles(it["title"] + " " + it.get("abstract", ""))
        if arts:
            it["articles"] = arts
        store[it["id"]] = it
        added += 1

    items = sorted(store.values(), key=lambda i: (i["date"], i["first_seen"]), reverse=True)
    digest = weekly_digest(items)                      # en premier : c'est le plus utile, il ne doit pas manquer de quota

    batch = int(os.getenv("AI_BATCH", "40"))
    deadline = time.time() + int(os.getenv("AI_TIME_BUDGET_S", "900"))
    done = 0
    for it in items:
        if done >= batch:
            break
        if it.get("ai_v") == AI_VERSION or it.get("ai_tries", 0) >= 3:
            continue
        if time.time() > deadline:
            log("Temps alloué aux fiches atteint : le reste sera repris au prochain lancement."); break
        if _providers() and all_ai_dead():
            log("Tous les fournisseurs IA sont à quota ou en erreur : arrêt immédiat des fiches pour ce run."); break
        if not _providers():
            log("Aucune clé IA configurée : pas de fiches."); break
        fiche, status = make_fiche(it)
        if status == "ok":
            it["ai"], it["ai_v"] = fiche, AI_VERSION; done += 1
        elif status == "vide":
            it["ai"], it["ai_v"] = None, AI_VERSION
        else:
            it["ai_tries"] = it.get("ai_tries", 0) + (0 if all_ai_dead() else 1)

    items = sorted(store.values(), key=lambda i: (i["date"], i["first_seen"]), reverse=True)
    json.dump(items, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"date": str(TODAY), "by_matiere": digest or {}}, open(os.path.join(DOCS, "digest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    write_html(items, digest)
    write_rss(items)
    notify([i for i in items if i["first_seen"] == str(TODAY)] if added else [])
    log(f"{added} nouveaux éléments, {len(items)} au total, {done} fiche(s) générée(s).")


PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Veille juridique notariale</title><link rel="alternate" type="application/rss+xml" href="feed.xml">
<link rel="manifest" href="manifest.json"><meta name="theme-color" content="#14213d">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Veille notariale"><link rel="apple-touch-icon" href="icon.png">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f2f0e9;--fg:#1d2433;--card:#fff;--mut:#667085;--bd:#e3e0d3;--navy:#14213d;--navy2:#26426b;--gold:#b8893b;--nd:#2f7a5b;--sp:#26426b;--ln:#8c2f39;--ring:#b8893b55;--side:#181f30;--soft:rgba(38,66,107,.06)}
:root[data-theme="dark"]{--bg:#0e1320;--fg:#e9ecf3;--card:#171f33;--mut:#9aa3b5;--bd:#28324a;--gold:#d9a95a;--nd:#6fcfa3;--sp:#7fa6e0;--ln:#e58a94;--ring:#d9a95a55;--side:#0b0f1a;--soft:rgba(127,166,224,.08)}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1320;--fg:#e9ecf3;--card:#171f33;--mut:#9aa3b5;--bd:#28324a;--gold:#d9a95a;--nd:#6fcfa3;--sp:#7fa6e0;--ln:#e58a94;--ring:#d9a95a55;--side:#0b0f1a;--soft:rgba(127,166,224,.08)}}
*{box-sizing:border-box}html{scroll-padding-top:70px}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 Inter,system-ui,sans-serif;overflow-x:hidden}
a{color:inherit}button{font-family:inherit}
.shell{display:flex;min-height:100vh}
.sidebar{width:262px;flex:none;background:var(--side);color:#dfe4ee;position:sticky;top:0;height:100vh;overflow-y:auto;padding:18px 14px calc(18px + env(safe-area-inset-bottom,0px));z-index:40}
.brand{display:flex;align-items:center;gap:10px;margin:0 4px 16px}
.mark{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,var(--gold),#8a5f22);display:flex;align-items:center;justify-content:center;font:700 17px 'Playfair Display',Georgia,serif;color:#1d1400;flex:none}
.brand b{font:700 15px 'Playfair Display',Georgia,serif;display:block;line-height:1.1}.brand small{font:500 10px Inter;color:#8b93a8;letter-spacing:.05em;text-transform:uppercase}
.ministats{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px}
.mstat{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:9px;padding:7px 4px;text-align:center}
.mstat b{display:block;font:700 15px 'Playfair Display',Georgia,serif;color:#f1d9a6}.mstat span{font-size:8.5px;color:#8b93a8;text-transform:uppercase;letter-spacing:.04em}
.navlbl{font:700 10px Inter;letter-spacing:.1em;text-transform:uppercase;color:#6b7690;margin:16px 6px 6px}
.navitem{display:flex;width:100%;align-items:center;gap:8px;text-align:left;background:none;border:none;color:#c7cede;padding:7px 8px;border-radius:8px;cursor:pointer;font:600 13px Inter}
.navitem:hover{background:rgba(255,255,255,.06)}.navitem.on{background:linear-gradient(135deg,var(--gold),#8a5f22);color:#1d1400}
.navitem .n{margin-left:auto;font:700 11px Inter;opacity:.65}.navitem .dot{width:8px;height:8px;border-radius:99px;flex:none}
.themebtn{margin-top:18px;width:100%;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:#c7cede;padding:8px;border-radius:9px;cursor:pointer;font:600 12px Inter}
.backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:35}.backdrop.show{display:block}
.content{flex:1;min-width:0}
.topbar{position:sticky;top:0;z-index:30;background:var(--bg);border-bottom:1px solid var(--bd);padding:10px 20px;display:flex;gap:10px;align-items:center}
.burger{display:none;background:none;border:1px solid var(--bd);border-radius:8px;padding:8px 10px;cursor:pointer;color:var(--fg);font-size:15px}
.search{position:relative;flex:1;max-width:460px}
.search input{width:100%;padding:10px 13px 10px 36px;border:1px solid var(--bd);border-radius:10px;font:14.5px Inter,system-ui;background:var(--card);color:var(--fg);outline:none}
.search input:focus{border-color:var(--gold);box-shadow:0 0 0 3px var(--ring)}
.search svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);width:15px;height:15px;stroke:var(--mut);fill:none;stroke-width:2}
.crumb{font:600 12px Inter;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.crumb b{color:var(--fg)}
.tbtn{margin-left:auto;flex:none;border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:9px;padding:7px 11px;font:600 12px Inter;cursor:pointer;white-space:nowrap}
.rss{flex:none;font:600 12px Inter;color:var(--gold);text-decoration:none}
main{max-width:980px;padding:18px 20px 34px}
.brief{background:linear-gradient(160deg,var(--navy),var(--navy2));border-radius:16px;padding:18px 20px 8px;margin-bottom:20px;color:#fff;box-shadow:0 8px 22px rgba(20,33,61,.16)}
.brief h2{font:700 19px 'Playfair Display',Georgia,serif;margin:0 0 2px}.brief .s2{font-size:12px;color:#cbd5e6;margin-bottom:10px}
.brief details{border-top:1px solid rgba(255,255,255,.15);padding:10px 0}
.brief summary{cursor:pointer;font:600 14px Inter;list-style:none;display:flex;justify-content:space-between}.brief summary::-webkit-details-marker{display:none}
.brief summary:after{content:"+";color:#e3c58c;font-size:17px}.brief details[open] summary:after{content:"–"}
.brief p{font-size:13.5px;line-height:1.6;color:#e7ecf5;margin:6px 0 2px}
.daysep{display:flex;align-items:center;gap:10px;margin:22px 2px 4px}.daysep:first-child{margin-top:0}
.daysep .l{font:700 11px Inter;letter-spacing:.1em;text-transform:uppercase;color:var(--gold)}
.daysep:before,.daysep:after{content:"";height:1px;flex:1;background:var(--bd)}
#list{display:flex;flex-direction:column;gap:14px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:16px 18px;position:relative;overflow:hidden;box-shadow:0 2px 8px rgba(20,33,61,.04)}
.card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c)}
.s-nd{--c:var(--nd)}.s-sp{--c:var(--sp)}.s-ln{--c:var(--ln)}
.top{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;margin-bottom:7px}
.badge{font:700 9.5px Inter;text-transform:uppercase;letter-spacing:.06em;padding:2px 9px;border-radius:99px;color:var(--c);border:1px solid var(--c)}
.badge.new{background:var(--gold);border-color:var(--gold);color:#14213d}
.badge.part{color:var(--mut);border-color:var(--bd);text-transform:none;letter-spacing:0;font-weight:600}
.date{margin-left:auto;font-size:11.5px;color:var(--mut);font-weight:600}
.card a.t{display:block;font:700 18px/1.3 'Playfair Display',Georgia,serif;text-decoration:none}.card a.t:hover{color:var(--gold)}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}
.tag{font-size:10.5px;font-weight:600;padding:2px 9px;border-radius:99px;background:rgba(184,137,59,.13);border:1px solid rgba(184,137,59,.35)}
.abs{font-size:14px;margin:10px 0 0;color:var(--mut)}
.resume{font-size:14.5px;margin:12px 0 0}
.aitag{display:inline-block;font:700 9.5px Inter;letter-spacing:.06em;text-transform:uppercase;color:var(--nd);background:rgba(47,122,91,.13);padding:2px 8px;border-radius:6px;margin-top:12px}
details.fiche{margin-top:10px;border:1px solid var(--bd);border-radius:11px;background:var(--soft)}
details.fiche>summary{cursor:pointer;list-style:none;padding:10px 14px;font:700 13px Inter;display:flex;justify-content:space-between;align-items:center}
details.fiche>summary::-webkit-details-marker{display:none}
details.fiche>summary:after{content:"Déplier ▾";font:600 11.5px Inter;color:var(--gold)}details.fiche[open]>summary:after{content:"Replier ▴"}
.fbody{padding:2px 16px 14px}
.sec{margin-top:14px}.sec h4{margin:0 0 5px;font:700 11px Inter;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}
.sec p{margin:0;font-size:14px}.sec ul,.sec ol{margin:0;padding-left:20px}.sec li{font-size:14px;margin:4px 0}
.chk{list-style:none;padding:0!important}.chk li{position:relative;padding-left:24px}.chk li:before{content:"☐";position:absolute;left:0;color:var(--gold);font-size:15px;line-height:1.4}
.vig{margin-top:14px;padding:11px 13px;border-radius:9px;background:rgba(184,137,59,.12);border-left:3px solid var(--gold);font-size:13.5px}
.vig b{display:block;color:var(--gold);font:700 10.5px Inter;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px}
.flow{margin-top:4px}.step{display:flex;gap:10px;align-items:flex-start;position:relative;padding-bottom:12px}
.step:not(:last-child):before{content:"";position:absolute;left:12px;top:26px;bottom:0;width:2px;background:var(--bd)}
.step .n{flex:none;width:26px;height:26px;border-radius:99px;background:var(--navy2);color:#fff;font:700 12px Inter;display:flex;align-items:center;justify-content:center}
:root[data-theme="dark"] .step .n{background:var(--gold);color:#14213d}
.step div{font-size:13.5px;padding-top:2px}
.refs{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.ref{font:600 11px Inter;padding:3px 9px;border-radius:7px;background:var(--bg);border:1px solid var(--bd);color:var(--mut)}
.src{display:inline-block;margin-top:12px;font:700 12.5px Inter;color:var(--gold);text-decoration:none}
.note{font-size:12px;color:var(--mut);margin-top:8px}
footer{padding:16px 20px calc(24px + env(safe-area-inset-bottom,0px));font-size:11.5px;color:var(--mut);border-top:1px solid var(--bd);max-width:980px}
@media(max-width:860px){.sidebar{position:fixed;left:0;top:0;transform:translateX(-100%);transition:transform .2s;width:80vw;max-width:300px}.sidebar.open{transform:none}.burger{display:inline-block}main{padding:14px}.crumb{display:none}}
</style></head><body>
<div class="shell"><div class="backdrop" id="bd"></div>
<aside class="sidebar" id="sb">
 <div class="brand"><div class="mark">§</div><div><b>Veille notariale</b><small>Actualité juridique</small></div></div>
 <div class="ministats"><div class="mstat"><b id="n1">0</b><span>Total</span></div><div class="mstat"><b id="n2">0</b><span>7 jours</span></div><div class="mstat"><b id="n3">0</b><span>Fiches</span></div></div>
 <div class="navlbl">Source</div><div id="navsrc"></div>
 <div class="navlbl">Matière</div><div id="navmat"></div>
 <button class="themebtn" id="themebtn">🌗 <span id="themelbl">Thème auto</span></button>
</aside>
<div class="content">
 <div class="topbar"><button class="burger" id="burger">☰</button>
  <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg><input id="q" placeholder="Rechercher : donation, bail, indivision, article 784…"></div>
  <div class="crumb" id="crumb"></div><button class="tbtn" id="expand">Tout déplier</button><a class="rss" href="feed.xml">RSS</a></div>
 <main><div id="brief"></div><div id="list"></div></main>
 <footer>Mise à jour <span>__UPD__</span>. Sources publiques : Notaires de France, SEPAJ, LegalNews Notaires (titres et extraits que ces sites publient eux-mêmes ; leurs contenus réservés aux abonnés ne sont pas lus). Les fiches sont des synthèses reformulées par IA à partir du texte public de la source : elles peuvent comporter des erreurs et ne remplacent jamais la lecture de la source. Vérifiez toujours avant tout usage professionnel.</footer>
</div></div>
<script>
const D=__DATA__, DIGEST=__DIGEST__; let src="",mat="",allOpen=false; const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const CLS={"Notaires de France":"nd","SEPAJ":"sp","LegalNews":"ln"}, COL={nd:"var(--nd)",sp:"var(--sp)",ln:"var(--ln)"};
const SRC=[...new Set(D.map(i=>i.source))].sort(), MAT=[...new Set(D.flatMap(i=>i.matieres))].sort();
const cut7=Date.now()-7*864e5, isNew=i=>new Date(i.first_seen).getTime()>=cut7;
function fd(d){try{return new Date(d).toLocaleDateString("fr-FR",{day:"numeric",month:"short",year:"numeric"})}catch(e){return d}}
$("n1").textContent=D.length;$("n2").textContent=D.filter(i=>new Date(i.date).getTime()>=cut7).length;$("n3").textContent=D.filter(i=>i.ai).length;

let theme="auto";try{theme=localStorage.getItem("veille-theme")||"auto"}catch(e){}
const TL={auto:"Thème auto",light:"Thème clair",dark:"Thème sombre"};
function applyTheme(){document.documentElement.setAttribute("data-theme",theme==="auto"?"":theme);$("themelbl").textContent=TL[theme]}
applyTheme();$("themebtn").onclick=()=>{theme={auto:"light",light:"dark",dark:"auto"}[theme];try{localStorage.setItem("veille-theme",theme)}catch(e){}applyTheme()};
$("burger").onclick=()=>{$("sb").classList.add("open");$("bd").classList.add("show")};
function closeSb(){$("sb").classList.remove("open");$("bd").classList.remove("show")}$("bd").onclick=closeSb;

function brief(){
 const e=DIGEST&&DIGEST.by_matiere?Object.entries(DIGEST.by_matiere):[]; if(!e.length)return;
 $("brief").innerHTML=`<div class="brief"><h2>📋 Brief de la semaine</h2><div class="s2">Synthèse automatique (IA) des 7 derniers jours, par matière — généré le ${fd(DIGEST.date)} · à vérifier sur les fiches</div>
 ${e.map(([m,t],k)=>`<details ${k===0?"open":""}><summary>${esc(m)}</summary>${t.split(/\n+/).filter(Boolean).map(s=>`<p>• ${esc(s)}</p>`).join("")}</details>`).join("")}</div>`;
}
function nav(){
 $("navsrc").innerHTML=`<button class="navitem ${!src&&!mat?"on":""}" data-k="all">Toutes les sources <span class="n">${D.length}</span></button>`+
  SRC.map(s=>`<button class="navitem ${src===s?"on":""}" data-k="src" data-v="${esc(s)}"><span class="dot" style="background:${COL[CLS[s]]||"var(--mut)"}"></span>${esc(s)} <span class="n">${D.filter(i=>i.source===s).length}</span></button>`).join("");
 $("navmat").innerHTML=MAT.map(m=>`<button class="navitem ${mat===m?"on":""}" data-k="mat" data-v="${esc(m)}">${esc(m)} <span class="n">${D.filter(i=>i.matieres.includes(m)).length}</span></button>`).join("");
 document.querySelectorAll(".navitem").forEach(b=>b.onclick=()=>{const k=b.dataset.k,v=b.dataset.v;
  if(k==="all"){src="";mat=""}else if(k==="src"){src=src===v?"":v}else{mat=mat===v?"":v}
  nav();draw();if(innerWidth<=860)closeSb()});
 $("crumb").innerHTML=(src||mat)?`Filtre : <b>${esc([src,mat].filter(Boolean).join(" · "))}</b>`:`<b>Toutes les publications</b>`;
}
function group(i){const d=new Date(i.date),t=new Date();t.setHours(0,0,0,0);const n=Math.round((t-new Date(d.getFullYear(),d.getMonth(),d.getDate()))/864e5);
 return n<=0?"Aujourd'hui":n===1?"Hier":n<=7?"Cette semaine":n<=31?"Ce mois-ci":"Plus ancien"}
const ul=(a,c)=>a&&a.length?`<ul class="${c||""}">${a.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:"";
function sec(t,body){return body?`<div class="sec"><h4>${t}</h4>${body}</div>`:""}
function fiche(i){
 const a=i.ai; if(!a)return"";
 const flow=a.schema&&a.schema.etapes&&a.schema.etapes.length?sec("Schéma — "+esc(a.schema.titre||"enchaînement"),`<div class="flow">${a.schema.etapes.map((s,k)=>`<div class="step"><span class="n">${k+1}</span><div>${esc(s)}</div></div>`).join("")}</div>`):"";
 const refs=(a.references&&a.references.length?a.references:(i.articles||[]));
 return `<details class="fiche" ${allOpen?"open":""}><summary>Fiche détaillée</summary><div class="fbody">
  ${sec("Contexte",a.contexte?`<p>${esc(a.contexte)}</p>`:"")}
  ${sec("Ce qu'il faut retenir",a.points&&a.points.length?`<ol>${a.points.map(x=>`<li>${esc(x)}</li>`).join("")}</ol>`:"")}
  ${sec("Règles et conditions",ul(a.regles))}
  ${sec("Qui est concerné",a.qui?`<p>${esc(a.qui)}</p>`:"")}
  ${sec("À vérifier dans la pratique",ul(a.a_verifier,"chk"))}
  ${a.vigilance?`<div class="vig"><b>⚠ Vigilance</b>${esc(a.vigilance)}</div>`:""}
  ${flow}
  ${refs&&refs.length?sec("Références citées",`<div class="refs">${refs.map(r=>`<span class="ref">${esc(r)}</span>`).join("")}</div>`):""}
  ${a.partielle?`<p class="note">Source partielle : seul l'extrait public a pu être exploité (l'article complet est réservé aux abonnés). Cette fiche ne couvre donc pas tout l'article.</p>`:""}
 </div></details>`;
}
function draw(){
 const q=$("q").value.toLowerCase();
 const r=D.filter(i=>(!src||i.source===src)&&(!mat||i.matieres.includes(mat))&&(!q||(i.title+" "+i.abstract+" "+(i.ai?i.ai.resume+" "+(i.ai.points||[]).join(" "):"")).toLowerCase().includes(q))).slice(0,300);
 let last=null,h="";
 for(const i of r){const g=group(i);if(g!==last){h+=`<div class="daysep"><span class="l">${g}</span></div>`;last=g}
  const c=CLS[i.source]||"sp";
  h+=`<article class="card s-${c}"><div class="top"><span class="badge">${esc(i.source)}</span>${isNew(i)?'<span class="badge new">Nouveau</span>':""}${i.ai&&i.ai.partielle?'<span class="badge part">Extrait public</span>':""}<span class="date">${fd(i.date)}</span></div>
  <a class="t" href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>
  <div class="tags">${i.matieres.map(m=>`<span class="tag">${esc(m)}</span>`).join("")}</div>
  ${i.ai?`<span class="aitag">Fiche IA — à vérifier</span><p class="resume">${esc(i.ai.resume)}</p>`:`<p class="abs">${esc(i.abstract)}</p>`}
  ${fiche(i)}<a class="src" href="${esc(i.url)}" target="_blank" rel="noopener">Consulter la source →</a></article>`}
 $("list").innerHTML=h||'<p style="color:var(--mut)">Aucun résultat.</p>';
}
$("expand").onclick=()=>{allOpen=!allOpen;document.querySelectorAll("details.fiche").forEach(d=>d.open=allOpen);$("expand").textContent=allOpen?"Tout replier":"Tout déplier"};
$("q").oninput=draw;brief();nav();draw();
</script></body></html>
"""

if __name__ == "__main__":
    main()
