# Projekt Prompt: Magyar Dohánytermék Árfigyelő (GitHub Pages)

## Szerepkör és Cél
Viselkedj tapasztalt Full-Stack és Frontend fejlesztőként. Készíts egy teljes, azonnal telepíthető, **GitHub Pages**-en futtatható statikus webalkalmazást és a hozzá tartozó **GitHub Actions** automatizálást, amely a Szabályozott Tevékenységek Felügyeleti Hatósága (SZTFH) hivatalos oldaláról automatikusan frissíti és megjeleníti a magyarországi dohánytermékek hatósági kiskereskedelmi árait.

---

## Technológiai Stack és Architektúra
* **Frontend:** Tiszta, keretrendszermentes (Vanilla) **HTML5, CSS3, ES6+ JavaScript** (build lépés nélkül, közvetlenül futtatható GitHub Pages-en).
* **Adatforrás:** `data/prices.json` (statikus JSON fájl a repóban).
* **Automatizált frissítés (Backend-mentes):** **Python 3** szkript + **GitHub Actions** workflow, amely ütemezetten letölti és feldolgozza az SZTFH Excel (`.xlsx`) táblázatait, frissíti a `data/prices.json` fájlt, és automatikusan commitolja a változásokat.

---

## 1. Adatgyűjtő, Pontos Terméknevek és Automatizálás (`scripts/update_prices.py` + `.github/workflows/update-prices.yml`)
* **Forrásoldal:** `https://sztfh.hu/tevekenysegek/dohanyugyi-felugyelet/arak-kozzetetele/`
* **Szigorú követelmény – Pontos, hivatalos terméknevek:**
  * A szkript **szó szerint, karakterpontosan** őrizze meg az SZTFH táblázatában szereplő hivatalos termékneveket (márka, típus, szín/variáns, szál- vagy tömegmegjelölés, pl. `Marlboro Red 20 szálas`, `Sopianae Multifilter Piros 20`, `TEREA Amber`, `Dunhill Fine Cut Blue 20` stb.).
  * Tilos a nevek rövidítése, csonkítása vagy önkényes átírása; kizárólag a felesleges kezdő/záró szóközök (`strip()`) és dupla szóközök tisztítása megengedett.
  * A minta `data/prices.json` fájl is tartalmazzon ~20–25 darab **valós, pontos megnevezésű** magyarországi dohányterméket (különböző kategóriákból: cigaretta, hevített dohánytermék, finomra vágott fogyasztási dohány, szivarka, nikotinpárna/egyéb), valósághű kiszerelésekkel és árakkal.
* **Python szkript működése (`requests`, `beautifulsoup4`, `openpyxl`):**
  1. Töltse le az SZTFH oldal HTML-jét, és keresse meg a legfrissebb, teljes árlistát tartalmazó `.xlsx` fájl linkjét.
  2. Töltse le a fájlt, és robusztusan dolgozza fel (kezelje az esetleges eltolt fejlécsort, az üres sorokat és a kategóriabontó sorokat).
  3. Nyerd ki minden terméknél: `id` (stabil hash a pontos `name` + `unit` alapján), `name` (pontos, hivatalos terméknév), `category` (kategória), `unit` (kiszerelés/mennyiség), `price` (aktuális ár Ft-ban, integer), `valid_from` (érvényesség kezdete).
  4. Olvassa be a korábbi `data/prices.json` fájlt (ha létezik), és számítsa ki az árváltozásokat (`previous_price`, `price_diff`, `changed_at`).
  5. Mentse az eredményt `data/prices.json` fájlba egy `meta` objektummal együtt (`last_updated`, `source_file`, `source_url`, `total_count`).
* **GitHub Actions workflow:**
  * Fusson automatikusan naponta egyszer (cron), valamint legyen manuálisan is indítható (`workflow_dispatch`).
  * Csak akkor commitoljon és pusholjon, ha a `data/prices.json` tartalma valóban megváltozott.

---

## 2. Vizuális Téma és Tipográfia (Nemzeti Dohánybolt Inspiráció)
A dizájn és a tipográfia hűen idézze meg a magyar Nemzeti Dohányboltok jellegzetes, letisztult, markáns arculatát, modern UI-val ötvözve:
* **Színpaletta (CSS változókkal):**
  * **Elsődleges háttér:** Mély dohánybarna / sötét eszpresszó (`#1E140F`, `#2B1B12`)
  * **Kártya / felület háttér:** Meleg sötétbarna (`#362318`) finom bronz/arany szegéllyel (`rgba(212, 175, 55, 0.22)`)
  * **Kiemelő szín (Accent):** Dohánybolt-arany / meleg okker (`#D4AF37`, hover: `#E5C158`)
  * **Szövegszínek:** Krémfehér (`#F6F1E9`) elsődleges szöveg, meleg bézs-szürke (`#BFA995`) másodlagos szöveg
  * **Állapotjelzők:** Áremelkedésnél visszafogott téglavörös (`#E05A47`), árcsökkenésnél olívazöld (`#5CA974`), új terméknél arany jelvény.
* **Dohánybolt-arculatú tipográfia (Google Fonts, kötelező `latin-ext` támogatással a magyar `ő`, `ű` ékezetekhez):**
  * **Főcímek, fejléc-felirat, kategória-címkék és árak:** **`Oswald`** (vagy `Barlow Condensed`), 500–700-as súllyal. A főcímeknél és az oldal fejlécében használj nagybetűs megjelenítést (`text-transform: uppercase`) és enyhén megnövelt betűközt (`letter-spacing: 0.05em–0.08em`), amely pontosan a Nemzeti Dohányboltok külső cégtábláinak és hivatalos árlistáinak jellegzetes, keskenyített, karakteres betűképét adja vissza.
  * **Terméknevek, kereső, szűrők és folyószöveg:** **`Barlow`** (vagy `PT Sans`), 400–600-as súllyal — tiszta, bolti árcímkéket idéző, kiválóan olvasható sans-serif betűtípus.
  * **Árak formázása:** Kötelezően `font-variant-numeric: tabular-nums`, félkövér (`Oswald` / `Barlow Condensed`) megjelenítéssel, hogy az árak árcédula-szerűen, egységesen és markánsan sorakozzanak.
* **Teljes név megjelenítése:** A kártyákon és a táblázatban a hosszú, pontos terméknevek tördelődhessenek több sorba (`word-wrap: break-word`), ne vágja le őket `text-overflow: ellipsis` olvashatatlanra.
* **Opcionális témaváltó:** Sötét (alapértelmezett dohánybarna-arany) és világos (krém/homok alapon sötétbarna szöveg és arany kiemelés) téma közötti váltás `localStorage` mentéssel.

---

## 3. Reszponzivitás (Minden Eszközre Optimalizálva)
Az oldal legyen **mobile-first** szemléletű és tökéletesen használható minden képernyőméreten:
* **Mobil (< 640px):**
  * Egyoszlopos, átlátható kártyás elrendezés (tilos a vízszintes görgetés).
  * Legalább `44x44px` érintési felületek (gombok, szűrők, keresőtörlő `×` gomb, kedvenc csillag).
  * Rögzített (sticky) keresősáv az oldal tetején és vízszintesen görgethető kategória-chipek.
* **Tablet (640px – 1024px):**
  * Kétoszlopos kártyarács, jól szellőző kereső- és szűrősávval.
* **Laptop és Desktop (> 1024px):**
  * Váltható nézet (táblázatos lista vagy 3–4 oszlopos kártyanézet), maximális tartalomszélesség (`max-width: 1280px`) középre igazítva.

---

## 4. Kereső és Funkcionális Követelmények
1. **Kiemelt, gyors kereső (Sticky Search Bar):**
   * Az oldal tetején jól látható, ikonnal és egykattintásos törlés (`×`) gombbal ellátott keresőmező (pl. placeholder: `Keresés pontos névre, márkára vagy típusra... pl. Marlboro Red, TEREA Amber`).
   * **Valós idejű, debounce-olt keresés** (pl. 150ms) több szóra is (szóközökkel elválasztott szavak sorrendtől függetlenül illeszkedjenek a teljes terméknévre és kategóriára, pl. `red marlboro` is megtalálja a `Marlboro Red` termékeket).
   * **Ékezet- és kis/nagybetű-független** normalizált keresés (pl. `sopianae piros`, `hevites`, `szivarka`).
   * **Találatok vizuális kiemelése:** A találati listában a keresett kifejezés legyen arany kiemeléssel (`<mark>`) jelölve a terméknévben.
   * **Találatszámláló és üres állapot:** Mutassa, hány találat van az összes termékből (pl. `12 / 1450 termék`), és ha nincs találat, jelenítsen meg egyértelmű üzenetet egy „Szűrők és keresés törlése” gombbal.
2. **Szűrés és rendezés:**
   * Kategória szerinti szűrés.
   * „Csak árváltozások” szűrőkapcsoló.
   * Rendezés: Pontos név (A–Z / Z–A), Ár (növekvő / csökkenő), Legutóbb változott.
3. **Kedvencek:** Felhasználó megjelölhet termékeket csillaggal (mentés `localStorage`-ban), és külön „Kedvencek” fülön/szűrővel megtekintheti őket.
4. **Árváltozás megjelenítése:** Ha egy termék ára változott, jelenjen meg egyértelmű jelvény (`+50 Ft` vagy `-30 Ft`), az előző ár áthúzva, valamint az érvényesség dátuma.
5. **Állapot- és forrásinformáció:** Fejlécben vagy láblécben jelenjen meg az utolsó frissítés dátuma, a listázott termékek száma, valamint a hivatalos SZTFH adatforrás linkje és egy rövid jogi nyilatkozat (tájékoztató jellegű adatok).
6. **Teljesítmény:** Nagy termékszám (több száz / több ezer tétel) esetén is legyen gyors a keresés és a megjelenítés (lapozás vagy „Továbbiak betöltése”).

---

## 5. Elvárt Kimenet
1. A teljes mappastruktúra bemutatása.
2. Minden egyes fájl (`index.html`, `styles.css`, `app.js`, `scripts/update_prices.py`, `requirements.txt`, `.github/workflows/update-prices.yml`, `data/prices.json`, `README.md`) **teljes, hiánytalan, azonnal másolható kódja** (ne használj `// TODO` vagy kihagyott blokkokat).
3. A `README.md`-ben lépésről lépésre leírás a GitHub repó létrehozásához, a GitHub Actions jogosultságok (`Read and write permissions`) beállításához és a GitHub Pages élesítéséhez.

