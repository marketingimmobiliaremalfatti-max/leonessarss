# Feed RSS annunci – Immobiliare Leonessa

Genera automaticamente un feed RSS degli annunci in vendita pubblicati su
`immobiliareleonessa.com`, da collegare a **Postpikr** per la pubblicazione
automatica su Facebook e Instagram (1 annuncio al giorno).

Stessa architettura del progetto gemello per Immobiliare Malfatti, adattata
alla struttura del sito (WordPress + plugin immobiliare, dati più ricchi:
titolo e descrizione già specifici per annuncio, categorie di tipo immobile
taggate esplicitamente).

## Come funziona

- `scraper.py` scarica le pagine di elenco da `immobili-in-vendita/` (il
  sito separa già vendita e affitto, quindi gli affitti sono esclusi per
  costruzione) ed esclude negozi, uffici, magazzini, capannoni, terreni e
  box/cantine, sia per parola chiave nello slug sia controllando i tag
  categoria `/property-type/<slug>/` trovati sulla pagina di dettaglio.
- Per ogni annuncio legge titolo, descrizione, foto (già forniti dal sito
  come meta tag Open Graph specifici) e i dati tecnici (prezzo, camere,
  bagni, superficie) dalla sezione "Panoramica" della pagina.
- Se è configurato il secret `ANTHROPIC_API_KEY`, chiede a Claude di
  scrivere la sezione narrativa "DESCRIZIONE" del post (generata una sola
  volta per annuncio, poi riusata).
- Se l'annuncio ha un funnel associato in `data/funnels.json`, il post
  include un invito con quel link.
- Inserisce la foto dell'immobile nel template brandizzato
  (`assets/template_vendita.png`).
- **Ciclo di ripubblicazione**: la lunghezza del ciclo è pari al numero di
  annunci attivi (pensato per un ritmo di 1 post al giorno in Postpikr).
  Quando il ciclo finisce, tutti gli annunci ancora attivi vengono
  "rinfrescati" così Postpikr li ripubblica da capo. Stato in
  `data/cycle.json`.
- Il workflow gira **una volta a settimana** (lunedì alle 6:00 UTC) e
  pubblica `docs/rss.xml` via GitHub Pages.

## Setup

1. Crea un repository **pubblico** su GitHub chiamato `leonessarss` (o il
   nome che preferisci — se lo cambi, aggiorna `PAGES_BASE_URL` in
   `scraper.py`).
2. Carica tutti i file di questo progetto (incluse le cartelle nascoste
   `.github/` e i file `.gitkeep` — se l'upload da browser non li
   trascina, creali a mano da **Add file → Create new file** scrivendo il
   percorso completo, es. `docs/images/.gitkeep`).
3. **Settings → Pages**: Source `Deploy from a branch`, Branch `main`,
   cartella `/docs`.
4. **Settings → Actions → General**: "Read and write permissions" per i
   workflow.
5. (Opzionale) **Settings → Secrets and variables → Actions → New
   repository secret**: nome `ANTHROPIC_API_KEY`, valore la tua chiave API
   Anthropic.
6. Avvia il workflow manualmente: tab **Actions** → "Aggiorna feed RSS
   Immobiliare Leonessa" → **Run workflow**.
7. Il feed sarà su:
   ```
   https://<tuo-utente-github>.github.io/<nome-repository>/rss.xml
   ```
8. Incolla l'URL in Postpikr, con un limite di **1 post al giorno**.

## Gestire i funnel personalizzati

In `data/funnels.json`, chiave = URL completo della pagina annuncio, valore
= URL del funnel:

```json
{
  "https://www.immobiliareleonessa.com/property/indipendente-in-vendita-a-leonessa-centro-storico-70/": "https://esempio-funnel.it"
}
```

## Relazione con il vecchio sistema (Leonessa-bot)

Questo progetto è pensato per **sostituire** l'integrazione diretta con le
API di Meta usata da `Leonessa-bot` (repository esistente): il feed RSS via
Postpikr si è dimostrato più affidabile e flessibile. `Leonessa-bot` può
restare attivo in parallelo durante il periodo di transizione, ma andrebbe
disattivato una volta verificato che questo nuovo sistema funziona bene,
per evitare pubblicazioni doppie.
