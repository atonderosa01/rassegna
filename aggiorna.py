#!/usr/bin/env python3
"""Raccoglie le notizie dai feed in fonti.json e genera sito/dati.js.

Salva solo titolo, testata, data e link: il testo resta sul sito del giornale.
Una fonte che non risponde non blocca le altre e l'archivio resta valido.
"""
from __future__ import annotations

import calendar
import concurrent.futures as cf
import html
import json
import logging
import logging.handlers
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import feedparser

BASE = Path(__file__).resolve().parent
DATI = BASE / "dati"
FILE_ARCHIVIO = DATI / "archivio.json"
FILE_STATO = DATI / "stato_feed.json"
FILE_SITO = BASE / "sito" / "dati.js"

GIORNI_ARCHIVIO = 7
GIORNI_FONTE_FERMA = 7
FINESTRA_STESSA_STORIA = 48 * 3600
SOGLIA_STESSA_STORIA = 0.42
MIN_PAROLE_IN_COMUNE = 3
MAX_BYTE_FEED = 15_000_000
TIMEOUT = 25
USER_AGENT = "RassegnaNotizie/1.0 (lettore RSS personale)"

STOPWORD = set(
    """a ad al alla alle allo ai agli anche ancora che chi ci come con cosa cosi da dal dalla dalle dallo dai dagli
    dei del della delle dello degli di dopo due e ecco ed era fa fra gli ha hanno ho i il in io la le lo loro lui ma
    mai me mi ne nei nel nella nelle nello negli no noi non nuova nuove nuovi nuovo o ogni oggi per perche piu poi
    puo qui quando quanto quella quelle quello quelli questa queste questo questi se sara sei si sia sono su sua sue
    sui sul sulla sulle sullo suo suoi tra tre tu tutti tutto tutte un una uno vi video foto ora ore anni anno""".split()
)
PARAMETRI_TRACCIAMENTO = re.compile(r"^(utm_.*|ref|refresh_ce|rss|fbclid|gclid|cmpid|xtor)$", re.I)
MESI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
DATA_ITALIANA = re.compile(rf"\b(\d{{1,2}}) ({'|'.join(MESI)})[a-z]* (\d{{4}}) (\d{{2}}) (\d{{2}})\b")

log = logging.getLogger("rassegna")


def normalizza(testo: str) -> str:
    """Minuscolo, senza accenti né punteggiatura: 'Caro-vita, più' -> 'caro vita piu'."""
    testo = unicodedata.normalize("NFKD", testo)
    testo = "".join(c for c in testo if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", testo).split())


def pulisci_titolo(titolo: str, testata_google: str | None = None) -> str:
    titolo = re.sub(r"<[^>]+>", " ", html.unescape(titolo or ""))
    # Alcuni feed perdono apostrofi e virgolette: "all?asta", "ipotesi ?garante?".
    titolo = re.sub(r"(?<=[^\W\d_])\?(?=[^\W\d_])", "’", titolo)
    titolo = re.sub(r"(?<![^\s(])\?(\w[^?]*\w)\?", r"“\1”", titolo)
    titolo = " ".join(titolo.split())
    if testata_google and titolo.lower().endswith(f" - {testata_google.lower()}"):
        titolo = titolo[: -len(testata_google) - 3].rstrip()
    return titolo


def pulisci_link(link: str) -> str | None:
    link = (link or "").strip()
    parti = urllib.parse.urlsplit(link)
    if parti.scheme not in ("http", "https") or not parti.netloc:
        return None
    # Il feed di Libero pubblica anche voci rotte, con link ".../undefined/" che danno 404.
    if re.search(r"/(undefined|null)(/|$)", parti.path):
        return None
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parti.query, keep_blank_values=True)
             if not PARAMETRI_TRACCIAMENTO.match(k)]
    return urllib.parse.urlunsplit((parti.scheme, parti.netloc, parti.path, urllib.parse.urlencode(query), ""))


def data_italiana(testo: str) -> float | None:
    """Sky TG24 scrive le date in italiano ("mar, 03 mag 2026 ..."), che feedparser non sempre capisce."""
    trovata = DATA_ITALIANA.search(normalizza(testo or ""))
    if not trovata:
        return None
    giorno, mese, anno, ore, minuti = trovata.groups()
    return float(calendar.timegm((int(anno), MESI.index(mese) + 1, int(giorno), int(ore), int(minuti), 0)))


def data_articolo(voce, adesso: float) -> float:
    """Data del feed; se manca o è nel futuro (succede) vale il momento in cui l'abbiamo visto."""
    ts = None
    for campo in ("published_parsed", "updated_parsed"):
        if voce.get(campo):
            try:
                ts = float(calendar.timegm(voce[campo]))
                break
            except (ValueError, OverflowError):
                continue
    if ts is None:
        ts = data_italiana(voce.get("published") or voce.get("updated"))
    return adesso if ts is None or ts > adesso + 600 else ts


def assegna_temi(titolo: str, sezione: str | None, config: dict) -> list[str]:
    testo = f" {normalizza(titolo)} "
    contiene = lambda parole: any(f" {normalizza(p)} " in testo for p in parole)
    trovati = {t for t, parole in config["temi"].items() if contiene(parole)}
    trovati = {t for t in trovati if not contiene(config["temi_esclusioni"].get(t, []))}
    if sezione:
        trovati.add(sezione)
    # "Juventus-Roma" è sport: non deve finire tra le notizie su Roma o sul bilancio.
    if "Sport" in trovati:
        trovati = {t for t in trovati if t in config["temi_anche_nello_sport"] or t == sezione}
    return sorted(trovati)


def nome_testata(grezzo: str | None, note: dict[str, str]) -> str:
    """Unifica 'Il Sole 24 ORE' e 'Il Sole 24 Ore'; Google a volte dà un indirizzo al posto del nome."""
    nome = (grezzo or "").strip()
    if nome.startswith("http"):
        nome = urllib.parse.urlsplit(nome).netloc.removeprefix("www.")
    return note.get(normalizza(nome), nome) or "Altra fonte"


def temi_della_storia(articoli: list[dict]) -> list[str]:
    """Un tema vale per la storia solo se lo condivide almeno un terzo dei titoli."""
    conteggio = Counter(k for a in articoli for k in a["k"])
    return sorted(k for k, n in conteggio.items() if n * 3 >= len(articoli))


def url_google(query: str) -> str:
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "it", "gl": "IT", "ceid": "IT:it"}
    )


def scarica(url: str, stato: dict) -> bytes | None:
    """Scarica il feed; None se non è cambiato dall'ultima volta (HTTP 304)."""
    intestazioni = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}
    if stato.get("etag"):
        intestazioni["If-None-Match"] = stato["etag"]
    if stato.get("modified"):
        intestazioni["If-Modified-Since"] = stato["modified"]
    richiesta = urllib.request.Request(url, headers=intestazioni)
    for tentativo in (1, 2):
        try:
            with urllib.request.urlopen(richiesta, timeout=TIMEOUT) as risposta:
                corpo = risposta.read(MAX_BYTE_FEED + 1)
                if len(corpo) > MAX_BYTE_FEED:
                    raise ValueError("feed troppo grande")
                stato["etag"] = risposta.headers.get("ETag")
                stato["modified"] = risposta.headers.get("Last-Modified")
                return corpo
        except urllib.error.HTTPError as errore:
            if errore.code == 304:
                return None
            if errore.code < 500 or tentativo == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if tentativo == 2:
                raise
        time.sleep(2)
    return None


def leggi_fonte(fonte: dict, stato_tutti: dict, config: dict, adesso: float) -> tuple[list[dict], dict]:
    """Restituisce gli articoli della fonte e il suo stato per il pannello 'fonti'."""
    url = fonte.get("url") or url_google(fonte["google"])
    testata = fonte.get("testata")
    nome = testata or f"Ricerca: {fonte['tema']}"
    if fonte.get("sezione"):
        nome += f" · {fonte['sezione']}"
    stato = stato_tutti.setdefault(url, {})
    esito = {"nome": nome, "testata": testata or "", "ok": True, "errore": "", "nuovi": 0}
    articoli: list[dict] = []
    try:
        corpo = scarica(url, stato)
    except Exception as errore:
        esito.update(ok=False, errore=str(errore)[:120])
        log.warning("%s: %s", nome, errore)
        esito["ultimo"] = stato.get("ultimo")
        return articoli, esito
    if corpo is None:
        esito["ultimo"] = stato.get("ultimo")
        return articoli, esito

    feed = feedparser.parse(corpo)
    if not feed.entries and feed.bozo:
        esito.update(ok=False, errore=f"feed non leggibile: {feed.bozo_exception}"[:120])
        log.warning("%s: %s", nome, esito["errore"])
        esito["ultimo"] = stato.get("ultimo")
        return articoli, esito

    scarta = [re.compile(r, re.I) for r in config["scarta_titoli"]]
    note = {normalizza(f["testata"]): f["testata"] for f in config["testate"]}
    limite = adesso - GIORNI_ARCHIVIO * 86400
    for voce in feed.entries:
        fonte_voce = (voce.get("source") or {}).get("title")
        titolo = pulisci_titolo(voce.get("title"), fonte_voce if fonte.get("google") else None)
        link = pulisci_link(voce.get("link"))
        if not titolo or not link or any(r.search(normalizza(titolo)) for r in scarta):
            continue
        data = data_articolo(voce, adesso)
        stato["ultimo"] = max(stato.get("ultimo") or 0, data)
        if data < limite:
            continue
        temi = assegna_temi(titolo, fonte.get("sezione"), config)
        # La ricerca Google trova anche articoli che citano il tema di sfuggita: serve nel titolo.
        if fonte.get("tema") and fonte["tema"] not in temi:
            continue
        articoli.append({"t": titolo, "u": link, "s": testata or nome_testata(fonte_voce, note), "d": data, "k": temi})
    esito["ultimo"] = stato.get("ultimo")
    esito["nuovi"] = len(articoli)
    return articoli, esito


def unisci_in_archivio(archivio: list[dict], nuovi: list[dict], limite: float) -> list[dict]:
    """Stessa testata + stesso titolo = stesso articolo. Il link diretto vince su quello di Google."""
    per_chiave: dict[tuple[str, str], dict] = {}
    for art in archivio + nuovi:
        if art["d"] < limite:
            continue
        chiave = (normalizza(art["s"]), normalizza(art["t"]))
        esistente = per_chiave.get(chiave)
        if esistente is None:
            per_chiave[chiave] = art
            continue
        esistente["k"] = sorted(set(esistente["k"]) | set(art["k"]))
        if "news.google.com" in esistente["u"] and "news.google.com" not in art["u"]:
            esistente["u"] = art["u"]
    return sorted(per_chiave.values(), key=lambda a: a["d"])


def parole_chiave(titolo: str) -> set[str]:
    parole = set()
    for parola in normalizza(titolo).split():
        if len(parola) > 2 and parola not in STOPWORD:
            parole.add(parola[:-1] if len(parola) > 4 else parola)
    return parole


def raggruppa_storie(articoli: list[dict]) -> list[list[dict]]:
    """Riunisce i titoli di testate diverse sulla stessa notizia (coseno TF-IDF sui titoli)."""
    insiemi = [parole_chiave(a["t"]) for a in articoli]
    frequenza = Counter(p for insieme in insiemi for p in insieme)
    totale = max(len(articoli), 1)
    idf = {p: math.log(1 + totale / n) for p, n in frequenza.items()}
    norme = [math.sqrt(sum(idf[p] ** 2 for p in insieme)) or 1.0 for insieme in insiemi]

    storie: list[list[int]] = []
    indice: dict[str, set[int]] = defaultdict(set)
    for i, art in enumerate(articoli):
        candidate = Counter()
        for parola in insiemi[i]:
            for s in indice[parola]:
                candidate[s] += 1
        migliore, punteggio = None, 0.0
        for s, _ in candidate.most_common(40):
            if art["d"] - articoli[storie[s][-1]]["d"] > FINESTRA_STESSA_STORIA:
                continue
            for j in storie[s]:
                comuni = insiemi[i] & insiemi[j]
                if len(comuni) < MIN_PAROLE_IN_COMUNE:
                    continue
                coseno = sum(idf[p] ** 2 for p in comuni) / (norme[i] * norme[j])
                if coseno > punteggio:
                    migliore, punteggio = s, coseno
        if migliore is None or punteggio < SOGLIA_STESSA_STORIA:
            migliore = len(storie)
            storie.append([])
        storie[migliore].append(i)
        for parola in insiemi[i]:
            indice[parola].add(migliore)
    return [[articoli[i] for i in reversed(s)] for s in storie]


def scrivi_atomico(percorso: Path, testo: str) -> None:
    percorso.parent.mkdir(parents=True, exist_ok=True)
    temporaneo = percorso.with_suffix(percorso.suffix + ".tmp")
    temporaneo.write_text(testo, encoding="utf-8")
    os.replace(temporaneo, percorso)


def leggi_json(percorso: Path, predefinito):
    try:
        return json.loads(percorso.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return predefinito
    except ValueError:
        log.warning("%s danneggiato: riparto da vuoto", percorso.name)
        return predefinito


def esegui() -> int:
    adesso = time.time()
    config = json.loads((BASE / "fonti.json").read_text(encoding="utf-8"))
    archivio = [a for a in leggi_json(FILE_ARCHIVIO, []) if pulisci_link(a["u"])]
    # Senza archivio non possiamo fidarci di "non è cambiato nulla": riscarichiamo tutto.
    stato = leggi_json(FILE_STATO, {}) if archivio else {}

    fonti = config["testate"] + config["ricerche_per_tema"]
    dirette = [f for f in fonti if "url" in f]
    google = [f for f in fonti if "google" in f]
    risultati = []
    with cf.ThreadPoolExecutor(8) as esecutore:
        risultati += esecutore.map(lambda f: leggi_fonte(f, stato, config, adesso), dirette)
    for fonte in google:  # una alla volta, per non martellare lo stesso server
        risultati.append(leggi_fonte(fonte, stato, config, adesso))
        time.sleep(1)

    esiti = [e for _, e in risultati]
    attive = sum(e["ok"] for e in esiti)
    if not attive:
        log.error("Nessuna fonte raggiungibile: connessione assente? Dati precedenti conservati.")
        return 1

    nuovi = [a for arts, _ in risultati for a in arts]
    archivio = unisci_in_archivio(archivio, nuovi, adesso - GIORNI_ARCHIVIO * 86400)
    storie = raggruppa_storie(archivio)
    storie.sort(key=lambda s: s[0]["d"], reverse=True)

    per_sito = [{"a": [{"t": a["t"], "u": a["u"], "s": a["s"], "d": int(a["d"])} for a in s],
                 "k": temi_della_storia(s)} for s in storie]
    fonti_sito = []
    for e in sorted(esiti, key=lambda e: (e["ok"], e["nome"].lower())):
        ultimo = e.get("ultimo")
        ferma = e["ok"] and bool(ultimo) and adesso - ultimo > GIORNI_FONTE_FERMA * 86400
        fonti_sito.append({"nome": e["nome"], "ok": e["ok"], "ferma": ferma, "errore": e["errore"],
                           "ultimo": int(ultimo) if ultimo else None})
    dati = {"aggiornato": int(adesso), "temi": list(config["temi"]), "fonti": fonti_sito, "storie": per_sito}

    scrivi_atomico(FILE_ARCHIVIO, json.dumps(archivio, ensure_ascii=False, separators=(",", ":")))
    scrivi_atomico(FILE_STATO, json.dumps(stato, ensure_ascii=False, indent=0))
    scrivi_atomico(FILE_SITO, "window.NOTIZIE=" + json.dumps(dati, ensure_ascii=False, separators=(",", ":")) + ";\n")
    log.info("Fonti attive %d/%d, articoli %d, storie %d", attive, len(esiti), len(archivio), len(storie))
    return 0


def main() -> int:
    DATI.mkdir(exist_ok=True)
    gestori: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(DATI / "aggiorna.log", maxBytes=500_000, backupCount=1, encoding="utf-8")
    ]
    if sys.stdout is not None:  # con pythonw (attività pianificata) non c'è console
        sys.stdout.reconfigure(encoding="utf-8")
        gestori.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=gestori)
    try:
        return esegui()
    except Exception:
        log.exception("Aggiornamento fallito")
        return 1


if __name__ == "__main__":
    sys.exit(main())
