"""Veille juridique notariale (version privée).

Sources : Notaires de France (flux RSS), SEPAJ et LegalNews Notaires (pages publiques : titre + extrait), et les
NOTA-BENE du CRIDON Sud-Ouest (PDF accessibles avec les identifiants de l'abonné, lus à titre strictement personnel).

Confidentialité : les données (fiches, brief, NOTA-BENE) sont CHIFFRÉES (AES-256-GCM) avec la phrase secrète
DATA_PASSPHRASE avant d'être publiées. Le dépôt GitHub peut rester public : sans la phrase secrète, seul du
chiffré illisible est visible. Aucun texte intégral n'est publié ; les fiches sont des reformulations.
"""
import os, sys, json, html, re, time, zlib, base64, hashlib, secrets, datetime as dt
from urllib.parse import urljoin
import requests, feedparser

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(DOCS, "data.enc")
TODAY = dt.date.today()
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "45"))
AI_VERSION = 4                      # change quand le format des fiches change : les anciennes sont régénérées
CRIDON_VERSION = 1                  # idem pour les fiches CRIDON
ALLOWED_SOURCES = ("Notaires de France", "SEPAJ", "LegalNews")
TEASER_MAX = 320                    # longueur maximale de l'extrait affiché (celui que publie la source)
CRIDON_BASE = "https://www.cridon-so.fr"
UA_BOT = {"User-Agent": "veille-notariale/3.0"}
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
# Chiffrement : PBKDF2-SHA256 (600 000 itérations) -> AES-256-GCM ; charge utile compressée (zlib)
# --------------------------------------------------------------------------------------------------
KDF_ITER = 600_000


def _kdf(passphrase, salt, iters=KDF_ITER):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iters, dklen=32)


def encrypt_payload(obj, passphrase, salt=None):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = salt or secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    raw = zlib.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
    ct = AESGCM(_kdf(passphrase, salt)).encrypt(iv, raw, b"veille-v1")
    b = lambda x: base64.b64encode(x).decode("ascii")
    return {"v": 1, "kdf": "PBKDF2-SHA256", "it": KDF_ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt_payload(blob, passphrase):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = lambda k: base64.b64decode(blob[k])
    raw = AESGCM(_kdf(passphrase, d("salt"), int(blob.get("it", KDF_ITER)))).decrypt(d("iv"), d("ct"), b"veille-v1")
    return json.loads(zlib.decompress(raw).decode("utf-8"))


def load_payload(passphrase):
    """Charge les données chiffrées existantes. Fichier absent -> None. Mauvaise phrase secrète -> arrêt explicite."""
    if not os.path.exists(DATA):
        return None
    try:
        return decrypt_payload(json.load(open(DATA, encoding="utf-8")), passphrase)
    except Exception:
        log("ERREUR : data.enc existe mais ne peut pas être déchiffré avec DATA_PASSPHRASE (phrase secrète modifiée ?).")
        log("Si vous avez volontairement changé la phrase secrète : supprimez docs/data.enc sur GitHub puis relancez.")
        sys.exit(1)


def save_payload(payload, passphrase):
    """N'écrit data.enc que si le contenu a changé (évite de gonfler l'historique du dépôt)."""
    old = None
    if os.path.exists(DATA):
        try:
            blob = json.load(open(DATA, encoding="utf-8"))
            if decrypt_payload(blob, passphrase) == json.loads(json.dumps(payload, ensure_ascii=False)):
                log("Données inchangées : data.enc conservé.")
                return False
            old = base64.b64decode(blob["salt"])
        except Exception:
            old = None
    json.dump(encrypt_payload(payload, passphrase, old), open(DATA, "w", encoding="utf-8"))
    return True


# --------------------------------------------------------------------------------------------------
# IA : fournisseurs en cascade, arrêt immédiat quand un quota est épuisé
#   - contenus publics : Groq (gpt-oss-120b puis 20b), Gemini, Anthropic (haiku) si clé
#   - contenus privés (CRIDON) : Anthropic (sonnet) si clé, sinon Groq gpt-oss-120b. JAMAIS Gemini (offre gratuite
#     susceptible de servir à l'entraînement).
# --------------------------------------------------------------------------------------------------
_DEAD = set()
_QUOTA_HINTS = ("per day", "tokens per day", "tpd", "requests per day", "rpd", "billing", "exceeded your current quota",
                "credit balance", "insufficient")
GROQ_TPM = int(os.getenv("GROQ_TPM", "7000"))      # plafond gratuit ~8000 tokens/minute, marge de sécurité
_groq_log = []                                      # (instant, tokens) des appels Groq de la dernière minute


def _providers(private=False):
    p = []
    if private:
        if os.getenv("ANTHROPIC_API_KEY"):
            p.append(("anthropic-sonnet", "anthropic", "claude-sonnet-5-5"))
        if os.getenv("GROQ_API_KEY"):
            p.append(("groq-120b", "groq", "openai/gpt-oss-120b"))
        return p
    if os.getenv("GROQ_API_KEY"):
        p += [("groq-120b", "groq", "openai/gpt-oss-120b"), ("groq-20b", "groq", "openai/gpt-oss-20b")]
    if os.getenv("GEMINI_API_KEY"):
        p.append(("gemini", "gemini", "gemini-flash-latest"))
    if os.getenv("ANTHROPIC_API_KEY"):
        p.append(("anthropic", "anthropic", "claude-haiku-5-5"))
    return p


def _retry_after(body):
    m = re.search(r"try again in\s*(?:(\d+)m)?\s*(\d+(?:\.\d+)?)s", body or "", re.I)
    return (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else None


def _groq_pace(est):
    """Respecte la limite par minute de Groq en attendant juste ce qu'il faut (évite les 429 en rafale)."""
    while True:
        now = time.time()
        _groq_log[:] = [(t, n) for t, n in _groq_log if now - t < 61]
        used = sum(n for _, n in _groq_log)
        if used + est <= GROQ_TPM or not _groq_log:
            break
        time.sleep(max(1.0, _groq_log[0][0] + 61 - now))
    _groq_log.append((time.time(), est))


def _request(kind, model, prompt, max_tokens):
    if kind == "groq":
        _groq_pace(len(prompt) // 3 + max_tokens)
        body = {"model": model, "temperature": 0.2, "max_completion_tokens": max_tokens, "reasoning_effort": "low",
                "messages": [{"role": "user", "content": prompt}]}
        return requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=180, json=body,
                             headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '').strip()}"})
    if kind == "gemini":
        return requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", timeout=120,
                             headers={"x-goog-api-key": os.getenv("GEMINI_API_KEY", "").strip()},
                             json={"contents": [{"parts": [{"text": prompt}]}],
                                   "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2}})
    return requests.post("https://api.anthropic.com/v1/messages", timeout=300,
                         headers={"x-api-key": os.getenv("ANTHROPIC_API_KEY", "").strip(), "anthropic-version": "2023-06-01"},
                         json={"model": model, "max_tokens": max_tokens, "temperature": 0.2,
                               "messages": [{"role": "user", "content": prompt}]})


def _extract(kind, r):
    j = r.json()
    if kind == "groq":
        ch = j["choices"][0]
        txt = (ch["message"].get("content") or "").strip()
        return txt if ch.get("finish_reason") != "length" else txt          # JSON tronqué : réparé plus loin
    if kind == "gemini":
        return j["candidates"][0]["content"]["parts"][0]["text"].strip()
    return j["content"][0]["text"].strip()


def call_ai(prompt, max_tokens=2800, private=False):
    """Essaie les fournisseurs dans l'ordre. Quota journalier épuisé ou erreur de configuration -> fournisseur écarté
    pour tout le run, sans nouvel essai. Simple limite par minute -> une seule attente courte, puis on continue."""
    if not private:
        time.sleep(1.0)
    for name, kind, model in _providers(private):
        if name in _DEAD:
            continue
        try:
            r = _request(kind, model, prompt, max_tokens)
            if r.status_code == 400 and kind == "groq" and "reasoning" in (r.text or "").lower():
                r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=180,
                                  headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '').strip()}"},
                                  json={"model": model, "temperature": 0.2, "max_completion_tokens": max_tokens,
                                        "messages": [{"role": "user", "content": prompt}]})
            if r.status_code in (429, 503, 529) and not any(h in (r.text or "").lower() for h in _QUOTA_HINTS):
                wait = _retry_after(r.text)
                wait = min(wait + 1, 75) if wait is not None else 15
                log(f"IA ({name}) : limite de débit, attente {wait:.0f}s puis nouvel essai")
                time.sleep(wait)
                r = _request(kind, model, prompt, max_tokens)
            if r.status_code >= 400:
                log(f"IA ({name}) : échec {r.status_code} — {(r.text or '')[:220].replace(chr(10), ' ')}")
                if r.status_code not in (503, 529):
                    _DEAD.add(name)          # quota épuisé ou erreur de configuration : on ne réessaie plus ce run-ci
                continue
            return _extract(kind, r)
        except Exception as e:
            log(f"IA ({name}) : exception", e)
            _DEAD.add(name)
    return None


def all_ai_dead(private=False):
    p = [n for n, _, _ in _providers(private)]
    return bool(p) and all(n in _DEAD for n in p)


def _close_json(s):
    """Referme les structures ouvertes d'un JSON coupé ; None si impossible."""
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
    return s + ('"' if in_str else "") + "".join(reversed(stack))


def _json_from(txt):
    """Extrait un objet JSON ; répare un JSON tronqué en coupant au dernier élément complet."""
    if not txt:
        return None
    s = txt.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    a = s.find("{")
    if a < 0:
        return None
    s = s[a:]
    b = s.rfind("}")
    try:
        d = json.loads(s[:b + 1]) if b > 0 else None
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    cuts = [m.end() for m in re.finditer(r'[}\]"]', s)]
    for c in reversed(cuts[-400:]):
        head = s[:c].rstrip().rstrip(",")
        fixed = _close_json(head)
        if not fixed:
            continue
        try:
            d = json.loads(fixed)
            if isinstance(d, dict):
                return d
        except Exception:
            continue
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


# --------------------------------------------------------------------------------------------------
# CRIDON Sud-Ouest : NOTA-BENE (PDF, accès abonné avec les identifiants de l'utilisateur)
# Usage strictement personnel : les fiches sont des reformulations, chiffrées, jamais publiées en clair.
# --------------------------------------------------------------------------------------------------
MOIS = ["janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"]
_NOISE = re.compile(r"^(NOTA-BENE|[A-ZÉÛÈ]+\s+\d{4}\s*/\s*N°\s*\d+|ET DE LA FAMILLE|DROIT (DU PATRIMOINE|IMMOBILIER|DE LA FAMILLE|DES AFFAIRES|RURAL|FISCAL|PUBLIC|COMMERCIAL)[A-ZÉ ]*)$", re.I)


def _norm_line(s):
    return re.sub(r"\s+", " ", s).strip()


def _page_columns_text(q, W, H):
    """Texte d'une page, colonne gauche puis droite si la page est sur deux colonnes."""
    mid = W / 2
    ws = q.extract_words()
    body = [w for w in ws if 0.08 * H < w["top"] < 0.95 * H]
    cross = [w for w in body if w["x0"] < mid - 4 and w["x1"] > mid + 4]
    left = sum(1 for w in body if w["x1"] <= mid + 4)
    right = sum(1 for w in body if w["x0"] >= mid - 4)
    if len(cross) <= 3 and left > 40 and right > 40:
        a = q.crop((0, 0, mid, H)).extract_text() or ""
        b = q.crop((mid, 0, W, H)).extract_text() or ""
        return a + "\n" + b
    return q.extract_text() or ""


def read_issue_pdf(path):
    """Retourne {'numero','mois','annee','docs':[{'num','rubrique','titre','auteur','texte'}]}.
    docs[0] peut être l'éditorial (num='ED'). Les pages sans numéro de fiche (jeux, agenda, podcast) sont ignorées."""
    import pdfplumber
    info = {"numero": None, "mois": None, "annee": None, "docs": []}
    pages = []
    with pdfplumber.open(path) as pdf:
        for n, p in enumerate(pdf.pages, 1):
            W, H = p.width, p.height
            rot = [w["text"] for w in p.extract_words() if not w.get("upright", True)] if n > 1 else []
            num = None
            for t in rot:
                if re.fullmatch(r"\d{4}", t):
                    num = t[::-1]                      # le numéro de fiche est imprimé à la verticale, donc lu à l'envers
            q = p.filter(lambda o: o.get("upright", True) or o.get("object_type") != "char")
            if n == 1:
                txt = q.extract_text() or ""
                m = re.search(r"(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})", txt, re.I)
                if m:
                    info["mois"], info["annee"] = m.group(1).lower(), int(m.group(2))
                m = re.search(r"num[ée]ro\s*(\d{3,4})", txt, re.I)
                if m:
                    info["numero"] = int(m.group(1))
                info["_cover"] = txt
                continue
            if num is None:
                if not pages:                                           # avant la première fiche : éditorial ?
                    words = p.extract_words()
                    if len(words) < 1500:
                        txt = q.extract_text() or ""
                        if "ÉDITORIAL" in txt[:200].upper() or "EDITORIAL" in txt[:200].upper():
                            pages.append(("ED", n, txt, None))
                continue
            words = q.extract_words(extra_attrs=["size"])
            heads = [w for w in words if w["size"] > 13 and 95 < w["top"] < 330 and w["x0"] < W / 2]
            pages.append((num, n, _page_columns_text(q, W, H), heads))
    info.pop("_cover", None)
    order = []
    for num, n, txt, heads in pages:
        if not order or order[-1]["num"] != num:
            order.append({"num": num, "pages": [], "heads": heads or []})
        order[-1]["pages"].append(txt)
    for d in order:
        lines = []
        raw = "\n".join(d["pages"]).splitlines()
        author = None
        if d["num"] != "ED":
            hl = " ".join(w["text"] for w in d["heads"])
            d["titre"] = _norm_line(hl).capitalize() if hl else f"Fiche {d['num']}"
            head = [_norm_line(l) for l in raw[:4]]
            rub = []
            for l in head:
                if re.match(r"^(DROIT\b|ET DE LA FAMILLE)", l):
                    rub.append(l)
            d["rubrique"] = _norm_line(" ".join(rub)).capitalize() if rub else ""
            for l in head:
                if re.fullmatch(r"[A-ZÉÈÎÂÔÛ\-]{3,}( [A-ZÉÈÎÂÔÛ'\-]{2,}){1,2}", l) and not l.startswith(("DROIT", "NOTA", "ET DE")):
                    author = l
        else:
            d["titre"] = "Éditorial"; d["rubrique"] = "Éditorial"
        d["auteur"] = author.title() if author else ""
        for l in raw:
            s = _norm_line(l)
            if not s or _NOISE.match(s) or (author and s == author):
                continue
            lines.append(s)
        txt = "\n".join(lines)
        txt = re.sub(r"-\n(?=[a-zéèêàùâîôû])", "", txt)           # mots coupés en fin de ligne
        d["texte"] = txt
        del d["pages"], d["heads"]
    info["docs"] = order
    return info


def _bs(html_text):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html_text or "", "html.parser")


def _find_login_form(soup):
    for f in soup.find_all("form"):
        if f.find("input", {"type": "password"}):
            return f
    return None


def cridon_login(s, user, pw):
    """Connexion au site (formulaire Drupal). Retourne (ok, rejet_explicite). Aucun mot de passe n'est journalisé."""
    form, r = None, None
    for path in ("/user/login", "/nota-bene?check_logged_in=1", "/"):
        try:
            r = s.get(CRIDON_BASE + path, timeout=40)
        except Exception as e:
            log("CRIDON : site inaccessible", path, e); return False, False
        form = _find_login_form(_bs(r.text))
        if form is not None:
            break
    if form is None:
        log(f"CRIDON : aucun formulaire de connexion trouvé (dernière page : {r.url}, HTTP {r.status_code}).")
        return False, False
    fields = [(i.get("name"), (i.get("type") or "text").lower()) for i in form.find_all("input") if i.get("name")]
    log("CRIDON : formulaire de connexion repéré, champs :", ", ".join(f"{n}({t})" for n, t in fields))
    data, submit = {}, None
    for i in form.find_all(["input", "select", "textarea"]):
        n, t = i.get("name"), (i.get("type") or "text").lower()
        if not n:
            continue
        if t in ("submit", "image", "button"):
            if submit is None and t == "submit":
                submit = (n, i.get("value", ""))
            continue
        if t in ("checkbox", "radio") and not i.has_attr("checked"):
            continue
        data[n] = i.get("value", "")
    pw_field = next(n for n, t in fields if t == "password")
    cand = [n for n, t in fields if t in ("text", "email") and not any(k in n.lower() for k in ("search", "captcha", "honey", "url"))]
    pref = [n for n in cand if any(k in n.lower() for k in ("name", "mail", "user", "login", "ident"))]
    user_field = (pref or cand or [None])[0]
    if not user_field:
        log("CRIDON : champ identifiant introuvable."); return False, False
    data[user_field], data[pw_field] = user, pw
    if submit:
        data[submit[0]] = submit[1]
    action = urljoin(r.url, form.get("action") or r.url)
    try:
        r2 = s.post(action, data=data, timeout=40, allow_redirects=True)
        r3 = s.get(CRIDON_BASE + "/nota-bene", timeout=40)
    except Exception as e:
        log("CRIDON : erreur réseau pendant la connexion", e); return False, False
    still = _find_login_form(_bs(r3.text)) is not None
    log(f"CRIDON : connexion -> HTTP {r2.status_code}, page nota-bene HTTP {r3.status_code}, formulaire encore présent : {still}")
    if still:
        msg = _bs(r2.text).find(class_=re.compile("messages|error|alert", re.I))
        if msg:
            log("CRIDON : message du site :", msg.get_text(" ", strip=True)[:200])
        return False, r2.status_code < 500 and r3.status_code < 500          # identifiants refusés (ou captcha)
    return r3.status_code == 200, False


def _num_from(*texts):
    """Numéro de NOTA-BENE d'après l'adresse ou le libellé d'un lien (ex. NB_314_septembre.pdf, « Nota-Bene n° 314 »)."""
    for t in texts:
        m = re.search(r"(?:nota[-_ ]?bene|\bnb)[-_ ]*(?:n\s*[°ºo]?\s*)?[-_ ]*(\d{3})(?!\d)|\bn\s*[°º]\s*(\d{3})(?!\d)", t or "", re.I)
        if m:
            return int(m.group(1) or m.group(2))
    return None


def cridon_list_pdfs(s):
    """Liste les PDF de NOTA-BENE visibles une fois connecté. Retourne [{'url','num'}] du plus récent au plus ancien."""
    seen, out, pages, visited = set(), [], [CRIDON_BASE + "/nota-bene"] + [f"{CRIDON_BASE}/nota-bene?page={i}" for i in (1, 2)], set()
    n_fetch = 0
    queue = list(pages)
    while queue and n_fetch < 14:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url); n_fetch += 1
        try:
            r = s.get(url, timeout=40)
        except Exception as e:
            log("CRIDON : liste inaccessible", url, e); continue
        if r.status_code != 200:
            continue
        for a in _bs(r.text).find_all("a", href=True):
            href = urljoin(r.url, a["href"]).split("#")[0]
            ctx = a.get_text(" ", strip=True) + " " + (a.parent.get_text(" ", strip=True)[:200] if a.parent else "")
            if re.search(r"\.pdf(\?|$)|/system/files/", href, re.I):
                if href not in seen:
                    seen.add(href); out.append({"url": href, "num": _num_from(href, ctx)})
            elif "/nota-bene" in href and href not in visited and href.startswith(CRIDON_BASE) and "page=" not in href \
                    and len(queue) < 10 and re.search(r"\d{3}", href + ctx):
                queue.append(href)
    out.sort(key=lambda x: (x["num"] is None, -(x["num"] or 0)))
    log(f"CRIDON : {len(out)} PDF repéré(s) :", ", ".join(str(o["num"]) for o in out[:12]))
    return out


def cridon_download(s, url, dest):
    r = s.get(url, timeout=180)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        log(f"CRIDON : téléchargement refusé ({url[-60:]}) HTTP {r.status_code}, {len(r.content)} octets, début : {r.content[:40]!r}")
        return False
    open(dest, "wb").write(r.content)
    return True


def split_chunks(text, size=6500):
    """Découpe un texte en parties d'environ `size` caractères, de préférence en fin de paragraphe."""
    out, cur, n = [], [], 0
    for line in text.splitlines():
        cur.append(line); n += len(line) + 1
        if (n >= size and line.rstrip().endswith((".", ":", "?", "»", ")", ";"))) or n >= size * 1.35:
            out.append("\n".join(cur)); cur, n = [], 0
    if cur:
        tail = "\n".join(cur)
        if out and len(tail) < size * 0.25:
            out[-1] += "\n" + tail
        else:
            out.append(tail)
    return out


def cridon_chunk_prompt(doc, i, n, chunk):
    return (
        "Tu es juriste et prépares, pour un notaire français exigeant, la fiche d'étude d'un article de la revue "
        "NOTA-BENE du CRIDON. Le notaire ne lira PAS l'article : ta fiche doit lui permettre de tout savoir et "
        "d'agir. Travaille STRICTEMENT à partir du passage fourni.\n"
        "RÈGLES ABSOLUES : (1) n'invente rien : aucun article, date, chiffre, arrêt ou conséquence absent du passage ; "
        "(2) REFORMULE avec tes mots, sans recopier de phrases (citation de 15 mots maximum, exceptionnelle) ; "
        "(3) EXHAUSTIVITÉ : chaque règle, condition, exception, délai, seuil, taux, montant, texte visé, décision citée, "
        "exemple chiffré et position doctrinale du passage doit se retrouver dans un point, avec sa base légale ; "
        "(4) distingue le droit positif de l'opinion : quand l'auteur ou le CRIDON recommande ou soutient une position "
        "(« nous pensons », « selon nous », « il est conseillé »), écris-le explicitement « Position de l'auteur : … » ; "
        "(5) phrases complètes et précises, pas de formules vagues (« pourrait », « dans certains cas ») quand le texte "
        "est précis ; (6) conserve les intertitres du texte comme titres de sections.\n"
        f"ARTICLE : « {doc['titre']} » ({doc.get('rubrique', '')}) — partie {i} sur {n}.\n"
        "Réponds UNIQUEMENT par un objet JSON valide et complet avec exactement ces clés :\n"
        '{"resume_partiel": "4 à 7 phrases : ce que dit cette partie, solution retenue, portée",\n'
        ' "sections": [{"titre": "intertitre", "points": ["un point autonome et complet par idée : règle, condition, exception, délai, chiffre, base légale"]}],\n'
        ' "jurisprudence": [{"decision": "juridiction, date, n°", "solution": "ce qui est jugé, sur quel fondement, avec les faits utiles"}],\n'
        ' "tableaux": [{"titre": "", "colonnes": ["", ""], "lignes": [["", ""]]}],\n'
        ' "a_verifier": ["geste concret pour le notaire, seulement si le texte le donne ou l\'implique"],\n'
        ' "vigilance": ["piège, point discuté, divergence, réserve ou risque signalé"],\n'
        ' "references": ["textes et décisions cités, format court"],\n'
        ' "etapes": ["étapes ordonnées d\'une procédure ou chronologie décrite dans cette partie ; liste vide sinon"]}\n'
        "Le champ « tableaux » n'est rempli que si le passage compare des régimes, situations, seuils ou taux "
        "(sinon liste vide). Ne laisse jamais « sections » vide.\n\n"
        f"PASSAGE :\n{chunk}")


def parse_chunk(txt):
    d = _json_from(txt)
    if not isinstance(d, dict):
        return None
    secs = []
    for s in d.get("sections") or []:
        if isinstance(s, dict):
            pts = _lst(s.get("points"), 40, 800)
            if pts:
                secs.append({"titre": str(s.get("titre", "")).strip()[:170], "points": pts})
    if not secs:
        return None
    jur = [{"decision": str(j.get("decision", "")).strip()[:200], "solution": str(j.get("solution", "")).strip()[:700]}
           for j in (d.get("jurisprudence") or []) if isinstance(j, dict) and str(j.get("decision", "")).strip()][:20]
    tabs = []
    for t in d.get("tableaux") or []:
        if not isinstance(t, dict):
            continue
        cols = [str(c).strip()[:80] for c in (t.get("colonnes") or [])][:6]
        rows = []
        for ln in t.get("lignes") or []:
            if isinstance(ln, list) and any(str(c).strip() for c in ln):
                row = [str(c).strip()[:300] for c in ln][:len(cols)]
                rows.append(row + [""] * (len(cols) - len(row)))
        if len(cols) >= 2 and len(rows) >= 2:
            tabs.append({"titre": str(t.get("titre", "")).strip()[:140], "colonnes": cols, "lignes": rows[:14]})
    return {"resume": str(d.get("resume_partiel", "")).strip()[:1500], "sections": secs, "jurisprudence": jur,
            "tableaux": tabs[:4], "a_verifier": _lst(d.get("a_verifier"), 15, 500), "vigilance": _lst(d.get("vigilance"), 12, 600),
            "references": _lst(d.get("references"), 25, 170), "etapes": _lst(d.get("etapes"), 10, 260)}


def _dedupe(lst, n):
    seen, out = set(), []
    for x in lst:
        k = re.sub(r"\W+", "", x.lower())[:80]
        if k and k not in seen:
            seen.add(k); out.append(x)
    return out[:n]


def cridon_reduce_prompt(doc, parts):
    resumes = "\n".join(f"- {p['resume']}" for p in parts if p.get("resume"))
    titres = "\n".join("- " + s["titre"] for p in parts for s in p["sections"] if s["titre"])
    return (f"Voici les notes d'étude, partie par partie, de l'article « {doc['titre']} » (revue NOTA-BENE du CRIDON), "
            "destinées à un notaire. En t'appuyant UNIQUEMENT sur elles (rien d'inventé), produis la synthèse d'ensemble. "
            "Réponds UNIQUEMENT par un objet JSON valide et complet :\n"
            '{"resume": "6 à 10 phrases complètes : problématique, solution ou règle, raisonnement, portée pratique",\n'
            ' "contexte": "2 à 4 phrases : état du droit ou difficulté pratique qui motive l\'article",\n'
            ' "a_retenir": ["6 à 10 enseignements essentiels, chacun une phrase complète et précise (règle, seuil, délai, texte)"],\n'
            ' "qui": "situations et personnes concernées, avec leurs limites",\n'
            ' "schema_titre": "titre court d\'un schéma de synthèse (procédure, chronologie ou arbre de décision), sinon vide",\n'
            ' "schema_etapes": ["3 à 8 étapes ou conditions successives, brèves ; liste vide s\'il n\'y a pas de séquence réelle"]}\n\n'
            f"NOTES PAR PARTIE :\n{resumes}\n\nINTERTITRES :\n{titres}")


def assemble_doc(doc, parts, red, total):
    secs = []
    for p in parts:
        for s in p["sections"]:
            if secs and s["titre"] and secs[-1]["titre"].lower() == s["titre"].lower():
                secs[-1]["points"] += s["points"]
            else:
                secs.append({"titre": s["titre"], "points": list(s["points"])})
    for s in secs:
        s["points"] = _dedupe(s["points"], 45)
    flat = lambda k: [x for p in parts for x in p[k]]
    jur, seen = [], set()
    for j in flat("jurisprudence"):
        k = re.sub(r"\W+", "", j["decision"].lower())
        if k not in seen:
            seen.add(k); jur.append(j)
    red = red or {}
    etapes = _lst(red.get("schema_etapes"), 8, 260) if red else []
    if len(etapes) < 3:
        etapes = _dedupe(flat("etapes"), 8)
    fiche = {"resume": str(red.get("resume") or " ".join(p["resume"] for p in parts if p.get("resume")))[:2600],
             "contexte": str(red.get("contexte", "")).strip()[:1200] or None,
             "a_retenir": _lst(red.get("a_retenir"), 12, 500), "qui": str(red.get("qui", "")).strip()[:800] or None,
             "sections": secs[:45], "jurisprudence": jur[:25], "tableaux": flat("tableaux")[:6],
             "a_verifier": _dedupe(flat("a_verifier"), 25), "vigilance": _dedupe(flat("vigilance"), 18),
             "references": _dedupe(flat("references"), 40),
             "schema": {"titre": str(red.get("schema_titre", "")).strip()[:140] or "Enchaînement", "etapes": etapes} if len(etapes) >= 3 else None,
             "parties": f"{len(parts)}/{total}", "provisoire": (not red) or len(parts) < total}
    return fiche


_FIG = re.compile(
    r"\d+(?:[,.]\d+)?\s?%"                                                         # taux
    r"|\d{1,3}(?:[\s\u00a0\u202f.]\d{3})+(?:,\d+)?\s?(?:€|euros?)|\d+(?:,\d+)?\s?(?:€|euros?)"        # montants
    r"|\b(?:art(?:icles?|\.)?|L\.|R\.|D\.)\s?[LRD]?\.?\s?\d{1,4}(?:-\d+)+"                 # articles à numérotation composée
    r"|\bn[°o]\s?\d{2}-\d{2}\.\d{3}\b|\b\d{2}-\d{2}\.\d{3}\b", re.I)                      # pourvois
_key = lambda t: re.sub(r"[^0-9a-z%€]", "", re.sub(r"(?<=\d),(\d*?)0+(?=\D|$)", lambda m: "," + m.group(1) if m.group(1) else "", re.sub(r"^n[°o]\s?", "", t.lower().strip())).replace("euros", "€").replace("euro", "€").replace("articles", "").replace("article", "").replace("art", ""))
_unused = lambda t: re.sub(r"[^0-9a-z%€]", "", t.lower().replace("euros", "€").replace("euro", "€").replace("articles", "").replace("article", "").replace("art", ""))


def coverage_check(source, fiche):
    """Contrôle déterministe (hors IA) : chaque chiffre, taux, article ou pourvoi du texte source figure-t-il dans la fiche,
    et la fiche contient-elle des chiffres absents du texte ? Sert à signaler au lecteur ce qu'il doit relire dans le PDF."""
    src_flat = re.sub(r"[^0-9a-z%€]", "", source.lower().replace("euros", "€").replace("euro", "€"))
    fic_txt = json.dumps({k: v for k, v in fiche.items() if k not in ("controle",)}, ensure_ascii=False)
    fic_flat = re.sub(r"[^0-9a-z%€]", "", fic_txt.lower().replace("euros", "€").replace("euro", "€"))
    src_tokens, seen = [], set()
    for m in _FIG.finditer(source):
        k = _key(m.group(0))
        if k and k not in seen and len(k) >= 2:
            seen.add(k); src_tokens.append((k, re.sub(r"\s+", " ", m.group(0)).strip()))
    missing = [raw for k, raw in src_tokens if k not in fic_flat]
    src_keys = {k for k, _ in src_tokens}
    sus, seen2 = [], set()
    for m in _FIG.finditer(fic_txt):
        k = _key(m.group(0))
        if k and k not in seen2 and len(k) >= 2 and k not in src_keys and k not in src_flat:
            seen2.add(k); sus.append(re.sub(r"\s+", " ", m.group(0)).strip())
    return {"total": len(src_tokens), "repris": len(src_tokens) - len(missing), "absents": missing[:25], "suspects": sus[:15]}


def cridon_gap_prompt(doc, chunk, absents):
    return ("Une fiche d'étude de l'article « " + doc["titre"] + " » (revue NOTA-BENE) omet des éléments précis du passage ci-dessous. "
            "Complète-la : pour CHAQUE élément de la liste, retrouve-le dans le passage et rédige un point complet et exact "
            "(règle, condition, montant, taux, texte ou décision concerné, avec son contexte). Reformule, n'invente rien ; "
            "si un élément n'est finalement pas pertinent, ignore-le. Réponds UNIQUEMENT par un objet JSON valide avec les clés "
            '{"resume_partiel": "", "sections": [{"titre": "Compléments", "points": ["..."]}], "jurisprudence": [], "tableaux": [], '
            '"a_verifier": [], "vigilance": [], "references": []}.\n'
            "ÉLÉMENTS OMIS : " + " ; ".join(absents[:25]) + "\n\nPASSAGE :\n" + chunk)


def cridon_process_doc(issue_id, doc, partials, deadline):
    """Analyse un article par parties ; reprend là où le run précédent s'est arrêté. Retourne la fiche (éventuellement provisoire) ou None."""
    chunks = split_chunks(doc["texte"])
    parts, complete = [], True
    for idx, ch in enumerate(chunks, 1):
        key = f"{doc['num']}:{idx}:{hashlib.sha1(ch.encode()).hexdigest()[:8]}"
        if key not in partials:
            if time.time() > deadline or all_ai_dead(True):
                complete = False; continue
            res = None
            for attempt in (1, 2):
                out = call_ai(cridon_chunk_prompt(doc, idx, len(chunks), ch), 3600, private=True)
                res = parse_chunk(out)
                if res or all_ai_dead(True):
                    break
                log(f"CRIDON : partie {idx}/{len(chunks)} de {doc['num']} illisible (essai {attempt})")
            if res:
                cc = coverage_check(ch, res)
                if cc["total"] and len(cc["absents"]) >= 3 and cc["repris"] / cc["total"] < 0.85 and not all_ai_dead(True) and time.time() <= deadline:
                    out2 = call_ai(cridon_gap_prompt(doc, ch, cc["absents"]), 2400, private=True)
                    add = parse_chunk(out2)
                    if add:
                        res["sections"] += [{"titre": x["titre"] or "Compléments", "points": x["points"]} for x in add["sections"]]
                        for k in ("jurisprudence", "tableaux", "a_verifier", "vigilance", "references"):
                            res[k] = res[k] + add[k]
                        log(f"CRIDON : fiche {doc['num']} partie {idx} complétée ({len(cc['absents'])} éléments chiffrés manquaient)")
                partials[key] = res
            else:
                complete = False; continue
            log(f"CRIDON : fiche {doc['num']} — partie {idx}/{len(chunks)} analysée")
        parts.append(partials[key])
    if not parts:
        return None
    rkey = f"red:{doc['num']}:{len(parts)}:{hashlib.sha1(json.dumps([p['resume'] for p in parts]).encode()).hexdigest()[:8]}"
    red = partials.get(rkey)
    if red is None and complete and not all_ai_dead(True) and time.time() <= deadline:
        out = call_ai(cridon_reduce_prompt(doc, parts), 2600, private=True)
        d = _json_from(out)
        if d and str(d.get("resume", "")).strip():
            red = partials[rkey] = d
    fiche = assemble_doc(doc, parts, red, len(chunks))
    fiche["controle"] = coverage_check(doc["texte"], fiche)
    return fiche


def cridon_run(cr, meta, deadline):
    """Met à jour `cr` (dict numéro -> NOTA-BENE). Retourne la liste des numéros nouvellement complétés."""
    user, pw = os.getenv("CRIDON_USER", "").strip(), os.getenv("CRIDON_PASS", "")
    if not (user and pw):
        log("CRIDON : identifiants non configurés (secrets CRIDON_USER / CRIDON_PASS) : module ignoré."); return []
    if not _providers(True):
        log("CRIDON : aucune IA autorisée pour du contenu privé (clé GROQ_API_KEY ou ANTHROPIC_API_KEY requise) : module ignoré."); return []
    fp = hashlib.sha256((user + "\0" + pw).encode()).hexdigest()[:16]
    if meta.get("cridon_fail") == fp:
        log("CRIDON : ces identifiants ont déjà été refusés ; pas de nouvel essai pour éviter un blocage du compte. "
            "Corrigez les secrets CRIDON_USER / CRIDON_PASS pour réessayer."); return []
    s = requests.Session(); s.headers.update(UA_WEB)
    ok, rejected = cridon_login(s, user, pw)
    if not ok:
        if rejected:
            meta["cridon_fail"] = fp
            log("CRIDON : connexion refusée. Vérifiez identifiant et mot de passe (aucune nouvelle tentative tant que les secrets ne changent pas).")
        return []
    meta.pop("cridon_fail", None)
    cands = cridon_list_pdfs(s)
    maxn = int(os.getenv("CRIDON_MAX_ISSUES", "3"))
    todo = [c for c in cands if not (c["num"] and cr.get(str(c["num"]), {}).get("complete"))][:maxn]
    done_now = []
    partials_all = meta.setdefault("cridon_partial", {})
    tmpdir = os.path.join(os.getenv("RUNNER_TEMP", "/tmp"), "nb_pdf"); os.makedirs(tmpdir, exist_ok=True)
    for c in todo:
        if time.time() > deadline or all_ai_dead(True):
            log("CRIDON : temps ou quota atteint, reprise au prochain lancement."); break
        pdf = os.path.join(tmpdir, f"nb_{abs(hash(c['url'])) % 10**8}.pdf")
        if not cridon_download(s, c["url"], pdf):
            continue
        try:
            info = read_issue_pdf(pdf)
        except Exception as e:
            log("CRIDON : PDF illisible", e); continue
        finally:
            try: os.remove(pdf)
            except OSError: pass
        num = info.get("numero") or c["num"]
        if not num or not info["docs"]:
            log("CRIDON : structure du PDF non reconnue (numéro ou fiches introuvables)."); continue
        key = str(num)
        label = f"{info['mois'].capitalize()} {info['annee']}" if info.get("mois") else f"n°{num}"
        iss = cr.setdefault(key, {"id": f"CRIDON-{num}", "numero": num, "label": label, "first_seen": str(TODAY), "docs": []})
        iss["label"] = label
        was_complete = bool(iss.get("complete"))
        known = {d["num"]: d for d in iss["docs"]}
        log(f"CRIDON : NOTA-BENE n°{num} ({label}) — {len(info['docs'])} article(s) : " + ", ".join(d['num'] for d in info["docs"]))
        partials = partials_all.setdefault(iss["id"], {})
        for d in info["docs"]:
            rec = known.get(d["num"]) or {"num": d["num"], "rubrique": d["rubrique"], "titre": d["titre"], "auteur": d["auteur"]}
            rec.update({"rubrique": d["rubrique"], "titre": d["titre"], "auteur": d["auteur"], "chars": len(d["texte"])})
            if not (rec.get("fiche") and not rec["fiche"].get("provisoire") and rec.get("v") == CRIDON_VERSION):
                f = cridon_process_doc(iss["id"], d, partials, deadline)
                if f:
                    rec["fiche"], rec["v"] = f, CRIDON_VERSION
            known[d["num"]] = rec
        iss["docs"] = [known[d["num"]] for d in info["docs"]]
        iss["complete"] = all(r.get("fiche") and not r["fiche"].get("provisoire") for r in iss["docs"])
        if iss["complete"]:
            partials_all.pop(iss["id"], None)
            if not was_complete:
                done_now.append(key)
            log(f"CRIDON : NOTA-BENE n°{num} complet.")
    return done_now


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
# Sorties : notification, page statique, données chiffrées
# --------------------------------------------------------------------------------------------------
def notify(new_items, new_issues):
    topic = os.getenv("NTFY_TOPIC")
    if not topic or not (new_items or new_issues):
        return
    lines = [f"• NOTA-BENE du CRIDON n°{n} disponible" for n in new_issues]          # contenu CRIDON : jamais détaillé ici
    top = sorted(new_items, key=lambda i: i["date"], reverse=True)[:5]
    lines += [f"• {i['title'][:90]}" for i in top]
    if len(new_items) > 5:
        lines.append(f"… et {len(new_items) - 5} de plus")
    try:
        requests.post(f"https://ntfy.sh/{topic}", timeout=20, data="\n".join(lines).encode("utf-8"),
                      headers={"Title": f"Veille notariale : {len(new_items) + len(new_issues)} nouveauté(s)".encode("utf-8"),
                               "Tags": "scales"})
    except Exception as e:
        log("Notification ntfy", e)


def write_shell():
    """Page statique (sans aucune donnée) : identique à chaque run, donc jamais re-commitée inutilement."""
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(PAGE)
    for old in ("items.json", "digest.json", "feed.xml"):                           # anciens fichiers en clair : supprimés
        p = os.path.join(DOCS, old)
        if os.path.exists(p):
            os.remove(p); log("Ancien fichier public supprimé :", old)


# --------------------------------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------------------------------
def main():
    os.makedirs(DOCS, exist_ok=True)
    pw = os.getenv("DATA_PASSPHRASE", "")
    if len(pw) < 12:
        log("ERREUR : le secret DATA_PASSPHRASE est absent ou trop court (12 caractères minimum). Rien n'est publié.")
        sys.exit(1)
    payload = load_payload(pw) or {}
    # migration éventuelle d'un ancien items.json en clair
    legacy = os.path.join(DOCS, "items.json")
    if not payload.get("items") and os.path.exists(legacy):
        try:
            payload["items"] = json.load(open(legacy, encoding="utf-8"))
        except Exception:
            pass
    store = {i["id"]: i for i in payload.get("items", [])}
    cr = {str(k): v for k, v in (payload.get("cridon") or {}).items()} if isinstance(payload.get("cridon"), dict) else \
         {str(i["numero"]): i for i in (payload.get("cridon") or [])}
    meta = payload.get("meta") or {}

    kept = {k: i for k, i in store.items() if str(i.get("source", "")).startswith(ALLOWED_SOURCES)}
    if len(kept) != len(store):
        log(f"Nettoyage : {len(store) - len(kept)} élément(s) d'anciennes sources retiré(s).")
    store = kept
    for i in store.values():
        i["abstract"] = cut_teaser(i.get("abstract", ""))
        i.pop("type", None); i.pop("bulletin", None)
        if i.get("ai_v") != AI_VERSION:
            i.pop("ai", None); i.pop("ai_v", None)

    # 1) CRIDON en priorité : c'est la source la plus riche et il ne faut pas manquer de quota pour elle
    new_issues = []
    try:
        new_issues = cridon_run(cr, meta, time.time() + int(os.getenv("CRIDON_TIME_BUDGET_S", "2700")))
    except Exception as e:
        log("CRIDON : erreur inattendue (le reste de la veille continue) :", repr(e)[:300])

    # 2) Sources publiques
    cutoff = (TODAY - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    added_items = []
    for it in src_rss() + src_sepaj() + src_legalnews():
        if it["id"] in store or it["date"] < cutoff or not it["title"]:
            continue
        it["matieres"] = classify(it["title"] + " " + it.get("abstract", ""), it.get("url", ""))
        it["first_seen"] = str(TODAY)
        arts = extract_articles(it["title"] + " " + it.get("abstract", ""))
        if arts:
            it["articles"] = arts
        store[it["id"]] = it
        added_items.append(it)
    store = {k: i for k, i in store.items() if i["date"] >= (TODAY - dt.timedelta(days=max(LOOKBACK_DAYS, 120))).isoformat()}

    items = sorted(store.values(), key=lambda i: (i["date"], i["first_seen"]), reverse=True)
    digest = weekly_digest(items) or (payload.get("digest") or {}).get("by_matiere")

    # 3) Fiches des actualités publiques
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
        if not _providers():
            log("Aucune clé IA configurée : pas de fiches."); break
        if all_ai_dead():
            log("Tous les fournisseurs IA sont à quota ou en erreur : arrêt immédiat des fiches pour ce run."); break
        fiche, status = make_fiche(it)
        if status == "ok":
            it["ai"], it["ai_v"] = fiche, AI_VERSION; done += 1
        elif status == "vide":
            it["ai"], it["ai_v"] = None, AI_VERSION
        else:
            it["ai_tries"] = it.get("ai_tries", 0) + (0 if all_ai_dead() else 1)

    items = sorted(store.values(), key=lambda i: (i["date"], i["first_seen"]), reverse=True)
    issues = sorted(cr.values(), key=lambda x: -int(x["numero"]))
    out = {"v": 4, "updated": dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y %H:%M UTC"), "updated_day": str(TODAY),
           "items": items, "cridon": issues, "digest": {"date": str(TODAY), "by_matiere": digest or {}}, "meta": meta}
    # la date de mise à jour ne doit pas, à elle seule, provoquer une réécriture : on la compare hors champ "updated"
    prev = payload and {k: v for k, v in payload.items() if k not in ("updated",)}
    cur = json.loads(json.dumps({k: v for k, v in out.items() if k != "updated"}, ensure_ascii=False))
    if prev == cur:
        log("Données inchangées : data.enc conservé.")
    else:
        save_payload(out, pw)
    write_shell()
    notify(added_items, new_issues)
    log(f"{len(added_items)} nouveaux éléments, {len(items)} au total, {done} fiche(s) générée(s), "
        f"{len(issues)} NOTA-BENE, dont {len(new_issues)} nouvellement complété(s).")


PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Veille juridique notariale</title><meta name="robots" content="noindex,nofollow">
<link rel="manifest" href="manifest.json"><meta name="theme-color" content="#14213d">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Veille notariale"><link rel="apple-touch-icon" href="icon.png">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f2f0e9;--fg:#1d2433;--card:#fff;--mut:#667085;--bd:#e3e0d3;--navy:#14213d;--navy2:#26426b;--gold:#b8893b;--nd:#2f7a5b;--sp:#26426b;--ln:#8c2f39;--cr:#6b3fa0;--ring:#b8893b55;--side:#181f30;--soft:rgba(38,66,107,.06)}
:root[data-theme="dark"]{--bg:#0e1320;--fg:#e9ecf3;--card:#171f33;--mut:#9aa3b5;--bd:#28324a;--gold:#d9a95a;--nd:#6fcfa3;--sp:#7fa6e0;--ln:#e58a94;--cr:#b79af0;--ring:#d9a95a55;--side:#0b0f1a;--soft:rgba(127,166,224,.08)}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1320;--fg:#e9ecf3;--card:#171f33;--mut:#9aa3b5;--bd:#28324a;--gold:#d9a95a;--nd:#6fcfa3;--sp:#7fa6e0;--ln:#e58a94;--cr:#b79af0;--ring:#d9a95a55;--side:#0b0f1a;--soft:rgba(127,166,224,.08)}}
*{box-sizing:border-box}html{scroll-padding-top:70px}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 Inter,system-ui,sans-serif;overflow-x:hidden}
a{color:inherit}button{font-family:inherit}[hidden]{display:none!important}
/* verrou */
#lock{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;background:linear-gradient(160deg,var(--navy),var(--navy2))}
.lockbox{background:var(--card);border-radius:18px;padding:28px 26px;max-width:380px;width:100%;box-shadow:0 18px 50px rgba(0,0,0,.3);text-align:center}
.lockbox .mark{margin:0 auto 12px;width:46px;height:46px;font-size:22px}
.lockbox h1{font:700 22px 'Playfair Display',Georgia,serif;margin:0 0 4px}.lockbox p{font-size:13px;color:var(--mut);margin:0 0 16px}
.lockbox input[type=password]{width:100%;padding:12px 14px;border:1px solid var(--bd);border-radius:10px;font:16px Inter;background:var(--bg);color:var(--fg);outline:none}
.lockbox input:focus{border-color:var(--gold);box-shadow:0 0 0 3px var(--ring)}
.lockbox .rem{display:flex;gap:8px;align-items:center;justify-content:center;font-size:12.5px;color:var(--mut);margin:12px 0}
.lockbox button{width:100%;padding:12px;border:none;border-radius:10px;background:var(--navy);color:#fff;font:700 14px Inter;cursor:pointer}
#lockerr{color:#b42318;font-size:13px;min-height:20px;margin-top:10px}
.mark{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,var(--gold),#8a5f22);display:flex;align-items:center;justify-content:center;font:700 17px 'Playfair Display',Georgia,serif;color:#1d1400;flex:none}
/* structure */
.shell{display:flex;min-height:100vh}
.sidebar{width:272px;flex:none;background:var(--side);color:#dfe4ee;position:sticky;top:0;height:100vh;overflow-y:auto;padding:18px 14px calc(18px + env(safe-area-inset-bottom,0px));z-index:40}
.brand{display:flex;align-items:center;gap:10px;margin:0 4px 16px}
.brand b{font:700 15px 'Playfair Display',Georgia,serif;display:block;line-height:1.1}.brand small{font:500 10px Inter;color:#8b93a8;letter-spacing:.05em;text-transform:uppercase}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:6px}
.seg button{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);color:#c7cede;padding:9px 6px;border-radius:9px;cursor:pointer;font:700 12px Inter}
.seg button.on{background:linear-gradient(135deg,var(--gold),#8a5f22);color:#1d1400;border-color:transparent}
.navlbl{font:700 10px Inter;letter-spacing:.1em;text-transform:uppercase;color:#6b7690;margin:16px 6px 6px}
.navitem{display:flex;width:100%;align-items:center;gap:8px;text-align:left;background:none;border:none;color:#c7cede;padding:7px 8px;border-radius:8px;cursor:pointer;font:600 13px Inter}
.navitem:hover{background:rgba(255,255,255,.06)}.navitem.on{background:rgba(255,255,255,.14);color:#fff}
.navitem .n{margin-left:auto;font:700 11px Inter;opacity:.65}.navitem .dot{width:8px;height:8px;border-radius:99px;flex:none}
.sbtn{margin-top:10px;width:100%;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:#c7cede;padding:8px;border-radius:9px;cursor:pointer;font:600 12px Inter}
.backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:35}.backdrop.show{display:block}
.content{flex:1;min-width:0}
.topbar{position:sticky;top:0;z-index:30;background:var(--bg);border-bottom:1px solid var(--bd);padding:10px 20px;display:flex;gap:10px;align-items:center}
.burger{display:none;background:none;border:1px solid var(--bd);border-radius:8px;padding:8px 10px;cursor:pointer;color:var(--fg);font-size:15px}
.search{position:relative;flex:1;max-width:460px}
.search input{width:100%;padding:10px 13px;border:1px solid var(--bd);border-radius:10px;font:14.5px Inter,system-ui;background:var(--card);color:var(--fg);outline:none}
.search input:focus{border-color:var(--gold);box-shadow:0 0 0 3px var(--ring)}
.tbtn{flex:none;border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:9px;padding:7px 11px;font:600 12px Inter;cursor:pointer;white-space:nowrap}
main{max-width:1000px;padding:18px 20px 34px}
/* brief */
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
.s-nd{--c:var(--nd)}.s-sp{--c:var(--sp)}.s-ln{--c:var(--ln)}.s-cr{--c:var(--cr)}
.top{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;margin-bottom:7px}
.badge{font:700 9.5px Inter;text-transform:uppercase;letter-spacing:.06em;padding:2px 9px;border-radius:99px;color:var(--c);border:1px solid var(--c)}
.badge.new{background:var(--gold);border-color:var(--gold);color:#14213d}
.badge.part{color:var(--mut);border-color:var(--bd);text-transform:none;letter-spacing:0;font-weight:600}
.date{margin-left:auto;font-size:11.5px;color:var(--mut);font-weight:600}
.card a.t,.card .t{display:block;font:700 18px/1.3 'Playfair Display',Georgia,serif;text-decoration:none;cursor:pointer}.card a.t:hover,.card .t:hover{color:var(--gold)}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}
.tag{font-size:10.5px;font-weight:600;padding:2px 9px;border-radius:99px;background:rgba(184,137,59,.13);border:1px solid rgba(184,137,59,.35)}
.abs{font-size:14px;margin:10px 0 0;color:var(--mut)}.resume{font-size:14.5px;margin:12px 0 0}
.aitag{display:inline-block;font:700 9.5px Inter;letter-spacing:.06em;text-transform:uppercase;color:var(--nd);background:rgba(47,122,91,.13);padding:2px 8px;border-radius:6px;margin-top:12px}
details.fiche{margin-top:10px;border:1px solid var(--bd);border-radius:11px;background:var(--soft)}
details.fiche>summary{cursor:pointer;list-style:none;padding:10px 14px;font:700 13px Inter;display:flex;justify-content:space-between;align-items:center}
details.fiche>summary::-webkit-details-marker{display:none}
details.fiche>summary:after{content:"Déplier ▾";font:600 11.5px Inter;color:var(--gold)}details.fiche[open]>summary:after{content:"Replier ▴"}
.fbody{padding:2px 16px 14px}
.sec{margin-top:16px}.sec h4{margin:0 0 6px;font:700 11px Inter;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}
.sec p{margin:0;font-size:14.5px}.sec ul,.sec ol{margin:0;padding-left:20px}.sec li{font-size:14.5px;margin:5px 0}
.chk{list-style:none;padding:0!important}.chk li{position:relative;padding-left:26px}.chk li:before{content:"☐";position:absolute;left:0;color:var(--gold);font-size:15px;line-height:1.4}
.ctl{font-size:12.5px;color:var(--mut)}.ctl p{margin:4px 0}
.vig{margin-top:14px;padding:11px 13px;border-radius:9px;background:rgba(184,137,59,.12);border-left:3px solid var(--gold);font-size:13.5px}
.vig b{display:block;color:var(--gold);font:700 10.5px Inter;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px}
.flow{margin-top:4px}.step{display:flex;gap:10px;align-items:flex-start;position:relative;padding-bottom:12px}
.step:not(:last-child):before{content:"";position:absolute;left:12px;top:26px;bottom:0;width:2px;background:var(--bd)}
.step .n{flex:none;width:26px;height:26px;border-radius:99px;background:var(--navy2);color:#fff;font:700 12px Inter;display:flex;align-items:center;justify-content:center}
:root[data-theme="dark"] .step .n{background:var(--gold);color:#14213d}
.step div{font-size:14px;padding-top:2px}
.refs{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.ref{font:600 11px Inter;padding:3px 9px;border-radius:7px;background:var(--bg);border:1px solid var(--bd);color:var(--mut)}
.src{display:inline-block;margin-top:12px;font:700 12.5px Inter;color:var(--gold);text-decoration:none}
.note{font-size:12px;color:var(--mut);margin-top:8px}
.openbtn{display:inline-block;margin-top:12px;padding:8px 14px;border-radius:9px;background:var(--navy);color:#fff;font:700 12.5px Inter;border:none;cursor:pointer}
:root[data-theme="dark"] .openbtn{background:var(--gold);color:#14213d}
/* lecteur NOTA-BENE */
.reader{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:22px 26px;border-left:5px solid var(--cr)}
.reader h1{font:700 26px/1.25 'Playfair Display',Georgia,serif;margin:6px 0 4px}
.reader .meta{font-size:13px;color:var(--mut)}
.toc{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 4px}.toc a{font:600 12px Inter;text-decoration:none;padding:4px 11px;border-radius:99px;border:1px solid var(--bd);background:var(--soft)}
.bigsum{margin-top:16px;padding:14px 16px;border-radius:12px;background:var(--soft);border:1px solid var(--bd);font-size:15px}
.reader h2{font:700 19px 'Playfair Display',Georgia,serif;margin:28px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--bd)}
.reader h3{font:700 15px Inter;margin:18px 0 4px;color:var(--navy2)}:root[data-theme="dark"] .reader h3{color:var(--gold)}
.reader ul,.reader ol{padding-left:22px;margin:4px 0}.reader li{margin:6px 0}
.tw{overflow-x:auto;margin:8px 0;border:1px solid var(--bd);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:480px}
th{background:var(--navy);color:#fff;text-align:left;padding:8px 10px;font:700 12px Inter}:root[data-theme="dark"] th{background:#26324d}
td{padding:8px 10px;border-top:1px solid var(--bd);vertical-align:top}tr:nth-child(even) td{background:var(--soft)}
.todo{list-style:none;padding:0!important}.todo li{display:flex;gap:10px;align-items:flex-start;margin:8px 0}.todo input{margin-top:5px;width:17px;height:17px;accent-color:var(--gold);flex:none}
.todo input:checked+span{text-decoration:line-through;opacity:.55}
.banner{margin-top:14px;padding:10px 13px;border-radius:9px;background:rgba(184,137,59,.14);border:1px solid rgba(184,137,59,.4);font-size:13px}
.rtools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.issuehead{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;margin:0 2px 14px}.issuehead h2{font:700 22px 'Playfair Display',Georgia,serif;margin:0}
footer{padding:16px 20px calc(24px + env(safe-area-inset-bottom,0px));font-size:11.5px;color:var(--mut);border-top:1px solid var(--bd);max-width:1000px}
@media(max-width:860px){.sidebar{position:fixed;left:0;top:0;transform:translateX(-100%);transition:transform .2s;width:82vw;max-width:310px}.sidebar.open{transform:none}.burger{display:inline-block}main{padding:14px}.reader{padding:16px 15px}.reader h1{font-size:22px}}
@media print{.sidebar,.topbar,.rtools,footer,.backdrop{display:none!important}body{background:#fff;color:#000}.reader{border:none;padding:0}.shell{display:block}main{max-width:none;padding:0}th{background:#ddd!important;color:#000!important}}
</style></head><body>
<div id="lock"><form class="lockbox" id="lockform" autocomplete="on"><div class="mark">§</div><h1>Veille notariale</h1><p>Espace privé. Saisissez votre phrase secrète pour déchiffrer les données.</p>
<input type="text" name="username" autocomplete="username" value="veille" hidden><input type="password" id="pass" autocomplete="current-password" placeholder="Phrase secrète" required>
<label class="rem"><input type="checkbox" id="rem" checked> Rester connecté sur cet appareil</label><button type="submit" id="lockbtn">Déverrouiller</button><div id="lockerr"></div></form></div>
<div class="shell" id="app" hidden><div class="backdrop" id="bd"></div>
<aside class="sidebar" id="sb">
 <div class="brand"><div class="mark">§</div><div><b>Veille notariale</b><small>Actualité juridique</small></div></div>
 <div class="seg"><button id="vC">NOTA-BENE</button><button id="vA">Actualités</button></div>
 <div id="nav"></div>
 <button class="sbtn" id="themebtn">🌗 <span id="themelbl">Thème auto</span></button>
 <button class="sbtn" id="lockbtn2">🔒 Verrouiller</button>
</aside>
<div class="content">
 <div class="topbar"><button class="burger" id="burger">☰</button><div class="search"><input id="q" placeholder="Rechercher : donation, bail, indivision, article 784…"></div><button class="tbtn" id="expand">Tout déplier</button></div>
 <main id="main"></main>
 <footer id="foot"></footer>
</div></div>
<script>
/*DEC_START*/
async function decryptBlob(blob,pass){
 const b=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
 const km=await crypto.subtle.importKey("raw",new TextEncoder().encode(pass),"PBKDF2",false,["deriveKey"]);
 const key=await crypto.subtle.deriveKey({name:"PBKDF2",salt:b(blob.salt),iterations:blob.it||600000,hash:"SHA-256"},km,{name:"AES-GCM",length:256},false,["decrypt"]);
 const plain=await crypto.subtle.decrypt({name:"AES-GCM",iv:b(blob.iv),additionalData:new TextEncoder().encode("veille-v1")},key,b(blob.ct));
 const ds=new Blob([plain]).stream().pipeThrough(new DecompressionStream("deflate"));
 return JSON.parse(await new Response(ds).text());
}
/*DEC_END*/
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let DB=null,view="cridon",src="",mat="",allOpen=false,cur=null,openDoc=null;
const CLS={"Notaires de France":"nd","SEPAJ":"sp","LegalNews":"ln"},COL={nd:"var(--nd)",sp:"var(--sp)",ln:"var(--ln)"};
const ls={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}},del(k){try{localStorage.removeItem(k)}catch(e){}}};
function fd(d){try{return new Date(d).toLocaleDateString("fr-FR",{day:"numeric",month:"short",year:"numeric"})}catch(e){return d}}
/* thème */
let theme=ls.get("veille-theme")||"auto";const TL={auto:"Thème auto",light:"Thème clair",dark:"Thème sombre"};
function applyTheme(){document.documentElement.setAttribute("data-theme",theme==="auto"?"":theme);$("themelbl").textContent=TL[theme]}
$("themebtn").onclick=()=>{theme={auto:"light",light:"dark",dark:"auto"}[theme];ls.set("veille-theme",theme);applyTheme()};applyTheme();
$("burger").onclick=()=>{$("sb").classList.add("open");$("bd").classList.add("show")};
function closeSb(){$("sb").classList.remove("open");$("bd").classList.remove("show")}$("bd").onclick=closeSb;
/* verrou */
async function unlock(pass,remember){
 let blob;
 if(window.__PREVIEW__){DB=window.__PREVIEW__}else{
  const r=await fetch("data.enc?t="+Date.now(),{cache:"no-store"});
  if(!r.ok)throw new Error("NODATA");blob=await r.json();
  try{DB=await decryptBlob(blob,pass)}catch(e){throw new Error("BADPASS")}
  if(remember)ls.set("veille-pass",pass);
 }
 $("lock").hidden=true;$("app").hidden=false;start();
}
$("lockform").onsubmit=async e=>{e.preventDefault();$("lockerr").textContent="Déchiffrement…";$("lockbtn").disabled=true;
 try{await unlock($("pass").value,$("rem").checked)}catch(x){$("lockerr").textContent=x.message==="NODATA"?"Aucune donnée publiée pour l'instant : lancez la mise à jour (Actions) une première fois.":"Phrase secrète incorrecte."}
 $("lockbtn").disabled=false};
$("lockbtn2").onclick=()=>{ls.del("veille-pass");location.reload()};
/* démarrage */
function start(){
 if(!(DB.cridon||[]).length)view="actus";
 $("foot").textContent="Mise à jour "+(DB.updated||"")+". Données chiffrées de bout en bout ; les fiches sont des synthèses reformulées par IA (NOTA-BENE du CRIDON : usage personnel de l'abonné ; Notaires de France, SEPAJ, LegalNews : titres et extraits publics). Elles peuvent comporter des erreurs et ne remplacent jamais la lecture de la source : vérifiez avant tout usage professionnel.";
 $("vC").onclick=()=>{view="cridon";cur=null;openDoc=null;render()};$("vA").onclick=()=>{view="actus";render()};
 $("q").oninput=render;
 $("expand").onclick=()=>{allOpen=!allOpen;document.querySelectorAll("details.fiche").forEach(d=>d.open=allOpen);$("expand").textContent=allOpen?"Tout replier":"Tout déplier"};
 window.onhashchange=route;route();
}
function route(){const m=location.hash.match(/^#\/n\/(\d+)(?:\/(\w+))?/);
 if(m){view="cridon";cur=m[1];openDoc=m[2]||null}else if(location.hash==="#/a"){view="actus";openDoc=null}
 render();scrollTo(0,0)}
function render(){$("vC").classList.toggle("on",view==="cridon");$("vA").classList.toggle("on",view==="actus");$("vC").hidden=!(DB.cridon||[]).length;
 view==="cridon"?renderCridon():renderActus()}
/* ===== NOTA-BENE ===== */
const ul=(a,c)=>a&&a.length?`<ul class="${c||""}">${a.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:"";
function flow(s){return s&&s.etapes&&s.etapes.length>=3?`<div class="flow">${s.etapes.map((t,k)=>`<div class="step"><span class="n">${k+1}</span><div>${esc(t)}</div></div>`).join("")}</div>`:""}
function table(t){return `<h3>${esc(t.titre||"Tableau comparatif")}</h3><div class="tw"><table><thead><tr>${t.colonnes.map(c=>`<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${t.lignes.map(r=>`<tr>${r.map(c=>`<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`}
function renderCridon(){
 const is=DB.cridon||[];
 if(!cur||!is.find(i=>String(i.numero)===String(cur)))cur=is.length?String(is[0].numero):null;
 $("nav").innerHTML=`<div class="navlbl">Numéros</div>`+is.map(i=>`<button class="navitem ${String(i.numero)===String(cur)?"on":""}" data-n="${i.numero}"><span class="dot" style="background:var(--cr)"></span>n°${i.numero} · ${esc(i.label)}<span class="n">${i.docs.length}</span></button>`).join("");
 document.querySelectorAll("#nav .navitem").forEach(b=>b.onclick=()=>{location.hash="#/n/"+b.dataset.n;closeSb()});
 const iss=is.find(i=>String(i.numero)===String(cur));if(!iss){$("main").innerHTML='<p style="color:var(--mut)">Aucun NOTA-BENE disponible pour l\'instant.</p>';return}
 const d=openDoc&&iss.docs.find(x=>x.num===openDoc);
 $("main").innerHTML=d?reader(iss,d):issueView(iss);
 document.querySelectorAll(".todo input").forEach(c=>{const k=c.dataset.k;c.checked=ls.get(k)==="1";c.onchange=()=>ls.set(k,c.checked?"1":"0")});
 const pb=$("pbtn");if(pb)pb.onclick=()=>print();
}
function issueView(iss){
 const q=$("q").value.toLowerCase();
 const docs=iss.docs.filter(d=>!q||JSON.stringify(d).toLowerCase().includes(q));
 return `<div class="issuehead"><h2>NOTA-BENE n°${iss.numero}</h2><span class="note">${esc(iss.label)} · ${iss.docs.length} article(s)${iss.complete?"":" · analyse en cours, complétée au prochain passage"}</span></div>`+
 docs.map(d=>{const f=d.fiche;return `<article class="card s-cr" style="margin-bottom:14px"><div class="top"><span class="badge">${esc(d.rubrique||"NOTA-BENE")}</span>${d.num!=="ED"?`<span class="badge part">Fiche ${esc(d.num)}</span>`:""}${f&&f.provisoire?'<span class="badge part">En cours</span>':""}</div>
 <a class="t" href="#/n/${iss.numero}/${esc(d.num)}">${esc(d.titre)}</a>${d.auteur?`<div class="note">${esc(d.auteur)}</div>`:""}
 ${f?`<p class="resume">${esc(f.resume)}</p>${f.a_retenir&&f.a_retenir.length?`<div class="sec"><h4>À retenir</h4><ol>${f.a_retenir.slice(0,4).map(x=>`<li>${esc(x)}</li>`).join("")}</ol></div>`:""}<a class="openbtn" href="#/n/${iss.numero}/${esc(d.num)}">Lire la fiche complète →</a>`:`<p class="abs">Analyse en attente (quota IA ou temps du jour atteint) : elle reprendra automatiquement au prochain passage.</p>`}
 </article>`}).join("")||'<p style="color:var(--mut)">Aucun résultat.</p>';
}
function reader(iss,d){
 const f=d.fiche;if(!f)return `<a class="tbtn" href="#/n/${iss.numero}">← Retour</a><p>Analyse en attente.</p>`;
 const key=`chk:${iss.id}:${d.num}:`;
 const toc=[["Synthèse","s"],f.schema?["Schéma","sc"]:0,["Analyse détaillée","an"],(f.tableaux||[]).length?["Tableaux","tb"]:0,(f.jurisprudence||[]).length?["Jurisprudence","ju"]:0,(f.a_verifier||[]).length?["À vérifier","av"]:0,(f.vigilance||[]).length?["Vigilance","vi"]:0,(f.references||[]).length?["Références","re"]:0].filter(Boolean);
 return `<div class="rtools"><a class="tbtn" href="#/n/${iss.numero}" style="text-decoration:none">← NOTA-BENE n°${iss.numero}</a><button class="tbtn" id="pbtn">🖨 Imprimer / PDF</button></div>
 <article class="reader"><span class="badge s-cr" style="--c:var(--cr);display:inline-block">${esc(d.rubrique||"")}</span>
 <h1>${esc(d.titre)}</h1><div class="meta">${d.auteur?esc(d.auteur)+" · ":""}NOTA-BENE n°${iss.numero}, ${esc(iss.label)}${d.num!=="ED"?" · fiche "+esc(d.num):""}</div>
 ${f.provisoire?`<div class="banner">Fiche en cours de finalisation (${esc(f.parties)} parties analysées). Elle sera complétée automatiquement au prochain passage.</div>`:""}
 <div class="toc">${toc.map(t=>`<a href="#" onclick="document.getElementById('${t[1]}').scrollIntoView({behavior:'smooth'});return false">${t[0]}</a>`).join("")}</div>
 <h2 id="s">Synthèse</h2><div class="bigsum">${esc(f.resume)}</div>
 ${f.contexte?`<div class="sec"><h4>Contexte</h4><p>${esc(f.contexte)}</p></div>`:""}
 ${(f.a_retenir||[]).length?`<div class="sec"><h4>À retenir</h4><ol>${f.a_retenir.map(x=>`<li>${esc(x)}</li>`).join("")}</ol></div>`:""}
 ${f.qui?`<div class="sec"><h4>Situations concernées</h4><p>${esc(f.qui)}</p></div>`:""}
 ${f.schema?`<h2 id="sc">Schéma — ${esc(f.schema.titre)}</h2>${flow(f.schema)}`:""}
 <h2 id="an">Analyse détaillée</h2>${(f.sections||[]).map(s=>`${s.titre?`<h3>${esc(s.titre)}</h3>`:""}${ul(s.points)}`).join("")}
 ${(f.tableaux||[]).length?`<h2 id="tb">Tableaux</h2>${f.tableaux.map(table).join("")}`:""}
 ${(f.jurisprudence||[]).length?`<h2 id="ju">Jurisprudence citée</h2><div class="tw"><table><thead><tr><th>Décision</th><th>Solution retenue</th></tr></thead><tbody>${f.jurisprudence.map(j=>`<tr><td>${esc(j.decision)}</td><td>${esc(j.solution)}</td></tr>`).join("")}</tbody></table></div>`:""}
 ${(f.a_verifier||[]).length?`<h2 id="av">À vérifier dans la pratique</h2><ul class="todo">${f.a_verifier.map((x,k)=>`<li><input type="checkbox" data-k="${esc(key+k)}"><span>${esc(x)}</span></li>`).join("")}</ul>`:""}
 ${(f.vigilance||[]).length?`<h2 id="vi">Points de vigilance</h2><div class="vig"><b>⚠ À garder en tête</b>${ul(f.vigilance)}</div>`:""}
 ${(f.references||[]).length?`<h2 id="re">Références citées</h2><div class="refs">${f.references.map(r=>`<span class="ref">${esc(r)}</span>`).join("")}</div>`:""}
 ${f.controle&&f.controle.total?`<div class="sec ctl"><h4>Contrôle automatique des chiffres et références</h4><p>${f.controle.repris} élément(s) chiffré(s) ou référence(s) sur ${f.controle.total} relevés dans l'article figurent dans cette fiche (taux, montants, articles à numérotation composée, pourvois).</p>${(f.controle.absents||[]).length?`<p><b>Relevés dans l'article mais absents de la fiche — à relire dans le PDF :</b> ${f.controle.absents.map(esc).join(" · ")}</p>`:""}${(f.controle.suspects||[]).length?`<p><b>Présents dans la fiche mais introuvables dans l'article — à vérifier :</b> ${f.controle.suspects.map(esc).join(" · ")}</p>`:""}</div>`:""}
 <p class="note" style="margin-top:26px">Fiche reformulée par IA à partir du NOTA-BENE n°${iss.numero} du CRIDON Sud-Ouest, pour l'usage personnel de l'abonné. Elle peut comporter des erreurs ou omissions : l'article original (sur le site du CRIDON) fait foi.</p></article>`;
}
/* ===== Actualités ===== */
function group(i){const d=new Date(i.date),t=new Date();t.setHours(0,0,0,0);const n=Math.round((t-new Date(d.getFullYear(),d.getMonth(),d.getDate()))/864e5);
 return n<=0?"Aujourd'hui":n===1?"Hier":n<=7?"Cette semaine":n<=31?"Ce mois-ci":"Plus ancien"}
function sec(t,body){return body?`<div class="sec"><h4>${t}</h4>${body}</div>`:""}
function fiche(i){const a=i.ai;if(!a)return"";
 const refs=(a.references&&a.references.length?a.references:(i.articles||[]));
 return `<details class="fiche" ${allOpen?"open":""}><summary>Fiche détaillée</summary><div class="fbody">
  ${sec("Contexte",a.contexte?`<p>${esc(a.contexte)}</p>`:"")}${sec("Ce qu'il faut retenir",a.points&&a.points.length?`<ol>${a.points.map(x=>`<li>${esc(x)}</li>`).join("")}</ol>`:"")}
  ${sec("Règles et conditions",ul(a.regles))}${sec("Qui est concerné",a.qui?`<p>${esc(a.qui)}</p>`:"")}${sec("À vérifier dans la pratique",ul(a.a_verifier,"chk"))}
  ${a.vigilance?`<div class="vig"><b>⚠ Vigilance</b>${esc(a.vigilance)}</div>`:""}
  ${a.schema&&a.schema.etapes&&a.schema.etapes.length?sec("Schéma — "+esc(a.schema.titre||"enchaînement"),flow(a.schema)):""}
  ${refs&&refs.length?sec("Références citées",`<div class="refs">${refs.map(r=>`<span class="ref">${esc(r)}</span>`).join("")}</div>`):""}
  ${a.partielle?`<p class="note">Source partielle : seul l'extrait public a pu être exploité. Cette fiche ne couvre donc pas tout l'article.</p>`:""}</div></details>`}
function renderActus(){
 const D=DB.items||[],cut7=Date.now()-7*864e5;
 const SRC=[...new Set(D.map(i=>i.source))].sort(),MAT=[...new Set(D.flatMap(i=>i.matieres))].sort();
 $("nav").innerHTML=`<div class="navlbl">Source</div><button class="navitem ${!src&&!mat?"on":""}" data-k="all">Toutes les sources <span class="n">${D.length}</span></button>`+
  SRC.map(s=>`<button class="navitem ${src===s?"on":""}" data-k="src" data-v="${esc(s)}"><span class="dot" style="background:${COL[CLS[s]]||"var(--mut)"}"></span>${esc(s)} <span class="n">${D.filter(i=>i.source===s).length}</span></button>`).join("")+
  `<div class="navlbl">Matière</div>`+MAT.map(m=>`<button class="navitem ${mat===m?"on":""}" data-k="mat" data-v="${esc(m)}">${esc(m)} <span class="n">${D.filter(i=>i.matieres.includes(m)).length}</span></button>`).join("");
 document.querySelectorAll("#nav .navitem").forEach(b=>b.onclick=()=>{const k=b.dataset.k,v=b.dataset.v;if(k==="all"){src="";mat=""}else if(k==="src"){src=src===v?"":v}else{mat=mat===v?"":v}render();if(innerWidth<=860)closeSb()});
 const q=$("q").value.toLowerCase();
 const r=D.filter(i=>(!src||i.source===src)&&(!mat||i.matieres.includes(mat))&&(!q||(i.title+" "+i.abstract+" "+(i.ai?i.ai.resume+" "+(i.ai.points||[]).join(" "):"")).toLowerCase().includes(q))).slice(0,300);
 const e=DB.digest&&DB.digest.by_matiere?Object.entries(DB.digest.by_matiere):[];
 let h=e.length&&!q&&!src&&!mat?`<div class="brief"><h2>📋 Brief de la semaine</h2><div class="s2">Synthèse automatique (IA) des 7 derniers jours, par matière — généré le ${fd(DB.digest.date)} · à vérifier sur les fiches</div>${e.map(([m,t],k)=>`<details ${k===0?"open":""}><summary>${esc(m)}</summary>${t.split(/\n+/).filter(Boolean).map(s=>`<p>• ${esc(s)}</p>`).join("")}</details>`).join("")}</div>`:"";
 h+='<div id="list">';let last=null;
 for(const i of r){const g=group(i);if(g!==last){h+=`<div class="daysep"><span class="l">${g}</span></div>`;last=g}
  const c=CLS[i.source]||"sp",nw=new Date(i.first_seen).getTime()>=cut7;
  h+=`<article class="card s-${c}"><div class="top"><span class="badge">${esc(i.source)}</span>${nw?'<span class="badge new">Nouveau</span>':""}${i.ai&&i.ai.partielle?'<span class="badge part">Extrait public</span>':""}<span class="date">${fd(i.date)}</span></div>
  <a class="t" href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a><div class="tags">${i.matieres.map(m=>`<span class="tag">${esc(m)}</span>`).join("")}</div>
  ${i.ai?`<span class="aitag">Fiche IA — à vérifier</span><p class="resume">${esc(i.ai.resume)}</p>`:`<p class="abs">${esc(i.abstract)}</p>`}
  ${fiche(i)}<a class="src" href="${esc(i.url)}" target="_blank" rel="noopener">Consulter la source →</a></article>`}
 $("main").innerHTML=h+(r.length?"":'<p style="color:var(--mut)">Aucun résultat.</p>')+"</div>";
}
/* connexion automatique si l'utilisateur l'a demandé */
(async()=>{if(window.__PREVIEW__){await unlock("",false);return}
 const p=ls.get("veille-pass");if(p){try{await unlock(p,false)}catch(e){if(e.message==="BADPASS")ls.del("veille-pass")}}})();
</script></body></html>
"""

if __name__ == "__main__":
    main()
