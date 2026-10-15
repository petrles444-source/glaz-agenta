#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
umnoe_vision — анализатор скриншотов рабочего стола для ИИ-агента.

Задача: агент смотрит на PNG рабочего стола и должен за секунды понять
ЧТО ВАЖНО, а не получить простыню сырого OCR. Поэтому модуль делает три вещи:

  1. ТРАНСКРИБЦИЯ. Русский + английский в одном проходе (tesseract rus+eng).
     Поддерживается движок RapidOCR как запасной (но он китайско-английский,
     кириллицу не тянет — об этом честно сказано в отчёте движка).

  2. РАЗБОР. Из текста вытаскиваются ссылки, почта, IP, пути, номера,
     даты, коды ошибок, деньги, идентификаторы.

  3. ОТСЕВ МУСОРА. Панель задач, часы, трей, мелкий шрифт и повторяющиеся
     строки выбрасываются. Остаётся то, что менялось и на что стоит смотреть.

Дальше — приоритизация: строки с ошибками и ссылками всплывают наверх,
потому что именно они обычно решают задачу агента.

Запуск:
    python umnoe_vision.py КАДР.png              разбор одного файла
    python umnoe_vision.py ПАПКА --top 40        разбор папки, 40 самых важных
    python umnoe_vision.py ПАПКА --watch         следить за новыми кадрами
    python umnoe_vision.py КАДР.png --json out.json   машинный вывод
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Движки распознавания
# ─────────────────────────────────────────────────────────────────────────────

TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Языковые данные кладём в проект, а не в Program Files — туда нет доступа на запись.
# Ставим русский (rus) + английский (eng), иначе кириллица не читается вовсе.
def _найти_tessdata() -> Path:
    """Ищет языковые данные, поднимаясь вверх по дереву папок.

    Копия этого файла может лежать не в корне проекта, а в папке
    агента. Если считать путь только от файла, копия в agents/BASIL/
    будет искать языки у себя и не найдёт: русский текст перестанет
    читаться, а это самая частая беда.
    """
    тут = Path(__file__).resolve()
    for база in (тут.parent, *тут.parents):
        путь = база / "tools" / "tessdata"
        if (путь / "rus.traineddata").is_file():
            return путь
    return тут.parent / "tools" / "tessdata"


TESSDATA = str(_найти_tessdata())

_dvigatel_zagruzhen = None


def tesseract_exe():
    """Путь к tesseract: сначала как ставили, потом из PATH."""
    if os.path.exists(TESSERACT_EXE):
        return TESSERACT_EXE
    from shutil import which
    return which("tesseract")


def est_tesseract():
    exe = tesseract_exe()
    if not exe:
        return False
    # проверяем именно наличие языка, а не просто файла
    rus = Path(TESSDATA) / "rus.traineddata"
    return rus.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Структуры данных
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Blok:
    """Один распознанный блок текста с координатами на экране."""
    tekst: str
    x: int
    y: int
    sh: int          # ширина
    vy: int          # высота
    uverenost: float  # 0..1
    vazhno: float = 0.0
    pouchki: list[str] = field(default_factory=list)

    @property
    def nizh(self):
        return self.y + self.vy

    def slovo(self):
        return self.tekst.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Шаблоны для разбора содержимого
# ─────────────────────────────────────────────────────────────────────────────

RE_SSYLKA = re.compile(
    r"""(?xi)
    \b(
        (?:https?://|www\.)[^\s<>"'\)\]]+
      | (?:vk\.com|vk\.ru|youtube\.com|youtu\.be|github\.com|gitlab\.com)
        /[^\s<>"'\)\]]*
      | [a-z0-9][a-z0-9\-]*\.(?:com|ru|net|org|io|su|do\.am|am|info|site|pro)
        (?:/[^\s<>"'\)\]]*)?
      | [a-z0-9\-]+\.(?:do\.am)[^\s]*
    )
    """
)

RE_POCHTA = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

RE_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")

RE_PUT = re.compile(
    r"""(?xi)
    (?:
        [A-Z]:\\[^\s<>"']+
      | /[a-z0-9_\-./]{3,}
      | \b[\w-]+\.(?:py|json|md|txt|html|log|yaml|yml|csv|js|css|png)\b
    )
    """
)

RE_NOMER = re.compile(r"(?<![\w.])\d[\d\s.,]{0,17}\d(?![\w.])")

RE_VREMYA = re.compile(
    r"\b(?:"
    r"[01]?\d|2[0-3][:][0-5]\d(?:[:][0-5]\d)?"
    r"|(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)"
    r"[ьяе]*\s+\d{1,2}"
    r"|\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)"
    r"[ьяе]*"
    r"|\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?"
    r"|\d{2}\.\d{2}\.\d{4}"
    r"|\d{1,2}:\d{2}:\d{2}\s*(?:am|pm|AM|PM)"
    r"|\d+\s*(?:мин|сек|час|ч|м|kb|mb|gb|ms)"
    r")\b",
    re.IGNORECASE,
)

# коды ошибок: HTTP, 4xx/5xx, Exit code, errno и т.п.
RE_KOD_OSHIBKI = re.compile(
    r"(?xi)"
    r"\b("
    r"[45]\d{2}"                                  # HTTP-подобные
    r"|(?:exit|error|code)\s*(?:code)?\s*[:=]?\s*-?\d+"
    r"|(?:errno|код)\s*[:=]?\s*\d+"
    r"|(?:traceback|стектрейс|exception)"
    r"|(?:WIN32|FILE_NOT_FOUND|PERMISSION_DENIED|TIMEOUT|ECONNREFUSED)"
    r")\b"
)

RE_DENGI = re.compile(r"(?i)(?:[$€₽]|\bруб\.?|\brub\b|\busd\b)\s?\d[\d\s.,]*|\d[\d\s.,]*\s?(?:руб|р\.|₽)")

RE_IDENTIFIKATOR = re.compile(
    r"(?i)\b(?:[a-f0-9]{7,40}|NN-\d+|ART-\d+|[A-Z]{2,}-\d{2,})\b"
)

# Слова-маркеры важности. Вес = насколько строка вероятно требует внимания.
MARKEY = {
    9.0: ["traceback", "стектрейс", "stack trace", "fatal", "паника",
          "critical", "segmentation fault"],
    7.0: ["ошибка", "error", "exception", "failed", "failure", "сбой",
          "не работает", "не открывается", "отказ", "permission denied",
          "access denied", "503", "500", "404", "timeout", "таймаут"],
    5.5: ["предупреждение", "warning", "deprecated", "устарел",
          "неопределён", "undefined", "nan", "null pointer"],
    5.0: ["работает", "готово", "готова", "готовы", "сделано", "выполнено",
          "успешно", "success", "passed", "окончательно", "завершено"],
    4.5: ["ждёт", "ожидает", "завис", "blocked", "очередь", "queue",
          "работа", "working", "running", "строит", "собирает"],
    4.0: ["задача", "заявка", "требование", "todo", "план", "отчёт",
          "report", "сводка", "итог"],
    3.5: ["гость", "папк", "файл", "файлы", "директор", "folder", "file",
          "path", "путь"],
    3.0: ["отправ", "получ", "письм", "почт", "mail", "message", "сообщен"],
}

RE_MARKER = {
    "ошибка": re.compile(r"(?i)\b(error|ошибк|exception|failed|сбой|отказ)\b"),
    "предупреждение": re.compile(r"(?i)\b(warn|предупреж|устарел|deprecated)\b"),
    "успех": re.compile(r"(?i)\b(ok|готово|готова|успешн|success|passed|выполнен)\b"),
    "работа": re.compile(r"(?i)\b(work|работа|running|working|build|сборк|запуск)\b"),
    "статус": re.compile(r"(?i)\b(status|статус|готов|готовы|состояни)\b"),
    "ожидание": re.compile(r"(?i)\b(ожида|жд[её]т|queue|очеред|pending|wait)\b"),
}

# Строки интерфейса, которые не несут смысла для агента
SHUM_MARKEY = re.compile(
    r"(?i)^("
    r"меню|file|edit|view|окно|справка|закрыть|минимизировать|развернуть"
    r"|поиск|панель задач|громкость|сеть|батарея|уведомлен"
    r"|copilot|ctrl\+|shift\+|alt\+|tab$"
    r")\s*$"
)

RE_CLOCK = re.compile(r"^\s*[\d]{1,2}[:.][\d]{2}([:.][\d]{2})?\s*$")
RE_TRASH = re.compile(r"(?i)^(\d+[.,]\d+\s*(kb|mb|gb|байт|б)|\d{1,2}:\d{2})$")


# ─────────────────────────────────────────────────────────────────────────────
# Движок OCR
# ─────────────────────────────────────────────────────────────────────────────

def _init_tesseract():
    """Одноразовая инициализация pytesseract."""
    global _dvigatel_zagruzhen
    if _dvigatel_zagruzhen is None:
        import pytesseract
        exe = tesseract_exe()
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        # tesseract должен брать языковые данные из проекта, а не из Program Files
        if Path(TESSDATA).exists():
            os.environ["TESSDATA_PREFIX"] = TESSDATA
            pytesseract.pytesseract.tessdata_dir = TESSDATA
        _dvigatel_zagruzhen = True
    return _dvigatel_zagruzhen


def ocr_tesseract(pil, yazyki="rus+eng", psm=None):
    """Распознавание через tesseract. Русский и английский одним проходом."""
    _init_tesseract()
    import pytesseract
    from pytesseract import Output

    config = []
    if psm is not None:
        config.append(f"--psm {psm}")

    dannye = pytesseract.image_to_data(
        pil,
        lang=yazyki,
        config=" ".join(config),
        output_type=Output.DICT,
    )

    slova = dannye.get("text", [])
    bloki = []
    for i, slovo in enumerate(slova):
        if not slovo or not slovo.strip():
            continue
        try:
            uver = float(danniye_i_uver(dannye, i))
        except Exception:
            uver = 0.0
        x = int(danniye_i(dannye, "left", i))
        y = int(danniye_i(dannye, "top", i))
        w = int(danniye_i(dannye, "width", i))
        h = int(danniye_i(dannye, "height", i))
        bloki.append((slovo.strip(), x, y, w, h, uver))
    return sloki_v_bloki(bloki)


def danniye_i(dannye, klyuch, i):
    arr = dannye.get(klyuch)
    return arr[i] if arr is not None and i < len(arr) else 0


def danniye_i_uver(dannye, i):
    return float(dannye.get("conf", [0] * (i + 1))[i])


def sloki_v_bloki(sloki):
    """Склеивает слова в строки по вертикальному перекрытию.

    Важная тонкость: у слов ОДНОЙ визуальной строки координата y разная,
    потому что tesseract measured каждое слово по своим крайним буквам.
    Например «проверяй» (верхние выносные) даёт y=22, а «каждые» — y=25.
    Если сортировать просто по (y, x), все слова с y=22 встанут перед всеми
    словами с y=25, и строка перемешается: получится «проверяй 2 своей делай
    быстрые каждые секунды». Поэтому сначала группируем слова в строки по
    перекрытию по вертикали, и только потом сортируем внутри строки по x.
    """
    if not sloki:
        return []

    # 1. Сортируем по верхней границе и обходим сверху вниз.
    uporyadochennye = sorted(sloki, key=lambda s: (s[2], s[1]))

    stroki = []          # список строк; строка = список слов
    granicy = []         # (y_верх, y_низ) каждой накопленной строки

    for slovo in uporyadochennye:
        _, x, y, w, h, u = slovo
        vysota = h or 12
        y_niz = y + vysota

        namer = None
        for i, (yv, yn) in enumerate(granicy):
            # перекрытие по вертикали относительно меньшей высоты
            peresecheni = min(yn, y_niz) - max(yv, y)
            menshaya = min(yn - yv, vysota)
            if menshaya > 0 and peresecheni / menshaya >= 0.5:
                namer = i
                break

        if namer is None:
            stroki.append([slovo])
            granicy.append([y, y_niz])
        else:
            stroki[namer].append(slovo)
            granicy[namer][0] = min(granicy[namer][0], y)
            granicy[namer][1] = max(granicy[namer][1], y_niz)

    # 2. Каждая строка — это абзац из одной области; режем по большим вертикальным разрывам.
    stroki.sort(key=lambda s: min(w[2] for w in s))

    rezultat = []
    for stroka in stroki:
        # внутри строки порядок строго по x — это и есть порядок чтения
        stroka = sorted(stroka, key=lambda s: s[1])

        # Экран часто разделён на колонки (например панель агентов слева
        # и браузер справа). Слова из разных колонок попадают в одну строку
        # по вертикали, и в тексте они склеиваются в кашу. Разрываем строку
        # там, где между соседними словами пропасть заметно шире их самих.
        kolonki = razbit_na_kolonki(stroka)

        for chast in kolonki:
            tekst = " ".join(s[0] for s in chast)
            x = min(s[1] for s in chast)
            y = min(s[2] for s in chast)
            kon = max(s[1] + s[3] for s in chast)
            verh = max(s[2] + s[4] for s in chast)
            uver = sum(s[5] for s in chast) / len(chast)
            rezultat.append(Blok(tekst, x, y, kon - x, verh - y, uver))
    return rezultat


def razbit_na_kolonki(stroka):
    """Режет строку слов на части по большим горизонтальным провалам."""
    if len(stroka) < 2:
        return [stroka]

    # типичная ширина буквы в этой строке
    shiriny = [s[3] for s in stroka if s[3] > 0]
    if not shiriny:
        return [stroka]
    srednyaya = sorted(shiriny)[len(shiriny) // 2]

    # пропасть считаем колонкой, если она шире 2.5 средних букв и 40 пикселей
    porog = max(srednyaya * 2.5, 40)

    chasti = []
    tekushchaya = [stroka[0]]
    for pred, sled in zip(stroka, stroka[1:]):
        prist = pred[1] + pred[3]          # правый край предыдущего слова
        razryv = sled[1] - prist          # горизонтальная пропасть
        if razryv > porog:
            chasti.append(tekushchaya)
            tekushchaya = [sled]
        else:
            tekushchaya.append(sled)
    chasti.append(tekushchaya)
    return chasti


def ocr_rapid(pil):
    """Запасной движок. Кириллицу НЕ поддерживает — честно помечаем в отчёте."""
    from rapidocr_onnxruntime import RapidOCR
    global _rapid
    try:
        _rapid
    except NameError:
        _rapid = RapidOCR()

    res, _ = _rapid(np.array(pil))
    if not res:
        return []
    out = []
    for box, tekst, uver in res:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, y0 = int(min(xs)), int(min(ys))
        w, h = int(max(xs) - x0), int(max(ys) - y0)
        out.append(Blok(tekst, x0, y0, w, h, float(uver)))
    return out


import numpy as np  # noqa: E402  (нужен для rapidocr)


# ─────────────────────────────────────────────────────────────────────────────
# Анализ содержимого одного кадра
# ─────────────────────────────────────────────────────────────────────────────

def panel_zadach(vysota):
    """Нижняя полоса — панель задач. Всё, что в ней, — шум."""
    return max(0, vysota - 48)


def yabloko_poley(f):
    """Прямоугольник верхней панели окон (заголовки вкладок)."""
    return 0 <= f.y <= 56


def otseivat_shum(bloki, vysota_kadra):
    """Убирает панель задач, мусорные подписи и совсем ненадёжные строки."""
    niz = panel_zadach(vysota_kadra)
    chistye = []
    for b in bloki:
        if b.nizh > niz and b.y > niz:      # целиком в панели задач
            continue
        t = b.slovo()
        if not t or len(t) < 2:
            continue
        if SHUM_MARKEY.match(t):
            continue
        if RE_CLOCK.match(t) and b.vy < 34:
            continue
        if RE_TRASH.match(t) and b.vy < 28:
            continue
        if b.uverenost < 0.30 and len(t) < 6:
            continue
        chistye.append(b)
    return chistye


def razobrat_stroku(b):
    """Достаёт из строки ссылки, числа, коды и прочие ценности."""
    t = b.tekst
    b.pouchki = []

    ss = []
    for m in RE_SSYLKA.findall(t):
        ss.append(m.rstrip(".,;:!?)]}"))
    if ss:
        b.pouchki.extend(f"ссылка: {x}" for x in ss)

    for m in RE_POCHTA.findall(t):
        b.pouchki.append(f"почта: {m}")
    for m in RE_IP.findall(t):
        b.pouchki.append(f"адрес: {m}")
    for m in RE_KOD_OSHIBKI.findall(t):
        b.pouchki.append(f"код: {m}")
    for m in RE_DENGI.findall(t):
        b.pouchki.append(f"сумма: {m.strip()}")
    for m in RE_PUT.findall(t):
        if len(m) > 4:
            b.pouchki.append(f"путь: {m}")
    for m in RE_VREMYA.findall(t):
        b.pouchki.append(f"время: {m.strip()}")
    ids = RE_IDENTIFIKATOR.findall(t)
    if ids:
        b.pouchki.extend(f"ид: {i}" for i in ids[:4])

    chisla = [c.strip() for c in RE_NOMER.findall(t) if len(c.strip()) > 1]
    if chisla:
        b.pouchki.extend(f"число: {c}" for c in chisla[:6])

    nizhn = b.tekst.lower()
    for ves, slova in MARKEY.items():
        for slovo in slova:
            if slovo in nizhn:
                b.vazhno = max(b.vazhno, ves)
                b.pouchki.append(f"маркер: {slovo}")

    if b.pouchki:
        b.vazhno += min(2.5, 0.45 * (len(b.pouchki) - 1))

    # текст в самом верху — заголовок окна, полезно знать
    if yabloko_poley(b):
        b.vazhno += 0.6

    return b


def vse_chto_est(f):
    """Собирает из блоков плоский список интересного."""
    ss, pochty, ip, puti, kody, vremena, chisla, summy = (set() for _ in range(8))
    for b in f.bloki:
        for p in b.pouchki:
            if p.startswith("ссылка:"):
                ss.add(p[7:])
            elif p.startswith("почта:"):
                pochty.add(p[6:])
            elif p.startswith("адрес:"):
                ip.add(p[6:])
            elif p.startswith("путь:"):
                puti.add(p[5:])
            elif p.startswith("код:"):
                kody.add(p[4:])
            elif p.startswith("время:"):
                vremena.add(p[6:])
            elif p.startswith("число:"):
                chisla.add(p[6:])
            elif p.startswith("сумма:"):
                summy.add(p[6:])
    return {
        "ssylki": sorted(ss),
        "pochty": sorted(pochty),
        "adresa": sorted(ip),
        "puti": sorted(puti)[:25],
        "kody": sorted(kody),
        "vremya": sorted(vremena)[:15],
        "chisla": sorted(chisla)[:25],
        "sumy": sorted(summy),
    }


def sostoyanie(f):
    """Светофор по состоянию кадра — что вообще происходит."""
    vesy = {}
    for b in f.bloki:
        nizh = b.tekst.lower()
        for imya, rx in RE_MARKER.items():
            if rx.search(nizh):
                vesy[imya] = vesy.get(imya, 0.0) + b.vazhno + 1.0
    if not vesy:
        return {"itog": "нейтрально", "detali": {}}

    glavnoe = max(vesy, key=vesy.get)
    porog = vesy[glavnoe] * 0.55
    znachimye = {k: round(v, 1) for k, v in vesy.items() if v >= porog}

    if "ошибка" in znachimye and vesy.get("ошибка", 0) >= vesy.get("успех", 0):
        itog = "ошибка"
    elif "успех" in znachimye:
        itog = "успех"
    elif "ожидание" in znachimye:
        itog = "ожидание"
    elif "работа" in znachimye:
        itog = "работа"
    elif "предупреждение" in znachimye:
        itog = "предупреждение"
    else:
        itog = "нейтрально"

    return {"itog": itog, "detali": znachimye}


@dataclass
class Kadr:
    """Разобранный снимок экрана."""
    put: str = ""
    vremya: str = ""
    razmer: tuple = (0, 0)
    bloki: list = field(default_factory=list)
    vse: dict = field(default_factory=dict)
    sost: dict = field(default_factory=dict)
    dvigatel: str = ""
    sekundy: float = 0.0
    hesh: str = ""

    def vazhnye(self, n=25):
        """Строки, отсортированные по важности: сверху — самое ценное."""
        kop = sorted(self.bloki, key=lambda b: -b.vazhno)
        return kop[:n]

    def v_kratkoe(self, n=12):
        """Короткая выжимка: заголовок + главное."""
        chasti = []
        for b in self.vazhnye(n):
            t = b.slovo()
            if len(t) > 150:
                t = t[:147] + "..."
            chasti.append(f"  [{b.vazhno:4.1f}] {t}")
        return "\n".join(chasti)


# ─────────────────────────────────────────────────────────────────────────────
# Разбор файла
# ─────────────────────────────────────────────────────────────────────────────

def hesh_faila(p):
    h = hashlib.md5()
    st = os.stat(p)
    h.update(str(st.st_size).encode())
    h.update(str(int(st.st_mtime)).encode())
    return h.hexdigest()


def vremya_iz_imeni(p):
    imya = p.name
    try:
        if imya.startswith("20") and len(imya) > 13 and imya[8] == "-":
            ch, mm = imya[9:11], imya[11:13]
            if ch.isdigit() and mm.isdigit():
                sek = time.strftime("%S", time.localtime(os.stat(p).st_mtime))
                return f"{ch}:{mm}:{sek}"
        if len(imya) >= 6 and imya[:2].isdigit() and imya[2:4].isdigit():
            sek = time.strftime("%S", time.localtime(os.stat(p).st_mtime))
            return f"{imya[0:2]}:{imya[2:4]}:{sek}"
    except Exception:
        pass
    return time.strftime("%H:%M:%S", time.localtime(os.stat(p).st_mtime))


def razobrat_kadr(p, dvigatel=None, cache=None):
    """Главная точка входа: путь к PNG -> объект Kadr."""
    from PIL import Image

    p = Path(p)
    hh = hesh_faila(p)
    if cache is not None and hh in cache:
        k = cache[hh]
        k.put = str(p)
        return k

    t0 = time.time()
    with Image.open(p) as im:
        im = im.convert("RGB")
        shir, vys = im.size

        if dvigatel is None:
            dvigatel = "tesseract" if est_tesseract() else "rapidocr"

        try:
            if dvigatel == "tesseract":
                bloki = ocr_tesseract(im)
                nazv = "tesseract rus+eng"
            else:
                bloki = ocr_rapid(im)
                nazv = "rapidocr (без кириллицы!)"
        except Exception as e:
            bloki = []
            nazv = f"ОШИБКА {type(e).__name__}: {e}"

    bloki = otseivat_shum(bloki, vys)
    for b in bloki:
        razobrat_stroku(b)

    k = Kadr(
        put=str(p),
        vremya=vremya_iz_imeni(p),
        razmer=(shir, vys),
        bloki=bloki,
        vse=vse_chto_est(type("O", (), {"bloki": bloki})()),
        sost=sostoyanie(type("O", (), {"bloki": bloki})()),
        dvigatel=nazv,
        sekundy=round(time.time() - t0, 2),
        hesh=hh,
    )
    if cache is not None:
        cache[hh] = k
    return k


# ─────────────────────────────────────────────────────────────────────────────
# Отчёт
# ─────────────────────────────────────────────────────────────────────────────

def otchet(k, n=20, vse=True):
    """Человекочитаемый отчёт по кадру."""
    L = []
    L.append("═" * 72)
    L.append(f"КАДР  {k.vremya}   {k.put}")
    L.append(f"размер {k.razmer[0]}×{k.razmer[1]}   движок {k.dvigatel}   {k.sekundy} с")
    L.append(f"СОСТОЯНИЕ: {k.sost.get('itog','?').upper()}"
             f"   {k.sost.get('detali',{})}")
    L.append(f"блоков после отсева мусора: {len(k.bloki)}")
    L.append("─" * 72)
    L.append("ГЛАВНОЕ (по убыванию важности):")
    for b in k.vazhnye(n):
        t = b.slovo()
        if len(t) > 160:
            t = t[:157] + "..."
        L.append(f"  [{b.vazhno:4.1f}] y={b.y:4d} {t}")
        if b.pouchki:
            L.append(f"          → {'; '.join(b.pouchki[:6])}")

    if vse:
        v = k.vse
        L.append("─" * 72)
        L.append("ИЗВЛЕЧЁННЫЕ ДАННЫЕ:")
        for klyuch, zagolovok in (
            ("ssylki", "ССЫЛКИ"), ("pochty", "ПОЧТА"), ("adresa", "IP"),
            ("puti", "ПУТИ"), ("kody", "КОДЫ ОШИБОК"), ("vremya", "ВРЕМЯ"),
            ("chisla", "ЧИСЛА"), ("sumy", "СУММЫ"),
        ):
            if v.get(klyuch):
                pokaz = v[klyuch][:14]
                L.append(f"  {zagolovok} ({len(v[klyuch])}): {', '.join(pokaz)}")
    L.append("═" * 72)
    return "\n".join(L)


def kak_json(k, n=40):
    return {
        "put": k.put,
        "vremya": k.vremya,
        "razmer": list(k.razmer),
        "dvigatel": k.dvigatel,
        "sekundy": k.sekundy,
        "sostoyanie": k.sost,
        "vse": k.vse,
        "stroki": [
            {
                "tekst": b.slovo(),
                "x": b.x, "y": b.y, "sh": b.sh, "vy": b.vy,
                "uverenost": round(b.uverenost, 3),
                "vazhno": round(b.vazhno, 2),
                "pouchki": b.pouchki,
            }
            for b in k.vazhnye(n)
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Пакетная обработка и наблюдение
# ─────────────────────────────────────────────────────────────────────────────

def spisok_kadrov(papka, kazhdyy=1):
    """Кадры по времени записи, а не по имени (имена двух форматов).

    kazhdyy>1 — брать каждый N-й кадр. Нужен для больших папок: полный
    разбор 850 кадров по 4 секунды на кадр — это почти час ожидания.
    """
    fayly = sorted(
        Path(papka).glob("*.png"),
        key=lambda p: (os.stat(p).st_mtime, p.name),
    )
    if kazhdyy > 1:
        fayly = fayly[::kazhdyy]
    return fayly


def razobrati_papku(papka, top=15, cache=None, vse=True, tiho=False, kazhdyy=1):
    """Разбирает папку и печатает сводку по самым важным кадрам."""
    vsego = len(spisok_kadrov(papka))
    fayly = spisok_kadrov(papka, kazhdyy)
    if not fayly:
        print(f"В папке {papka} нет PNG.")
        return []

    if not tiho:
        print(f"Кадров в папке: {vsego}")
        if kazhdyy > 1:
            print(f"Берём каждый {kazhdyy}-й: {len(fayly)} кадров")
        print(f"Движок: {'tesseract rus+eng' if est_tesseract() else 'rapidocr (без кириллицы!)'}")
        print("Разбираю...\n")

    karty = []
    for i, f in enumerate(fayly, 1):
        try:
            k = razobrat_kadr(f, cache=cache)
            karty.append(k)
        except Exception as e:
            print(f"  ! {f.name}: {type(e).__name__}: {e}")
        if not tiho and i % 25 == 0:
            print(f"  ... {i}/{len(fayly)}")

    # ранжируем кадры по суммарной важности
    def reiting(k):
        oshibki = sum(1 for b in k.bloki if any("маркер: ошибка" in p for p in b.pouchki))
        ssylki = len(k.vse.get("ssylki", []))
        vazhnyh = sum(1 for b in k.bloki if b.vazhno >= 5)
        return oshibki * 4 + ssylki * 2 + vazhnyh

    reit = sorted(karty, key=reiting, reverse=True)

    if not tiho:
        print(f"\nГотово. Самые содержательные кадры из {len(karty)}:\n")
    for k in reit[:top]:
        print(otchet(k, n=10, vse=vse))
        print()
    return reit


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Анализатор скриншотов: OCR rus+eng, ссылки, цифры, коды, важное.",
    )
    ap.add_argument("put", help="PNG-файл или папка с кадрами")
    ap.add_argument("--top", type=int, default=12, help="сколько кадров показать (для папки)")
    ap.add_argument("--strok", type=int, default=20, help="сколько строк на кадр")
    ap.add_argument("--json", metavar="FILE", help="выгрузить разбор в JSON")
    ap.add_argument("--dvigatel", choices=["tesseract", "rapidocr", "auto"],
                    default="auto", help="движок OCR")
    ap.add_argument("--kratko", action="store_true", help="только главное, без списков")
    ap.add_argument("--watch", action="store_true",
                    help="следить за папкой и разбирать новые кадры")
    ap.add_argument("--interval", type=float, default=2.0, help="пауза в watch-режиме")
    ap.add_argument("--kazhdyy", type=int, default=1,
                    help="разбирать каждый N-й кадр папки (для больших папок)")
    args = ap.parse_args()

    if args.dvigatel == "auto":
        dv = "tesseract" if est_tesseract() else "rapidocr"
    else:
        dv = args.dvigatel

    if dv == "tesseract" and not est_tesseract():
        print("! tesseract не найден либо нет rus.traineddata — беру rapidocr.")
        print("  rapidocr не читает кириллицу, качество будет низким.")
        dv = "rapidocr"

    p = Path(args.put)

    if p.is_dir():
        cache = {}
        if args.watch:
            nablyudat(p, dv, cache, args)
        else:
            razobrati_papku(p, top=args.top, cache=cache,
                            vse=not args.kratko, kazhdyy=args.kazhdyy)
    elif p.is_file():
        k = razobrat_kadr(p, dvigatel=dv)
        print(otchet(k, n=args.strok, vse=not args.kratko))
        if args.json:
            Path(args.json).write_text(
                json.dumps(kak_json(k), ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"\nJSON записан: {args.json}")
    else:
        print(f"Не найдено: {p}")
        sys.exit(1)


def nablyudat(papka, dvigatel, cache, args):
    """Следит за папкой и разбирает каждый новый кадр."""
    print(f"Слежу за {papka}, пауза {args.interval} с. Ctrl+C — остановить.")
    izvestno = {p.name for p in spisok_kadrov(papka)}
    while True:
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nОстановлено.")
            return
        vse = spisok_kadrov(papka)
        novye = [p for p in vse if p.name not in izvestno]
        if not novye:
            continue
        izvestno = {p.name for p in vse}
        for f in novye:
            try:
                k = razobrat_kadr(f, dvigatel=dvigatel, cache=cache)
                print()
                print(otchet(k, n=args.strok, vse=not args.kratko))
            except Exception as e:
                print(f"! {f.name}: {type(e).__name__}: {e}")
        if args.json:
            posledniy = novye[-1]
            k = razobrat_kadr(posledniy, dvigatel=dvigatel, cache=cache)
            Path(args.json).write_text(
                json.dumps(kak_json(k), ensure_ascii=False, indent=2),
                encoding="utf-8")


if __name__ == "__main__":
    main()