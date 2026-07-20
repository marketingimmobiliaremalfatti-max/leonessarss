#!/usr/bin/env python3
"""
Scraper per Immobiliare Leonessa -> genera un feed RSS compatibile con
Postpikr per la pubblicazione automatica su Facebook/Instagram.

Il sito (WordPress + plugin immobiliare) espone dati molto più ricchi di
quelli di Malfatti: og:title e og:description già specifici per annuncio,
categorie di tipo immobile taggate esplicitamente. Questo rende lo scraping
più robusto (meno bisogno di euristiche di fallback).

Stessa architettura dello scraper Malfatti:
- esclude negozi/uffici/magazzini/capannoni/terreni/box-cantina/affitti
- genera la descrizione narrativa con Claude (una volta per annuncio)
- compone la foto nel template brandizzato
- gestisce un ciclo di ripubblicazione (1 annuncio al giorno, poi ricomincia)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from PIL import Image

BASE_URL = "https://www.immobiliareleonessa.com/"
SITE_NAME_SUFFIX = " - Immobiliare Leonessa"

PAGES_BASE_URL = "https://marketingimmobiliaremalfatti-max.github.io/leonessarss/"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-sonnet-5"

AGENCY_NAME = "Immobiliare Leonessa"

TEMPLATE_PATH = Path(__file__).parent / "assets" / "template_vendita.png"
IMAGES_DIR = Path(__file__).parent / "docs" / "images"
# Area (sinistra, alto, destra, basso) del riquadro trasparente nel template
# dove va inserita la foto, in pixel sul canvas 3375x3375.
PHOTO_AREA = (0, 670, 3375, 3375)

# Solo vendita: il sito separa già vendita e affitto in due sezioni.
LIST_URL = "immobili-in-vendita/"

# Parole chiave (cercate nello slug URL, nel titolo, e nei tag categoria
# /property-type/<slug>/) che identificano tipi di immobile da escludere:
# negozi, uffici, magazzini, capannoni, terreni, box/cantine, affitti.
EXCLUDE_KEYWORDS = (
    "negozio", "ufficio", "magazzino", "capannone",
    "terreno", "agricolo", "edificabile", "box-cantina", "box - cantina",
    "commerciali", "terreni", "affitto",
)

STATE_FILE = Path(__file__).parent / "data" / "state.json"
FUNNELS_FILE = Path(__file__).parent / "data" / "funnels.json"
ROTATION_FILE = Path(__file__).parent / "data" / "rotation.json"
OUTPUT_FILE = Path(__file__).parent / "docs" / "rss.xml"
MAX_ITEMS_IN_FEED = 60

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LeonessaRSSBot/1.0; +https://www.immobiliareleonessa.com/)"
}

DETAIL_URL_PATTERN = re.compile(r"^https://www\.immobiliareleonessa\.com/property/[^/]+/?$")


def is_excluded_listing(text):
    lowered = text.lower()
    return any(keyword in lowered for keyword in EXCLUDE_KEYWORDS)


def fetch(url, retries=3, pause=2):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            print(f"  [!] errore su {url} (tentativo {attempt+1}/{retries}): {e}", file=sys.stderr)
            time.sleep(pause)
    return None


def discover_listing_urls():
    urls = set()
    page = 1
    while True:
        list_url = urljoin(BASE_URL, LIST_URL) if page == 1 else urljoin(BASE_URL, f"{LIST_URL}page/{page}/")
        html = fetch(list_url)
        if not html:
            break

        soup = BeautifulSoup(html, "html.parser")
        page_urls = set()
        excluded_count = 0
        for a in soup.find_all("a", href=True):
            href = urljoin(BASE_URL, a["href"])
            if not DETAIL_URL_PATTERN.match(href):
                continue
            if is_excluded_listing(href):
                excluded_count += 1
                continue
            page_urls.add(href)

        new_urls = page_urls - urls
        urls |= page_urls

        print(f"  pagina {page} ({list_url}): {len(page_urls)} annunci validi, {excluded_count} esclusi, {len(new_urls)} nuovi")

        # Il sito mostra il numero di pagine in fondo (link "page/N/"): ci
        # fermiamo quando una pagina non porta nulla di nuovo.
        if len(new_urls) == 0:
            break
        page += 1
        if page > 30:  # limite di sicurezza anti-loop infinito
            break

    return sorted(urls)


def extract_meta(soup, prop):
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return tag["content"].strip() if tag and tag.get("content") else None


def extract_title(soup, url):
    og_title = extract_meta(soup, "og:title")
    if og_title:
        if og_title.endswith(SITE_NAME_SUFFIX):
            og_title = og_title[: -len(SITE_NAME_SUFFIX)]
        return og_title.strip()
    if soup.title and soup.title.string:
        t = soup.title.string.strip()
        if t.endswith(SITE_NAME_SUFFIX):
            t = t[: -len(SITE_NAME_SUFFIX)]
        return t
    return url


def extract_full_description(soup, fallback):
    """Cerca la sezione 'Descrizione' del corpo pagina per un testo più
    completo del riassunto (spesso troncato) presente in og:description."""
    text = soup.get_text("\n", strip=True)
    m = re.search(r"\nDescrizione\n(.*?)(?:\nUlteriori dettagli\n|\nPlanimetria\n|\nClasse energetica\n|$)", text, re.DOTALL)
    if m:
        desc = m.group(1).strip()
        if len(desc) > 20:
            return desc
    return fallback or ""


def extract_technical_fields(soup):
    """Estrae Prezzo, Camere da letto, Bagni, Area, Garage dalla sezione
    'Panoramica' del testo della pagina."""
    text = soup.get_text("\n", strip=True)

    fields = {}

    # Prezzo: cerca il primo importo in euro presente nella pagina, prima
    # della sezione Panoramica/Descrizione (evita di prendere prezzi di
    # "immobili simili" più in basso nella pagina).
    head_text = text.split("Panoramica")[0] if "Panoramica" in text else text[:800]
    price_matches = re.findall(r"€\s?[\d\.,]+", head_text)
    if price_matches:
        fields["Prezzo"] = price_matches[0].replace("€", "€ ").strip()

    panoramica = ""
    m = re.search(r"\nPanoramica\n(.*?)(?:\nDescrizione\n|$)", text, re.DOTALL)
    if m:
        panoramica = m.group(1)

    for label, key in (
        ("Camere da letto", "Camere"),
        ("Bagni", "Bagni"),
        ("Garage", "Garage"),
        ("Area", "Area"),
    ):
        mm = re.search(rf"{re.escape(label)}\n(.+)", panoramica)
        if mm:
            value = mm.group(1).strip()
            # Si ferma al primo a-capo/prossima etichetta
            value = value.split("\n")[0].strip()
            if value and value.lower() != "assente":
                fields[key] = value

    return fields


def extract_property_type_tags(soup):
    """Restituisce gli slug delle categorie /property-type/<slug>/ collegate
    nella pagina, utile come controllo di sicurezza aggiuntivo per i tipi da
    escludere (più affidabile del solo testo dello slug URL)."""
    tags = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/property-type/([^/]+)/?", href)
        if m:
            tags.add(m.group(1).lower())
    return tags


def scrape_listing(url):
    html = fetch(url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    title = extract_title(soup, url)
    og_description = extract_meta(soup, "og:description")
    description = extract_full_description(soup, og_description)
    image = extract_meta(soup, "og:image")
    canonical = extract_meta(soup, "og:url") or url

    fields = extract_technical_fields(soup)
    type_tags = extract_property_type_tags(soup)

    # ID stabile: usa lo slug finale dell'URL (univoco su questo sito).
    listing_id = url.rstrip("/").split("/")[-1]

    return {
        "id": listing_id,
        "url": canonical,
        "title": title,
        "description": description,
        "image": image,
        "fields": fields,
        "type_tags": type_tags,
    }


def generate_narrative(title, raw_description, url):
    if not ANTHROPIC_API_KEY or not raw_description:
        return None

    prompt = f"""Sei un copywriter immobiliare italiano. Scrivi SOLO la sezione
narrativa "DESCRIZIONE" di un annuncio, nello stile di questo esempio:

---
Nel cuore del centro storico della frazione Villa Colapietro di Leonessa,
proponiamo in vendita un'abitazione indipendente luminosa, sviluppata su tre
livelli, con giardino privato.
Al piano terra si trova una comoda e ampia cantina, ideale per il rimessaggio
o come spazio di servizio aggiuntivo. Al primo piano si accede al soggiorno
con angolo cottura, un bagno e un balcone da cui godere dell'atmosfera
tranquilla del borgo. Al secondo piano sono ricavate la camera da letto e la
cameretta, perfette per una coppia o una piccola famiglia.
La soluzione su più livelli garantisce una distribuzione funzionale degli
spazi, mentre la presenza del giardino rappresenta un plus di grande valore,
ideale per trascorrere momenti all'aperto in totale relax.
Un'opportunità concreta per chi cerca una prima casa, una residenza
secondaria o un investimento in un contesto autentico e tranquillo
dell'entroterra reatino, a un prezzo davvero accessibile.
Per maggiori informazioni o per fissare una visita, non esitare a
contattarci. Saremo felici di accompagnarti nella scoperta di questa
proprietà.
---

Regole:
- 3-5 brevi paragrafi, tono caldo e professionale, come nell'esempio
- Usa SOLO i dati forniti (descrizione originale e dati tecnici), NON
  inventare dettagli non presenti
- Se i dati disponibili sono pochi, scrivi una descrizione più breve ma
  comunque coerente: meglio corta e accurata che lunga e inventata
- Chiudi con un invito a contattare l'agenzia per informazioni o una visita,
  simile all'ultimo paragrafo dell'esempio
- NON includere hashtag, NON includere il titolo, NON scrivere l'intestazione
  "DESCRIZIONE:" (viene aggiunta separatamente) -- scrivi solo il testo

Titolo annuncio: {title}
Descrizione originale e dati tecnici: {raw_description}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        narrative = "\n".join(parts).strip()
        return narrative or None
    except requests.RequestException as e:
        print(f"  [!] Errore generazione narrativa AI per {url}: {e}", file=sys.stderr)
        return None


def build_full_caption(fields, narrative, funnel_url):
    lines = []

    if funnel_url:
        lines += ["Scopri subito le foto e il virtual tour:", funnel_url, ""]

    lines += ["CARATTERISTICHE PRINCIPALI:"]

    if fields.get("Prezzo"):
        lines.append(f"Prezzo: {fields['Prezzo']}")
    if fields.get("Area"):
        lines.append(f"Superficie: {fields['Area']}")
    if fields.get("Camere"):
        lines.append(f"Camere: {fields['Camere']}")
    if fields.get("Bagni"):
        lines.append(f"Bagni: {fields['Bagni']}")

    if narrative:
        lines += ["", "DESCRIZIONE:", narrative]

    lines += ["", AGENCY_NAME]

    return "\n".join(lines)


def compose_branded_image(photo_url, listing_id):
    if not photo_url:
        return None

    out_filename = f"{listing_id}.jpg"
    out_path = IMAGES_DIR / out_filename

    if out_path.exists():
        return urljoin(PAGES_BASE_URL, f"images/{out_filename}")

    if not TEMPLATE_PATH.exists():
        print(f"  [!] Template non trovato in {TEMPLATE_PATH}", file=sys.stderr)
        return None

    try:
        resp = requests.get(photo_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        photo = Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        print(f"  [!] Errore scaricando la foto per il template ({listing_id}): {e}", file=sys.stderr)
        return None

    template = Image.open(TEMPLATE_PATH).convert("RGBA")
    canvas_w, canvas_h = template.size
    area_left, area_top, area_right, area_bottom = PHOTO_AREA
    area_w = area_right - area_left
    area_h = area_bottom - area_top

    photo_ratio = photo.width / photo.height
    area_ratio = area_w / area_h
    if photo_ratio > area_ratio:
        new_height = area_h
        new_width = int(new_height * photo_ratio)
    else:
        new_width = area_w
        new_height = int(new_width / photo_ratio)

    photo_resized = photo.resize((new_width, new_height), Image.LANCZOS)
    left = (new_width - area_w) // 2
    top = (new_height - area_h) // 2
    photo_cropped = photo_resized.crop((left, top, left + area_w, top + area_h))

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255))
    canvas.paste(photo_cropped, (area_left, area_top))
    canvas.alpha_composite(template)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, "JPEG", quality=88)

    return urljoin(PAGES_BASE_URL, f"images/{out_filename}")


def load_rotation():
    if ROTATION_FILE.exists():
        try:
            return json.loads(ROTATION_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"order": [], "pointer": -1, "last_advanced_date": None}


def save_rotation(rotation):
    ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROTATION_FILE.write_text(json.dumps(rotation, ensure_ascii=False, indent=2), encoding="utf-8")


def advance_daily_rotation(state, active_ids):
    """Postpikr salta la pubblicazione se il feed non ha 'contenuti nuovi'
    rispetto all'ultima volta -- non tiene una coda propria da svuotare nel
    tempo. Per garantire che ci sia sempre qualcosa di nuovo da pubblicare
    ogni giorno, ogni giorno di calendario scegliamo UN annuncio tra quelli
    già attivi e gli assegniamo una data di pubblicazione e un guid nuovi
    (vedi 'refresh_count' in state). Si ruota nell'ordine tra tutti gli
    annunci attivi, ricominciando da capo una volta arrivati in fondo."""
    rotation = load_rotation()
    order = [lid for lid in rotation.get("order", []) if lid in active_ids]
    for lid in sorted(active_ids):
        if lid not in order:
            order.append(lid)

    today = datetime.now(timezone.utc).date().isoformat()

    if not order:
        save_rotation({"order": order, "pointer": -1, "last_advanced_date": today})
        return

    if rotation.get("last_advanced_date") != today:
        pointer = (rotation.get("pointer", -1) + 1) % len(order)
        chosen_id = order[pointer]
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = state.setdefault(chosen_id, {})
        entry["last_published_at"] = now_iso
        entry["refresh_count"] = entry.get("refresh_count", 0) + 1
        print(f"  [ROTAZIONE] Oggi tocca a: {entry.get('title', chosen_id)}")
        rotation = {"order": order, "pointer": pointer, "last_advanced_date": today}
    else:
        rotation["order"] = order

    save_rotation(rotation)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_funnels():
    if FUNNELS_FILE.exists():
        try:
            return json.loads(FUNNELS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  [!] funnels.json non valido, ignorato: {e}", file=sys.stderr)
    return {}


def build_feed(listings_with_dates):
    fg = FeedGenerator()
    fg.title("Immobiliare Leonessa - Annunci")
    fg.link(href=BASE_URL, rel="alternate")
    fg.description("Feed automatico degli annunci pubblicati su immobiliareleonessa.com")
    fg.language("it")

    listings_with_dates.sort(key=lambda x: x["last_published_at"], reverse=True)

    for item in listings_with_dates[:MAX_ITEMS_IN_FEED]:
        fe = fg.add_entry()
        fe.id(item["url"])
        fe.title(item["title"])
        fe.link(href=item["url"])
        fe.guid(f"{item['url']}#r{item['refresh_count']}", permalink=False)

        pub_date = datetime.fromisoformat(item["last_published_at"])
        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)
        fe.pubDate(pub_date)

        raw_caption = item.get("caption") or item["description"] or ""
        desc_html = raw_caption.replace("\n", "<br/>\n")
        if item.get("image"):
            desc_html = f'<img src="{item["image"]}" /><br/>{desc_html}'
        fe.description(desc_html)

        if item.get("image"):
            try:
                fe.enclosure(item["image"], 0, "image/jpeg")
            except Exception:
                pass

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(OUTPUT_FILE), pretty=True)


def main():
    print("== Scoperta annunci ==")
    listing_urls = discover_listing_urls()
    print(f"Totale annunci unici trovati: {len(listing_urls)}")

    state = load_state()
    funnels = load_funnels()
    print(f"Funnel configurati: {len(funnels)}")
    now_iso = datetime.now(timezone.utc).isoformat()

    listings_with_dates = []
    seen_ids = set()

    print("== Estrazione dettagli annunci ==")
    for url in listing_urls:
        data = scrape_listing(url)
        if not data:
            continue

        # Controllo di sicurezza aggiuntivo dopo lo scraping: usa titolo,
        # URL canonico e i tag di categoria /property-type/ effettivamente
        # trovati sulla pagina di dettaglio.
        type_tags_text = " ".join(data["type_tags"])
        if (
            is_excluded_listing(data["url"])
            or is_excluded_listing(data["title"])
            or is_excluded_listing(type_tags_text)
        ):
            print(f"  [ESCLUSO] {data['title']} ({data['url']}) -- rilevato dopo lo scraping")
            continue

        listing_id = data["id"]
        seen_ids.add(listing_id)
        previous = state.get(listing_id, {})

        if listing_id in state:
            first_seen = previous["first_seen"]
            last_published_at = previous.get("last_published_at", first_seen)
            refresh_count = previous.get("refresh_count", 0)
        else:
            first_seen = now_iso
            last_published_at = now_iso
            refresh_count = 0
            print(f"  [NUOVO] {data['title']} ({url})")

        narrative = previous.get("narrative")
        if not narrative:
            narrative = generate_narrative(data["title"], data["description"], data["url"])
            if narrative:
                print(f"  [AI] Descrizione narrativa generata per {data['title']}")

        funnel_url = funnels.get(data["url"])
        caption = build_full_caption(data["fields"], narrative, funnel_url)

        branded_image = compose_branded_image(data["image"], listing_id)
        image_for_feed = branded_image or data["image"]

        state[listing_id] = {
            "first_seen": first_seen,
            "last_published_at": last_published_at,
            "refresh_count": refresh_count,
            "url": data["url"],
            "title": data["title"],
            "narrative": narrative,
        }

        listings_with_dates.append({
            **data,
            "image": image_for_feed,
            "first_seen": first_seen,
            "last_published_at": last_published_at,
            "refresh_count": refresh_count,
            "caption": caption,
        })

    removed = set(state.keys()) - seen_ids
    for rid in removed:
        print(f"  [RIMOSSO] {state[rid]['title']}")
        del state[rid]

    save_state(state)

    advance_daily_rotation(state, active_ids=seen_ids)
    for item in listings_with_dates:
        item["last_published_at"] = state[item["id"]]["last_published_at"]
        item["refresh_count"] = state[item["id"]]["refresh_count"]
    save_state(state)

    print("== Generazione feed RSS ==")
    build_feed(listings_with_dates)
    print(f"Feed scritto in: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
