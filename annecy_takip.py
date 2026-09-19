#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Annecy (Fransa) is ilani takipcisi
GitHub Actions uzerinde calisir, bilgisayarin acik olmasi gerekmez.

Calisma mantigi:
  - siteler_is.txt icindeki arama linklerini gercek bir tarayiciyla acar
  - Yeni ilanlarin sayfasini acip metnini okur
  - filtreler_is.txt'deki kurallara gore eler
      [ISTENMEYEN]       aciklamada gecerse elenir (or. "fluent Portuguese")
      [KESIN ISTENMEYEN] sayfanin her yerinde aranir
      [ZORUNLU]          her satirdan en az bir ifade gecmeli (rol kelimeleri)
  - Portekizce yazilmis ilanlari (dil tahmini ile) eler
  - Ayni ilan tekrar yayinlanirsa parmak izinden tanir, iki kez gondermez
  - Bulduklarini biriktirir, Turkiye saatiyle 19:00'da toplu gonderir

Ortam degiskenleri (GitHub Secrets):
  BOT_TOKEN   - Telegram bot token
  CHAT_ID_IS  - Ilanlarin gidecegi Telegram sohbeti (is botuyla ayni)
"""

import hashlib
import html
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------- ayarlar

SITELER_DOSYASI = "siteler_annecy.txt"
FILTRE_DOSYASI = "filtreler_annecy.txt"
DURUM_DOSYASI = "annecy_durum.json"

ANINDA_GONDER = True              # False olursa ilanlar GONDERIM_SAATI'ne kadar biriktirilir
GONDERIM_SAATI = 19               # Turkiye saatiyle
KUYRUK_MAKS_MESAJ = 40
KAYNAK_BASINA_MAKS_MESAJ = 10
DETAY_LIMITI = 45                 # bir turda en fazla kac ilan sayfasi acilsin
KIMLIK_SAKLAMA_LIMITI = 4000
BOS_UYARI_ESIGI = 3
MESAJ_ONEKI = "🇫🇷 Annecy"        # mesajlarin basinda gorunur
ELE_PORTEKIZCE = False            # Annecy icin dil elemesi yok
ELENENLERI_BILDIR = False

TR_SAAT = timezone(timedelta(hours=3))

TARAYICI_KIMLIGI = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Bilinen sitelerin ilan linki yapisi: (alan adi, desen, link sablonu)
KALIPLAR = [
    ("francetravail.fr", r"/offres/recherche/detail/(\w+)",
     "https://candidat.francetravail.fr/offres/recherche/detail/{id}"),
    ("hellowork.com", r"/emplois/(\d+)\.html", "https://www.hellowork.com/fr-fr/emplois/{id}.html"),
    ("welcometothejungle.com", r"(/fr/companies/[\w\-]+/jobs/[\w\-]+)",
     "https://www.welcometothejungle.com{id}"),
    ("apec.fr", r"/detail-offre/(\d+)",
     "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{id}"),
    ("meteojob.com", r"/emploi/offre/([\w\-]+)", "https://www.meteojob.com/emploi/offre/{id}"),
    ("jobijoba.com", r"/fr/annonces/([\w\-]+)", "https://www.jobijoba.com/fr/annonces/{id}"),
    ("indeed.", r"/(?:viewjob|rc/clk)\?jk=(\w+)", "https://fr.indeed.com/viewjob?jk={id}"),
]

# Bilinmeyen siteler icin genel ilan linki tanima
GENEL_DESEN = (r"/((?:[\w\-]+/)*(?:emploi|emplois|offre|offres|annonce|annonces|poste|"
               r"recrutement|job|jobs|career|careers|position|vacancy)[\w\-]*/[\w\-]{8,})")

ENGEL_ISARETLERI = [
    "captcha", "access denied", "attention required", "are you a robot",
    "pardon our interruption", "unusual traffic", "acesso negado",
    "verifying you are human", "please verify you are a human",
]

JS_TOPLA = r"""
(desen) => {
  const re = new RegExp(desen);
  const cikti = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    if (!re.test(href)) continue;
    let el = a, metin = '';
    for (let i = 0; i < 8 && el; i++) {
      metin = (el.innerText || '').trim();
      if (metin.length > 30) break;
      el = el.parentElement;
    }
    cikti.push({ href: href, metin: metin.slice(0, 600) });
  }
  return cikti;
}
"""

DETAY_SECICILERI = [
    "[data-cy='ad_description']", "[data-testid='ad_description']",
    "[class*='job-description']", "[class*='jobDescription']",
    "[class*='description']", "[id*='description']",
    "[class*='offer-body']", "[class*='vacancy']", "article",
]

JS_DETAY = r"""
(seciciler) => {
  const parca = [];
  const meta = (p) => {
    const el = document.querySelector(`meta[property="${p}"], meta[name="${p}"]`);
    return el ? (el.getAttribute('content') || '') : '';
  };
  const h1 = document.querySelector('h1');
  const baslik = [document.title, meta('og:title'), h1 ? h1.innerText : ''].join(' · ');
  parca.push(document.title, meta('og:title'), meta('og:description'), meta('description'));
  if (h1) parca.push(h1.innerText);
  let bulundu = false;
  for (const s of seciciler) {
    for (const el of document.querySelectorAll(s)) {
      const t = (el.innerText || '').trim();
      if (t.length > 200) { parca.push(t.slice(0, 12000)); bulundu = true; }
    }
    if (bulundu) break;
  }
  const kok = document.querySelector('main') || document.body;
  const tum = ((kok && kok.innerText) || '').slice(0, 20000);
  if (!bulundu) parca.push(tum.slice(0, 6000));
  return { aciklama: parca.join('\n'), tum: parca.join('\n') + '\n' + tum,
           baslik: baslik, bulundu: bulundu };
}
"""

PT_ISARETLERI = [" que ", " para ", " com ", " nao ", " voce ", " experiencia ",
                 " candidato", " candidatura", " empresa ", " oferecemos",
                 " requisitos", " funcao ", " conhecimentos", " sera ", " nossa ",
                 " ofertas ", " salario", " horario"]
EN_ISARETLERI = [" the ", " and ", " you ", " we ", " your ", " will ", " experience ",
                 " requirements", " team ", " role ", " with ", " for ", " our ",
                 " about ", " skills"]


# ---------------------------------------------------------------- yardimcilar

def simdi():
    return datetime.now(TR_SAAT)


def log(mesaj):
    print(f"[{simdi().strftime('%d.%m.%Y %H:%M:%S')}] {mesaj}", flush=True)


def esc(metin):
    return html.escape(str(metin), quote=False)


def normallestir(metin):
    metin = unicodedata.normalize("NFKD", (metin or "").lower())
    metin = "".join(c for c in metin if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", metin)


def _gecer_mi(ifade, n):
    return re.search(r"(?<![a-z0-9])" + re.escape(ifade) + r"(?![a-z0-9])", n) is not None


def yasakli_ifade_bul(metin, filtreler):
    n = normallestir(metin)
    for ifade in filtreler:
        if _gecer_mi(ifade, n):
            return ifade
    return None


def eksik_sart_bul(metin, zorunlu):
    n = normallestir(metin)
    for secenekler in zorunlu:
        if not any(_gecer_mi(x, n) for x in secenekler):
            return secenekler[0]
    return None


def dil_tahmini(metin):
    """Ilan metni agirlikli olarak Portekizce mi Ingilizce mi?"""
    n = " " + normallestir(metin) + " "
    pt = sum(n.count(x) for x in PT_ISARETLERI)
    en = sum(n.count(x) for x in EN_ISARETLERI)
    if pt >= 5 and pt > en * 1.3:
        return "pt"
    if en >= 5 and en > pt:
        return "en"
    return "belirsiz"


def parmak_izi(metin):
    n = normallestir(metin)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()[:80]
    return hashlib.sha1(n.encode()).hexdigest()[:16]


def temizle(metin, sinir=300):
    satirlar = [s.strip() for s in (metin or "").splitlines() if s.strip()]
    goruldu, temiz = set(), []
    for s in satirlar:
        if s in goruldu or len(s) < 2:
            continue
        goruldu.add(s)
        temiz.append(s)
    ozet = " · ".join(temiz)
    return ozet[:sinir] + ("…" if len(ozet) > sinir else "")


# ---------------------------------------------------------------- telegram

def telegram_gonder(metin, onizleme=True):
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID_IS", "").strip()
    if not token or not chat_id:
        log("HATA: BOT_TOKEN veya CHAT_ID_IS tanimli degil.")
        return False

    for _ in range(3):
        try:
            cevap = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": metin, "parse_mode": "HTML",
                      "disable_web_page_preview": not onizleme},
                timeout=20,
            )
            if cevap.status_code == 200:
                return True
            if cevap.status_code == 429:
                bekle = cevap.json().get("parameters", {}).get("retry_after", 5)
                log(f"Telegram yavaslatti, {bekle} sn bekleniyor.")
                time.sleep(bekle + 1)
                continue
            log(f"Telegram hatasi {cevap.status_code}: {cevap.text[:200]}")
            return False
        except requests.RequestException as e:
            log(f"Telegram baglanti hatasi: {e}")
            time.sleep(3)
    return False


# ---------------------------------------------------------------- dosyalar

def siteleri_oku():
    siteler = []
    with open(SITELER_DOSYASI, encoding="utf-8") as f:
        for satir in f:
            satir = satir.strip()
            if not satir or satir.startswith("#"):
                continue
            parcalar = [p.strip() for p in satir.split("|")]
            if len(parcalar) >= 2:
                ad, url = parcalar[0], parcalar[1]
            else:
                url, ad = parcalar[0], urlparse(parcalar[0]).netloc.replace("www.", "")
            if url.startswith("http"):
                siteler.append((ad, url))
    return siteler


def filtreleri_oku():
    istenmeyen, kesin, baslikta, zorunlu = [], [], [], []
    if not os.path.exists(FILTRE_DOSYASI):
        return istenmeyen, kesin, baslikta, zorunlu
    bolum = "ISTENMEYEN"
    with open(FILTRE_DOSYASI, encoding="utf-8") as f:
        for satir in f:
            satir = satir.strip()
            if not satir or satir.startswith("#"):
                continue
            if satir.startswith("[") and satir.endswith("]"):
                bolum = satir.strip("[]").strip().upper()
                continue
            if bolum == "ZORUNLU":
                secenekler = [normallestir(x.strip()) for x in satir.split("/") if x.strip()]
                if secenekler:
                    zorunlu.append(secenekler)
            elif bolum.startswith("KESIN"):
                kesin.append(normallestir(satir))
            elif bolum.startswith("BASLIK"):
                baslikta.append(normallestir(satir))
            else:
                istenmeyen.append(normallestir(satir))
    return istenmeyen, kesin, baslikta, zorunlu


def durum_yukle():
    d = {}
    if os.path.exists(DURUM_DOSYASI):
        try:
            with open(DURUM_DOSYASI, encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            d = {}
    d.setdefault("gorulen", {})
    d.setdefault("imzalar", [])
    d.setdefault("kaynaklar", {})
    d.setdefault("kuyruk", [])
    d.setdefault("son_gonderim", "")
    d.setdefault("sayaclar", {"elenen": 0, "tekrar": 0})
    return d


def durum_kaydet(d):
    for alan, liste in d["gorulen"].items():
        d["gorulen"][alan] = liste[-KIMLIK_SAKLAMA_LIMITI:]
    d["imzalar"] = d["imzalar"][-KIMLIK_SAKLAMA_LIMITI:]
    with open(DURUM_DOSYASI, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def kalip_bul(url):
    """(anahtar, desen, link sablonu) dondurur. Bilinmeyen site ise genel kalip."""
    bolumler = urlparse(url)
    alan_adi = bolumler.netloc.lower()
    for parca, desen, sablon in KALIPLAR:
        if parca in alan_adi:
            return parca, desen, sablon
    taban = f"{bolumler.scheme}://{bolumler.netloc}"
    return alan_adi, GENEL_DESEN, taban + "/{id}"


# ---------------------------------------------------------------- tarama

def sayfa_tara(sayfa, url, desen):
    sayfa.goto(url, timeout=60000, wait_until="domcontentloaded")
    sayfa.wait_for_timeout(6000)
    for _ in range(4):
        sayfa.mouse.wheel(0, 2500)
        sayfa.wait_for_timeout(1200)

    rx = re.compile(desen)
    ilanlar = {}
    for kart in sayfa.evaluate(JS_TOPLA, desen):
        m = rx.search(kart["href"])
        if not m:
            continue
        kimlik = m.group(1)
        if len(kart["metin"]) > len(ilanlar.get(kimlik, "")):
            ilanlar[kimlik] = kart["metin"]
        else:
            ilanlar.setdefault(kimlik, kart["metin"])

    if not ilanlar:
        for m in rx.finditer(sayfa.content()):
            ilanlar.setdefault(m.group(1), "")

    engel = False
    if not ilanlar:
        try:
            ic = (sayfa.title() + " " + sayfa.inner_text("body")[:3000]).lower()
            engel = any(i in ic for i in ENGEL_ISARETLERI)
        except Exception:
            pass
    return ilanlar, engel


def detay_oku(baglam, link):
    sayfa = baglam.new_page()
    try:
        sayfa.goto(link, timeout=45000, wait_until="domcontentloaded")
        sayfa.wait_for_timeout(3500)
        sonuc = sayfa.evaluate(JS_DETAY, DETAY_SECICILERI) or {}
        tum = sonuc.get("tum", "")
        if len(tum) < 150 or any(i in tum.lower()[:3000] for i in ENGEL_ISARETLERI):
            return None
        return sonuc
    except Exception as e:
        log(f"  detay acilamadi: {link} ({e.__class__.__name__})")
        return None
    finally:
        sayfa.close()


def ilan_mesaji(ad, link, metin, not_=None):
    satirlar = [f"<b>💼 {MESAJ_ONEKI} · {esc(ad)}</b>"]
    ozet = temizle(metin)
    if ozet:
        satirlar.append(esc(ozet))
    if not_:
        satirlar.append(f"<i>{esc(not_)}</i>")
    satirlar.append(link)
    return "\n".join(satirlar)


# ---------------------------------------------------------------- ana akis

def main():
    from playwright.sync_api import sync_playwright

    siteler = siteleri_oku()
    if not siteler:
        log("siteler_annecy.txt icinde aktif link yok.")
        return

    durum = durum_yukle()
    filtreler, kesin_filtreler, baslik_filtreleri, zorunlular = filtreleri_oku()
    log(f"{len(filtreler)} istenmeyen, {len(kesin_filtreler)} kesin, "
        f"{len(baslik_filtreleri)} baslik ifadesi, {len(zorunlular)} zorunlu sart yuklendi.")

    ilanlar_msj, ilk_ozet, uyarilar, rapor, elenen_msj = [], [], [], [], []
    elenen_sayisi = tekrar_sayisi = 0
    detay_hakki = DETAY_LIMITI
    imzalar = set(durum["imzalar"])

    with sync_playwright() as p:
        tarayici = p.chromium.launch(headless=True)
        baglam = tarayici.new_context(
            user_agent=TARAYICI_KIMLIGI, locale="fr-FR",
            timezone_id="Europe/Paris", viewport={"width": 1366, "height": 900})

        for ad, url in siteler:
            anahtar, desen, sablon = kalip_bul(url)
            ks = durum["kaynaklar"].setdefault(url, {"ilk": True, "bos": 0, "uyarildi": False})
            gorulen = durum["gorulen"].setdefault(anahtar, [])
            gorulen_kume = set(gorulen)

            sayfa = baglam.new_page()
            try:
                ilanlar, engel = sayfa_tara(sayfa, url, desen)
            except Exception as e:
                log(f"{ad}: sayfa acilamadi ({e.__class__.__name__}: {str(e)[:150]})")
                ilanlar, engel = {}, False
            finally:
                sayfa.close()

            log(f"{ad}: {len(ilanlar)} ilan bulundu" + (" (engel isareti var)" if engel else ""))
            rapor.append((ad, len(ilanlar)))

            if not ilanlar:
                ks["bos"] += 1
                if ks["bos"] >= BOS_UYARI_ESIGI and not ks["uyarildi"]:
                    neden = ("site otomatik erişimi engelliyor gibi görünüyor" if engel
                             else "ilan bulunamadı; link ya da site yapısı değişmiş olabilir")
                    uyarilar.append(f"⚠️ <b>{esc(ad)}</b> son {ks['bos']} turdur boş döndü: {neden}.")
                    ks["uyarildi"] = True
                time.sleep(2)
                continue

            if ks["uyarildi"]:
                uyarilar.append(f"✅ <b>{esc(ad)}</b> yeniden ilan getirmeye başladı.")
            ks["bos"] = 0
            ks["uyarildi"] = False

            yeniler = [(k, t) for k, t in ilanlar.items() if k not in gorulen_kume]
            gorulen.extend(k for k, _ in yeniler)

            if ks["ilk"]:
                ks["ilk"] = False
                ilk_ozet.append(f"• {esc(ad)}: {len(ilanlar)} ilan")
                for k, t in list(ilanlar.items())[:1]:
                    imzalar.add(parmak_izi(t or k))
                time.sleep(2)
                continue

            gonderilecek = fazla = 0
            for k, t in yeniler:
                if gonderilecek >= KAYNAK_BASINA_MAKS_MESAJ:
                    fazla += 1
                    continue
                link = sablon.format(id=k)

                izi = parmak_izi(t or k)
                if t and izi in imzalar:
                    tekrar_sayisi += 1
                    log(f"  tekrar ilan, atlandi: {link}")
                    continue

                sebep = yasakli_ifade_bul(t, filtreler)
                if not sebep and baslik_filtreleri:
                    sebep = yasakli_ifade_bul(t, baslik_filtreleri)
                not_ = None
                if not sebep:
                    if detay_hakki > 0:
                        detay_hakki -= 1
                        detay = detay_oku(baglam, link)
                        if detay is None:
                            not_ = "⚠️ Sayfa okunamadı, filtreler kontrol edilemedi."
                        else:
                            aciklama = detay.get("aciklama", "")
                            hepsi = t + "\n" + detay.get("tum", "")
                            sebep = yasakli_ifade_bul(aciklama, filtreler)
                            if not sebep and baslik_filtreleri:
                                sebep = yasakli_ifade_bul(detay.get("baslik", ""),
                                                          baslik_filtreleri)
                            if not sebep and kesin_filtreler:
                                sebep = yasakli_ifade_bul(hepsi, kesin_filtreler)
                            if not sebep and zorunlular:
                                eksik = eksik_sart_bul(hepsi, zorunlular)
                                if eksik:
                                    sebep = f"şart yok: {eksik}"
                            if not sebep and ELE_PORTEKIZCE and detay.get("bulundu"):
                                if dil_tahmini(aciklama) == "pt":
                                    sebep = "ilan Portekizce"
                    else:
                        not_ = "⚠️ Bu turda sayfa kontrol limiti doldu, filtreler uygulanmadı."

                if sebep:
                    elenen_sayisi += 1
                    log(f"  elendi ({sebep}): {link}")
                    if ELENENLERI_BILDIR:
                        elenen_msj.append(f"🚫 <b>Elendi · {esc(ad)}</b> — “{esc(sebep)}”\n{link}")
                    continue

                imzalar.add(izi)
                durum["imzalar"].append(izi)
                ilanlar_msj.append(ilan_mesaji(ad, link, t, not_=not_))
                gonderilecek += 1

            if fazla > 0:
                ilanlar_msj.append(
                    f"…ve <b>{esc(ad)}</b> sitesinde {fazla} yeni ilan daha:\n{url}")
            time.sleep(2)

        tarayici.close()

    # ---- mesajlar
    if ilk_ozet:
        telegram_gonder(
            "🟢 <b>Annecy iş ilanı takibi başladı</b>\nŞu an listelenen ilanlar kaydedildi, "
            "bundan sonra sadece <b>yeni</b> ilanlar gelecek.\n\n" + "\n".join(ilk_ozet),
            onizleme=False)

    for m in uyarilar + elenen_msj:
        telegram_gonder(m, onizleme=False)
        time.sleep(1)

    sayaclar = durum["sayaclar"]
    sayaclar["elenen"] = sayaclar.get("elenen", 0) + elenen_sayisi
    sayaclar["tekrar"] = sayaclar.get("tekrar", 0) + tekrar_sayisi
    gonderilen = 0

    if ANINDA_GONDER:
        # daha once biriktirilmis ilan kaldiysa once onlari gonder
        bekleyen = durum.get("kuyruk", [])
        if bekleyen:
            telegram_gonder(f"📦 Daha önce biriken {len(bekleyen)} ilan gönderiliyor.",
                            onizleme=False)
            time.sleep(1)
        for m in bekleyen + ilanlar_msj:
            if telegram_gonder(m):
                gonderilen += 1
            time.sleep(1.5)
        durum["kuyruk"] = []
    else:
        durum["kuyruk"].extend(ilanlar_msj)
        log(f"{len(ilanlar_msj)} ilan kuyruga eklendi "
            f"(kuyrukta toplam {len(durum['kuyruk'])}).")

        bugun = simdi().strftime("%Y-%m-%d")
        if simdi().hour >= GONDERIM_SAATI and durum.get("son_gonderim") != bugun:
            kuyruk = durum["kuyruk"]
            durum_satirlari = "\n".join(
                f"• {esc(ad)}: {'✅' if n else '⚠️'} {n} ilan taranıyor" for ad, n in rapor)
            telegram_gonder(
                f"💼 <b>Annecy — bugünün ilanları: {len(kuyruk)}</b>\n"
                f"{sayaclar['elenen']} ilan filtrelere takıldı, "
                f"{sayaclar['tekrar']} ilan daha önce gelmişti.\n\n{durum_satirlari}",
                onizleme=False)
            time.sleep(1)

            for m in kuyruk[:KUYRUK_MAKS_MESAJ]:
                if telegram_gonder(m):
                    gonderilen += 1
                time.sleep(2)

            kalan = kuyruk[KUYRUK_MAKS_MESAJ:]
            if kalan:
                telegram_gonder(f"📦 Bugün {len(kalan)} ilan daha var, linkler aşağıda:",
                                onizleme=False)
                linkler = re.findall(r"https?://\S+", "\n".join(kalan))
                for i in range(0, len(linkler), 15):
                    telegram_gonder("\n".join(linkler[i:i + 15]), onizleme=False)
                    time.sleep(2)

            durum["kuyruk"] = []
            durum["son_gonderim"] = bugun
            sayaclar["elenen"] = 0
            sayaclar["tekrar"] = 0

    durum_kaydet(durum)
    log(f"Tur bitti. {gonderilen} mesaj gonderildi, {elenen_sayisi} elendi, "
        f"{tekrar_sayisi} tekrar.")


if __name__ == "__main__":
    main()
