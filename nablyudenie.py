#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Наблюдение за папкой `ekran`: помечает новые кадры, которые ещё не разобраны.

Запуск:  python ekran/nablyudenie.py          -- разбор новых кадров и вывод в консоль
          python ekran/nablyudenie.py --watch  -- следить непрерывно, дописывать в СВОДКА.md

Как работает:
  - ведёт файл отметок ekran/.razbrano.json с именами уже просмотренных кадров;
  - новые кадры разбивает на группы по "похожести" соседних картинок
    (сравение уменьшенных копий, дешёвое и быстрое);
  - на каждую группу печатает одну строку-подсказку со временем и именем первого кадра.

Сам разбор содержимого (что именно на экране) делается агентом глазами —
скрипт только отсеивает дубли, чтобы не смотреть 656 почти одинаковых файлов.
"""

import json
import os
import sys
import time
from pathlib import Path

from PIL import Image

PAVKA = Path(__file__).resolve().parent
OTMETKI = PAVKA / ".razbrano.json"
SVODKA = PAVKA / "СВОДКА.md"

# маленькая метка для сравнения кадров между собой
PODPIS = 64
# доля пикселей, где кадр считается отличным от первого кадра группы.
#
# Порог подобран по замерам на реальных кадрах этой папки: у соседних
# снимков медиана различия ~0.16, и это не шум — на экране идёт живой стрим
# вывода агентов, текст и счётчики меняются постоянно. При пороге 0.22
# получалось 403 группы из 777 кадров (то есть отсечения почти не работали),
# при 0.50 — 119 срезов: это уже обозримое число для разбора глазами.
POROG = 0.50


def spisok_kadrov():
    """Все PNG в папке, отсортированные по времени записи на диск.

    Сортируем именно по времени файла, а не по имени: в папке лежат кадры
    двух разных форматов имён (120210-00000.png и 20261014-1324-00-....png),
    и сортировка по имени перемешивает хронологию.
    """
    # Расширение спросим у папки: съёмка может писать .png или .jpg,
    # и группировщик, ищущий только .png, ослепнет молча.
    kadry = [f for rasshirenie in ("*.png", "*.jpg")
             for f in PAVKA.glob(rasshirenie)]
    return sorted(
        kadry,
        key=lambda p: (p.stat().st_mtime, p.name),
    )


def vremya_iz_imeni(p):
    """Время кадра 'ЧЧ:ММ:СС'.

    В папке встречаются два формата имён:
      новый — 20261014-1324-00-274587-044.png
      старый — 120210-00000.png
    ВНИМАНИЕ: в новом формате группа после часов-минут всегда '-00-',
    то есть секунд там нет вовсе (это ложное поле, не время).
    Часы и минуты берём из имени, а секунды — из времени записи файла на диск.
    """
    imya = p.name
    ch = mm = None
    try:
        if imya.startswith("20") and len(imya) > 13 and imya[8] == "-":
            ch, mm = imya[9:11], imya[11:13]
        elif len(imya) >= 6 and imya[:2].isdigit() and imya[2:4].isdigit():
            ch, mm = imya[0:2], imya[2:4]
    except Exception:
        pass
    if not (ch and ch.isdigit() and mm and mm.isdigit()):
        ss = time.strftime("%H:%M:%S", time.localtime(p.stat().st_mtime))
        return ss
    try:
        sek = time.strftime("%S", time.localtime(p.stat().st_mtime))
    except Exception:
        sek = "00"
    return f"{ch}:{mm}:{sek}"


def sklonenie(n, one, few, mnogo):
    """Русское склонение: 1 кадр, 2 кадра, 5 кадров."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {mnogo}"


def metka(p):
    """Маленькое уменьшенное серое изображение — отпечаток кадра."""
    with Image.open(p) as im:
        im = im.convert("L")
        im.thumbnail((PODPIS, PODPIS))
        try:
            return list(im.get_flattened_data())
        except AttributeError:
            return list(im.getdata())


def raznica(a, b):
    """Доля пикселей, где отпечатки отличаются сильнее порога чувствительности."""
    if not a or len(a) != len(b):
        return 1.0
    sladko = 12
    raznye = sum(1 for x, y in zip(a, b) if abs(x - y) > sladko)
    return raznye / len(a)


def prochitannye():
    """Множество имён кадров, уже отмеченных как разобранные."""
    if OTMETKI.exists():
        try:
            return set(json.loads(OTMETKI.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def sohranit(vybrannye):
    OTMETKI.write_text(
        json.dumps(sorted(vybrannye), ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def vypis_stroku(kadry, vremya):
    """Группа кадров -> строка для консоли и для СВОДКА.md."""
    pervyy, posledniy = kadry[0], kadry[-1]
    opisanie = "один кадр"
    if len(kadry) > 1:
        opisanie = f"{sklonenie(len(kadry), 'кадр', 'кадра', 'кадров')} подряд"
    return (
        f"- `{vremya}` · {opisanie} · начало: `{pervyy.name}` · конец: `{posledniy.name}`"
    )


def razobrat_novye(gruppy, pometki):
    """Печатает новые группы и помечает их кадры как разобранные."""
    novyh = 0
    stroki = []
    vsego = sum(len(g) for g, _ in gruppy)
    for kadry, vremya in gruppy:
        stroki.append(vypis_stroku(kadry, vremya))
        for k in kadry:
            pometki.add(k.name)
        novyh += 1
    if stroki:
        print(f"\nНовых экранов: {novyh} (пропущено дублей: {vsego} кадров)\n")
        for s in stroki:
            print(s)
    else:
        print("Новых кадров нет.")
    sohranit(pometki)
    return stroki


def gruppovat(files):
    """Делит список файлов на группы соседних похожих кадров.

    Сравниваем каждый кадр не с предыдущим, а с ЯКОРМ — первым кадром группы.
    Иначе мелкий шум (курсор, счётчик секунд) накапливается кадр за кадром,
    группа «уползает» от исходного снимка и в итоге режется на куски,
    хотя экран всё это время был один и тот же.
    """
    gruppy = []
    tekushchaya = []
    yakor = None
    for f in files:
        try:
            m = metka(f)
        except Exception:
            continue
        if yakor is not None and raznica(yakor, m) > POROG:
            if tekushchaya:
                gruppy.append(tekushchaya)
            tekushchaya = []
            yakor = m
        if not tekushchaya:
            yakor = m
        tekushchaya.append(f)
    if tekushchaya:
        gruppy.append(tekushchaya)
    return [(g, vremya_iz_imeni(g[0])) for g in gruppy]


def main():
    rezhim = "--watch" in sys.argv
    vse = spisok_kadrov()
    pometki = prochitannye()
    novye = [f for f in vse if f.name not in pometki]
    print(f"В папке кадров: {len(vse)}   уже разобрано: {len(pometki)}   новых: {len(novye)}")

    stroki = razobrat_novye(gruppovat(novye), pometki)

    if stroki and SVODKA.exists():
        s = "\n".join(stroki)
        with SVODKA.open("a", encoding="utf-8") as f:
            f.write(f"\n### Добавлено {time.strftime('%H:%M:%S')}\n\n{s}\n")
        print(f"\nДописано в {SVODKA.name}")

    if not rezhim:
        return

    print("\nСлежу за папкой, Ctrl+C — остановить.")
    izvestno = len(vse)
    try:
        while True:
            time.sleep(2)
            vse = spisok_kadrov()
            if len(vse) == izvestno:
                continue
            novye = [f for f in vse if f.name not in pometki]
            if not novye:
                izvestno = len(vse)
                continue
            stroki = razobrat_novye(gruppovat(novye), pometki)
            if stroki and SVODKA.exists():
                with SVODKA.open("a", encoding="utf-8") as f:
                    f.write(f"\n### Добавлено {time.strftime('%H:%M:%S')}\n\n" + "\n".join(stroki) + "\n")
                print(f"Дописано в {SVODKA.name}")
            izvestno = len(vse)
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()