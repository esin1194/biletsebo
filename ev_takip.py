#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lizbon kiralik ev takipcisi
GitHub Actions uzerinde calisir, bilgisayarin acik olmasi gerekmez.

Calisma mantigi:
  - siteler.txt icindeki her arama linkini gercek bir tarayiciyla acar
  - Sayfadaki ilan linklerini toplar, daha once gorulmemis olanlari
    Telegram'a gonderir (fiyat + kisa ozet + link; Telegram fotografi
    link onizlemesinden kendisi gosterir)
  - Her yeni ilanin sayfasini acip aciklamasini okur; filtreler.txt'deki
    istenmeyen ifadelerden biri geciyorsa ilani gondermez
  - Bir site ilk kez eklendiginde mevcut ilanlari "goruldu" sayar,
    sadece ozet ve bir ornek ilan gonderir (ilk turda mesaj yagmuru olmaz)
  - Bir site ust uste bos donerse (engellendi / link bozuk) bir kez uyarir
  - Her sabah "hala calisiyorum" ozeti atar

Ortam degiskenleri (GitHub Secrets'tan gelir):
  BOT_TOKEN  - Telegram bot token
  CHAT_ID    - Telegram chat id
"""

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

SITELER_DOSYASI = "siteler.txt"
DURUM_DOSYASI = "ev_durum.json"
FILTRE_DOSYASI = "filtreler.txt"

MAKS_FIYAT = 2000                 # bunun ustundeki ilanlar gonderilmez
FIYAT_FILTRESI_UYGULANMAYAN = ["airbnb."]   # Airbnb fiyatlari gecelik/toplam karisik
KAYNAK_BASINA_MAKS_MESAJ = 8      # bir turda tek siteden en fazla bu kadar mesaj
KIMLIK_SAKLAMA_LIMITI = 4000      # site basina hatirlanan ilan sayisi
BOS_UYARI_ESIGI = 3               # kac tur ust uste bos donerse uyarilsin
GUNLUK_OZET_SAATI = 9             # Turkiye saatiyle
DETAY_LIMITI = 40                 # bir turda en fazla kac ilan sayfasi acilsin
ZORUNLU_UYGULANMAYAN = ["airbnb."]   # Airbnb evleri zaten hep esyali
ELENENLERI_BILDIR = False         # True yapilirsa elenen ilanlar da kisaca bildirilir

# Ilan sayfasinda aciklamanin bulundugu yerler (siteye gore)
DETAY_SECICILERI = [
    ".comment", ".details-property_features",                 # idealista
    "[data-cy='adPageAdDescription']",                        # imovirtual
    "[data-cy='ad_description']", "[data-testid='ad_description']",  # olx
    "[data-section-id='DESCRIPTION_DEFAULT']",                # airbnb
    "[data-section-id='OVERVIEW_DEFAULT_V2']",
    "[data-section-id='OVERVIEW_DEFAULT']",
    "[data-testid*='description']", "[class*='description']", # diger siteler
]

TR_SAAT = timezone(timedelta(hours=3))

TARAYICI_KIMLIGI = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# (alan adi parcasi, ilan linki deseni, ilan linki sablonu)
# Desenin 1. grubu ilanin kimligidir.
KALIPLAR = [
    ("idealista.pt", r"/imovel/(\d+)", "https://www.idealista.pt/imovel/{id}/"),
    ("imovirtual.com", r"/(?:pt/anuncio|en/ad)/([\w\-]+?-ID\w+)",
     "https://www.imovirtual.com/pt/anuncio/{id}"),
    ("olx.pt", r"/d/anuncio/([\w\-]+?-ID\w+)\.html",
     "https://www.olx.pt/d/anuncio/{id}.html"),
    ("spotahome.com", r"/for-rent:([\w\-]+/\d+)",
     "https://www.spotahome.com/lisbon/for-rent:{id}"),
    ("uniplaces.com", r"/accommodation/lisbon/(\d+)",
     "https://www.uniplaces.com/accommodation/lisbon/{id}"),
    ("housinganywhere.com", r"/room/(ut\d+)", "https://housinganywhere.com/room/{id}"),
    ("airbnb.", r"/rooms/(\d+)", "https://www.airbnb.com/rooms/{id}"),
]

ENGEL_ISARETLERI = [
    "captcha", "access denied", "attention required", "are you a robot",
    "pardon our interruption", "unusual traffic", "acesso negado",
    "verifying you are human", "please verify you are a human",
]

# Sayfadaki ilan linklerini ve her ilanin kart metnini toplayan kod
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
      if (metin.length > 40 && /€|EUR/.test(metin)) break;
      el = el.parentElement;
    }
    cikti.push({ href: href, metin: metin.slice(0, 600) });
  }
  return cikti;
}
"""

JS_DETAY = r"""
(seciciler) => {
  const parca = [];
  const meta = (p) => {
    const el = document.querySelector(`meta[property="${p}"], meta[name="${p}"]`);
    return el ? (el.getAttribute('content') || '') : '';
  };
  parca.push(document.title, meta('og:title'), meta('og:description'), meta('description'));
  const h1 = document.querySelector('h1');
  if (h1) parca.push(h1.innerText);
  let bulundu = false;
  for (const s of seciciler) {
    for (const el of document.querySelectorAll(s)) {
      const t = (el.innerText || '').trim();
      if (t.length > 20) { parca.push(t.slice(0, 8000)); bulundu = true; }
    }
  }
  const kok = document.querySelector('main') || document.body;
  const tum = ((kok && kok.innerText) || '').slice(0, 20000);
  if (!bulundu) parca.push(tum.slice(0, 5000));
  return { aciklama: parca.join('\n'), tum: parca.join('\n') + '\n' + tum };
}
"""

FIYAT_RE = re.compile(
    r"€\s?(\d{1,3}(?:[.,\s]\d{3})+|\d+)|(\d{1,3}(?:[.,\s]\d{3})+|\d+)\s?€"
)


# ---------------------------------------------------------------- yardimcilar

def simdi():
    return datetime.now(TR_SAAT)


def log(mesaj):
    print(f"[{simdi().strftime('%d.%m.%Y %H:%M:%S')}] {mesaj}", flush=True)


def esc(metin):
    return html.escape(str(metin), quote=False)


def fiyat_bul(metin):
    """Kart metnindeki ilk makul fiyati (>=100 €) dondurur."""
    for m in FIYAT_RE.finditer(metin or ""):
        ham = m.group(1) or m.group(2)
        try:
            sayi = int(re.sub(r"[.,\s]", "", ham))
        except ValueError:
            continue
        if sayi >= 100:
            return sayi
    return None


def temizle(metin, sinir=280):
    satirlar = [s.strip() for s in (metin or "").splitlines() if s.strip()]
    goruldu, temiz = set(), []
    for s in satirlar:
        if s in goruldu or len(s) < 2 or re.fullmatch(r"\d+\s*/\s*\d+", s):
            continue
        goruldu.add(s)
        temiz.append(s)
    ozet = " · ".join(temiz)
    return ozet[:sinir] + ("…" if len(ozet) > sinir else "")


def normallestir(metin):
    """Kucuk harf, aksansiz, tek bosluk: 'Proprietário' -> 'proprietario'."""
    metin = unicodedata.normalize("NFKD", (metin or "").lower())
    metin = "".join(c for c in metin if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", metin)


def filtreleri_oku():
    """
    [ISTENMEYEN] bolumu: biri gecerse ilan elenir.
    [ZORUNLU] bolumu: her satir bir sarttir, satirdaki / ile ayrilmis
    seceneklerden en az biri gecmelidir. Birden fazla satir = hepsi gerekli.
    """
    istenmeyen, zorunlu = [], []
    if not os.path.exists(FILTRE_DOSYASI):
        return istenmeyen, zorunlu
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
            else:
                istenmeyen.append(normallestir(satir))
    return istenmeyen, zorunlu


def _gecer_mi(ifade, n):
    return re.search(r"(?<![a-z0-9])" + re.escape(ifade) + r"(?![a-z0-9])", n) is not None


def yasakli_ifade_bul(metin, filtreler):
    n = normallestir(metin)
    for ifade in filtreler:
        if _gecer_mi(ifade, n):
            return ifade
    return None


def eksik_sart_bul(metin, zorunlu):
    """Karsilanmayan ilk sartin ilk secenegini dondurur; hepsi tamamsa None."""
    n = normallestir(metin)
    for secenekler in zorunlu:
        if not any(_gecer_mi(x, n) for x in secenekler):
            return secenekler[0]
    return None


def para(sayi):
    return f"{sayi:,}".replace(",", ".") + " €"


# ---------------------------------------------------------------- telegram

def telegram_gonder(metin, onizleme=True):
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    if not token or not chat_id:
        log("HATA: BOT_TOKEN veya CHAT_ID tanimli degil.")
        return False

    for deneme in range(3):
        try:
            cevap = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": metin,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": not onizleme,
                },
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
            if "|" in satir:
                ad, url = [p.strip() for p in satir.split("|", 1)]
            else:
                url = satir
                ad = urlparse(url).netloc.replace("www.", "")
            if url.startswith("http"):
                siteler.append((ad, url))
    return siteler


def durum_yukle():
    if os.path.exists(DURUM_DOSYASI):
        try:
            with open(DURUM_DOSYASI, encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            d = {}
    else:
        d = {}
    d.setdefault("gorulen", {})
    d.setdefault("kaynaklar", {})
    d.setdefault("gunluk", {"tarih": simdi().strftime("%Y-%m-%d"), "gonderilen": 0})
    return d


def durum_kaydet(d):
    for alan, liste in d["gorulen"].items():
        d["gorulen"][alan] = liste[-KIMLIK_SAKLAMA_LIMITI:]
    with open(DURUM_DOSYASI, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def kalip_bul(url):
    alan_adi = urlparse(url).netloc.lower()
    for parca, desen, sablon in KALIPLAR:
        if parca in alan_adi:
            return parca, desen, sablon
    return None


# ---------------------------------------------------------------- tarama

def sayfa_tara(sayfa, url, desen):
    sayfa.goto(url, timeout=60000, wait_until="domcontentloaded")
    sayfa.wait_for_timeout(6000)
    for _ in range(5):
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

    # Link bulunamadiysa sayfa kaynagina gomulu verilere bak
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
    """Ilan sayfasini acip aciklama metnini dondurur. Okunamazsa None."""
    sayfa = baglam.new_page()
    try:
        sayfa.goto(link, timeout=45000, wait_until="domcontentloaded")
        sayfa.wait_for_timeout(3500)
        sonuc = sayfa.evaluate(JS_DETAY, DETAY_SECICILERI) or {}
        tum = sonuc.get("tum", "")
        kucuk = tum.lower()
        if len(tum) < 150 or any(i in kucuk[:3000] for i in ENGEL_ISARETLERI):
            return None
        return sonuc
    except Exception as e:
        log(f"  detay acilamadi: {link} ({e.__class__.__name__})")
        return None
    finally:
        sayfa.close()


def ilan_mesaji(ad, link, metin, fiyat, ornek=False, not_=None):
    baslik = "🔎 Örnek ilan" if ornek else "🏠 Yeni ilan"
    satirlar = [f"<b>{baslik} · {esc(ad)}</b>"]
    if fiyat:
        satirlar.append(f"💶 {para(fiyat)}")
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
        log("siteler.txt icinde aktif link yok.")
        return

    durum = durum_yukle()
    filtreler, zorunlular = filtreleri_oku()
    log(f"{len(filtreler)} istenmeyen ifade, {len(zorunlular)} zorunlu sart yuklendi.")
    ilanlar_msj, ornekler, ilk_ozet, uyarilar, rapor = [], [], [], [], []
    elenen_msj = []
    elenen_sayisi = 0
    detay_hakki = DETAY_LIMITI

    with sync_playwright() as p:
        tarayici = p.chromium.launch(headless=True)
        baglam = tarayici.new_context(
            user_agent=TARAYICI_KIMLIGI,
            locale="pt-PT",
            timezone_id="Europe/Lisbon",
            viewport={"width": 1366, "height": 900},
        )

        for ad, url in siteler:
            kalip = kalip_bul(url)
            if not kalip:
                log(f"{ad}: bu site desteklenmiyor, atlaniyor.")
                continue
            alan, desen, sablon = kalip

            ks = durum["kaynaklar"].setdefault(
                url, {"ilk": True, "bos": 0, "uyarildi": False})
            gorulen = durum["gorulen"].setdefault(alan, [])
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
                    neden = ("site otomatik erisimi engelliyor gibi görünüyor"
                             if engel else
                             "ilan bulunamadı; link bozulmuş ya da site yapısı değişmiş olabilir")
                    uyarilar.append(
                        f"⚠️ <b>{esc(ad)}</b> son {ks['bos']} turdur boş döndü: {neden}.")
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
                k, t = next(iter(ilanlar.items()))
                ornekler.append(ilan_mesaji(ad, sablon.format(id=k), t, fiyat_bul(t), ornek=True))
                time.sleep(2)
                continue

            fiyat_serbest = any(x in alan for x in FIYAT_FILTRESI_UYGULANMAYAN)
            aktif_zorunlu = [] if any(x in alan for x in ZORUNLU_UYGULANMAYAN) else zorunlular
            filtre_var = bool(filtreler or aktif_zorunlu)
            uygun = []
            for k, t in yeniler:
                fiyat = fiyat_bul(t)
                if fiyat and fiyat > MAKS_FIYAT and not fiyat_serbest:
                    continue
                uygun.append((k, t, fiyat))

            gonderilecek = 0
            fazla = 0
            for k, t, f in uygun:
                if gonderilecek >= KAYNAK_BASINA_MAKS_MESAJ:
                    fazla += 1
                    continue
                link = sablon.format(id=k)

                # once kart metnine bak (sayfa acmaya gerek kalmayabilir)
                sebep = yasakli_ifade_bul(t, filtreler) if filtreler else None
                not_ = None
                if not sebep and filtre_var:
                    if detay_hakki > 0:
                        detay_hakki -= 1
                        detay = detay_oku(baglam, link)
                        if detay is None:
                            not_ = "⚠️ Açıklama okunamadı, filtreler kontrol edilemedi."
                        else:
                            aciklama = detay.get("aciklama", "")
                            sebep = yasakli_ifade_bul(aciklama, filtreler)
                            if not sebep and aktif_zorunlu:
                                eksik = eksik_sart_bul(t + "\n" + detay.get("tum", ""), aktif_zorunlu)
                                if eksik:
                                    sebep = f"şart yok: {eksik}"
                            if f is None:
                                f = fiyat_bul(aciklama) if not fiyat_serbest else None
                                if f and f > MAKS_FIYAT:
                                    sebep = f"fiyat {para(f)}"
                    else:
                        not_ = "⚠️ Bu turda sayfa kontrol limiti doldu, açıklama okunmadı."

                if sebep:
                    elenen_sayisi += 1
                    log(f"  elendi ({sebep}): {link}")
                    if ELENENLERI_BILDIR:
                        elenen_msj.append(
                            f"🚫 <b>Elendi · {esc(ad)}</b> — “{esc(sebep)}”\n{link}")
                    continue

                ilanlar_msj.append(ilan_mesaji(ad, link, t, f, not_=not_))
                gonderilecek += 1

            if fazla > 0:
                ilanlar_msj.append(
                    f"…ve <b>{esc(ad)}</b> sitesinde {fazla} yeni ilan daha. Hepsi burada:\n{url}")

            time.sleep(2)

        tarayici.close()

    # ---- mesajlari gonder
    if ilk_ozet:
        telegram_gonder(
            "🟢 <b>Lizbon ev takibi başladı</b>\n"
            "Şu an listelenen ilanlar kaydedildi, bundan sonra sadece "
            "<b>yeni</b> ilanlar gelecek.\n\n" + "\n".join(ilk_ozet),
            onizleme=False)
        for m in ornekler:
            telegram_gonder(m)
            time.sleep(1)

    for m in uyarilar:
        telegram_gonder(m, onizleme=False)
        time.sleep(1)

    for m in elenen_msj:
        telegram_gonder(m, onizleme=False)
        time.sleep(1)

    gonderilen = 0
    for m in ilanlar_msj:
        if telegram_gonder(m):
            gonderilen += 1
        time.sleep(1.2)

    # ---- gunluk ozet
    bugun = simdi().strftime("%Y-%m-%d")
    gunluk = durum["gunluk"]
    gunluk["gonderilen"] = gunluk.get("gonderilen", 0) + gonderilen
    gunluk["elenen"] = gunluk.get("elenen", 0) + elenen_sayisi
    if gunluk.get("tarih") != bugun and simdi().hour >= GUNLUK_OZET_SAATI:
        durum_satirlari = "\n".join(
            f"• {esc(ad)}: {'✅' if n else '⚠️'} {n} ilan görünüyor" for ad, n in rapor)
        telegram_gonder(
            f"☀️ <b>Günaydın, takip çalışıyor.</b>\n"
            f"Son özetten bu yana {gunluk['gonderilen']} yeni ilan gönderildi, "
            f"{gunluk['elenen']} ilan filtreye takılıp elendi.\n\n{durum_satirlari}",
            onizleme=False)
        gunluk["tarih"] = bugun
        gunluk["gonderilen"] = 0
        gunluk["elenen"] = 0

    durum_kaydet(durum)
    log(f"Tur bitti. {gonderilen} yeni ilan gonderildi.")


if __name__ == "__main__":
    main()
